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

from . import analysis, config


HOST = "127.0.0.1"
PORT = int(os.environ.get("RAW_ASSIST_PORT", "8788"))
ALLOWED_ORIGINS = {"http://localhost:1420", "http://127.0.0.1:1420", "tauri://localhost"}
MAX_JSON_BODY_BYTES = 64 * 1024

# DNS リバインディング対策。ブラウザは攻撃者のドメインを 127.0.0.1 に向けられても
# Host ヘッダは書き換えられないため、ループバック名以外を弾けば無効化できる。
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}"}
if PORT == 80:
    ALLOWED_HOSTS |= {"127.0.0.1", "localhost", "[::1]"}

#: 静的配信を許可する拡張子（レシピJSON や .tmp を配信しないため）。
SERVABLE_SUFFIXES = {".jpg", ".webp"}

# Static roots mirror the directory layout frozen in API_CONTRACT.md §4/§9.
# They are derived locally rather than imported from engine.py so that the
# analysis and static-file endpoints keep working while Wave 1-A rewrites the
# development stack. Modules owned by Wave 1-A (engine / presets / lens) are
# imported inside the handlers that need them.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 現像元フォルダはユーザーが `POST /api/source-folder` で切り替えられるため定数にしない。
# 参照は `config.source_root()` で**リクエスト処理時に**解決する。
OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "developed"
PREVIEW_ROOT = PROJECT_ROOT / "outputs" / "preview"
MERGED_ROOT = PROJECT_ROOT / "outputs" / "merged"
# プレビュー合成は本合成と別ディレクトリ（契約 §4）。分けないと `POST /api/export` の
# `variant: "merged"` にプレビュー解像度の合成を書き出せてしまう（`bracket.py` の定数参照）。
MERGED_PREVIEW_ROOT = PROJECT_ROOT / "outputs" / "merged_preview"
THUMBS_ROOT = analysis.THUMBS_ROOT


class RawAssistHandler(BaseHTTPRequestHandler):
    server_version = "GeoFrameRawAssist/1.0"
    #: 無言で接続だけ張るクライアントにスレッドを占有されないためのソケットタイムアウト。
    #: ハンドラ全体の制限時間ではないので、10〜20秒かかる本現像には影響しない。
    timeout = 10

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")

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

    def _serve_from_root(self, root: Path, prefix: str, parsed: ParseResult, missing: str) -> None:
        """Serve a file from `root`, rejecting anything that escapes it."""
        filename = unquote(parsed.path.removeprefix(prefix))
        try:
            # `%00` などはここで ValueError/OSError になる。500 にせず 404 として扱う。
            requested_path = (root / filename).resolve()
        except (ValueError, OSError):
            self._json(HTTPStatus.NOT_FOUND, {"error": missing})
            return
        if (
            requested_path.suffix.lower() not in SERVABLE_SUFFIXES
            or requested_path.parent != root.resolve()
            or not requested_path.is_file()
        ):
            self._json(HTTPStatus.NOT_FOUND, {"error": missing})
            return
        self._serve_file(requested_path)

    def _host_allowed(self) -> bool:
        """Host ヘッダをループバック名に限定する（DNS リバインディング対策）。"""
        if self.headers.get("Host") in ALLOWED_HOSTS:
            return True
        self._json(HTTPStatus.FORBIDDEN, {"error": "許可されていないホスト名です。"})
        return False

    def _read_json_body(self) -> dict:
        """Read and parse the JSON request body.

        Raises ValueError for an unusable or non-numeric Content-Length and
        json.JSONDecodeError for malformed JSON. Both are mapped to HTTP 400 by
        the POST handlers.
        """
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_JSON_BODY_BYTES:
            raise ValueError("リクエストサイズが不正です。")
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
        if not self._host_allowed():
            return
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
        if not self._host_allowed():
            return
        # Origin は**必須**にする。`origin and ...` のように「無ければ素通し」にすると、
        # 同一マシン上の任意のプロセスが `curl`（既定で Origin を送らない）で
        # `POST /api/source-folder` を叩いて現像元フォルダを差し替えられる。
        # 画面からの操作は Tauri が `tauri://localhost`、ブラウザ開発モードが
        # `http://localhost:1420` を必ず送るので、必須化しても正規の経路は落ちない
        # （`do_OPTIONS` は元から必須で、こちらを揃えた形）。
        if self.headers.get("Origin") not in ALLOWED_ORIGINS:
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


