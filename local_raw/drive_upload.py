"""書き出し済みバッチを Google Drive の「GeoFrame」フォルダへアップロードする。

契約: docs/API_CONTRACT.md §1（`POST /api/drive-upload`・`GET /api/drive-upload`）

方針:

- 送る側の対象は `outputs/export/<batch_name>/` の**直下にあるファイル**のみ
  （`manifest.json` を含む。`.tmp` は書き込み途中なので除外、サブディレクトリは対象外）。
- 置き先は Drive の `GeoFrame` フォルダ**そのものの中**で、バッチごとのサブフォルダは
  **作らない**。消費側（GeoFrame 本体アプリ）の Drive インデックス処理が `GeoFrame`
  フォルダを**非再帰**で走査するため、サブフォルダへ入れると一切認識されない。
- 平置きにするとバッチをまたいでファイル名が衝突しうる（カメラ命名の
  `DSC04431.JPG` や、全バッチが持つ `manifest.json`）。そこで Drive 上の名前は
  `<batch_name>-<元のファイル名>`（例 `26_08_hiuchi-DSC04431.JPG`）にする。
- **同名ファイルは既アップロード済みとみなしてスキップ**する。上書きも別名追加もしない。
  判定は Drive 上の名前、つまり**プレフィックス込み**で行う。部分失敗からの再実行が
  「まだ無いものだけ」を送るレジューム動作になる。
  なお `GeoFrame` 直下の一覧にはフォルダ名も混ざる（旧版が作ったバッチ名の
  サブフォルダが残っている場合など）が、`26_08_hiuchi` と
  `26_08_hiuchi-DSC04431.JPG` は一致しないので誤スキップにはならない。
- 平置きの副作用として `GeoFrame` 直下には**全バッチ**のファイルが累積するので、
  スキップ判定の一覧取得は Drive 側の `name contains '<batch_name>-'` で絞る。
  絞らないと列挙件数がバッチ数に比例して増え、`resolving_folder` に留まる時間も伸びる。
- 入力の検証は `ValueError`（→ HTTP 400）。認証・通信の失敗はジョブ状態
  （`GET /api/drive-upload` の `status: "error"` + `error_kind`）で返す。
- ユーザーへ返すエラー文言は**固定文言のみ**。`HttpError` の生レスポンス本文や
  トークンの断片は絶対に載せない（詳細はサーバログにだけ出す）。

⚠️ **google 系ライブラリをモジュールのトップレベルで import しないこと。**
`google_auth_oauthlib` / `google.oauth2.credentials` / `googleapiclient.*` は、実際に
ネットワーク処理をする関数の**内部**でだけ import する。こうしておくと、ライブラリが
1つも入っていない環境でも `import local_raw.drive_upload` 自体は成功し、
`GET /api/drive-upload` が `configured` を返せる（= 未設定の画面を出せる）。
遅延 import は `_google_error_types()` / `_credentials_from_payload()` /
`_refresh_credentials()` / `_build_service()` / `_media_file_upload()` /
`_run_oauth_consent()` の 6 箇所に閉じ込めてあり、テストはこの継ぎ目を差し替える。
"""

from __future__ import annotations

import json
import os
import socket
import threading
import traceback
import uuid
from pathlib import Path
from typing import Any

from . import paths
from .export import export_dir_for

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CLIENT_SECRET_PATH = PROJECT_ROOT / "local_raw" / "drive_client_secret.json"
#: 旧パス（outputs/ 配下）。ensure_migrated() が一度きりの移行に使う。
_LEGACY_TOKEN_PATH = PROJECT_ROOT / "outputs" / "drive_auth.json"
TOKEN_PATH = paths.app_data_dir() / "drive_auth.json"
SCOPES = ["https://www.googleapis.com/auth/drive"]
DRIVE_FOLDER_NAME = "GeoFrame"
OAUTH_CONSENT_TIMEOUT_SECONDS = 300
UPLOAD_NUM_RETRIES = 5

#: Drive の「フォルダ」を表す MIME タイプ。検索条件に必ず含める（含めないと同名の
#: **ファイル**を親として拾ってしまい、その配下への作成が分かりにくく失敗する）。
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
#: `files().list()` の既定の `fields` には `createdTime` が**含まれない**。同名フォルダの
#: 決定的な選択（最古を採用）に必要なので明示する。
FILE_FIELDS = "nextPageToken, files(id, name, createdTime)"
LIST_PAGE_SIZE = 1000

