"""Structured Graph V3 tests (Phase LA-1.2).

Synthetic conformance gate on the 22 canonical fixtures:
- typed DIRECT edge conformance (Direct Edge P/R = 1.0)
- canonical evaluator full agreement (Context P/R = 1.0 per axis)
- ambiguity preservation (first-class, never dropped, never guessed)
- stable ids across repeated builds
- frozen table detection reuse
- V3-specific contract tests (A-O) and V3 mutation suite

The historical parser behaviour, gold fixtures and frozen evaluator are
never modified.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from backend.layout_research.canonical_evaluator import (
    DIRECT_EVAL_RELATIONS,
    evaluate_canonical_pages,
)
from backend.layout_research.canonical_fixtures import (
    canonical_fixtures,
    fixtures_digest,
)
from backend.layout_research.models import BBox, HeaderRelationship, TableCell
from backend.layout_research.ownership_contract import (
    CONTEXT_AXES,
    ContextGraph,
    DIRECT_RELATIONS,
)
from backend.layout_research.structured_graph_v3 import (
    PARSER_NAME,
    AmbiguousRelationV3,
    PdfPlumberStructuredGraphV3Parser,
    StructuredChunkCandidateV3,
    build_structured_graph_v3,
    structured_chunk_candidates_v3,
)

FIXTURES = canonical_fixtures()
FIXTURE_BY_ID = {build.spec.fixture_id: build for build in FIXTURES}
SCORED_AXES = ("MODEL", "UNIT", "ROW", "COLUMN", "MULTI_LEVEL", "SECTION")


def _fixture(fixture_id: str):
    return FIXTURE_BY_ID[fixture_id]


def _cell_id(fixture_id: str, row: int, col: int) -> str:
    return f"{fixture_id}-c{row}-{col}"


def _typed_eval_edges(v3):
    return {
        (edge.source_id, edge.target_id, edge.relation)
        for edge in v3.graph.direct_edges
        if edge.relation in DIRECT_EVAL_RELATIONS
    }


def _scored_ambiguity(v3):
    return {
        (record.value_id, record.axis)
        for record in v3.graph.ambiguous_relations
        if record.axis in SCORED_AXES and record.value_id
    }


def _edge(v3, source_id: str, target_id: str, relation: str) -> bool:
    return (source_id, target_id, relation) in _typed_eval_edges(v3) or any(
        edge.source_id == source_id and edge.target_id == target_id and edge.relation == relation
        for edge in v3.graph.direct_edges
    )


# ---------------------------------------------------------------------------
# Synthetic conformance gate (22/22)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("build", FIXTURES, ids=lambda build: build.spec.fixture_id)
def test_v3_direct_edges_match_gold_exactly(build):
    v3 = build_structured_graph_v3(build.page)
    assert _typed_eval_edges(v3) == {edge for edge in build.direct if edge[2] in DIRECT_EVAL_RELATIONS}


@pytest.mark.parametrize("build", FIXTURES, ids=lambda build: build.spec.fixture_id)
def test_v3_canonical_evaluator_full_agreement(build):
    result = evaluate_canonical_pages([(build.page, build_structured_graph_v3(build.page).page)])
    for key, value in result["metrics"].items():
        if key.endswith(("_precision", "_recall")):
            assert value == 1.0, (build.spec.fixture_id, key, value)


@pytest.mark.parametrize("build", FIXTURES, ids=lambda build: build.spec.fixture_id)
def test_v3_ambiguity_is_first_class_and_exact(build):
    v3 = build_structured_graph_v3(build.page)
    assert _scored_ambiguity(v3) == build.ambiguous
    for record in v3.graph.ambiguous_relations:
        assert record.candidate_ids, (build.spec.fixture_id, record)
        assert record.reason


@pytest.mark.parametrize("build", FIXTURES, ids=lambda build: build.spec.fixture_id)
def test_v3_never_outputs_legacy_closure(build):
    v3 = build_structured_graph_v3(build.page)
    assert all(edge.relationship_type != "HEADER_APPLIES_TO" for edge in v3.page.relationships)
    assert all(edge.relation != "HEADER_APPLIES_TO" for edge in v3.graph.direct_edges)


@pytest.mark.parametrize("build", FIXTURES, ids=lambda build: build.spec.fixture_id)
def test_v3_derived_context_matches_gold_cells(build):
    v3 = build_structured_graph_v3(build.page)
    cells = {cell.cell_id: cell for cell in build.page.cells}
    for value_id in sorted({value for _, _, value in build.context} | {value for value, _ in build.ambiguous}):
        derived = v3.graph.derived_context[value_id]
        for axis, member, value in build.context:
            if value != value_id or axis == "SECTION":
                continue
            assert member in derived.axis_members(axis), (build.spec.fixture_id, value_id, axis, derived)
        for value, axis in build.ambiguous:
            if value == value_id:
                assert axis in derived.ambiguous_axes, (build.spec.fixture_id, value_id, axis)


def test_v3_fixture_inventory_gate():
    assert len(FIXTURES) == 22
    assert fixtures_digest() == fixtures_digest()


# ---------------------------------------------------------------------------
# Stable ids and frozen detection
# ---------------------------------------------------------------------------


def test_v3_stable_ids_across_repeated_builds():
    build = _fixture("fx02")
    digests = set()
    edge_ids = set()
    for _ in range(3):
        v3 = build_structured_graph_v3(build.page)
        digests.add(v3.graph.digest)
        edge_ids.add(tuple(sorted(edge.edge_id for edge in v3.graph.direct_edges)))
    assert len(digests) == 1
    assert len(edge_ids) == 1


def test_v3_direct_edge_ids_are_deterministic_functions_of_input():
    first = build_structured_graph_v3(_fixture("fx10").page)
    second = build_structured_graph_v3(_fixture("fx10").page)
    assert [edge.edge_id for edge in first.graph.direct_edges] == [edge.edge_id for edge in second.graph.direct_edges]


@pytest.mark.parametrize("build", FIXTURES, ids=lambda build: build.spec.fixture_id)
def test_v3_table_detection_is_frozen(build):
    v3 = build_structured_graph_v3(build.page)
    assert v3.page.table_regions == build.page.table_regions
    assert v3.page.cells == build.page.cells
    assert v3.graph.table_regions == build.page.table_regions


def test_v3_parser_identity_and_parallel_parsers_intact():
    assert PdfPlumberStructuredGraphV3Parser.name == "PDFPLUMBER_STRUCTURED_GRAPH_V3"
    from backend.layout_research.parsers import (
        FlatBaselineParser,
        PdfPlumberStructuredOwnershipParser,
        PdfPlumberStructuredOwnershipV2Parser,
        PdfPlumberTableParser,
    )

    assert PdfPlumberTableParser.name == "PDFPLUMBER_TABLE"
    assert PdfPlumberStructuredOwnershipParser.name == "PDFPLUMBER_STRUCTURED_OWNERSHIP_V1"
    assert PdfPlumberStructuredOwnershipV2Parser.name == "PDFPLUMBER_STRUCTURED_OWNERSHIP_V2"
    assert FlatBaselineParser.name == "FLAT_BASELINE"


# ---------------------------------------------------------------------------
# Header tree, axes and roles
# ---------------------------------------------------------------------------


def test_v3_header_tree_multi_level_chain():
    build = _fixture("fx02")
    v3 = build_structured_graph_v3(build.page)
    assert _edge(v3, _cell_id("fx02", 0, 1), _cell_id("fx02", 1, 1), "HEADER_PARENT_OF_HEADER")
    assert _edge(v3, _cell_id("fx02", 0, 1), _cell_id("fx02", 1, 2), "HEADER_PARENT_OF_HEADER")
    assert not _edge(v3, _cell_id("fx02", 0, 1), _cell_id("fx02", 2, 1), "COLUMN_HEADER_OF")


def test_v3_header_tree_three_children():
    build = _fixture("fx11")
    v3 = build_structured_graph_v3(build.page)
    for col in (1, 2, 3):
        assert _edge(v3, _cell_id("fx11", 0, 1), _cell_id("fx11", 1, col), "HEADER_PARENT_OF_HEADER")
    group = v3.graph.column_axes[0].column_groups[0]
    assert group.header_id == _cell_id("fx11", 0, 1)
    assert set(group.covered_leaf_column_ids) == {
        f"fx11-col-{col}" for col in (1, 2, 3)
    }


def test_v3_role_classification_specific_wins():
    fx03 = build_structured_graph_v3(_fixture("fx03").page)
    roles_fx03 = {node.header_id: node.role for node in fx03.graph.header_nodes}
    assert roles_fx03[_cell_id("fx03", 1, 1)] == "MODEL_HEADER"
    assert roles_fx03[_cell_id("fx03", 0, 1)] == "SPANNING_HEADER"

    fx05 = build_structured_graph_v3(_fixture("fx05").page)
    roles_fx05 = {node.header_id: node.role for node in fx05.graph.header_nodes}
    assert roles_fx05[_cell_id("fx05", 1, 1)] == "UNIT_HEADER"

    fx01 = build_structured_graph_v3(_fixture("fx01").page)
    roles_fx01 = {node.header_id: node.role for node in fx01.graph.header_nodes}
    assert roles_fx01[_cell_id("fx01", 0, 0)] == "GENERIC_COLUMN_HEADER"

    fx14 = build_structured_graph_v3(_fixture("fx14").page)
    roles_fx14 = {node.header_id: node.role for node in fx14.graph.header_nodes}
    assert roles_fx14[_cell_id("fx14", 0, 0)] == "MODEL_HEADER"


def test_v3_row_axis_declares_row_header_column():
    build = _fixture("fx01")
    v3 = build_structured_graph_v3(build.page)
    axis = v3.graph.row_axes[0]
    assert axis.row_header_cell_ids == (_cell_id("fx01", 1, 0), _cell_id("fx01", 2, 0))
    assert axis.row_header_column_ids == ("fx01-col-0",)


def test_v3_column_axis_positive_overlap_group_membership():
    build = _fixture("fx03")
    v3 = build_structured_graph_v3(build.page)
    axis = v3.graph.column_axes[0]
    assert [group.header_id for group in axis.column_groups] == [_cell_id("fx03", 0, 1)]
    assert set(axis.column_groups[0].covered_leaf_column_ids) == {"fx03-col-1", "fx03-col-2"}


def test_v3_column_axis_competition_reported_not_guessed():
    build = _fixture("fx14")
    v3 = build_structured_graph_v3(build.page)
    axis = v3.graph.column_axes[0]
    assert axis.ambiguous_leaf_column_ids == ("fx14-col-1",)


# ---------------------------------------------------------------------------
# V3-specific contract tests (A-O)
# ---------------------------------------------------------------------------


def test_v3_a_model_header_wins_over_generic_column_role():
    build = _fixture("fx03")
    v3 = build_structured_graph_v3(build.page)
    assert _edge(v3, _cell_id("fx03", 1, 1), _cell_id("fx03", 2, 1), "MODEL_OF")
    assert not _edge(v3, _cell_id("fx03", 1, 1), _cell_id("fx03", 2, 1), "COLUMN_HEADER_OF")


def test_v3_b_unit_header_wins_over_generic_column_role():
    build = _fixture("fx05")
    v3 = build_structured_graph_v3(build.page)
    assert _edge(v3, _cell_id("fx05", 1, 1), _cell_id("fx05", 2, 1), "UNIT_OF")
    assert not _edge(v3, _cell_id("fx05", 1, 1), _cell_id("fx05", 2, 1), "COLUMN_HEADER_OF")


def test_v3_c_pure_value_semantic_role_rule():
    build = _fixture("fx03")
    v3 = build_structured_graph_v3(build.page)
    # Model headers emit MODEL_OF only to pure values; they never receive
    # semantic-role edges themselves.
    assert not any(
        edge.target_id == _cell_id("fx03", 1, 1) and edge.relation in {"MODEL_OF", "UNIT_OF"}
        for edge in v3.graph.direct_edges
    )
    derived = v3.graph.derived_context[_cell_id("fx03", 1, 1)]
    assert not derived.model and not derived.unit


def test_v3_d_edge_touch_is_not_overlap():
    build = _fixture("fx01")
    v3 = build_structured_graph_v3(build.page)
    # (0,0) ends exactly where the value's column starts: edge contact,
    # never positive overlap, therefore never a parent.
    assert not _edge(v3, _cell_id("fx01", 0, 0), _cell_id("fx01", 1, 1), "COLUMN_HEADER_OF")


def test_v3_e_unique_center_containment_resolves_parent():
    build = _fixture("fx03")
    v3 = build_structured_graph_v3(build.page)
    # The merged header also contains the value centre, but the immediate
    # parent is unique after intermediate exclusion.
    assert _edge(v3, _cell_id("fx03", 1, 1), _cell_id("fx03", 2, 1), "MODEL_OF")
    assert not _edge(v3, _cell_id("fx03", 0, 1), _cell_id("fx03", 2, 1), "COLUMN_HEADER_OF")
    assert not _edge(v3, _cell_id("fx03", 0, 1), _cell_id("fx03", 2, 1), "MODEL_OF")


def test_v3_f_multiple_valid_parents_become_ambiguous():
    build = _fixture("fx14")
    v3 = build_structured_graph_v3(build.page)
    records = {(record.axis, record.value_id): record for record in v3.graph.ambiguous_relations}
    column = records[("COLUMN", _cell_id("fx14", 1, 1))]
    model = records[("MODEL", _cell_id("fx14", 1, 1))]
    assert set(column.candidate_ids) == {_cell_id("fx14", 0, 0), _cell_id("fx14", 0, 1)}
    assert set(model.candidate_ids) == {_cell_id("fx14", 0, 0), _cell_id("fx14", 0, 1)}
    assert column.reason == "COMPETING_PARENTS"
    # No guessed edge from either competitor to the contested value.
    for competitor in (_cell_id("fx14", 0, 0), _cell_id("fx14", 0, 1)):
        for relation in ("COLUMN_HEADER_OF", "MODEL_OF"):
            assert not _edge(v3, competitor, _cell_id("fx14", 1, 1), relation)


def test_v3_g_row_label_column_header_parent_chain():
    build = _fixture("fx22")
    v3 = build_structured_graph_v3(build.page)
    assert _edge(v3, _cell_id("fx22", 0, 0), _cell_id("fx22", 1, 0), "COLUMN_HEADER_OF")
    assert _edge(v3, _cell_id("fx22", 0, 0), _cell_id("fx22", 2, 0), "COLUMN_HEADER_OF")
    derived = v3.graph.derived_context[_cell_id("fx22", 1, 0)]
    assert derived.column_header == (_cell_id("fx22", 0, 0),)


def test_v3_h_merged_header_child_span():
    build = _fixture("fx11")
    v3 = build_structured_graph_v3(build.page)
    # The spanning header covers three leaf columns via the header tree;
    # it never propagates directly onto the values.
    for col in (1, 2, 3):
        assert not _edge(v3, _cell_id("fx11", 0, 1), _cell_id("fx11", 2, col), "COLUMN_HEADER_OF")
        assert _edge(v3, _cell_id("fx11", 1, col), _cell_id("fx11", 2, col), "COLUMN_HEADER_OF")


def test_v3_i_spanning_context_via_intermediate_node():
    build = _fixture("fx02")
    v3 = build_structured_graph_v3(build.page)
    derived = v3.graph.derived_context[_cell_id("fx02", 2, 1)]
    assert derived.column_header == (_cell_id("fx02", 1, 1),)
    assert derived.multi_level_parents == (_cell_id("fx02", 0, 1),)


def test_v3_j_diagram_adjacent_to_table_is_excluded():
    build = _fixture("fx15")
    v3 = build_structured_graph_v3(build.page)
    diagram_id = "fx15-diagram"
    assert all(edge.source_id != diagram_id and edge.target_id != diagram_id for edge in v3.graph.direct_edges)
    contained = {
        edge.target_id for edge in v3.graph.direct_edges if edge.relation == "TABLE_CONTAINS"
    }
    cells = {cell.cell_id for cell in build.page.cells}
    assert contained == cells


def test_v3_k_caption_describes_table_never_cell_ownership():
    build = _fixture("fx19")
    caption = replace(
        next(block for block in build.page.blocks if block.block_id == "fx19-heading"),
        block_id="fx19-caption",
        block_type="CAPTION",
        text="Table 4 - Mass overview",
        reading_order=5,
    )
    page = replace(build.page, blocks=(*build.page.blocks, caption))
    v3 = build_structured_graph_v3(page)
    assert _edge(v3, "fx19-caption", "fx19-table", "CAPTION_DESCRIBES")
    assert not any(
        edge.relation == "CAPTION_DESCRIBES" and edge.target_id in {cell.cell_id for cell in page.cells}
        for edge in v3.graph.direct_edges
    )


def test_v3_l_section_context_is_not_cell_ownership():
    build = _fixture("fx18")
    v3 = build_structured_graph_v3(build.page)
    heading_id = "fx18-heading"
    assert _edge(v3, heading_id, "fx18-table", "SECTION_CONTAINS")
    assert not any(
        edge.source_id == heading_id and edge.target_id in {cell.cell_id for cell in build.page.cells}
        for edge in v3.graph.direct_edges
    )
    derived = v3.graph.derived_context[_cell_id("fx18", 1, 1)]
    assert derived.section == "4.2 Control Limits"


def test_v3_m_cross_page_no_evidence_no_continuation():
    build = _fixture("fx12")
    v3 = build_structured_graph_v3(build.page)
    assert not any(edge.relation == "TABLE_CONTINUES_ON" for edge in v3.graph.direct_edges)
    # A foreign-page cell can never enter the ownership graph.
    foreign = TableCell(
        cell_id="fx12-foreign",
        bbox=BBox(200.0, 0.0, 300.0, 10.0),
        text="Previous page header",
        page_number=0,
        row_id="fx12-foreign-row",
        column_id="fx12-foreign-col",
    )
    page = replace(build.page, cells=(*build.page.cells, foreign))
    v3_foreign = build_structured_graph_v3(page)
    assert all(
        edge.source_id != foreign.cell_id and edge.target_id != foreign.cell_id
        for edge in v3_foreign.graph.direct_edges
    )


def test_v3_n_stable_direct_edge_ids():
    build = _fixture("fx20")
    runs = [build_structured_graph_v3(build.page) for _ in range(3)]
    first_ids = [edge.edge_id for edge in runs[0].graph.direct_edges]
    assert all([edge.edge_id for edge in run.graph.direct_edges] == first_ids for run in runs[1:])


def test_v3_o_derived_context_deterministic_ordering():
    build = _fixture("fx20")
    first = build_structured_graph_v3(build.page).graph.derived_context
    second = build_structured_graph_v3(build.page).graph.derived_context
    assert first == second
    for context in first.values():
        for axis in CONTEXT_AXES:
            members = context.axis_members(axis)
            assert members == tuple(sorted(members))


# ---------------------------------------------------------------------------
# V3 mutation suite (all captured by the canonical evaluator)
# ---------------------------------------------------------------------------


def _v3_typed_relationships(v3):
    return v3.page.relationships


def test_v3_mutation_wrong_direct_parent_captured():
    build = _fixture("fx01")
    v3 = build_structured_graph_v3(build.page)
    mutated_edges = tuple(
        replace(edge, header_block_id=_cell_id("fx01", 0, 0))
        if edge.header_block_id == _cell_id("fx01", 0, 1) and edge.value_block_id == _cell_id("fx01", 1, 1)
        else edge
        for edge in _v3_typed_relationships(v3)
    )
    mutated = replace(v3.page, relationships=mutated_edges)
    result = evaluate_canonical_pages([(build.page, mutated)])
    assert result["metrics"]["direct_edge_precision"] < 1.0


def test_v3_mutation_merged_over_propagation_captured():
    build = _fixture("fx02")
    v3 = build_structured_graph_v3(build.page)
    over = HeaderRelationship(
        relationship_id="v3-mutation-over",
        header_block_id=_cell_id("fx02", 0, 1),
        value_block_id=_cell_id("fx02", 2, 1),
        relationship_type="COLUMN_HEADER_OF",
        page_number=1,
        table_region_id="fx02-table",
    )
    mutated = replace(v3.page, relationships=(*_v3_typed_relationships(v3), over))
    result = evaluate_canonical_pages([(build.page, mutated)])
    assert result["metrics"]["direct_edge_precision"] < 1.0


def test_v3_mutation_axis_swap_captured():
    build = _fixture("fx01")
    v3 = build_structured_graph_v3(build.page)
    swapped = tuple(
        replace(edge, relationship_type="COLUMN_HEADER_OF")
        if edge.relationship_type == "ROW_HEADER_OF" and edge.value_block_id == _cell_id("fx01", 1, 1)
        else edge
        for edge in _v3_typed_relationships(v3)
    )
    mutated = replace(v3.page, relationships=swapped)
    result = evaluate_canonical_pages([(build.page, mutated)])
    assert result["metrics"]["direct_edge_precision"] < 1.0
    assert result["metrics"]["row_header_of_recall"] < 1.0


def test_v3_mutation_missing_model_captured():
    build = _fixture("fx03")
    v3 = build_structured_graph_v3(build.page)
    pruned = tuple(
        edge for edge in _v3_typed_relationships(v3)
        if not (edge.header_block_id == _cell_id("fx03", 1, 1) and edge.relationship_type == "MODEL_OF")
    )
    mutated = replace(v3.page, relationships=pruned)
    result = evaluate_canonical_pages([(build.page, mutated)])
    assert result["metrics"]["direct_edge_recall"] < 1.0
    assert result["metrics"]["model_of_recall"] < 1.0


def test_v3_mutation_wrong_unit_context_captured():
    build = _fixture("fx05")
    v3 = build_structured_graph_v3(build.page)
    mutated_cells = tuple(
        replace(cell, text="X") if cell.cell_id == _cell_id("fx05", 1, 1) else cell
        for cell in v3.page.cells
    )
    mutated = replace(v3.page, cells=mutated_cells)
    result = evaluate_canonical_pages([(build.page, mutated)])
    assert result["metrics"]["unit_context_recall"] < 1.0


def test_v3_mutation_duplicate_parent_resolves_ambiguous():
    build = _fixture("fx01")
    value_cell = next(cell for cell in build.page.cells if cell.cell_id == _cell_id("fx01", 1, 1))
    competitor = TableCell(
        cell_id="fx01-v3-competitor",
        bbox=BBox(value_cell.bbox.x0 - 20.0, value_cell.bbox.y0 - 26.0, value_cell.bbox.x1 + 20.0, value_cell.bbox.y0 - 8.0),
        text="Ambiguous",
        page_number=1,
        row_id="fx01-r0",
        column_id="fx01-competitor-col",
    )
    mutated_regions = tuple(
        replace(region, cell_ids=(*region.cell_ids, competitor.cell_id))
        for region in build.page.table_regions
    )
    page = replace(
        build.page,
        cells=(*build.page.cells, competitor),
        table_regions=mutated_regions,
    )
    v3 = build_structured_graph_v3(page)
    records = [record for record in v3.graph.ambiguous_relations if record.value_id == value_cell.cell_id]
    assert any(record.axis == "COLUMN" for record in records)
    result = evaluate_canonical_pages([(build.page, v3.page)])
    assert result["metrics"]["column_context_precision"] < 1.0


def test_v3_mutation_header_tree_is_cycle_free():
    for build in FIXTURES:
        v3 = build_structured_graph_v3(build.page)
        parents: dict[str, str] = {}
        for edge in v3.graph.header_tree:
            assert edge.target_id not in parents, (build.spec.fixture_id, edge)
            parents[edge.target_id] = edge.source_id
        for child in parents:
            visited = set()
            node = child
            while node in parents:
                assert node not in visited, (build.spec.fixture_id, "cycle")
                visited.add(node)
                node = parents[node]


def test_v3_mutation_sibling_column_leakage_rejected():
    build = _fixture("fx04")
    v3 = build_structured_graph_v3(build.page)
    assert not _edge(v3, _cell_id("fx04", 0, 2), _cell_id("fx04", 1, 1), "MODEL_OF")
    assert not _edge(v3, _cell_id("fx04", 0, 2), _cell_id("fx04", 1, 1), "COLUMN_HEADER_OF")


def test_v3_mutation_section_misuse_rejected():
    build = _fixture("fx18")
    v3 = build_structured_graph_v3(build.page)
    assert not any(
        edge.source_id == "fx18-heading" and edge.relation in DIRECT_EVAL_RELATIONS
        for edge in v3.graph.direct_edges
    )


# ---------------------------------------------------------------------------
# Structured chunk candidates
# ---------------------------------------------------------------------------


def test_v3_chunk_candidates_are_structural_and_experimental():
    build = _fixture("fx01")
    graph = build_structured_graph_v3(build.page).graph
    edge_ids = {edge.edge_id for edge in graph.direct_edges}
    # One candidate per pure value cell: target of an eval relation that
    # never acts as a source itself.
    sources = {source for source, _, _ in build.direct}
    pure_values = {target for _, target, relation in build.direct if relation in DIRECT_EVAL_RELATIONS} - sources
    candidates = structured_chunk_candidates_v3(graph)
    assert {candidate.value_cell_id for candidate in candidates} == pure_values
    for candidate in candidates:
        assert candidate.text
        assert candidate.document_id == "canonical-fx01"
        assert candidate.page_number == 1
        assert candidate.table_id == "fx01-table"
        assert set(candidate.direct_edge_ids) <= edge_ids
        assert candidate.source_bboxes
        assert candidate.chunk_id.startswith("v3chunk-")
        assert "COLUMN" in candidate.derived_context
