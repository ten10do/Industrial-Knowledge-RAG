from __future__ import annotations

import argparse
import json
from pathlib import Path

from .annotation import freeze_dev_gold, validate_page_annotation
from .annotation_workspace import prepare_human_annotation_workspace
from .dev_sampling import allocate_final_gate_holdout, select_dev_sample
from .evaluator import apply_frozen_gates, evaluate_pages, stable_id_reproducibility
from .models import BBox, DocumentLayout, LayoutBlock, PageLayout, stable_id
from .private_workspace import prepare_private_annotation_pack
from .qualification import run_full_qualification
from .synthetic import (
    gold_page,
    load_synthetic_fixture,
    predict_synthetic_case,
    write_synthetic_fixture,
)


def _flat_synthetic(case: dict) -> PageLayout:
    inputs = case["input"]
    text = "\n".join(item["text"] for item in inputs["spans"])
    block = LayoutBlock(
        block_id=stable_id("flat", inputs["document_id"], inputs["page_number"], text),
        block_type="PARAGRAPH",
        bbox=BBox(0.0, 0.0, float(inputs["width"]), float(inputs["height"])),
        reading_order=0,
        text=text,
        page_number=int(inputs["page_number"]),
        source_span=f"page:{inputs['page_number']}",
        parser_name="FLAT_BASELINE",
        parser_version="1",
    )
    return PageLayout(
        inputs["document_id"],
        int(inputs["page_number"]),
        float(inputs["width"]),
        float(inputs["height"]),
        0,
        "top-left",
        (block,),
        parser_name="FLAT_BASELINE",
        parser_version="1",
    )


