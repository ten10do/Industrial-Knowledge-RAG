from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

from .models import (
    BBox,
    HeaderRelationship,
    LayoutBlock,
    PageLayout,
    TableCell,
    TableColumn,
    TableRegion,
    TableRow,
    page_from_dict,
    stable_id,
    to_dict,
)
from .parsers import TextSpan, parse_page_spans


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    slice_name: str
    rows: tuple[tuple[str, ...], ...] = ()
    header_rows: int = 1
    heading: str = "Technical Specifications"
    caption: str = ""
    column_xs: tuple[float, ...] = (72.0, 250.0)
    column_widths: tuple[float, ...] = (150.0, 100.0)
    merged_cells: dict[tuple[int, int], tuple[float, float]] = field(default_factory=dict)
    ambiguous_values: frozenset[tuple[int, int]] = frozenset()
    paragraphs: tuple[str, ...] = ()


CASES = (
    CaseSpec(
        "two_column_specification",
        "simple_table",
        (("Parameter", "Value"), ("Rated voltage", "24 V"), ("Rated current", "2 A")),
    ),
    CaseSpec(
        "three_model_comparison",
        "model_comparison",
        (("Parameter", "Model A", "Model B", "Model C"), ("Power", "10 kW", "12 kW", "14 kW"), ("Speed", "900 rpm", "1100 rpm", "1300 rpm")),
        column_xs=(60.0, 190.0, 300.0, 410.0),
        column_widths=(110.0, 90.0, 90.0, 90.0),
    ),
    CaseSpec(
        "merged_header",
        "merged_header",
        (("Category", "Performance"), ("Parameter", "Model A", "Model B"), ("Torque", "40 Nm", "55 Nm")),
        header_rows=2,
        column_xs=(60.0, 210.0, 340.0),
        column_widths=(125.0, 105.0, 105.0),
        merged_cells={(0, 1): (210.0, 445.0)},
    ),
    CaseSpec(
        "unit_column",
        "multi_column",
        (("Parameter", "Unit", "Value"), ("Pressure", "bar", "8.5"), ("Temperature", "°C", "65")),
        column_xs=(70.0, 250.0, 340.0),
        column_widths=(150.0, 70.0, 100.0),
    ),
    CaseSpec(
        "range_value",
        "simple_table",
        (("Parameter", "Allowed range"), ("Supply", "20–28 V"), ("Humidity", "10-90 %")),
    ),
    CaseSpec(
        "section_and_table",
        "mixed_layout",
        (("Parameter", "Value"), ("Cycle time", "5 ms"), ("Accuracy", "0.1 %")),
        heading="4.2 Control Limits",
        paragraphs=("Use these limits during normal operation.",),
    ),
    CaseSpec(
        "borderless_aligned_columns",
        "borderless_table",
        (("Item", "Minimum", "Maximum"), ("Voltage", "18 V", "30 V"), ("Current", "1 A", "4 A")),
        column_xs=(65.0, 245.0, 365.0),
        column_widths=(150.0, 95.0, 95.0),
    ),
    CaseSpec(
        "caption_and_table",
        "mixed_layout",
        (("Parameter", "Value"), ("Mass", "12 kg"), ("Height", "420 mm")),
        caption="Table 2 — Mechanical data",
    ),
    CaseSpec(
        "multi_line_cell",
        "multi_column",
        (("Parameter", "Value"), ("Rated\ncurrent", "3.2 A"), ("Peak current", "8.0 A")),
    ),
    CaseSpec(
        "ambiguous_ownership",
        "multi_column",
        (("Parameter", "Low", "High"), ("Limit", "", "50"), ("Nominal", "20", "30")),
        column_xs=(65.0, 235.0, 350.0),
        column_widths=(145.0, 95.0, 95.0),
        merged_cells={(1, 2): (225.0, 445.0)},
        ambiguous_values=frozenset({(1, 2)}),
    ),
    CaseSpec(
        "continued_table_page",
        "cross_page",
        (("Parameter", "Value"), ("Ingress rating", "54"), ("Service life", "40000 h")),
        caption="Table 4 (continued)",
    ),
    CaseSpec(
        "ordinary_text_page",
        "ordinary_text",
        rows=(),
        heading="2 Safety Information",
        paragraphs=(
            "Disconnect power before servicing the equipment.",
            "Keep the enclosure dry and free of conductive dust.",
        ),
    ),
)


def _union(boxes: list[BBox]) -> BBox:
    result = boxes[0]
    for box in boxes[1:]:
        result = result.union(box)
    return result.rounded()


