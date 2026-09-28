from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Iterable

from .geometry import intersection_over_union
from .models import DocumentLayout, LayoutBlock, PageLayout, TableCell, to_dict


UNIT_HEADER_PATTERN = re.compile(
    r"^(?:%|a|bar|degc|hz|in|kg|kw|lb|m|ma|mm|ms|nm|rpm|s|v|vac|vdc|w)$",
    re.I,
)
MODEL_HEADER_PATTERN = re.compile(r"^(?=.*[a-z])(?=.*\d)[a-z0-9][a-z0-9+./_-]{1,31}$", re.I)
PARAMETER_CODE_PATTERN = re.compile(r"^[a-z]\d{2,4}$", re.I)
OWNERSHIP_DIAGNOSTIC_TYPES = (
    "UNIT",
    "MODEL_COLUMN",
    "MERGED_HEADER",
    "ROW_HEADER",
    "CROSS_PAGE",
    "MULTI_LEVEL_HEADER",
    "COLUMN_HEADER",
    "OTHER",
)
OWNERSHIP_MISBIND_TYPES = (
    "OVER_BROAD_COLUMN_HEADER",
    "WRONG_PARENT_HEADER",
    "MERGED_HEADER_OVER_PROPAGATION",
    "UNIT_HEADER_MISBIND",
    "MODEL_COLUMN_MISBIND",
    "ROW_HEADER_MISBIND",
    "CROSS_COLUMN_MISBIND",
    "CROSS_PAGE_CONTEXT_LEAK",
    "MULTI_LEVEL_HEADER_ERROR",
    "SPANNING_HEADER_ERROR",
    "PARAMETER_CODE_MISBIND",
    "FAULT_CODE_CAUSE_ACTION_MISBIND",
    "DUPLICATE_HEADER_AMBIGUITY",
    "GEOMETRY_OVERLAP_AMBIGUITY",
    "OTHER",
)


FROZEN_GATES = {
    "block_type_accuracy": (">=", 0.90),
    "table_region_recall": (">=", 0.90),
    "section_attribution_accuracy": (">=", 0.95),
    "header_value_ownership_precision": (">=", 0.95),
    "false_merge_rate": ("<=", 0.02),
    "page_provenance_accuracy": ("==", 1.00),
    "stable_id_reproducibility": ("==", 1.00),
    "parser_fatal_failure_rate": ("<=", 0.01),
}


@dataclass(frozen=True)
class Match:
    gold_index: int
    predicted_index: int
    score: float