def _markdown_report(report: dict) -> str:
    lines = [
        "# Layout Ingestion Evaluation",
        "",
        f"Decision: `{report['decision']}`",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for name, value in report["metrics"].items():
        lines.append(f"| {name} | {value:.6f} |")
    lines.extend(("", "## Frozen gates", "", "| Gate | Result | Target |", "|---|---|---|"))
    for name, result in report["gates"].items():
        lines.append(f"| {name} | {result['status']} | {result['target']} |")
    lines.extend(("", "Synthetic results are not real-data authority and cannot produce GO.", ""))
    return "\n".join(lines)


def evaluate_synthetic(parser_name: str, fixture_path: str | Path) -> dict:
    cases = load_synthetic_fixture(fixture_path)
    pairs = []
    run_layouts: list[list[DocumentLayout]] = [[], [], []]
    for case in cases:
        predictions = []
        for _ in range(3):
            predicted = _flat_synthetic(case) if parser_name == "flat_baseline" else predict_synthetic_case(case)
            predictions.append(predicted)
        pairs.append((gold_page(case), predictions[0], case["slice"]))
        for index, predicted in enumerate(predictions):
            run_layouts[index].append(
                DocumentLayout(
                    document_id=predicted.document_id,
                    pages=(predicted,),
                    parser_name=predicted.parser_name,
                    parser_version=predicted.parser_version,
                    config_digest="synthetic-v1",
                )
            )
    evaluation = evaluate_pages(pairs)
    reproducible = all(
        stable_id_reproducibility([run_layouts[index][case_index] for index in range(3)]) == 1.0
        for case_index in range(len(cases))
    )
    evaluation["metrics"]["stable_id_reproducibility"] = 1.0 if reproducible else 0.0
    gate = apply_frozen_gates(evaluation["metrics"], real_human_gold=False)
    return {
        "status": "SYNTHETIC_FEASIBILITY_ONLY",
        "parser": parser_name,
        "fixture_count": len(cases),
        **evaluation,
        **gate,
    }


def _write_evaluation(report: dict, output: str | Path) -> None:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    output_path.with_suffix(".md").write_text(_markdown_report(report), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Layout-aware ingestion feasibility harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate-fixtures", help="materialize public synthetic geometry fixtures")
    generate.add_argument("--output", required=True)

    prepare = subparsers.add_parser("prepare", help="create a local-only sanitized annotation worksheet")
    prepare.add_argument("--input", required=True)
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--max-pages", type=int, default=40)

    evaluate = subparsers.add_parser("evaluate", help="run deterministic evaluation")
    evaluate.add_argument("--parser", required=True, choices=("synthetic_geometry", "flat_baseline"))
    evaluate.add_argument("--gold", required=True)
    evaluate.add_argument("--input", required=True, help="fixture input; retained separately from gold for sealed-set workflows")
    evaluate.add_argument("--output", required=True)

    compare = subparsers.add_parser("compare", help="compare existing evaluation JSON reports")
    compare.add_argument("--input", required=True, nargs="+")
    compare.add_argument("--output", required=True)

    prepare_human = subparsers.add_parser("prepare-human", help="build a private DEV annotation workspace")
    prepare_human.add_argument("--source-map", required=True)
    prepare_human.add_argument("--output", required=True)
    prepare_human.add_argument("--max-pages", type=int, default=36)
    prepare_human.add_argument("--selection-manifest", default=None)
    prepare_human.add_argument("--pages-jsonl", default=None)
    prepare_human.add_argument("--corpus-root", default=None)

    annotate = subparsers.add_parser("annotate", help="launch the local-only annotation helper")
    annotate.add_argument("--workspace", required=True)
    annotate.add_argument("--port", type=int, default=8765)
    annotate.add_argument("--no-browser", action="store_true")

    validate = subparsers.add_parser("validate-annotations", help="validate private annotation drafts")
    validate.add_argument("--workspace", required=True)
    validate.add_argument("--require-complete", action="store_true")

    freeze = subparsers.add_parser("freeze-dev-gold", help="freeze completed human-confirmed DEV Gold")
    freeze.add_argument("--workspace", required=True)
    freeze.add_argument("--output", required=True)
    freeze.add_argument("--correction-reason")

    qualify = subparsers.add_parser("qualify-corpus", help="research-only full-corpus page qualification")
    qualify.add_argument("--source-map", required=True)
    qualify.add_argument("--corpus-root", required=True)
    qualify.add_argument("--output", required=True)

    allocate = subparsers.add_parser("allocate-holdout", help="research-only document-level Final-Gate holdout plan")
    allocate.add_argument("--pages-jsonl", required=True)
    allocate.add_argument("--source-map", required=True)
    allocate.add_argument("--output", required=True)

    sample = subparsers.add_parser("sample-dev", help="research-only stratified DEV sample + manifest digest")
    sample.add_argument("--pages-jsonl", required=True)
    sample.add_argument("--holdout", required=True)
    sample.add_argument("--source-map", required=True)
    sample.add_argument("--output", required=True)
    sample.add_argument("--target-pages", type=int, default=36)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "generate-fixtures":
        write_synthetic_fixture(args.output)
        print(json.dumps({"status": "OK", "output": str(Path(args.output))}))
        return
    if args.command == "prepare":
        summary = prepare_private_annotation_pack(args.input, args.output, max_pages=args.max_pages)
        print(json.dumps(summary, ensure_ascii=False))
        return
    if args.command == "prepare-human":
        summary = prepare_human_annotation_workspace(
            args.source_map,
            args.output,
            max_pages=args.max_pages,
            selection_manifest=args.selection_manifest,
            pages_jsonl=args.pages_jsonl,
            corpus_root=args.corpus_root,
        )
        print(json.dumps(summary, ensure_ascii=False))
        return
    if args.command == "annotate":
        from .annotation_tool.server import serve

        serve(args.workspace, port=args.port, open_browser=not args.no_browser)
        return
    if args.command == "validate-annotations":
        workspace = Path(args.workspace)
        paths = sorted((workspace / "annotations").glob("*.json"))
        results = []
        for path in paths:
            annotation = json.loads(path.read_text(encoding="utf-8"))
            validation = validate_page_annotation(annotation, require_complete=args.require_complete)
            results.append({"key": path.stem, **validation})
        report = {
            "annotation_schema_valid": all(item["valid"] for item in results),
            "ready_for_evaluation": bool(results) and all(item["ready_for_evaluation"] for item in results),
            "page_count": len(results),
            "invalid_pages": [item["key"] for item in results if not item["valid"]],
            "ambiguous_count": sum(item["ambiguous_count"] for item in results),
            "pages": results,
        }
        print(json.dumps(report, ensure_ascii=False))
        if not report["annotation_schema_valid"]:
            raise SystemExit(1)
        return
    if args.command == "freeze-dev-gold":
        workspace = Path(args.workspace)
        annotations = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((workspace / "annotations").glob("*.json"))
        ]
        output = Path(args.output)
        previous = json.loads(output.read_text(encoding="utf-8")) if output.exists() else None
        payload = freeze_dev_gold(annotations, output)
        if previous and previous.get("dev_gold_digest") != payload["dev_gold_digest"]:
            if not args.correction_reason:
                output.write_text(json.dumps(previous, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                raise ValueError("Gold changed; --correction-reason is required")
            ledger_path = workspace / "audit" / "gold_corrections.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else []
            ledger.append({
                "old_digest": previous["dev_gold_digest"],
                "new_digest": payload["dev_gold_digest"],
                "reason": args.correction_reason,
            })
            ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({"status": "REAL_LAYOUT_DEV_GOLD", "dev_gold_digest": payload["dev_gold_digest"]}))
        return
    if args.command == "qualify-corpus":
        summary = run_full_qualification(args.source_map, args.corpus_root, args.output)
        print(json.dumps({key: value for key, value in summary.items() if key != "document_summaries"}, ensure_ascii=False))
        return
    if args.command == "allocate-holdout":
        from .dev_sampling import load_candidates

        mapping = json.loads(Path(args.source_map).read_text(encoding="utf-8"))
        vendors = {doc_id: entry.get("vendor", "unknown") for doc_id, entry in mapping.items()}
        plan = allocate_final_gate_holdout(load_candidates(args.pages_jsonl), vendors)
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(plan, ensure_ascii=False))
        return
    if args.command == "sample-dev":
        from .dev_sampling import load_candidates

        mapping = json.loads(Path(args.source_map).read_text(encoding="utf-8"))
        vendors = {doc_id: entry.get("vendor", "unknown") for doc_id, entry in mapping.items()}
        holdout = json.loads(Path(args.holdout).read_text(encoding="utf-8"))
        plan = select_dev_sample(
            load_candidates(args.pages_jsonl), holdout, vendors, target_pages=args.target_pages
        )
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({key: value for key, value in plan.items() if key != "pages"}, ensure_ascii=False))
        return
    if args.command == "evaluate":
        if Path(args.gold).resolve() != Path(args.input).resolve():
            gold_payload = json.loads(Path(args.gold).read_text(encoding="utf-8"))
            input_payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
            if [item["case_id"] for item in gold_payload["cases"]] != [item["case_id"] for item in input_payload["cases"]]:
                raise ValueError("gold and input case identities differ")
        report = evaluate_synthetic(args.parser, args.gold)
        _write_evaluation(report, args.output)
        print(json.dumps({"status": "OK", "decision": report["decision"], "output": str(Path(args.output))}))
        return
    reports = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.input]
    comparison = {
        "reports": [
            {"parser": report["parser"], "decision": report["decision"], "metrics": report["metrics"]}
            for report in reports
        ]
    }
    Path(args.output).write_text(json.dumps(comparison, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": "OK", "output": str(Path(args.output))}))