#: 容量不足を表す reason code。403 で返る（ステータスだけでは権限不足と区別できない）。
STORAGE_QUOTA_REASON_CODE = "storageQuotaExceeded"
#: スロットリングを表す reason code。Drive はこれを **403** で返すので、
#: 429 と同じ「混雑しています」に寄せるにはステータスではなく code を見る必要がある。
RATE_LIMIT_REASON_CODES = frozenset({"userRateLimitExceeded", "rateLimitExceeded"})

# --- ユーザー向け固定文言 ---------------------------------------------------
CLIENT_SECRET_MISSING_MESSAGE = (
    "Google Drive の認証情報（local_raw/drive_client_secret.json）が見つかりません。"
)
CLIENT_SECRET_INVALID_MESSAGE = (
    "Google Drive の認証情報（local_raw/drive_client_secret.json）の形式が不正です。"
    "デスクトップアプリ用のJSONを配置してください。"
)
AUTH_REVOKED_MESSAGE = "Google Drive の認証が無効になりました。もう一度サインインしてください。"
QUOTA_MESSAGE = "Google Drive の空き容量が不足しています。"
RATE_LIMIT_MESSAGE = "Google Drive が混雑しています。しばらくしてから再試行してください。"
DEPENDENCIES_MISSING_MESSAGE = "必要なライブラリがインストールされていません。"
NETWORK_MESSAGE = "ネットワークに接続できませんでした。"
CONSENT_TIMEOUT_MESSAGE = "Google Drive のサインインが時間内に完了しませんでした。"
UNKNOWN_MESSAGE = "予期しないエラーが発生しました。"

__all__ = [
    "CLIENT_SECRET_PATH",
    "DRIVE_FOLDER_NAME",
    "SCOPES",
    "TOKEN_PATH",
    "DriveUploadError",
    "Progress",
    "ensure_migrated",
    "get_drive_upload_state",
    "is_drive_configured",
    "start_drive_upload",
]


