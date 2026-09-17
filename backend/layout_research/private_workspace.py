from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
import re


INDUSTRIAL_TERMS = (
    "technical specification",
    "specifications",
    "installation manual",
    "operating instructions",
    "maintenance",
    "rated voltage",
    "rated current",
    "fault code",
    "parameter",
    "技术参数",
    "安装手册",
    "操作手册",
    "维护",
    "额定电压",
    "额定电流",
    "故障代码",
    "参数",
)


def _candidate_slice(text: str) -> str:
    lowered = text.casefold()
    if not text.strip():
        return "scanned_page"
    if "continued" in lowered or "续表" in text:
        return "cross_page"
    if re.search(r"\bmodel\s+[a-z0-9]", lowered) or "型号" in text:
        return "model_comparison"
    lines = [line for line in text.splitlines() if line.strip()]
    tabular = sum(bool(re.search(r"\S\s{2,}\S", line)) for line in lines)
    numeric = sum(bool(re.search(r"\d", line)) for line in lines)
    if tabular >= 3 and any(term in lowered for term in ("unit", "minimum", "maximum", "min", "max")):
        return "multi_column"
    if tabular >= 3:
        return "borderless_table"
    if numeric >= 4 and any(term in lowered for term in ("table", "parameter", "specification")):
        return "simple_table"
    if numeric >= 4 and any(term in text for term in ("表", "参数", "规格")):
        return "simple_table"
    return "ordinary_text"


def prepare_private_annotation_pack(
    input_root: str | Path,
    output_root: str | Path,
    *,
    max_pages: int = 40,
) -> dict:
    """Create a local-only sanitized worksheet; never treats predictions as gold."""

    from pypdf import PdfReader

    input_path = Path(input_root).resolve()
    output_path = Path(output_root).resolve()
    if input_path == output_path or input_path in output_path.parents:
        raise ValueError("private output must not be inside the source corpus")
    output_path.mkdir(parents=True, exist_ok=True)
    documents = []
    failures = 0
    for source in sorted(input_path.rglob("*.pdf"), key=lambda item: str(item).casefold()):
        try:
            reader = PdfReader(str(source))
            sample = "\n".join((page.extract_text() or "") for page in reader.pages[:3])
        except Exception:
            failures += 1
            continue
        score = sum(term.casefold() in sample.casefold() for term in INDUSTRIAL_TERMS)
        if score == 0:
            continue
        doc_id = f"doc_{len(documents) + 1:03d}"
        page_candidates = []
        # Candidate sampling is bounded; annotation can expand a chosen document later.
        for page_number, page in enumerate(reader.pages[: min(len(reader.pages), 20)], start=1):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            page_candidates.append(
                {
                    "doc_id": doc_id,
                    "page_number": page_number,
                    "slice_candidate": _candidate_slice(text),
                    "text_layer_available": bool(text.strip()),
                }
            )
        documents.append(
            {
                "doc_id": doc_id,
                "source_path": str(source.resolve()),
                "source_name": source.name,
                "page_count": len(reader.pages),
                "term_score": score,
                "pages": page_candidates,
            }
        )

    by_slice: dict[str, list[dict]] = defaultdict(list)
    for document in documents:
        for page in document["pages"]:
            by_slice[page["slice_candidate"]].append(page)
    target_order = (
        "simple_table",
        "borderless_table",
        "multi_column",
        "model_comparison",
        "cross_page",
        "ordinary_text",
        "scanned_page",
    )
    selected = []
    indices = Counter()
    while len(selected) < max_pages:
        progressed = False
        for slice_name in target_order:
            index = indices[slice_name]
            if index < len(by_slice[slice_name]) and len(selected) < max_pages:
                selected.append(by_slice[slice_name][index])
                indices[slice_name] += 1
                progressed = True
        if not progressed:
            break

    source_map = {
        "private": True,
        "documents": [
            {key: value for key, value in document.items() if key != "pages"}
            for document in documents
        ],
    }
    worksheet = {
        "status": "ANNOTATION_PACK_READY",
        "schema_version": 1,
        "split": "DEV",
        "human_gold_available": False,
        "pages": [
            {
                **page,
                "annotation_status": "UNANNOTATED",
                "blocks": [],
                "table_regions": [],
                "relationships": [],
                "notes": "",
            }
            for page in selected
        ],
    }
    (output_path / "private_source_map.json").write_text(
        json.dumps(source_map, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (output_path / "annotation_worksheet.json").write_text(
        json.dumps(worksheet, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    summary = {
        "status": "LAYOUT_ANNOTATION_PACK_READY",
        "pdf_files_examined": len(list(input_path.rglob("*.pdf"))),
        "candidate_documents": len(documents),
        "sampled_pages": len(selected),
        "slice_distribution": dict(sorted(Counter(page["slice_candidate"] for page in selected).items())),
        "parser_failures_during_inventory": failures,
        "human_gold_available": False,
    }
    (output_path / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return summary