def _fail(req: RawAssistHandler, message: str, error: Exception) -> None:
    """500 を返す。例外の詳細（内部パスを含みうる）はサーバログにだけ出す。"""
    req.log_message("%s", f"{message} ({type(error).__name__}: {error})")
    req._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": message})


def _request_payload(req: RawAssistHandler) -> dict:
    payload = req._read_json_body()
    if not isinstance(payload, dict):
        raise ValueError("リクエストの形式が不正です。")
    return payload


def handle_health(req: RawAssistHandler, parsed: ParseResult) -> None:
    try:
        from .lens import lensfun_info
        from .presets import preset_infos
    except Exception as error:
        _fail(req, "プリセット情報を読み込めません。", error)
        return
    req._json(HTTPStatus.OK, {
        "status": "ready",
        "engine": "rawpy/LibRaw",
        "rawpy_version": rawpy.__version__,
        "lensfun": lensfun_info(),
        "presets": preset_infos(),
    })


def handle_analysis(req: RawAssistHandler, parsed: ParseResult) -> None:
    req._json(HTTPStatus.OK, analysis.get_analysis_state())


def handle_analyze(req: RawAssistHandler, parsed: ParseResult) -> None:
    try:
        # Contract §0: a zero-byte or malformed Content-Length is a 400, so the
        # body is always read rather than defaulted.
        payload = _request_payload(req)
        status = analysis.start_analysis(force=bool(payload.get("force", False)))
        req._json(HTTPStatus.OK, {"status": status})
    except (ValueError, TypeError, OverflowError, json.JSONDecodeError) as error:
        req._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
    except Exception as error:
        _fail(req, "解析の開始に失敗しました。", error)


def handle_outputs(req: RawAssistHandler, parsed: ParseResult) -> None:
    req._serve_from_root(OUTPUT_ROOT, "/outputs/", parsed, "出力ファイルが見つかりません。")


def handle_preview(req: RawAssistHandler, parsed: ParseResult) -> None:
    req._serve_from_root(PREVIEW_ROOT, "/preview/", parsed, "プレビュー画像が見つかりません。")


def handle_merged(req: RawAssistHandler, parsed: ParseResult) -> None:
    req._serve_from_root(MERGED_ROOT, "/merged/", parsed, "合成画像が見つかりません。")


def handle_merged_preview(req: RawAssistHandler, parsed: ParseResult) -> None:
    req._serve_from_root(
        MERGED_PREVIEW_ROOT, "/merged-preview/", parsed, "合成プレビューが見つかりません。"
    )


def handle_thumbs(req: RawAssistHandler, parsed: ParseResult) -> None:
    req._serve_from_root(THUMBS_ROOT, "/thumbs/", parsed, "サムネイルが見つかりません。")


def handle_source_jpg(req: RawAssistHandler, parsed: ParseResult) -> None:
    filename = unquote(parsed.path.removeprefix("/source-jpg/"))
    # isdecimal() は全角・Unicode 数字を弾く（isdigit() は "²" 等を通してしまう）。
    if not filename.startswith("DSC") or not filename.endswith(".JPG") or len(filename) != 12 or not filename[3:8].isdecimal():
        req._json(HTTPStatus.BAD_REQUEST, {"error": "無効なJPGファイル名です。"})
        return
    source_root = config.source_root().resolve()
    requested_path = (source_root / filename).resolve()
    if requested_path.parent != source_root or not requested_path.is_file():
        req._json(HTTPStatus.NOT_FOUND, {"error": "元JPGが見つかりません。"})
        return
    req._serve_file(requested_path)


