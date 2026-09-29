"""Google Drive アップロードの検証。

**実際の Google API・ブラウザは一切叩かない。** `local_raw/drive_upload.py` は google 系
ライブラリへの遅延 import を 6 つの関数（`_google_error_types` / `_credentials_from_payload`
/ `_refresh_credentials` / `_build_service` / `_media_file_upload` / `_run_oauth_consent`）に
閉じ込めてあるので、テストはこの継ぎ目をフェイクへ差し替える。`.venv` には google 系
ライブラリが**入っていない**前提なので、本物のクラスを import してはいけない
（`RefreshError` / `HttpError` の代役はこのファイル内で定義する）。

⚠️ `drive_upload.CLIENT_SECRET_PATH` の実体はユーザー個人の未追跡ファイルなので、
`DriveUploadTestCase` が基底で必ず一時ディレクトリへ差し替える。上書き・削除すると
復旧できない。
"""

import contextlib
import importlib
import io
import json
import os
import re
import socket
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from local_raw import drive_upload
from local_raw import export as export_module
from local_raw import paths

#: デスクトップアプリ種別の client secret（`installed` キーを持つ）。
CLIENT_SECRET = {
    "installed": {
        "client_id": "test.apps.googleusercontent.com",
        "client_secret": "test-secret",
        "redirect_uris": ["http://localhost"],
    }
}

BATCH = "trip-2026"


# ---------------------------------------------------------------------------
# フェイク
# ---------------------------------------------------------------------------


class StubRefreshError(Exception):
    """`google.auth.exceptions.RefreshError` の代役。"""


class StubTransportError(Exception):
    """`google.auth.exceptions.TransportError` の代役。

    本物の MRO は `TransportError` → `GoogleAuthError` → `Exception`（確認済み）。
    **`OSError` 派生ではない**ので、代役も `Exception` から直に派生させる。
    ここを `OSError` 派生にすると「`OSError` は全部 network」な実装でも緑になり、
    本物の通信障害が `unknown` へ落ちる回帰を捕まえられなくなる。
    """


class StubServerNotFoundError(Exception):
    """`httplib2.ServerNotFoundError`（DNS 失敗）の代役。

    本物の MRO は `ServerNotFoundError` → `HttpLib2Error` → `Exception`（確認済み）。
    こちらも `OSError` でも `socket.gaierror` でもない。
    """


class StubHttpError(Exception):
    """`googleapiclient.errors.HttpError` の代役。

    本物と同じく `resp.status` / `status_code` / `reason` / `error_details` を持たせる
    （`_http_status()` が両方の経路を辿れることを確かめるため）。

    ⚠️ **`reason` と `error_details` の役割を本物と揃えること。** 本物の
    `reason` はレスポンス本文の `error.message`、つまり**人間向け文言**であって、
    `storageQuotaExceeded` のようなマシン可読コードは入らない。コードが入るのは
    `error_details` の側で、Drive v3 の 403 では本文の `error.errors[]` が
    そのまま `[{"domain": ..., "reason": "storageQuotaExceeded", ...}, ...]` という
    **dict の list** として載る（google-api-python-client 2.199.0 の
    `HttpError._get_reason()` で確認済み）。ここを取り違えて `reason` にコードを
    置くと、本番では絶対に発火しない分類がテストでだけ緑になる。

    `codes` は `error_details` へ載せる reason code、`reason` は人間向け文言。
    `codes` を渡さないときの `error_details` は本物の初期値と同じ `""`（空文字）。
    """

    def __init__(self, status, reason="", codes=None):
        super().__init__(f"stub http error {status}")
        self.resp = types.SimpleNamespace(status=status)
        self.status_code = status
        self.reason = reason
        self.error_details = (
            [{"domain": "global", "reason": code} for code in codes] if codes is not None else ""
        )


class FakeCredentials:
    """`google.oauth2.credentials.Credentials` の代役。"""

    def __init__(self, *, token="access-token", refresh_token="refresh-token", scopes=None, valid=True):
        self.token = token
        self.refresh_token = refresh_token
        self.scopes = list(scopes if scopes is not None else drive_upload.SCOPES)
        self.valid = valid

    def has_scopes(self, scopes):
        return set(scopes).issubset(set(self.scopes))

    def to_json(self):
        return json.dumps(
            {
                "token": self.token,
                "refresh_token": self.refresh_token,
                "client_id": "test.apps.googleusercontent.com",
                "client_secret": "test-secret",
                "scopes": self.scopes,
            }
        )


class _FakeExecutable:
    """`files().list(...)` / `files().create(...)` の戻り（`.execute()` だけ持つ）。"""

    def __init__(self, run):
        self._run = run

    def execute(self, **kwargs):
        return self._run(**kwargs)


class _FakeFiles:
    def __init__(self, drive):
        self._drive = drive

    def list(self, **kwargs):
        return _FakeExecutable(lambda **_: self._drive.handle_list(kwargs))

    def create(self, **kwargs):
        return _FakeExecutable(lambda **execute: self._drive.handle_create(kwargs, execute))


class FakeDrive:
    """Drive API の最小スタブ。呼び出しを記録する。

    `q` にフォルダの mimeType が含まれるかどうかで「フォルダ検索」と
    「フォルダ内のファイル一覧」を振り分ける（実装が投げるクエリの形をそのまま検査対象にする）。
    """

    def __init__(self, *, folder_hits=None, listing_pages=None):
        #: フォルダ名 → 検索ヒット（`[{"id":..., "createdTime":...}]`）。既定は「見つからない」。
        self.folder_hits = dict(folder_hits or {})
        #: `_existing_filenames()` が読むページ列。
        self.listing_pages = list(
            listing_pages if listing_pages is not None else [{"files": []}]
        )
        self.list_calls = []
        self.created_folders = []
        self.uploads = []
        self._listing_index = 0
        self._issued = 0

    def files(self):
        return _FakeFiles(self)

    def _issue_id(self):
        self._issued += 1
        return f"created-{self._issued}"

    def handle_list(self, kwargs):
        self.list_calls.append(kwargs)
        query = kwargs.get("q") or ""
        if drive_upload.FOLDER_MIME_TYPE in query:
            matched = re.search(r"name = '([^']*)'", query)
            name = matched.group(1) if matched else ""
            return {"files": list(self.folder_hits.get(name, []))}
        page = self.listing_pages[self._listing_index]
        self._listing_index = min(self._listing_index + 1, len(self.listing_pages) - 1)
        return page

    def handle_create(self, kwargs, execute_kwargs):
        body = kwargs.get("body") or {}
        if kwargs.get("media_body") is not None:
            self.uploads.append(
                {
                    "name": body.get("name"),
                    "parents": body.get("parents"),
                    "media": kwargs.get("media_body"),
                    "num_retries": execute_kwargs.get("num_retries"),
                }
            )
            return {"id": self._issue_id()}
        self.created_folders.append(dict(kwargs))
        return {"id": self._issue_id()}


# ---------------------------------------------------------------------------
# 基底
# ---------------------------------------------------------------------------


