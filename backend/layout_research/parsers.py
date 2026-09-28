from __future__ import annotations

from dataclasses import dataclass, replace
import importlib.metadata
import re
from pathlib import Path
from statistics import median
from typing import Iterable, Protocol

from .geometry import normalize_bbox
from .models import (
    BBox,
    DocumentLayout,
    HeaderRelationship,
    LayoutBlock,
    PageLayout,
    TableCell,
    TableColumn,
    TableRegion,
    TableRow,
    stable_id,
)


NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:[.,]\d+)?(?:\s*(?:[-–—]|to)\s*[-+]?\d+(?:[.,]\d+)?)?", re.I)
HEADING_PATTERN = re.compile(r"^(?:\d+(?:\.\d+)*|[A-Z])(?:[.)]?\s+)\S")
LIST_PATTERN = re.compile(r"^(?:[-•▪]|\d+[.)])\s+")
CAPTION_PATTERN = re.compile(r"^(?:table|figure|表|图)\s*[\dA-Z一二三四五六七八九十.-]*", re.I)


@dataclass(frozen=True)
class TextSpan:
    text: str
    bbox: BBox
    font_size: float = 10.0
    font_name: str = ""
    source_span: str = ""


class LayoutParser(Protocol):
    name: str
    version: str

    def parse(self, path: str | Path, document_id: str) -> DocumentLayout: ...


def _config_digest(parser_name: str, parser_version: str) -> str:
    return stable_id("config", parser_name, parser_version, "layout-research-v1")


def _union(items: Iterable[BBox]) -> BBox:
    iterator = iter(items)
    result = next(iterator)
    for item in iterator:
        result = result.union(item)
    return result.rounded()


def _line_groups(spans: list[TextSpan]) -> list[list[TextSpan]]:
    ordered = sorted(spans, key=lambda item: ((item.bbox.y0 + item.bbox.y1) / 2, item.bbox.x0))
    groups: list[list[TextSpan]] = []
    for span in ordered:
        center = (span.bbox.y0 + span.bbox.y1) / 2
        tolerance = max(2.5, span.font_size * 0.35)
        if groups:
            prior_center = median((item.bbox.y0 + item.bbox.y1) / 2 for item in groups[-1])
            if abs(center - prior_center) <= tolerance:
                groups[-1].append(span)
                continue
        groups.append([span])
    return [sorted(group, key=lambda item: item.bbox.x0) for group in groups]


def _line_segments(line: list[TextSpan]) -> list[TextSpan]:
    segments: list[list[TextSpan]] = []
    for span in line:
        if not segments:
            segments.append([span])
            continue
        prior = segments[-1][-1]
        join_gap = max(5.0, min(prior.font_size, span.font_size) * 1.1)
        if span.bbox.x0 - prior.bbox.x1 <= join_gap:
            segments[-1].append(span)
        else:
            segments.append([span])
    results = []
    for segment in segments:
        text = " ".join(item.text.strip() for item in segment if item.text.strip())
        if text:
            results.append(
                TextSpan(
                    text=text,
                    bbox=_union(item.bbox for item in segment),
                    font_size=max(item.font_size for item in segment),
                    font_name=" ".join(sorted({item.font_name for item in segment if item.font_name})),
                    source_span="|".join(item.source_span for item in segment if item.source_span),
                )
            )
    return results


def _aligned_columns(lines: list[list[TextSpan]], tolerance: float = 14.0) -> int:
    anchors: list[list[tuple[int, float]]] = []
    for line_number, line in enumerate(lines):
        for span in line:
            for cluster in anchors:
                if abs(median(item[1] for item in cluster) - span.bbox.x0) <= tolerance:
                    cluster.append((line_number, span.bbox.x0))
                    break
            else:
                anchors.append([(line_number, span.bbox.x0)])
    return sum(1 for cluster in anchors if len({item[0] for item in cluster}) >= 2)


def _detected_table_runs(lines: list[list[TextSpan]]) -> list[list[list[TextSpan]]]:
    candidates = [
        len(line) >= 2
        for line in lines
    ]
    runs: list[list[list[TextSpan]]] = []
    start = 0
    while start < len(lines):
        if not candidates[start]:
            start += 1
            continue
        end = start + 1
        while end < len(lines) and candidates[end]:
            prior_box = _union(item.bbox for item in lines[end - 1])
            current_box = _union(item.bbox for item in lines[end])
            if current_box.y0 - prior_box.y1 > max(24.0, prior_box.height * 2.0):
                break
            end += 1
        run = lines[start:end]
        if len(run) >= 2 and _aligned_columns(run) >= 2:
            runs.append(run)
        start = end
    return runs


