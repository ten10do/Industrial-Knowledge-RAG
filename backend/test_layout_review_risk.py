"""Tests for research-only ownership risk classification and review queue."""

from __future__ import annotations

import json
import math
from pathlib import Path

from backend.layout_research.review_risk import (
    RISK_LEVELS,
    build_review_queue,
    classify_option,
)


def _cell(cell_id: str, bbox: list[float], text: str = "") -> dict:
    return {"cell_id": cell_id, "bbox": bbox, "text": text, "page_number": 1,
            "row_id": "", "column_id": "", "human_confirmed": False}


def _option(cells: list[dict], columns: list[dict], relationships: list[dict]) -> dict:
    return {
        "prediction_only": True,
        "human_confirmed": False,
        "parser_key": "suggestion_2",
        "doc_id": "doc_002",
        "page_number": 1,
        "blocks": [],
        "table_regions": [{"table_region_id": "t1", "bbox": [0, 0, 400, 400], "page_number": 1,
                           "row_ids": [], "column_ids": [], "cell_ids": [], "human_confirmed": False}],
        "rows": [], "columns": columns, "cells": cells,
        "relationships": relationships,
    }


def _edge(rel_id: str, source: str, target: str, *, rel_type: str = "HEADER_APPLIES_TO", confidence: float = 1.0) -> dict:
    return {"relationship_id": rel_id, "source_id": source, "target_id": target,
            "relationship_type": rel_type, "page_number": 1, "table_region_id": "t1",
            "confidence": confidence, "human_confirmed": False}


def test_ambiguous_edges_are_amplified_not_forced():
    cells = [_cell("v1", [100, 40, 150, 55])]
    option = _option(cells, [], [_edge("r1", "", "v1", rel_type="AMBIGUOUS", confidence=0.0)])
    result = classify_option(option, [])
    assert result["counts"]["AMBIGUOUS"] == 1
    assert result["edges"][0]["risk"] == "AMBIGUOUS"


def test_multiple_parent_candidates_are_high():
    cells = [_cell("h1", [100, 10, 150, 25], "power"), _cell("h2", [100, 25, 150, 40], "kW"),
             _cell("v1", [100, 40, 150, 55], "3.5")]
    columns = [{"column_id": "c1", "bbox": [95, 0, 160, 400], "page_number": 1, "human_confirmed": False}]
    edges = [_edge("r1", "h1", "v1"), _edge("r2", "h2", "v1")]
    option = _option(cells, columns, edges)
    result = classify_option(option, [])
    assert all(edge["risk"] == "HIGH" for edge in result["edges"])
    assert "multiple_parent_candidates" in result["edges"][0]["reasons"]


def test_unit_and_model_headers_are_high():
    cells = [
        _cell("h1", [100, 10, 150, 25], "unit"), _cell("v1", [100, 40, 150, 55], "3.5"),
        _cell("h2", [200, 10, 260, 25], "model A"), _cell("v2", [200, 40, 260, 55], "5"),
    ]
    columns = [
        {"column_id": "c1", "bbox": [95, 0, 160, 400], "page_number": 1, "human_confirmed": False},
        {"column_id": "c2", "bbox": [195, 0, 270, 400], "page_number": 1, "human_confirmed": False},
    ]
    edges = [_edge("r1", "h1", "v1"), _edge("r2", "h2", "v2")]
    result = classify_option(_option(cells, columns, edges), [])
    assert result["counts"]["HIGH"] == 2


def test_clean_single_confident_edge_is_low():
    cells = [
        _cell("h1", [100, 10, 150, 25], "rated voltage"), _cell("v1", [100, 40, 150, 55], "400"),
        _cell("h2", [200, 10, 260, 25], "rated current"), _cell("v2", [200, 40, 260, 55], "12"),
    ]
    columns = [
        {"column_id": "c1", "bbox": [95, 0, 160, 400], "page_number": 1, "human_confirmed": False},
        {"column_id": "c2", "bbox": [195, 0, 270, 400], "page_number": 1, "human_confirmed": False},
    ]
    result = classify_option(_option(cells, columns, [_edge("r1", "h1", "v1"), _edge("r2", "h2", "v2")]), [])
    assert result["counts"]["LOW"] == 2
    assert result["counts"]["HIGH"] == 0


def test_cross_page_page_flag_raises_risk():
    cells = [_cell("h1", [100, 10, 150, 25], "rated voltage"), _cell("v1", [100, 40, 150, 55], "400")]
    columns = [{"column_id": "c1", "bbox": [95, 0, 160, 400], "page_number": 1, "human_confirmed": False}]
    result = classify_option(_option(cells, columns, [_edge("r1", "h1", "v1")]), ["cross_page"])
    assert result["counts"]["HIGH"] == 1
    assert "cross_page_continuation_page" in result["edges"][0]["reasons"]


