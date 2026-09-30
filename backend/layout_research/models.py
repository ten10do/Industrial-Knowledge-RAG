from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass
import hashlib
import json
from typing import Any, Literal


BlockType = Literal[
    "HEADING",
    "PARAGRAPH",
    "LIST",
    "TABLE_CELL",
    "CAPTION",
    "OTHER",
]
RelationshipType = Literal[
    "SECTION_CONTAINS",
    "TABLE_CONTAINS",
    "ROW_CONTAINS",
    "COLUMN_CONTAINS",
    "HEADER_APPLIES_TO",
    "CAPTION_DESCRIBES",
    "AMBIGUOUS",
]


def stable_id(kind: str, *parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"{kind}-{digest}"


@dataclass(frozen=True)
class BBox:
    """Normalized page box in points, top-left origin, x rightward, y downward."""

    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        if self.x0 > self.x1 or self.y0 > self.y1:
            raise ValueError("bbox coordinates must satisfy x0 <= x1 and y0 <= y1")

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return self.width * self.height

    def rounded(self, digits: int = 3) -> "BBox":
        return BBox(*(round(value, digits) for value in asdict(self).values()))

    def union(self, other: "BBox") -> "BBox":
        return BBox(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )

    def overlaps_x(self, other: "BBox", tolerance: float = 0.0) -> bool:
        return min(self.x1, other.x1) - max(self.x0, other.x0) >= -tolerance

    def to_list(self) -> list[float]:
        return [self.x0, self.y0, self.x1, self.y1]

    @classmethod
    def from_value(cls, value: "BBox | list[float] | tuple[float, ...]") -> "BBox":
        if isinstance(value, cls):
            return value
        return cls(*(float(item) for item in value))


@dataclass(frozen=True)
class LayoutBlock:
    block_id: str
    block_type: BlockType
    bbox: BBox
    reading_order: int
    text: str
    page_number: int
    section_id: str = ""
    section_title: str = ""
    table_region_id: str = ""
    row_id: str = ""
    column_id: str = ""
    header_ids: tuple[str, ...] = ()
    caption_id: str = ""
    source_span: str = ""
    parser_name: str = ""
    parser_version: str = ""
    confidence: float = 1.0


@dataclass(frozen=True)
class TableCell:
    cell_id: str
    bbox: BBox
    text: str
    page_number: int
    row_id: str
    column_id: str
    header_ids: tuple[str, ...] = ()
    source_span: str = ""
    confidence: float = 1.0


@dataclass(frozen=True)
class TableRow:
    row_id: str
    bbox: BBox
    page_number: int
    cell_ids: tuple[str, ...]


@dataclass(frozen=True)
class TableColumn:
    column_id: str
    bbox: BBox
    page_number: int
    cell_ids: tuple[str, ...]


@dataclass(frozen=True)
class HeaderRelationship:
    relationship_id: str
    header_block_id: str
    value_block_id: str
    relationship_type: RelationshipType
    page_number: int
    table_region_id: str = ""
    confidence: float = 1.0


@dataclass(frozen=True)
class TableRegion:
    table_region_id: str
    bbox: BBox
    page_number: int
    row_ids: tuple[str, ...]
    column_ids: tuple[str, ...]
    cell_ids: tuple[str, ...]
    caption_id: str = ""
    source_span: str = ""
    confidence: float = 1.0


@dataclass(frozen=True)
class PageLayout:
    document_id: str
    page_number: int
    width: float
    height: float
    rotation: int
    coordinate_origin: str
    blocks: tuple[LayoutBlock, ...] = ()
    table_regions: tuple[TableRegion, ...] = ()
    rows: tuple[TableRow, ...] = ()
    columns: tuple[TableColumn, ...] = ()
    cells: tuple[TableCell, ...] = ()
    relationships: tuple[HeaderRelationship, ...] = ()
    parser_name: str = ""
    parser_version: str = ""
    failure: str = ""


@dataclass(frozen=True)
class DocumentLayout:
    document_id: str
    pages: tuple[PageLayout, ...]
    parser_name: str
    parser_version: str
    config_digest: str


@dataclass(frozen=True)
class StructuredChunkCandidate:
    chunk_id: str
    text: str
    document_id: str
    page_number: int
    section_id: str
    block_ids: tuple[str, ...]
    block_type: BlockType
    source_bboxes: tuple[BBox, ...]
    table_region_id: str = ""
    row_ids: tuple[str, ...] = ()
    column_ids: tuple[str, ...] = ()
    header_context: tuple[str, ...] = ()


def _encode(value: Any) -> Any:
    if isinstance(value, BBox):
        return value.to_list()
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if is_dataclass(value):
        return {item.name: _encode(getattr(value, item.name)) for item in fields(value)}
    return value


def to_dict(value: Any) -> dict[str, Any]:
    return _encode(value)


def page_from_dict(data: dict[str, Any]) -> PageLayout:
    blocks = tuple(
        LayoutBlock(
            **{
                **item,
                "bbox": BBox.from_value(item["bbox"]),
                "header_ids": tuple(item.get("header_ids", ())),
            }
        )
        for item in data.get("blocks", ())
    )
    rows = tuple(
        TableRow(
            **{
                **item,
                "bbox": BBox.from_value(item["bbox"]),
                "cell_ids": tuple(item.get("cell_ids", ())),
            }
        )
        for item in data.get("rows", ())
    )
    columns = tuple(
        TableColumn(
            **{
                **item,
                "bbox": BBox.from_value(item["bbox"]),
                "cell_ids": tuple(item.get("cell_ids", ())),
            }
        )
        for item in data.get("columns", ())
    )
    cells = tuple(
        TableCell(
            **{
                **item,
                "bbox": BBox.from_value(item["bbox"]),
                "header_ids": tuple(item.get("header_ids", ())),
            }
        )
        for item in data.get("cells", ())
    )
    regions = tuple(
        TableRegion(
            **{
                **item,
                "bbox": BBox.from_value(item["bbox"]),
                "row_ids": tuple(item.get("row_ids", ())),
                "column_ids": tuple(item.get("column_ids", ())),
                "cell_ids": tuple(item.get("cell_ids", ())),
            }
        )
        for item in data.get("table_regions", ())
    )
    relationships = tuple(HeaderRelationship(**item) for item in data.get("relationships", ()))
    return PageLayout(
        document_id=data["document_id"],
        page_number=int(data["page_number"]),
        width=float(data["width"]),
        height=float(data["height"]),
        rotation=int(data.get("rotation", 0)),
        coordinate_origin=data.get("coordinate_origin", "top-left"),
        blocks=blocks,
        table_regions=regions,
        rows=rows,
        columns=columns,
        cells=cells,
        relationships=relationships,
        parser_name=data.get("parser_name", ""),
        parser_version=data.get("parser_version", ""),
        failure=data.get("failure", ""),
    )
