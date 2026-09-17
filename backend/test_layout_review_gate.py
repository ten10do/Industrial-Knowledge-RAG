"""Tests for the LA-0.7 annotation server: reviewed gate + bulk confirmations."""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from backend.layout_research.annotation_tool.server import AnnotationHandler


def _cell(cell_id: str, bbox: list[float], text: str = "") -> dict:
    return {"cell_id": cell_id, "bbox": bbox, "text": text, "page_number": 1,
            "row_id": "", "column_id": "", "human_confirmed": False}


def _write_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    for name in ("pages", "predictions", "annotations", "audit", "previews"):
        (root / name).mkdir(parents=True)
    page_key = "doc_002_p0001"
    page = {"key": page_key, "doc_id": "doc_002", "page_number": 1, "width": 595, "height": 842,
            "slices": ["simple_table"], "layout_risk_score": 6, "selection_reason": "stratified_coverage",
            "preview": f"previews/{page_key}.png", "prediction": f"predictions/{page_key}.json",
            "annotation": f"annotations/{page_key}.json"}
    (root / "pages" / f"{page_key}.json").write_text(json.dumps(page), encoding="utf-8")
    (root / "workspace_manifest.json").write_text(json.dumps({
        "status": "HUMAN_ANNOTATION_ACTION_REQUIRED", "pages": [page], "predictions_hidden_by_default": True,
    }), encoding="utf-8")
    header = _cell("h1", [100, 10, 150, 25], "rated voltage")
    header2 = _cell("h2", [100, 25, 150, 40], "unit")
    value = _cell("v1", [100, 40, 150, 55], "400")
    option = {
        "prediction_only": True, "human_confirmed": False, "parser_key": "suggestion_2",
        "doc_id": "doc_002", "page_number": 1,
        "blocks": [
            dict(header, block_id="h1", block_type="TABLE_CELL", reading_order=0, section_id="", table_region_id="t1"),
            dict(header2, block_id="h2", block_type="TABLE_CELL", reading_order=1, section_id="", table_region_id="t1"),
            dict(value, block_id="v1", block_type="TABLE_CELL", reading_order=2, section_id="", table_region_id="t1"),
        ],
        "table_regions": [{"table_region_id": "t1", "bbox": [50, 0, 300, 200], "page_number": 1,
                           "row_ids": [], "column_ids": [], "cell_ids": [], "human_confirmed": False}],
        "rows": [], "columns": [], "cells": [],
        "relationships": [
            {"relationship_id": "r_low", "source_id": "h1", "target_id": "v1",
             "relationship_type": "HEADER_APPLIES_TO", "page_number": 1, "table_region_id": "t1",
             "confidence": 1.0, "human_confirmed": False},
            {"relationship_id": "r_high", "source_id": "h2", "target_id": "v1",
             "relationship_type": "HEADER_APPLIES_TO", "page_number": 1, "table_region_id": "t1",
             "confidence": 1.0, "human_confirmed": False},
        ],
    }
    (root / page["prediction"]).write_text(json.dumps({
        "prediction_only": True, "human_confirmed": False, "predictions_hidden_by_default": True,
        "options": {"suggestion_2": option},
    }), encoding="utf-8")
    annotation = {
        "schema_version": 2, "gold_type": "REAL_LAYOUT_DEV_GOLD", "split": "DEV",
        "doc_id": "doc_002", "page_number": 1, "width": 595, "height": 842,
        "coordinate_origin": "top-left", "slices": ["simple_table"], "status": "IN_PROGRESS",
        "human_confirmed": False,
        "blocks": [dict(item, human_confirmed=False) for item in option["blocks"]],
        "table_regions": [dict(item, human_confirmed=False) for item in option["table_regions"]],
        "rows": [], "columns": [], "cells": [],
        "relationships": [dict(item, human_confirmed=False) for item in option["relationships"]],
        "continuations": [],
        "audit": {"prediction_imported": True, "review_count": 0},
    }
    (root / page["annotation"]).write_text(json.dumps(annotation), encoding="utf-8")
    review_risk = {
        "prediction_only": True, "human_confirmed": False,
        "available": True,
        "primary_options": {page_key: "suggestion_2"},
        "page_risk": {page_key: {
            "doc_id": "doc_002", "page_number": 1, "slices": ["simple_table"], "layout_risk_score": 6,
            "primary_option": "suggestion_2",
            "counts": {"LOW": 1, "MEDIUM": 0, "HIGH": 1, "AMBIGUOUS": 0},
            "edges": [
                {"relationship_id": "r_low", "risk": "LOW", "reasons": ["single_confident_header"], "table_region_id": "t1", "source_id": "h1", "target_id": "v1"},
                {"relationship_id": "r_high", "risk": "HIGH", "reasons": ["multiple_parent_candidates"], "table_region_id": "t1", "source_id": "h2", "target_id": "v1"},
            ],
        }},
        "workload": {}, "second_review_queue": [{"page_key": page_key, "layout_risk_score": 6}],
        "review_queue": [],
    }
    (root / "audit" / "review_risk.json").write_text(json.dumps(review_risk), encoding="utf-8")
    (root / "previews" / f"{page_key}.png").write_bytes(b"png")
    return root