def _is_numeric(text: str) -> bool:
    return bool(NUMBER_PATTERN.search(text))


def _make_table(
    document_id: str,
    page_number: int,
    lines: list[list[TextSpan]],
    parser_name: str,
    parser_version: str,
) -> tuple[TableRegion, list[TableRow], list[TableColumn], list[TableCell], list[HeaderRelationship]]:
    table_box = _union(span.bbox for line in lines for span in line)
    table_id = stable_id("table", document_id, page_number, table_box.to_list())
    anchors: list[list[float]] = []
    for line in lines:
        for span in line:
            for cluster in anchors:
                if abs(median(cluster) - span.bbox.x0) <= 14.0:
                    cluster.append(span.bbox.x0)
                    break
            else:
                anchors.append([span.bbox.x0])
    column_xs = sorted(median(cluster) for cluster in anchors if len(cluster) >= 2)
    if len(column_xs) < 2:
        column_xs = sorted({round(span.bbox.x0, 3) for line in lines for span in line})

    row_ids = [stable_id("row", table_id, index, _union(span.bbox for span in line).to_list()) for index, line in enumerate(lines)]
    column_ids = [stable_id("column", table_id, index, x) for index, x in enumerate(column_xs)]
    row_cell_ids: dict[str, list[str]] = {row_id: [] for row_id in row_ids}
    column_cell_ids: dict[str, list[str]] = {column_id: [] for column_id in column_ids}
    cells: list[TableCell] = []
    cell_by_position: dict[tuple[int, int], TableCell] = {}
    for row_index, line in enumerate(lines):
        for span in line:
            distances = sorted((abs(span.bbox.x0 - x), index) for index, x in enumerate(column_xs))
            column_index = distances[0][1]
            column_id = column_ids[column_index]
            cell_id = stable_id("cell", table_id, row_index, column_index, span.bbox.to_list(), span.text)
            cell = TableCell(
                cell_id=cell_id,
                bbox=span.bbox,
                text=span.text,
                page_number=page_number,
                row_id=row_ids[row_index],
                column_id=column_id,
                source_span=span.source_span,
            )
            cells.append(cell)
            cell_by_position[(row_index, column_index)] = cell
            row_cell_ids[row_ids[row_index]].append(cell_id)
            column_cell_ids[column_id].append(cell_id)

    header_row_count = 0
    for line in lines:
        numeric_ratio = sum(_is_numeric(span.text) for span in line) / len(line)
        if numeric_ratio < 0.5:
            header_row_count += 1
        else:
            break
    header_row_count = min(max(1, header_row_count), max(1, len(lines) - 1))

    relationships: list[HeaderRelationship] = []
    headers_by_value: dict[str, list[str]] = {}
    for row_index in range(header_row_count, len(lines)):
        row_header = cell_by_position.get((row_index, 0))
        for column_index in range(1, len(column_ids)):
            value = cell_by_position.get((row_index, column_index))
            if value is None or not _is_numeric(value.text):
                continue
            header_candidates: list[TableCell] = []
            peer_conflict = False
            for header_row in range(header_row_count):
                row_candidates = []
                for header_column in range(len(column_ids)):
                    candidate = cell_by_position.get((header_row, header_column))
                    if candidate and (
                        header_column == column_index
                        or candidate.bbox.overlaps_x(value.bbox, tolerance=2.0)
                    ):
                        row_candidates.append(candidate)
                if len(row_candidates) > 1:
                    peer_conflict = True
                header_candidates.extend(row_candidates)
            if row_header and row_header.cell_id != value.cell_id:
                header_candidates.append(row_header)
            unique_headers = list(dict.fromkeys(item.cell_id for item in header_candidates))
            if peer_conflict or not unique_headers:
                relationships.append(
                    HeaderRelationship(
                        relationship_id=stable_id("rel", table_id, "AMBIGUOUS", value.cell_id),
                        header_block_id="",
                        value_block_id=value.cell_id,
                        relationship_type="AMBIGUOUS",
                        page_number=page_number,
                        table_region_id=table_id,
                        confidence=0.0,
                    )
                )
                continue
            headers_by_value[value.cell_id] = unique_headers
            for header_id in unique_headers:
                relationships.append(
                    HeaderRelationship(
                        relationship_id=stable_id("rel", table_id, header_id, value.cell_id),
                        header_block_id=header_id,
                        value_block_id=value.cell_id,
                        relationship_type="HEADER_APPLIES_TO",
                        page_number=page_number,
                        table_region_id=table_id,
                    )
                )
    cells = [replace(cell, header_ids=tuple(headers_by_value.get(cell.cell_id, ()))) for cell in cells]
    rows = [
        TableRow(row_id, _union(cell.bbox for cell in cells if cell.row_id == row_id), page_number, tuple(row_cell_ids[row_id]))
        for row_id in row_ids
    ]
    columns = [
        TableColumn(column_id, _union(cell.bbox for cell in cells if cell.column_id == column_id), page_number, tuple(column_cell_ids[column_id]))
        for column_id in column_ids
    ]
    region = TableRegion(
        table_region_id=table_id,
        bbox=table_box,
        page_number=page_number,
        row_ids=tuple(row_ids),
        column_ids=tuple(column_ids),
        cell_ids=tuple(cell.cell_id for cell in cells),
        source_span="|".join(cell.source_span for cell in cells if cell.source_span),
    )
    return region, rows, columns, cells, relationships


