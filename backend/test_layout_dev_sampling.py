"""Tests for research-only corpus qualification and DEV sampling."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from backend.layout_research.dev_sampling import (
    SLICE_PRIORITY,
    allocate_final_gate_holdout,
    select_dev_sample,
)
from backend.layout_research.qualification import SLICE_LABELS, canonical_digest


def _candidate(doc_id: str, page_number: int, slices: list[str], risk: int = 4) -> dict:
    return {
        "doc_id": doc_id,
        "page_number": page_number,
        "width": 595.0,
        "height": 842.0,
        "slices": slices,
        "layout_risk_score": risk,
        "discovery_evidence": [],
        "aligned_column_count": 4,
        "numeric_line_count": 6,
        "detected_table_count": 1 if "ordinary_text" not in slices else 0,
        "text_layer_available": True,
        "font_profile": {"distinct_fonts": 2.0, "median_size": 10.0, "size_variance": 1.0},
        "parameter_code_hits": 0,
        "fault_code_hits": 0,
    }


def _synthetic_corpus() -> list[dict]:
    candidates = []
    # doc_001..doc_008 spread across slices; docs 006-008 are large.
    layout_map = {
        "doc_001": (["multi_column", "merged_header"], 40),
        "doc_002": (["model_comparison", "unit_column"], 60),
        "doc_003": (["borderless_table", "parameter_code_table"], 80),
        "doc_004": (["fault_code_table", "cross_page"], 50),
        "doc_005": (["section_table", "mixed_layout", "multi_column"], 70),
        "doc_006": (["simple_table", "ordinary_text"], 120),
        "doc_007": (["multi_column", "borderless_table"], 100),
        "doc_008": (["ordinary_text", "simple_table"], 90),
        "doc_009": (["model_comparison", "unit_column"], 30),
        "doc_010": (["section_table", "fault_code_table"], 25),
        "doc_011": (["merged_header", "cross_page"], 20),
    }
    for doc_id, (slices, page_count) in layout_map.items():
        for page_number in range(1, page_count + 1):
            alternating = list(slices) if page_number % 3 else ["ordinary_text"]
            candidates.append(_candidate(doc_id, page_number, alternating, risk=5 if page_number % 3 else 0))
    return candidates


def test_all_slice_labels_are_covered_by_priority_and_targets():
    assert set(SLICE_LABELS) == set(SLICE_PRIORITY)
    assert set(SLICE_LABELS) == {
        "ordinary_text", "simple_table", "borderless_table", "model_comparison",
        "multi_column", "merged_header", "cross_page", "mixed_layout",
        "unit_column", "section_table", "parameter_code_table", "fault_code_table",
    }


def test_holdout_is_document_level_and_marks_unconsumed():
    candidates = _synthetic_corpus()
    vendors = {f"doc_{i:03d}": f"vendor_{i % 5}" for i in range(1, 12)}
    plan = allocate_final_gate_holdout(candidates, vendors)
    assert len(plan["reserve_docs"]) >= 3
    assert plan["FINAL_GATE_UNCONSUMED"] is True
    assert not (set(plan["reserve_docs"]) & set(plan["dev_eligible_docs"]))
    assert plan["reserve_pages"] == sum(
        1 for item in candidates if item["doc_id"] in set(plan["reserve_docs"])
    )


def test_dev_sample_respects_target_and_document_diversity():
    candidates = _synthetic_corpus()
    vendors = {f"doc_{i:03d}": f"vendor_{i % 5}" for i in range(1, 12)}
    holdout = allocate_final_gate_holdout(candidates, vendors)
    plan = select_dev_sample(candidates, holdout, vendors, target_pages=36)
    assert 32 <= plan["selected_pages"] <= 40
    assert plan["dev_document_count"] >= 6
    assert plan["max_pages_from_single_manual"] <= 6
    assert plan["dev_vendor_count"] >= 4
    assert not (set(plan["dev_documents"]) & set(holdout["reserve_docs"]))
    # Ordinary text must not dominate the DEV sample.
    ordinary = plan["slice_distribution"].get("ordinary_text", 0)
    assert ordinary <= 8


def test_dev_sample_manifest_digest_is_canonical_and_sensitive():
    candidates = _synthetic_corpus()
    vendors = {f"doc_{i:03d}": f"vendor_{i % 5}" for i in range(1, 12)}
    holdout = allocate_final_gate_holdout(candidates, vendors)
    first = select_dev_sample(candidates, holdout, vendors, target_pages=36)
    second = select_dev_sample(candidates, holdout, vendors, target_pages=36)
    assert first["dev_sample_manifest_digest"] == second["dev_sample_manifest_digest"]
    mutated = [dict(item, layout_risk_score=item["layout_risk_score"] + 1) for item in candidates]
    third = select_dev_sample(mutated, holdout, vendors, target_pages=36)
    assert third["dev_sample_manifest_digest"] != first["dev_sample_manifest_digest"]


def test_manifest_page_shape_matches_private_schema():
    candidates = _synthetic_corpus()
    vendors = {f"doc_{i:03d}": f"vendor_{i % 5}" for i in range(1, 12)}
    holdout = allocate_final_gate_holdout(candidates, vendors)
    plan = select_dev_sample(candidates, holdout, vendors, target_pages=36)
    for page in plan["pages"]:
        assert set(page) == {
            "sample_id", "sanitized_doc_id", "page_number", "slice_labels",
            "layout_risk_score", "selection_reason", "review_priority",
        }
        assert page["sanitized_doc_id"].startswith("doc_")
        assert isinstance(page["slice_labels"], list)
        assert 0 <= page["review_priority"] <= 9


def test_holdout_preserves_rare_slices_when_possible():
    # fault_code_table only exists in doc_004 and doc_009; holdout must not
    # consume both.
    candidates = (
        [_candidate("doc_004", n, ["fault_code_table"]) for n in range(1, 40)]
        + [_candidate("doc_009", n, ["fault_code_table"]) for n in range(1, 40)]
        + [_candidate("doc_001", n, ["multi_column"]) for n in range(1, 60)]
        + [_candidate("doc_002", n, ["model_comparison"]) for n in range(1, 55)]
        + [_candidate("doc_003", n, ["unit_column"]) for n in range(1, 50)]
        + [_candidate("doc_010", n, ["ordinary_text"]) for n in range(1, 80)]
    )
    vendors = {f"doc_{i:03d}": "v" for i in (1, 2, 3, 4, 9, 10)}
    plan = allocate_final_gate_holdout(candidates, vendors)
    fault_docs_in_dev = [doc for doc in plan["dev_eligible_docs"] if doc in ("doc_004", "doc_009")]
    assert fault_docs_in_dev


def test_canonical_digest_stable_across_key_order():
    assert canonical_digest({"a": 1, "b": 2}) == canonical_digest({"b": 2, "a": 1})
