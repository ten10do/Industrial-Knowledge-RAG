from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .models import BBox


PAGE_STATUSES = {"NOT_STARTED", "IN_PROGRESS", "COMPLETED", "REVIEWED"}
RELATIONSHIP_TYPES = {
    "SECTION_CONTAINS",
    "TABLE_CONTAINS",
    "ROW_CONTAINS",
    "COLUMN_CONTAINS",
    "HEADER_APPLIES_TO",
    "CAPTION_DESCRIBES",
    "AMBIGUOUS",
}
BLOCK_TYPES = {"HEADING", "PARAGRAPH", "LIST", "TABLE_CELL", "CAPTION", "OTHER"}


def new_page_annotation(page: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "gold_type": "REAL_LAYOUT_DEV_GOLD",
        "split": "DEV",
        "doc_id": page["doc_id"],
        "page_number": int(page["page_number"]),
        "width": float(page["width"]),
        "height": float(page["height"]),
        "coordinate_origin": "top-left",
        "slices": sorted(set(page.get("slices", ()))),
        "status": "NOT_STARTED",
        "human_confirmed": False,
        "blocks": [],
        "table_regions": [],
        "rows": [],
        "columns": [],
        "cells": [],
        "relationships": [],
        "continuations": [],
        "audit": {
            "prediction_imported": False,
            "review_count": 0,
            "correction_count": 0,
            "ownership_disagreement_count": 0,
        },
    }


def _bbox(value: Any) -> BBox:
    return BBox.from_value(value)


def _box_inside(inner: BBox, outer: BBox, tolerance: float = 1.0) -> bool:
    return (
        inner.x0 >= outer.x0 - tolerance
        and inner.y0 >= outer.y0 - tolerance
        and inner.x1 <= outer.x1 + tolerance
        and inner.y1 <= outer.y1 + tolerance
    )


