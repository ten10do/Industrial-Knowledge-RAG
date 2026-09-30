"""Research-only Final-Gate holdout allocation and stratified DEV sampling.

Inputs are qualification records produced by ``qualification``.  Outputs are
pure planning artifacts: a document-level holdout plan, a DEV sample manifest,
and a canonical digest.  No AI output here is ever Gold.
"""

from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .qualification import canonical_digest

# Slice sampling priority bands (P0 highest).
SLICE_PRIORITY = {
    "multi_column": "P0",
    "merged_header": "P0",
    "model_comparison": "P0",
    "unit_column": "P0",
    "borderless_table": "P1",
    "parameter_code_table": "P1",
    "fault_code_table": "P1",
    "cross_page": "P1",
    "section_table": "P1",
    "simple_table": "P2",
    "mixed_layout": "P2",
    "ordinary_text": "P3",
}

# DEV slice coverage targets (section 10 of the phase brief).
DEV_SLICE_TARGETS = {
    "ordinary_text": (3, 4),
    "simple_table": (3, 5),
    "borderless_table": (5, 7),
    "model_comparison": (6, 8),
    "multi_column": (7, 10),
    "merged_header": (6, 8),
    "unit_column": (5, 7),
    "parameter_code_table": (4, 6),
    "fault_code_table": (4, 6),
    "section_table": (4, 6),
    "mixed_layout": (4, 6),
    "cross_page": (3, 4),
}

# Human review priority order (section 22).
REVIEW_PRIORITY_ORDER = (
    "multi_column",
    "merged_header",
    "model_comparison",
    "unit_column",
    "borderless_table",
    "parameter_code_table",
    "fault_code_table",
    "section_table",
    "cross_page",
    "ordinary_text",
)

MIN_RESERVE_DOCS = 3
MAX_PAGES_PER_DOC_DEFAULT = 6
MIN_DEV_DOCS = 6


def load_candidates(pages_jsonl: str | Path) -> list[dict[str, Any]]:
    candidates = []
    with Path(pages_jsonl).open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                candidates.append(json.loads(line))
    return candidates


def slice_availability_by_doc(candidates: list[dict[str, Any]]) -> dict[str, set[str]]:
    availability: dict[str, set[str]] = defaultdict(set)
    for item in candidates:
        for slice_name in item["slices"]:
            availability[item["doc_id"]].add(slice_name)
    return availability


def allocate_final_gate_holdout(
    candidates: list[dict[str, Any]],
    doc_vendors: dict[str, str],
    *,
    min_docs: int = MIN_RESERVE_DOCS,
    min_dev_docs: int = 8,
) -> dict[str, Any]:
    """Document-level holdout.  Greedy: prefer large documents whose removal
    keeps every slice available in at least two remaining DEV-eligible docs."""
    availability = slice_availability_by_doc(candidates)
    doc_pages = Counter(item["doc_id"] for item in candidates)
    docs = sorted(doc_pages, key=lambda doc: (-doc_pages[doc], doc))
    slice_doc_count = Counter()
    for doc, slices in availability.items():
        for slice_name in slices:
            slice_doc_count[slice_name] += 1

    reserve: list[str] = []
    remaining = set(docs)
    max_reserve = max(0, len(docs) - min_dev_docs)
    for doc in docs:
        if len(reserve) >= min(min_docs, max_reserve):
            break
        would_remaining = remaining - {doc}
        safe = True
        for slice_name, doc_count in slice_doc_count.items():
            if slice_name == "ordinary_text":
                continue
            covering = sum(1 for other in would_remaining if slice_name in availability.get(other, set()))
            if slice_doc_count[slice_name] - covering > 0 and covering < 2:
                safe = False
                break
        if safe:
            reserve.append(doc)
            remaining.discard(doc)
    # Guarantee the minimum even if safety checks blocked greedy picks, while
    # keeping the DEV pool at min_dev_docs when the corpus allows it.
    for doc in docs:
        if len(reserve) >= min_docs:
            break
        if len(remaining) <= min_dev_docs:
            break
        if doc not in reserve:
            reserve.append(doc)
            remaining.discard(doc)
    reserve.sort()
    dev_docs = sorted(remaining)
    return {
        "strategy": "document_level_greedy_preserving_slice_availability",
        "reserve_docs": reserve,
        "reserve_vendors": sorted({doc_vendors.get(doc, "unknown") for doc in reserve}),
        "reserve_pages": sum(doc_pages[doc] for doc in reserve),
        "dev_eligible_docs": dev_docs,
        "dev_eligible_pages": sum(doc_pages[doc] for doc in dev_docs),
        "FINAL_GATE_UNCONSUMED": True,
    }