class DriveUploadError(Exception):
    """分類済みのアップロード失敗。`kind` が契約の `error_kind` になる。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------------------
# クライアントシークレット
# ---------------------------------------------------------------------------


def _validate_client_secret() -> None:
    """`drive_client_secret.json` を同期的に検証する。失敗は `ValueError`。

    存在チェックだけでなく JSON として読めるか・トップレベルに `installed` キーが
    あるかまで見る。Web アプリ種別のJSON（`web` キー）を渡された場合に
    `InstalledAppFlow` 側の分かりにくいエラーで落ちるのを、ここで先に止めるため。
    """
    try:
        raw = CLIENT_SECRET_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ValueError(CLIENT_SECRET_MISSING_MESSAGE) from error
    except (OSError, ValueError) as error:
        # 読めない（権限なし等）・UTF-8 として不正。どちらも「壊れている」側へ寄せる。
        raise ValueError(CLIENT_SECRET_INVALID_MESSAGE) from error

    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise ValueError(CLIENT_SECRET_INVALID_MESSAGE) from error
    if not isinstance(payload, dict) or not isinstance(payload.get("installed"), dict):
        raise ValueError(CLIENT_SECRET_INVALID_MESSAGE)


def is_drive_configured() -> bool:
    """`_validate_client_secret()` が通るか（契約 §1 の `configured`）。"""
    try:
        _validate_client_secret()
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# トークンの永続化
# ---------------------------------------------------------------------------


def _read_token_payload() -> dict[str, Any] | None:
    """保存済みトークンを読む。存在しない・壊れている場合は `None`（例外は投げない）。"""
    try:
        loaded = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _write_token_payload(payload: dict[str, Any]) -> None:
    """リフレッシュトークンを 0600 で保存する（tmp + `os.replace` のアトミック書き込み）。

    `config._write_config()` と同じ流儀だが、**作成時モードを 0600 に固定する**点だけ
    違う。`write_text()` してから `chmod` すると、作成〜chmod の間に他ユーザーから
    読める窓が空く。長期の認証情報なのでその窓を作らない。
    """
    if _read_token_payload() == payload:
        # 中身が変わっていなければ書かない（refresh のたびに 0 バイトの窓を作らない）。
        return

    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = TOKEN_PATH.with_suffix(f".{os.getpid()}-{uuid.uuid4().hex}.tmp.json")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
        os.replace(temporary, TOKEN_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def _invalidate_token() -> None:
    """保存済みトークンを捨てる（失効・剥奪されたとき）。"""
    try:
        TOKEN_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def ensure_migrated() -> bool:
    """`_LEGACY_TOKEN_PATH`（outputs/drive_auth.json）から `TOKEN_PATH` への一度きりの移行。

    トークンファイルなので `mode=0o600` を指定する（`_write_token_payload` と同じ、
    他ユーザーから読めない権限を作成時から保つ）。

    ⚠️ **モジュールロード時には呼ばないこと。** `npm run test:raw` は
    `import local_raw.drive_upload` を伴うため、import 時に移行を走らせると
    テストを実行するだけで生のOAuthリフレッシュトークンが動いてしまう。
    呼び出しは実アプリの起動経路（`server.main()`）からのみ行うこと。
    """
    return paths.migrate_legacy_file(_LEGACY_TOKEN_PATH, TOKEN_PATH, mode=0o600)


# ---------------------------------------------------------------------------
# google 系ライブラリとの継ぎ目（遅延 import はすべてここに閉じ込める）
# ---------------------------------------------------------------------------


def _google_error_types() -> dict[str, type[BaseException] | None]:
    """例外分類に使う google 側の例外クラス。引けなかったものは `None`。

    ライブラリ未インストールでも `_classify_error()` が動く必要があるので、
    isinstance 判定の相手をここでまとめて解決し、`None` の分は判定を飛ばす。
    """
    resolved: dict[str, type[BaseException] | None] = {
        "refresh_error": None,
        "http_error": None,
        "transport_error": None,
        "server_not_found": None,
    }
    try:
        from google.auth.exceptions import RefreshError

        resolved["refresh_error"] = RefreshError
    except Exception:
        pass
    try:
        from googleapiclient.errors import HttpError

        resolved["http_error"] = HttpError
    except Exception:
        pass
    try:
        from google.auth.exceptions import TransportError

        resolved["transport_error"] = TransportError
    except Exception:
        pass
    try:
        # httplib2 は googleapiclient の HTTP トランスポート。名前解決に失敗すると
        # `ServerNotFoundError` を投げるが、これは `HttpLib2Error` → `Exception` 派生で
        # **`OSError` ではない**。下の `socket.gaierror` 判定には引っかからないので、
        # ここで別立てに解決しておく。
        from httplib2 import ServerNotFoundError

        resolved["server_not_found"] = ServerNotFoundError
    except Exception:
        pass
    return resolved


def _credentials_from_payload(payload: dict[str, Any]) -> Any:
    """保存済みトークンから `Credentials` を復元する。

    `scopes` は**渡さない**。渡すとその値がそのまま `credentials.scopes` になり、
    実際に許可された範囲に関わらず `has_scopes()` が常に True になってしまう。
    引数を省くと `from_authorized_user_info()` は保存された `scopes` を読むので、
    スコープ不足を検出できる。
    """
    from google.oauth2.credentials import Credentials

    return Credentials.from_authorized_user_info(payload)


def _refresh_credentials(credentials: Any) -> None:
    """アクセストークンを更新する。"""
    from google.auth.transport.requests import Request

    credentials.refresh(Request())


def _build_service(credentials: Any) -> Any:
    """Drive API クライアントを組み立てる。

    `cache_discovery=False`: 既定の True は oauth2client 由来のファイルキャッシュを
    引き込み、無い場合に警告を出す。
    """
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _media_file_upload(path: Path) -> Any:
    """アップロード用のメディアオブジェクト（再開可能アップロード）。"""
    from googleapiclient.http import MediaFileUpload

    return MediaFileUpload(str(path), resumable=True)


def _run_oauth_consent(progress: "Progress") -> Any:
    """ブラウザを開いて OAuth 同意を取る。タイムアウトは `consent_timeout` に変換する。

    `InstalledAppFlow.run_local_server(timeout_seconds=...)` は
    `google_auth_oauthlib.flow.WSGITimeoutError` を投げる（実測 google-auth-oauthlib
    1.4.0。`AttributeError` の**サブクラス**）。裸の `except AttributeError` にすると
    無関係な属性エラーまでタイムアウトと誤分類するので、クラスを名指しで解決して
    `isinstance` で判定する。クラスが引けない版に当たった場合は戻り値の有無で代替する。
    """
    progress.set_stage("waiting_for_consent")
    import google_auth_oauthlib.flow as flow_module

    timeout_error = getattr(flow_module, "WSGITimeoutError", None)
    flow = flow_module.InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    try:
        credentials = flow.run_local_server(
            port=0, open_browser=True, timeout_seconds=OAUTH_CONSENT_TIMEOUT_SECONDS
        )
    except Exception as error:
        if timeout_error is not None and isinstance(error, timeout_error):
            raise DriveUploadError("consent_timeout", CONSENT_TIMEOUT_MESSAGE) from error
        raise
    if credentials is None:
        raise DriveUploadError("consent_timeout", CONSENT_TIMEOUT_MESSAGE)
    return credentials


# ---------------------------------------------------------------------------
# 認証
# ---------------------------------------------------------------------------


def _is_refresh_error(error: BaseException) -> bool:
    refresh_error = _google_error_types().get("refresh_error")
    return refresh_error is not None and isinstance(error, refresh_error)


def _get_credentials(progress: "Progress") -> Any:
    """保存済みトークンを復元するか、足りなければ同意フローへ落とす。

    復元 → スコープ検査 → 期限切れなら refresh、の順。`RefreshError`（ユーザーが
    アクセスを取り消した等）はトークンを捨てて同意フローからやり直す。
    """
    progress.set_stage("authenticating")

    credentials: Any = None
    payload = _read_token_payload()
    if payload is not None:
        try:
            credentials = _credentials_from_payload(payload)
        except (ValueError, TypeError, KeyError):
            # 形が想定と違う保存トークン。捨てて同意からやり直す。
            credentials = None

    if credentials is not None and not credentials.has_scopes(SCOPES):
        # 以前より狭い範囲でしか許可されていない。再同意しないと書き込めない。
        credentials = None

    if credentials is not None and not credentials.valid:
        if getattr(credentials, "refresh_token", None):
            try:
                _refresh_credentials(credentials)
            except Exception as error:
                if not _is_refresh_error(error):
                    raise
                _invalidate_token()
                credentials = None
        else:
            credentials = None

    if credentials is None:
        credentials = _run_oauth_consent(progress)

    _write_token_payload(json.loads(credentials.to_json()))
    return credentials


# ---------------------------------------------------------------------------
# Drive 操作
# ---------------------------------------------------------------------------


def _escape_query_value(value: str) -> str:
    """Drive 検索クエリ（`q`）の文字列リテラルをエスケープする。

    呼び出し側が渡す値は正規化済みバッチ名（`[A-Za-z0-9_-]+`）と定数だけなので
    現状は素通りするが、`q` は文字列連結で組み立てる以上ここを二重の防御として持つ。
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _resolve_folder(
    service: Any, name: str, parent_id: str | None, *, owned_only: bool = False
) -> str:
    """`name` のフォルダIDを返す。無ければ作る。

    同名フォルダが複数ある場合は **`createdTime` が最も古いもの**を採用する。
    実行のたびに違うフォルダへ入れてしまうと、スキップ判定（同名ファイルは
    アップロード済み）が成立しなくなるため、選択は決定的でなければならない。

    `owned_only=True` は検索を **自分所有 かつ My Drive 直下** に限定する
    （`'me' in owners` と `'root' in parents`）。ルートの `GeoFrame` フォルダ解決に使う。
    `'me' in owners` だけだと、他人が自分と**共有**している同名フォルダを拾ってしまいうる
    （共有フォルダは自分の Drive のトップレベルにも並んで見えるため、`parent_id=None` の
    検索に紛れ込む）。`'root' in parents` も要るのは、所有者を絞っても Drive の
    **任意の階層**にある自分所有の同名フォルダが依然として一致するため。作成は常に自分の
    My Drive 直下（`parents` 省略）に行われるので、検索側を同じ場所に揃えないと
    「作成時と検索時で別のフォルダを指す」自己矛盾が起きうる。
    既定が `False` なのは、`parent_id` 指定の検索（配下のフォルダを引く汎用の使い方）に
    まで広げると壊れるため: 共有ドライブ配下のフォルダは所有者が自分でないことがある。
    現状 `_run_upload()` からの呼び出しはルートの `GeoFrame` 解決 1 回だけで、
    バッチごとのサブフォルダは作らない（アップロードは `GeoFrame` 直下へ平置きする）。
    """
    conditions = [
        f"name = '{_escape_query_value(name)}'",
        f"mimeType = '{FOLDER_MIME_TYPE}'",
        "trashed = false",
    ]
    if parent_id is not None:
        conditions.append(f"'{_escape_query_value(parent_id)}' in parents")
    if owned_only:
        conditions.append("'me' in owners")
        # 所有者だけでは足りない: My Drive の**任意の階層**にある自分所有の同名フォルダを
        # 拾いうる。作成は必ず My Drive 直下（`parents` 省略）なので、検索も直下に
        # 揃えないと「昔どこかに作った同名フォルダへ黙って積む」ことになる。
        conditions.append("'root' in parents")

    response = (
        service.files()
        .list(
            q=" and ".join(conditions),
            spaces="drive",
            fields=FILE_FIELDS,
            pageSize=LIST_PAGE_SIZE,
            # ページをまたいでも最古が先頭に来るようサーバ側にも並べさせる。
            orderBy="createdTime",
        )
        .execute()
    )
    matches = [item for item in (response.get("files") or []) if item.get("id")]
    if matches:
        # `createdTime` が欠けていても順序が壊れないよう空文字へ畳み、同着は id で割る。
        matches.sort(key=lambda item: (str(item.get("createdTime") or ""), str(item.get("id"))))
        return str(matches[0]["id"])

    body: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME_TYPE}
    if parent_id is not None:
        body["parents"] = [parent_id]
    created = service.files().create(body=body, fields="id").execute()
    folder_id = (created or {}).get("id")
    if not folder_id:
        raise DriveUploadError("unknown", UNKNOWN_MESSAGE)
    return str(folder_id)


