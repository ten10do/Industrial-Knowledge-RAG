"""Structured Graph V3 parser (Phase LA-1.2, experimental).

Research-only implementation of the layout-aware structured graph:

    PDF -> Table Region -> Cell Geometry -> Row/Column Axis ->
    Header Role Classification -> Header Tree -> Typed Direct
    Structural Edges -> Ambiguity Representation -> Derived Context ->
    Structured Chunk Candidate

Contract principles enforced here:

1. The primary output is a typed DIRECT graph over the 11 canonical
   DIRECT relations. ``HEADER_APPLIES_TO`` is never stored; it only
   exists as a DERIVED relation produced by the canonical contract.
2. Table region detection is frozen: this parser reuses
   ``PdfPlumberTableParser``'s detection path unchanged and never
   mutates table finder thresholds, line detection, rectangle rules or
   region merging.
3. Build order is geometry first: cell geometry -> axes -> header
   roles -> typed relations. No nearest-text, nearest-bbox-centre or
   closest-header-string fallback is ever used as final authority.
   Unresolvable structural slots are emitted as first-class
   AMBIGUOUS records.
4. Derived context is produced exclusively by the canonical
   ``ContextGraph.derive_context`` authority. No second context logic
   is implemented in this module.
5. All identifiers are deterministic and stable across repeated runs
   on identical input.

The historical V1/V2 parsers, the frozen contract module, the canonical
fixtures and all historical metrics remain untouched.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import importlib.metadata
import json
from pathlib import Path

from .models import (
    BBox,
    DocumentLayout,
    HeaderRelationship,
    PageLayout,
    stable_id,
)
from .ownership_contract import (
    VALUE_TARGET_RELATIONS,
    ContextGraph,
    TableGeometry,
    _VERTICAL_TOLERANCE,
    is_model_text,
    is_unit_text,
)
from .parsers import PdfPlumberTableParser, _config_digest

PARSER_NAME = "PDFPLUMBER_STRUCTURED_GRAPH_V3"
PARSER_VERSION = "1"

HEADER_ROLES = (
    "GENERIC_COLUMN_HEADER",
    "ROW_HEADER",
    "MODEL_HEADER",
    "UNIT_HEADER",
    "MERGED_HEADER",
    "SPANNING_HEADER",
    "SECTION_HEADER",
    "OTHER",
)

_SPAN_RATIO = 1.5
_CAPTION_MAX_GAP = 72.0
_EVAL_RELATIONS = ("COLUMN_HEADER_OF", "ROW_HEADER_OF", "UNIT_OF", "MODEL_OF")


# ---------------------------------------------------------------------------
# V3 output model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TypedDirectEdgeV3:
    """One typed DIRECT structural edge with a stable id."""

    edge_id: str
    source_id: str
    target_id: str
    relation: str
    evidence: str  # GEOMETRY | STRUCTURE | TEXT_ROLE
    table_region_id: str = ""


@dataclass(frozen=True)
class HeaderNodeV3:
    """A header candidate with its classified role."""

    header_id: str
    role: str
    text: str
    bbox: BBox
    table_region_id: str
    page_number: int


@dataclass(frozen=True)
class ColumnGroupV3:
    """A multi-level column group covered by one spanning header."""

    group_id: str
    header_id: str
    covered_leaf_column_ids: tuple[str, ...]


@dataclass(frozen=True)
class ColumnAxisV3:
    """Explicit column axis of one table region."""

    table_region_id: str
    leaf_column_ids: tuple[str, ...]
    column_groups: tuple[ColumnGroupV3, ...]
    ambiguous_leaf_column_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RowAxisV3:
    """Explicit row axis of one table region."""

    table_region_id: str
    row_ids: tuple[str, ...]
    row_header_column_ids: tuple[str, ...]
    row_header_cell_ids: tuple[str, ...]


@dataclass(frozen=True)
class AmbiguousRelationV3:
    """First-class ambiguity record; never silently dropped, never guessed."""

    value_id: str
    axis: str
    candidate_ids: tuple[str, ...]
    reason: str  # COMPETING_PARENTS | GEOMETRY_OVERLAP | OTHER


@dataclass(frozen=True)
class StructuredChunkCandidateV3:
    """Experimental structured chunk candidate (never indexed here)."""

    chunk_id: str
    text: str
    document_id: str
    page_number: int
    table_id: str
    row_id: str
    column_id: str
    value_cell_id: str
    direct_edge_ids: tuple[str, ...]
    derived_context: dict
    ambiguous_context: tuple[str, ...]
    source_bboxes: tuple[BBox, ...]


@dataclass(frozen=True)
class StructuredGraphV3:
    """Primary V3 output: typed DIRECT graph plus derived context."""

    document_id: str
    page_number: int
    parser_name: str
    parser_version: str
    table_regions: tuple
    cells: tuple
    row_axes: tuple[RowAxisV3, ...]
    column_axes: tuple[ColumnAxisV3, ...]
    header_nodes: tuple[HeaderNodeV3, ...]
    header_tree: tuple[TypedDirectEdgeV3, ...]
    direct_edges: tuple[TypedDirectEdgeV3, ...]
    ambiguous_relations: tuple[AmbiguousRelationV3, ...]
    derived_context: dict
    source_bboxes: tuple[BBox, ...]
    digest: str


@dataclass(frozen=True)
class V3Build:
    """Page-level result: canonical PageLayout view plus the V3 graph."""

    page: PageLayout
    graph: StructuredGraphV3


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_structured_graph_v3(
    page: PageLayout,
    *,
    parser_name: str = PARSER_NAME,
    parser_version: str = PARSER_VERSION,
) -> V3Build:
    """Build the typed DIRECT structural graph for one page.

    Input is any page whose table regions and cells are already parsed
    (for PDFs: the frozen ``PdfPlumberTableParser`` detection path). The
    input page's flat candidate ownership edges are used only as zone
    and source-membership signal, exactly like the canonical contract
    functions; every stored edge decision is made from geometry.
    """
    geometries = {
        region.table_region_id: TableGeometry(page, region.table_region_id)
        for region in page.table_regions
    }
    table_of_cell = {
        cell_id: region.table_region_id
        for region in page.table_regions
        for cell_id in region.cell_ids
    }
    candidate_target_ids = {
        edge.value_block_id
        for edge in page.relationships
        if edge.relationship_type in VALUE_TARGET_RELATIONS and edge.value_block_id
    }
    source_ids = {
        edge.header_block_id
        for edge in page.relationships
        if edge.relationship_type in VALUE_TARGET_RELATIONS and edge.header_block_id
    }

    edges: list[TypedDirectEdgeV3] = []
    tree_edges: list[TypedDirectEdgeV3] = []
    ambiguous: list[AmbiguousRelationV3] = []
    header_nodes: list[HeaderNodeV3] = []
    row_axes: list[RowAxisV3] = []
    column_axes: list[ColumnAxisV3] = []
    merged_parents: set[str] = set()
    resolved_parent_targets: set[str] = set()

    def _edge(source: str, target: str, relation: str, evidence: str, region_id: str) -> TypedDirectEdgeV3:
        return TypedDirectEdgeV3(
            edge_id=stable_id("v3edge", region_id, source, target, relation),
            source_id=source,
            target_id=target,
            relation=relation,
            evidence=evidence,
            table_region_id=region_id,
        )

    for region in page.table_regions:
        region_id = region.table_region_id
        geometry = geometries[region_id]
        boundary = geometry.header_boundary
        members = [geometry.cells[cid] for cid in region.cell_ids if cid in geometry.cells]
        members.sort(key=lambda item: (item.bbox.y0, item.bbox.x0, item.cell_id))

        pure_values = [
            cell for cell in members
            if cell.cell_id in candidate_target_ids and cell.cell_id not in source_ids
        ]
        header_zone_ids = {
            cell.cell_id
            for cell in members
            if cell.bbox.y0 < boundary and cell.bbox.y1 <= boundary + _VERTICAL_TOLERANCE
        }
        row_label_ids = {
            cell.cell_id
            for cell in members
            if cell.cell_id in source_ids and cell.cell_id not in header_zone_ids
        }

        # -- pure-value slots: column / model / unit / row -------------------
        for value in pure_values:
            resolved, candidates, ambiguous_slot = geometry.resolve_parent(value)
            if ambiguous_slot:
                ambiguous.append(AmbiguousRelationV3(
                    value_id=value.cell_id,
                    axis="COLUMN",
                    candidate_ids=tuple(sorted(cell.cell_id for cell in candidates)),
                    reason="COMPETING_PARENTS",
                ))
                model_candidates = [cell for cell in candidates if is_model_text(cell.text)]
                if len(model_candidates) > 1:
                    ambiguous.append(AmbiguousRelationV3(
                        value_id=value.cell_id,
                        axis="MODEL",
                        candidate_ids=tuple(sorted(cell.cell_id for cell in model_candidates)),
                        reason="COMPETING_PARENTS",
                    ))
            elif resolved is not None:
                resolved_parent_targets.add(resolved.cell_id)
                if resolved.bbox.width > value.bbox.width * _SPAN_RATIO and resolved.column_id != value.column_id:
                    merged_parents.add(resolved.cell_id)
                if is_unit_text(resolved.text):
                    relation, evidence = "UNIT_OF", "TEXT_ROLE"
                elif is_model_text(resolved.text):
                    relation, evidence = "MODEL_OF", "TEXT_ROLE"
                else:
                    relation, evidence = "COLUMN_HEADER_OF", "GEOMETRY"
                edges.append(_edge(resolved.cell_id, value.cell_id, relation, evidence, region_id))

            row_candidates = geometry.row_header_candidates(value)
            # Unit axis keeps the wide geometric candidates (canonical
            # ContextGraph semantics); the row-header slot is gated on
            # candidate-edge source membership, matching the frozen
            # contract mapping (data cells never compete for the slot).
            unit_row_cells = [cell for cell in row_candidates if is_unit_text(cell.text)]
            row_only = [
                cell for cell in row_candidates
                if cell.cell_id in source_ids and not is_unit_text(cell.text)
            ]
            if len(row_only) > 1:
                ambiguous.append(AmbiguousRelationV3(
                    value_id=value.cell_id,
                    axis="ROW",
                    candidate_ids=tuple(sorted(cell.cell_id for cell in row_only)),
                    reason="COMPETING_PARENTS",
                ))
            elif row_only:
                edges.append(_edge(row_only[0].cell_id, value.cell_id, "ROW_HEADER_OF", "GEOMETRY", region_id))
            units = list(unit_row_cells)
            if resolved is not None and not ambiguous_slot and is_unit_text(resolved.text):
                units.append(resolved)
            if len(units) > 1:
                ambiguous.append(AmbiguousRelationV3(
                    value_id=value.cell_id,
                    axis="UNIT",
                    candidate_ids=tuple(sorted(cell.cell_id for cell in units)),
                    reason="COMPETING_PARENTS",
                ))

        # -- row-label column: data-row structural cells ----------------------
        for cell_id in sorted(row_label_ids):
            cell = geometry.cells[cell_id]
            resolved, candidates, ambiguous_slot = geometry.resolve_parent(cell)
            if ambiguous_slot:
                ambiguous.append(AmbiguousRelationV3(
                    value_id=cell.cell_id,
                    axis="COLUMN",
                    candidate_ids=tuple(sorted(item.cell_id for item in candidates)),
                    reason="COMPETING_PARENTS",
                ))
            elif resolved is not None:
                resolved_parent_targets.add(resolved.cell_id)
                if resolved.bbox.width > cell.bbox.width * _SPAN_RATIO and resolved.column_id != cell.column_id:
                    merged_parents.add(resolved.cell_id)
                edges.append(_edge(resolved.cell_id, cell.cell_id, "COLUMN_HEADER_OF", "GEOMETRY", region_id))

        # -- header tree: header-zone parent/child links ----------------------
        for cell_id in sorted(header_zone_ids):
            cell = geometry.cells[cell_id]
            resolved, candidates, ambiguous_slot = geometry.resolve_parent(cell)
            if ambiguous_slot:
                ambiguous.append(AmbiguousRelationV3(
                    value_id=cell.cell_id,
                    axis="MULTI_LEVEL",
                    candidate_ids=tuple(sorted(item.cell_id for item in candidates)),
                    reason="COMPETING_PARENTS",
                ))
            elif resolved is not None:
                tree_edges.append(_edge(resolved.cell_id, cell.cell_id, "HEADER_PARENT_OF_HEADER", "STRUCTURE", region_id))

        # -- header node roles (specific roles win over generic) --------------
        region_tree_parents = {edge.source_id for edge in tree_edges if edge.table_region_id == region_id}
        for cell_id in sorted(header_zone_ids):
            cell = geometry.cells[cell_id]
            if is_unit_text(cell.text):
                role = "UNIT_HEADER"
            elif is_model_text(cell.text):
                role = "MODEL_HEADER"
            elif cell.cell_id in region_tree_parents:
                role = "SPANNING_HEADER"
            elif cell.cell_id in merged_parents:
                role = "MERGED_HEADER"
            elif cell.cell_id in resolved_parent_targets:
                role = "GENERIC_COLUMN_HEADER"
            else:
                role = "OTHER"
            header_nodes.append(HeaderNodeV3(
                header_id=cell.cell_id,
                role=role,
                text=cell.text,
                bbox=cell.bbox,
                table_region_id=region_id,
                page_number=cell.page_number,
            ))
        for cell_id in sorted(row_label_ids):
            cell = geometry.cells[cell_id]
            header_nodes.append(HeaderNodeV3(
                header_id=cell.cell_id,
                role="ROW_HEADER",
                text=cell.text,
                bbox=cell.bbox,
                table_region_id=region_id,
                page_number=cell.page_number,
            ))

        # -- row axis ---------------------------------------------------------
        row_header_cells = [geometry.cells[cid] for cid in sorted(row_label_ids)]
        row_axes.append(RowAxisV3(
            table_region_id=region_id,
            row_ids=tuple(sorted(region.row_ids)),
            row_header_column_ids=tuple(sorted({cell.column_id for cell in row_header_cells})),
            row_header_cell_ids=tuple(cell.cell_id for cell in row_header_cells),
        ))

        # -- column axis: leaf columns, multi-level groups, ambiguity ---------
        column_axes.append(_build_column_axis(geometry, region, header_zone_ids))

        # -- table containment -------------------------------------------------
        for cell in members:
            edges.append(_edge(region_id, cell.cell_id, "TABLE_CONTAINS", "STRUCTURE", region_id))

        # -- section context (structural object level only) --------------------
        section_edge = _section_edge(page, region)
        if section_edge is not None:
            edges.append(_edge(section_edge, region_id, "SECTION_CONTAINS", "STRUCTURE", region_id))

    # -- captions describe tables, never cell values ---------------------------
    edges.extend(_caption_edges(page, ambiguous))

    # -- derived context: canonical authority reuse (no second logic) ----------
    contexts = ContextGraph(page).derive_all(page)

    ownership_edges = sorted([*edges, *tree_edges], key=lambda item: item.edge_id)
    tree_sorted = tuple(sorted(tree_edges, key=lambda item: item.edge_id))
    ambiguity_view = tuple(sorted(
        ambiguous,
        key=lambda item: (item.value_id, item.axis, item.candidate_ids),
    ))
    all_bboxes = tuple(cell.bbox for cell in page.cells)
    graph = StructuredGraphV3(
        document_id=page.document_id,
        page_number=page.page_number,
        parser_name=parser_name,
        parser_version=parser_version,
        table_regions=page.table_regions,
        cells=page.cells,
        row_axes=tuple(row_axes),
        column_axes=tuple(column_axes),
        header_nodes=tuple(sorted(header_nodes, key=lambda item: item.header_id)),
        header_tree=tree_sorted,
        direct_edges=tuple(ownership_edges),
        ambiguous_relations=ambiguity_view,
        derived_context=dict(contexts),
        source_bboxes=all_bboxes,
        digest=_graph_digest(ownership_edges, ambiguity_view, header_nodes),
    )
    return V3Build(page=_v3_page(page, ownership_edges, parser_name, parser_version), graph=graph)


def _build_column_axis(geometry: TableGeometry, region, header_zone_ids: set[str]) -> ColumnAxisV3:
    """Leaf columns, spanning groups and group-level ambiguity.

    Leaf columns are the column chains that contain at least one
    data-row cell; chains holding only header-zone cells (span keys)
    are group-level, never leaves. A header cell forms a group over a
    leaf when it positively overlaps the leaf (edge contact excluded),
    is wider than the leaf by the span ratio, and uniquely contains the
    leaf centre. A leaf claimed by two or more groups stays unassigned
    and is reported as ambiguous instead of being guessed.
    """
    leaf_ids: list[str] = []
    leaf_spans: dict[str, tuple[float, float]] = {}
    for column_id in sorted(region.column_ids):
        cells = _column_cells(geometry, column_id)
        if not any(cell.cell_id not in header_zone_ids for cell in cells):
            continue
        leaf_ids.append(column_id)
        leaf_spans[column_id] = (
            min(cell.bbox.x0 for cell in cells),
            max(cell.bbox.x1 for cell in cells),
        )
    groups: list[ColumnGroupV3] = []
    claimants: dict[str, list[str]] = {}
    for cell_id in sorted(header_zone_ids):
        cell = geometry.cells[cell_id]
        spanned: list[str] = []
        for column_id in leaf_ids:
            x0, x1 = leaf_spans[column_id]
            overlap = min(cell.bbox.x1, x1) - max(cell.bbox.x0, x0)
            width = x1 - x0
            center = (x0 + x1) / 2
            if (
                overlap > _VERTICAL_TOLERANCE
                and width > 0
                and cell.bbox.width > width * _SPAN_RATIO
                and cell.bbox.x0 <= center <= cell.bbox.x1
            ):
                spanned.append(column_id)
        for column_id in spanned:
            claimants.setdefault(column_id, []).append(cell.cell_id)
        if len(spanned) >= 2:
            groups.append(ColumnGroupV3(
                group_id=stable_id("v3colgroup", region.table_region_id, cell.cell_id),
                header_id=cell.cell_id,
                covered_leaf_column_ids=tuple(sorted(spanned)),
            ))
    ambiguous_leaves = tuple(sorted(
        column_id for column_id, headers in claimants.items() if len(headers) > 1
    ))
    return ColumnAxisV3(
        table_region_id=region.table_region_id,
        leaf_column_ids=tuple(sorted(leaf_ids)),
        column_groups=tuple(sorted(groups, key=lambda item: item.group_id)),
        ambiguous_leaf_column_ids=ambiguous_leaves,
    )


def _column_cells(geometry: TableGeometry, column_id: str):
    return geometry.chains.get(column_id).cells if column_id in geometry.chains else []


def _section_edge(page: PageLayout, region) -> str | None:
    """Heading block governing the region, using the canonical selection rule."""
    blocks_by_id = {block.block_id: block for block in page.blocks}
    min_order = min(
        (blocks_by_id[c].reading_order for c in region.cell_ids if c in blocks_by_id),
        default=10**9,
    )
    section_block_id = ""
    for block in page.blocks:
        if block.block_type == "HEADING" and block.reading_order < min_order:
            section_block_id = block.block_id
    return section_block_id or None


def _caption_edges(page: PageLayout, ambiguous: list[AmbiguousRelationV3]) -> list[TypedDirectEdgeV3]:
    """CAPTION_DESCRIBES edges: unique nearest region only, never cell values."""
    edges: list[TypedDirectEdgeV3] = []
    regions = page.table_regions
    for block in page.blocks:
        if block.block_type != "CAPTION":
            continue
        candidates = []
        for region in regions:
            gap = max(0.0, block.bbox.y0 - region.bbox.y1, region.bbox.y0 - block.bbox.y1)
            horizontal = (min(block.bbox.x1, region.bbox.x1) - max(block.bbox.x0, region.bbox.x0)) > 0
            if horizontal and gap <= _CAPTION_MAX_GAP:
                candidates.append((gap, region.table_region_id))
        if not candidates:
            continue
        candidates.sort()
        best_gap = candidates[0][0]
        nearest = [region_id for gap, region_id in candidates if gap <= best_gap + 1.0]
        if len(nearest) != 1:
            ambiguous.append(AmbiguousRelationV3(
                value_id=block.block_id,
                axis="CAPTION",
                candidate_ids=tuple(sorted(nearest)),
                reason="GEOMETRY_OVERLAP",
            ))
            continue
        region_id = nearest[0]
        edges.append(TypedDirectEdgeV3(
            edge_id=stable_id("v3edge", region_id, block.block_id, region_id, "CAPTION_DESCRIBES"),
            source_id=block.block_id,
            target_id=region_id,
            relation="CAPTION_DESCRIBES",
            evidence="GEOMETRY",
            table_region_id=region_id,
        ))
    return edges


def _graph_digest(edges, ambiguous, header_nodes) -> str:
    payload = json.dumps(
        {
            "edges": [[item.edge_id, item.source_id, item.target_id, item.relation] for item in edges],
            "ambiguous": [[item.value_id, item.axis, list(item.candidate_ids)] for item in ambiguous],
            "roles": sorted([item.header_id, item.role] for item in header_nodes),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _v3_page(page: PageLayout, edges, parser_name: str, parser_version: str) -> PageLayout:
    """Canonical page view carrying the typed DIRECT graph as relationships."""
    typed = tuple(
        HeaderRelationship(
            relationship_id=edge.edge_id,
            header_block_id=edge.source_id,
            value_block_id=edge.target_id,
            relationship_type=edge.relation,
            page_number=page.page_number,
            table_region_id=edge.table_region_id,
            confidence=1.0,
        )
        for edge in edges
    )
    blocks = tuple(
        replace(block, parser_name=parser_name, parser_version=parser_version)
        for block in page.blocks
    )
    return replace(
        page,
        blocks=blocks,
        relationships=typed,
        parser_name=parser_name,
        parser_version=parser_version,
    )


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def structured_chunk_candidates_v3(graph: StructuredGraphV3) -> tuple[StructuredChunkCandidateV3, ...]:
    """Derive experimental chunk candidates for every pure value cell.

    Purely deterministic and in-memory: nothing is written to Chroma or
    any production index. Identity of a pure value: target of a leaf
    ownership relation that never acts as a source itself.
    """
    sources = {edge.source_id for edge in graph.direct_edges if edge.relation in _EVAL_RELATIONS}
    value_ids = sorted(
        {
            edge.target_id
            for edge in graph.direct_edges
            if edge.relation in _EVAL_RELATIONS and edge.target_id not in sources
        }
    )
    cells = {cell.cell_id: cell for cell in graph.cells}
    ambiguous_by_value: dict[str, tuple[str, ...]] = {}
    for record in graph.ambiguous_relations:
        if record.value_id in cells:
            ambiguous_by_value.setdefault(record.value_id, tuple(sorted(record.candidate_ids)))
    candidates: list[StructuredChunkCandidateV3] = []
    for value_id in value_ids:
        cell = cells.get(value_id)
        if cell is None:
            continue
        touching = tuple(sorted(
            edge.edge_id
            for edge in graph.direct_edges
            if edge.source_id == value_id or edge.target_id == value_id
        ))
        table_id = next(
            (
                edge.table_region_id
                for edge in graph.direct_edges
                if edge.target_id == value_id and edge.table_region_id
            ),
            "",
        )
        candidates.append(
            StructuredChunkCandidateV3(
                chunk_id=stable_id("v3chunk", graph.document_id, graph.page_number, value_id),
                text=cell.text,
                document_id=graph.document_id,
                page_number=graph.page_number,
                table_id=table_id,
                row_id=cell.row_id,
                column_id=cell.column_id,
                value_cell_id=value_id,
                direct_edge_ids=touching,
                derived_context=_context_view(graph.derived_context.get(value_id)),
                ambiguous_context=ambiguous_by_value.get(value_id, ()),
                source_bboxes=(cell.bbox,),
            )
        )
    return tuple(candidates)


def _context_view(context) -> dict:
    """Serializable view of a canonical DerivedContext (or empty view)."""
    if context is None:
        return {}
    return {
        "MODEL": list(context.model),
        "UNIT": list(context.unit),
        "ROW": list(context.row_header),
        "COLUMN": list(context.column_header),
        "MULTI_LEVEL": list(context.multi_level_parents),
        "SECTION": context.section,
        "AMBIGUOUS_AXES": list(context.ambiguous_axes),
    }


class PdfPlumberStructuredGraphV3Parser:
    """Experimental structured-graph parser.

    Table detection is reused unchanged from ``PdfPlumberTableParser``
    (frozen path); this class only replaces the ownership layer with the
    typed DIRECT graph. Existing parsers keep running in parallel.
    """

    name = PARSER_NAME

    def __init__(self) -> None:
        self.pdfplumber_version = importlib.metadata.version("pdfplumber")
        self.version = f"{self.pdfplumber_version}+structured-graph-v3"

    def parse(self, path: str | Path, document_id: str) -> DocumentLayout:
        baseline = PdfPlumberTableParser().parse(path, document_id)
        builds = tuple(
            build_structured_graph_v3(page, parser_name=self.name, parser_version=self.version)
            for page in baseline.pages
        )
        return DocumentLayout(
            document_id,
            tuple(item.page for item in builds),
            self.name,
            self.version,
            _config_digest(self.name, self.version),
        )