def handle_source_folder(req: RawAssistHandler, parsed: ParseResult) -> None:
    """`GET /api/source-folder` → `SourceFolderInfo`（契約 §1）。"""
    try:
        req._json(HTTPStatus.OK, config.source_folder_info())
    except Exception as error:
        _fail(req, "現像元フォルダの情報を取得できません。", error)


def handle_set_source_folder(req: RawAssistHandler, parsed: ParseResult) -> None:
    """`POST /api/source-folder` → `SourceFolderInfo`（保存できたときのみ 200）。

    検証・保存の判断は全て `config` 側にあり、ここは例外を HTTP に写すだけに保つ
    （HTTP レイヤはユニットテストで直接叩けないため、分岐を持たせない）。
    """
    try:
        payload = _request_payload(req)
        root = config.set_source_root(
            payload.get("path"), analysis_running=analysis.is_analysis_running()
        )
        req._json(HTTPStatus.OK, config.source_folder_info(root))
    except config.SourceFolderError as error:
        req._json(HTTPStatus.BAD_REQUEST, {"error": str(error), "reason": error.reason})
    except (ValueError, TypeError, OverflowError, json.JSONDecodeError) as error:
        # ボディそのものが読めない場合（§0 の 64KiB 超過・不正 JSON 等）。
        # 契約どおり reason 付きの形を崩さないよう invalid に寄せる。
        req._json(HTTPStatus.BAD_REQUEST, {"error": str(error), "reason": "invalid"})
    except Exception as error:
        _fail(req, "現像元フォルダを設定できません。", error)


def handle_raw_develop(req: RawAssistHandler, parsed: ParseResult) -> None:
    # Imported at call time so a broken/mid-edit engine cannot stop the server
    # from starting. RawDevelopmentError is resolved here too, so the 400
    # mapping always uses the class the engine actually raises.
    try:
        from .engine import RawDevelopmentError, develop_raw
    except Exception as error:
        _fail(req, "現像エンジンを読み込めません。", error)
        return

    try:
        payload = _request_payload(req)
        adjustments = payload.get("adjustments")
        if adjustments is not None and not isinstance(adjustments, dict):
            raise ValueError("adjustments はオブジェクトで指定してください。")
        preview = bool(payload.get("preview", False))
        result = develop_raw(
            str(payload.get("capture_id", "")),
            str(payload.get("preset", "neutral_recovery")),
            adjustments=adjustments,
            preview=preview,
            force=bool(payload.get("force", False)),
            max_edge=int(payload.get("max_edge", 6000)),
        )
        route = "/preview/" if result.get("preview", preview) else "/outputs/"
        root = PREVIEW_ROOT if result.get("preview", preview) else OUTPUT_ROOT
        output_path = root / result["filename"]
        cache_buster = f"?v={output_path.stat().st_mtime_ns}" if output_path.is_file() else ""
        result["output_url"] = f"http://{HOST}:{PORT}{route}{quote(result['filename'])}{cache_buster}"
        req._json(HTTPStatus.OK, result)
    except (
        RawDevelopmentError,
        ValueError,
        TypeError,
        OverflowError,  # max_edge: 1e999 → int() が OverflowError
        json.JSONDecodeError,
    ) as error:
        req._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
    except Exception as error:
        _fail(req, "現像処理に失敗しました。", error)