def _existing_filenames(service: Any, parent_id: str, name_prefix: str | None = None) -> set[str]:
    """`parent_id` 直下にある（ゴミ箱に入っていない）名前の集合。

    mimeType で絞っていないのでフォルダ名も混ざる（旧版が作ったバッチ名の
    サブフォルダなど）。スキップ判定に使う名前は `<batch_name>-<ファイル名>` で、
    フォルダ名（`<batch_name>` のみ）とは一致しないため誤スキップにはならない。

    `name_prefix` を指定すると Drive 側の `name contains '<prefix>'` 条件で絞り込む。
    平置きにしたことで `parent_id` 直下には全バッチのファイルが累積するため、絞らずに
    列挙すると件数がバッチ数に比例して増え続ける（`resolving_folder` で止まる時間も
    伸びる）。呼び出し側は自分のバッチのプレフィックス（`<batch_name>-`）を渡して、
    そのバッチに関係する名前だけを引く。`contains` は部分一致なので別バッチの名前を
    巻き込むことがあるが、最終的な一致判定は呼び出し側の完全一致（`in existing`）なので
    誤スキップにはならない（過剰に拾うだけで安全側に倒れる）。
    """
    conditions = [f"'{_escape_query_value(parent_id)}' in parents", "trashed = false"]
    if name_prefix is not None:
        conditions.append(f"name contains '{_escape_query_value(name_prefix)}'")
    query = " and ".join(conditions)
    names: set[str] = set()
    page_token: str | None = None
    while True:
        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields=FILE_FIELDS,
                pageSize=LIST_PAGE_SIZE,
                pageToken=page_token,
            )
            .execute()
        )
        for item in (response or {}).get("files") or []:
            name = item.get("name")
            if isinstance(name, str):
                names.add(name)
        page_token = (response or {}).get("nextPageToken")
        if not page_token:
            return names