def validate_page_annotation(annotation: dict[str, Any], *, require_complete: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    if annotation.get("schema_version") != 2:
        errors.append("schema_version must be 2")
    if annotation.get("gold_type") != "REAL_LAYOUT_DEV_GOLD":
        errors.append("gold_type must be REAL_LAYOUT_DEV_GOLD")
    if annotation.get("split") != "DEV":
        errors.append("split must be DEV; FINAL_GATE is forbidden in LA-0.5")
    if annotation.get("status") not in PAGE_STATUSES:
        errors.append("invalid page status")
    if annotation.get("coordinate_origin") != "top-left":
        errors.append("coordinate_origin must be top-left")
    try:
        page_number = int(annotation["page_number"])
        width = float(annotation["width"])
        height = float(annotation["height"])
        if page_number < 1 or width <= 0 or height <= 0:
            errors.append("invalid page geometry")
    except (KeyError, TypeError, ValueError):
        page_number, width, height = 0, 0.0, 0.0
        errors.append("page_number, width and height are required")

    collections = {
        "blocks": annotation.get("blocks", []),
        "table_regions": annotation.get("table_regions", []),
        "rows": annotation.get("rows", []),
        "columns": annotation.get("columns", []),
        "cells": annotation.get("cells", []),
    }
    id_fields = {
        "blocks": "block_id",
        "table_regions": "table_region_id",
        "rows": "row_id",
        "columns": "column_id",
        "cells": "cell_id",
    }
    all_ids: set[str] = set()
    items_by_id: dict[str, dict[str, Any]] = {}
    tables_by_id: dict[str, dict[str, Any]] = {}
    for collection_name, items in collections.items():
        if not isinstance(items, list):
            errors.append(f"{collection_name} must be a list")
            continue
        id_field = id_fields[collection_name]
        for index, item in enumerate(items):
            item_id = item.get(id_field, "")
            if not item_id:
                errors.append(f"{collection_name}[{index}] missing {id_field}")
            elif item_id in all_ids:
                errors.append(f"duplicate ID: {item_id}")
            else:
                all_ids.add(item_id)
                items_by_id[item_id] = item
            if item.get("page_number", page_number) != page_number:
                errors.append(f"page mismatch: {item_id or collection_name}")
            if not isinstance(item.get("human_confirmed"), bool):
                errors.append(f"{item_id or collection_name} missing human_confirmed boolean")
            if "bbox" in item:
                try:
                    box = _bbox(item["bbox"])
                    if not _box_inside(box, BBox(0, 0, width, height), tolerance=0):
                        errors.append(f"bbox outside page: {item_id}")
                except (TypeError, ValueError):
                    errors.append(f"invalid bbox: {item_id}")
            if collection_name == "table_regions" and item_id:
                tables_by_id[item_id] = item
            if collection_name == "blocks" and item.get("block_type") not in BLOCK_TYPES:
                errors.append(f"invalid block_type: {item_id}")

    reading_orders = [item.get("reading_order") for item in collections["blocks"]]
    if any(not isinstance(order, int) or order < 0 for order in reading_orders):
        errors.append("reading_order must be a non-negative integer")
    if len(reading_orders) != len(set(reading_orders)):
        errors.append("duplicate reading_order")

    section_ids = {item.get("section_id") for item in collections["blocks"] if item.get("section_id")}
    relationship_ids: set[str] = set()
    relationship_tuples: set[tuple[str, str, str]] = set()
    ambiguous_count = 0
    ownership_count = 0
    relationships = annotation.get("relationships", [])
    if not isinstance(relationships, list):
        errors.append("relationships must be a list")
        relationships = []
    for index, relationship in enumerate(relationships):
        relationship_id = relationship.get("relationship_id", "")
        source_id = relationship.get("source_id", "")
        target_id = relationship.get("target_id", "")
        relationship_type = relationship.get("relationship_type", "")
        if not relationship_id:
            errors.append(f"relationships[{index}] missing relationship_id")
        elif relationship_id in relationship_ids:
            errors.append(f"duplicate relationship ID: {relationship_id}")
        relationship_ids.add(relationship_id)
        if relationship_type not in RELATIONSHIP_TYPES:
            errors.append(f"invalid relationship_type: {relationship_id}")
        if not isinstance(relationship.get("human_confirmed"), bool):
            errors.append(f"{relationship_id or 'relationship'} missing human_confirmed boolean")
        if relationship.get("page_number", page_number) != page_number:
            errors.append(f"page mismatch: {relationship_id}")
        known_sources = all_ids | section_ids
        if relationship_type != "AMBIGUOUS" and source_id not in known_sources:
            errors.append(f"orphan relationship source: {relationship_id}")
        if target_id not in all_ids:
            errors.append(f"orphan relationship target: {relationship_id}")
        identity = (source_id, target_id, relationship_type)
        if identity in relationship_tuples:
            errors.append(f"duplicate relationship edge: {identity}")
        relationship_tuples.add(identity)
        if relationship_type == "AMBIGUOUS":
            ambiguous_count += 1
        if relationship_type in {"HEADER_APPLIES_TO", "AMBIGUOUS"}:
            ownership_count += 1
        if relationship_type == "HEADER_APPLIES_TO" and source_id in items_by_id and target_id in items_by_id:
            source = items_by_id[source_id]
            target = items_by_id[target_id]
            source_table = source.get("table_region_id", "")
            target_table = target.get("table_region_id", "")
            if not source_table or source_table != target_table or source_table not in tables_by_id:
                errors.append(f"header/value table mismatch: {relationship_id}")
            elif "bbox" in source:
                table_box = _bbox(tables_by_id[source_table]["bbox"])
                if not _box_inside(_bbox(source["bbox"]), table_box):
                    errors.append(f"header outside table: {relationship_id}")

    row_ids = {item.get("row_id") for item in collections["rows"]}
    column_ids = {item.get("column_id") for item in collections["columns"]}
    for cell in collections["cells"]:
        if cell.get("row_id") not in row_ids:
            errors.append(f"orphan cell row: {cell.get('cell_id', '')}")
        if cell.get("column_id") not in column_ids:
            errors.append(f"orphan cell column: {cell.get('cell_id', '')}")
        table_id = cell.get("table_region_id", "")
        if table_id not in tables_by_id:
            errors.append(f"orphan cell table: {cell.get('cell_id', '')}")

    continuations = annotation.get("continuations", [])
    if not isinstance(continuations, list):
        errors.append("continuations must be a list")
    else:
        for continuation in continuations:
            if continuation.get("from_doc_id") != annotation.get("doc_id"):
                errors.append("continuation document mismatch")
            if not isinstance(continuation.get("human_confirmed"), bool):
                errors.append("continuation missing human_confirmed boolean")

    confirmed_items = [
        item
        for values in (*collections.values(), relationships, continuations)
        for item in values
    ]
    if require_complete:
        if annotation.get("status") != "REVIEWED":
            errors.append("completed Gold must have REVIEWED status")
        if annotation.get("human_confirmed") is not True:
            errors.append("completed Gold must set human_confirmed=true")
        if any(item.get("human_confirmed") is not True for item in confirmed_items):
            errors.append("all Gold items must be human_confirmed=true")

    return {
        "valid": not errors,
        "errors": sorted(set(errors)),
        "ready_for_evaluation": not errors and require_complete,
        "ambiguous_count": ambiguous_count,
        "ownership_annotation_count": ownership_count,
        "ambiguous_rate": ambiguous_count / ownership_count if ownership_count else 0.0,
    }


def _semantic_page(annotation: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "schema_version",
        "gold_type",
        "split",
        "doc_id",
        "page_number",
        "width",
        "height",
        "coordinate_origin",
        "slices",
        "status",
        "human_confirmed",
        "blocks",
        "table_regions",
        "rows",
        "columns",
        "cells",
        "relationships",
        "continuations",
    )
    return {key: annotation.get(key) for key in keys}


def canonical_gold_digest(annotations: list[dict[str, Any]]) -> str:
    pages = sorted(
        (_semantic_page(annotation) for annotation in annotations),
        key=lambda item: (item["doc_id"], item["page_number"]),
    )
    payload = json.dumps(pages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def freeze_dev_gold(annotations: list[dict[str, Any]], output: str | Path) -> dict[str, Any]:
    validations = [validate_page_annotation(annotation, require_complete=True) for annotation in annotations]
    errors = [error for validation in validations for error in validation["errors"]]
    if errors:
        raise ValueError("DEV Gold is not ready: " + "; ".join(sorted(set(errors))))
    digest = canonical_gold_digest(annotations)
    payload = {
        "status": "REAL_LAYOUT_DEV_GOLD",
        "split": "DEV",
        "final_gate_consumed": False,
        "annotation_schema_valid": True,
        "dev_gold_digest": digest,
        "pages": sorted(
            (_semantic_page(annotation) for annotation in annotations),
            key=lambda item: (item["doc_id"], item["page_number"]),
        ),
    }
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


def load_confirmed_dev_gold(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("status") != "REAL_LAYOUT_DEV_GOLD" or payload.get("split") != "DEV":
        raise ValueError("evaluator accepts only frozen REAL_LAYOUT_DEV_GOLD")
    pages = payload.get("pages", [])
    if canonical_gold_digest(pages) != payload.get("dev_gold_digest"):
        raise ValueError("DEV Gold digest mismatch")
    for page in pages:
        validation = validate_page_annotation(page, require_complete=True)
        if not validation["valid"]:
            raise ValueError("DEV Gold page is invalid")
    return pages
