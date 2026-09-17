"""Research-only ownership risk classification and owner review queue.

Classifies prediction relationships into LOW / MEDIUM / HIGH / AMBIGUOUS for
REVIEW PRIORITIZATION ONLY.  Classification never mutates parser output,
never sets human_confirmed and never creates Gold.  Outputs are new files
inside the private workspace; existing prediction/page/annotation files are
never rewritten.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .annotation_workspace import MODEL_PATTERN, UNIT_PATTERN
from .qualification import PARAMETER_CODE_PATTERNS, FAULT_CODE_PATTERNS

RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "AMBIGUOUS")
PRIMARY_OPTION_PREFERENCE = ("suggestion_2", "suggestion_1", "flat_baseline")

SECONDARY_REVIEW_RATIO = 0.2

TABLE_STRUCTURE_STATUSES = ("UNREVIEWED", "ACCEPTED", "CORRECTED")
OWNERSHIP_STATUSES = ("UNREVIEWED", "ACCEPTED", "REJECTED", "AMBIGUOUS")


def _any_match(patterns: tuple[Any, ...], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _spans_columns(bbox: list[float], column_boxes: list[list[float]]) -> int:
    """Count columns whose center x lies inside the header cell bbox.

    Center-based detection avoids flagging a single-column header whose text
    box merely grazes the wide union-box of a neighbouring column.
    """
    x0, _, x1, _ = bbox
    if x1 <= x0:
        return 0
    hit = 0
    for cx0, _, cx1, _ in column_boxes:
        center = (cx0 + cx1) / 2
        if x0 + 1.0 <= center <= x1 - 1.0:
            hit += 1
    return hit


def classify_option(
    option: dict[str, Any],
    page_slices: list[str],
    page_evidence: list[str] | None = None,
) -> dict[str, Any]:
    """Classify every relationship of one prediction option.

    ``page_evidence`` carries qualification discovery evidence.  A cross-page
    HIGH flag requires an explicit continuation marker; a page that was only
    flagged via the consecutive-table heuristic stays MEDIUM and is reported
    as unverified so the owner can judge it.
    """
    cells = {cell["cell_id"]: cell for cell in option.get("cells", [])}
    blocks = {block["block_id"]: block for block in option.get("blocks", [])}
    columns = {column["column_id"]: column for column in option.get("columns", [])}
    column_boxes = [column["bbox"] for column in columns.values()]
    in_degree: Counter[str] = Counter()
    for edge in option.get("relationships", []):
        if edge.get("relationship_type") == "HEADER_APPLIES_TO":
            in_degree[edge["target_id"]] += 1
    cross_page_page = "cross_page" in page_slices and (
        page_evidence is None or "explicit_continuation_marker" in page_evidence
    )
    cross_page_unverified = (
        "cross_page" in page_slices and not cross_page_page
    )

    classified = []
    for edge in option.get("relationships", []):
        reasons: list[str] = []
        risk = "MEDIUM"
        edge_type = edge.get("relationship_type", "")
        source_id = edge.get("source_id", "")
        target_id = edge.get("target_id", "")
        source = cells.get(source_id) or blocks.get(source_id) or {}
        target = cells.get(target_id) or blocks.get(target_id) or {}
        source_text = (source.get("text") or "").strip()
        target_text = (target.get("text") or "").strip()
        confidence = float(edge.get("confidence", 1.0))

        if edge_type == "AMBIGUOUS":
            risk = "AMBIGUOUS"
            reasons.append("parser_marked_ambiguous")
        else:
            if in_degree[target_id] > 1:
                risk = "HIGH"
                reasons.append("multiple_parent_candidates")
            if source_id and _spans_columns(source.get("bbox", []), column_boxes) >= 2:
                risk = "HIGH"
                reasons.append("cross_column_span_or_merged_header")
            if source_text and UNIT_PATTERN.search(source_text):
                risk = "HIGH"
                reasons.append("unit_header_binding")
            if MODEL_PATTERN.search(source_text) or MODEL_PATTERN.search(target_text):
                risk = "HIGH"
                reasons.append("model_column_binding")
            if _any_match(PARAMETER_CODE_PATTERNS, source_text) and _any_match(
                PARAMETER_CODE_PATTERNS, target_text
            ):
                risk = "HIGH"
                reasons.append("parameter_code_composite_row")
            if _any_match(FAULT_CODE_PATTERNS, source_text) or _any_match(
                FAULT_CODE_PATTERNS, target_text
            ):
                risk = "HIGH"
                reasons.append("fault_code_composite_row")
            if cross_page_page:
                risk = "HIGH"
                reasons.append("cross_page_continuation_page")
            elif cross_page_unverified:
                if risk != "HIGH":
                    risk = "MEDIUM"
                reasons.append("consecutive_table_pages_unverified")
            if risk != "HIGH":
                target_box = target.get("bbox")
                if target_box:
                    overlaps = [
                        other["cell_id"]
                        for other in option.get("cells", [])
                        if other["cell_id"] != target_id
                        and _overlaps(target_box, other.get("bbox", []))
                    ]
                    if overlaps:
                        risk = "HIGH"
                        reasons.append("geometry_overlap_conflict")
                if risk == "MEDIUM" and confidence >= 0.9 and in_degree[target_id] == 1:
                    risk = "LOW"
                    reasons.append("single_confident_header")
        classified.append(
            {
                "relationship_id": edge.get("relationship_id", ""),
                "risk": risk,
                "reasons": reasons,
                "table_region_id": edge.get("table_region_id", ""),
                "source_id": source_id,
                "target_id": target_id,
            }
        )
    counts = Counter(item["risk"] for item in classified)
    return {
        "edges": classified,
        "counts": {level: counts.get(level, 0) for level in RISK_LEVELS},
    }


def _overlaps(a: list[float], b: list[float], tolerance: float = 1.0) -> bool:
    return (
        a[0] < b[2] - tolerance
        and b[0] < a[2] - tolerance
        and a[1] < b[3] - tolerance
        and b[1] < a[3] - tolerance
    )


def _primary_option(options: dict[str, Any]) -> str:
    best = None
    for key, option in options.items():
        if option.get("failure") and not option.get("relationships"):
            continue
        rank = (len(option.get("relationships", [])), -PRIMARY_OPTION_PREFERENCE.index(key))
        if best is None or rank > best[0]:
            best = (rank, key)
    if best is None:
        return PRIMARY_OPTION_PREFERENCE[0]
    return best[1]


def _severity(risk: str) -> int:
    return {"AMBIGUOUS": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}.get(risk, 2)


def plan_bundles(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group ownership edges into interpretable review bundles.

    A bundle is ONE header applying to a set of values (a single
    interpretable ownership rule).  Edges are never mixed across sources or
    risk classifications; AMBIGUOUS edges stay their own units so they can
    never be bulk-accepted into Gold.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        groups[(edge.get("table_region_id", ""), edge.get("source_id", ""))].append(edge)
    bundles = []
    for (table_region_id, source_id), members in sorted(groups.items()):
        risks = {item["risk"] for item in members}
        risk = min(risks, key=_severity)
        reasons = sorted({reason for item in members for reason in item["reasons"]})
        bundles.append(
            {
                "bundle_id": f"{table_region_id}::{source_id or 'ambiguous'}::{risk}",
                "table_region_id": table_region_id,
                "source_id": source_id,
                "risk": risk,
                "reasons": reasons,
                "relationship_ids": sorted(item["relationship_id"] for item in members),
                "target_value_count": len({item["target_id"] for item in members}),
                "why_grouped": "single_header_rule",
                "requires_independent_review": risk in ("HIGH", "AMBIGUOUS"),
            }
        )
    return bundles


def build_review_queue(
    workspace: str | Path,
    *,
    secondary_ratio: float = SECONDARY_REVIEW_RATIO,
    pages_jsonl: str | Path | None = None,
) -> dict[str, Any]:
    """Build risk classification, review queue and workload from predictions."""
    root = Path(workspace)
    manifest = json.loads((root / "workspace_manifest.json").read_text(encoding="utf-8"))
    evidence_by_key: dict[tuple[str, int], list[str]] = {}
    if pages_jsonl is not None:
        with Path(pages_jsonl).open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                evidence_by_key[(record["doc_id"], record["page_number"])] = record.get("discovery_evidence", [])
    page_risk: dict[str, Any] = {}
    queue: list[dict[str, Any]] = []
    for page in manifest["pages"]:
        key = page["key"]
        prediction = json.loads((root / page["prediction"]).read_text(encoding="utf-8"))
        options = prediction.get("options", {})
        primary = _primary_option(options)
        option = options.get(primary, {})
        classification = classify_option(
            option,
            page.get("slices", []),
            evidence_by_key.get((page["doc_id"], page["page_number"])),
        )
        bundles = plan_bundles(classification["edges"])
        page_risk[key] = {
            "doc_id": page["doc_id"],
            "page_number": page["page_number"],
            "slices": page.get("slices", []),
            "layout_risk_score": page.get("layout_risk_score", 0),
            "primary_option": primary,
            "counts": classification["counts"],
            "edges": classification["edges"],
            "bundles": bundles,
        }
        rank = {"AMBIGUOUS": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        for edge in classification["edges"]:
            queue.append(
                {
                    "page_key": key,
                    "doc_id": page["doc_id"],
                    "page_number": page["page_number"],
                    **edge,
                    "review_rank": rank[edge["risk"]],
                }
            )
    queue.sort(key=lambda item: (item["review_rank"], item["page_key"], item["relationship_id"]))

    dev_pages = [page for page in manifest["pages"] if page.get("selection_reason") != "cross_page_continuation_unit"]
    secondary_count = max(1, math.ceil(len(dev_pages) * secondary_ratio))
    ranked_pages = sorted(
        dev_pages,
        key=lambda page: (-page.get("layout_risk_score", 0), page["key"]),
    )
    secondary = [
        {"page_key": page["key"], "layout_risk_score": page.get("layout_risk_score", 0)}
        for page in ranked_pages[:secondary_count]
    ]

    totals = Counter()
    for risk in page_risk.values():
        totals.update(risk["counts"])
    all_bundles = [bundle for risk in page_risk.values() for bundle in risk.get("bundles", [])]
    bundle_counts = Counter(bundle["risk"] for bundle in all_bundles)
    table_units = {bundle["table_region_id"] for bundle in all_bundles if bundle["table_region_id"]}
    standalone_high = sum(
        1 for bundle in all_bundles if bundle["risk"] == "HIGH" and len(bundle["relationship_ids"]) == 1
    )
    workload = {
        "pages_total": len(manifest["pages"]),
        "dev_pages_total": len(dev_pages),
        "pages_requiring_review": len(manifest["pages"]),
        "second_review_pages": len(secondary),
        "ownership_edges_total": sum(totals.values()),
        "risk_counts": {level: totals.get(level, 0) for level in RISK_LEVELS},
        # Explicit owner review units (click-level workload, not edge count):
        "table_structure_units": len(table_units),
        "ownership_bundle_units": len(all_bundles),
        "ownership_bundle_units_by_risk": {
            level: bundle_counts.get(level, 0) for level in RISK_LEVELS
        },
        "standalone_high_edges": standalone_high,
        "ambiguous_review_units": bundle_counts.get("AMBIGUOUS", 0),
        "low_bulk_groups": bundle_counts.get("LOW", 0),
        "medium_bulk_groups": bundle_counts.get("MEDIUM", 0),
        "owner_explicit_review_units": (
            len(table_units) + len(all_bundles)
        ),
    }
    result = {
        "prediction_only": True,
        "human_confirmed": False,
        "purpose": "review_prioritization_only_never_gold",
        "authority_model": {
            "table_structure_status": list(TABLE_STRUCTURE_STATUSES),
            "ownership_status": list(OWNERSHIP_STATUSES),
            "STRUCTURE_ACCEPTANCE_CANNOT_AUTHORIZE_OWNERSHIP": True,
        },
        "primary_options": {key: value["primary_option"] for key, value in page_risk.items()},
        "page_risk": page_risk,
        "workload": workload,
        "second_review_queue": secondary,
        "review_queue": queue,
    }
    (root / "audit" / "review_risk.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return result