def _upload_file(service: Any, path: Path, parent_id: str, upload_name: str) -> None:
    """1ファイルを再開可能アップロードで送る。Drive 上のファイル名は `upload_name`。

    中身は `path` から、名前は `upload_name` から取る（平置きの衝突回避で
    Drive 側にだけプレフィックスを付けるため、両者は一致しない）。
    """
    media = _media_file_upload(path)
    service.files().create(
        body={"name": upload_name, "parents": [parent_id]}, media_body=media
    ).execute(num_retries=UPLOAD_NUM_RETRIES)


# ---------------------------------------------------------------------------
# 進捗
# ---------------------------------------------------------------------------


class Progress:
    """スレッドセーフな進捗カウンタ（`analysis.Progress` と同形 + バイト数・スキップ）。

    `done` / `bytes_done` は**スキップ分も含む**「処理済み」を表す。含めないと、
    再実行で大半がスキップされたときに進捗バーが 100% に届かないまま完了してしまう。
    実際に送った件数は結果側の `uploaded` が持つ。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stage = "authenticating"
        self._done = 0
        self._total = 0
        self._bytes_done = 0
        self._bytes_total = 0
        self._skipped: list[str] = []

    def set_stage(self, stage: str) -> None:
        with self._lock:
            self._stage = stage

    def set_totals(self, total: int, bytes_total: int) -> None:
        with self._lock:
            self._total = int(total)
            self._bytes_total = int(bytes_total)

    def advance(self, size: int = 0) -> None:
        with self._lock:
            self._done += 1
            self._bytes_done += int(size)

    def add_skipped(self, name: str, size: int = 0) -> None:
        with self._lock:
            self._skipped.append(name)
            self._done += 1
            self._bytes_done += int(size)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            # `skipped` は**複製**して返す。生のリストを渡すとロックの外へ可変状態が漏れる。
            return {
                "stage": self._stage,
                "done": self._done,
                "total": self._total,
                "bytes_done": self._bytes_done,
                "bytes_total": self._bytes_total,
                "skipped": list(self._skipped),
            }


# ---------------------------------------------------------------------------
# アップロード本体
# ---------------------------------------------------------------------------


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _batch_files(export_dir: Path) -> list[Path]:
    """`export_dir` 直下のアップロード対象を名前順で返す。

    `manifest.json` は**含む**（GeoFrame 側が出所を復元するのに要る）。書き込み途中の
    `.tmp` は除外し、サブディレクトリは辿らない。
    """
    resolved = export_dir.resolve()
    files = [
        path
        for path in resolved.iterdir()
        if path.is_file() and not path.name.endswith(".tmp") and path.parent == resolved
    ]
    return sorted(files, key=lambda path: path.name)


def _run_upload(progress: Progress, batch_name: str) -> dict[str, Any]:
    """認証 → フォルダ解決 → 差分アップロード。戻り値は契約の `done` ペイロード。"""
    export_dir = export_dir_for(batch_name)
    files = _batch_files(export_dir)
    sizes = {path.name: _file_size(path) for path in files}
    progress.set_totals(len(files), sum(sizes.values()))

    credentials = _get_credentials(progress)
    service = _build_service(credentials)

    progress.set_stage("resolving_folder")
    # 置き先は `GeoFrame` フォルダ**そのもの**。バッチごとのサブフォルダは作らない
    # （消費側の Drive インデックスが非再帰スキャンなので、入れると認識されない）。
    root_id = _resolve_folder(service, DRIVE_FOLDER_NAME, None, owned_only=True)
    # 一覧は自分のバッチのプレフィックスで絞る。平置きで `GeoFrame` 直下に全バッチが
    # 積み上がるので、絞らないと列挙件数がバッチ数に比例して増え続ける。
    existing = _existing_filenames(service, root_id, name_prefix=f"{export_dir.name}-")

    progress.set_stage("uploading")
    uploaded = 0
    for path in files:
        size = sizes.get(path.name, 0)
        # プレフィックスには**正規化済み**のバッチ名（`export_dir.name`）だけを使う。
        # 生の入力を Drive へ渡す経路をここ一本に絞る。
        upload_name = f"{export_dir.name}-{path.name}"
        if upload_name in existing:
            # 既にある名前は「アップロード済み」とみなす。上書きも別名追加もしない
            # （部分失敗からの再実行が、まだ無いものだけを送るレジュームになる）。
            # 進捗へ渡すのは**元のファイル名**（画面にはバッチ内で見慣れた名前を出す）。
            progress.add_skipped(path.name, size)
            continue
        _upload_file(service, path, root_id, upload_name)
        uploaded += 1
        progress.advance(size)

    return {
        "uploaded": uploaded,
        "total": len(files),
        "skipped": progress.snapshot()["skipped"],
        "folder_url": f"https://drive.google.com/drive/folders/{root_id}",
    }


# ---------------------------------------------------------------------------
# エラー分類
# ---------------------------------------------------------------------------


def _http_status(error: BaseException) -> int | None:
    """`HttpError` から HTTP ステータスを読む。読めなければ `None`。

    `error.resp.status` を先に見て `status_code` へ落ちる。どちらも属性の有無・型が
    保証されないので（テストのスタブでは Mock が返る）、`int()` を try で包む。
    """
    for candidate in (
        getattr(getattr(error, "resp", None), "status", None),
        getattr(error, "status_code", None),
    ):
        try:
            return int(candidate)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return None


def _http_reason_codes(error: BaseException) -> set[str]:
    """`HttpError` からマシン可読な reason code を集める。読めなければ空集合。

    ⚠️ `HttpError.reason` は**使えない**。あれはレスポンス本文の
    `error.message`（人間向け文言。ローカライズもされうる）であって、
    `storageQuotaExceeded` のようなコードは入らない。コードが載るのは
    `HttpError.error_details` の側で、Drive v3 の 403 では本文の `error.errors[]` が
    そのまま入り `[{"domain": ..., "reason": "storageQuotaExceeded", ...}, ...]` になる。

    `error_details` は本文の形しだいで list にも str にも `""` にもなるので、
    list の dict 要素の `reason` だけを拾う。**str 形の中身は走査しない**:
    そちらは人間向け文言であり、部分一致を許すと「権限不足の 403」を
    容量不足と誤診する。
    """
    details = getattr(error, "error_details", None)
    if not isinstance(details, list):
        return set()
    codes: set[str] = set()
    for item in details:
        if not isinstance(item, dict):
            continue
        reason = item.get("reason")
        if isinstance(reason, str) and reason:
            codes.add(reason)
    return codes


def _classify_error(error: Exception) -> tuple[str, str]:
    """例外を `(error_kind, ユーザー向け固定文言)` に写す。

    **戻り値の文言に例外の中身を混ぜない。** `HttpError.__str__` は要求URIと
    レスポンス本文を含み、トークンや内部パスが画面へ出うる。詳細は
    `traceback.print_exc()` でサーバログにだけ残す。
    """
    if isinstance(error, DriveUploadError):
        return error.kind, str(error)

    error_types = _google_error_types()
    refresh_error = error_types.get("refresh_error")
    http_error = error_types.get("http_error")

    if refresh_error is not None and isinstance(error, refresh_error):
        # リフレッシュトークンが失効・剥奪された。次回は同意からやり直させる。
        _invalidate_token()
        return "auth_revoked", AUTH_REVOKED_MESSAGE

    if http_error is not None and isinstance(error, http_error):
        status = _http_status(error)
        if status == 401:
            _invalidate_token()
            return "auth_revoked", AUTH_REVOKED_MESSAGE
        if status == 403:
            # 403 は容量不足・スロットリング・権限不足が同居する。ステータスでは
            # 割れないので reason code で分ける（`reason` は人間向け文言なので不可）。
            codes = _http_reason_codes(error)
            if STORAGE_QUOTA_REASON_CODE in codes:
                return "quota", QUOTA_MESSAGE
            if codes & RATE_LIMIT_REASON_CODES:
                return "rate_limit", RATE_LIMIT_MESSAGE
        if status == 429 or (status is not None and 500 <= status < 600):
            return "rate_limit", RATE_LIMIT_MESSAGE
        traceback.print_exc()
        return "unknown", UNKNOWN_MESSAGE

    if isinstance(error, ModuleNotFoundError):
        return "dependencies_missing", DEPENDENCIES_MISSING_MESSAGE

    # --- ここから通信失敗の判定 ---------------------------------------------
    # 「`OSError` なら network」で一括りにしてはいけない。`OSError` はローカル I/O
    # 失敗（フォルダ消失・ディスク書込失敗）でも上がるので、それを「ネットワークに
    # 接続できませんでした」と案内すると原因究明を誤らせる。逆に本物の通信障害には
    # `OSError` 派生でないものがある。実際のクラスを名指しで並べる。
    transport_error = error_types.get("transport_error")
    if transport_error is not None and isinstance(error, transport_error):
        # google-auth の通信失敗（トークン更新時など）。`GoogleAuthError` →
        # `Exception` 派生で `OSError` ではないため、名指ししないと届かない。
        return "network", NETWORK_MESSAGE

    server_not_found = error_types.get("server_not_found")
    if server_not_found is not None and isinstance(error, server_not_found):
        # httplib2 の名前解決失敗。`HttpLib2Error` → `Exception` 派生で `OSError`
        # ではないので、下の `socket.gaierror` では拾えない。
        return "network", NETWORK_MESSAGE

    # `TimeoutError` は `OSError` 派生だが `ConnectionError` 派生では**ない**
    # （3.10+ では `socket.timeout` のエイリアス）。名指ししないと、アップロード中の
    # 接続タイムアウトが下の `unknown` へ落ちてしまう。
    if isinstance(error, (ConnectionError, TimeoutError, socket.gaierror, socket.herror)):
        return "network", NETWORK_MESSAGE

    # 残った `OSError` はローカル I/O 失敗とみなし、`unknown` としてログに残す。
    traceback.print_exc()
    return "unknown", UNKNOWN_MESSAGE


# ---------------------------------------------------------------------------
# ジョブ管理
# ---------------------------------------------------------------------------

_STATE_LOCK = threading.Lock()
_RUNNING = False
_PROGRESS: Progress | None = None
#: 実行中・直近の完了/失敗が指すバッチ名。`done` / `error` も `batch_name` を返すので、
#: ジョブ終了時には**クリアしない**（次の `start_drive_upload` 成功で上書きする）。
_CURRENT_BATCH: str | None = None
_LAST_RESULT: dict[str, Any] | None = None
_LAST_ERROR_KIND: str | None = None
_LAST_ERROR_MESSAGE: str | None = None


def _run_upload_job(progress: Progress, batch_name: str) -> None:
    """ワーカー本体。**例外を外へ漏らさない**（`analysis._run_analysis` と同型）。"""
    global _RUNNING, _LAST_RESULT, _LAST_ERROR_KIND, _LAST_ERROR_MESSAGE
    try:
        result = _run_upload(progress, batch_name)
        with _STATE_LOCK:
            _LAST_RESULT = result
            _LAST_ERROR_KIND = None
            _LAST_ERROR_MESSAGE = None
    except Exception as error:
        try:
            kind, message = _classify_error(error)
        except Exception:
            # 分類自体が失敗しても状態機械を固着させない。
            traceback.print_exc()
            kind, message = "unknown", UNKNOWN_MESSAGE
        with _STATE_LOCK:
            _LAST_RESULT = None
            _LAST_ERROR_KIND = kind
            _LAST_ERROR_MESSAGE = message
    finally:
        with _STATE_LOCK:
            _RUNNING = False


def start_drive_upload(batch_name: Any) -> str:
    """アップロードをバックグラウンドで開始する。`started` | `already_running`。

    入力不正（バッチ名・フォルダ不在・対象0件・認証情報未配置）は `ValueError`（→400）。
    """
    global _RUNNING, _PROGRESS, _CURRENT_BATCH, _LAST_RESULT
    global _LAST_ERROR_KIND, _LAST_ERROR_MESSAGE

    # 同期検証。ここを通ってからでないとジョブを起こさない（起動してから 400 相当の
    # 失敗をジョブ状態で返すと、画面が「実行中 → エラー」を無駄に往復する）。
    export_dir = export_dir_for(batch_name)
    if not export_dir.is_dir():
        raise ValueError("書き出し済みのフォルダが見つかりません。先に書き出しを実行してください。")
    if not _batch_files(export_dir):
        # `.tmp` しか無いフォルダも「対象0件」。`iterdir()` ではなく実際の対象で判定する。
        raise ValueError("アップロードできるファイルが書き出しフォルダにありません。")
    _validate_client_secret()

    normalized = export_dir.name
    with _STATE_LOCK:
        if _RUNNING:
            return "already_running"
        progress = Progress()
        _PROGRESS = progress
        _CURRENT_BATCH = normalized
        _LAST_RESULT = None
        _LAST_ERROR_KIND = None
        _LAST_ERROR_MESSAGE = None
        _RUNNING = True
    try:
        threading.Thread(
            target=_run_upload_job,
            args=(progress, normalized),
            daemon=True,
            name="drive-upload",
        ).start()
    except RuntimeError:
        # start() がワーカー起動前に失敗した（OS がスレッドを拒否した等）。上で立てた
        # _RUNNING を戻さないと GET /api/drive-upload が running のまま固着する。
        with _STATE_LOCK:
            _RUNNING = False
        raise
    return "started"


def get_drive_upload_state() -> dict[str, Any]:
    """契約 §1 の `GET /api/drive-upload` ペイロード。

    `done` / `error` はメモリ上にだけ保持する（サーバ再起動で `idle` に戻る）。
    """
    with _STATE_LOCK:
        running = _RUNNING
        progress = _PROGRESS
        batch_name = _CURRENT_BATCH
        result = _LAST_RESULT
        error_kind = _LAST_ERROR_KIND
        error_message = _LAST_ERROR_MESSAGE

    if running and progress is not None:
        return {"status": "running", "batch_name": batch_name, "progress": progress.snapshot()}
    if error_kind is not None:
        return {
            "status": "error",
            "batch_name": batch_name,
            "error": error_message,
            "error_kind": error_kind,
        }
    if result is not None:
        return {"status": "done", "batch_name": batch_name, **result}
    return {"status": "idle", "configured": is_drive_configured()}
