"""書き出し済みバッチを Apple「写真」へ取り込む（osascript 経由）。

契約: docs/API_CONTRACT.md §1（`POST /api/import-to-photos`）

- 対象は `outputs/export/<batch_name>/` 直下の `*.jpg` / `*.JPG` のみ。
- `osascript` は**配列形式**で呼ぶ（AppleScript を文字列連結で組み立てない）。
- 初回実行時は macOS の自動化許可（ターミナル →「写真」）が必要。未許可のときは
  osascript がエラー -1743 等で失敗するが、これは想定内の経路として
  `{"status": "error", ...}` で返す（HTTP は 200）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .engine import PROJECT_ROOT
from .export import export_dir_for

SCRIPT_PATH = PROJECT_ROOT / "scripts" / "photos_import.applescript"
OSASCRIPT_TIMEOUT_SECONDS = 180
ALBUM_PREFIX = "GeoFrame RAW Assist"
MAX_ALBUM_NAME_LENGTH = 120
STDERR_EXCERPT_LENGTH = 500

PERMISSION_HINT = (
    "「システム設定 > プライバシーとセキュリティ > オートメーション」で、"
    "ターミナル（実行元アプリ）に「写真」の操作を許可してください。"
)
#: 自動化未許可でよく出るシグネチャ。日本語ロケールでは文言が変わるので番号も見る。
_PERMISSION_SIGNATURES = ("-1743", "-1728", "Not authorized", "not allowed", "許可されていません")

__all__ = ["PERMISSION_HINT", "SCRIPT_PATH", "import_batch"]


def _album_name(album: Any, batch_name: str) -> str:
    if album is None:
        return f"{ALBUM_PREFIX} {batch_name}"
    if not isinstance(album, str):
        raise ValueError("アルバム名は文字列で指定してください。")
    name = album.strip()
    if not name:
        raise ValueError("アルバム名を指定してください。")
    if len(name) > MAX_ALBUM_NAME_LENGTH:
        raise ValueError(f"アルバム名は{MAX_ALBUM_NAME_LENGTH}文字以内で指定してください。")
    if any(character in name for character in ("\n", "\r", "\x00")):
        raise ValueError("アルバム名に改行は使用できません。")
    return name


def _exported_jpegs(export_dir: Path) -> list[Path]:
    """`export_dir` 直下の JPEG だけを集める。

    `Path.glob("*.jpg")` は posix flavour では大文字小文字を区別するため、
    カメラJPG（`DSC02943.JPG`）を取りこぼす。拡張子は明示的に小文字化して比較する。
    """
    resolved_dir = export_dir.resolve()
    files = [
        path
        for path in resolved_dir.iterdir()
        if path.is_file() and path.suffix.lower() == ".jpg" and path.parent == resolved_dir
    ]
    return sorted(files, key=lambda path: path.name)


def _error(message: str, stderr: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"status": "error", "error": message}
    if stderr:
        result["stderr"] = stderr[:STDERR_EXCERPT_LENGTH]
    return result


def import_batch(batch_name: str, album: str | None = None) -> dict[str, Any]:
    """書き出し済みバッチを「写真」のアルバムへ取り込む。

    入力不正（バッチ名・対象0件）は `ValueError`（→ HTTP 400）、
    osascript の失敗は `{"status": "error", ...}`（→ HTTP 200）で返す。
    """
    export_dir = export_dir_for(batch_name)
    if not export_dir.is_dir():
        raise ValueError("書き出し済みのフォルダが見つかりません。先に書き出しを実行してください。")

    files = _exported_jpegs(export_dir)
    if not files:
        raise ValueError("取り込めるJPEGが書き出しフォルダにありません。")

    album_name = _album_name(album, export_dir.name)

    if not SCRIPT_PATH.is_file():
        return _error("取り込み用スクリプトが見つかりません。")

    command = ["osascript", str(SCRIPT_PATH), album_name, *[str(path) for path in files]]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=OSASCRIPT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        # 初回実行時の自動化許可ダイアログが未応答のまま放置されるとここに来る。
        return _error(f"「写真」への取り込みが時間内に終わりませんでした。{PERMISSION_HINT}")
    except FileNotFoundError:
        return _error("osascript が見つかりません。macOS 上で実行してください。")
    except OSError as error:
        return _error(f"「写真」への取り込みを開始できませんでした: {error.strerror or error}")

    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        if any(signature in stderr for signature in _PERMISSION_SIGNATURES):
            message = f"「写真」の操作が許可されていません。{PERMISSION_HINT}"
        else:
            message = f"「写真」への取り込みに失敗しました。{PERMISSION_HINT}"
        return _error(message, stderr)

    imported = len(files)
    stdout = (completed.stdout or "").strip()
    if stdout.isdecimal():
        imported = int(stdout)

    return {"status": "ok", "imported": imported, "album": album_name}
