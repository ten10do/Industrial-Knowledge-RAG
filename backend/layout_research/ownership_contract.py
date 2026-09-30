"""Canonical layout ownership contract (Phase LA-1.1).

Research-only module. It defines the shared ownership ontology that
annotator, parser and evaluator must all use, and provides:

1. A relation ontology with explicit DIRECT vs DERIVED semantics.
2. A per-relation contract registry (source/target types, evidence,
   cardinality, ambiguity behaviour, transitivity, storage class,
   evaluation rule).
3. A deterministic geometric classifier for legacy flat
   HEADER_APPLIES_TO edges (11-class taxonomy).
4. A contract mapping that partitions legacy edges into DIRECT,
   DERIVED and AMBIGUOUS.
5. A cycle-safe, deterministic context derivation function
   ``derive_context`` with explicit AMBIGUOUS semantics and no
   nearest-text fallback.

This module never mutates historical gold, the frozen evaluator or
production code paths.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable

from .models import PageLayout, TableCell

# ---------------------------------------------------------------------------
# Relation ontology
# ---------------------------------------------------------------------------

# Direct structural relations: stored as parsed, backed by local geometric
# or structural evidence. They are the ground truth of the layout graph.
DIRECT_RELATIONS = (
    "HEADER_PARENT_OF_HEADER",
    "COLUMN_HEADER_OF",
    "ROW_HEADER_OF",
    "UNIT_OF",
    "MODEL_OF",
    "VALUE_IN_COLUMN",
    "VALUE_IN_ROW",
    "TABLE_CONTAINS",
    "SECTION_CONTAINS",
    "TABLE_CONTINUES_ON",
    "CAPTION_DESCRIBES",
)

# Derived relations: never stored by the parser; computed deterministically
# from the direct graph on demand.
DERIVED_RELATIONS = (
    "HEADER_APPLIES_TO",
)

RELATION_TYPES = DIRECT_RELATIONS + DERIVED_RELATIONS

# The legacy flat relation. Retained ONLY as a derived context edge so that
# historical artifacts remain interpretable. It must never again be the
# primary stored output of a parser.
LEGACY_HEADER_APPLIES_TO = "HEADER_APPLIES_TO"


@dataclass(frozen=True)
class RelationContract:
    """Canonical contract for one relation type."""

    relation: str
    storage: str  # DIRECT | DERIVED
    source_type: str
    target_type: str
    geometric_evidence: str
    structural_evidence: str
    cardinality: str
    ambiguity_behavior: str
    transitive: bool
    stored_directly: bool
    evaluation_rule: str


CONTRACTS: dict[str, RelationContract] = {
    "HEADER_PARENT_OF_HEADER": RelationContract(
        relation="HEADER_PARENT_OF_HEADER",
        storage="DIRECT",
        source_type="header_cell",
        target_type="header_cell",
        geometric_evidence="same column axis; source bottom adjacent to target top with no intermediate header cell, or source bbox horizontally spans target's column",
        structural_evidence="both cells belong to header rows of the same table region",
        cardinality="target has at most one parent per axis; source may have many children",
        ambiguity_behavior="two competing parents at the same level => AMBIGUOUS, no nearest-text fallback",
        transitive=False,
        stored_directly=True,
        evaluation_rule="Direct Edge Precision/Recall on (source, target) pairs",
    ),
    "COLUMN_HEADER_OF": RelationContract(
        relation="COLUMN_HEADER_OF",
        storage="DIRECT",
        source_type="header_cell",
        target_type="data_cell",
        geometric_evidence="source is the adjacent cell above target in the same column axis with no intermediate header cell",
        structural_evidence="source in a header row; target in a data row of the same table region",
        cardinality="one direct column parent per data cell per table",
        ambiguity_behavior="competing adjacent parents => AMBIGUOUS",
        transitive=False,
        stored_directly=True,
        evaluation_rule="Direct Edge Precision/Recall",
    ),
    "ROW_HEADER_OF": RelationContract(
        relation="ROW_HEADER_OF",
        storage="DIRECT",
        source_type="header_cell",
        target_type="data_cell",
        geometric_evidence="same row axis; source right edge at or left of target left edge",
        structural_evidence="source in the row-header column of the same table region",
        cardinality="normally one; multiple non-nested row headers => AMBIGUOUS",
        ambiguity_behavior="unresolved multiple row headers => AMBIGUOUS",
        transitive=False,
        stored_directly=True,
        evaluation_rule="Direct Edge Precision/Recall",
    ),
    "UNIT_OF": RelationContract(
        relation="UNIT_OF",
        storage="DIRECT",
        source_type="unit_cell",
        target_type="data_cell",
        geometric_evidence="adjacent on the column axis (unit row below header / unit column above value)",
        structural_evidence="source text matches the unit vocabulary contract",
        cardinality="at most one unit per data cell",
        ambiguity_behavior="two unit candidates => AMBIGUOUS",
        transitive=False,
        stored_directly=True,
        evaluation_rule="Unit Context Precision/Recall (derived view)",
    ),
    "MODEL_OF": RelationContract(
        relation="MODEL_OF",
        storage="DIRECT",
        source_type="model_header_cell",
        target_type="data_cell",
        geometric_evidence="source governs the column (or row) that contains the target; source is the adjacent model header on that axis",
        structural_evidence="source text matches the model vocabulary contract and is in a header position",
        cardinality="at most one model per data cell unless a comparison table declares a model axis",
        ambiguity_behavior="competing model headers => AMBIGUOUS",
        transitive=False,
        stored_directly=True,
        evaluation_rule="Model Context Precision/Recall (derived view)",
    ),
    "VALUE_IN_COLUMN": RelationContract(
        relation="VALUE_IN_COLUMN",
        storage="DIRECT",
        source_type="data_cell",
        target_type="header_cell",
        geometric_evidence="inverse direction of COLUMN_HEADER_OF; stored in header->value orientation",
        structural_evidence="same table region",
        cardinality="see COLUMN_HEADER_OF",
        ambiguity_behavior="see COLUMN_HEADER_OF",
        transitive=False,
        stored_directly=True,
        evaluation_rule="counted once with COLUMN_HEADER_OF to avoid double counting",
    ),
    "VALUE_IN_ROW": RelationContract(
        relation="VALUE_IN_ROW",
        storage="DIRECT",
        source_type="data_cell",
        target_type="header_cell",
        geometric_evidence="inverse direction of ROW_HEADER_OF; stored in header->value orientation",
        structural_evidence="same table region",
        cardinality="see ROW_HEADER_OF",
        ambiguity_behavior="see ROW_HEADER_OF",
        transitive=False,
        stored_directly=True,
        evaluation_rule="counted once with ROW_HEADER_OF",
    ),
    "TABLE_CONTAINS": RelationContract(
        relation="TABLE_CONTAINS",
        storage="DIRECT",
        source_type="table_region",
        target_type="cell",
        geometric_evidence="cell bbox inside table region bbox",
        structural_evidence="table region membership list",
        cardinality="one per cell",
        ambiguity_behavior="never ambiguous (region membership is definitional)",
        transitive=False,
        stored_directly=True,
        evaluation_rule="structural accuracy, not ownership",
    ),
    "SECTION_CONTAINS": RelationContract(
        relation="SECTION_CONTAINS",
        storage="DIRECT",
        source_type="section_heading",
        target_type="block_or_table",
        geometric_evidence="reading order: target after nearest preceding heading",
        structural_evidence="document heading hierarchy",
        cardinality="one section per target",
        ambiguity_behavior="no heading found => empty section, never guessed",
        transitive=False,
        stored_directly=True,
        evaluation_rule="section attribution accuracy",
    ),
    "TABLE_CONTINUES_ON": RelationContract(
        relation="TABLE_CONTINUES_ON",
        storage="DIRECT",
        source_type="table_region",
        target_type="table_region",
        geometric_evidence="cross-page continuation with explicit continuation evidence (caption marker or annotation)",
        structural_evidence="annotated continuation record",
        cardinality="many-to-many allowed",
        ambiguity_behavior="no explicit evidence => no continuation edge (never inferred from visual similarity alone)",
        transitive=False,
        stored_directly=True,
        evaluation_rule="cross-page continuation recall; context must NOT leak across pages without this edge",
    ),
    "CAPTION_DESCRIBES": RelationContract(
        relation="CAPTION_DESCRIBES",
        storage="DIRECT",
        source_type="caption_block",
        target_type="table_region",
        geometric_evidence="caption adjacent (above/below) to exactly one table region",
        structural_evidence="caption text pattern",
        cardinality="one caption per table region (single direction)",
        ambiguity_behavior="caption equidistant to two tables => AMBIGUOUS",
        transitive=False,
        stored_directly=True,
        evaluation_rule="Direct Edge Precision/Recall",
    ),
    "HEADER_APPLIES_TO": RelationContract(
        relation="HEADER_APPLIES_TO",
        storage="DERIVED",
        source_type="any_header_cell",
        target_type="data_cell",
        geometric_evidence="none of its own; inherited from the direct graph path",
        structural_evidence="derived from ancestor closure of the direct header graph",
        cardinality="a value may have many derived owners (full ancestor closure)",
        ambiguity_behavior="derived context is only emitted when the underlying direct graph is unambiguous",
        transitive=True,
        stored_directly=False,
        evaluation_rule="Context Precision/Recall per axis; NEVER a single flat F1",
    ),
}


# ---------------------------------------------------------------------------
# Legacy edge taxonomy (11 classes)
# ---------------------------------------------------------------------------

TAXONOMY_CLASSES = (
    "DIRECT_LEAF_EDGE",
    "ROW_CONTEXT_EDGE",
    "COLUMN_CONTEXT_EDGE",
    "MODEL_CONTEXT_EDGE",
    "UNIT_CONTEXT_EDGE",
    "MULTI_LEVEL_ANCESTOR_EDGE",
    "MERGED_HEADER_EDGE",
    "SPANNING_CONTEXT_EDGE",
    "SECTION_CONTEXT_EDGE",
    "DERIVED_TRANSITIVE_EDGE",
    "OTHER",
)

# DERIVED_TRANSITIVE_EDGE is reported as a semantic flag (edge has an
# intermediate header between source and target) that co-occurs with the
# structural classes MULTI_LEVEL_ANCESTOR_EDGE / SPANNING_CONTEXT_EDGE /
# non-adjacent UNIT/MODEL edges. It is not a mutually exclusive bucket.

UNIT_TEXT_PATTERN = re.compile(r"^(?:%|a|bar|degc|hz|in|kg|kw|lb|m|ma|mm|ms|nm|rpm|s|v|vac|vdc|w)$", re.I)
MODEL_TEXT_PATTERN = re.compile(r"^(?=.*[a-z])(?=.*\d)[a-z0-9][a-z0-9+./_-]{1,31}$", re.I)
PARAMETER_CODE_PATTERN = re.compile(r"^[a-z]\d{2,4}$", re.I)

_VERTICAL_TOLERANCE = 2.0
_SPAN_RATIO = 1.5


def normalized_text(text: str) -> str:
    return re.sub(r"[°\s()]", "", text).casefold()


def is_unit_text(text: str) -> bool:
    return bool(UNIT_TEXT_PATTERN.fullmatch(normalized_text(text)))


def is_model_text(text: str) -> bool:
    token = normalized_text(text)
    return bool(MODEL_TEXT_PATTERN.fullmatch(token)) and not bool(PARAMETER_CODE_PATTERN.fullmatch(token))


@dataclass(frozen=True)
class EdgeClassification:
    """Result of classifying one legacy ownership edge."""

    header_id: str
    value_id: str
    taxonomy: str
    derived_transitive: bool
    same_page: bool
    in_same_table: bool
    direct_candidate: bool
    ambiguous: bool


@dataclass
class _ColumnChain:
    """Vertical chain of a table column, bottom = closest to data rows."""

    column_id: str
    cells: list[TableCell] = field(default_factory=list)  # sorted by y0 ascending


VALUE_TARGET_RELATIONS = (
    "HEADER_APPLIES_TO",
    "COLUMN_HEADER_OF",
    "ROW_HEADER_OF",
    "UNIT_OF",
    "MODEL_OF",
)


class TableGeometry:
    """Deterministic geometric view of one table region.

    The header zone is delimited deterministically: the topmost cell that
    appears as a value target of an ownership relation marks the boundary;
    every cell strictly above the boundary is a header cell. This keeps
    multi-row data tables from leaking data cells into header chains.
    """

    def __init__(self, page: PageLayout, table_region_id: str):
        self.table_region_id = table_region_id
        region = next((r for r in page.table_regions if r.table_region_id == table_region_id), None)
        member_ids = set(region.cell_ids) if region else set()
        self.cells: dict[str, TableCell] = {
            cell.cell_id: cell for cell in page.cells if cell.cell_id in member_ids
        }
        self.header_boundary = self._compute_header_boundary(page)
        self.chains: dict[str, _ColumnChain] = {}
        for cell in self.cells.values():
            self.chains.setdefault(cell.column_id, _ColumnChain(cell.column_id)).cells.append(cell)
        for chain in self.chains.values():
            chain.cells.sort(key=lambda item: (item.bbox.y0, item.bbox.x0))
        self.rows_by_id: dict[str, list[TableCell]] = {}
        for cell in self.cells.values():
            self.rows_by_id.setdefault(cell.row_id, []).append(cell)
        for row_cells in self.rows_by_id.values():
            row_cells.sort(key=lambda item: item.bbox.x0)

    def _compute_header_boundary(self, page: PageLayout) -> float:
        boundary = float("inf")
        sources = {
            edge.header_block_id
            for edge in page.relationships
            if edge.relationship_type in VALUE_TARGET_RELATIONS and edge.header_block_id
        }
        for edge in page.relationships:
            if edge.relationship_type not in VALUE_TARGET_RELATIONS or not edge.value_block_id:
                continue
            value = self.cells.get(edge.value_block_id)
            # Pure values only: cells that themselves emit ownership edges
            # (row headers) do not delimit the header zone.
            if value is not None and value.cell_id not in sources:
                boundary = min(boundary, value.bbox.y0)
        return boundary

    def chain_above(self, value: TableCell) -> list[TableCell]:
        """Header cells of the value's column strictly above it, closest first."""
        chain = self.chains.get(value.column_id)
        if not chain:
            return []
        limit = min(value.bbox.y0, self.header_boundary)
        above = [
            cell
            for cell in chain.cells
            if cell.cell_id != value.cell_id and cell.bbox.y1 <= limit + _VERTICAL_TOLERANCE
        ]
        above.sort(key=lambda item: item.bbox.y0, reverse=True)
        return above

    def has_intermediate(self, header: TableCell, value: TableCell) -> bool:
        """True when another header cell sits between header and value on the column axis."""
        limit = min(value.bbox.y0, self.header_boundary)
        for cell in self.chains.get(value.column_id, _ColumnChain("")).cells:
            if cell.cell_id in (header.cell_id, value.cell_id):
                continue
            if cell.bbox.y0 >= header.bbox.y1 - _VERTICAL_TOLERANCE and cell.bbox.y1 <= limit + _VERTICAL_TOLERANCE:
                return True
        return False

    def overlaps_value_column(self, cell: TableCell, value: TableCell) -> bool:
        """Positive horizontal overlap with the value's column (edge contact excluded)."""
        return (min(cell.bbox.x1, value.bbox.x1) - max(cell.bbox.x0, value.bbox.x0)) > _VERTICAL_TOLERANCE

    def row_header_candidates(self, value: TableCell) -> list[TableCell]:
        row = self.rows_by_id.get(value.row_id, [])
        return [cell for cell in row if cell.cell_id != value.cell_id and cell.bbox.x1 <= value.bbox.x0 + 1.0]

    def parent_candidates(self, value: TableCell) -> list[TableCell]:
        """Header-zone cells that occupy the value's parent slot.

        A candidate overlaps the value's column, sits above it in the
        header zone, and has no other header cell between it and the
        value. Sorted closest-first; competing candidates at this slot
        are the AMBIGUITY source.
        """
        candidates: list[TableCell] = []
        seen: set[str] = set()
        for chain in self.chains.values():
            for cell in chain.cells:
                if cell.cell_id in seen or cell.cell_id == value.cell_id:
                    continue
                if cell.bbox.y1 > min(value.bbox.y0, self.header_boundary) + _VERTICAL_TOLERANCE:
                    continue
                if not self.overlaps_value_column(cell, value):
                    continue
                if self.has_intermediate(cell, value):
                    continue
                seen.add(cell.cell_id)
                candidates.append(cell)
        candidates.sort(key=lambda item: (item.bbox.y0, item.bbox.x0, item.cell_id), reverse=True)
        return candidates

    def resolve_parent(self, value: TableCell) -> tuple[TableCell | None, list[TableCell], bool]:
        """Resolve the parent slot: (resolved, all_candidates, ambiguous)."""
        candidates = self.parent_candidates(value)
        if not candidates:
            return None, [], False
        if len(candidates) == 1:
            return candidates[0], candidates, False
        center = (value.bbox.x0 + value.bbox.x1) / 2
        containing = [
            cell
            for cell in candidates
            if cell.bbox.x0 - 1 <= center <= cell.bbox.x1 + 1
        ]
        if len(containing) == 1:
            return containing[0], candidates, False
        return None, candidates, True


