"""Authority mutation tests: table structure confirmation vs ownership.

Guards the Gold authority boundary introduced in LA-0.7b:

STRUCTURE_ACCEPTANCE_CANNOT_AUTHORIZE_OWNERSHIP

Accepting a table structure (region / rows / columns / membership) must
never accept ownership edges, never set their human_confirmed, and never
satisfy the ownership completion gate.
"""

from __future__ import annotations

import json

import pytest

from backend.layout_research.annotation_tool.server import (
    STRUCTURE_ACCEPTANCE_CANNOT_AUTHORIZE_OWNERSHIP,
)
from backend.test_layout_review_gate import _post, _write_workspace, server  # noqa: F401


def _annotation(root):
    return json.loads((root / "annotations" / "doc_002_p0001.json").read_text(encoding="utf-8"))


def _stored(root):
    return _annotation(root)


def test_gold_authority_guard_is_hardcoded():
    assert STRUCTURE_ACCEPTANCE_CANNOT_AUTHORIZE_OWNERSHIP is True


def test_a_structure_acceptance_does_not_accept_ownership(server):
    root, base = server
    result = _post(f"{base}/api/bulk?key=doc_002_p0001",
                   {"operation": "accept_table_structure", "table_region_id": "t1", "owner_clicked": True})
    assert result["saved"] is True
    stored = _stored(root)
    assert all(edge["human_confirmed"] is False for edge in stored["relationships"])
    assert "relationships" not in [group for group in result.get("affected", [])]
    assert not any(rid.startswith("r_") for rid in result["affected"] if not rid.startswith("table_structure_status"))


def test_b_structure_acceptance_does_not_set_human_confirmed(server):
    root, base = server
    _post(f"{base}/api/bulk?key=doc_002_p0001",
          {"operation": "accept_table_structure", "table_region_id": "t1", "owner_clicked": True})
    stored = _stored(root)
    assert stored["human_confirmed"] is False
    assert stored["status"] != "REVIEWED"


def test_c_structure_acceptance_does_not_satisfy_ownership_gate(server):
    root, base = server
    _post(f"{base}/api/bulk?key=doc_002_p0001",
          {"operation": "accept_table_structure", "table_region_id": "t1", "owner_clicked": True})
    annotation = _annotation(root)
    # Simulate an owner that confirms everything EXCEPT ownership statuses.
    for item in annotation["blocks"] + annotation["table_regions"] + annotation["relationships"]:
        item["human_confirmed"] = True
    annotation["status"] = "REVIEWED"
    annotation["human_confirmed"] = True
    annotation["owner_clicked"] = True
    result = _post(f"{base}/api/save?key=doc_002_p0001", annotation)
    ownership_violations = [
        error for error in result["validation"]["errors"] if "ownership not explicitly confirmed" in error
    ]
    assert ownership_violations, result["validation"]["errors"]
    assert result.get("reviewed_gate", {}) and result["reviewed_gate"]["passed"] is False


def test_d_high_ownership_unresolved_after_structure_acceptance(server):
    root, base = server
    _post(f"{base}/api/bulk?key=doc_002_p0001",
          {"operation": "accept_table_structure", "table_region_id": "t1", "owner_clicked": True})
    stored = _stored(root)
    high_edge = next(edge for edge in stored["relationships"] if edge["relationship_id"] == "r_high")
    assert high_edge["human_confirmed"] is False, "HIGH ownership must remain unresolved"
    assert stored["authority"]["ownership_status"].get("r_high", "UNREVIEWED") == "UNREVIEWED"
    annotation = _annotation(root)
    annotation["status"] = "REVIEWED"
    annotation["human_confirmed"] = True
    annotation["owner_clicked"] = True
    result = _post(f"{base}/api/save?key=doc_002_p0001", annotation)
    assert result["saved"] is False, "page must not become REVIEWED with unresolved HIGH ownership"
    assert result["validation"]["errors"]


def test_e_bundle_acceptance_requires_owner_click(server):
    _root, base = server
    result = _post(f"{base}/api/bulk?key=doc_002_p0001",
                   {"operation": "accept_ownership_bundle", "relationship_ids": ["r_low"],
                    "owner_clicked": False})
    assert result.get("saved") is False
    assert "explicit owner click" in result.get("error", "")


def test_f_bundle_acceptance_only_affects_listed_ids(server):
    root, base = server
    result = _post(f"{base}/api/bulk?key=doc_002_p0001",
                   {"operation": "accept_ownership_bundle", "relationship_ids": ["r_low"],
                    "owner_clicked": True})
    assert result["saved"] is True
    assert result["affected"] == ["r_low"]
    stored = _stored(root)
    by_id = {edge["relationship_id"]: edge for edge in stored["relationships"]}
    assert by_id["r_low"]["human_confirmed"] is True
    assert by_id["r_high"]["human_confirmed"] is False
    assert stored["authority"]["ownership_status"]["r_low"] == "ACCEPTED"
    assert stored["authority"]["ownership_status"].get("r_high", "UNREVIEWED") == "UNREVIEWED"