def handle_merge_bracket(req: RawAssistHandler, parsed: ParseResult) -> None:
    """`POST /api/merge-bracket` → `MergedResult`（契約 §1）。

    `handle_raw_develop` と同じ流儀でハンドラは薄く保つ（判断は `bracket.py` 側）。
    import を呼び出し時にするのも同じ理由で、編集途中の合成モジュールがサーバの起動を
    止めないようにするため。`RawDevelopmentError` もここで解決し、400 マッピングが
    常に実際に投げられるクラスを見るようにする。
    """
    try:
        from .bracket import merge_bracket
        from .engine import RawDevelopmentError
    except Exception as error:
        _fail(req, "合成エンジンを読み込めません。", error)
        return

    try:
        payload = _request_payload(req)
        adjustments = payload.get("adjustments")
        if adjustments is not None and not isinstance(adjustments, dict):
            raise ValueError("adjustments はオブジェクトで指定してください。")
        preview = bool(payload.get("preview", False))
        result = merge_bracket(
            payload.get("capture_ids"),
            str(payload.get("preset", "neutral_recovery")),
            adjustments=adjustments,
            preview=preview,
            force=bool(payload.get("force", False)),
            max_edge=int(payload.get("max_edge", 6000)),
        )
        # プレビュー合成と本合成は別ディレクトリなので、URL のルートも `preview` で切り替える
        # （`handle_raw_develop` の `/preview/` と `/outputs/` の切り替えと同じ形）。
        route = "/merged-preview/" if result.get("preview", preview) else "/merged/"
        root = MERGED_PREVIEW_ROOT if result.get("preview", preview) else MERGED_ROOT
        output_path = root / result["filename"]
        cache_buster = f"?v={output_path.stat().st_mtime_ns}" if output_path.is_file() else ""
        result["output_url"] = (
            f"http://{HOST}:{PORT}{route}{quote(result['filename'])}{cache_buster}"
        )
        req._json(HTTPStatus.OK, result)
    except (
        RawDevelopmentError,
        ValueError,
        TypeError,
        OverflowError,  # max_edge: 1e999 → int() が OverflowError
        json.JSONDecodeError,
    ) as error:
        req._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
    except Exception as error:
        _fail(req, "合成処理に失敗しました。", error)


def handle_development_candidates(req: RawAssistHandler, parsed: ParseResult) -> None:
    """`POST /api/development-candidates` → JPG proxy candidates.

    Candidate generation is intentionally separate from ``raw-develop``: the
    optimizer reads one safe, reduced JPEG and never decodes the ARW for each
    particle.  Keep request validation strict here so values such as ``true``
    do not silently become a seed or budget of ``1``.
    """
    try:
        from .development_candidates import (
            DevelopmentCandidateError,
            DevelopmentCandidateNotFound,
            generate_development_candidates,
        )
    except Exception as error:
        _fail(req, "現像候補生成機能を読み込めません。", error)
        return

    try:
        payload = _request_payload(req)
        unknown = sorted(set(payload) - {"capture_id", "preset", "seed", "budget"})
        if unknown:
            raise ValueError(f"未対応の項目です: {', '.join(unknown)}")
        capture_id = payload.get("capture_id")
        preset = payload.get("preset")
        if not isinstance(capture_id, str) or not capture_id:
            raise ValueError("capture_id は文字列で指定してください。")
        if not isinstance(preset, str) or not preset:
            raise ValueError("preset は文字列で指定してください。")
        seed = payload.get("seed")
        budget = payload.get("budget")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise ValueError("seed は整数で指定してください。")
        if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int)):
            raise ValueError("budget は整数で指定してください。")
        result = generate_development_candidates(
            capture_id,
            preset,
            seed=seed,
            budget=budget,
        )
        req._json(HTTPStatus.OK, result)
    except DevelopmentCandidateNotFound as error:
        req._json(HTTPStatus.NOT_FOUND, {"error": str(error)})
    except (
        DevelopmentCandidateError,
        ValueError,
        TypeError,
        OverflowError,
        json.JSONDecodeError,
    ) as error:
        req._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
    except Exception as error:
        _fail(req, "現像候補の生成に失敗しました。", error)


def handle_export(req: RawAssistHandler, parsed: ParseResult) -> None:
    try:
        from .export import export_batch
    except Exception as error:
        _fail(req, "書き出し機能を読み込めません。", error)
        return

    try:
        payload = _request_payload(req)
        req._json(HTTPStatus.OK, export_batch(payload.get("batch_name"), payload.get("items")))
    except (ValueError, TypeError, OverflowError, json.JSONDecodeError) as error:
        req._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
    except Exception as error:
        _fail(req, "書き出しに失敗しました。", error)