def _page_tables(page: PageLayout) -> dict[str, TableGeometry]:
    return {region.table_region_id: TableGeometry(page, region.table_region_id) for region in page.table_regions}


def classify_legacy_edge(
    page: PageLayout,
    header: TableCell,
    value: TableCell,
    *,
    geometry: TableGeometry | None = None,
) -> EdgeClassification:
    """Classify one flat header->value edge under the canonical taxonomy.

    Priority order (most specific first): section, row, unit, model,
    direct leaf, merged header, multi-level ancestor, spanning context,
    other. ``derived_transitive`` marks edges whose source has another
    header cell between it and the value (semantic transitivity).
    """
    same_table = geometry is not None and header.cell_id in geometry.cells
    same_page = header.page_number == value.page_number == page.page_number
    if not same_table:
        header_block = next((b for b in page.blocks if b.block_id == header.cell_id), None)
        if header_block is not None and header_block.block_type == "HEADING":
            return EdgeClassification(header.cell_id, value.cell_id, "SECTION_CONTEXT_EDGE", False, same_page, False, False, False)
        return EdgeClassification(header.cell_id, value.cell_id, "OTHER", False, same_page, False, False, False)

    assert geometry is not None
    # Semantic role override (unit/model text) only applies to pure
    # values: cells that themselves emit ownership edges (row headers,
    # nested headers) keep their structural classification.
    source_ids = {
        edge.header_block_id
        for edge in page.relationships
        if edge.relationship_type in VALUE_TARGET_RELATIONS and edge.header_block_id
    }
    pure_value = value.cell_id not in source_ids

    # Row axis (same row, left of value). A unit-text cell on the row
    # axis is a unit column member, never a row header.
    if header.row_id == value.row_id and header.bbox.x1 <= value.bbox.x0 + 1.0:
        if pure_value and is_unit_text(header.text):
            return EdgeClassification(header.cell_id, value.cell_id, "UNIT_CONTEXT_EDGE", False, same_page, True, True, False)
        return EdgeClassification(header.cell_id, value.cell_id, "ROW_CONTEXT_EDGE", False, same_page, True, True, False)

    if header.bbox.y1 <= value.bbox.y0 + _VERTICAL_TOLERANCE and geometry.overlaps_value_column(header, value):
        resolved, candidates, ambiguous_slot = geometry.resolve_parent(value)
        intermediate = geometry.has_intermediate(header, value)
        ambiguous = ambiguous_slot or (resolved is not None and header.cell_id != resolved.cell_id and not intermediate)
        derived = intermediate or (resolved is not None and header.cell_id != resolved.cell_id)
        if not candidates and not intermediate:
            derived = False
        if ambiguous:
            direct = False
        else:
            direct = header.cell_id == resolved.cell_id if resolved is not None else not intermediate
        spanned = header.bbox.width > value.bbox.width * _SPAN_RATIO and header.column_id != value.column_id
        if pure_value and is_unit_text(header.text):
            taxonomy = "UNIT_CONTEXT_EDGE"
        elif pure_value and is_model_text(header.text):
            taxonomy = "MODEL_CONTEXT_EDGE"
        elif spanned:
            taxonomy = "MERGED_HEADER_EDGE" if not derived else "SPANNING_CONTEXT_EDGE"
        elif not derived:
            taxonomy = "DIRECT_LEAF_EDGE"
        else:
            taxonomy = "MULTI_LEVEL_ANCESTOR_EDGE"
        direct = direct and not derived
        return EdgeClassification(header.cell_id, value.cell_id, taxonomy, derived, same_page, True, direct, ambiguous)

    return EdgeClassification(header.cell_id, value.cell_id, "OTHER", False, same_page, True, False, False)