@pytest.fixture()
def server(tmp_path):
    root = _write_workspace(tmp_path)

    class Handler(AnnotationHandler):
        pass

    Handler.workspace = root
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield root, f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(url, method="POST", data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return json.loads(error.read().decode("utf-8"))


def _get(url: str) -> dict:
    with urllib.request.urlopen(url) as response:
        return json.loads(response.read().decode("utf-8"))


def test_schema_blocks_reviewed_with_unconfirmed_gold_items(server):
    root, base = server
    annotation = json.loads((root / "annotations" / "doc_002_p0001.json").read_text(encoding="utf-8"))
    annotation["status"] = "REVIEWED"
    annotation["human_confirmed"] = True
    annotation["owner_clicked"] = True
    result = _post(f"{base}/api/save?key=doc_002_p0001", annotation)
    assert result["saved"] is False
    assert "all Gold items must be human_confirmed=true" in result["validation"]["errors"]
    stored = json.loads((root / "annotations" / "doc_002_p0001.json").read_text(encoding="utf-8"))
    assert stored["status"] == "IN_PROGRESS", "gate must persist the draft, never REVIEWED"


def test_reviewed_gate_requires_recorded_owner_action(server):
    root, base = server
    annotation = json.loads((root / "annotations" / "doc_002_p0001.json").read_text(encoding="utf-8"))
    annotation["status"] = "REVIEWED"
    annotation["human_confirmed"] = True
    annotation["owner_clicked"] = True
    for item in annotation["blocks"] + annotation["table_regions"] + annotation["relationships"]:
        item["human_confirmed"] = True
    annotation["audit"]["prediction_imported"] = False
    annotation["audit"]["operations"] = []
    result = _post(f"{base}/api/save?key=doc_002_p0001", annotation)
    assert result["saved"] is False
    assert any("no owner action" in error for error in result["validation"]["errors"])


def test_full_authority_path_to_reviewed(server):
    """Structure accept → LOW bulk → HIGH bundle → REVIEWED passes."""
    root, base = server
    structure = _post(f"{base}/api/bulk?key=doc_002_p0001",
                      {"operation": "accept_table_structure", "table_region_id": "t1", "owner_clicked": True})
    assert structure["saved"] is True
    stored = json.loads((root / "annotations" / "doc_002_p0001.json").read_text(encoding="utf-8"))
    assert all(edge["human_confirmed"] is False for edge in stored["relationships"]), (
        "structure acceptance must never confirm ownership edges"
    )
    assert stored["authority"]["table_structure_status"]["t1"] == "ACCEPTED"
    low = _post(f"{base}/api/bulk?key=doc_002_p0001",
                {"operation": "accept_low_risk_ownership", "table_region_id": "t1", "owner_clicked": True})
    assert low["affected"] == ["r_low"]
    bundle = _post(f"{base}/api/bulk?key=doc_002_p0001",
                   {"operation": "accept_ownership_bundle", "relationship_ids": ["r_high"], "owner_clicked": True})
    assert bundle["saved"] is True
    stored = json.loads((root / "annotations" / "doc_002_p0001.json").read_text(encoding="utf-8"))
    assert all(edge["human_confirmed"] for edge in stored["relationships"])
    annotation = dict(stored, status="REVIEWED", human_confirmed=True, owner_clicked=True)
    result = _post(f"{base}/api/save?key=doc_002_p0001", annotation)
    assert result["saved"] is True, result


def test_bulk_requires_explicit_owner_click(server):
    _root, base = server
    result = _post(f"{base}/api/bulk?key=doc_002_p0001",
                   {"operation": "accept_table_structure", "table_region_id": "t1", "owner_clicked": False})
    assert "explicit owner click" in result["error"]
    assert result.get("saved") is False


def test_low_risk_bulk_only_touches_low_edges(server):
    root, base = server
    bulk = _post(f"{base}/api/bulk?key=doc_002_p0001",
                 {"operation": "accept_low_risk_ownership", "table_region_id": "t1", "owner_clicked": True})
    assert bulk["saved"] is True
    assert bulk["affected"] == ["r_low"]
    stored = json.loads((root / "annotations" / "doc_002_p0001.json").read_text(encoding="utf-8"))
    by_id = {edge["relationship_id"]: edge for edge in stored["relationships"]}
    assert by_id["r_low"]["human_confirmed"] is True
    assert by_id["r_high"]["human_confirmed"] is False


def test_workload_endpoint_reports_remaining(server):
    _root, base = server
    workload = _get(f"{base}/api/workload")
    assert workload["pages_total"] == 1
    assert workload["high_risk_remaining"] == 1
    assert workload["ownership_unresolved"] == 1
    assert workload["second_review"][0]["SECOND_REVIEW_REQUIRED"] is True
    assert workload["final_gate_consumed"] is False


def test_ui_operations_log_stays_private(server):
    root, base = server
    _post(f"{base}/api/bulk?key=doc_002_p0001",
          {"operation": "accept_low_risk_ownership", "table_region_id": "t1", "owner_clicked": True})
    log_path = root / "audit" / "ui_operations.jsonl"
    assert log_path.exists()
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert entries and entries[-1]["operation"] == "accept_low_risk_ownership"