def test_g_bundle_cannot_mix_sources_or_risk_classes(server):
    _root, base = server
    mixed_source = _post(f"{base}/api/bulk?key=doc_002_p0001",
                         {"operation": "accept_ownership_bundle",
                          "relationship_ids": ["r_low", "r_high"], "owner_clicked": True})
    # r_low (LOW) and r_high (HIGH) also have different sources in the fixture;
    # both the single-source and single-risk rules must block this bundle.
    assert mixed_source.get("saved") is False
    assert "single" in mixed_source.get("error", "")


def test_h_ambiguous_edges_cannot_be_accepted_via_bundle(server):
    root, base = server
    # Turn r_high into an AMBIGUOUS-classified relation first.
    annotation = _annotation(root)
    by_id = {edge["relationship_id"]: edge for edge in annotation["relationships"]}
    by_id["r_high"]["relationship_type"] = "AMBIGUOUS"
    (root / "annotations" / "doc_002_p0001.json").write_text(json.dumps(annotation), encoding="utf-8")
    review = json.loads((root / "audit" / "review_risk.json").read_text(encoding="utf-8"))
    for edge in review["page_risk"]["doc_002_p0001"]["edges"]:
        if edge["relationship_id"] == "r_high":
            edge["risk"] = "AMBIGUOUS"
    (root / "audit" / "review_risk.json").write_text(json.dumps(review), encoding="utf-8")
    result = _post(f"{base}/api/bulk?key=doc_002_p0001",
                   {"operation": "accept_ownership_bundle", "relationship_ids": ["r_high"],
                    "owner_clicked": True})
    assert result.get("saved") is False
    assert "AMBIGUOUS" in result.get("error", "")


def test_i_page_cannot_become_reviewed_while_ownership_unresolved(server):
    root, base = server
    _post(f"{base}/api/bulk?key=doc_002_p0001",
          {"operation": "accept_table_structure", "table_region_id": "t1", "owner_clicked": True})
    annotation = _annotation(root)
    for item in annotation["blocks"] + annotation["table_regions"]:
        item["human_confirmed"] = True
    for edge in annotation["relationships"]:
        if edge["relationship_id"] == "r_low":
            edge["human_confirmed"] = True
    annotation["status"] = "REVIEWED"
    annotation["human_confirmed"] = True
    annotation["owner_clicked"] = True
    result = _post(f"{base}/api/save?key=doc_002_p0001", annotation)
    assert result["saved"] is False
    stored = _stored(root)
    assert stored["status"] != "REVIEWED", "rejected REVIEWED must never be persisted"


def test_j_undo_restores_bundle_statuses_exactly(server):
    root, base = server
    accepted = _post(f"{base}/api/bulk?key=doc_002_p0001",
                     {"operation": "accept_ownership_bundle", "relationship_ids": ["r_low"],
                      "owner_clicked": True})
    assert accepted["saved"] is True
    prior = accepted["prior_state"]
    assert prior["r_low"]["human_confirmed"] is False
    assert prior["r_low"]["ownership_status"] == "UNREVIEWED"
    restored = _post(f"{base}/api/bulk?key=doc_002_p0001",
                     {"operation": "restore_ownership_state", "prior_state": prior,
                      "owner_clicked": True})
    assert restored["saved"] is True
    stored = _stored(root)
    by_id = {edge["relationship_id"]: edge for edge in stored["relationships"]}
    assert by_id["r_low"]["human_confirmed"] is False
    assert stored["authority"]["ownership_status"]["r_low"] == "UNREVIEWED"


def test_bundle_reject_marks_rejected_status(server):
    root, base = server
    rejected = _post(f"{base}/api/bulk?key=doc_002_p0001",
                     {"operation": "reject_ownership_bundle", "relationship_ids": ["r_low"],
                      "owner_clicked": True})
    assert rejected["saved"] is True
    stored = _stored(root)
    by_id = {edge["relationship_id"]: edge for edge in stored["relationships"]}
    assert by_id["r_low"]["relationship_type"] == "AMBIGUOUS"
    assert stored["authority"]["ownership_status"]["r_low"] == "REJECTED"


def test_unknown_relationship_ids_rejected(server):
    _root, base = server
    result = _post(f"{base}/api/bulk?key=doc_002_p0001",
                   {"operation": "accept_ownership_bundle", "relationship_ids": ["ghost"],
                    "owner_clicked": True})
    assert result.get("saved") is False
    assert "unknown relationship ids" in result.get("error", "")