def iter_legacy_edges(page: PageLayout) -> Iterable[tuple[TableCell, TableCell]]:
    cells = {cell.cell_id: cell for cell in page.cells}
    for edge in page.relationships:
        if edge.relationship_type != LEGACY_HEADER_APPLIES_TO:
            continue
        header = cells.get(edge.header_block_id)
        value = cells.get(edge.value_block_id)
        if header is None or value is None:
            continue
        yield header, value


def classify_legacy_page(page: PageLayout) -> list[EdgeClassification]:
    """Classify all flat ownership edges of one page."""
    geometries = _page_tables(page)
    results = []
    for header, value in iter_legacy_edges(page):
        region_id = next(
            (r.table_region_id for r in page.table_regions if header.cell_id in r.cell_ids and value.cell_id in r.cell_ids),
            None,
        )
        geometry = geometries.get(region_id) if region_id else None
        results.append(classify_legacy_edge(page, header, value, geometry=geometry))
    return results


# ---------------------------------------------------------------------------
# Legacy edge -> DIRECT / DERIVED / AMBIGUOUS contract mapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LegacyEdgeMapping:
    header_id: str
    value_id: str
    mapping: str  # DIRECT | DERIVED | AMBIGUOUS | OTHER
    direct_relation: str  # canonical relation when DIRECT, else ""
    taxonomy: str


