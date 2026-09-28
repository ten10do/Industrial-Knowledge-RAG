from dataclasses import replace
import json
from pathlib import Path

import pytest

from backend.layout_research.evaluator import (
    apply_frozen_gates,
    evaluate_pages,
    stable_id_reproducibility,
)
from backend.layout_research.geometry import intersection_over_union, normalize_bbox
from backend.layout_research.models import (
    BBox,
    DocumentLayout,
    HeaderRelationship,
    stable_id,
)
from backend.layout_research.synthetic import (
    gold_page,
    load_synthetic_fixture,
    predict_synthetic_case,
    synthetic_cases,
    write_synthetic_fixture,
)


FIXTURE_PATH = Path(__file__).parent / "layout_research" / "fixtures" / "synthetic_cases.json"


def _cases():
    return load_synthetic_fixture(FIXTURE_PATH)


def _case(case_id):
    return next(item for item in _cases() if item["case_id"] == case_id)


def _evaluate(gold, predicted):
    return evaluate_pages([(gold, predicted, "test")])


def test_bbox_normalization_and_rotation_contract():
    box, width, height = normalize_bbox(
        (10, 20, 30, 40), page_width=100, page_height=200, origin="bottom-left"
    )
    assert box == BBox(10, 160, 30, 180)
    assert (width, height) == (100, 200)
    rotated, width, height = normalize_bbox(
        (10, 20, 30, 40), page_width=100, page_height=200, origin="top-left", rotation=90
    )
    assert rotated == BBox(160, 10, 180, 30)
    assert (width, height) == (200, 100)


def test_bbox_normalization_rejects_invalid_contract():
    with pytest.raises(ValueError):
        normalize_bbox((0, 0, 1, 1), page_width=1, page_height=1, origin="center")
    with pytest.raises(ValueError):
        normalize_bbox((0, 0, 1, 1), page_width=1, page_height=1, origin="top-left", rotation=45)


def test_stable_ids_are_deterministic_and_input_sensitive():
    assert stable_id("block", "doc", 1, [1.0, 2.0]) == stable_id("block", "doc", 1, [1.0, 2.0])
    assert stable_id("block", "doc", 1, [1.0, 2.0]) != stable_id("block", "doc", 2, [1.0, 2.0])


def test_synthetic_fixture_has_required_cases_and_slices(tmp_path):
    output = tmp_path / "fixture.json"
    write_synthetic_fixture(output)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert len(payload["cases"]) >= 10
    assert {
        "ordinary_text",
        "simple_table",
        "borderless_table",
        "merged_header",
        "model_comparison",
        "multi_column",
        "cross_page",
        "mixed_layout",
    } <= {item["slice"] for item in payload["cases"]}


def test_row_column_grouping_and_header_ownership():
    predicted = predict_synthetic_case(_case("two_column_specification"))
    assert len(predicted.table_regions) == 1
    assert len(predicted.rows) == 3
    assert len(predicted.columns) == 2
    ownership = [edge for edge in predicted.relationships if edge.relationship_type == "HEADER_APPLIES_TO"]
    assert len(ownership) == 4


def test_multi_level_merged_header_is_preserved():
    predicted = predict_synthetic_case(_case("merged_header"))
    values = {cell.cell_id: cell for cell in predicted.cells if any(character.isdigit() for character in cell.text)}
    assert values
    assert all(len(cell.header_ids) >= 2 for cell in values.values())


def test_ambiguous_ownership_is_not_forced():
    predicted = predict_synthetic_case(_case("ambiguous_ownership"))
    ambiguous = [edge for edge in predicted.relationships if edge.relationship_type == "AMBIGUOUS"]
    assert ambiguous
    forced_values = {
        edge.value_block_id
        for edge in predicted.relationships
        if edge.relationship_type == "HEADER_APPLIES_TO"
    }
    assert all(edge.value_block_id not in forced_values for edge in ambiguous)


