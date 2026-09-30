from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import threading
import webbrowser
from urllib.parse import parse_qs, urlparse

from ..annotation import validate_page_annotation


STATIC_ROOT = Path(__file__).resolve().parent / "static"
PAGE_KEY = re.compile(r"^doc_[0-9]{3}_p[0-9]{4}$")

# Gold authority guard: accepting table structure (region, rows, columns,
# membership) must never authorize header→value ownership.  Ownership enters
# Gold only through explicit owner actions on relationships or bundles.
# Hardcoded here and asserted in backend/test_layout_authority.py so that
# regressions fail tests instead of contaminating Gold silently.
STRUCTURE_ACCEPTANCE_CANNOT_AUTHORIZE_OWNERSHIP = True

TABLE_STRUCTURE_STATUSES = ("UNREVIEWED", "ACCEPTED", "CORRECTED")
OWNERSHIP_STATUSES = ("UNREVIEWED", "ACCEPTED", "REJECTED", "AMBIGUOUS")

BULK_OPERATIONS = {
    "accept_table_structure",
    "accept_low_risk_ownership",
    "accept_ownership_bundle",
    "reject_ownership_bundle",
    "ambiguous_ownership_bundle",
    "restore_ownership_state",
}
OWNERSHIP_BUNDLE_OPERATIONS = {
    "accept_ownership_bundle",
    "reject_ownership_bundle",
    "ambiguous_ownership_bundle",
}


