from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Iterable

from .geometry import intersection_over_union
from .models import DocumentLayout, LayoutBlock, PageLayout, TableCell, to_dict


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
    return {"metrics": metrics, "counts": counts, "slices": slices, "error_taxonomy": error_taxonomy}


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