def map_legacy_page(page: PageLayout) -> list[LegacyEdgeMapping]:
    """Partition a page's flat HEADER_APPLIES_TO edges under the contract.

    DIRECT edges are those backed by immediate structural evidence
    (adjacent column parent, row header, adjacent unit/model header,
    adjacent merged header). DERIVED edges are ancestor-closure edges.
    AMBIGUOUS edges compete for the same structural slot. The mapping is
    a CONTRACT DIAGNOSTIC for consumed corpora; it is never a new
    evaluation.
    """
    mappings: list[LegacyEdgeMapping] = []
    for classification in classify_legacy_page(page):
        taxonomy = classification.taxonomy
        if not classification.in_same_table:
            mapping = "OTHER"
            relation = ""
        elif classification.ambiguous:
            mapping = "AMBIGUOUS"
            relation = ""
        elif classification.derived_transitive:
            mapping = "DERIVED"
            relation = LEGACY_HEADER_APPLIES_TO
        elif classification.direct_candidate and taxonomy in {
            "ROW_CONTEXT_EDGE",
            "DIRECT_LEAF_EDGE",
            "MERGED_HEADER_EDGE",
            "UNIT_CONTEXT_EDGE",
            "MODEL_CONTEXT_EDGE",
        }:
            mapping = "DIRECT"
            relation = {
                "ROW_CONTEXT_EDGE": "ROW_HEADER_OF",
                "DIRECT_LEAF_EDGE": "COLUMN_HEADER_OF",
                "MERGED_HEADER_EDGE": "COLUMN_HEADER_OF",
                "UNIT_CONTEXT_EDGE": "UNIT_OF",
                "MODEL_CONTEXT_EDGE": "MODEL_OF",
            }[taxonomy]
        elif taxonomy == "SECTION_CONTEXT_EDGE":
            mapping = "DIRECT"
            relation = "SECTION_CONTAINS"
        else:
            mapping = "OTHER"
            relation = ""
        mappings.append(
            LegacyEdgeMapping(classification.header_id, classification.value_id, mapping, relation, taxonomy)
        )
    return mappings


