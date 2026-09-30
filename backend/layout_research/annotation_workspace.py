from __future__ import annotations

from collections import Counter, defaultdict
import json
import logging
from pathlib import Path
import re
from typing import Any

from .annotation import new_page_annotation, validate_page_annotation
from .models import PageLayout, to_dict
from .parsers import FlatBaselineParser, PdfPlumberTableParser, PypdfGeometryParser


TARGETS = {
    "ordinary_text": 5,
    "simple_table": 5,
    "borderless_table": 5,
    "model_comparison": 7,
    "multi_column": 7,
    "merged_header": 5,
    "cross_page": 4,
    "mixed_layout": 5,
    "section_table": 5,
    "unit_column": 5,
}
HIGH_RISK_SLICES = {
    "simple_table",
    "borderless_table",
    "model_comparison",
    "multi_column",
    "merged_header",
    "cross_page",
    "mixed_layout",
    "section_table",
    "unit_column",
}
UNIT_PATTERN = re.compile(r"\b(?:unit|units|v|a|kw|w|hz|rpm|bar|psi|mm|cm|kg|ms|°c)\b|单位|伏|安|赫兹|毫米|千克", re.I)
MODEL_PATTERN = re.compile(r"\b(?:model|type)\s*[:#-]?\s*[a-z0-9]|型号|机型", re.I)
CONTINUATION_PATTERN = re.compile(r"\bcontinued\b|\bcont\.?\b|续表|接上页", re.I)
TABLE_TERM_PATTERN = re.compile(r"\b(?:table|parameter|specification|minimum|maximum)\b|表\s*\d|参数|规格|最小|最大", re.I)
HEADING_PATTERN = re.compile(r"^(?:\d+(?:\.\d+)*|[A-Z])(?:[.)]?\s+)\S")
INDUSTRIAL_SIGNAL_PATTERNS = {
    "electrical": re.compile(r"\b(?:rated voltage|rated current|power supply|input voltage|output current|frequency range)\b|\b\d+(?:\.\d+)?\s*(?:v|a|hz|kw)\b|额定电压|额定电流|供电电源|输入电压|输出电流", re.I),
    "mechanical": re.compile(r"\b(?:torque|rpm|operating temperature|pressure range|flow rate|load capacity)\b|扭矩|转速|工作温度|压力范围|流量|负载能力", re.I),
    "automation": re.compile(r"\b(?:plc|servo drive|frequency converter|inverter|industrial controller|fault code|alarm code|proximity sensor)\b|可编程控制器|伺服驱动|变频器|工业控制器|故障代码|报警代码|接近传感器", re.I),
    "manual": re.compile(r"\b(?:technical specifications?|installation instructions?|wiring diagram|operation manual|safety instructions?|maintenance schedule)\b|技术规格|技术参数|安装说明|接线图|操作手册|安全说明|维护周期", re.I),
    "equipment": re.compile(r"\b(?:electric motor|industrial pump|control valve|compressor|industrial robot|switchgear|actuator|drive system)\b|电动机|工业泵|控制阀|压缩机|工业机器人|开关柜|执行器|驱动系统", re.I),
}
NON_MANUAL_PROFILE_PATTERN = re.compile(r"\b(?:github|portfolio|education|professional skills|project experience|internship)\b|教育经历|专业技能|项目经历|实习经历|个人信息", re.I)


def _line_segments(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    lines: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda item: (round(float(item["top"]), 1), float(item["x0"]))):
        center = (float(word["top"]) + float(word["bottom"])) / 2
        for line in reversed(lines[-3:]):
            line_center = sum((float(item["top"]) + float(item["bottom"])) / 2 for item in line) / len(line)
            if abs(center - line_center) <= 3.0:
                line.append(word)
                break
        else:
            lines.append([word])
    segmented = []
    for line in lines:
        line = sorted(line, key=lambda item: float(item["x0"]))
        segments: list[list[dict[str, Any]]] = []
        for word in line:
            if not segments or float(word["x0"]) - float(segments[-1][-1]["x1"]) > 14.0:
                segments.append([word])
            else:
                segments[-1].append(word)
        segmented.append(
            [
                {
                    "x0": min(float(item["x0"]) for item in segment),
                    "x1": max(float(item["x1"]) for item in segment),
                    "text": " ".join(str(item["text"]) for item in segment),
                }
                for segment in segments
            ]
        )
    return segmented