def test_table_region_iou():
    assert intersection_over_union(BBox(0, 0, 10, 10), BBox(0, 0, 10, 10)) == 1.0
    assert intersection_over_union(BBox(0, 0, 10, 10), BBox(20, 20, 30, 30)) == 0.0


def test_section_attribution_and_reading_order():
    case = _case("section_and_table")
    report = _evaluate(gold_page(case), predict_synthetic_case(case))
    assert report["metrics"]["section_attribution_accuracy"] == 1.0
    assert report["metrics"]["reading_order_accuracy"] == 1.0


def test_evaluator_is_exact_on_identical_layout():
    gold = gold_page(_case("three_model_comparison"))
    report = _evaluate(gold, gold)
    assert report["metrics"]["block_type_accuracy"] == 1.0
    assert report["metrics"]["table_region_f1"] == 1.0
    assert report["metrics"]["header_value_ownership_f1"] == 1.0
    assert report["metrics"]["page_provenance_accuracy"] == 1.0
    assert report["metrics"]["false_merge_rate"] == 0.0


def test_mutation_wrong_header_and_swapped_value_are_detected():
    gold = gold_page(_case("three_model_comparison"))
    edges = list(gold.relationships)
    first = edges[0]
    wrong_header = next(cell.cell_id for cell in gold.cells if cell.cell_id != first.header_block_id)
    wrong_value = next(cell.cell_id for cell in gold.cells if cell.cell_id != first.value_block_id)
    edges[0] = replace(first, header_block_id=wrong_header, value_block_id=wrong_value)
    mutated = replace(gold, relationships=tuple(edges))
    report = _evaluate(gold, mutated)
    assert report["metrics"]["header_value_ownership_precision"] < 1.0
    assert report["error_taxonomy"]["HEADER_MISBIND"] >= 1


def test_ownership_diagnostics_are_exact_and_deterministic():
    gold = gold_page(_case("two_column_specification"))
    report = _evaluate(gold, gold)
    diagnostics = report["ownership_diagnostics"]
    assert diagnostics["metrics"]["row_header_ownership_precision"] == 1.0
    assert diagnostics["metrics"]["column_header_ownership_precision"] == 1.0
    assert sum(diagnostics["misbind_taxonomy"].values()) == 0


def test_unit_and_model_misbinds_are_counted_separately():
    gold = gold_page(_case("two_column_specification"))
    target = gold.relationships[0].value_block_id
    wrong_source = next(
        cell.cell_id for cell in gold.cells
        if cell.cell_id not in {edge.header_block_id for edge in gold.relationships}
    )
    cells = tuple(
        replace(cell, text="V" if cell.cell_id == wrong_source else cell.text)
        for cell in gold.cells
    )
    unit_edge = HeaderRelationship(
        relationship_id="unit-misbind",
        header_block_id=wrong_source,
        value_block_id=target,
        relationship_type="HEADER_APPLIES_TO",
        page_number=gold.page_number,
        table_region_id=gold.table_regions[0].table_region_id,
    )
    unit_report = _evaluate(gold, replace(gold, cells=cells, relationships=gold.relationships + (unit_edge,)))
    assert unit_report["error_taxonomy"]["UNIT_MISBIND"] == 1
    assert unit_report["ownership_diagnostics"]["counts"]["UNIT"]["false"] == 1

    model_cells = tuple(
        replace(cell, text="Model-A1" if cell.cell_id == wrong_source else cell.text)
        for cell in gold.cells
    )
    model_report = _evaluate(
        gold,
        replace(gold, cells=model_cells, relationships=gold.relationships + (replace(unit_edge, relationship_id="model-misbind"),)),
    )
    assert model_report["error_taxonomy"]["MODEL_COLUMN_MISBIND"] == 1
    assert model_report["ownership_diagnostics"]["counts"]["MODEL_COLUMN"]["false"] == 1