# ---------------------------------------------------------------------------
# Context derivation (deterministic, cycle-safe, AMBIGUOUS-explicit)
# ---------------------------------------------------------------------------

CONTEXT_AXES = ("MODEL", "UNIT", "ROW", "COLUMN", "MULTI_LEVEL", "SECTION")

AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class DerivedContext:
    """Derived context closure for one data cell."""

    value_id: str
    row_header: tuple[str, ...] = ()
    column_header: tuple[str, ...] = ()
    model: tuple[str, ...] = ()
    unit: tuple[str, ...] = ()
    multi_level_parents: tuple[str, ...] = ()
    section: str = ""
    ambiguous_axes: tuple[str, ...] = ()

    def axis_members(self, axis: str) -> tuple[str, ...]:
        return {
            "MODEL": self.model,
            "UNIT": self.unit,
            "ROW": self.row_header,
            "COLUMN": self.column_header,
            "MULTI_LEVEL": self.multi_level_parents,
            "SECTION": (self.section,) if self.section else (),
        }.get(axis, ())


class ContextGraph:
    """Direct structural graph of one page.

    Built deterministically from cell geometry: adjacent column parents,
    row headers, header-tree chains and merged-header containment. It is
    the single source from which context is derived. Cycles are
    structurally impossible in the geometric build but guarded anyway.
    Semantic role axes (MODEL/UNIT) apply only to pure values; cells that
    themselves emit ownership edges (row headers, nested headers) get
    structural context only.
    """

    def __init__(self, page: PageLayout):
        self.page = page
        self.tables = _page_tables(page)
        self.source_ids = {
            edge.header_block_id
            for edge in page.relationships
            if edge.relationship_type in VALUE_TARGET_RELATIONS and edge.header_block_id
        }
        blocks_by_id = {block.block_id: block for block in page.blocks}
        self.section_titles: dict[str, str] = {}
        for region in page.table_regions:
            section = ""
            for block in page.blocks:
                if block.block_type == "HEADING" and block.reading_order < min(
                    (blocks_by_id[c].reading_order for c in region.cell_ids if c in blocks_by_id),
                    default=10**9,
                ):
                    section = block.section_title or block.text
            self.section_titles[region.table_region_id] = section

    def derive_all(self, page: PageLayout | None = None) -> dict[str, DerivedContext]:
        """Derive context for every data cell (deterministic cell order)."""
        target = page if page is not None else self.page
        return {
            cell.cell_id: self.derive_context(cell)
            for cell in sorted(target.cells, key=lambda item: item.cell_id)
        }

    def derive_context(self, value: TableCell) -> DerivedContext:
        geometry = self.tables.get(self._table_of(value))
        if geometry is None:
            return DerivedContext(value_id=value.cell_id, ambiguous_axes=("COLUMN", "ROW"))
        ambiguous: list[str] = []

        resolved, candidates, ambiguous_slot = geometry.resolve_parent(value)
        column_header: tuple[str, ...] = ()
        if ambiguous_slot:
            ambiguous.append("COLUMN")
            column_header = tuple(sorted(cell.cell_id for cell in candidates))
        elif resolved is not None:
            column_header = (resolved.cell_id,)

        # Row axis: same-row left cells; unit-text cells belong to the
        # unit axis, the rest compete for the row-header slot.
        row_candidates = geometry.row_header_candidates(value)
        unit_cells = [cell for cell in row_candidates if is_unit_text(cell.text)]
        row_only = [cell for cell in row_candidates if not is_unit_text(cell.text)]
        row_header: tuple[str, ...] = ()
        if len(row_only) > 1:
            row_header = tuple(sorted(cell.cell_id for cell in row_only))
            ambiguous.append("ROW")
        elif row_only:
            row_header = (row_only[0].cell_id,)

        # Multi-level ancestors: every header-zone cell overlapping the
        # value's column that is NOT in the parent slot (it sits behind
        # an intermediate header). Cycle-safe: finite geometric set plus
        # a visited guard.
        visited: set[str] = set()
        multi: list[str] = []
        candidate_ids = {cell.cell_id for cell in candidates}
        for chain in geometry.chains.values():
            for cell in chain.cells:
                if cell.cell_id in visited or cell.cell_id == value.cell_id:
                    continue
                if cell.bbox.y1 > min(value.bbox.y0, geometry.header_boundary) + _VERTICAL_TOLERANCE:
                    continue
                if not geometry.overlaps_value_column(cell, value):
                    continue
                visited.add(cell.cell_id)
                if cell.cell_id not in candidate_ids:
                    multi.append(cell.cell_id)
        multi = sorted(set(multi))

        pure_value = value.cell_id not in self.source_ids
        model_candidates = [cell for cell in candidates if is_model_text(cell.text)] if pure_value else []
        model = tuple(sorted(cell.cell_id for cell in model_candidates))
        if len(model) > 1:
            ambiguous.append("MODEL")
        units = list(unit_cells) if pure_value else []
        if resolved is not None and pure_value and is_unit_text(resolved.text):
            units.append(resolved)
        unit = tuple(sorted(cell.cell_id for cell in units))
        if len(units) > 1:
            ambiguous.append("UNIT")
        section = self.section_titles.get(self._table_of(value), "")
        return DerivedContext(
            value_id=value.cell_id,
            row_header=row_header,
            column_header=column_header,
            model=model,
            unit=unit,
            multi_level_parents=tuple(multi),
            section=section,
            ambiguous_axes=tuple(sorted(set(ambiguous))),
        )

    def _table_of(self, value: TableCell) -> str:
        return next(
            (region.table_region_id for region in self.page.table_regions if value.cell_id in region.cell_ids),
            "",
        )


