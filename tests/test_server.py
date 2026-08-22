"""HTTPハンドラの分岐の検証（ソケットは開かない）。

`server.py` は「判断は各モジュール側、ハンドラは薄く」という方針なので、ここで見るのは
**ハンドラにしか無い分岐**だけに絞る:

- `POST /api/merge-bracket` の `output_url` が `preview` で配信ルートを切り替えること
- `GET /merged-preview/` が `_serve_from_root()` のパストラバーサル検査を緩めていないこと
- 同じディレクトリを別々に導出している定数（`server` / `bracket` / `export`）が食い違わないこと
- `/api/drive-upload` の 2 ハンドラが例外を HTTP へ写す分岐（400 / 500）

`RawAssistHandler` はソケットに紐づくのでインスタンス化せず、ハンドラが実際に呼ぶ
メソッドだけを持つ `StubRequest` を渡す（ポートは一切使わない）。
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

from local_raw import bracket as bracket_module
from local_raw import drive_upload as drive_upload_module
from local_raw import export as export_module
from local_raw import server as server_module
from local_raw.server import RawAssistHandler

MERGED_NAME = "DSC04510-merged5-neutral_recovery-a1b2c3d4e5.jpg"


class StubRequest:
    """ハンドラへ渡す最小の受け口（応答と配信先を記録するだけ）。"""

    def __init__(self, payload: dict | None = None):
        self.payload = payload
        self.status = None
        self.body: dict | None = None
        self.served: Path | None = None
        self.logs: list[str] = []

    def _read_json_body(self) -> dict | None:
        return self.payload

    def _json(self, status, payload: dict) -> None:
        self.status = status
        self.body = payload

    def _serve_file(self, path: Path) -> None:
        self.served = path

    def log_message(self, format: str, *args) -> None:
        self.logs.append(format % args)

    #: パストラバーサル検査は**本物を使う**（差し替えると防御を検証したことにならない）。
    #: `_serve_from_root` は `self._json` / `self._serve_file` しか触らないので、
    #: 素の関数としてそのまま束ねられる。
    _serve_from_root = RawAssistHandler._serve_from_root


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # macOS の /var → /private/var シンボリックリンク対策に resolve する。
        self.project_root = Path(temporary.name).resolve()
        self.merged_root = self.project_root / "outputs" / "merged"
        self.merged_preview_root = self.project_root / "outputs" / "merged_preview"
        self.merged_root.mkdir(parents=True, exist_ok=True)
        self.merged_preview_root.mkdir(parents=True, exist_ok=True)

        patcher = mock.patch.multiple(
            server_module,
            MERGED_ROOT=self.merged_root,
            MERGED_PREVIEW_ROOT=self.merged_preview_root,
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class MergeBracketUrlTests(ServerTestCase):
    """`output_url` は `preview` の値で配信ルートを切り替える（`handle_raw_develop` と同じ形）。"""

    def call_merge(self, *, preview: bool) -> StubRequest:
        result = {"filename": MERGED_NAME, "preview": preview, "cached": False}
        request = StubRequest({"capture_ids": ["DSC04510", "DSC04513"]})
        with mock.patch.object(bracket_module, "merge_bracket", return_value=result):
            server_module.handle_merge_bracket(request, urlparse("/api/merge-bracket"))
        return request

    def test_preview_merge_is_served_from_merged_preview(self):
        output_path = self.merged_preview_root / MERGED_NAME
        output_path.write_bytes(b"jpeg")
        request = self.call_merge(preview=True)

        self.assertEqual(request.status, 200)
        self.assertEqual(
            request.body["output_url"],
            f"http://{server_module.HOST}:{server_module.PORT}"
            f"/merged-preview/{MERGED_NAME}?v={output_path.stat().st_mtime_ns}",
        )

    def test_full_merge_is_served_from_merged(self):
        output_path = self.merged_root / MERGED_NAME
        output_path.write_bytes(b"jpeg")
        request = self.call_merge(preview=False)

        self.assertEqual(request.status, 200)
        self.assertEqual(
            request.body["output_url"],
            f"http://{server_module.HOST}:{server_module.PORT}"
            f"/merged/{MERGED_NAME}?v={output_path.stat().st_mtime_ns}",
        )

    def test_cache_buster_is_omitted_when_the_file_is_missing(self):
        """ファイルが無ければ `?v=` は付かない（既存の挙動を保つ）。"""
        request = self.call_merge(preview=True)
        self.assertEqual(
            request.body["output_url"],
            f"http://{server_module.HOST}:{server_module.PORT}/merged-preview/{MERGED_NAME}",
        )


class MergedPreviewRouteTests(ServerTestCase):
    """`GET /merged-preview/` の配信と、パストラバーサル検査が緩んでいないこと。"""

    def get(self, path: str) -> StubRequest:
        request = StubRequest()
        server_module.handle_merged_preview(request, urlparse(path))
        return request

    def matched_handler(self, path: str):
        """`do_GET` と同じ順序で前方一致のルート表を引く。"""
        for prefix, handler in server_module.GET_PREFIX:
            if path.startswith(prefix):
                return handler
        return None

    def test_serves_a_file_from_the_preview_root(self):
        (self.merged_preview_root / MERGED_NAME).write_bytes(b"jpeg")
        request = self.get(f"/merged-preview/{MERGED_NAME}")
        self.assertEqual(request.served, self.merged_preview_root / MERGED_NAME)
        self.assertIsNone(request.status)

    def test_picks_the_preview_root_when_both_directories_have_the_file(self):
        """同名のファイルが両方にあっても、プレビュー側のルートから配信すること。

        片方にしか置かないと「配信できなかった」のか「ファイルが無かった」のか
        区別できないので、**同じ名前を両方に置いて**どちらを選んだかを見る。
        """
        (self.merged_root / MERGED_NAME).write_bytes(b"full-merge")
        (self.merged_preview_root / MERGED_NAME).write_bytes(b"preview-merge")
        request = self.get(f"/merged-preview/{MERGED_NAME}")
        self.assertEqual(request.served, self.merged_preview_root / MERGED_NAME)
        self.assertEqual(request.served.read_bytes(), b"preview-merge")
        self.assertIsNone(request.status)

    def test_rejects_traversal_and_non_servable_suffixes(self):
        (self.merged_root / MERGED_NAME).write_bytes(b"jpeg")
        (self.merged_preview_root / "recipe.json").write_text("{}", encoding="utf-8")
        (self.merged_preview_root / "sub").mkdir()
        (self.merged_preview_root / "sub" / MERGED_NAME).write_bytes(b"jpeg")
        for path in (
            f"/merged-preview/../merged/{MERGED_NAME}",
            f"/merged-preview/%2e%2e%2fmerged%2f{MERGED_NAME}",
            f"/merged-preview/sub/{MERGED_NAME}",
            "/merged-preview/recipe.json",
            "/merged-preview/../../etc/passwd",
            "/merged-preview/%00.jpg",
        ):
            with self.subTest(path=path):
                request = self.get(path)
                self.assertIsNone(request.served)
                self.assertEqual(request.status, 404)

    def test_route_table_separates_preview_from_full_merges(self):
        self.assertIs(
            self.matched_handler(f"/merged-preview/{MERGED_NAME}"),
            server_module.handle_merged_preview,
        )
        self.assertIs(
            self.matched_handler(f"/merged/{MERGED_NAME}"), server_module.handle_merged
        )


class PostOriginTests(unittest.TestCase):
    """`do_POST` は Origin ヘッダを**必須**にすること。

    「Origin があれば照合、無ければ素通し」だと、同一マシン上の任意のプロセスが
    `curl`（既定で Origin を送らない）で `POST /api/source-folder` を叩いて現像元
    フォルダを差し替えられる。ソケットは開かず、`do_POST` を素の関数として
    スタブに束ねて分岐だけを見る（`StubRequest._serve_from_root` と同じ流儀）。
    """

    class PostRequest(StubRequest):
        do_POST = RawAssistHandler.do_POST

        def __init__(self, headers: dict, path: str = "/api/source-folder"):
            super().__init__()
            self.headers = headers
            self.path = path

        def _host_allowed(self) -> bool:
            return True

    def post(self, headers: dict):
        """ルート表はスタブに差し替え、ハンドラまで届いたかを記録する。"""
        request = self.PostRequest(headers)
        called: list[str] = []
        routes = {"/api/source-folder": lambda req, parsed: called.append(parsed.path)}
        with mock.patch.object(server_module, "POST_ROUTES", routes):
            request.do_POST()
        return request, called

    def test_post_without_an_origin_header_is_rejected(self):
        request, called = self.post({})
        self.assertEqual(request.status, 403)
        self.assertEqual(called, [], "403 のときはハンドラまで届かないこと")

    def test_post_with_a_foreign_origin_is_rejected(self):
        request, called = self.post({"Origin": "https://evil.example"})
        self.assertEqual(request.status, 403)
        self.assertEqual(called, [])

    def test_post_from_the_app_and_the_dev_server_still_passes(self):
        """パッケージ版（`tauri://localhost`）とブラウザ開発モードは通ること。"""
        for origin in ("tauri://localhost", "http://localhost:1420"):
            with self.subTest(origin=origin):
                request, called = self.post({"Origin": origin})
                self.assertIsNone(request.status)
                self.assertEqual(called, ["/api/source-folder"])


class DriveUploadHandlerTests(unittest.TestCase):
    """`/api/drive-upload` の 2 ハンドラ（判断は `drive_upload.py` 側なので分岐だけ見る）。"""

    def post(self, payload: dict | None = None) -> StubRequest:
        request = StubRequest(payload if payload is not None else {"batch_name": "trip-2026"})
        server_module.handle_drive_upload(request, urlparse("/api/drive-upload"))
        return request

    def test_a_started_job_returns_the_status(self):
        with mock.patch.object(drive_upload_module, "start_drive_upload", return_value="started"):
            request = self.post()
        self.assertEqual(request.status, 200)
        self.assertEqual(request.body, {"status": "started"})

    def test_a_second_request_reports_already_running(self):
        with mock.patch.object(
            drive_upload_module, "start_drive_upload", return_value="already_running"
        ):
            request = self.post()
        self.assertEqual(request.status, 200)
        self.assertEqual(request.body, {"status": "already_running"})

    def test_a_validation_failure_is_a_400(self):
        with mock.patch.object(
            drive_upload_module,
            "start_drive_upload",
            side_effect=ValueError("書き出し済みのフォルダが見つかりません。"),
        ):
            request = self.post()
        self.assertEqual(request.status, 400)
        self.assertEqual(request.body, {"error": "書き出し済みのフォルダが見つかりません。"})

    def test_an_unexpected_failure_is_a_500_without_the_detail(self):
        with mock.patch.object(
            drive_upload_module, "start_drive_upload", side_effect=RuntimeError("/Users/secret")
        ):
            request = self.post()
        self.assertEqual(request.status, 500)
        self.assertNotIn("/Users/secret", request.body["error"])
        self.assertTrue(request.logs, "詳細はサーバログにだけ残すこと")

    def test_a_non_object_body_is_a_400(self):
        request = StubRequest(payload=["not", "an", "object"])
        server_module.handle_drive_upload(request, urlparse("/api/drive-upload"))
        self.assertEqual(request.status, 400)

    def test_the_state_endpoint_passes_the_payload_through(self):
        state = {
            "status": "running",
            "batch_name": "trip-2026",
            "progress": {
                "stage": "uploading",
                "done": 1,
                "total": 2,
                "bytes_done": 10,
                "bytes_total": 20,
                "skipped": [],
            },
        }
        request = StubRequest()
        with mock.patch.object(
            drive_upload_module, "get_drive_upload_state", return_value=state
        ):
            server_module.handle_get_drive_upload(request, urlparse("/api/drive-upload"))
        self.assertEqual(request.status, 200)
        self.assertEqual(request.body, state)

    def test_a_state_failure_is_a_500(self):
        request = StubRequest()
        with mock.patch.object(
            drive_upload_module, "get_drive_upload_state", side_effect=RuntimeError("boom")
        ):
            server_module.handle_get_drive_upload(request, urlparse("/api/drive-upload"))
        self.assertEqual(request.status, 500)

    def test_the_route_table_wires_both_methods(self):
        self.assertIs(
            server_module.GET_EXACT["/api/drive-upload"], server_module.handle_get_drive_upload
        )
        self.assertIs(
            server_module.POST_ROUTES["/api/drive-upload"], server_module.handle_drive_upload
        )


class StaticRootConsistencyTests(unittest.TestCase):
    """同じディレクトリを複数モジュールで別々に導出しているため、実体の一致を固定する。

    `server.py` / `export.py` は cv2 を引き込まないよう `bracket.py` を import せずに
    パスを組み立てている（各モジュールのコメント参照）。綴りがずれると配信だけが
    静かに 404 になるので、ここで突き合わせる。
    """

    def test_merged_roots_agree(self):
        self.assertEqual(server_module.MERGED_ROOT, bracket_module.MERGED_ROOT)
        self.assertEqual(export_module.MERGED_ROOT, bracket_module.MERGED_ROOT)

    def test_merged_preview_roots_agree_and_differ_from_the_full_merge_root(self):
        self.assertEqual(
            server_module.MERGED_PREVIEW_ROOT, bracket_module.MERGED_PREVIEW_ROOT
        )
        self.assertNotEqual(
            bracket_module.MERGED_PREVIEW_ROOT, bracket_module.MERGED_ROOT
        )
        self.assertEqual(bracket_module.MERGED_PREVIEW_ROOT.name, "merged_preview")


if __name__ == "__main__":
    unittest.main()