def _review_priority(slices: list[str]) -> int:
    positions = [REVIEW_PRIORITY_ORDER.index(s) for s in slices if s in REVIEW_PRIORITY_ORDER]
    return min(positions) if positions else len(REVIEW_PRIORITY_ORDER)


def _slice_deficit_gain(slices: set[str], coverage: Counter[str]) -> int:
    gain = 0
    for slice_name in slices:
        low, high = DEV_SLICE_TARGETS.get(slice_name, (0, 99))
        gain += max(0, high - coverage[slice_name])
    return gain


def select_dev_sample(
    candidates: list[dict[str, Any]],
    holdout: dict[str, Any],
    doc_vendors: dict[str, str],
    *,
    target_pages: int = 36,
    max_pages_per_doc: int = MAX_PAGES_PER_DOC_DEFAULT,
    seed: int = 42,
) -> dict[str, Any]:
    dev_docs = set(holdout["dev_eligible_docs"])
    pool = [item for item in candidates if item["doc_id"] in dev_docs and item["text_layer_available"]]
    coverage: Counter[str] = Counter()
    doc_page_count: Counter[str] = Counter()
    selected: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, int]] = set()

    structural = [item for item in pool if any(s != "ordinary_text" for s in item["slices"])]
    # Deterministic ordering with a seeded tiebreak for equal-risk candidates.
    rng = random.Random(seed)
    for item in structural:
        item["_tiebreak"] = rng.random()

    reasons: dict[tuple[str, int], str] = {}

    def try_select(item: dict[str, Any], reason: str) -> bool:
        key = (item["doc_id"], item["page_number"])
        if key in selected_keys or len(selected) >= target_pages:
            return False
        if doc_page_count[item["doc_id"]] >= max_pages_per_doc:
            return False
        selected.append(item)
        selected_keys.add(key)
        doc_page_count[item["doc_id"]] += 1
        coverage.update(item["slices"])
        reasons[key] = reason
        return True

    # Round 1: rare slices first (slices whose target is not yet covered and
    # that are scarce across the DEV pool), highest risk first.
    slice_doc_count = Counter()
    for item in pool:
        for slice_name in item["slices"]:
            if slice_name != "ordinary_text":
                slice_doc_count[slice_name] += 1
    rare_slices = sorted(
        (s for s, n in slice_doc_count.items() if n <= 2),
        key=lambda s: (SLICE_PRIORITY.get(s, "P3"), s),
    )
    for rare in rare_slices:
        options = sorted(
            (item for item in structural if rare in item["slices"]),
            key=lambda item: (-item["layout_risk_score"], item["_tiebreak"]),
        )
        for option in options:
            if coverage[rare] >= DEV_SLICE_TARGETS.get(rare, (1, 1))[1] or len(selected) >= target_pages:
                break
            try_select(option, f"rare_slice:{rare}")

    # Round 2: greedy coverage of P0..P2 targets weighted by deficit gain.
    blocked_keys: set[tuple[str, int]] = set()
    while len(selected) < target_pages:
        best = None
        best_key = None
        for item in structural:
            key = (item["doc_id"], item["page_number"])
            if key in selected_keys or key in blocked_keys:
                continue
            slices = set(item["slices"])
            deficit = _slice_deficit_gain(slices, coverage)
            if deficit <= 0:
                continue
            priority_rank = min(SLICE_PRIORITY.get(s, "P3") for s in slices)
            rank_key = (-deficit, priority_rank, -item["layout_risk_score"], item["_tiebreak"])
            if best_key is None or rank_key < best_key:
                best, best_key = item, rank_key
        if best is None:
            break
        gained = [s for s in best["slices"] if DEV_SLICE_TARGETS.get(s, (0, 0))[0] > coverage[s]]
        if not try_select(best, "stratified_coverage:" + ",".join(sorted(gained))):
            blocked_keys.add((best["doc_id"], best["page_number"]))

    # Round 3: ordinary_text balance (never dominant).
    ordinary_pool = [item for item in pool if item["slices"] == ["ordinary_text"]]
    ordinary_by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in ordinary_pool:
        ordinary_by_doc[item["doc_id"]].append(item)
    for doc_id in sorted(ordinary_by_doc):
        if coverage["ordinary_text"] >= DEV_SLICE_TARGETS["ordinary_text"][1] or len(selected) >= target_pages:
            break
        if doc_page_count[doc_id] >= max_pages_per_doc:
            continue
        item = ordinary_by_doc[doc_id][0]
        try_select(item, "ordinary_text_balance")

    # Round 4: fill toward target with remaining high-risk pages.
    remainder = sorted(
        (item for item in structural if (item["doc_id"], item["page_number"]) not in selected_keys),
        key=lambda item: (-item["layout_risk_score"], item["_tiebreak"]),
    )
    for item in remainder:
        if len(selected) >= target_pages:
            break
        try_select(item, "high_risk_fill")

    selected.sort(key=lambda item: (item["doc_id"], item["page_number"]))

    # Cross-page continuation units: page N selected with cross_page label and
    # a table-bearing page N+1 joins the annotation unit.
    pages_by_doc: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for item in candidates:
        pages_by_doc[item["doc_id"]][item["page_number"]] = item
    continuation_notes = []
    for item in list(selected):
        if "cross_page" not in item["slices"]:
            continue
        nxt = pages_by_doc[item["doc_id"]].get(item["page_number"] + 1)
        if nxt and nxt["detected_table_count"] and (nxt["doc_id"], nxt["page_number"]) not in selected_keys:
            continuation_notes.append(
                {
                    "anchor": f"{item['doc_id']}:p{item['page_number']}",
                    "continuation": f"{nxt['doc_id']}:p{nxt['page_number']}",
                }
            )

    manifest_pages = []
    for item in selected:
        manifest_pages.append(
            {
                "sample_id": f"{item['doc_id']}_p{item['page_number']:04d}",
                "sanitized_doc_id": item["doc_id"],
                "page_number": item["page_number"],
                "slice_labels": item["slices"],
                "layout_risk_score": item["layout_risk_score"],
                "selection_reason": reasons.get((item["doc_id"], item["page_number"]), ""),
                "review_priority": _review_priority(item["slices"]),
            }
        )
    vendor_counts = Counter(doc_vendors.get(doc, "unknown") for doc in doc_page_count)
    plan = {
        "split": "DEV",
        "target_pages": target_pages,
        "selected_pages": len(selected),
        "dev_documents": sorted(doc_page_count),
        "dev_document_count": len(doc_page_count),
        "dev_vendor_count": len(vendor_counts),
        "vendor_counts": dict(sorted(vendor_counts.items())),
        "max_pages_from_single_manual": max(doc_page_count.values(), default=0),
        "slice_distribution": dict(sorted(coverage.items())),
        "continuation_units": continuation_notes,
        "pages": manifest_pages,
    }
    plan["dev_sample_manifest_digest"] = canonical_digest(
        {k: v for k, v in plan.items() if k != "dev_sample_manifest_digest"}
    )
    return plan
