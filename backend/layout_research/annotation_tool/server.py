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


class AnnotationHandler(SimpleHTTPRequestHandler):
    workspace: Path

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

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/save":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            key = self._key(parse_qs(parsed.query))
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 5_000_000:
                raise ValueError("invalid annotation payload size")
            annotation = json.loads(self.rfile.read(length).decode("utf-8"))
            page = self._read_json(f"pages/{key}.json")
            if annotation.get("doc_id") != page["doc_id"] or annotation.get("page_number") != page["page_number"]:
                raise ValueError("annotation identity does not match page")
            require_complete = annotation.get("status") == "REVIEWED"
            validation = validate_page_annotation(annotation, require_complete=require_complete)
            annotation.setdefault("audit", {})["last_saved_at"] = datetime.now(timezone.utc).isoformat()
            annotation["audit"]["validation"] = validation
            output = self.workspace / "annotations" / f"{key}.json"
            temporary = output.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(annotation, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(temporary, output)
            self._json({"saved": True, "validation": validation})
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self._json({"saved": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)

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
