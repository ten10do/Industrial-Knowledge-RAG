"""LA-0.7.1 regression tests: annotation unit loading contract.

Covers the runtime load failure fixed in LA-0.7.1: the UI must always
receive a complete unit payload (all editable collections present), the
preview route must serve real content, and loading a unit must never
mutate the stored draft or its authority state.
"""
from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from backend.test_layout_review_gate import _write_workspace
from backend.layout_research.annotation_tool.server import AnnotationHandler

UNIT_COLLECTIONS = ("blocks", "table_regions", "rows", "columns", "cells", "relationships")


@pytest.fixture(scope="module")
def load_server(tmp_path_factory):
    root = _write_workspace(tmp_path_factory.mktemp("ui-load"))
    AnnotationHandler.workspace = root
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), AnnotationHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield root, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _get(url: str) -> dict:
    with urllib.request.urlopen(url) as response:
        return json.loads(response.read().decode("utf-8"))


def _load_payload(root: Path, base: str, key: str) -> dict:
    payload = _get(f"{base}/api/page?key={key}")
    # Mirror of the frontend validateUnitPayload contract (app.js).
    for field in ("page", "annotation", "prediction"):
        assert isinstance(payload.get(field), dict), f"{field} missing for {key}"
    for collection in UNIT_COLLECTIONS:
        assert isinstance(payload["annotation"].get(collection), list), (
            f"annotation.{collection} missing or not a list for {key}"
        )
    assert isinstance(payload["prediction"].get("options"), dict)
    return payload


def test_page_payload_exposes_full_collection_shape(load_server):
    root, base = load_server
    _load_payload(root, base, "doc_002_p0001")


def test_valid_empty_block_list_is_allowed(load_server):
    root, base = load_server
    # A schema-valid unit may legitimately have zero blocks (cover page):
    # an empty list must pass the shape contract like any other list.
    annotation_path = root / "annotations" / "doc_002_p0001.json"
    original = json.loads(annotation_path.read_text(encoding="utf-8"))
    empty = dict(original, blocks=[], table_regions=[], rows=[], columns=[],
                 cells=[], relationships=[])
    annotation_path.write_text(json.dumps(empty), encoding="utf-8")
    try:
        payload = _load_payload(root, base, "doc_002_p0001")
        assert payload["annotation"]["blocks"] == []
    finally:
        annotation_path.write_text(json.dumps(original), encoding="utf-8")


def test_single_page_unit_loads(load_server):
    root, base = load_server
    payload = _load_payload(root, base, "doc_002_p0001")
    assert payload["page"]["key"] == "doc_002_p0001"
    assert payload["page"]["page_number"] == 1


def test_cross_page_unit_loads(load_server):
    root, base = load_server
    # Extend the fixture with a cross-page continuation unit sharing shape.
    source_page = json.loads((root / "pages" / "doc_002_p0001.json").read_text(encoding="utf-8"))
    page = dict(source_page, key="doc_002_p0338", page_number=338, slices=["cross_page", "simple_table"])
    (root / "pages" / "doc_002_p0338.json").write_text(json.dumps(page), encoding="utf-8")
    for folder in ("predictions", "annotations"):
        data = json.loads((root / folder / "doc_002_p0001.json").read_text(encoding="utf-8"))
        data["page_number"] = 338
        (root / folder / "doc_002_p0338.json").write_text(json.dumps(data), encoding="utf-8")
    (root / "previews" / "doc_002_p0338.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    payload = _load_payload(root, base, "doc_002_p0338")
    assert payload["page"]["page_number"] == 338


def test_preview_route_returns_real_content(load_server):
    root, base = load_server
    preview = root / "previews" / "doc_002_p0001.png"
    preview.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    with urllib.request.urlopen(f"{base}/workspace/previews/doc_002_p0001.png") as response:
        body = response.read()
    assert response.headers["Content-Type"] == "image/png"
    assert body[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(body) > 0


def test_loading_a_unit_never_mutates_draft_or_authority(load_server):
    root, base = load_server
    before = (root / "annotations" / "doc_002_p0001.json").read_bytes()
    for _ in range(3):
        _load_payload(root, base, "doc_002_p0001")
    after = (root / "annotations" / "doc_002_p0001.json").read_bytes()
    assert before == after


def test_missing_page_artifact_is_explicit_error_not_silent(load_server):
    root, base = load_server
    (root / "predictions" / "doc_002_p0001.json").unlink()
    try:
        with urllib.request.urlopen(f"{base}/api/page?key=doc_002_p0001") as response:
            payload = json.loads(response.read().decode("utf-8"))
            assert payload.get("prediction") is None or "error" in payload, (
                "server must not return a silently incomplete unit payload"
            )
    except Exception as error:  # noqa: BLE001
        assert "error" in str(error).lower() or "HTTP Error" in str(error)
    finally:
        (root / "predictions" / "doc_002_p0001.json").write_text(
            json.dumps({"options": {}}), encoding="utf-8"
        )