class DriveUploadTestCase(unittest.TestCase):
    """モジュール定数を一時ディレクトリへ差し替え、ジョブ状態を毎回素に戻す。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # macOS の /var → /private/var シンボリックリンク対策に resolve する。
        self.root = Path(temporary.name).resolve()
        self.client_secret_path = self.root / "drive_client_secret.json"
        self.token_path = self.root / "outputs" / "drive_auth.json"
        self.export_root = self.root / "outputs" / "export"
        self.export_root.mkdir(parents=True, exist_ok=True)
        self.write_client_secret(CLIENT_SECRET)

        patcher = mock.patch.multiple(
            drive_upload,
            CLIENT_SECRET_PATH=self.client_secret_path,
            TOKEN_PATH=self.token_path,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        export_patcher = mock.patch.object(export_module, "EXPORT_ROOT", self.export_root)
        export_patcher.start()
        self.addCleanup(export_patcher.stop)

        self.reset_job_state()
        self.addCleanup(self.reset_job_state)

    def reset_job_state(self):
        with drive_upload._STATE_LOCK:
            drive_upload._RUNNING = False
            drive_upload._PROGRESS = None
            drive_upload._CURRENT_BATCH = None
            drive_upload._LAST_RESULT = None
            drive_upload._LAST_ERROR_KIND = None
            drive_upload._LAST_ERROR_MESSAGE = None

    def write_client_secret(self, payload):
        if isinstance(payload, str):
            self.client_secret_path.write_text(payload, encoding="utf-8")
        else:
            self.client_secret_path.write_text(json.dumps(payload), encoding="utf-8")

    def make_batch(self, name=BATCH, files=None):
        """`outputs/export/<name>/` を作ってファイルを置く。"""
        contents = files if files is not None else {"manifest.json": b"{}", "DSC00001.JPG": b"jpeg"}
        directory = self.export_root / name
        directory.mkdir(parents=True, exist_ok=True)
        for filename, data in contents.items():
            (directory / filename).write_bytes(data)
        return directory

    def run_upload(self, batch_name=BATCH, drive=None, credentials=None):
        """`_run_upload()` をフェイクの継ぎ目越しに走らせる。"""
        drive = drive if drive is not None else FakeDrive()
        progress = drive_upload.Progress()
        with mock.patch.multiple(
            drive_upload,
            _get_credentials=mock.DEFAULT,
            _build_service=mock.DEFAULT,
            _media_file_upload=mock.DEFAULT,
        ) as patched:
            patched["_get_credentials"].return_value = credentials or FakeCredentials()
            patched["_build_service"].return_value = drive
            patched["_media_file_upload"].side_effect = lambda path: f"media:{path.name}"
            result = drive_upload._run_upload(progress, batch_name)
        return result, progress, drive


# ---------------------------------------------------------------------------
# 1. フォルダ解決
# ---------------------------------------------------------------------------


class ResolveFolderTests(DriveUploadTestCase):
    def test_an_exact_match_is_reused_without_creating_anything(self):
        drive = FakeDrive(folder_hits={"GeoFrame": [{"id": "root-1", "createdTime": "2026-01-01T00:00:00Z"}]})
        self.assertEqual(drive_upload._resolve_folder(drive, "GeoFrame", None), "root-1")
        self.assertEqual(drive.created_folders, [], "既存フォルダがあるのに作ってはいけない")

    def test_a_missing_folder_is_created_as_a_folder(self):
        drive = FakeDrive()
        folder_id = drive_upload._resolve_folder(drive, "GeoFrame", None)

        self.assertEqual(folder_id, "created-1")
        self.assertEqual(len(drive.created_folders), 1)
        body = drive.created_folders[0]["body"]
        self.assertEqual(body["name"], "GeoFrame")
        self.assertEqual(body["mimeType"], drive_upload.FOLDER_MIME_TYPE)
        self.assertNotIn("parents", body, "親を指定しない検索で作るならルート直下")

    def test_a_child_folder_is_created_under_its_parent(self):
        drive = FakeDrive()
        folder_id = drive_upload._resolve_folder(drive, BATCH, "root-1")

        self.assertEqual(folder_id, "created-1")
        self.assertEqual(drive.created_folders[0]["body"]["parents"], ["root-1"])

    def test_the_oldest_match_wins_when_several_folders_share_the_name(self):
        """同名フォルダが複数あっても実行ごとに入れ先が変わらないこと。

        API の並び順に依存していないかを見るため、**わざと新しい順**で返す。
        """
        drive = FakeDrive(
            folder_hits={
                "GeoFrame": [
                    {"id": "newer", "createdTime": "2026-05-01T00:00:00Z"},
                    {"id": "oldest", "createdTime": "2024-01-01T00:00:00Z"},
                    {"id": "middle", "createdTime": "2025-03-01T00:00:00Z"},
                ]
            }
        )
        self.assertEqual(drive_upload._resolve_folder(drive, "GeoFrame", None), "oldest")

    def test_a_missing_created_time_does_not_break_the_ordering(self):
        drive = FakeDrive(
            folder_hits={
                "GeoFrame": [
                    {"id": "dated", "createdTime": "2026-05-01T00:00:00Z"},
                    {"id": "undated"},
                ]
            }
        )
        # `createdTime` 無しは空文字へ畳まれるので必ず先頭に来る（順序が壊れないことが要点）。
        self.assertEqual(drive_upload._resolve_folder(drive, "GeoFrame", None), "undated")

    def test_the_query_restricts_to_folders_and_to_the_parent(self):
        drive = FakeDrive()
        drive_upload._resolve_folder(drive, BATCH, "root-1")

        query = drive.list_calls[0]["q"]
        self.assertIn(f"name = '{BATCH}'", query)
        self.assertIn(f"mimeType = '{drive_upload.FOLDER_MIME_TYPE}'", query)
        self.assertIn("trashed = false", query)
        self.assertIn("'root-1' in parents", query)

    def test_a_root_level_search_is_not_scoped_to_a_parent(self):
        drive = FakeDrive()
        drive_upload._resolve_folder(drive, "GeoFrame", None)
        self.assertNotIn("in parents", drive.list_calls[0]["q"])

    def test_created_time_is_requested_explicitly(self):
        """既定の `fields` に `createdTime` は含まれない。要求し忘れると最古判定が死ぬ。"""
        drive = FakeDrive()
        drive_upload._resolve_folder(drive, "GeoFrame", None)
        self.assertIn("createdTime", drive.list_calls[0]["fields"])

    def test_owned_only_restricts_the_search_to_the_users_own_files(self):
        """`owned_only=True` は自分が所有するフォルダだけを検索対象にする。

        指定しないと、他人が自分に**共有**した同名フォルダが Drive のトップレベルにも
        並んで見えるため、ルート検索（`parent_id=None`）に紛れ込みうる。作成は常に
        自分の My Drive 直下に行われるので、検索側を絞らないと「作成時と検索時で
        別のフォルダを指す」自己矛盾が起きる。
        """
        drive = FakeDrive()
        drive_upload._resolve_folder(drive, "GeoFrame", None, owned_only=True)
        self.assertIn("'me' in owners", drive.list_calls[0]["q"])

    def test_owned_only_also_scopes_the_search_to_my_drive_root(self):
        """`'me' in owners` だけでは足りない。

        所有者を絞っても、Drive の**任意の階層**にある自分所有の同名フォルダが一致しうる。
        作成は必ず My Drive 直下（`parents` 省略）なので、検索も直下へ揃えないと、
        ユーザーが昔どこか別の場所に作った `GeoFrame` へ黙って積むことになる。
        """
        drive = FakeDrive()
        drive_upload._resolve_folder(drive, "GeoFrame", None, owned_only=True)
        self.assertIn("'root' in parents", drive.list_calls[0]["q"])

    def test_owned_only_defaults_to_off_for_child_folder_lookups(self):
        """`parent_id` 指定の検索には既定で適用しない。

        共有ドライブ配下のフォルダは所有者が自分でないことがあり、絞ると検索が壊れる。
        （アップロード経路はこの枝を使わなくなったが、既定値の意味は保つ。）
        """
        drive = FakeDrive()
        drive_upload._resolve_folder(drive, BATCH, "root-1")
        self.assertNotIn("owners", drive.list_calls[0]["q"])


class ExistingFilenamesTests(DriveUploadTestCase):
    def test_pages_are_followed_until_the_token_runs_out(self):
        drive = FakeDrive(
            listing_pages=[
                {"files": [{"name": "DSC00001.JPG"}], "nextPageToken": "page-2"},
                {"files": [{"name": "DSC00002.JPG"}, {"name": "manifest.json"}]},
            ]
        )
        names = drive_upload._existing_filenames(drive, "folder-1")

        self.assertEqual(names, {"DSC00001.JPG", "DSC00002.JPG", "manifest.json"})
        self.assertEqual(len(drive.list_calls), 2)
        self.assertIsNone(drive.list_calls[0]["pageToken"])
        self.assertEqual(drive.list_calls[1]["pageToken"], "page-2")

    def test_trashed_files_are_excluded_by_the_query(self):
        """ゴミ箱の同名ファイルでスキップされると、消したファイルが二度と上がらない。"""
        drive = FakeDrive()
        drive_upload._existing_filenames(drive, "folder-1")
        query = drive.list_calls[0]["q"]
        self.assertIn("'folder-1' in parents", query)
        self.assertIn("trashed = false", query)

    def test_a_name_prefix_narrows_the_listing_on_the_drive_side(self):
        """平置きで `GeoFrame` 直下に全バッチが積み上がるので、絞り込みは Drive 側で行う。

        クライアント側で捨てても列挙件数は減らず、バッチが増えるほど遅くなる。
        """
        drive = FakeDrive()
        drive_upload._existing_filenames(drive, "folder-1", name_prefix=f"{BATCH}-")
        query = drive.list_calls[0]["q"]
        self.assertIn(f"name contains '{BATCH}-'", query)
        # 絞り込みは既存の条件に**足す**もの。親とゴミ箱の条件を落としてはいけない。
        self.assertIn("'folder-1' in parents", query)
        self.assertIn("trashed = false", query)

    def test_without_a_prefix_the_listing_is_not_narrowed(self):
        """既定は絞り込み無し（プレフィックスを持たない用途のために残す）。"""
        drive = FakeDrive()
        drive_upload._existing_filenames(drive, "folder-1")
        self.assertNotIn("name contains", drive.list_calls[0]["q"])


class UploadFileTests(DriveUploadTestCase):
    """`_upload_file()` は中身をローカルのパスから、名前を `upload_name` から取る。"""

    def test_the_drive_name_comes_from_upload_name_and_the_body_from_the_path(self):
        path = self.make_batch(files={"DSC00001.JPG": b"jpeg"}) / "DSC00001.JPG"
        drive = FakeDrive()
        with mock.patch.object(
            drive_upload, "_media_file_upload", side_effect=lambda given: f"media:{given.name}"
        ) as media:
            drive_upload._upload_file(drive, path, "folder-1", f"{BATCH}-DSC00001.JPG")

        media.assert_called_once_with(path)
        self.assertEqual(
            drive.uploads,
            [
                {
                    "name": f"{BATCH}-DSC00001.JPG",
                    "parents": ["folder-1"],
                    "media": "media:DSC00001.JPG",
                    "num_retries": drive_upload.UPLOAD_NUM_RETRIES,
                }
            ],
        )


# ---------------------------------------------------------------------------
# 2. 平置き + バッチ名プレフィックス / 同名スキップ / 部分再実行
# ---------------------------------------------------------------------------


class SkipExistingTests(DriveUploadTestCase):
    """アップロードは `GeoFrame` **直下**へ平置きし、名前へ `<batch_name>-` を付ける。

    消費側（GeoFrame 本体アプリ）の Drive インデックスが `GeoFrame` を**非再帰**で
    走査するのでサブフォルダは作れず、平置きにするとバッチ間で
    `DSC00001.JPG` が衝突するため、名前側で分ける。
    fixture には `manifest.json` を置いたままにし、Drive へ送られないことも合わせて見る。
    """

    FILES = {
        "DSC00001.JPG": b"a" * 10,
        "DSC00002.JPG": b"b" * 20,
        "DSC00003.JPG": b"c" * 30,
        "manifest.json": b"{}",
    }

    @staticmethod
    def prefixed(*names, batch=BATCH):
        return [f"{batch}-{name}" for name in names]

    def test_a_fresh_batch_uploads_everything_with_the_batch_name_prefix(self):
        self.make_batch(files=self.FILES)
        result, _, drive = self.run_upload()

        self.assertEqual(result["uploaded"], 3)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["skipped"], [])
        self.assertEqual(
            [upload["name"] for upload in drive.uploads],
            self.prefixed("DSC00001.JPG", "DSC00002.JPG", "DSC00003.JPG"),
        )

    def test_existing_names_are_skipped_and_not_uploaded(self):
        """スキップ判定は Drive 上の名前（プレフィックス込み）で行うこと。"""
        self.make_batch(files=self.FILES)
        drive = FakeDrive(listing_pages=[{"files": [{"name": f"{BATCH}-DSC00001.JPG"}]}])
        result, _, drive = self.run_upload(drive=drive)

        self.assertEqual(result["skipped"], ["DSC00001.JPG"])
        self.assertEqual(result["uploaded"], 2)
        self.assertEqual(result["total"], 3)
        self.assertNotIn(
            f"{BATCH}-DSC00001.JPG", [upload["name"] for upload in drive.uploads]
        )

    def test_the_skipped_list_holds_the_local_names_without_the_prefix(self):
        """画面に出すのはバッチ内で見慣れた名前。契約の `skipped` の意味を変えない。"""
        self.make_batch(files=self.FILES)
        drive = FakeDrive(
            listing_pages=[
                {"files": [{"name": f"{BATCH}-DSC00001.JPG"}, {"name": f"{BATCH}-DSC00002.JPG"}]}
            ]
        )
        result, progress, _ = self.run_upload(drive=drive)

        self.assertEqual(result["skipped"], ["DSC00001.JPG", "DSC00002.JPG"])
        self.assertEqual(progress.snapshot()["skipped"], ["DSC00001.JPG", "DSC00002.JPG"])

    def test_the_same_name_from_another_batch_does_not_cause_a_skip(self):
        """平置きの肝。別バッチの `DSC00001.JPG` と衝突しないこと。

        プレフィックスが無ければ、カメラ命名のファイルが先着のバッチに食われて
        二度と上がらなくなる。
        """
        self.make_batch(files=self.FILES)
        drive = FakeDrive(
            listing_pages=[
                {
                    "files": [
                        {"name": "other_trip-DSC00001.JPG"},
                        # プレフィックス無しの素の名前（旧版が上げた残骸）にも引っかからない。
                        {"name": "DSC00001.JPG"},
                        # 旧版が作ったバッチ名のフォルダが同じ一覧に混ざっても無害。
                        {"name": BATCH},
                    ]
                }
            ]
        )
        result, _, drive = self.run_upload(drive=drive)

        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["uploaded"], 3)
        self.assertEqual(
            [upload["name"] for upload in drive.uploads],
            self.prefixed("DSC00001.JPG", "DSC00002.JPG", "DSC00003.JPG"),
        )

    def test_a_partial_rerun_only_sends_what_is_still_missing(self):
        """前回 2/3 だけ成功した状態からの再実行が、残り 1 件だけを送ること。"""
        self.make_batch(files=self.FILES)
        drive = FakeDrive(
            listing_pages=[
                {
                    "files": [
                        {"name": f"{BATCH}-DSC00001.JPG"},
                        {"name": f"{BATCH}-DSC00002.JPG"},
                    ]
                }
            ]
        )
        result, progress, drive = self.run_upload(drive=drive)

        self.assertEqual(
            [upload["name"] for upload in drive.uploads], self.prefixed("DSC00003.JPG")
        )
        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(result["skipped"], ["DSC00001.JPG", "DSC00002.JPG"])
        self.assertEqual(
            result["uploaded"] + len(result["skipped"]), result["total"],
            "uploaded + skipped が total と一致しないと画面の集計が合わない",
        )
        self.assertEqual(progress.snapshot()["done"], 3, "スキップ分も処理済みに数える")

    def test_uploads_target_the_geoframe_folder_itself_and_carry_the_retry_count(self):
        self.make_batch(files={"DSC00001.JPG": b"jpeg"})
        result, _, drive = self.run_upload()

        # `_resolve_folder` は `GeoFrame` の 1 回だけなので、置き先は created-1。
        self.assertEqual(drive.uploads[0]["parents"], ["created-1"])
        self.assertEqual(drive.uploads[0]["num_retries"], drive_upload.UPLOAD_NUM_RETRIES)
        self.assertEqual(drive.uploads[0]["name"], f"{BATCH}-DSC00001.JPG")
        # メディアは**ローカルのパス**から作る。プレフィックス付きの名前を
        # `_media_file_upload()` へ渡してしまう取り違えをここで止める。
        self.assertEqual(drive.uploads[0]["media"], "media:DSC00001.JPG")
        self.assertEqual(
            result["folder_url"],
            "https://drive.google.com/drive/folders/created-1",
            "folder_url は GeoFrame フォルダ自体を指すこと",
        )

    def test_only_the_geoframe_folder_is_resolved(self):
        """バッチごとのサブフォルダを作らないこと（この変更の中核）。"""
        self.make_batch(files={"DSC00001.JPG": b"jpeg"})
        _, _, drive = self.run_upload()

        folder_queries = [call["q"] for call in drive.list_calls if "mimeType" in call["q"]]
        self.assertEqual(len(folder_queries), 1, "フォルダ解決は GeoFrame の 1 回だけ")
        self.assertIn("name = 'GeoFrame'", folder_queries[0])
        # ルート検索の親は My Drive 直下（`'root'`）に固定。**具体的なフォルダIDでは
        # 絞らない**こと（`GeoFrame` は誰かの配下ではなくトップに作る）。
        self.assertIn("'root' in parents", folder_queries[0])
        self.assertNotIn("created-", folder_queries[0])
        self.assertEqual(
            [call["body"]["name"] for call in drive.created_folders],
            ["GeoFrame"],
            "バッチ名のフォルダを作ってはいけない",
        )

    def test_the_root_geoframe_lookup_is_scoped_to_the_users_own_files(self):
        """ルート検索は `owned_only=True` で呼ばれること。

        他人が共有した同名の `GeoFrame` フォルダを拾って、実在しないユーザーの
        フォルダへアップロードしてしまう回帰を防ぐ。
        """
        self.make_batch(files={"DSC00001.JPG": b"jpeg"})
        _, _, drive = self.run_upload()

        folder_queries = [call["q"] for call in drive.list_calls if "mimeType" in call["q"]]
        self.assertIn("'me' in owners", folder_queries[0], "ルート検索は自分の所有物に絞ること")
        self.assertIn(
            "'root' in parents", folder_queries[0], "ルート検索は My Drive 直下に絞ること"
        )

    def test_the_existing_names_are_read_from_the_geoframe_folder(self):
        self.make_batch(files={"DSC00001.JPG": b"jpeg"})
        _, _, drive = self.run_upload()

        listing_queries = [
            call["q"] for call in drive.list_calls if "mimeType" not in call["q"]
        ]
        self.assertIn("'created-1' in parents", listing_queries[0])
        # 一覧は自分のバッチのプレフィックスで絞る。絞らないと `GeoFrame` 直下に
        # 積み上がった全バッチ分を毎回列挙することになる。
        self.assertIn(f"name contains '{BATCH}-'", listing_queries[0])

    def test_the_prefix_uses_the_normalized_batch_name(self):
        """生の入力ではなく正規化済みの名前だけが Drive へ渡ること。

        出口は create の `name` と、一覧の絞り込みに使う検索クエリ `q` の 2 つ。
        どちらの経路にも「生の `batch_name` は Drive へ届かない」を固定しておく。
        """
        self.make_batch(name="my_trip", files={"DSC00001.JPG": b"jpeg"})
        _, _, drive = self.run_upload(batch_name="my trip")

        self.assertEqual([upload["name"] for upload in drive.uploads], ["my_trip-DSC00001.JPG"])

        listing_queries = [
            call["q"] for call in drive.list_calls if "mimeType" not in call["q"]
        ]
        self.assertIn("name contains 'my_trip-'", listing_queries[0])
        self.assertNotIn("my trip", listing_queries[0], "生の入力が `q` へ漏れてはいけない")


# ---------------------------------------------------------------------------
# 3. ファイル収集
# ---------------------------------------------------------------------------


class BatchFilesTests(DriveUploadTestCase):
    def test_collects_direct_files_in_name_order_excluding_the_manifest(self):
        directory = self.make_batch(
            files={"DSC00002.JPG": b"b", "manifest.json": b"{}", "DSC00001.JPG": b"a"}
        )
        self.assertEqual(
            [path.name for path in drive_upload._batch_files(directory)],
            ["DSC00001.JPG", "DSC00002.JPG"],
        )

    def test_temporary_files_and_subdirectories_are_excluded(self):
        directory = self.make_batch(
            files={"DSC00001.JPG": b"a", "DSC00002.JPG.tmp": b"partial", "manifest.json.tmp": b"{"}
        )
        (directory / "nested").mkdir()
        (directory / "nested" / "DSC00003.JPG").write_bytes(b"c")

        self.assertEqual(
            [path.name for path in drive_upload._batch_files(directory)], ["DSC00001.JPG"]
        )


# ---------------------------------------------------------------------------
# 4. 進捗
# ---------------------------------------------------------------------------


class ProgressTests(DriveUploadTestCase):
    def test_snapshot_has_the_contract_shape(self):
        progress = drive_upload.Progress()
        self.assertEqual(
            set(progress.snapshot()),
            {"stage", "done", "total", "bytes_done", "bytes_total", "skipped"},
        )
        self.assertEqual(progress.snapshot()["stage"], "authenticating")

    def test_counters_accumulate_over_uploads_and_skips(self):
        progress = drive_upload.Progress()
        progress.set_totals(3, 60)
        progress.advance(10)
        progress.advance(20)
        progress.add_skipped("DSC00003.JPG", 30)

        self.assertEqual(
            progress.snapshot(),
            {
                "stage": "authenticating",
                "done": 3,
                "total": 3,
                "bytes_done": 60,
                "bytes_total": 60,
                "skipped": ["DSC00003.JPG"],
            },
        )

    def test_snapshot_returns_a_copy_of_the_skipped_list(self):
        """生のリストを返すとロックの外へ可変状態が漏れる。"""
        progress = drive_upload.Progress()
        progress.add_skipped("DSC00001.JPG")
        leaked = progress.snapshot()["skipped"]
        leaked.append("injected")
        self.assertEqual(progress.snapshot()["skipped"], ["DSC00001.JPG"])

    def test_a_run_reaches_the_totals_and_walks_the_stages(self):
        self.make_batch(files={"DSC00001.JPG": b"a" * 7, "DSC00002.JPG": b"b" * 2, "manifest.json": b"{}"})
        _, progress, _ = self.run_upload()

        snapshot = progress.snapshot()
        self.assertEqual(snapshot["stage"], "uploading")
        self.assertEqual(snapshot["done"], snapshot["total"], 2)
        self.assertEqual(snapshot["bytes_done"], snapshot["bytes_total"])
        self.assertEqual(snapshot["bytes_total"], 7 + 2)


# ---------------------------------------------------------------------------
# 5. 認証情報の解決
# ---------------------------------------------------------------------------


class GetCredentialsTests(DriveUploadTestCase):
    def setUp(self):
        super().setUp()
        self.patch_error_types()

    def patch_error_types(self):
        patcher = mock.patch.object(
            drive_upload,
            "_google_error_types",
            return_value={"refresh_error": StubRefreshError, "http_error": StubHttpError},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_token(self, payload=None):
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(
            json.dumps(payload if payload is not None else {"refresh_token": "stored"}),
            encoding="utf-8",
        )

    def resolve(self, *, stored=None, refresh=None, consent=None):
        """継ぎ目を差し替えて `_get_credentials()` を呼ぶ。"""
        progress = drive_upload.Progress()
        with mock.patch.multiple(
            drive_upload,
            _credentials_from_payload=mock.DEFAULT,
            _refresh_credentials=mock.DEFAULT,
            _run_oauth_consent=mock.DEFAULT,
        ) as patched:
            if stored is None:
                patched["_credentials_from_payload"].side_effect = AssertionError(
                    "保存トークンが無いのに復元を試みている"
                )
            else:
                patched["_credentials_from_payload"].return_value = stored
            if refresh is not None:
                patched["_refresh_credentials"].side_effect = refresh
            patched["_run_oauth_consent"].return_value = consent or FakeCredentials()
            credentials = drive_upload._get_credentials(progress)
            return credentials, patched, progress

    def test_a_valid_stored_token_skips_the_consent_flow(self):
        self.write_token()
        stored = FakeCredentials(valid=True)
        credentials, patched, _ = self.resolve(stored=stored)

        self.assertIs(credentials, stored)
        patched["_run_oauth_consent"].assert_not_called()
        patched["_refresh_credentials"].assert_not_called()

    def test_an_expired_token_is_refreshed_rather_than_re_consented(self):
        self.write_token()
        stored = FakeCredentials(valid=False)
        credentials, patched, _ = self.resolve(stored=stored)

        self.assertIs(credentials, stored)
        patched["_refresh_credentials"].assert_called_once_with(stored)
        patched["_run_oauth_consent"].assert_not_called()

    def test_a_refresh_error_drops_the_token_and_falls_back_to_consent(self):
        self.write_token()
        fresh = FakeCredentials(token="fresh")
        credentials, patched, _ = self.resolve(
            stored=FakeCredentials(valid=False),
            refresh=StubRefreshError("revoked"),
            consent=fresh,
        )

        self.assertIs(credentials, fresh)
        patched["_run_oauth_consent"].assert_called_once()
        # 失効したトークンは消え、同意で得た新しいものが書かれていること。
        self.assertEqual(drive_upload._read_token_payload()["token"], "fresh")

    def test_an_unrelated_refresh_failure_is_not_swallowed(self):
        """ネットワーク断で無言に再同意（ブラウザを開く）へ落ちないこと。"""
        self.write_token()
        with self.assertRaises(ConnectionError):
            self.resolve(stored=FakeCredentials(valid=False), refresh=ConnectionError("down"))

    def test_missing_scopes_force_a_new_consent(self):
        self.write_token()
        narrow = FakeCredentials(scopes=["https://www.googleapis.com/auth/drive.file"])
        fresh = FakeCredentials(token="fresh")
        credentials, patched, _ = self.resolve(stored=narrow, consent=fresh)

        self.assertIs(credentials, fresh)
        patched["_run_oauth_consent"].assert_called_once()

    def test_no_stored_token_goes_straight_to_consent(self):
        fresh = FakeCredentials(token="fresh")
        credentials, patched, _ = self.resolve(consent=fresh)

        self.assertIs(credentials, fresh)
        patched["_run_oauth_consent"].assert_called_once()
        self.assertEqual(drive_upload._read_token_payload()["token"], "fresh")

    def test_a_corrupt_stored_token_falls_back_to_consent(self):
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text("not json", encoding="utf-8")
        fresh = FakeCredentials(token="fresh")
        credentials, patched, _ = self.resolve(consent=fresh)

        self.assertIs(credentials, fresh)
        patched["_run_oauth_consent"].assert_called_once()

    def test_a_token_without_a_refresh_token_cannot_be_refreshed(self):
        self.write_token()
        stored = FakeCredentials(valid=False, refresh_token=None)
        fresh = FakeCredentials(token="fresh")
        credentials, patched, _ = self.resolve(stored=stored, consent=fresh)

        self.assertIs(credentials, fresh)
        patched["_refresh_credentials"].assert_not_called()
        patched["_run_oauth_consent"].assert_called_once()

    def test_the_stage_is_authenticating_before_anything_else(self):
        self.write_token()
        _, _, progress = self.resolve(stored=FakeCredentials())
        self.assertEqual(progress.snapshot()["stage"], "authenticating")


class OauthConsentTests(DriveUploadTestCase):
    """`_run_oauth_consent()` のタイムアウト変換（実測 google-auth-oauthlib 1.4.0）。

    `run_local_server(timeout_seconds=...)` は `WSGITimeoutError`（`AttributeError` の
    サブクラス）を投げる。裸の `except AttributeError` にしないことを確かめる。
    """

    class StubWSGITimeoutError(AttributeError):
        pass

    def build_flow_module(self, *, run, with_timeout_class=True):
        flow_module = types.ModuleType("google_auth_oauthlib.flow")
        installed_app_flow = mock.Mock()
        installed_app_flow.from_client_secrets_file.return_value = mock.Mock(
            run_local_server=(
                mock.Mock(side_effect=run)
                if isinstance(run, BaseException)
                else mock.Mock(return_value=run)
            )
        )
        flow_module.InstalledAppFlow = installed_app_flow
        if with_timeout_class:
            flow_module.WSGITimeoutError = self.StubWSGITimeoutError
        return flow_module

    def consent(self, flow_module):
        """`import google_auth_oauthlib.flow as ...` は**親パッケージも**引くので両方置く。"""
        package = types.ModuleType("google_auth_oauthlib")
        package.flow = flow_module
        progress = drive_upload.Progress()
        modules = {"google_auth_oauthlib": package, "google_auth_oauthlib.flow": flow_module}
        with mock.patch.dict(sys.modules, modules):
            return drive_upload._run_oauth_consent(progress), progress

    def test_a_wsgi_timeout_becomes_a_consent_timeout(self):
        module = self.build_flow_module(run=self.StubWSGITimeoutError("timed out"))
        with self.assertRaises(drive_upload.DriveUploadError) as caught:
            self.consent(module)
        self.assertEqual(caught.exception.kind, "consent_timeout")

    def test_an_unrelated_attribute_error_is_not_treated_as_a_timeout(self):
        module = self.build_flow_module(run=AttributeError("something else"))
        with self.assertRaises(AttributeError):
            self.consent(module)

    def test_a_missing_timeout_class_falls_back_to_the_return_value(self):
        """クラス名に依存しない代替経路（戻り値が None ならタイムアウト扱い）。"""
        module = self.build_flow_module(run=None, with_timeout_class=False)
        with self.assertRaises(drive_upload.DriveUploadError) as caught:
            self.consent(module)
        self.assertEqual(caught.exception.kind, "consent_timeout")

    def test_a_successful_consent_returns_the_credentials_and_sets_the_stage(self):
        expected = FakeCredentials()
        module = self.build_flow_module(run=expected)
        credentials, progress = self.consent(module)

        self.assertIs(credentials, expected)
        self.assertEqual(progress.snapshot()["stage"], "waiting_for_consent")
        flow = module.InstalledAppFlow.from_client_secrets_file.return_value
        self.assertEqual(
            flow.run_local_server.call_args.kwargs["timeout_seconds"],
            drive_upload.OAUTH_CONSENT_TIMEOUT_SECONDS,
        )


# ---------------------------------------------------------------------------
# 6. トークン永続化
# ---------------------------------------------------------------------------


class TokenPersistenceTests(DriveUploadTestCase):
    def test_the_written_token_is_unreadable_by_other_users(self):
        """umask に依存しない形で見る（`== 0o600` は umask 次第で偽陽性になる）。"""
        drive_upload._write_token_payload({"refresh_token": "secret"})
        self.assertEqual(os.stat(self.token_path).st_mode & 0o077, 0)

    def test_an_unchanged_payload_is_not_rewritten(self):
        payload = {"refresh_token": "secret", "token": "access"}
        drive_upload._write_token_payload(payload)
        with mock.patch.object(os, "replace") as replace:
            drive_upload._write_token_payload(payload)
        replace.assert_not_called()

    def test_a_changed_payload_replaces_the_file_atomically(self):
        drive_upload._write_token_payload({"refresh_token": "first"})
        drive_upload._write_token_payload({"refresh_token": "second"})

        self.assertEqual(drive_upload._read_token_payload(), {"refresh_token": "second"})
        leftovers = [path.name for path in self.token_path.parent.iterdir() if ".tmp." in path.name]
        self.assertEqual(leftovers, [], "tmp ファイルが残っている")

    def test_the_parent_directory_is_created_on_demand(self):
        """初回起動時は `outputs/` がまだ無い（`server.main()` より先に呼ばれうる）。"""
        nested = self.root / "fresh" / "outputs" / "drive_auth.json"
        self.assertFalse(nested.parent.exists())
        with mock.patch.object(drive_upload, "TOKEN_PATH", nested):
            drive_upload._write_token_payload({"refresh_token": "secret"})
            self.assertTrue(nested.is_file())

    def test_invalidate_removes_the_token_and_tolerates_a_missing_file(self):
        drive_upload._write_token_payload({"refresh_token": "secret"})
        drive_upload._invalidate_token()
        self.assertFalse(self.token_path.exists())
        drive_upload._invalidate_token()  # 2 回目でも壊れない

    def test_reading_a_non_object_payload_yields_none(self):
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text("[1, 2]", encoding="utf-8")
        self.assertIsNone(drive_upload._read_token_payload())


class TokenLocationTests(unittest.TestCase):
    """保存先の定数そのものの検証（差し替えないので、読むだけで書き込みはしない）。"""

    def test_the_token_lives_in_the_app_data_dir(self):
        self.assertEqual(drive_upload.TOKEN_PATH, paths.app_data_dir() / "drive_auth.json")

    def test_the_legacy_path_is_the_old_outputs_location(self):
        self.assertEqual(
            drive_upload._LEGACY_TOKEN_PATH,
            drive_upload.PROJECT_ROOT / "outputs" / "drive_auth.json",
        )

    def test_the_client_secret_did_not_move(self):
        """`drive_client_secret.json` はリポジトリ配下のまま（移行対象外）。"""
        self.assertEqual(
            drive_upload.CLIENT_SECRET_PATH,
            drive_upload.PROJECT_ROOT / "local_raw" / "drive_client_secret.json",
        )


class MigrationTests(unittest.TestCase):
    """`outputs/drive_auth.json` → アプリデータディレクトリへの一度きりの移行。

    ⚠️ **`TOKEN_PATH` と `_LEGACY_TOKEN_PATH` の両方**を tmpdir へ差し替えること。
    片方だけだと、テストを走らせるだけで実データ（リポジトリの `outputs/drive_auth.json`、
    または実ユーザーの `~/Library/Application Support/<identifier>/drive_auth.json`）を
    読み書き・削除してしまう。中身は生の OAuth リフレッシュトークンなので、消すと
    ユーザーは再サインインするまで Drive へ送れなくなる。`DriveUploadTestCase` は
    `TOKEN_PATH` しか差し替えないので、ここでは継承せず独立した `setUp` で両方を押さえる。
    """

    PAYLOAD = {"refresh_token": "secret", "token": "access"}

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # macOS の /var → /private/var シンボリックリンク対策に resolve する。
        self.root = Path(temporary.name).resolve()
        self.legacy_path = self.root / "outputs" / "drive_auth.json"
        self.token_path = self.root / "appdata" / "drive_auth.json"
        patcher = mock.patch.multiple(
            drive_upload,
            TOKEN_PATH=self.token_path,
            _LEGACY_TOKEN_PATH=self.legacy_path,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_legacy(self, payload=None):
        payload = self.PAYLOAD if payload is None else payload
        self.legacy_path.parent.mkdir(parents=True, exist_ok=True)
        self.legacy_path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def test_the_legacy_token_is_moved_to_the_new_location(self):
        payload = self.write_legacy()
        self.assertTrue(drive_upload.ensure_migrated())
        self.assertEqual(drive_upload._read_token_payload(), payload)
        self.assertFalse(self.legacy_path.exists(), "移行後は旧トークンを残さない")

    def test_the_migrated_token_is_unreadable_by_other_users(self):
        """umask に依存しない形で見る（`== 0o600` は umask 次第で偽陽性になる）。

        長期の認証情報なので、移行先も `_write_token_payload` と同じ 0600 でなければ
        ならない（旧パスが緩い権限だった場合に、それを引き継いでしまわないこと）。
        """
        self.legacy_path.parent.mkdir(parents=True, exist_ok=True)
        self.legacy_path.write_text(json.dumps(self.PAYLOAD), encoding="utf-8")
        self.legacy_path.chmod(0o644)

        self.assertTrue(drive_upload.ensure_migrated())
        self.assertEqual(os.stat(self.token_path).st_mode & 0o077, 0)

    def test_nothing_happens_without_a_legacy_token(self):
        self.assertFalse(self.legacy_path.exists())
        self.assertFalse(drive_upload.ensure_migrated())
        self.assertFalse(self.token_path.exists())
        self.assertFalse(self.token_path.parent.exists(), "空振りでディレクトリを作らない")

    def test_a_second_call_is_a_no_op(self):
        """冪等性: 起動のたびに呼ばれるので、2 回目以降は新パスを書き換えない。"""
        self.write_legacy()
        self.assertTrue(drive_upload.ensure_migrated())

        # 旧パスに別のトークンが復活しても、新パスがある限り上書きしない。
        self.write_legacy({"refresh_token": "stale", "token": "old"})
        self.assertFalse(drive_upload.ensure_migrated())
        self.assertEqual(drive_upload._read_token_payload(), self.PAYLOAD)

    def test_a_failed_migration_warns_instead_of_going_silent(self):
        """旧トークンはあるのに移行できなかった＝再認証が必要になるケースを黙らせない。

        `paths.migrate_legacy_file()` の `False` は「対象なし」と「失敗」を区別しないので、
        呼び出し側のこの検出が無いと、未認証のまま静かに起動してしまう。失敗は本物を使う
        （移行先の親に**普通のファイル**を置いて `mkdir` を ENOTDIR にする）。

        警告文にトークンの中身を載せないこと（パスだけ）も併せて見る。
        """
        self.write_legacy()
        self.token_path.parent.write_text("not a directory", encoding="utf-8")

        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            self.assertFalse(drive_upload.ensure_migrated())
        warning = stream.getvalue()
        self.assertIn("[migrate]", warning)
        self.assertIn(str(self.legacy_path), warning)
        self.assertIn(str(self.token_path), warning)
        self.assertNotIn(self.PAYLOAD["refresh_token"], warning)

    def test_a_no_op_migration_stays_quiet(self):
        """移行対象が無いだけの `False` で警告を出さない（毎起動の誤報になる）。"""
        for label, prepare in (
            ("旧トークンが無い", lambda: None),
            ("新パスに既にある（冪等）", self.migrate_once),
        ):
            with self.subTest(label):
                prepare()
                stream = io.StringIO()
                with contextlib.redirect_stderr(stream):
                    drive_upload.ensure_migrated()
                self.assertEqual(stream.getvalue(), "")

    def migrate_once(self):
        self.write_legacy()
        self.assertTrue(drive_upload.ensure_migrated())
        # 旧トークンを復活させて「両方ある」状態を作る（冪等性で False になる経路）。
        self.write_legacy({"refresh_token": "stale", "token": "old"})


# ---------------------------------------------------------------------------
# 7. 同期検証
# ---------------------------------------------------------------------------


class StartValidationTests(DriveUploadTestCase):
    def assert_rejected(self):
        with self.assertRaises(ValueError):
            drive_upload.start_drive_upload(BATCH)

    def test_a_missing_client_secret_is_rejected(self):
        self.make_batch()
        self.client_secret_path.unlink()
        self.assert_rejected()
        self.assertFalse(drive_upload.is_drive_configured())

    def test_a_malformed_client_secret_is_rejected(self):
        self.make_batch()
        self.write_client_secret("{ not json")
        self.assert_rejected()
        self.assertFalse(drive_upload.is_drive_configured())

    def test_a_client_secret_without_the_installed_key_is_rejected(self):
        """Web アプリ種別のJSONを渡された場合（`installed` ではなく `web`）。"""
        self.make_batch()
        self.write_client_secret({"web": {"client_id": "x"}})
        self.assert_rejected()
        self.assertFalse(drive_upload.is_drive_configured())

    def test_a_missing_export_directory_is_rejected(self):
        self.assert_rejected()

    def test_an_empty_export_directory_is_rejected(self):
        self.make_batch(files={})
        self.assert_rejected()

    def test_a_directory_holding_only_temporary_files_is_rejected(self):
        """`iterdir()` で判定していると通ってしまい、0 件アップロードが走る。"""
        self.make_batch(files={"DSC00001.JPG.tmp": b"partial"})
        self.assert_rejected()

    def test_an_invalid_batch_name_is_rejected(self):
        for name in ("", "../evil", 42, None):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    drive_upload.start_drive_upload(name)

    def test_a_valid_client_secret_reports_configured(self):
        self.assertTrue(drive_upload.is_drive_configured())
        self.assertEqual(
            drive_upload.get_drive_upload_state(), {"status": "idle", "configured": True}
        )


# ---------------------------------------------------------------------------
# 8. 二重起動防止 / Thread.start() 失敗時の巻き戻し
# ---------------------------------------------------------------------------


class StartDriveUploadThreadTests(DriveUploadTestCase):
    """`tests/test_analysis.py::StartAnalysisThreadFailureTest` と同型。"""

    def setUp(self):
        super().setUp()
        self.make_batch()

    def test_the_running_flag_is_reset_and_the_error_propagates_when_start_fails(self):
        with mock.patch.object(
            threading.Thread, "start", side_effect=RuntimeError("can't start new thread")
        ):
            with self.assertRaises(RuntimeError):
                drive_upload.start_drive_upload(BATCH)

        with drive_upload._STATE_LOCK:
            self.assertFalse(
                drive_upload._RUNNING,
                "start() 失敗後も _RUNNING が True のままでは GET /api/drive-upload が固着する",
            )

    def test_a_subsequent_call_is_not_blocked_by_already_running(self):
        with mock.patch.object(
            threading.Thread, "start", side_effect=RuntimeError("can't start new thread")
        ):
            with self.assertRaises(RuntimeError):
                drive_upload.start_drive_upload(BATCH)

        with mock.patch.object(threading.Thread, "start", return_value=None):
            self.assertEqual(drive_upload.start_drive_upload(BATCH), "started")

    def test_a_second_start_while_running_is_refused(self):
        with mock.patch.object(threading.Thread, "start", return_value=None):
            self.assertEqual(drive_upload.start_drive_upload(BATCH), "started")
            self.assertEqual(drive_upload.start_drive_upload(BATCH), "already_running")

    def test_the_running_state_reports_the_batch_and_progress(self):
        with mock.patch.object(threading.Thread, "start", return_value=None):
            drive_upload.start_drive_upload(BATCH)

        state = drive_upload.get_drive_upload_state()
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["batch_name"], BATCH)
        self.assertEqual(state["progress"]["stage"], "authenticating")


class JobStateTests(DriveUploadTestCase):
    """`_run_upload_job()` が状態機械を固着させず、契約どおりの形を残すこと。"""

    def setUp(self):
        super().setUp()
        self.make_batch(files={"DSC00001.JPG": b"jpeg"})
        with drive_upload._STATE_LOCK:
            drive_upload._CURRENT_BATCH = BATCH
            drive_upload._RUNNING = True

    def test_a_successful_job_leaves_a_done_payload(self):
        result = {
            "uploaded": 1,
            "total": 1,
            "skipped": [],
            "folder_url": "https://drive.google.com/drive/folders/created-1",
        }
        with mock.patch.object(drive_upload, "_run_upload", return_value=result):
            drive_upload._run_upload_job(drive_upload.Progress(), BATCH)

        self.assertEqual(
            drive_upload.get_drive_upload_state(), {"status": "done", "batch_name": BATCH, **result}
        )

    def test_a_failing_job_leaves_a_classified_error_and_clears_running(self):
        with mock.patch.object(
            drive_upload, "_run_upload", side_effect=ConnectionError("offline")
        ):
            drive_upload._run_upload_job(drive_upload.Progress(), BATCH)

        self.assertEqual(
            drive_upload.get_drive_upload_state(),
            {
                "status": "error",
                "batch_name": BATCH,
                "error": drive_upload.NETWORK_MESSAGE,
                "error_kind": "network",
            },
        )
        with drive_upload._STATE_LOCK:
            self.assertFalse(drive_upload._RUNNING)

    def test_a_failure_inside_the_classifier_still_terminates_the_job(self):
        with mock.patch.object(drive_upload, "_run_upload", side_effect=RuntimeError("boom")):
            with mock.patch.object(
                drive_upload, "_classify_error", side_effect=RuntimeError("classifier broke")
            ):
                with mock.patch.object(drive_upload.traceback, "print_exc"):
                    drive_upload._run_upload_job(drive_upload.Progress(), BATCH)

        self.assertEqual(drive_upload.get_drive_upload_state()["error_kind"], "unknown")
        with drive_upload._STATE_LOCK:
            self.assertFalse(drive_upload._RUNNING)


# ---------------------------------------------------------------------------
# 9. エラー分類
# ---------------------------------------------------------------------------


class ClassifyErrorTests(DriveUploadTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(
            drive_upload,
            "_google_error_types",
            return_value={"refresh_error": StubRefreshError, "http_error": StubHttpError},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # `unknown` 経路は traceback をサーバログへ出す。テスト出力を汚さないよう黙らせる。
        trace_patcher = mock.patch.object(drive_upload.traceback, "print_exc")
        self.print_exc = trace_patcher.start()
        self.addCleanup(trace_patcher.stop)

    def classify(self, error):
        """本番と同じく「例外処理中」に呼ぶ（`traceback.print_exc()` の前提を揃える）。"""
        try:
            raise error
        except Exception as raised:
            return drive_upload._classify_error(raised)

    def write_token(self):
        drive_upload._write_token_payload({"refresh_token": "secret"})

    def test_a_refresh_error_is_auth_revoked_and_drops_the_token(self):
        self.write_token()
        self.assertEqual(
            self.classify(StubRefreshError("revoked")),
            ("auth_revoked", drive_upload.AUTH_REVOKED_MESSAGE),
        )
        self.assertFalse(self.token_path.exists(), "失効時はトークンを捨てること")

    def test_a_401_is_auth_revoked_and_drops_the_token(self):
        self.write_token()
        self.assertEqual(
            self.classify(StubHttpError(401)), ("auth_revoked", drive_upload.AUTH_REVOKED_MESSAGE)
        )
        self.assertFalse(self.token_path.exists())

    def test_a_storage_quota_403_is_quota(self):
        """コードは `error_details` 側（本物と同じ場所）から読むこと。"""
        self.assertEqual(
            self.classify(
                StubHttpError(
                    403,
                    reason="The user's Drive storage quota has been exceeded.",
                    codes=["storageQuotaExceeded"],
                )
            ),
            ("quota", drive_upload.QUOTA_MESSAGE),
        )

    def test_a_403_is_not_quota_when_only_the_human_reason_mentions_it(self):
        """⚠️ この回帰そのものを止めるテスト。

        本物の `HttpError.reason` は `error.message`（人間向け文言）で、
        `storageQuotaExceeded` のようなコードは入らない。`reason` を部分一致で見る
        実装は、テストのフェイクが本物と違う場所にコードを置いていれば緑になるが、
        本番では**絶対に発火しない**。逆向き（`reason` にだけコードらしき文字列が
        あり `error_details` には無い）を unknown に落とすことで、`reason` への
        部分一致フォールバックが後から戻ってきたら落ちるようにする。
        """
        self.assertEqual(
            self.classify(StubHttpError(403, reason="storageQuotaExceeded")),
            ("unknown", drive_upload.UNKNOWN_MESSAGE),
        )

    def test_a_403_throttling_code_is_rate_limit(self):
        """Drive のスロットリングは 429 ではなく **403** で返る。429 と同じ案内に寄せる。"""
        for code in ("userRateLimitExceeded", "rateLimitExceeded"):
            with self.subTest(code=code):
                self.assertEqual(
                    self.classify(
                        StubHttpError(403, reason="User Rate Limit Exceeded", codes=[code])
                    ),
                    ("rate_limit", drive_upload.RATE_LIMIT_MESSAGE),
                )

    def test_reason_codes_survive_the_other_error_details_shapes(self):
        """`error_details` は本文しだいで str にも「余計な要素混じりの list」にもなる。

        str 形は**走査しない**こと（人間向け文言なので部分一致は誤診の元）。
        """
        as_string = StubHttpError(403)
        as_string.error_details = "The user's storageQuotaExceeded quota"
        self.assertEqual(self.classify(as_string), ("unknown", drive_upload.UNKNOWN_MESSAGE))

        mixed = StubHttpError(403)
        mixed.error_details = [
            "not-a-dict",
            {"domain": "usageLimits"},  # `reason` 欠け
            {"reason": "rateLimitExceeded"},
        ]
        self.assertEqual(self.classify(mixed), ("rate_limit", drive_upload.RATE_LIMIT_MESSAGE))

    def test_429_and_5xx_are_rate_limit(self):
        for status in (429, 500, 502, 503):
            with self.subTest(status=status):
                self.assertEqual(
                    self.classify(StubHttpError(status)),
                    ("rate_limit", drive_upload.RATE_LIMIT_MESSAGE),
                )

    def test_a_missing_dependency_is_reported_as_such(self):
        self.assertEqual(
            self.classify(ModuleNotFoundError("No module named 'googleapiclient'")),
            ("dependencies_missing", drive_upload.DEPENDENCIES_MISSING_MESSAGE),
        )

    def test_network_failures_are_reported_as_network(self):
        """通信障害として名指ししたクラスが確かに `network` へ落ちること。

        ⚠️ `TimeoutError` を落とさないこと。3.10+ ではこれが `socket.timeout` の
        エイリアスで、`OSError` 派生だが **`ConnectionError` 派生ではない**。
        `ConnectionError` だけを見る実装だとアップロード中の接続タイムアウトが
        `unknown` に落ちる（「`OSError` は全部 network」だった頃より後退する）。
        """
        errors = (
            ConnectionError("refused"),
            ConnectionResetError("reset by peer"),
            TimeoutError("slow"),
            socket.gaierror("name resolution failed"),
            socket.herror("host lookup failed"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(
                    self.classify(error), ("network", drive_upload.NETWORK_MESSAGE)
                )

    def test_timeout_error_is_not_a_connection_error_subclass(self):
        """上のテストが `TimeoutError` を個別に挙げている理由を、前提ごと固定する。

        この前提が崩れた（= `ConnectionError` に含まれるようになった）なら、
        実装の名指しは冗長になる。崩れていない限り外してはいけない。
        """
        self.assertTrue(issubclass(TimeoutError, OSError))
        self.assertFalse(issubclass(TimeoutError, ConnectionError))

    def test_local_io_failures_are_not_reported_as_network(self):
        """ローカル I/O 失敗（フォルダ消失・書込失敗）は `OSError` でも `network` ではない。

        「ネットワークに接続できませんでした」と案内すると原因究明を誤らせる。
        `unknown` へ寄せ、詳細はサーバログにだけ残す。
        """
        for error in (
            FileNotFoundError("no such export folder"),
            PermissionError("read-only volume"),
            IsADirectoryError("expected a file"),
            OSError("No space left on device"),
        ):
            with self.subTest(error=type(error).__name__):
                self.print_exc.reset_mock()
                self.assertEqual(
                    self.classify(error), ("unknown", drive_upload.UNKNOWN_MESSAGE)
                )
                self.assertTrue(
                    self.print_exc.called, "分類できない例外はサーバログへ残すこと"
                )

    def test_transport_failures_that_are_not_os_errors_are_network(self):
        """`OSError` 派生でない本物の通信障害まで届くこと。

        `google.auth.exceptions.TransportError` は `GoogleAuthError` → `Exception` 派生、
        `httplib2.ServerNotFoundError`（DNS 失敗）は `HttpLib2Error` → `Exception` 派生で、
        どちらも `OSError` でも `socket.gaierror` でもない。名指ししないと `unknown` に落ちる。
        """
        with mock.patch.object(
            drive_upload,
            "_google_error_types",
            return_value={
                "refresh_error": StubRefreshError,
                "http_error": StubHttpError,
                "transport_error": StubTransportError,
                "server_not_found": StubServerNotFoundError,
            },
        ):
            for error in (
                StubTransportError("connection failed"),
                StubServerNotFoundError("Unable to find the server at www.googleapis.com"),
            ):
                with self.subTest(error=type(error).__name__):
                    self.assertEqual(
                        self.classify(error), ("network", drive_upload.NETWORK_MESSAGE)
                    )

    def test_transport_classes_that_cannot_be_resolved_are_skipped(self):
        """引けなかったクラス（`None`）で isinstance を呼ばず、無関係な例外も誤分類しないこと。"""
        with mock.patch.object(
            drive_upload,
            "_google_error_types",
            return_value={
                "refresh_error": None,
                "http_error": None,
                "transport_error": None,
                "server_not_found": None,
            },
        ):
            self.assertEqual(
                self.classify(ConnectionError("refused")),
                ("network", drive_upload.NETWORK_MESSAGE),
            )
            self.assertEqual(
                self.classify(RuntimeError("boom")), ("unknown", drive_upload.UNKNOWN_MESSAGE)
            )

    def test_a_drive_upload_error_keeps_its_own_kind_and_message(self):
        self.assertEqual(
            self.classify(drive_upload.DriveUploadError("consent_timeout", "時間切れです。")),
            ("consent_timeout", "時間切れです。"),
        )

    def test_an_unrelated_exception_falls_back_to_unknown(self):
        for error in (AttributeError("no attribute"), RuntimeError("boom"), KeyError("k")):
            with self.subTest(error=type(error).__name__):
                kind, message = self.classify(error)
                self.assertEqual((kind, message), ("unknown", drive_upload.UNKNOWN_MESSAGE))
        self.assertTrue(self.print_exc.called, "分類できない例外はサーバログへ残すこと")

    def test_an_http_error_with_an_unreadable_status_falls_back_to_unknown(self):
        """`Mock` は `status_code` に Mock を返す。`int()` で落ちずに unknown へ寄せること。"""
        error = mock.Mock(spec=StubHttpError)
        self.assertIsInstance(error, StubHttpError)
        self.assertEqual(
            self.classify(error), ("unknown", drive_upload.UNKNOWN_MESSAGE)
        )

    def test_an_unclassified_http_status_falls_back_to_unknown(self):
        for status in (400, 404, 409):
            with self.subTest(status=status):
                self.assertEqual(
                    self.classify(StubHttpError(status)),
                    ("unknown", drive_upload.UNKNOWN_MESSAGE),
                )

    def test_a_403_for_another_reason_is_not_reported_as_a_full_disk(self):
        """403 は権限不足でも出る。理由を見ずに quota と言うと案内が嘘になる。"""
        for code in ("insufficientFilePermissions", "appNotAuthorizedToFile"):
            with self.subTest(code=code):
                self.assertEqual(
                    self.classify(StubHttpError(403, codes=[code])),
                    ("unknown", drive_upload.UNKNOWN_MESSAGE),
                )
        self.assertEqual(
            self.classify(StubHttpError(403)),
            ("unknown", drive_upload.UNKNOWN_MESSAGE),
            "コードがまったく読めない 403 も quota と決めつけないこと",
        )

    def test_the_message_never_leaks_the_raw_response(self):
        """`HttpError.__str__` は要求URIとレスポンス本文を含む。文言へ混ぜないこと。

        分類済み（429 / 5xx など）と**未分類**の両方を見る。未分類の枝は
        「とりあえず例外を文字列化して返す」に退化しやすい。
        """
        leaky = "quotaExceeded for /drive/v3/files?uploadType=resumable&token=ya29.SECRET"
        for status in (429, 500, 404):
            with self.subTest(status=status):
                _, message = self.classify(StubHttpError(status, reason=leaky))
                self.assertNotIn("drive/v3", message)
                self.assertNotIn("uploadType", message)
                self.assertNotIn("ya29.", message)
                self.assertNotIn("stub http error", message)

    def test_classification_still_works_without_the_google_libraries(self):
        """`_google_error_types()` が両方 None でも分類が壊れないこと。"""
        with mock.patch.object(
            drive_upload,
            "_google_error_types",
            return_value={"refresh_error": None, "http_error": None},
        ):
            self.assertEqual(
                self.classify(ConnectionError("offline")),
                ("network", drive_upload.NETWORK_MESSAGE),
            )
            self.assertEqual(
                self.classify(StubHttpError(401)), ("unknown", drive_upload.UNKNOWN_MESSAGE)
            )


class LazyImportSeamTests(DriveUploadTestCase):
    """遅延 import の継ぎ目そのものを検証する。

    他のテストはこの 4 関数を差し替えて呼ぶので、**本体の中身はここでしか検証されない**。
    `.venv` に google 系ライブラリは無いため、`sys.modules` へ偽のパッケージ木を差し込む
    （`import a.b.c` は親パッケージも辿るので、途中の階層まで用意する必要がある）。
    """

    @staticmethod
    def package_tree(dotted_name, leaf):
        """`a.b.c` の形で親を辿れるモジュール群を組み立てる。"""
        parts = dotted_name.split(".")
        modules = {dotted_name: leaf}
        child = leaf
        for index in range(len(parts) - 1, 0, -1):
            parent_name = ".".join(parts[:index])
            parent = types.ModuleType(parent_name)
            setattr(parent, parts[index], child)
            modules[parent_name] = parent
            child = parent
        return modules

    def test_restoring_credentials_does_not_force_the_requested_scopes(self):
        """`scopes` を渡すと `has_scopes()` が常に True になり、スコープ不足を検出できない。"""
        credentials_class = mock.Mock()
        leaf = types.ModuleType("google.oauth2.credentials")
        leaf.Credentials = credentials_class
        payload = {"refresh_token": "stored", "scopes": ["https://www.googleapis.com/auth/drive"]}

        with mock.patch.dict(sys.modules, self.package_tree("google.oauth2.credentials", leaf)):
            drive_upload._credentials_from_payload(payload)

        credentials_class.from_authorized_user_info.assert_called_once_with(payload)

    def test_refreshing_uses_a_transport_request(self):
        leaf = types.ModuleType("google.auth.transport.requests")
        leaf.Request = mock.Mock(return_value="transport-request")
        credentials = mock.Mock()

        with mock.patch.dict(
            sys.modules, self.package_tree("google.auth.transport.requests", leaf)
        ):
            drive_upload._refresh_credentials(credentials)

        credentials.refresh.assert_called_once_with("transport-request")

    def test_the_service_is_built_without_the_discovery_cache(self):
        """`cache_discovery=True`（既定）は oauth2client 由来のキャッシュを引き込み警告を出す。"""
        leaf = types.ModuleType("googleapiclient.discovery")
        leaf.build = mock.Mock(return_value="drive-service")
        credentials = FakeCredentials()

        with mock.patch.dict(sys.modules, self.package_tree("googleapiclient.discovery", leaf)):
            service = drive_upload._build_service(credentials)

        self.assertEqual(service, "drive-service")
        leaf.build.assert_called_once_with(
            "drive", "v3", credentials=credentials, cache_discovery=False
        )

    def test_media_uploads_are_resumable(self):
        leaf = types.ModuleType("googleapiclient.http")
        leaf.MediaFileUpload = mock.Mock(return_value="media")
        path = self.make_batch() / "DSC00001.JPG"

        with mock.patch.dict(sys.modules, self.package_tree("googleapiclient.http", leaf)):
            self.assertEqual(drive_upload._media_file_upload(path), "media")

        leaf.MediaFileUpload.assert_called_once_with(str(path), resumable=True)


class EscapeQueryValueTests(unittest.TestCase):
    def test_quotes_and_backslashes_are_escaped(self):
        self.assertEqual(drive_upload._escape_query_value("a'b"), "a\\'b")
        self.assertEqual(drive_upload._escape_query_value("a\\b"), "a\\\\b")
        self.assertEqual(drive_upload._escape_query_value("plain-name_1"), "plain-name_1")


# ---------------------------------------------------------------------------
# 10. google 系ライブラリ非依存の import
# ---------------------------------------------------------------------------


class ImportWithoutGoogleLibrariesTest(unittest.TestCase):
    """google 系ライブラリが1つも無くても `import local_raw.drive_upload` が成功すること。

    ⚠️ このテストの前に別のテストが `local_raw.drive_upload` を import 済みだと
    `sys.modules` のキャッシュが返り、**何も検証せずに無条件で pass する**（偽陽性）。
    実行順に依存しないよう、明示的に `sys.modules` から外してから再 import し、
    「本当に再評価された」ことを `assertIsNot` で確かめる。外した module オブジェクトは
    cleanup で必ず戻す（`_STATE_LOCK` を直接触る他のテストが別インスタンスを掴まないため）。
    """

    #: `sys.modules[name] = None` は import 時に ImportError を起こす。
    BLOCKED = (
        "google",
        "google.auth",
        "google.auth.exceptions",
        "google.auth.transport",
        "google.auth.transport.requests",
        "google.oauth2",
        "google.oauth2.credentials",
        "google_auth_oauthlib",
        "google_auth_oauthlib.flow",
        "googleapiclient",
        "googleapiclient.discovery",
        "googleapiclient.errors",
        "googleapiclient.http",
        # httplib2 は google 系ではないが `_google_error_types()` が引く先の一つ。
        # 塞がないと「実行環境に httplib2 が入っているか」で下の assert が揺れる。
        "httplib2",
        "httplib2.error",
    )

    MODULE_NAME = "local_raw.drive_upload"

    def restore(self, original):
        import local_raw

        if original is None:
            sys.modules.pop(self.MODULE_NAME, None)
            return
        sys.modules[self.MODULE_NAME] = original
        # 再 import は親パッケージの属性も差し替えるので、そちらも戻す。
        local_raw.drive_upload = original

    def test_the_module_imports_and_reports_state_without_google_libraries(self):
        original = sys.modules.get(self.MODULE_NAME)
        self.assertIsNotNone(original, "ファイル先頭の import で読み込まれているはず")
        self.addCleanup(self.restore, original)
        sys.modules.pop(self.MODULE_NAME, None)

        with mock.patch.dict(sys.modules, dict.fromkeys(self.BLOCKED, None)):
            module = importlib.import_module(self.MODULE_NAME)
            self.assertIsNot(module, original, "キャッシュが返っていて何も検証できていない")

            # 遅延 import はすべて失敗するが、例外分類は動くこと。
            self.assertEqual(
                module._google_error_types(),
                {
                    "refresh_error": None,
                    "http_error": None,
                    "transport_error": None,
                    "server_not_found": None,
                },
            )
            with mock.patch.object(
                module, "CLIENT_SECRET_PATH", Path("/nonexistent/drive_client_secret.json")
            ):
                self.assertEqual(
                    module.get_drive_upload_state(), {"status": "idle", "configured": False}
                )


if __name__ == "__main__":
    unittest.main()