def parse_page_spans(
    *,
    document_id: str,
    page_number: int,
    width: float,
    height: float,
    spans: list[TextSpan],
    parser_name: str,
    parser_version: str,
    table_hints: list[BBox] | None = None,
) -> PageLayout:
    lines = [_line_segments(line) for line in _line_groups([span for span in spans if span.text.strip()])]
    lines = [line for line in lines if line]
    if table_hints is None:
        table_runs = _detected_table_runs(lines)
    else:
        table_runs = []
        for hint in table_hints:
            selected = [line for line in lines if any(hint.overlaps_x(span.bbox) and hint.y0 <= (span.bbox.y0 + span.bbox.y1) / 2 <= hint.y1 for span in line)]
            if len(selected) >= 2:
                table_runs.append(selected)

    table_line_keys = {
        tuple((span.bbox.to_list()[0], span.bbox.to_list()[1], span.text) for span in line)
        for run in table_runs
        for line in run
    }
    regions: list[TableRegion] = []
    rows: list[TableRow] = []
    columns: list[TableColumn] = []
    cells: list[TableCell] = []
    relationships: list[HeaderRelationship] = []
    for run in table_runs:
        region, new_rows, new_columns, new_cells, new_relationships = _make_table(
            document_id, page_number, run, parser_name, parser_version
        )
        regions.append(region)
        rows.extend(new_rows)
        columns.extend(new_columns)
        cells.extend(new_cells)
        relationships.extend(new_relationships)

    cell_lookup = {(cell.bbox.to_list()[0], cell.bbox.to_list()[1], cell.text): cell for cell in cells}
    font_sizes = [span.font_size for line in lines for span in line]
    body_size = median(font_sizes) if font_sizes else 10.0
    blocks: list[LayoutBlock] = []
    section_id = ""
    section_title = ""
    order = 0
    for line in lines:
        key = tuple((span.bbox.to_list()[0], span.bbox.to_list()[1], span.text) for span in line)
        if key in table_line_keys:
            for span in line:
                cell = cell_lookup.get((span.bbox.to_list()[0], span.bbox.to_list()[1], span.text))
                if cell is None:
                    continue
                block_id = cell.cell_id
                blocks.append(
                    LayoutBlock(
                        block_id=block_id,
                        block_type="TABLE_CELL",
                        bbox=span.bbox,
                        reading_order=order,
                        text=span.text,
                        page_number=page_number,
                        section_id=section_id,
                        section_title=section_title,
                        table_region_id=next(region.table_region_id for region in regions if cell.cell_id in region.cell_ids),
                        row_id=cell.row_id,
                        column_id=cell.column_id,
                        header_ids=cell.header_ids,
                        source_span=span.source_span,
                        parser_name=parser_name,
                        parser_version=parser_version,
                    )
                )
                order += 1
            continue
        text = " ".join(span.text for span in line).strip()
        line_box = _union(span.bbox for span in line)
        max_size = max(span.font_size for span in line)
        font_names = " ".join(span.font_name.casefold() for span in line)
        if CAPTION_PATTERN.match(text):
            block_type = "CAPTION"
        elif LIST_PATTERN.match(text):
            block_type = "LIST"
        elif max_size >= body_size * 1.22 or "bold" in font_names or HEADING_PATTERN.match(text):
            block_type = "HEADING"
        else:
            block_type = "PARAGRAPH"
        block_id = stable_id("block", document_id, page_number, line_box.to_list(), text)
        if block_type == "HEADING":
            section_id = stable_id("section", document_id, page_number, block_id)
            section_title = text
        blocks.append(
            LayoutBlock(
                block_id=block_id,
                block_type=block_type,
                bbox=line_box,
                reading_order=order,
                text=text,
                page_number=page_number,
                section_id=section_id,
                section_title=section_title,
                source_span="|".join(span.source_span for span in line if span.source_span),
                parser_name=parser_name,
                parser_version=parser_version,
            )
        )
        order += 1
    return PageLayout(
        document_id=document_id,
        page_number=page_number,
        width=width,
        height=height,
        rotation=0,
        coordinate_origin="top-left",
        blocks=tuple(blocks),
        table_regions=tuple(regions),
        rows=tuple(rows),
        columns=tuple(columns),
        cells=tuple(cells),
        relationships=tuple(relationships),
        parser_name=parser_name,
        parser_version=parser_version,
    )


