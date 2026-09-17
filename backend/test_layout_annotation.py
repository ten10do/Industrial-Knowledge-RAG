from copy import deepcopy
import json
from pathlib import Path

import pytest

from backend.layout_research.annotation import (
    canonical_gold_digest,
    freeze_dev_gold,
    load_confirmed_dev_gold,
    new_page_annotation,
    validate_page_annotation,
)
from backend.layout_research.annotation_workspace import select_dev_pages


def _draft():
    return new_page_annotation(
        {
            "doc_id": "doc_001",
            "page_number": 3,
            "width": 600,
            "height": 800,
            "slices": ["multi_column", "unit_column"],
        }
    )


def _complete_annotation():
    annotation = _draft()
    annotation.update({"status": "REVIEWED", "human_confirmed": True})
    annotation["table_regions"] = [
        {"table_region_id": "table-1", "bbox": [50, 100, 550, 400], "page_number": 3, "human_confirmed": True}
    ]
    annotation["rows"] = [
        {"row_id": "row-1", "bbox": [50, 100, 550, 150], "page_number": 3, "cell_ids": [], "human_confirmed": True}
    ]
    annotation["columns"] = [
        {"column_id": "column-1", "bbox": [50, 100, 250, 400], "page_number": 3, "cell_ids": [], "human_confirmed": True}
    ]
    annotation["blocks"] = [
        {
            "block_id": "header-1", "bbox": [50, 100, 250, 140], "block_type": "TABLE_CELL",
            "reading_order": 0, "text": "header", "section_id": "section-1", "table_region_id": "table-1",
            "row_id": "row-1", "column_id": "column-1", "page_number": 3, "human_confirmed": True,
        },
        {
            "block_id": "value-1", "bbox": [50, 200, 250, 240], "block_type": "TABLE_CELL",
            "reading_order": 1, "text": "10", "section_id": "section-1", "table_region_id": "table-1",
            "row_id": "row-2", "column_id": "column-1", "page_number": 3, "human_confirmed": True,
        },
    ]
    annotation["relationships"] = [
        {
            "relationship_id": "rel-1", "source_id": "header-1", "target_id": "value-1",
            "relationship_type": "HEADER_APPLIES_TO", "page_number": 3, "human_confirmed": True,
        }
    ]
    return annotation


def test_empty_annotation_draft_is_valid_but_not_evaluation_ready():
    validation = validate_page_annotation(_draft())
    assert validation["valid"] is True
    assert validation["ready_for_evaluation"] is False


def test_only_reviewed_human_confirmed_page_is_evaluation_ready():
    annotation = _complete_annotation()
    assert validate_page_annotation(annotation, require_complete=True)["ready_for_evaluation"] is True
    annotation["relationships"][0]["human_confirmed"] = False
    validation = validate_page_annotation(annotation, require_complete=True)
    assert validation["valid"] is False
    assert "all Gold items must be human_confirmed=true" in validation["errors"]


def test_validator_detects_duplicate_ids_and_reading_order():
    annotation = _complete_annotation()
    annotation["blocks"][1]["block_id"] = "header-1"
    annotation["blocks"][1]["reading_order"] = 0
    validation = validate_page_annotation(annotation)
    assert "duplicate ID: header-1" in validation["errors"]
    assert "duplicate reading_order" in validation["errors"]


def test_validator_detects_orphan_page_and_bbox_mutations():
    annotation = _complete_annotation()
    annotation["relationships"][0]["target_id"] = "missing"
    annotation["blocks"][0]["page_number"] = 99
    annotation["blocks"][1]["bbox"] = [0, 0, 900, 900]
    errors = validate_page_annotation(annotation)["errors"]
    assert "orphan relationship target: rel-1" in errors
    assert "page mismatch: header-1" in errors
    assert "bbox outside page: value-1" in errors


def test_validator_detects_header_outside_table_and_duplicate_edge():
    annotation = _complete_annotation()
    annotation["blocks"][0]["bbox"] = [0, 0, 40, 40]
    duplicate = deepcopy(annotation["relationships"][0])
    duplicate["relationship_id"] = "rel-2"
    annotation["relationships"].append(duplicate)
    errors = validate_page_annotation(annotation)["errors"]
    assert "header outside table: rel-1" in errors
    assert any(error.startswith("duplicate relationship edge") for error in errors)


def test_ambiguous_rate_is_separate_from_exact_ownership():
    annotation = _complete_annotation()
    annotation["relationships"].append(
        {
            "relationship_id": "rel-ambiguous", "source_id": "", "target_id": "value-1",
            "relationship_type": "AMBIGUOUS", "page_number": 3, "human_confirmed": True,
        }
    )
    validation = validate_page_annotation(annotation)
    assert validation["valid"] is True
    assert validation["ambiguous_count"] == 1
    assert validation["ownership_annotation_count"] == 2
    assert validation["ambiguous_rate"] == 0.5


def test_canonical_digest_ignores_mutable_audit_metadata():
    annotation = _complete_annotation()
    first = canonical_gold_digest([annotation])
    annotation["audit"]["last_saved_at"] = "later"
    assert canonical_gold_digest([annotation]) == first
    annotation["blocks"][1]["bbox"][0] += 1
    assert canonical_gold_digest([annotation]) != first


def test_freeze_and_load_accept_only_confirmed_dev_gold(tmp_path):
    output = tmp_path / "dev-gold.json"
    payload = freeze_dev_gold([_complete_annotation()], output)
    assert payload["final_gate_consumed"] is False
    assert load_confirmed_dev_gold(output)[0]["split"] == "DEV"
    changed = json.loads(output.read_text(encoding="utf-8"))
    changed["pages"][0]["blocks"][0]["bbox"][0] += 1
    output.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        load_confirmed_dev_gold(output)


def test_high_risk_sampling_allows_overlapping_slices_and_caps_ordinary():
    candidates = []
    for index in range(20):
        candidates.append({"doc_id": f"doc_{index % 3 + 1:03d}", "page_number": index + 1, "slices": ["ordinary_text"], "risk_score": 0})
    candidates.extend(
        [
            {"doc_id": "doc_010", "page_number": 1, "slices": ["multi_column", "merged_header", "unit_column"], "risk_score": 9},
            {"doc_id": "doc_010", "page_number": 2, "slices": ["cross_page", "mixed_layout", "simple_table"], "risk_score": 7},
            {"doc_id": "doc_011", "page_number": 1, "slices": ["borderless_table", "model_comparison"], "risk_score": 5},
        ]
    )
    selected, distribution, missing = select_dev_pages(candidates, max_pages=10)
    assert selected[0]["doc_id"].startswith("doc_")
    assert distribution["ordinary_text"] <= 5
    assert distribution["multi_column"] == 1
    assert "multi_column" not in missing


def test_annotation_ui_defaults_to_hidden_predictions():
    static = Path(__file__).parent / "layout_research" / "annotation_tool" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "app.js").read_text(encoding="utf-8")
    assert 'id="show-predictions" type="checkbox"' in html
    assert "$('show-predictions').checked = false" in script
    assert "human_confirmed: false" in script