def derive_context_from_direct_edges(
    edges: Iterable[tuple[str, str, str]],
    value_id: str,
    *,
    section: str = "",
) -> DerivedContext:
    """Derive context for one value from an explicit DIRECT edge set.

    Orientation: (header, target, relation) with target being the value
    for COLUMN_HEADER_OF / ROW_HEADER_OF / UNIT_OF / MODEL_OF and the
    child header for HEADER_PARENT_OF_HEADER. Cycle-safe via a visited
    set, deterministic via total ordering, and explicitly AMBIGUOUS when
    a slot has competing candidates. No nearest-text fallback.
    """
    edge_list = [(h, t, r) for h, t, r in edges]
    by_value: dict[str, list[tuple[str, str]]] = {}
    parent_of: dict[str, list[str]] = {}
    for header, target, relation in edge_list:
        if relation in ("COLUMN_HEADER_OF", "ROW_HEADER_OF", "UNIT_OF", "MODEL_OF") and target == value_id:
            by_value.setdefault(relation, []).append((header, relation))
        if relation == "HEADER_PARENT_OF_HEADER":
            parent_of.setdefault(target, []).append(header)

    ambiguous: list[str] = []

    def unique(relation: str, axis: str) -> tuple[str, ...]:
        members = sorted({header for header, _ in by_value.get(relation, [])})
        if len(members) > 1:
            ambiguous.append(axis)
        return tuple(members)

    column_header = unique("COLUMN_HEADER_OF", "COLUMN")
    row_header = unique("ROW_HEADER_OF", "ROW")
    unit = unique("UNIT_OF", "UNIT")
    model = unique("MODEL_OF", "MODEL")

    visited: set[str] = set()
    stack = [header for header, _ in by_value.get("COLUMN_HEADER_OF", [])]
    multi: list[str] = []
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        for parent in sorted(parent_of.get(node, ())):
            if parent not in visited:
                stack.append(parent)
        if node not in {header for header, _ in by_value.get("COLUMN_HEADER_OF", [])}:
            multi.append(node)
    multi_level = tuple(sorted(set(multi)))
    return DerivedContext(
        value_id=value_id,
        row_header=row_header,
        column_header=column_header,
        model=model,
        unit=unit,
        multi_level_parents=multi_level,
        section=section,
        ambiguous_axes=tuple(sorted(set(ambiguous))),
    )


def derive_page_contexts(page: PageLayout) -> dict[str, DerivedContext]:
    """Derive context for every data cell of a page (deterministic order)."""
    graph = ContextGraph(page)
    return {cell.cell_id: graph.derive_context(cell) for cell in sorted(page.cells, key=lambda item: item.cell_id)}