def _aligned_column_count(lines: list[list[dict[str, Any]]]) -> int:
    anchors: list[list[tuple[int, float]]] = []
    for line_number, line in enumerate(lines):
        if len(line) < 2:
            continue
        for segment in line:
            x0 = segment["x0"]
            for cluster in anchors:
                center = sum(item[1] for item in cluster) / len(cluster)
                if abs(center - x0) <= 16.0:
                    cluster.append((line_number, x0))
                    break
            else:
                anchors.append([(line_number, x0)])
    return sum(1 for cluster in anchors if len({item[0] for item in cluster}) >= 3)


def _discover_page(page, doc_id: str, page_number: int) -> dict[str, Any]:
    text = page.extract_text() or ""
    words = page.extract_words(use_text_flow=False)
    lines = _line_segments(words)
    aligned_columns = _aligned_column_count(lines)
    numeric_lines = sum(any(character.isdigit() for segment in line for character in segment["text"]) for line in lines)
    table_hints = []
    if aligned_columns >= 2 or numeric_lines >= 3 or TABLE_TERM_PATTERN.search(text):
        try:
            table_hints = page.find_tables()
        except Exception:
            table_hints = []
    table_count = len(table_hints)
    tableish = table_count > 0 or aligned_columns >= 2
    slices: set[str] = set()
    evidence: list[str] = []
    if not tableish:
        slices.add("ordinary_text")
    else:
        slices.add("simple_table")
        evidence.append("table_geometry")
        if aligned_columns >= 3:
            slices.add("multi_column")
            evidence.append("three_or_more_aligned_columns")
        if aligned_columns >= 2 and table_count == 0:
            slices.add("borderless_table")
            evidence.append("aligned_columns_without_detected_rulings")
        non_table_lines = sum(len(line) == 1 for line in lines)
        if non_table_lines >= 3:
            slices.add("mixed_layout")
            evidence.append("table_plus_independent_text_lines")
        if any(HEADING_PATTERN.match(line.strip()) for line in text.splitlines()[:30]):
            slices.add("section_table")
            evidence.append("numbered_heading_near_table")
    if tableish and MODEL_PATTERN.search(text):
        slices.add("model_comparison")
        evidence.append("model_language_with_table_geometry")
    if tableish and UNIT_PATTERN.search(text):
        slices.add("unit_column")
        evidence.append("unit_language_with_table_geometry")
    if CONTINUATION_PATTERN.search(text):
        slices.add("cross_page")
        evidence.append("explicit_continuation_marker")
    merged_header = False
    for table in table_hints:
        try:
            extracted = table.extract() or []
        except Exception:
            extracted = []
        row_counts = [sum(bool(cell and str(cell).strip()) for cell in row) for row in extracted[:4]]
        if len(row_counts) >= 2 and min(row_counts) < max(row_counts) and max(row_counts) >= 3:
            merged_header = True
            break
    if merged_header:
        slices.add("merged_header")
        evidence.append("unequal_header_row_cell_counts")
    risk_score = sum(3 if item in {"multi_column", "merged_header", "cross_page"} else 2 for item in slices & HIGH_RISK_SLICES)
    industrial_signals = sorted(name for name, pattern in INDUSTRIAL_SIGNAL_PATTERNS.items() if pattern.search(text))
    return {
        "doc_id": doc_id,
        "page_number": page_number,
        "width": float(page.width),
        "height": float(page.height),
        "slices": sorted(slices),
        "discovery_evidence": evidence,
        "risk_score": risk_score,
        "aligned_column_count": aligned_columns,
        "detected_table_count": table_count,
        "text_layer_available": bool(text.strip()),
        "industrial_signals": industrial_signals,
        "non_manual_profile_signal": bool(NON_MANUAL_PROFILE_PATTERN.search(text)),
    }


def discover_private_pages(source_map_path: str | Path, *, max_pages_per_document: int = 80) -> tuple[list[dict], list[dict]]:
    import pdfplumber

    source_payload = json.loads(Path(source_map_path).read_text(encoding="utf-8"))
    pages = []
    private_sources = []
    for document in source_payload.get("documents", []):
        doc_id = document["doc_id"]
        source_path = document["source_path"]
        try:
            with pdfplumber.open(source_path) as pdf:
                document_pages = []
                for page_number, page in enumerate(pdf.pages[: min(len(pdf.pages), max_pages_per_document)], start=1):
                    try:
                        document_pages.append(_discover_page(page, doc_id, page_number))
                    except Exception:
                        continue
                document_signals = {signal for item in document_pages for signal in item["industrial_signals"]}
                has_manual_authority = "manual" in document_signals and bool(
                    document_signals & {"electrical", "mechanical", "automation", "equipment"}
                )
                if not has_manual_authority or any(item["non_manual_profile_signal"] for item in document_pages):
                    continue
                private_sources.append({"doc_id": doc_id, "source_path": source_path, "source_name": document.get("source_name", "")})
                for index, current in enumerate(document_pages):
                    if "cross_page" in current["slices"]:
                        continue
                    previous = document_pages[index - 1] if index else None
                    if previous and previous["detected_table_count"] and current["detected_table_count"]:
                        current["slices"] = sorted(set(current["slices"]) | {"cross_page"})
                        current["discovery_evidence"].append("consecutive_table_pages")
                        current["risk_score"] += 3
                pages.extend(document_pages)
        except Exception:
            continue
    return pages, private_sources