def test_fault_and_parameter_composite_rows_are_high():
    cells = [
        _cell("h1", [100, 10, 150, 25], "F0025"), _cell("v1", [100, 40, 150, 55], "F0025 ov"),
        _cell("h2", [200, 10, 260, 25], "96.01"), _cell("v2", [200, 40, 260, 55], "A055"),
    ]
    columns = [
        {"column_id": "c1", "bbox": [95, 0, 160, 400], "page_number": 1, "human_confirmed": False},
        {"column_id": "c2", "bbox": [195, 0, 270, 400], "page_number": 1, "human_confirmed": False},
    ]
    result = classify_option(_option(cells, columns, [_edge("r1", "h1", "v1"), _edge("r2", "h2", "v2")]), [])
    assert result["counts"]["HIGH"] == 2


def test_merged_header_spans_multiple_columns():
    cells = [_cell("h1", [100, 10, 260, 25], "rating"), _cell("v1", [100, 40, 150, 55], "3.5")]
    columns = [
        {"column_id": "c1", "bbox": [95, 0, 160, 400], "page_number": 1, "human_confirmed": False},
        {"column_id": "c2", "bbox": [195, 0, 270, 400], "page_number": 1, "human_confirmed": False},
    ]
    result = classify_option(_option(cells, columns, [_edge("r1", "h1", "v1")]), [])
    assert result["counts"]["HIGH"] == 1
    assert "cross_column_span_or_merged_header" in result["edges"][0]["reasons"]


def _write_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    for name in ("pages", "predictions", "annotations", "audit"):
        (root / name).mkdir(parents=True)
    cells_a = [_cell("ha", [100, 10, 150, 25], "rated voltage"), _cell("va", [100, 40, 150, 55], "400")]
    cells_b = [_cell("hb", [100, 10, 150, 25], "unit"), _cell("vb", [100, 40, 150, 55], "3.5")]
    columns = [{"column_id": "c1", "bbox": [95, 0, 160, 400], "page_number": 1, "human_confirmed": False}]
    pages = [
        {"key": "doc_002_p0001", "doc_id": "doc_002", "page_number": 1, "width": 595, "height": 842,
         "slices": ["simple_table"], "layout_risk_score": 6, "selection_reason": "stratified_coverage",
         "preview": "previews/doc_002_p0001.png", "prediction": "predictions/doc_002_p0001.json",
         "annotation": "annotations/doc_002_p0001.json"},
        {"key": "doc_002_p0002", "doc_id": "doc_002", "page_number": 2, "width": 595, "height": 842,
         "slices": ["simple_table"], "layout_risk_score": 2, "selection_reason": "stratified_coverage",
         "preview": "previews/doc_002_p0002.png", "prediction": "predictions/doc_002_p0002.json",
         "annotation": "annotations/doc_002_p0002.json"},
    ]
    manifest = {"status": "HUMAN_ANNOTATION_ACTION_REQUIRED", "pages": pages, "predictions_hidden_by_default": True}
    (root / "workspace_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for index, page in enumerate(pages):
        (root / page["prediction"]).write_text(json.dumps({
            "prediction_only": True, "human_confirmed": False, "predictions_hidden_by_default": True,
            "options": {
                "suggestion_2": _option(cells_a if index == 0 else cells_b, columns, [
                    _edge("r1", "ha" if index == 0 else "hb", "va" if index == 0 else "vb"),
                ]),
                "flat_baseline": {"prediction_only": True, "human_confirmed": False, "parser_key": "flat_baseline",
                                  "doc_id": "doc_002", "page_number": index + 1, "blocks": [], "table_regions": [],
                                  "rows": [], "columns": [], "cells": [], "relationships": []},
            },
        }), encoding="utf-8")
    return root


def test_build_review_queue_orders_and_sizes_secondary_review(tmp_path):
    root = _write_workspace(tmp_path)
    result = build_review_queue(root)
    assert result["workload"]["pages_total"] == 2
    assert result["workload"]["second_review_pages"] == math.ceil(2 * 0.2)
    ranks = [entry["review_rank"] for entry in result["review_queue"]]
    assert ranks == sorted(ranks)
    assert result["prediction_only"] is True and result["human_confirmed"] is False
    assert set(RISK_LEVELS) == {"LOW", "MEDIUM", "HIGH", "AMBIGUOUS"}
    payload = json.loads((root / "audit" / "review_risk.json").read_text(encoding="utf-8"))
    assert payload["workload"]["ownership_edges_total"] == 2


def test_secondary_review_prefers_high_risk_pages(tmp_path):
    root = _write_workspace(tmp_path)
    result = build_review_queue(root)
    assert result["second_review_queue"][0]["page_key"] == "doc_002_p0001"
