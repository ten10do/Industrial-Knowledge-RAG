"""Canonical graph evaluator (Phase LA-1.1, experimental).

Parallel evaluator that never replaces the frozen historical evaluator.
It scores a parser/annotation pair under the canonical ownership
contract:

- Direct Edge Precision/Recall over typed DIRECT relations.
- Context Precision/Recall over the derived context closure, reported
  per axis (MODEL / UNIT / ROW / COLUMN / MULTI_LEVEL) so that a single
  flat HEADER_APPLIES_TO F1 can no longer mask structural differences.

The frozen evaluator and all historical Final Gate semantics remain
untouched; historical gates keep using the old evaluator.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .geometry import intersection_over_union
from .models import PageLayout, TableCell
from .ownership_contract import (
    AMBIGUOUS,
    CONTEXT_AXES,
    ContextGraph,
    DerivedContext,
    map_legacy_page,
)

DIRECT_EVAL_RELATIONS = ("COLUMN_HEADER_OF", "ROW_HEADER_OF", "UNIT_OF", "MODEL_OF")


@dataclass(frozen=True)
class _Match:
    gold_index: int
    predicted_index: int
    score: float


def _safe_div(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _agreement_div(numerator: float, denominator: float) -> float:
    """Precision/Recall agreement: empty-vs-empty counts as full agreement."""
    if not denominator:
        return 1.0 if not numerator else 0.0
    return float(numerator / denominator)


def _match_cells(gold: PageLayout, predicted: PageLayout, threshold: float = 0.5) -> dict[str, str]:
    """Greedy IoU mapping predicted cell id -> gold cell id (stable order)."""
    candidates: list[_Match] = []
    for gold_index, gold_cell in enumerate(gold.cells):
        for predicted_index, predicted_cell in enumerate(predicted.cells):
            score = intersection_over_union(gold_cell.bbox, predicted_cell.bbox)
            if score >= threshold:
                candidates.append(_Match(gold_index, predicted_index, score))
    candidates.sort(key=lambda item: (-item.score, item.gold_index, item.predicted_index))
    used_gold: set[int] = set()
    used_predicted: set[int] = set()
    mapping: dict[str, str] = {}
    for candidate in candidates:
        if candidate.gold_index in used_gold or candidate.predicted_index in used_predicted:
            continue
        used_gold.add(candidate.gold_index)
        used_predicted.add(candidate.predicted_index)
        mapping[predicted.cells[candidate.predicted_index].cell_id] = gold.cells[candidate.gold_index].cell_id
    return mapping


def direct_edges(page: PageLayout) -> set[tuple[str, str, str]]:
    """Canonical direct edge set of a page: (header, value, relation).

    Accepts either canonical typed relations directly, or derives the
    direct edge set from a flat legacy page via the contract mapping.
    """
    edges: set[tuple[str, str, str]] = set()
    has_canonical = any(
        edge.relationship_type in DIRECT_EVAL_RELATIONS for edge in page.relationships
    )
    if has_canonical:
        for edge in page.relationships:
            if edge.relationship_type in DIRECT_EVAL_RELATIONS and edge.header_block_id and edge.value_block_id:
                edges.add((edge.header_block_id, edge.value_block_id, edge.relationship_type))
        return edges
    for mapping in map_legacy_page(page):
        if mapping.mapping == "DIRECT" and mapping.direct_relation in DIRECT_EVAL_RELATIONS:
            edges.add((mapping.header_id, mapping.value_id, mapping.direct_relation))
    return edges


def context_edges(contexts: dict[str, DerivedContext]) -> set[tuple[str, str, str]]:
    """Derived context closure as (axis, header, value) edges.

    AMBIGUOUS axes emit an explicit ("AXIS", AMBIGUOUS, value) marker so
    ambiguity itself is scoreable instead of silently dropped.
    """
    edges: set[tuple[str, str, str]] = set()
    for value_id, context in contexts.items():
        for axis in CONTEXT_AXES:
            members = context.axis_members(axis)
            if not members:
                continue
            if axis in context.ambiguous_axes:
                edges.add((axis, AMBIGUOUS, value_id))
                continue
            for member in members:
                edges.add((axis, member, value_id))
    return edges


def _context_counts(
    gold_edges: set[tuple[str, str, str]],
    predicted_edges: set[tuple[str, str, str]],
) -> dict[str, dict[str, float]]:
    totals: dict[str, dict[str, float]] = {
        axis: {"gold": 0.0, "predicted": 0.0, "matched": 0.0} for axis in (*CONTEXT_AXES, "ALL")
    }
    for axis, _, _ in gold_edges:
        totals[axis]["gold"] += 1
        totals["ALL"]["gold"] += 1
    for axis, _, _ in predicted_edges:
        totals[axis]["predicted"] += 1
        totals["ALL"]["predicted"] += 1
    matched = gold_edges & predicted_edges
    for axis, _, _ in matched:
        totals[axis]["matched"] += 1
        totals["ALL"]["matched"] += 1
    return totals


def evaluate_canonical_pages(pairs: Iterable[tuple[PageLayout, PageLayout]]) -> dict:
    """Evaluate canonical direct edges and derived context over page pairs.

    Both sides may be canonical (typed) or flat legacy (HEADER_APPLIES_TO)
    pages; flat pages are mapped through the ownership contract first.
    """
    totals = {
        "gold_direct": 0,
        "predicted_direct": 0,
        "matched_direct": 0,
    }
    per_relation: dict[str, dict[str, int]] = {
        relation: {"gold": 0, "predicted": 0, "matched": 0} for relation in DIRECT_EVAL_RELATIONS
    }
    ambiguous_gold = 0
    ambiguous_predicted = 0
    context_totals: dict[str, dict[str, float]] = {
        axis: {"gold": 0.0, "predicted": 0.0, "matched": 0.0} for axis in (*CONTEXT_AXES, "ALL")
    }
    pages = 0
    for gold, predicted in pairs:
        pages += 1
        mapping = _match_cells(gold, predicted)
        gold_direct = direct_edges(gold)
        predicted_direct = {
            (mapping[h], mapping[v], relation)
            for (h, v, relation) in direct_edges(predicted)
            if h in mapping and v in mapping
        }
        totals["gold_direct"] += len(gold_direct)
        totals["predicted_direct"] += len(predicted_direct)
        matched_direct = gold_direct & predicted_direct
        totals["matched_direct"] += len(matched_direct)
        for header, value, relation in gold_direct:
            per_relation[relation]["gold"] += 1
        for header, value, relation in predicted_direct:
            per_relation[relation]["predicted"] += 1
        for header, value, relation in matched_direct:
            per_relation[relation]["matched"] += 1

        gold_context = context_edges(ContextGraph(gold).derive_all(gold))
        predicted_context = context_edges(ContextGraph(predicted).derive_all(predicted))
        predicted_context_mapped = {
            (
                axis,
                mapping[header] if header != AMBIGUOUS and axis != "SECTION" else header,
                value,
            )
            for (axis, header, value) in predicted_context
            if header == AMBIGUOUS or axis == "SECTION" or (header in mapping and value in mapping)
        }
        ambiguous_gold += sum(1 for axis, header, _ in gold_context if header == AMBIGUOUS)
        ambiguous_predicted += sum(1 for axis, header, _ in predicted_context_mapped if header == AMBIGUOUS)
        page_context = _context_counts(gold_context, predicted_context_mapped)
        for axis in page_context:
            for key in ("gold", "predicted", "matched"):
                context_totals[axis][key] += page_context[axis][key]

    direct_precision = _agreement_div(totals["matched_direct"], totals["predicted_direct"])
    direct_recall = _agreement_div(totals["matched_direct"], totals["gold_direct"])
    metrics: dict[str, float] = {
        "pages": pages,
        "direct_edge_precision": direct_precision,
        "direct_edge_recall": direct_recall,
        "ambiguous_context_values_gold": ambiguous_gold,
        "ambiguous_context_values_predicted": ambiguous_predicted,
    }
    for axis, counts in context_totals.items():
        label = axis.casefold()
        metrics[f"{label}_context_precision"] = _agreement_div(counts["matched"], counts["predicted"])
        metrics[f"{label}_context_recall"] = _agreement_div(counts["matched"], counts["gold"])
    for relation, counts in per_relation.items():
        label = relation.casefold()
        metrics[f"{label}_precision"] = _agreement_div(counts["matched"], counts["predicted"])
        metrics[f"{label}_recall"] = _agreement_div(counts["matched"], counts["gold"])
    return {
        "metrics": metrics,
        "direct_totals": {
            **totals,
            "per_relation": per_relation,
        },
        "context_totals": context_totals,
    }