def test_mutation_missing_and_extra_ownership_are_detected():
    gold = gold_page(_case("two_column_specification"))
    missing = replace(gold, relationships=gold.relationships[1:])
    missing_report = _evaluate(gold, missing)
    assert missing_report["metrics"]["header_value_ownership_recall"] < 1.0
    first = gold.relationships[0]
    extra = HeaderRelationship(
        relationship_id="mutation-extra",
        header_block_id=first.value_block_id,
        value_block_id=first.header_block_id,
        relationship_type="HEADER_APPLIES_TO",
        page_number=1,
        table_region_id=first.table_region_id,
    )
    extra_report = _evaluate(gold, replace(gold, relationships=gold.relationships + (extra,)))
    assert extra_report["metrics"]["header_value_ownership_precision"] < 1.0


def test_mutation_wrong_page_is_detected():
    gold = gold_page(_case("two_column_specification"))
    blocks = (replace(gold.blocks[0], page_number=99),) + gold.blocks[1:]
    report = _evaluate(gold, replace(gold, blocks=blocks))
    assert report["metrics"]["page_provenance_accuracy"] < 1.0
    assert report["error_taxonomy"]["PAGE_PROVENANCE_ERROR"] == 1


def test_mutation_wrong_table_region_is_detected():
    gold = gold_page(_case("two_column_specification"))
    region = replace(gold.table_regions[0], bbox=BBox(400, 500, 500, 600))
    report = _evaluate(gold, replace(gold, table_regions=(region,)))
    assert report["metrics"]["table_region_recall"] == 0.0
    assert report["error_taxonomy"]["TABLE_MISSED"] == 1


def test_mutation_merged_columns_is_detected():
    gold = gold_page(_case("three_model_comparison"))
    first, second, *remaining = gold.columns
    merged = replace(first, cell_ids=first.cell_ids + second.cell_ids)
    report = _evaluate(gold, replace(gold, columns=(merged, *remaining)))
    assert report["metrics"]["false_merge_rate"] > 0.0
    assert report["error_taxonomy"]["COLUMN_MERGE"] == 1


def test_page_provenance_requires_in_bounds_source_boxes():
    gold = gold_page(_case("two_column_specification"))
    blocks = (replace(gold.blocks[0], bbox=BBox(0, 0, 1000, 1000)),) + gold.blocks[1:]
    assert _evaluate(gold, replace(gold, blocks=blocks))["metrics"]["page_provenance_accuracy"] < 1.0


def test_stable_id_reproducibility_requires_exact_output():
    page = predict_synthetic_case(_case("two_column_specification"))
    layout = DocumentLayout(page.document_id, (page,), page.parser_name, page.parser_version, "config")
    assert stable_id_reproducibility([layout, layout, layout]) == 1.0
    changed = replace(layout, config_digest="changed")
    assert stable_id_reproducibility([layout, changed]) == 0.0


def test_synthetic_only_cannot_produce_go():
    metrics = {
        "block_type_accuracy": 1.0,
        "table_region_recall": 1.0,
        "section_attribution_accuracy": 1.0,
        "header_value_ownership_precision": 1.0,
        "false_merge_rate": 0.0,
        "page_provenance_accuracy": 1.0,
        "stable_id_reproducibility": 1.0,
        "parser_fatal_failure_rate": 0.0,
    }
    assert apply_frozen_gates(metrics, real_human_gold=False)["decision"] == "LAYOUT_ANNOTATION_REQUIRED"


def test_annotation_schema_and_template_are_machine_readable():
    root = Path(__file__).parent / "layout_research"
    schema = json.loads((root / "annotation_schema.json").read_text(encoding="utf-8"))
    template = json.loads((root / "annotation_pack_template.json").read_text(encoding="utf-8"))
    assert schema["properties"]["status"]
    assert template["status"] == "ANNOTATION_PACK_READY"
    assert template["human_gold_available"] is False