class AnnotationHandler(SimpleHTTPRequestHandler):
    workspace: Path

    # ------------------------------------------------------------------ utils
    def _json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _key(self, query: dict[str, list[str]]) -> str:
        key = query.get("key", [""])[0]
        if not PAGE_KEY.fullmatch(key):
            raise ValueError("invalid sanitized page key")
        return key

    def _read_json(self, relative: str) -> dict:
        return json.loads((self.workspace / relative).read_text(encoding="utf-8"))

    def _review_data(self) -> dict:
        path = self.workspace / "audit" / "review_risk.json"
        if not path.exists():
            return {"available": False}
        return json.loads(path.read_text(encoding="utf-8"))

    def _page_risk(self, key: str, review: dict | None = None) -> dict:
        review = review if review is not None else self._review_data()
        if not review.get("available", True):
            return {}
        return review.get("page_risk", {}).get(key, {})

    def _append_ui_operations(self, key: str, annotation: dict) -> None:
        operations = (annotation.get("audit") or {}).get("operations") or []
        if not operations:
            return
        log_path = self.workspace / "audit" / "ui_operations.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            stamp = datetime.now(timezone.utc).isoformat()
            for operation in operations:
                handle.write(
                    json.dumps(
                        {"page_key": key, "recorded_at": stamp, **operation},
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    # ------------------------------------------------------------------- GET
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        try:
            if parsed.path == "/api/manifest":
                manifest = self._read_json("workspace_manifest.json")
                progress = self._progress(manifest)
                self._json({**manifest, "progress": progress})
                return
            if parsed.path == "/api/page":
                key = self._key(query)
                page = self._read_json(f"pages/{key}.json")
                annotation = self._read_json(f"annotations/{key}.json")
                prediction = self._read_json(f"predictions/{key}.json")
                self._json({"page": page, "annotation": annotation, "prediction": prediction})
                return
            if parsed.path == "/api/validate":
                key = self._key(query)
                annotation = self._read_json(f"annotations/{key}.json")
                self._json(validate_page_annotation(annotation, require_complete=annotation.get("status") == "REVIEWED"))
                return
            if parsed.path == "/api/review-data":
                review = self._review_data()
                if review.get("available", True):
                    compact = {
                        "available": True,
                        "prediction_only": review.get("prediction_only", True),
                        "purpose": review.get("purpose", ""),
                        "workload": review.get("workload", {}),
                        "primary_options": review.get("primary_options", {}),
                        "second_review_queue": review.get("second_review_queue", []),
                        "page_risk": {
                            key: {
                                "counts": value["counts"],
                                "edges": value["edges"],
                                "bundles": value.get("bundles", []),
                            }
                            for key, value in review.get("page_risk", {}).items()
                        },
                        "review_queue": review.get("review_queue", []),
                    }
                    self._json(compact)
                else:
                    self._json(review)
                return
            if parsed.path == "/api/workload":
                self._json(self._workload())
                return
            if parsed.path.startswith("/workspace/previews/"):
                name = Path(parsed.path).name
                if not re.fullmatch(r"doc_[0-9]{3}_p[0-9]{4}\.png", name):
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
                self._send_file(self.workspace / "previews" / name)
                return
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/":
            self.path = "/index.html"
        self._send_file(STATIC_ROOT / self.path.lstrip("/"))

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/save":
                self._handle_save(parse_qs(parsed.query))
                return
            if parsed.path == "/api/bulk":
                self._handle_bulk(parse_qs(parsed.query))
                return
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self._json({"saved": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 5_000_000:
            raise ValueError("invalid annotation payload size")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _write_annotation(self, key: str, annotation: dict, *, owner_action: bool) -> dict:
        page = self._read_json(f"pages/{key}.json")
        if annotation.get("doc_id") != page["doc_id"] or annotation.get("page_number") != page["page_number"]:
            raise ValueError("annotation identity does not match page")
        require_complete = annotation.get("status") == "REVIEWED"
        validation = validate_page_annotation(annotation, require_complete=require_complete)
        gate = None
        if require_complete and validation["valid"]:
            gate = self._reviewed_gate(key, annotation)
            if not gate["passed"]:
                validation = dict(validation)
                validation["valid"] = False
                validation["errors"] = list(validation.get("errors", [])) + gate["violations"]
        if require_complete and not validation["valid"]:
            # Never persist a rejected REVIEWED payload: keep the owner draft.
            return {"saved": False, "validation": validation, "reviewed_gate": gate, "draft_preserved": True}
        if not owner_action and annotation.get("human_confirmed") is True and (
            json.loads((self.workspace / "annotations" / f"{key}.json").read_text(encoding="utf-8")).get("human_confirmed") is not True
        ):
            raise ValueError("human_confirmed may only be set by an explicit owner action")
        annotation.setdefault("audit", {})["last_saved_at"] = datetime.now(timezone.utc).isoformat()
        annotation["audit"]["validation"] = validation
        self._append_ui_operations(key, annotation)
        output = self.workspace / "annotations" / f"{key}.json"
        temporary = output.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(annotation, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, output)
        return {"saved": validation["valid"], "validation": validation, "reviewed_gate": gate}

    def _handle_save(self, query: dict[str, list[str]]) -> None:
        key = self._key(query)
        annotation = self._read_body()
        owner_action = bool(annotation.pop("owner_clicked", False))
        self._json(self._write_annotation(key, annotation, owner_action=owner_action))

    def _handle_bulk(self, query: dict[str, list[str]]) -> None:
        key = self._key(query)
        payload = self._read_body()
        operation = payload.get("operation", "")
        owner_clicked = bool(payload.get("owner_clicked", False))
        if operation not in BULK_OPERATIONS:
            raise ValueError("unknown bulk operation")
        if not owner_clicked:
            raise ValueError("bulk confirmation requires an explicit owner click")
        annotation = self._read_json(f"annotations/{key}.json")
        review = self._review_data()
        page_risk = self._page_risk(key, review)
        risk_by_edge = {
            edge["relationship_id"]: edge for edge in page_risk.get("edges", [])
        }
        authority = self._authority(annotation)
        affected: list[str] = []
        stamp = datetime.now(timezone.utc).isoformat()
        prior_state: dict[str, dict] = {}
        if operation == "accept_table_structure":
            if not STRUCTURE_ACCEPTANCE_CANNOT_AUTHORIZE_OWNERSHIP:
                raise RuntimeError("authority guard must be enabled")
            table_id = payload.get("table_region_id", "")
            if not table_id:
                raise ValueError("table_region_id is required")
            for item in annotation.get("table_regions", []):
                if item.get("table_region_id") == table_id and not item.get("human_confirmed"):
                    item["human_confirmed"] = True
                    affected.append(item["table_region_id"])
            for collection, id_field in (("rows", "row_id"), ("columns", "column_id")):
                # Rows/columns carry no table back-reference; only touch the
                # selected table's groups when the page has a single table.
                page_tables = [item["table_region_id"] for item in annotation.get("table_regions", [])]
                if len(page_tables) == 1 and page_tables[0] == table_id:
                    for item in annotation.get(collection, []):
                        if not item.get("human_confirmed"):
                            item["human_confirmed"] = True
                            affected.append(item[id_field])
            for block in annotation.get("blocks", []):
                if block.get("table_region_id") == table_id and not block.get("human_confirmed"):
                    block["human_confirmed"] = True
                    affected.append(block["block_id"])
            # Authority boundary: relationships and ownership_status are NOT
            # touched here.  Structure acceptance never authorizes ownership.
            status = "CORRECTED" if payload.get("corrected") else "ACCEPTED"
            authority["table_structure_status"][table_id] = status
            affected.append(f"table_structure_status:{table_id}:{status}")
        elif operation == "restore_ownership_state":
            prior_state = payload.get("prior_state", {})
            if not isinstance(prior_state, dict) or not prior_state:
                raise ValueError("restore_ownership_state requires prior_state")
            edges_by_id = {
                edge.get("relationship_id"): edge for edge in annotation.get("relationships", [])
            }
            for relationship_id, snapshot in prior_state.items():
                edge = edges_by_id.get(relationship_id)
                if edge is not None:
                    edge["human_confirmed"] = bool(snapshot.get("human_confirmed", False))
                    edge["relationship_type"] = snapshot.get("relationship_type", edge["relationship_type"])
                authority["ownership_status"][relationship_id] = snapshot.get("ownership_status", "UNREVIEWED")
                affected.append(relationship_id)
        else:
            if operation == "accept_low_risk_ownership":
                table_id = payload.get("table_region_id", "")
                for edge in annotation.get("relationships", []):
                    risk_entry = risk_by_edge.get(edge.get("relationship_id"))
                    if risk_entry is None or risk_entry["risk"] != "LOW":
                        continue
                    if table_id and edge.get("table_region_id") != table_id:
                        continue
                    if not edge.get("human_confirmed"):
                        edge["human_confirmed"] = True
                        authority["ownership_status"][edge["relationship_id"]] = "ACCEPTED"
                        affected.append(edge["relationship_id"])
            else:
                affected = self._apply_ownership_bundle(
                    annotation, authority, payload, operation, risk_by_edge, prior_state
                )
        if not affected:
            self._json({"saved": True, "affected": [], "message": "nothing to accept"})
            return
        audit = annotation.setdefault("audit", {})
        audit.setdefault("operations", []).append(
            {
                "timestamp": stamp,
                "operation": operation,
                "affected_ids": affected,
                "actor": "owner",
                "prediction_only_source": True,
            }
        )
        result = self._write_annotation(key, annotation, owner_action=True)
        if operation in OWNERSHIP_BUNDLE_OPERATIONS or operation == "restore_ownership_state":
            result["prior_state"] = prior_state
        self._json({**result, "affected": affected})

    @staticmethod
    def _authority(annotation: dict) -> dict:
        authority = annotation.get("authority")
        if not isinstance(authority, dict):
            authority = {"table_structure_status": {}, "ownership_status": {}}
        authority.setdefault("table_structure_status", {})
        authority.setdefault("ownership_status", {})
        annotation["authority"] = authority
        return authority

    def _apply_ownership_bundle(
        self,
        annotation: dict,
        authority: dict,
        payload: dict,
        operation: str,
        risk_by_edge: dict[str, dict],
        prior_state: dict[str, dict],
    ) -> list[str]:
        """Apply an explicit ownership bundle action.

        Bundle rules enforced server-side:
        - every listed relationship must exist in the annotation
        - all edges must share ONE source header (single interpretable rule)
        - all edges must share ONE risk classification (no mixing of
          merged-header/unit/model/cross-page/etc. with ordinary edges)
        - AMBIGUOUS-classified edges can never be ACCEPTED through a bundle
        """
        requested = payload.get("relationship_ids", [])
        if not isinstance(requested, list) or not requested:
            raise ValueError("relationship_ids is required for bundle operations")
        edges_by_id = {
            edge.get("relationship_id"): edge for edge in annotation.get("relationships", [])
        }
        missing = [rid for rid in requested if rid not in edges_by_id]
        if missing:
            raise ValueError(f"unknown relationship ids: {missing[:5]}")
        sources = {
            edges_by_id[rid].get("source_id", "") for rid in requested
        }
        if len(sources) != 1:
            raise ValueError("bundle edges must share a single source header")
        risks = {
            (risk_by_edge.get(rid) or {}).get("risk", "MEDIUM") for rid in requested
        }
        if len(risks) != 1:
            raise ValueError("bundle edges must share a single risk classification")
        risk = risks.pop()
        if operation == "accept_ownership_bundle" and risk == "AMBIGUOUS":
            raise ValueError(
                "AMBIGUOUS relations cannot be accepted through a bundle; resolve them individually"
            )
        affected: list[str] = []
        new_status = {
            "accept_ownership_bundle": "ACCEPTED",
            "reject_ownership_bundle": "REJECTED",
            "ambiguous_ownership_bundle": "AMBIGUOUS",
        }[operation]
        for rid in requested:
            edge = edges_by_id[rid]
            prior_state[rid] = {
                "human_confirmed": bool(edge.get("human_confirmed", False)),
                "relationship_type": edge.get("relationship_type", ""),
                "ownership_status": authority["ownership_status"].get(rid, "UNREVIEWED"),
            }
            if operation == "accept_ownership_bundle":
                edge["human_confirmed"] = True
            elif operation == "reject_ownership_bundle":
                edge["human_confirmed"] = True  # rejection confirms the edge is NOT gold
                edge["relationship_type"] = "AMBIGUOUS"
            else:  # ambiguous_ownership_bundle
                edge["relationship_type"] = "AMBIGUOUS"
                edge["human_confirmed"] = True  # confirming the ambiguity is explicit
            authority["ownership_status"][rid] = new_status
            affected.append(rid)
        return affected

    # ------------------------------------------------------- gates + dashboards
    def _reviewed_gate(self, key: str, annotation: dict) -> dict:
        """Page completion gate.

        REVIEWED requires: table structures reviewed AND required ownership
        (HIGH/AMBIGUOUS relations) resolved through explicit owner actions AND
        AMBIGUOUS explicitly handled AND page-level human confirmation.  A
        table structure acceptance alone can never complete ownership.
        """
        page_risk = self._page_risk(key)
        authority = self._authority(annotation)
        structure_status = authority["table_structure_status"]
        ownership_status = authority["ownership_status"]
        violations: list[str] = []
        if not annotation.get("human_confirmed"):
            violations.append("page human_confirmed must be set by explicit owner action")
        audit = annotation.get("audit") or {}
        if not audit.get("prediction_imported") and not audit.get("operations"):
            violations.append("no owner action recorded for this page")
        if page_risk:
            primary = page_risk.get("primary_option", "")
            option_tables: list[str] = []
            if primary:
                try:
                    prediction = self._read_json(
                        next(page["prediction"] for page in self._read_json("workspace_manifest.json")["pages"] if page["key"] == key)
                    )
                    option_tables = [
                        table["table_region_id"]
                        for table in prediction.get("options", {}).get(primary, {}).get("table_regions", [])
                    ]
                except (OSError, StopIteration, json.JSONDecodeError, KeyError):
                    option_tables = []
            confirmed_tables = {
                table["table_region_id"]
                for table in annotation.get("table_regions", [])
                if table.get("human_confirmed")
            }
            for table_id in option_tables:
                status = structure_status.get(table_id, "")
                if status in ("ACCEPTED", "CORRECTED"):
                    continue
                if not status and table_id in confirmed_tables:
                    # Legacy draft confirmed before authority split.
                    continue
                violations.append(f"table structure not reviewed: {table_id}")
            counts = page_risk.get("counts", {})
            if counts.get("HIGH", 0) + counts.get("AMBIGUOUS", 0) > 0:
                edges_by_id = {
                    edge.get("relationship_id"): edge for edge in annotation.get("relationships", [])
                }
                for edge in page_risk.get("edges", []):
                    if edge["risk"] not in ("HIGH", "AMBIGUOUS"):
                        continue
                    current = edges_by_id.get(edge["relationship_id"])
                    if current is None:
                        continue  # owner deleted the suggestion: resolved
                    if current.get("relationship_type") == "AMBIGUOUS":
                        continue  # explicitly marked ambiguous
                    if not current.get("human_confirmed"):
                        violations.append(f"unresolved {edge['risk']} relation {edge['relationship_id']}")
                        continue
                    if ownership_status.get(edge["relationship_id"], "UNREVIEWED") == "UNREVIEWED":
                        violations.append(
                            f"ownership not explicitly confirmed: {edge['relationship_id']}"
                        )
        return {"passed": not violations, "violations": violations}

    def _workload(self) -> dict:
        manifest = self._read_json("workspace_manifest.json")
        review = self._review_data()
        pages = manifest.get("pages", [])
        statuses = {"NOT_STARTED": 0, "IN_PROGRESS": 0, "COMPLETED": 0, "REVIEWED": 0}
        tables_total = tables_reviewed = 0
        high_remaining = ambiguous_remaining = ownership_unresolved = 0
        for page in pages:
            try:
                annotation = self._read_json(page["annotation"])
            except (OSError, json.JSONDecodeError):
                annotation = {}
            statuses[annotation.get("status", "NOT_STARTED")] += 1
            page_risk = self._page_risk(page["key"], review)
            if not page_risk:
                continue
            primary = page_risk.get("primary_option", "")
            try:
                prediction = self._read_json(page["prediction"])
                option = prediction.get("options", {}).get(primary, {})
            except (OSError, json.JSONDecodeError):
                option = {}
            tables_total += len(option.get("table_regions", []))
            tables_reviewed += sum(
                1
                for table in annotation.get("table_regions", [])
                if table.get("human_confirmed")
                and any(
                    predicted["table_region_id"] == table["table_region_id"]
                    for predicted in option.get("table_regions", [])
                )
            )
            annotation_edges = {
                edge.get("relationship_id"): edge for edge in annotation.get("relationships", [])
            }
            for edge in page_risk.get("edges", []):
                current = annotation_edges.get(edge["relationship_id"])
                if edge["risk"] == "AMBIGUOUS":
                    if current is None:
                        continue
                    if current.get("relationship_type") != "AMBIGUOUS" and not current.get("human_confirmed"):
                        ambiguous_remaining += 1
                elif edge["risk"] == "HIGH" and current is not None:
                    if current.get("relationship_type") != "AMBIGUOUS" and not current.get("human_confirmed"):
                        high_remaining += 1
                elif current is None or not current.get("human_confirmed"):
                    ownership_unresolved += 1
        second_review = []
        for entry in (review.get("second_review_queue", []) if review.get("available", True) else []):
            key = entry["page_key"]
            try:
                annotation = self._read_json(
                    next(page["annotation"] for page in pages if page["key"] == key)
                )
            except (OSError, StopIteration, json.JSONDecodeError):
                annotation = {}
            second_review.append(
                {
                    "page_key": key,
                    "layout_risk_score": entry.get("layout_risk_score", 0),
                    "SECOND_REVIEW_REQUIRED": True,
                    "pending": annotation.get("status") != "REVIEWED"
                    or (annotation.get("audit", {}).get("review_count", 0) or 0) < 2,
                }
            )
        return {
            "pages_reviewed": statuses["REVIEWED"],
            "pages_total": len(pages),
            "status_counts": statuses,
            "tables_reviewed": tables_reviewed,
            "tables_total": tables_total,
            "high_risk_remaining": high_remaining,
            "ambiguous_remaining": ambiguous_remaining,
            "ownership_unresolved": ownership_unresolved,
            "second_review": second_review,
            "final_gate_consumed": False,
        }

    def _progress(self, manifest: dict) -> dict:
        counts = {status: 0 for status in ("NOT_STARTED", "IN_PROGRESS", "COMPLETED", "REVIEWED")}
        for page in manifest.get("pages", []):
            try:
                annotation = self._read_json(page["annotation"])
                counts[annotation.get("status", "NOT_STARTED")] += 1
            except (OSError, json.JSONDecodeError, KeyError):
                counts["NOT_STARTED"] += 1
        return {"counts": counts, "total": len(manifest.get("pages", []))}

    def _send_file(self, path: Path) -> None:
        try:
            resolved = path.resolve(strict=True)
            allowed = resolved.is_relative_to(STATIC_ROOT) or resolved.is_relative_to(self.workspace / "previews")
            if not allowed or not resolved.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = resolved.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(resolved.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args) -> None:
        print(f"annotation-tool: {format % args}")


def serve(workspace: str | Path, *, port: int = 8765, open_browser: bool = True) -> None:
    root = Path(workspace).resolve(strict=True)
    if not (root / "workspace_manifest.json").is_file():
        raise ValueError("workspace_manifest.json is missing")
    handler = type("WorkspaceAnnotationHandler", (AnnotationHandler,), {"workspace": root})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Human annotation helper: {url}")
    print("Predictions are hidden by default and are suggestions only.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Local-only human layout annotation helper")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    serve(args.workspace, port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
