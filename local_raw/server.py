from __future__ import annotations

import json
import mimetypes
import os
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import ParseResult, quote, unquote, urlparse

import rawpy

from .engine import OUTPUT_ROOT, PRESETS, SOURCE_ROOT, RawDevelopmentError, develop_raw


HOST = "127.0.0.1"
PORT = int(os.environ.get("RAW_ASSIST_PORT", "8788"))
ALLOWED_ORIGINS = {"http://localhost:3000", "http://127.0.0.1:3000"}
MAX_JSON_BODY_BYTES = 64 * 1024


class RawAssistHandler(BaseHTTPRequestHandler):
    server_version = "GeoFrameRawAssist/1.0"

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")

    def _json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path) -> None:
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self._cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                self.wfile.write(chunk)

    def _read_json_body(self) -> dict:
        """Read and parse the JSON request body.

        Raises RawDevelopmentError for an unusable Content-Length, ValueError for a
        non-numeric one, and json.JSONDecodeError for malformed JSON. All three are
        mapped to HTTP 400 by the POST handlers.
        """
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_JSON_BODY_BYTES:
            raise RawDevelopmentError("リクエストサイズが不正です。")
        return json.loads(self.rfile.read(length))

    def do_OPTIONS(self) -> None:
        if self.headers.get("Origin") not in ALLOWED_ORIGINS:
            self.send_response(HTTPStatus.FORBIDDEN)
            self.end_headers()
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        exact_handler = GET_EXACT.get(parsed.path)
        if exact_handler is not None:
            exact_handler(self, parsed)
            return
        for prefix, prefix_handler in GET_PREFIX:
            if parsed.path.startswith(prefix):
                prefix_handler(self, parsed)
                return
        self._json(HTTPStatus.NOT_FOUND, {"error": "未対応のURLです。"})

    def do_POST(self) -> None:
        origin = self.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:
            self._json(HTTPStatus.FORBIDDEN, {"error": "この画面からの操作は許可されていません。"})
            return
        parsed = urlparse(self.path)
        handler = POST_ROUTES.get(parsed.path)
        if handler is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "未対応のURLです。"})
            return
        handler(self, parsed)

    def log_message(self, format: str, *args) -> None:
        print(f"[raw-server] {self.address_string()} - {format % args}")


Handler = Callable[[RawAssistHandler, ParseResult], None]


def handle_health(req: RawAssistHandler, parsed: ParseResult) -> None:
    req._json(HTTPStatus.OK, {
        "status": "ready",
        "engine": "rawpy/LibRaw",
        "rawpy_version": rawpy.__version__,
        "presets": [{"name": name, "label": value["label"]} for name, value in PRESETS.items()],
    })


def handle_outputs(req: RawAssistHandler, parsed: ParseResult) -> None:
    filename = unquote(parsed.path.removeprefix("/outputs/"))
    requested_path = (OUTPUT_ROOT / filename).resolve()
    if requested_path.parent != OUTPUT_ROOT.resolve() or not requested_path.is_file():
        req._json(HTTPStatus.NOT_FOUND, {"error": "出力ファイルが見つかりません。"})
        return
    req._serve_file(requested_path)


def handle_source_jpg(req: RawAssistHandler, parsed: ParseResult) -> None:
    filename = unquote(parsed.path.removeprefix("/source-jpg/"))
    if not filename.startswith("DSC") or not filename.endswith(".JPG") or len(filename) != 12 or not filename[3:8].isdigit():
        req._json(HTTPStatus.BAD_REQUEST, {"error": "無効なJPGファイル名です。"})
        return
    requested_path = (SOURCE_ROOT / filename).resolve()
    if requested_path.parent != SOURCE_ROOT.resolve() or not requested_path.is_file():
        req._json(HTTPStatus.NOT_FOUND, {"error": "元JPGが見つかりません。"})
        return
    req._serve_file(requested_path)


def handle_raw_develop(req: RawAssistHandler, parsed: ParseResult) -> None:
    try:
        payload = req._read_json_body()
        result = develop_raw(
            str(payload.get("capture_id", "")),
            str(payload.get("preset", "neutral_recovery")),
            force=bool(payload.get("force", False)),
            max_edge=int(payload.get("max_edge", 6000)),
        )
        output_path = OUTPUT_ROOT / result["filename"]
        result["output_url"] = f"http://{HOST}:{PORT}/outputs/{quote(result['filename'])}?v={output_path.stat().st_mtime_ns}"
        req._json(HTTPStatus.OK, result)
    except (RawDevelopmentError, ValueError, TypeError, json.JSONDecodeError) as error:
        req._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
    except Exception as error:
        req._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"現像処理に失敗しました: {error}"})


GET_EXACT: dict[str, Handler] = {
    "/api/health": handle_health,
}

# Order matters: matched top-to-bottom, mirroring the original if-chain.
GET_PREFIX: list[tuple[str, Handler]] = [
    ("/outputs/", handle_outputs),
    ("/source-jpg/", handle_source_jpg),
]

POST_ROUTES: dict[str, Handler] = {
    "/api/raw-develop": handle_raw_develop,
}


def main() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), RawAssistHandler)
    print(f"RAW engine ready: http://{HOST}:{PORT} (rawpy {rawpy.__version__})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