def _safe_div(numerator: int | float, denominator: int | float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _match_boxes(gold: list, predicted: list, threshold: float = 0.5) -> list[Match]:
    candidates = []
    for gold_index, gold_item in enumerate(gold):
        for predicted_index, predicted_item in enumerate(predicted):
            score = intersection_over_union(gold_item.bbox, predicted_item.bbox)
            if score >= threshold:
                candidates.append(Match(gold_index, predicted_index, score))
    matches = []
    used_gold = set()
    used_predicted = set()
    for candidate in sorted(candidates, key=lambda item: (-item.score, item.gold_index, item.predicted_index)):
        if candidate.gold_index in used_gold or candidate.predicted_index in used_predicted:
            continue
        matches.append(candidate)
        used_gold.add(candidate.gold_index)
        used_predicted.add(candidate.predicted_index)
    return matches


def _reading_order_accuracy(gold: list[LayoutBlock], predicted: list[LayoutBlock], matches: list[Match]) -> float:
    pairs = 0
    correct = 0
    for left_index, left in enumerate(matches):
        for right in matches[left_index + 1 :]:
            gold_order = gold[left.gold_index].reading_order < gold[right.gold_index].reading_order
            predicted_order = predicted[left.predicted_index].reading_order < predicted[right.predicted_index].reading_order
            pairs += 1
            correct += gold_order == predicted_order
    return _safe_div(correct, pairs) if pairs else (1.0 if matches else 0.0)


def _ownership_edges(page: PageLayout) -> set[tuple[str, str]]:
    return {
        (edge.header_block_id, edge.value_block_id)
        for edge in page.relationships
        if edge.relationship_type == "HEADER_APPLIES_TO"
    }


def _cell_by_id(page: PageLayout) -> dict[str, TableCell]:
    return {cell.cell_id: cell for cell in page.cells}


def _normalized_header_text(text: str) -> str:
    return re.sub(r"[°\s()]", "", text).casefold()


def _is_unit_header(text: str) -> bool:
    return bool(UNIT_HEADER_PATTERN.fullmatch(_normalized_header_text(text)))


def _is_model_header(text: str) -> bool:
    normalized = _normalized_header_text(text)
    return bool(MODEL_HEADER_PATTERN.fullmatch(normalized)) and not bool(
        PARAMETER_CODE_PATTERN.fullmatch(normalized)
    )


def _ownership_axes(
    source: TableCell,
    target: TableCell,
    *,
    incoming_count: int,
    page_number: int,
) -> set[str]:
    axes: set[str] = set()
    if source.page_number != target.page_number or source.page_number != page_number:
        return {"CROSS_PAGE"}
    if _is_unit_header(source.text):
        axes.add("UNIT")
    if _is_model_header(source.text):
        axes.add("MODEL_COLUMN")
    if source.row_id == target.row_id and source.bbox.x0 <= target.bbox.x0:
        axes.add("ROW_HEADER")
    if source.bbox.y1 <= target.bbox.y0 + 1 and source.bbox.width > target.bbox.width * 1.5:
        axes.add("MERGED_HEADER")
    if incoming_count > 1 and source.bbox.y1 <= target.bbox.y0 + 1:
        axes.add("MULTI_LEVEL_HEADER")
    if source.column_id == target.column_id and source.bbox.y1 <= target.bbox.y1:
        axes.add("COLUMN_HEADER")
    return axes or {"OTHER"}


def _misbind_type(
    source: TableCell,
    target: TableCell,
    *,
    incoming_count: int,
    source_fanout: int,
    duplicate_header_text: bool,
) -> str:
    normalized = _normalized_header_text(source.text)
    if source.page_number != target.page_number:
        return "CROSS_PAGE_CONTEXT_LEAK"
    if _is_unit_header(source.text):
        return "UNIT_HEADER_MISBIND"
    if PARAMETER_CODE_PATTERN.fullmatch(normalized):
        return "PARAMETER_CODE_MISBIND"
    if re.search(r"fault|cause|action", source.text, re.I):
        return "FAULT_CODE_CAUSE_ACTION_MISBIND"
    if _is_model_header(source.text):
        return "MODEL_COLUMN_MISBIND"
    if duplicate_header_text:
        return "DUPLICATE_HEADER_AMBIGUITY"
    if source.row_id == target.row_id and source.bbox.x0 <= target.bbox.x0:
        return "ROW_HEADER_MISBIND"
    if source.bbox.y1 <= target.bbox.y0 + 1 and source.bbox.width > target.bbox.width * 1.5:
        if source_fanout > 12 or source.bbox.width > target.bbox.width * 4:
            return "OVER_BROAD_COLUMN_HEADER"
        return "MERGED_HEADER_OVER_PROPAGATION"
    if incoming_count > 1 and source.bbox.y1 <= target.bbox.y0 + 1:
        return "MULTI_LEVEL_HEADER_ERROR"
    if source.column_id == target.column_id:
        return "WRONG_PARENT_HEADER"
    if not source.bbox.overlaps_x(target.bbox, tolerance=1.0):
        return "CROSS_COLUMN_MISBIND"
    if source.bbox.y1 <= target.bbox.y0 + 1:
        return "SPANNING_HEADER_ERROR"
    if source.bbox.overlaps_x(target.bbox, tolerance=2.0):
        return "GEOMETRY_OVERLAP_AMBIGUITY"
    return "OTHER"


def _new_ownership_diagnostics() -> dict:
    return {
        "counts": {
            key: {"gold": 0, "predicted": 0, "matched": 0, "matched_gold": 0, "false": 0}
            for key in OWNERSHIP_DIAGNOSTIC_TYPES
        },
        "misbind_taxonomy": {key: 0 for key in OWNERSHIP_MISBIND_TYPES},
        "misbind_examples": {key: [] for key in OWNERSHIP_MISBIND_TYPES},
    }


def _ownership_diagnostics(
    gold: PageLayout,
    predicted: PageLayout,
    mapping: dict[str, str],
) -> dict:
    diagnostics = _new_ownership_diagnostics()
    gold_cells = _cell_by_id(gold)
    predicted_cells = _cell_by_id(predicted)
    gold_edges = _ownership_edges(gold)
    predicted_relationships = [
        edge for edge in predicted.relationships
        if edge.relationship_type == "HEADER_APPLIES_TO"
        and edge.header_block_id in mapping
        and edge.value_block_id in mapping
        and edge.header_block_id in predicted_cells
        and edge.value_block_id in predicted_cells
    ]
    predicted_by_identity = {
        (mapping[edge.header_block_id], mapping[edge.value_block_id]): edge
        for edge in predicted_relationships
    }
    predicted_incoming: dict[str, int] = {}
    source_fanout: dict[str, int] = {}
    for edge in predicted_by_identity.values():
        predicted_incoming[edge.value_block_id] = predicted_incoming.get(edge.value_block_id, 0) + 1
        source_fanout[edge.header_block_id] = source_fanout.get(edge.header_block_id, 0) + 1
    text_counts: dict[tuple[str, str], int] = {}
    for cell in predicted.cells:
        key = (next((region.table_region_id for region in predicted.table_regions if cell.cell_id in region.cell_ids), ""), _normalized_header_text(cell.text))
        text_counts[key] = text_counts.get(key, 0) + 1

    gold_incoming: dict[str, int] = {}
    for _, target_id in gold_edges:
        gold_incoming[target_id] = gold_incoming.get(target_id, 0) + 1
    for source_id, target_id in gold_edges:
        source = gold_cells.get(source_id)
        target = gold_cells.get(target_id)
        if not source or not target:
            continue
        axes = _ownership_axes(
            source, target, incoming_count=gold_incoming.get(target_id, 0), page_number=gold.page_number
        )
        for axis in axes:
            diagnostics["counts"][axis]["gold"] += 1
            if (source_id, target_id) in predicted_by_identity:
                diagnostics["counts"][axis]["matched_gold"] += 1

    for identity, edge in predicted_by_identity.items():
        source = predicted_cells[edge.header_block_id]
        target = predicted_cells[edge.value_block_id]
        axes = _ownership_axes(
            source,
            target,
            incoming_count=predicted_incoming.get(edge.value_block_id, 0),
            page_number=predicted.page_number,
        )
        for axis in axes:
            diagnostics["counts"][axis]["predicted"] += 1
        if identity in gold_edges:
            for axis in axes:
                diagnostics["counts"][axis]["matched"] += 1
            continue
        for axis in axes:
            diagnostics["counts"][axis]["false"] += 1
        table_id = edge.table_region_id
        duplicate = text_counts.get((table_id, _normalized_header_text(source.text)), 0) > 1
        error_type = _misbind_type(
            source,
            target,
            incoming_count=predicted_incoming.get(edge.value_block_id, 0),
            source_fanout=source_fanout.get(edge.header_block_id, 0),
            duplicate_header_text=duplicate,
        )
        diagnostics["misbind_taxonomy"][error_type] += 1
        examples = diagnostics["misbind_examples"][error_type]
        if len(examples) < 5:
            examples.append({
                "document_id": predicted.document_id,
                "page_number": predicted.page_number,
                "header_id": edge.header_block_id,
                "value_id": edge.value_block_id,
            })
    return diagnostics


def _merge_ownership_diagnostics(total: dict, page: dict) -> None:
    for axis in OWNERSHIP_DIAGNOSTIC_TYPES:
        for key in total["counts"][axis]:
            total["counts"][axis][key] += page["counts"][axis][key]
    for error_type in OWNERSHIP_MISBIND_TYPES:
        total["misbind_taxonomy"][error_type] += page["misbind_taxonomy"][error_type]
        remaining = 5 - len(total["misbind_examples"][error_type])
        if remaining > 0:
            total["misbind_examples"][error_type].extend(page["misbind_examples"][error_type][:remaining])


def _finalize_ownership_diagnostics(diagnostics: dict) -> dict:
    metrics = {}
    for axis, counts in diagnostics["counts"].items():
        label = axis.casefold()
        metrics[f"{label}_ownership_precision"] = _safe_div(counts["matched"], counts["predicted"])
        metrics[f"{label}_ownership_recall"] = _safe_div(counts["matched_gold"], counts["gold"])
    diagnostics["metrics"] = metrics
    return diagnostics


def _cell_mapping(gold: PageLayout, predicted: PageLayout) -> tuple[dict[str, str], list[Match]]:
    matches = _match_boxes(list(gold.cells), list(predicted.cells), threshold=0.5)
    mapping = {
        predicted.cells[item.predicted_index].cell_id: gold.cells[item.gold_index].cell_id
        for item in matches
    }
    return mapping, matches


def _group_error_counts(gold: PageLayout, predicted: PageLayout, mapping: dict[str, str]) -> tuple[int, int, int, int, int]:
    gold_cell_by_id = {cell.cell_id: cell for cell in gold.cells}
    row_merges = 0
    column_merges = 0
    for group in predicted.rows:
        mapped = [gold_cell_by_id[mapping[cell_id]] for cell_id in group.cell_ids if cell_id in mapping]
        row_merges += len({cell.row_id for cell in mapped}) > 1
    for group in predicted.columns:
        mapped = [gold_cell_by_id[mapping[cell_id]] for cell_id in group.cell_ids if cell_id in mapping]
        column_merges += len({cell.column_id for cell in mapped}) > 1

    reverse_mapping = {gold_id: predicted_id for predicted_id, gold_id in mapping.items()}
    predicted_cell_by_id = {cell.cell_id: cell for cell in predicted.cells}
    row_splits = 0
    for group in gold.rows:
        predicted_cells = [predicted_cell_by_id[reverse_mapping[cell_id]] for cell_id in group.cell_ids if cell_id in reverse_mapping]
        row_splits += len({cell.row_id for cell in predicted_cells}) > 1
    column_splits = 0
    for group in gold.columns:
        predicted_cells = [predicted_cell_by_id[reverse_mapping[cell_id]] for cell_id in group.cell_ids if cell_id in reverse_mapping]
        column_splits += len({cell.column_id for cell in predicted_cells}) > 1
    return row_merges, column_merges, row_splits, column_splits, len(predicted.rows) + len(predicted.columns)


def _provenance_counts(page: PageLayout) -> tuple[int, int]:
    total = 0
    correct = 0
    items = list(page.blocks) + list(page.table_regions) + list(page.cells)
    for item in items:
        total += 1
        box = item.bbox
        valid_box = 0 <= box.x0 <= box.x1 <= page.width and 0 <= box.y0 <= box.y1 <= page.height
        correct += item.page_number == page.page_number and valid_box
    for relationship in page.relationships:
        total += 1
        correct += relationship.page_number == page.page_number
    return correct, total


def evaluate_pages(pairs: Iterable[tuple[PageLayout, PageLayout, str]]) -> dict:
    counts = {
        "gold_blocks": 0,
        "block_type_correct": 0,
        "gold_tables": 0,
        "predicted_tables": 0,
        "matched_tables": 0,
        "section_total": 0,
        "section_correct": 0,
        "reading_order_sum": 0.0,
        "reading_order_pages": 0,
        "gold_ownership": 0,
        "predicted_ownership": 0,
        "matched_ownership": 0,
        "false_merges": 0,
        "predicted_groups": 0,
        "provenance_correct": 0,
        "provenance_total": 0,
        "pages": 0,
        "fatal_failures": 0,
    }
    slice_pairs: dict[str, list[tuple[PageLayout, PageLayout, str]]] = {}
    error_taxonomy = {key: 0 for key in (
        "TABLE_MISSED",
        "NON_TABLE_FALSE_POSITIVE",
        "ROW_SPLIT",
        "ROW_MERGE",
        "COLUMN_SPLIT",
        "COLUMN_MERGE",
        "HEADER_MISBIND",
        "UNIT_MISBIND",
        "MODEL_COLUMN_MISBIND",
        "SECTION_MISATTRIBUTION",
        "READING_ORDER_ERROR",
        "PAGE_PROVENANCE_ERROR",
        "AMBIGUOUS_CASE",
    )}
    ownership_diagnostics = _new_ownership_diagnostics()
    materialized = list(pairs)
    for gold, predicted, slice_name in materialized:
        slice_pairs.setdefault(slice_name, []).append((gold, predicted, slice_name))
        counts["pages"] += 1
        counts["fatal_failures"] += bool(predicted.failure)
        block_matches = _match_boxes(list(gold.blocks), list(predicted.blocks))
        counts["gold_blocks"] += len(gold.blocks)
        counts["block_type_correct"] += sum(
            gold.blocks[item.gold_index].block_type == predicted.blocks[item.predicted_index].block_type
            for item in block_matches
        )
        section_matches = [item for item in block_matches if gold.blocks[item.gold_index].section_title]
        counts["section_total"] += sum(bool(block.section_title) for block in gold.blocks)
        section_correct = sum(
            gold.blocks[item.gold_index].section_title == predicted.blocks[item.predicted_index].section_title
            for item in section_matches
        )
        counts["section_correct"] += section_correct
        error_taxonomy["SECTION_MISATTRIBUTION"] += len(section_matches) - section_correct
        order_accuracy = _reading_order_accuracy(list(gold.blocks), list(predicted.blocks), block_matches)
        counts["reading_order_sum"] += order_accuracy
        counts["reading_order_pages"] += 1
        error_taxonomy["READING_ORDER_ERROR"] += order_accuracy < 1.0

        table_matches = _match_boxes(list(gold.table_regions), list(predicted.table_regions))
        counts["gold_tables"] += len(gold.table_regions)
        counts["predicted_tables"] += len(predicted.table_regions)
        counts["matched_tables"] += len(table_matches)
        error_taxonomy["TABLE_MISSED"] += len(gold.table_regions) - len(table_matches)
        error_taxonomy["NON_TABLE_FALSE_POSITIVE"] += len(predicted.table_regions) - len(table_matches)

        mapping, _ = _cell_mapping(gold, predicted)
        gold_edges = _ownership_edges(gold)
        predicted_edges = {
            (mapping[header], mapping[value])
            for header, value in _ownership_edges(predicted)
            if header in mapping and value in mapping
        }
        matched_edges = gold_edges & predicted_edges
        counts["gold_ownership"] += len(gold_edges)
        counts["predicted_ownership"] += len(predicted_edges)
        counts["matched_ownership"] += len(matched_edges)
        error_taxonomy["HEADER_MISBIND"] += len(predicted_edges - gold_edges)
        error_taxonomy["AMBIGUOUS_CASE"] += sum(edge.relationship_type == "AMBIGUOUS" for edge in gold.relationships)
        _merge_ownership_diagnostics(
            ownership_diagnostics,
            _ownership_diagnostics(gold, predicted, mapping),
        )

        row_merges, column_merges, row_splits, column_splits, predicted_groups = _group_error_counts(gold, predicted, mapping)
        counts["false_merges"] += row_merges + column_merges
        counts["predicted_groups"] += predicted_groups
        error_taxonomy["ROW_MERGE"] += row_merges
        error_taxonomy["COLUMN_MERGE"] += column_merges
        error_taxonomy["ROW_SPLIT"] += row_splits
        error_taxonomy["COLUMN_SPLIT"] += column_splits
        provenance_correct, provenance_total = _provenance_counts(predicted)
        counts["provenance_correct"] += provenance_correct
        counts["provenance_total"] += provenance_total
        error_taxonomy["PAGE_PROVENANCE_ERROR"] += provenance_total - provenance_correct

    table_precision = _safe_div(counts["matched_tables"], counts["predicted_tables"])
    table_recall = _safe_div(counts["matched_tables"], counts["gold_tables"])
    ownership_precision = _safe_div(counts["matched_ownership"], counts["predicted_ownership"])
    ownership_recall = _safe_div(counts["matched_ownership"], counts["gold_ownership"])
    metrics = {
        "block_type_accuracy": _safe_div(counts["block_type_correct"], counts["gold_blocks"]),
        "table_region_precision": table_precision,
        "table_region_recall": table_recall,
        "table_region_f1": _f1(table_precision, table_recall),
        "section_attribution_accuracy": _safe_div(counts["section_correct"], counts["section_total"]),
        "reading_order_accuracy": _safe_div(counts["reading_order_sum"], counts["reading_order_pages"]),
        "header_value_ownership_precision": ownership_precision,
        "header_value_ownership_recall": ownership_recall,
        "header_value_ownership_f1": _f1(ownership_precision, ownership_recall),
        "false_ownership_rate": _safe_div(
            counts["predicted_ownership"] - counts["matched_ownership"], counts["predicted_ownership"]
        ),
        "false_merge_rate": _safe_div(counts["false_merges"], counts["predicted_groups"]),
        "page_provenance_accuracy": _safe_div(counts["provenance_correct"], counts["provenance_total"]),
        "parser_fatal_failure_rate": _safe_div(counts["fatal_failures"], counts["pages"]),
    }
    slices = {}
    if len(slice_pairs) > 1:
        for slice_name, items in sorted(slice_pairs.items()):
            slices[slice_name] = evaluate_pages(items)["metrics"]
    ownership_diagnostics = _finalize_ownership_diagnostics(ownership_diagnostics)
    error_taxonomy["UNIT_MISBIND"] = ownership_diagnostics["misbind_taxonomy"]["UNIT_HEADER_MISBIND"]
    error_taxonomy["MODEL_COLUMN_MISBIND"] = ownership_diagnostics["misbind_taxonomy"]["MODEL_COLUMN_MISBIND"]
    return {
        "metrics": metrics,
        "counts": counts,
        "slices": slices,
        "error_taxonomy": error_taxonomy,
        "ownership_diagnostics": ownership_diagnostics,
    }


def layout_digest(layout: DocumentLayout) -> str:
    payload = json.dumps(to_dict(layout), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_id_reproducibility(layouts: list[DocumentLayout]) -> float:
    if not layouts:
        return 0.0
    digests = [layout_digest(layout) for layout in layouts]
    return 1.0 if len(set(digests)) == 1 else 0.0


def apply_frozen_gates(metrics: dict[str, float], *, real_human_gold: bool) -> dict:
    results = {}
    for metric, (operator, target) in FROZEN_GATES.items():
        value = metrics.get(metric)
        if value is None:
            results[metric] = {"status": "NOT_EVALUATED", "target": f"{operator} {target:.2f}"}
            continue
        passed = value >= target if operator == ">=" else value <= target if operator == "<=" else value == target
        results[metric] = {"status": "PASS" if passed else "FAIL", "value": value, "target": f"{operator} {target:.2f}"}
    all_passed = all(item["status"] == "PASS" for item in results.values())
    if not real_human_gold:
        decision = "LAYOUT_ANNOTATION_REQUIRED"
    elif all_passed:
        decision = "LAYOUT_INGESTION_FEASIBILITY_GO"
    else:
        decision = "LAYOUT_INGESTION_FEASIBILITY_PARTIAL"
    return {"decision": decision, "gates": results}