def _span_data(spec: CaseSpec) -> list[dict]:
    spans = []
    y = 42.0
    spans.append({"text": spec.heading, "bbox": [60.0, y, 60.0 + len(spec.heading) * 9.0, y + 20.0], "font_size": 18.0, "font_name": "Synthetic-Bold"})
    y += 42.0
    for paragraph in spec.paragraphs:
        spans.append({"text": paragraph, "bbox": [60.0, y, 60.0 + len(paragraph) * 5.0, y + 12.0], "font_size": 10.0, "font_name": "Synthetic-Regular"})
        y += 24.0
    if spec.caption:
        spans.append({"text": spec.caption, "bbox": [60.0, y, 60.0 + len(spec.caption) * 5.5, y + 12.0], "font_size": 10.0, "font_name": "Synthetic-Regular"})
        y += 22.0
    for row_index, row in enumerate(spec.rows):
        for column_index, text in enumerate(row):
            if not text:
                continue
            x0 = spec.column_xs[column_index]
            x1 = x0 + spec.column_widths[column_index]
            if (row_index, column_index) in spec.merged_cells:
                x0, x1 = spec.merged_cells[(row_index, column_index)]
            spans.append(
                {
                    "text": text,
                    "bbox": [x0, y, x1, y + (22.0 if "\n" in text else 14.0)],
                    "font_size": 10.0,
                    "font_name": "Synthetic-Bold" if row_index < spec.header_rows else "Synthetic-Regular",
                }
            )
        y += 24.0
    for index, span in enumerate(spans):
        span["source_span"] = f"synthetic:{spec.case_id}:{index}"
    return spans


def _gold_page(spec: CaseSpec, spans_data: list[dict]) -> PageLayout:
    document_id = f"synthetic-{spec.case_id}"
    page_number = 1
    heading_span = spans_data[0]
    heading_box = BBox.from_value(heading_span["bbox"])
    section_id = f"gold-section-{spec.case_id}"
    blocks = [
        LayoutBlock(
            block_id=f"gold-block-{spec.case_id}-heading",
            block_type="HEADING",
            bbox=heading_box,
            reading_order=0,
            text=spec.heading,
            page_number=page_number,
            section_id=section_id,
            section_title=spec.heading,
            source_span=heading_span["source_span"],
            parser_name="HUMAN_SYNTHETIC_GOLD",
            parser_version="1",
        )
    ]
    cursor = 1
    offset = 1
    for paragraph in spec.paragraphs:
        span = spans_data[offset]
        offset += 1
        blocks.append(
            LayoutBlock(
                block_id=f"gold-block-{spec.case_id}-paragraph-{cursor}",
                block_type="PARAGRAPH",
                bbox=BBox.from_value(span["bbox"]),
                reading_order=cursor,
                text=paragraph,
                page_number=page_number,
                section_id=section_id,
                section_title=spec.heading,
                source_span=span["source_span"],
                parser_name="HUMAN_SYNTHETIC_GOLD",
                parser_version="1",
            )
        )
        cursor += 1
    if spec.caption:
        span = spans_data[offset]
        offset += 1
        blocks.append(
            LayoutBlock(
                block_id=f"gold-block-{spec.case_id}-caption",
                block_type="CAPTION",
                bbox=BBox.from_value(span["bbox"]),
                reading_order=cursor,
                text=spec.caption,
                page_number=page_number,
                section_id=section_id,
                section_title=spec.heading,
                source_span=span["source_span"],
                parser_name="HUMAN_SYNTHETIC_GOLD",
                parser_version="1",
            )
        )
        cursor += 1
    if not spec.rows:
        return PageLayout(document_id, page_number, 540.0, 720.0, 0, "top-left", tuple(blocks), parser_name="HUMAN_SYNTHETIC_GOLD", parser_version="1")

    table_spans = spans_data[offset:]
    table_id = f"gold-table-{spec.case_id}"
    row_ids = tuple(f"gold-row-{spec.case_id}-{index}" for index in range(len(spec.rows)))
    column_ids = tuple(f"gold-column-{spec.case_id}-{index}" for index in range(len(spec.column_xs)))
    cells = []
    cell_by_position = {}
    span_index = 0
    for row_index, row in enumerate(spec.rows):
        for column_index, text in enumerate(row):
            if not text:
                continue
            span = table_spans[span_index]
            span_index += 1
            cell = TableCell(
                cell_id=f"gold-cell-{spec.case_id}-{row_index}-{column_index}",
                bbox=BBox.from_value(span["bbox"]),
                text=text,
                page_number=page_number,
                row_id=row_ids[row_index],
                column_id=column_ids[column_index],
                source_span=span["source_span"],
            )
            cells.append(cell)
            cell_by_position[(row_index, column_index)] = cell

    relationships = []
    header_map: dict[str, list[str]] = {}
    for row_index in range(spec.header_rows, len(spec.rows)):
        row_header = cell_by_position.get((row_index, 0))
        for column_index in range(1, len(spec.column_xs)):
            value = cell_by_position.get((row_index, column_index))
            if value is None or not any(character.isdigit() for character in value.text):
                continue
            if (row_index, column_index) in spec.ambiguous_values:
                relationships.append(
                    HeaderRelationship(
                        relationship_id=f"gold-rel-{spec.case_id}-ambiguous-{row_index}-{column_index}",
                        header_block_id="",
                        value_block_id=value.cell_id,
                        relationship_type="AMBIGUOUS",
                        page_number=page_number,
                        table_region_id=table_id,
                        confidence=0.0,
                    )
                )
                continue
            headers = []
            for header_row in range(spec.header_rows):
                for header_column in range(len(spec.column_xs)):
                    header = cell_by_position.get((header_row, header_column))
                    if header and (header_column == column_index or header.bbox.overlaps_x(value.bbox, tolerance=2.0)):
                        headers.append(header.cell_id)
            if row_header:
                headers.append(row_header.cell_id)
            headers = list(dict.fromkeys(headers))
            header_map[value.cell_id] = headers
            for header_id in headers:
                relationships.append(
                    HeaderRelationship(
                        relationship_id=f"gold-rel-{spec.case_id}-{len(relationships)}",
                        header_block_id=header_id,
                        value_block_id=value.cell_id,
                        relationship_type="HEADER_APPLIES_TO",
                        page_number=page_number,
                        table_region_id=table_id,
                    )
                )
    cells = [
        TableCell(**{**cell.__dict__, "header_ids": tuple(header_map.get(cell.cell_id, ()))})
        for cell in cells
    ]
    for cell in cells:
        blocks.append(
            LayoutBlock(
                block_id=cell.cell_id,
                block_type="TABLE_CELL",
                bbox=cell.bbox,
                reading_order=cursor,
                text=cell.text,
                page_number=page_number,
                section_id=section_id,
                section_title=spec.heading,
                table_region_id=table_id,
                row_id=cell.row_id,
                column_id=cell.column_id,
                header_ids=cell.header_ids,
                source_span=cell.source_span,
                parser_name="HUMAN_SYNTHETIC_GOLD",
                parser_version="1",
            )
        )
        cursor += 1
    rows = tuple(
        TableRow(row_id, _union([cell.bbox for cell in cells if cell.row_id == row_id]), page_number, tuple(cell.cell_id for cell in cells if cell.row_id == row_id))
        for row_id in row_ids
    )
    columns = tuple(
        TableColumn(column_id, _union([cell.bbox for cell in cells if cell.column_id == column_id]), page_number, tuple(cell.cell_id for cell in cells if cell.column_id == column_id))
        for column_id in column_ids
        if any(cell.column_id == column_id for cell in cells)
    )
    region = TableRegion(
        table_region_id=table_id,
        bbox=_union([cell.bbox for cell in cells]),
        page_number=page_number,
        row_ids=row_ids,
        column_ids=tuple(column.column_id for column in columns),
        cell_ids=tuple(cell.cell_id for cell in cells),
        caption_id=f"gold-block-{spec.case_id}-caption" if spec.caption else "",
        source_span="|".join(cell.source_span for cell in cells),
    )
    return PageLayout(
        document_id,
        page_number,
        540.0,
        720.0,
        0,
        "top-left",
        tuple(blocks),
        (region,),
        rows,
        columns,
        tuple(cells),
        tuple(relationships),
        "HUMAN_SYNTHETIC_GOLD",
        "1",
    )