class PypdfGeometryParser:
    name = "PYPDF_GEOMETRY"

    def __init__(self) -> None:
        self.version = importlib.metadata.version("pypdf")

    def parse(self, path: str | Path, document_id: str) -> DocumentLayout:
        from pypdf import PdfReader

        pages = []
        reader = PdfReader(str(path))
        for page_number, page in enumerate(reader.pages, start=1):
            raw_width = float(page.mediabox.width)
            raw_height = float(page.mediabox.height)
            rotation = int(page.get("/Rotate", 0) or 0) % 360
            spans: list[TextSpan] = []

            def visitor(text, cm, tm, font_dict, font_size):
                if not text or not text.strip():
                    return
                x = float(tm[4] * cm[0] + tm[5] * cm[2] + cm[4])
                y = float(tm[4] * cm[1] + tm[5] * cm[3] + cm[5])
                lines = [item.strip() for item in text.splitlines() if item.strip()]
                for line_index, value in enumerate(lines):
                    cursor = x
                    baseline = y - line_index * float(font_size) * 1.2
                    for token_index, token in enumerate(value.split()):
                        width = max(float(font_size) * 0.45, len(token) * float(font_size) * 0.52)
                        raw_box = BBox(cursor, baseline - float(font_size) * 0.2, cursor + width, baseline + float(font_size) * 0.8)
                        box, _, _ = normalize_bbox(
                            raw_box,
                            page_width=raw_width,
                            page_height=raw_height,
                            origin="bottom-left",
                            rotation=rotation,
                        )
                        spans.append(
                            TextSpan(
                                token,
                                box,
                                float(font_size),
                                str((font_dict or {}).get("/BaseFont", "")),
                                f"op:{page_number}:{len(spans)}:{token_index}",
                            )
                        )
                        cursor += width + float(font_size) * 0.35

            page.extract_text(visitor_text=visitor)
            out_width, out_height = (raw_height, raw_width) if rotation in {90, 270} else (raw_width, raw_height)
            pages.append(
                parse_page_spans(
                    document_id=document_id,
                    page_number=page_number,
                    width=out_width,
                    height=out_height,
                    spans=spans,
                    parser_name=self.name,
                    parser_version=self.version,
                )
            )
        return DocumentLayout(document_id, tuple(pages), self.name, self.version, _config_digest(self.name, self.version))


class PdfPlumberTableParser:
    name = "PDFPLUMBER_TABLE"

    def __init__(self) -> None:
        self.version = importlib.metadata.version("pdfplumber")

    def parse(self, path: str | Path, document_id: str) -> DocumentLayout:
        import pdfplumber

        pages = []
        with pdfplumber.open(str(path)) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                spans = [
                    TextSpan(
                        text=str(word["text"]),
                        bbox=BBox(float(word["x0"]), float(word["top"]), float(word["x1"]), float(word["bottom"])).rounded(),
                        font_size=float(word.get("size", 10.0)),
                        font_name=str(word.get("fontname", "")),
                        source_span=f"word:{page_number}:{index}",
                    )
                    for index, word in enumerate(
                        page.extract_words(extra_attrs=["fontname", "size"], use_text_flow=False)
                    )
                ]
                table_hints = [BBox.from_value(table.bbox).rounded() for table in page.find_tables()]
                pages.append(
                    parse_page_spans(
                        document_id=document_id,
                        page_number=page_number,
                        width=float(page.width),
                        height=float(page.height),
                        spans=spans,
                        parser_name=self.name,
                        parser_version=self.version,
                        table_hints=table_hints,
                    )
                )
        return DocumentLayout(document_id, tuple(pages), self.name, self.version, _config_digest(self.name, self.version))