def select_dev_pages(candidates: list[dict], *, max_pages: int = 36) -> tuple[list[dict], dict[str, int], list[str]]:
    selected: list[dict] = []
    selected_keys: set[tuple[str, int]] = set()
    coverage = Counter()
    structural = [item for item in candidates if set(item["slices"]) & HIGH_RISK_SLICES]
    while len(selected) < max_pages:
        ranked = []
        for item in structural:
            key = (item["doc_id"], item["page_number"])
            if key in selected_keys:
                continue
            deficit_gain = sum(max(0, TARGETS[slice_name] - coverage[slice_name]) for slice_name in item["slices"] if slice_name in TARGETS)
            new_slice_gain = sum(coverage[slice_name] == 0 for slice_name in item["slices"] if slice_name in TARGETS)
            ranked.append((deficit_gain, new_slice_gain, item["risk_score"], -item["page_number"], item))
        if not ranked:
            break
        best = max(ranked, key=lambda row: row[:4])
        if best[0] <= 0 and len(selected) >= 30:
            break
        item = best[4]
        selected.append(item)
        selected_keys.add((item["doc_id"], item["page_number"]))
        coverage.update(item["slices"])

    ordinary = [item for item in candidates if item["slices"] == ["ordinary_text"]]
    ordinary_by_doc: dict[str, list[dict]] = defaultdict(list)
    for item in ordinary:
        ordinary_by_doc[item["doc_id"]].append(item)
    for doc_id in sorted(ordinary_by_doc):
        if coverage["ordinary_text"] >= TARGETS["ordinary_text"] or len(selected) >= max_pages:
            break
        item = ordinary_by_doc[doc_id][0]
        key = (item["doc_id"], item["page_number"])
        if key not in selected_keys:
            selected.append(item)
            selected_keys.add(key)
            coverage.update(item["slices"])

    if len(selected) < min(30, max_pages):
        remainder = sorted(
            (item for item in structural if (item["doc_id"], item["page_number"]) not in selected_keys),
            key=lambda item: (-item["risk_score"], item["doc_id"], item["page_number"]),
        )
        for item in remainder:
            if len(selected) >= min(30, max_pages):
                break
            selected.append(item)
            selected_keys.add((item["doc_id"], item["page_number"]))
            coverage.update(item["slices"])

    selected.sort(key=lambda item: (item["doc_id"], item["page_number"]))
    availability = {slice_name for item in candidates for slice_name in item["slices"]}
    missing = sorted(slice_name for slice_name in TARGETS if slice_name != "ordinary_text" and slice_name not in availability)
    return selected, dict(sorted(coverage.items())), missing


def _prediction_page(page: PageLayout, parser_key: str) -> dict[str, Any]:
    payload = to_dict(page)
    cell_to_table = {
        cell_id: region["table_region_id"]
        for region in payload["table_regions"]
        for cell_id in region["cell_ids"]
    }
    for collection in ("blocks", "table_regions", "rows", "columns", "cells"):
        for item in payload[collection]:
            item["human_confirmed"] = False
            if collection == "cells":
                item["table_region_id"] = cell_to_table.get(item["cell_id"], "")
    relationships = [
        {
            "relationship_id": item["relationship_id"],
            "source_id": item["header_block_id"],
            "target_id": item["value_block_id"],
            "relationship_type": item["relationship_type"],
            "page_number": item["page_number"],
            "table_region_id": item.get("table_region_id", ""),
            "confidence": item.get("confidence", 1.0),
            "human_confirmed": False,
        }
        for item in payload["relationships"]
    ]
    return {
        "prediction_only": True,
        "human_confirmed": False,
        "parser_key": parser_key,
        "doc_id": payload["document_id"],
        "page_number": payload["page_number"],
        "width": payload["width"],
        "height": payload["height"],
        "coordinate_origin": "top-left",
        "blocks": payload["blocks"],
        "table_regions": payload["table_regions"],
        "rows": payload["rows"],
        "columns": payload["columns"],
        "cells": payload["cells"],
        "relationships": relationships,
        "failure": payload.get("failure", ""),
    }


