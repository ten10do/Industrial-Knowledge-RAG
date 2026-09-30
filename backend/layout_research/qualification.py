"""Research-only full-corpus layout qualification.

Scans every page of a sanitized corpus and emits multi-label slice
candidates plus a LAYOUT_RISK_SCORE used ONLY for page discovery and
DEV sampling.  These signals are never Gold and never feed production
parsers.  Checkpointed so multi-session runs resume deterministically.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .annotation_workspace import (
    CONTINUATION_PATTERN,
    HEADING_PATTERN,
    MODEL_PATTERN,
    TABLE_TERM_PATTERN,
    UNIT_PATTERN,
    _aligned_column_count,
    _line_segments,
)

SLICE_LABELS = (
    "ordinary_text",
    "simple_table",
    "borderless_table",
    "model_comparison",
    "multi_column",
    "merged_header",
    "cross_page",
    "mixed_layout",
    "unit_column",
    "section_table",
    "parameter_code_table",
    "fault_code_table",
)

RISK_WEIGHTS = {
    "multi_column": 3,
    "merged_header": 3,
    "cross_page": 3,
    "mixed_layout": 2,
    "model_comparison": 2,
    "unit_column": 2,
    "parameter_code_table": 2,
    "fault_code_table": 2,
    "borderless_table": 2,
    "section_table": 2,
    "simple_table": 1,
    "ordinary_text": 0,
}

# Parameter code families observed across drive / PLC / servo manuals:
# dotted group.index (ABB 96.01), letter-prefixed codes (Rockwell A055,
# b003, P038; Mitsubishi Pa03 / No.12), bracketed indices, and generic
# "Pr"/"No." prefixes.
PARAMETER_CODE_PATTERNS = (
    re.compile(r"\b\d{1,3}\.\d{2}\b"),
    re.compile(r"\b(?:Pr|No|Pa|Pb|Pn)[.\-]?\d{1,4}\b", re.I),
    re.compile(r"\b[A-Za-z]\d{3}\b"),
)
FAULT_CODE_PATTERNS = (
    re.compile(r"\bF\d{2,4}\b"),
    re.compile(r"\bEr(?:-|\s?)?\d{1,4}\b", re.I),
    re.compile(r"\b(?:fault|alarm)(?:\s+code)?\s*[:#]?\s*[A-Za-z]?\d+", re.I),
    re.compile(r"\b[A-Z]{2,6}\d{2,5}\b"),
)
FAULT_KEYWORD_PATTERN = re.compile(r"\bfault\b|\balarm\b|\berror\b|故障|报警", re.I)


def _regex_density(patterns: tuple[re.Pattern[str], ...], text: str) -> int:
    matches: set[str] = set()
    for pattern in patterns:
        matches.update(match.group(0) for match in pattern.finditer(text))
    return len(matches)


def _font_profile(page: Any) -> dict[str, float]:
    chars = page.chars[:2000]
    if not chars:
        return {"distinct_fonts": 0.0, "median_size": 0.0, "size_variance": 0.0}
    sizes = sorted(round(float(char.get("size", 0.0)), 1) for char in chars)
    names = {str(char.get("fontname", "")) for char in chars if char.get("fontname")}
    median_size = sizes[len(sizes) // 2]
    variance = sum((size - median_size) ** 2 for size in sizes) / len(sizes)
    return {
        "distinct_fonts": float(len(names)),
        "median_size": float(median_size),
        "size_variance": round(variance, 2),
    }


def _merged_header_present(table_hints: list[Any]) -> bool:
    for table in table_hints:
        try:
            extracted = table.extract() or []
        except Exception:  # noqa: BLE001
            extracted = []
        row_counts = [
            sum(bool(cell and str(cell).strip()) for cell in row) for row in extracted[:4]
        ]
        if len(row_counts) >= 2 and min(row_counts) < max(row_counts) and max(row_counts) >= 3:
            return True
    return False


def qualify_page(page: Any, doc_id: str, page_number: int, previous_table_count: int) -> dict[str, Any]:
    text = page.extract_text() or ""
    words = page.extract_words(use_text_flow=False)
    lines = _line_segments(words)
    aligned_columns = _aligned_column_count(lines)
    numeric_lines = sum(
        any(character.isdigit() for segment in line for character in segment["text"])
        for line in lines
    )
    text_layer = bool(text.strip())
    table_hints: list[Any] = []
    if text_layer and (aligned_columns >= 2 or numeric_lines >= 3 or TABLE_TERM_PATTERN.search(text)):
        try:
            table_hints = list(page.find_tables())
        except Exception:  # noqa: BLE001
            table_hints = []
    table_count = len(table_hints)
    tableish = table_count > 0 or aligned_columns >= 2

    slices: set[str] = set()
    evidence: list[str] = []
    if not text_layer:
        slices.add("ordinary_text")
        evidence.append("no_text_layer")
    elif not tableish:
        slices.add("ordinary_text")
    else:
        slices.add("simple_table")
        evidence.append("table_geometry")
        if aligned_columns >= 3:
            slices.add("multi_column")
            evidence.append("three_or_more_aligned_columns")
        if table_count == 0:
            slices.add("borderless_table")
            evidence.append("aligned_columns_without_detected_rulings")
        if sum(len(line) == 1 for line in lines) >= 3:
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
    elif table_count and previous_table_count:
        slices.add("cross_page")
        evidence.append("consecutive_table_pages")
    if tableish and _merged_header_present(table_hints):
        slices.add("merged_header")
        evidence.append("unequal_header_row_cell_counts")
    parameter_hits = _regex_density(PARAMETER_CODE_PATTERNS, text)
    if tableish and parameter_hits >= 6:
        slices.add("parameter_code_table")
        evidence.append(f"parameter_code_density:{parameter_hits}")
    fault_hits = _regex_density(FAULT_CODE_PATTERNS, text)
    if tableish and fault_hits >= 3 and FAULT_KEYWORD_PATTERN.search(text):
        slices.add("fault_code_table")
        evidence.append(f"fault_code_density:{fault_hits}")

    if not slices:
        slices.add("ordinary_text")
    risk_score = sum(RISK_WEIGHTS[slice_name] for slice_name in slices)
    if aligned_columns >= 5:
        risk_score += 1
    if numeric_lines >= 10:
        risk_score += 1
    font_profile = _font_profile(page) if text_layer else {"distinct_fonts": 0.0, "median_size": 0.0, "size_variance": 0.0}
    return {
        "doc_id": doc_id,
        "page_number": page_number,
        "width": float(page.width),
        "height": float(page.height),
        "slices": sorted(slices),
        "layout_risk_score": risk_score,
        "discovery_evidence": evidence,
        "aligned_column_count": aligned_columns,
        "numeric_line_count": numeric_lines,
        "detected_table_count": table_count,
        "text_layer_available": text_layer,
        "font_profile": font_profile,
        "parameter_code_hits": parameter_hits,
        "fault_code_hits": fault_hits,
    }


def _lock_path(output_dir: Path) -> Path:
    return output_dir / "qualification.lock"


def _acquire_lock(output_dir: Path) -> None:
    lock = _lock_path(output_dir)
    if lock.exists():
        try:
            pid = int(lock.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            pid = 0
        if pid and pid != os.getpid():
            try:
                os.kill(pid, 0)
                raise RuntimeError(f"qualification already running (pid {pid}); SINGLE_INSTANCE_LOCK_HELD")
            except OSError:
                pass  # stale lock from a dead process
    lock.write_text(str(os.getpid()), encoding="utf-8")


def _release_lock(output_dir: Path) -> None:
    lock = _lock_path(output_dir)
    if lock.exists():
        try:
            lock.unlink()
        except OSError:
            # Unlink can fail under safe-delete shims; leave the lock.  Stale
            # PIDs are detected and superseded by _acquire_lock on next run.
            try:
                lock.write_text("0", encoding="utf-8")
            except OSError:
                pass


def run_full_qualification(
    source_map_path: str | Path,
    corpus_root: str | Path,
    output_dir: str | Path,
    *,
    max_pages_per_document: int | None = None,
) -> dict[str, Any]:
    """Scan every page of every sanitized document; resumable via checkpoint."""
    import pdfplumber

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    _acquire_lock(output)
    started = time.time()
    try:
        mapping = json.loads(Path(source_map_path).read_text(encoding="utf-8"))
        root = Path(corpus_root)
        pages_path = output / "pages.jsonl"
        checkpoint_path = output / "checkpoint.json"
        checkpoint: dict[str, Any] = {}
        if checkpoint_path.exists():
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        mode = "a" if checkpoint.get("resume_token") == pages_path.name else "w"
        if mode == "w":
            pages_path.write_text("", encoding="utf-8")
        checkpoint["resume_token"] = pages_path.name

        slice_totals: Counter[str] = Counter()
        doc_summaries: dict[str, dict[str, Any]] = {}
        scanned_pages = 0
        with pages_path.open(mode, encoding="utf-8") as sink:
            for doc_id in sorted(mapping):
                entry = mapping[doc_id]
                source = root / entry["rel_path"]
                doc_state = checkpoint.setdefault(doc_id, {"next_page": 1, "pages": 0})
                if doc_state["pages"] and doc_state["next_page"] > doc_state["pages"]:
                    scanned_pages += doc_state["pages"]
                    for slice_name, count in doc_state.get("slice_counts", {}).items():
                        slice_totals[slice_name] += count
                    continue
                doc_slices: Counter[str] = Counter()
                previous_table_count = 0
                with pdfplumber.open(str(source)) as pdf:
                    total = len(pdf.pages)
                    if max_pages_per_document is not None:
                        total = min(total, max_pages_per_document)
                    doc_state["pages"] = total
                    start_at = doc_state["next_page"]
                    for page_number in range(start_at, total + 1):
                        page = pdf.pages[page_number - 1]
                        record = qualify_page(page, doc_id, page_number, previous_table_count)
                        previous_table_count = record["detected_table_count"]
                        sink.write(json.dumps(record, ensure_ascii=False) + "\n")
                        for slice_name in record["slices"]:
                            doc_slices[slice_name] += 1
                        if page_number % 200 == 0 or page_number == total:
                            sink.flush()
                            doc_state["next_page"] = page_number + 1
                            doc_state["slice_counts"] = dict(doc_slices)
                            checkpoint["scanned_pages"] = scanned_pages + page_number - start_at + 1
                            checkpoint_path.write_text(
                                json.dumps(checkpoint, indent=2, ensure_ascii=False) + "\n",
                                encoding="utf-8",
                            )
                doc_state["next_page"] = total + 1
                doc_state["slice_counts"] = dict(doc_slices)
                scanned_pages += total
                slice_totals.update(doc_slices)
                doc_summaries[doc_id] = {
                    "pages": total,
                    "slice_counts": dict(sorted(doc_slices.items())),
                    "high_risk_pages": 0,
                }
                checkpoint["scanned_pages"] = scanned_pages
                checkpoint_path.write_text(
                    json.dumps(checkpoint, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
                )

        # Second pass over pages.jsonl for high-risk counts and doc stats.
        doc_high_risk: Counter[str] = Counter()
        with pages_path.open(encoding="utf-8") as source_lines:
            for line in source_lines:
                record = json.loads(line)
                if record["layout_risk_score"] >= 4:
                    doc_high_risk[record["doc_id"]] += 1
        for doc_id, count in doc_high_risk.items():
            doc_summaries.setdefault(doc_id, {"pages": 0, "slice_counts": {}})["high_risk_pages"] = count

        summary = {
            "status": "QUALIFIED_CORPUS_READY",
            "scanned_pages": scanned_pages,
            "documents": len(mapping),
            "slice_totals": dict(sorted(slice_totals.items())),
            "high_risk_pages_total": sum(doc_high_risk.values()),
            "document_summaries": doc_summaries,
            "max_pages_per_document": max_pages_per_document,
            "discovery_only": True,
            "seconds": round(time.time() - started, 1),
        }
        (output / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return summary
    finally:
        _release_lock(output)


def canonical_digest(payload: Any) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