def _ownership_candidate_axis(source: TableCell, target: TableCell) -> tuple[str, float] | None:
    """Return structural proof class and distance; text adjacency is never evidence."""
    if source.row_id == target.row_id and source.bbox.x1 <= target.bbox.x0 + 1:
        return "ROW", max(0.0, target.bbox.x0 - source.bbox.x1)
    if source.column_id == target.column_id and source.bbox.y1 <= target.bbox.y0 + 1:
        return "COLUMN", max(0.0, target.bbox.y0 - source.bbox.y1)
    target_center = (target.bbox.x0 + target.bbox.x1) / 2
    if (
        source.bbox.y1 <= target.bbox.y0 + 1
        and source.bbox.x0 - 1 <= target_center <= source.bbox.x1 + 1
    ):
        return "SPANNING", max(0.0, target.bbox.y0 - source.bbox.y1)
    return None


def _normalized_cell_text(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def refine_structured_ownership(
    page: PageLayout,
    *,
    parser_name: str = "PDFPLUMBER_STRUCTURED_OWNERSHIP_V1",
    parser_version: str = "1",
    reject_repeated_text_globally: bool = True,
) -> PageLayout:
    """Apply a precision-first structural ownership acceptance contract.

    Candidate generation remains the baseline parser's high-recall edge set.
    Acceptance requires same-table row/column proof or a unique containing
    spanning header. Broad fanout and duplicate-header ambiguity emit no
    ownership edge. Distance orders already eligible structural candidates;
    it never creates eligibility by itself.
    """
    cells = {cell.cell_id: cell for cell in page.cells}
    table_by_cell = {
        cell_id: region.table_region_id
        for region in page.table_regions
        for cell_id in region.cell_ids
    }
    candidate_edges = [
        edge for edge in page.relationships
        if edge.relationship_type == "HEADER_APPLIES_TO"
        and edge.header_block_id in cells
        and edge.value_block_id in cells
        and table_by_cell.get(edge.header_block_id) == table_by_cell.get(edge.value_block_id)
    ]
    source_fanout: dict[str, int] = {}
    text_counts: dict[tuple[str, str], int] = {}
    for edge in candidate_edges:
        source_fanout[edge.header_block_id] = source_fanout.get(edge.header_block_id, 0) + 1
    for cell_id, cell in cells.items():
        key = (table_by_cell.get(cell_id, ""), _normalized_cell_text(cell.text))
        text_counts[key] = text_counts.get(key, 0) + 1

    eligible: dict[str, dict[str, list[tuple[HeaderRelationship, float]]]] = {}
    rejected_targets: set[str] = set()
    for edge in candidate_edges:
        source = cells[edge.header_block_id]
        target = cells[edge.value_block_id]
        evidence = _ownership_candidate_axis(source, target)
        if evidence is None:
            rejected_targets.add(edge.value_block_id)
            continue
        axis, distance = evidence
        duplicate_key = (table_by_cell.get(source.cell_id, ""), _normalized_cell_text(source.text))
        if reject_repeated_text_globally and text_counts.get(duplicate_key, 0) > 1:
            rejected_targets.add(edge.value_block_id)
            continue
        if (
            axis == "SPANNING"
            and source_fanout.get(source.cell_id, 0) > 12
            and source.bbox.width > target.bbox.width * 4
        ):
            rejected_targets.add(edge.value_block_id)
            continue
        eligible.setdefault(target.cell_id, {}).setdefault(axis, []).append((edge, distance))

    accepted: list[HeaderRelationship] = []
    ambiguous_targets: set[str] = set(rejected_targets)
    for target_id, by_axis in eligible.items():
        for candidates in by_axis.values():
            ordered = sorted(candidates, key=lambda item: (item[1], item[0].header_block_id))
            best_distance = ordered[0][1]
            best = [item for item in ordered if abs(item[1] - best_distance) <= 0.5]
            if len(best) != 1:
                ambiguous_targets.add(target_id)
                continue
            accepted.append(best[0][0])

    accepted_by_target: dict[str, list[str]] = {}
    for edge in accepted:
        accepted_by_target.setdefault(edge.value_block_id, []).append(edge.header_block_id)
    cells_out = tuple(
        replace(cell, header_ids=tuple(sorted(accepted_by_target.get(cell.cell_id, ()))))
        for cell in page.cells
    )
    relationships: list[HeaderRelationship] = list(accepted)
    accepted_targets = set(accepted_by_target)
    for target_id in sorted(ambiguous_targets - accepted_targets):
        relationships.append(
            HeaderRelationship(
                relationship_id=stable_id("rel", table_by_cell.get(target_id, ""), "AMBIGUOUS", target_id),
                header_block_id="",
                value_block_id=target_id,
                relationship_type="AMBIGUOUS",
                page_number=page.page_number,
                table_region_id=table_by_cell.get(target_id, ""),
                confidence=0.0,
            )
        )
    header_ids_by_cell = {cell.cell_id: cell.header_ids for cell in cells_out}
    blocks = tuple(
        replace(
            block,
            header_ids=header_ids_by_cell.get(block.block_id, block.header_ids),
            parser_name=parser_name,
            parser_version=parser_version,
        )
        for block in page.blocks
    )
    return replace(
        page,
        blocks=blocks,
        cells=cells_out,
        relationships=tuple(sorted(relationships, key=lambda edge: edge.relationship_id)),
        parser_name=parser_name,
        parser_version=parser_version,
    )


class PdfPlumberStructuredOwnershipParser:
    """Experimental ownership-only refinement; table detection is unchanged."""

    name = "PDFPLUMBER_STRUCTURED_OWNERSHIP_V1"

    def __init__(self) -> None:
        self.pdfplumber_version = importlib.metadata.version("pdfplumber")
        self.version = f"{self.pdfplumber_version}+ownership-v1"

    def parse(self, path: str | Path, document_id: str) -> DocumentLayout:
        baseline = PdfPlumberTableParser().parse(path, document_id)
        pages = tuple(
            refine_structured_ownership(
                page,
                parser_name=self.name,
                parser_version=self.version,
            )
            for page in baseline.pages
        )
        return DocumentLayout(
            document_id,
            pages,
            self.name,
            self.version,
            _config_digest(self.name, self.version),
        )


class PdfPlumberStructuredOwnershipV2Parser:
    """V2 scopes duplicate ambiguity to competing structural parents."""

    name = "PDFPLUMBER_STRUCTURED_OWNERSHIP_V2"

    def __init__(self) -> None:
        self.pdfplumber_version = importlib.metadata.version("pdfplumber")
        self.version = f"{self.pdfplumber_version}+ownership-v2"

    def parse(self, path: str | Path, document_id: str) -> DocumentLayout:
        baseline = PdfPlumberTableParser().parse(path, document_id)
        pages = tuple(
            refine_structured_ownership(
                page,
                parser_name=self.name,
                parser_version=self.version,
                reject_repeated_text_globally=False,
            )
            for page in baseline.pages
        )
        return DocumentLayout(
            document_id,
            pages,
            self.name,
            self.version,
            _config_digest(self.name, self.version),
        )


class FlatBaselineParser:
    name = "FLAT_BASELINE"

    def __init__(self) -> None:
        self.version = importlib.metadata.version("pypdf")

    def parse(self, path: str | Path, document_id: str) -> DocumentLayout:
        from pypdf import PdfReader

        pages = []
        reader = PdfReader(str(path))
        for page_number, page in enumerate(reader.pages, start=1):
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
            text = page.extract_text() or ""
            blocks = ()
            if text.strip():
                box = BBox(0.0, 0.0, width, height)
                blocks = (
                    LayoutBlock(
                        block_id=stable_id("block", document_id, page_number, text.strip()),
                        block_type="PARAGRAPH",
                        bbox=box,
                        reading_order=0,
                        text=text.strip(),
                        page_number=page_number,
                        source_span=f"page:{page_number}",
                        parser_name=self.name,
                        parser_version=self.version,
                    ),
                )
            pages.append(
                PageLayout(
                    document_id=document_id,
                    page_number=page_number,
                    width=width,
                    height=height,
                    rotation=0,
                    coordinate_origin="top-left",
                    blocks=blocks,
                    parser_name=self.name,
                    parser_version=self.version,
                )
            )
        return DocumentLayout(document_id, tuple(pages), self.name, self.version, _config_digest(self.name, self.version))


PARSERS = {
    "pypdf_geometry": PypdfGeometryParser,
    "pdfplumber_table": PdfPlumberTableParser,
    "pdfplumber_structured_ownership_v1": PdfPlumberStructuredOwnershipParser,
    "pdfplumber_structured_ownership_v2": PdfPlumberStructuredOwnershipV2Parser,
    "flat_baseline": FlatBaselineParser,
}