def _render_preview(source_path: str, page_number: int, output: Path) -> None:
    import pypdfium2

    pdf = pypdfium2.PdfDocument(source_path)
    try:
        page = pdf[page_number - 1]
        try:
            image = page.render(scale=1.5).to_pil()
            image.save(output, format="PNG", optimize=True)
        finally:
            page.close()
    finally:
        pdf.close()


def _load_precomputed_selection(
    selection_manifest: str | Path,
    pages_jsonl: str | Path,
    source_map_path: str | Path,
    corpus_root: str | Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, int], list[str]]:
    """Research-only path: build the workspace from a frozen DEV sample plan."""
    plan = json.loads(Path(selection_manifest).read_text(encoding="utf-8"))
    mapping = json.loads(Path(source_map_path).read_text(encoding="utf-8"))
    corpus = Path(corpus_root)
    records_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    for line in Path(pages_jsonl).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        records_by_key[(record["doc_id"], record["page_number"])] = record

    private_sources = []
    for doc_id in plan["dev_documents"]:
        entry = mapping[doc_id]
        private_sources.append(
            {
                "doc_id": doc_id,
                "source_path": str((corpus / entry["rel_path"]).resolve()),
                "source_name": Path(entry["rel_path"]).name,
                "vendor": entry.get("vendor", "unknown"),
            }
        )

    selected: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, int]] = set()
    coverage: Counter[str] = Counter()

    def _add(record: dict[str, Any], reason: str, priority: int, base: dict[str, Any]) -> None:
        key = (record["doc_id"], record["page_number"])
        if key in selected_keys:
            return
        selected_keys.add(key)
        selected.append(
            {
                "doc_id": record["doc_id"],
                "page_number": record["page_number"],
                "width": record["width"],
                "height": record["height"],
                "slices": record["slices"],
                "layout_risk_score": record["layout_risk_score"],
                "selection_reason": reason,
                "review_priority": priority,
                **base,
            }
        )
        coverage.update(record["slices"])

    for entry in plan["pages"]:
        record = records_by_key[(entry["sanitized_doc_id"], entry["page_number"])]
        _add(record, entry["selection_reason"], entry["review_priority"], {"dev_sample_id": entry["sample_id"]})
    # Cross-page continuation units: page N + page N+1 annotate as one unit.
    for unit in plan.get("continuation_units", []):
        anchor_doc, anchor_page = unit["anchor"].rsplit(":p", 1)
        continuation_key = (anchor_doc, int(unit["continuation"].rsplit(":p", 1)[1]))
        record = records_by_key.get(continuation_key)
        if record is not None and record["detected_table_count"]:
            _add(record, "cross_page_continuation_unit", 8, {"dev_sample_id": "", "anchor_unit": unit["anchor"]})

    selected.sort(key=lambda item: (item["doc_id"], item["page_number"]))
    distribution = dict(sorted(coverage.items()))
    available = {slice_name for record in records_by_key.values() for slice_name in record["slices"]}
    missing = sorted(
        slice_name for slice_name in TARGETS if slice_name != "ordinary_text" and slice_name not in available
    )
    return [], private_sources, selected, distribution, missing


