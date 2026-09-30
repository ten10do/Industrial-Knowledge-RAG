"""Canonical ownership contract tests (Phase LA-1.1).

Covers: contract registry integrity, fixture truth reproduction
(contract mapping + context derivation), the canonical evaluator, and
the required mutation suite. The historical evaluator and gold are
never touched.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from backend.layout_research.canonical_evaluator import (
    DIRECT_EVAL_RELATIONS,
    context_edges,
    direct_edges,
    evaluate_canonical_pages,
)
from backend.layout_research.canonical_fixtures import (
    CanonicalFixtureSpec,
    CellSpec,
    build_fixture,
    canonical_fixtures,
    fixtures_digest,
)
from backend.layout_research.models import BBox, HeaderRelationship, TableCell
from backend.layout_research.ownership_contract import (
    AMBIGUOUS,
    CONTRACTS,
    DERIVED_RELATIONS,
    DIRECT_RELATIONS,
    LEGACY_HEADER_APPLIES_TO,
    RELATION_TYPES,
    ContextGraph,
    classify_legacy_page,
    derive_context_from_direct_edges,
    map_legacy_page,
)


# ---------------------------------------------------------------------------
# Contract registry
# ---------------------------------------------------------------------------


def test_contract_registry_covers_every_relation():
    assert set(RELATION_TYPES) == set(CONTRACTS)
    for relation in DIRECT_RELATIONS:
        contract = CONTRACTS[relation]
        assert contract.storage == "DIRECT"
        assert contract.stored_directly is True
        assert contract.transitive is False
        assert contract.evaluation_rule
        assert contract.ambiguity_behavior
    for relation in DERIVED_RELATIONS:
        contract = CONTRACTS[relation]
        assert contract.storage == "DERIVED"
        assert contract.stored_directly is False
        assert contract.transitive is True


def test_legacy_header_applies_to_is_derived_only():
    contract = CONTRACTS[LEGACY_HEADER_APPLIES_TO]
    assert contract.storage == "DERIVED"
    assert contract.stored_directly is False
    assert contract.transitive is True


# ---------------------------------------------------------------------------
# Fixture truth reproduction
# ---------------------------------------------------------------------------


FIXTURES = canonical_fixtures()


def _value_cells(build):
    return [cell for cell in build.page.cells if cell.cell_id in {v for _, v, _ in build.direct} | {v for _, _, v in build.context} | {v for v, _ in build.ambiguous}]


@pytest.mark.parametrize("build", FIXTURES, ids=lambda build: build.spec.fixture_id)
def test_fixture_mapping_recovers_hand_direct_edges(build):
    mapping = map_legacy_page(build.page)
    mapped_direct = {
        (item.header_id, item.value_id, item.direct_relation)
        for item in mapping
        if item.mapping == "DIRECT" and item.direct_relation in DIRECT_EVAL_RELATIONS
    }
    assert mapped_direct == {edge for edge in build.direct if edge[2] in DIRECT_EVAL_RELATIONS}


@pytest.mark.parametrize("build", FIXTURES, ids=lambda build: build.spec.fixture_id)
def test_fixture_context_derivation_matches_hand_truth(build):
    graph = ContextGraph(build.page)
    cells = {cell.cell_id: cell for cell in build.page.cells}
    values = {value for _, _, value in build.context} | {value for value, _ in build.ambiguous}
    ambiguous_axes = {(value, axis) for value, axis in build.ambiguous}
    for value_id in sorted(values):
        derived = graph.derive_context(cells[value_id])
        expected_axes = {axis for axis, _, value in build.context if value == value_id}
        for axis in ("MODEL", "UNIT", "ROW", "COLUMN", "MULTI_LEVEL"):
            if axis not in expected_axes and (value_id, axis) not in ambiguous_axes:
                assert not derived.axis_members(axis), (build.spec.fixture_id, value_id, axis, derived)
        for axis, member, value in build.context:
            if value != value_id:
                continue
            if axis == "SECTION":
                assert derived.section
                continue
            assert member in derived.axis_members(axis), (build.spec.fixture_id, value_id, axis, derived)
        for value, axis in build.ambiguous:
            if value == value_id:
                assert axis in derived.ambiguous_axes, (build.spec.fixture_id, value_id, axis)


@pytest.mark.parametrize("build", FIXTURES, ids=lambda build: build.spec.fixture_id)
def test_canonical_evaluator_self_agreement(build):
    result = evaluate_canonical_pages([(build.page, build.page)])
    metrics = result["metrics"]
    for key, value in metrics.items():
        if key.endswith(("_precision", "_recall")):
            assert value == 1.0, (build.spec.fixture_id, key, value)


def test_fixture_inventory_and_digest_stable():
    assert len(FIXTURES) >= 20
    scenarios = {build.spec.scenario for build in FIXTURES}
    required = {
        "single_level_columns", "multi_level_header", "merged_header", "model_comparison",
        "unit_row", "unit_column", "row_header", "parameter_code", "fault_code",
        "nested_header", "spanning_header", "cross_page_continuation",
        "duplicate_visual_header", "ambiguous_model_header", "diagram_adjacent_to_table",
        "borderless_table",
    }
    assert required <= scenarios
    assert fixtures_digest() == fixtures_digest()


# ---------------------------------------------------------------------------
# Mutation tests
# ---------------------------------------------------------------------------


def _add_flat_edge(build, header_id: str, value_id: str):
    page = build.page
    edge = HeaderRelationship(
        relationship_id=f"mutation-{len(page.relationships)}-{header_id}-{value_id}",
        header_block_id=header_id,
        value_block_id=value_id,
        relationship_type="HEADER_APPLIES_TO",
        page_number=1,
        table_region_id=build.page.table_regions[0].table_region_id,
    )
    return replace(page, relationships=(*page.relationships, edge))


def _typed_page(build):
    page = build.page
    typed = tuple(
        HeaderRelationship(
            relationship_id=f"typed-{header}-{value}-{relation}",
            header_block_id=header,
            value_block_id=value,
            relationship_type=relation,
            page_number=1,
            table_region_id=page.table_regions[0].table_region_id,
        )
        for header, value, relation in sorted(build.direct)
    )
    return replace(page, relationships=typed)


def _header_id(build, rc):
    return f"{build.spec.fixture_id}-c{rc[0]}-{rc[1]}"


def test_mutation_wrong_direct_parent_is_detected():
    build = FIXTURES[0]  # fx01 single_level_columns
    header, value, relation = next(
        edge for edge in sorted(build.direct)
        if edge[2] == "COLUMN_HEADER_OF" and edge[0] != _header_id(build, (0, 0))
    )
    wrong_header = _header_id(build, (0, 0))  # row-label header of another column
    typed = _typed_page(build)
    mutated_relationships = tuple(
        replace(edge, header_block_id=wrong_header)
        if edge.header_block_id == header and edge.value_block_id == value
        else edge
        for edge in typed.relationships
    )
    mutated = replace(typed, relationships=mutated_relationships)
    result = evaluate_canonical_pages([(typed, mutated)])
    assert result["metrics"]["direct_edge_precision"] < 1.0
    assert result["metrics"]["column_header_of_precision"] < 1.0


def test_mutation_extra_ancestor_edge_is_derived_not_direct():
    build = next(item for item in FIXTURES if item.spec.fixture_id == "fx02")
    value_id = _header_id(build, (2, 1))
    merged = _header_id(build, (0, 1))
    mapped = map_legacy_page(build.page)
    ancestor_edges = [item for item in mapped if item.header_id == merged and item.value_id == value_id]
    assert ancestor_edges and all(item.mapping == "DERIVED" for item in ancestor_edges)
    direct = direct_edges(build.page)
    assert (merged, value_id, "COLUMN_HEADER_OF") not in direct


def test_mutation_missing_model_context_lowers_recall():
    build = next(item for item in FIXTURES if item.spec.fixture_id == "fx03")
    model_id = _header_id(build, (1, 1))
    page = build.page
    mutated_cells = tuple(cell for cell in page.cells if cell.cell_id != model_id)
    mutated_relationships = tuple(
        edge
        for edge in page.relationships
        if edge.header_block_id != model_id and edge.value_block_id != model_id
    )
    mutated = replace(
        page,
        cells=mutated_cells,
        blocks=tuple(block for block in page.blocks if block.block_id != model_id),
        relationships=mutated_relationships,
    )
    result = evaluate_canonical_pages([(page, mutated)])
    assert result["metrics"]["model_context_recall"] < 1.0


def test_mutation_wrong_unit_lowers_unit_context():
    build = next(item for item in FIXTURES if item.spec.fixture_id == "fx05")
    unit_id = _header_id(build, (1, 1))
    page = build.page
    mutated_cells = tuple(
        replace(cell, text="X") if cell.cell_id == unit_id else cell for cell in page.cells
    )
    mutated = replace(page, cells=mutated_cells)
    result = evaluate_canonical_pages([(page, mutated)])
    assert result["metrics"]["unit_context_recall"] < 1.0


def test_mutation_sibling_column_leakage_is_not_direct():
    build = next(item for item in FIXTURES if item.spec.fixture_id == "fx04")
    value_id = _header_id(build, (1, 1))
    sibling_header = _header_id(build, (0, 2))  # other model column header
    mutated = _add_flat_edge(build, sibling_header, value_id)
    direct = direct_edges(mutated)
    assert (sibling_header, value_id, "MODEL_OF") not in direct
    assert (sibling_header, value_id, "COLUMN_HEADER_OF") not in direct


def test_mutation_merged_header_over_propagation_detected():
    build = next(item for item in FIXTURES if item.spec.fixture_id == "fx02")
    value_id = _header_id(build, (2, 1))
    merged = _header_id(build, (0, 1))
    typed = _typed_page(build)
    over = HeaderRelationship(
        relationship_id="mutation-over-propagation",
        header_block_id=merged,
        value_block_id=value_id,
        relationship_type="COLUMN_HEADER_OF",
        page_number=1,
        table_region_id=typed.table_regions[0].table_region_id,
    )
    mutated = replace(typed, relationships=(*typed.relationships, over))
    result = evaluate_canonical_pages([(typed, mutated)])
    assert result["metrics"]["direct_edge_precision"] < 1.0


def test_mutation_row_column_axis_swap_detected():
    build = next(item for item in FIXTURES if item.spec.fixture_id == "fx01")
    header, value, relation = next(item for item in build.direct if item[2] == "ROW_HEADER_OF")
    typed = _typed_page(build)
    swapped = tuple(
        replace(edge, relationship_type="COLUMN_HEADER_OF")
        if edge.header_block_id == header and edge.value_block_id == value
        else edge
        for edge in typed.relationships
    )
    mutated = replace(typed, relationships=swapped)
    result = evaluate_canonical_pages([(typed, mutated)])
    assert result["metrics"]["direct_edge_precision"] < 1.0
    assert result["metrics"]["row_header_of_recall"] < 1.0


def test_mutation_duplicate_parent_resolves_ambiguous():
    build = next(item for item in FIXTURES if item.spec.fixture_id == "fx01")
    value_cell = next(cell for cell in build.page.cells if cell.cell_id == _header_id(build, (1, 1)))
    competitor = TableCell(
        cell_id=f"fx01-competitor",
        bbox=BBox(value_cell.bbox.x0 - 20.0, value_cell.bbox.y0 - 26.0, value_cell.bbox.x1 + 20.0, value_cell.bbox.y0 - 8.0),
        text="Ambiguous",
        page_number=1,
        row_id="fx01-r0",
        column_id="fx01-competitor-col",
    )
    page = build.page
    mutated_regions = tuple(
        replace(region, cell_ids=(*region.cell_ids, competitor.cell_id))
        for region in page.table_regions
    )
    mutated = replace(
        page,
        cells=(*page.cells, competitor),
        table_regions=mutated_regions,
        relationships=(
            *page.relationships,
            HeaderRelationship(
                relationship_id="mutation-duplicate-parent",
                header_block_id=competitor.cell_id,
                value_block_id=value_cell.cell_id,
                relationship_type="HEADER_APPLIES_TO",
                page_number=1,
                table_region_id=page.table_regions[0].table_region_id,
            ),
        ),
    )
    graph = ContextGraph(mutated)
    derived = graph.derive_context(value_cell)
    assert "COLUMN" in derived.ambiguous_axes
    assert AMBIGUOUS in {header for axis, header, value in context_edges({value_cell.cell_id: derived}) if axis == "COLUMN"}


def test_mutation_cycle_is_safe_and_deterministic():
    edges = {
        ("h1", "h2", "HEADER_PARENT_OF_HEADER"),
        ("h2", "h1", "HEADER_PARENT_OF_HEADER"),
        ("h2", "v1", "COLUMN_HEADER_OF"),
    }
    first = derive_context_from_direct_edges(edges, "v1")
    second = derive_context_from_direct_edges(edges, "v1")
    assert first == second
    assert first.column_header == ("h2",)
    assert set(first.multi_level_parents) <= {"h1", "h2"}


def test_mutation_cross_page_leakage_is_rejected():
    build = next(item for item in FIXTURES if item.spec.fixture_id == "fx01")
    header, value, relation = next(iter(build.direct))
    foreign = TableCell(
        cell_id="fx01-foreign-header",
        bbox=(200.0, 0.0, 300.0, 10.0),
        text="Previous page header",
        page_number=0,
        row_id="fx01-foreign-row",
        column_id="fx01-foreign-col",
    )
    page = build.page
    mutated = replace(
        page,
        cells=(*page.cells, foreign),
        relationships=(
            *page.relationships,
            HeaderRelationship(
                relationship_id="mutation-cross-page",
                header_block_id=foreign.cell_id,
                value_block_id=value,
                relationship_type="HEADER_APPLIES_TO",
                page_number=1,
                table_region_id=page.table_regions[0].table_region_id,
            ),
        ),
    )
    classifications = classify_legacy_page(mutated)
    foreign_edge = next(item for item in classifications if item.header_id == foreign.cell_id)
    assert foreign_edge.taxonomy == "OTHER"
    assert not foreign_edge.direct_candidate


def test_mutation_section_used_as_cell_header_is_not_ownership():
    build = next(item for item in FIXTURES if item.spec.fixture_id == "fx01")
    heading_id = f"{build.spec.fixture_id}-heading"
    _, value, _ = next(iter(build.direct))
    mutated = _add_flat_edge(build, heading_id, value)
    direct = direct_edges(mutated)
    assert all(header != heading_id for header, _, _ in direct)
    assert len(direct_edges(mutated)) == len(direct_edges(build.page))
    # Non-cell blocks are entirely outside the ownership graph: the flat
    # edge from the heading is ignored, never reinterpreted.
    mapped = map_legacy_page(mutated)
    assert all(item.header_id != heading_id for item in mapped)


# ---------------------------------------------------------------------------
# Explicit-graph derivation semantics
# ---------------------------------------------------------------------------


def test_explicit_graph_derivation_reports_ambiguity():
    edges = {
        ("unit_a", "v1", "UNIT_OF"),
        ("unit_b", "v1", "UNIT_OF"),
        ("param", "v1", "COLUMN_HEADER_OF"),
    }
    derived = derive_context_from_direct_edges(edges, "v1")
    assert "UNIT" in derived.ambiguous_axes
    assert derived.column_header == ("param",)


def test_explicit_graph_multi_level_ancestors():
    edges = {
        ("top", "leaf", "HEADER_PARENT_OF_HEADER"),
        ("leaf", "v1", "COLUMN_HEADER_OF"),
        ("row_h", "v1", "ROW_HEADER_OF"),
    }
    derived = derive_context_from_direct_edges(edges, "v1")
    assert derived.multi_level_parents == ("top",)
    assert derived.row_header == ("row_h",)