def handle_import_to_photos(req: RawAssistHandler, parsed: ParseResult) -> None:
    try:
        from .photos_import import import_batch
    except Exception as error:
        _fail(req, "「写真」への取り込み機能を読み込めません。", error)
        return

    try:
        payload = _request_payload(req)
        # osascript 側の失敗は契約どおり 200 + {"status": "error", ...} で返す。
        result = import_batch(payload.get("batch_name"), album=payload.get("album"))
        req._json(HTTPStatus.OK, result)
    except (ValueError, TypeError, OverflowError, json.JSONDecodeError) as error:
        req._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
    except Exception as error:
        _fail(req, "「写真」への取り込みに失敗しました。", error)


def handle_drive_upload(req: RawAssistHandler, parsed: ParseResult) -> None:
    """`POST /api/drive-upload` → `{"status": "started"|"already_running"}`（契約 §1）。

    認証・通信の失敗は 500 ではなくジョブ状態（`GET /api/drive-upload` の
    `status: "error"`）で返るので、ここが写すのは同期検証の失敗だけになる。
    """
    try:
        from .drive_upload import start_drive_upload
    except Exception as error:
        _fail(req, "Google Drive アップロード機能を読み込めません。", error)
        return

    try:
        payload = _request_payload(req)
        status = start_drive_upload(payload.get("batch_name"))
        req._json(HTTPStatus.OK, {"status": status})
    except (ValueError, TypeError, OverflowError, json.JSONDecodeError) as error:
        req._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
    except Exception as error:
        _fail(req, "Google Drive アップロードの開始に失敗しました。", error)


def handle_get_drive_upload(req: RawAssistHandler, parsed: ParseResult) -> None:
    """`GET /api/drive-upload` → ジョブ状態（契約 §1）。"""
    try:
        from .drive_upload import get_drive_upload_state

        req._json(HTTPStatus.OK, get_drive_upload_state())
    except Exception as error:
        _fail(req, "Google Drive アップロードの状態を取得できません。", error)


GET_EXACT: dict[str, Handler] = {
    "/api/health": handle_health,
    "/api/analysis": handle_analysis,
    "/api/source-folder": handle_source_folder,
    "/api/drive-upload": handle_get_drive_upload,
}

# Order matters: matched top-to-bottom, mirroring the original if-chain.
GET_PREFIX: list[tuple[str, Handler]] = [
    ("/outputs/", handle_outputs),
    ("/preview/", handle_preview),
    ("/merged-preview/", handle_merged_preview),
    ("/merged/", handle_merged),
    ("/thumbs/", handle_thumbs),
    ("/source-jpg/", handle_source_jpg),
]

POST_ROUTES: dict[str, Handler] = {
    "/api/raw-develop": handle_raw_develop,
    "/api/merge-bracket": handle_merge_bracket,
    "/api/development-candidates": handle_development_candidates,
    "/api/source-folder": handle_set_source_folder,
    "/api/analyze": handle_analyze,
    "/api/export": handle_export,
    "/api/import-to-photos": handle_import_to_photos,
    "/api/drive-upload": handle_drive_upload,
}


def main() -> None:
    if config.ensure_migrated():
        print(f"[migrate] moved to app data dir: {config.CONFIG_PATH}", flush=True)

    from . import drive_upload

    if drive_upload.ensure_migrated():
        print(f"[migrate] moved to app data dir: {drive_upload.TOKEN_PATH}", flush=True)

    for directory in (
        OUTPUT_ROOT,
        PREVIEW_ROOT,
        MERGED_ROOT,
        MERGED_PREVIEW_ROOT,
        THUMBS_ROOT,
        analysis.ANALYSIS_ROOT,
    ):
        directory.mkdir(parents=True, exist_ok=True)
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