def prepare_human_annotation_workspace(
    source_map_path: str | Path,
    output_root: str | Path,
    *,
    max_pages: int = 36,
    selection_manifest: str | Path | None = None,
    pages_jsonl: str | Path | None = None,
    corpus_root: str | Path | None = None,
) -> dict[str, Any]:
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    output = Path(output_root).resolve()
    for directory in ("pages", "previews", "predictions", "annotations", "exports", "audit"):
        (output / directory).mkdir(parents=True, exist_ok=True)
    if selection_manifest:
        if pages_jsonl is None or corpus_root is None:
            raise ValueError("selection manifest mode requires --pages-jsonl and --corpus-root")
        candidates, private_sources, selected, distribution, missing = _load_precomputed_selection(
            selection_manifest, pages_jsonl, source_map_path, corpus_root
        )
    else:
        candidates, private_sources = discover_private_pages(source_map_path)
        selected, distribution, missing = select_dev_pages(candidates, max_pages=max_pages)
        selected = [dict(item, selection_reason="greedy_coverage", review_priority=0) for item in selected]
    source_by_doc = {item["doc_id"]: item for item in private_sources}
    selected_by_doc: dict[str, set[int]] = defaultdict(set)
    for page in selected:
        selected_by_doc[page["doc_id"]].add(page["page_number"])

    parser_classes = {
        "flat_baseline": FlatBaselineParser,
        "suggestion_1": PypdfGeometryParser,
        "suggestion_2": PdfPlumberTableParser,
    }
    predictions: dict[tuple[str, int], dict[str, Any]] = defaultdict(dict)
    parser_failures = Counter()
    for doc_id, page_numbers in selected_by_doc.items():
        source_path = source_by_doc[doc_id]["source_path"]
        for parser_key, parser_class in parser_classes.items():
            try:
                layout = parser_class().parse(source_path, doc_id)
                page_by_number = {page.page_number: page for page in layout.pages}
                for page_number in page_numbers:
                    page_layout = page_by_number.get(page_number)
                    if page_layout:
                        predictions[(doc_id, page_number)][parser_key] = _prediction_page(page_layout, parser_key)
            except Exception as error:
                parser_failures[parser_key] += 1
                for page_number in page_numbers:
                    predictions[(doc_id, page_number)][parser_key] = {
                        "prediction_only": True,
                        "human_confirmed": False,
                        "parser_key": parser_key,
                        "doc_id": doc_id,
                        "page_number": page_number,
                        "failure": type(error).__name__,
                        "blocks": [],
                        "table_regions": [],
                        "rows": [],
                        "columns": [],
                        "cells": [],
                        "relationships": [],
                    }

    manifest_pages = []
    ownership_suggestions = 0
    for page in selected:
        key = f"{page['doc_id']}_p{page['page_number']:04d}"
        source_path = source_by_doc[page["doc_id"]]["source_path"]
        preview_path = output / "previews" / f"{key}.png"
        if not preview_path.exists():
            _render_preview(source_path, page["page_number"], preview_path)
        prediction_payload = {
            "prediction_only": True,
            "human_confirmed": False,
            "predictions_hidden_by_default": True,
            "options": predictions[(page["doc_id"], page["page_number"])],
        }
        (output / "predictions" / f"{key}.json").write_text(
            json.dumps(prediction_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        ownership_suggestions += max(
            (
                sum(edge.get("relationship_type") == "HEADER_APPLIES_TO" for edge in option.get("relationships", []))
                for option in prediction_payload["options"].values()
            ),
            default=0,
        )
        page_metadata = {
            "key": key,
            "doc_id": page["doc_id"],
            "page_number": page["page_number"],
            "width": page["width"],
            "height": page["height"],
            "slices": page["slices"],
            "layout_risk_score": page.get("layout_risk_score", 0),
            "selection_reason": page.get("selection_reason", ""),
            "review_priority": page.get("review_priority", 0),
            "preview": f"previews/{key}.png",
            "prediction": f"predictions/{key}.json",
            "annotation": f"annotations/{key}.json",
        }
        (output / "pages" / f"{key}.json").write_text(
            json.dumps(page_metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        annotation_path = output / "annotations" / f"{key}.json"
        if not annotation_path.exists():
            annotation = new_page_annotation(page_metadata)
            validation = validate_page_annotation(annotation)
            annotation["audit"]["validation"] = validation
            annotation_path.write_text(json.dumps(annotation, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        manifest_pages.append(page_metadata)

    manifest = {
        "status": "HUMAN_ANNOTATION_ACTION_REQUIRED",
        "gold_type": "REAL_LAYOUT_DEV_GOLD",
        "split": "DEV",
        "final_gate_consumed": False,
        "predictions_hidden_by_default": True,
        "prediction_labels": {
            "flat_baseline": "Flat reference",
            "suggestion_1": "Suggestion 1",
            "suggestion_2": "Suggestion 2",
        },
        "pages": manifest_pages,
        "slice_distribution": distribution,
        "missing_real_slices": missing,
    }
    (output / "workspace_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (output / "audit" / "private_source_map.json").write_text(
        json.dumps({"private": True, "documents": private_sources}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (output / "audit" / "selection_audit.json").write_text(
        json.dumps({"candidates": candidates, "selected_keys": [page["key"] for page in manifest_pages]}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    summary = {
        "status": "HUMAN_ANNOTATION_ACTION_REQUIRED",
        "sampled_pages": len(selected),
        "sampled_documents": len(selected_by_doc),
        "slice_distribution": distribution,
        "missing_real_slices": missing,
        "pages_requiring_manual_review": len(selected),
        "ownership_suggestions_requiring_confirmation": ownership_suggestions,
        "prediction_overlays_available": bool(selected) and all(bool(predictions[key]) for key in predictions),
        "predictions_hidden_by_default": True,
        "parser_document_failures": dict(parser_failures),
        "human_gold_completed": False,
        "dev_gold_consumed": False,
        "final_gate_consumed": False,
    }
    (output / "audit" / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return summary