def synthetic_cases() -> list[dict]:
    result = []
    for spec in CASES:
        spans = _span_data(spec)
        result.append(
            {
                "case_id": spec.case_id,
                "slice": spec.slice_name,
                "input": {
                    "document_id": f"synthetic-{spec.case_id}",
                    "page_number": 1,
                    "width": 540.0,
                    "height": 720.0,
                    "spans": spans,
                },
                "gold": to_dict(_gold_page(spec, spans)),
            }
        )
    return result


def write_synthetic_fixture(path: str | Path) -> None:
    payload = {
        "status": "SYNTHETIC_HUMAN_AUTHORED_GOLD",
        "schema_version": 1,
        "cases": synthetic_cases(),
    }
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_synthetic_fixture(path: str | Path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["cases"]


def predict_synthetic_case(case: dict, parser_name: str = "SYNTHETIC_GEOMETRY") -> PageLayout:
    inputs = case["input"]
    spans = [
        TextSpan(
            text=item["text"],
            bbox=BBox.from_value(item["bbox"]),
            font_size=float(item.get("font_size", 10.0)),
            font_name=item.get("font_name", ""),
            source_span=item.get("source_span", ""),
        )
        for item in inputs["spans"]
    ]
    return parse_page_spans(
        document_id=inputs["document_id"],
        page_number=int(inputs["page_number"]),
        width=float(inputs["width"]),
        height=float(inputs["height"]),
        spans=spans,
        parser_name=parser_name,
        parser_version="1",
    )


def gold_page(case: dict) -> PageLayout:
    return page_from_dict(case["gold"])
