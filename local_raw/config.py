"""現像元フォルダ（source root）の検証・永続化。

契約: docs/API_CONTRACT.md §1（`GET/POST /api/source-folder`）・§9

このモジュールは **`capture_id` がフォルダをまたいで衝突する**ことへの防波堤でもある。
`DSC02943` はカメラの連番なので、カードが違えば別の写真に同じ ID が付く。そのため
現像キャッシュ（`engine._params_hash`）・解析キャッシュ（`analysis._source_signature`）・
サムネイル名は、いずれも解決済みルートパス（または `root_key_for()`）を混ぜて
フォルダごとに分離する。混ぜ忘れると、別フォルダの同名 ID に対して**他人の写真の
現像結果が `cached: true` で返る**。

⚠️ **どのモジュールでも `SOURCE_ROOT = config.source_root()` のようにモジュールレベルで
束縛しないこと。** import 時に固定されるとフォルダ切り替えが半分しか効かなくなる。
参照は必ずリクエスト処理時の関数呼び出しで解決する。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat as stat_module
import threading
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE_ROOT = PROJECT_ROOT / "sample" / "100MSDCF"
CONFIG_PATH = PROJECT_ROOT / "outputs" / "config.json"

CONFIG_VERSION = 1

#: `outputs/config.json` 内で現像元フォルダを保持するキー。
SOURCE_ROOT_KEY = "source_root"

#: `root_key`（サムネイル名の接頭辞 / フォルダ識別子）の長さ。
ROOT_KEY_LENGTH = 8

#: macOS の TCC（プライバシー保護）で拒否されたときの案内。
PERMISSION_MESSAGE = (
    "フォルダを読み取れません。"
    "システム設定 > プライバシーとセキュリティ からアクセスを許可してください。"
)

_WRITE_LOCK = threading.Lock()

__all__ = [
    "CONFIG_PATH",
    "CONFIG_VERSION",
    "DEFAULT_SOURCE_ROOT",
    "PERMISSION_MESSAGE",
    "PROJECT_ROOT",
    "SourceFolderError",
    "display_path",
    "root_key_for",
    "set_source_root",
    "source_folder_info",
    "source_root",
    "source_root_key",
]


class SourceFolderError(ValueError):
    """現像元フォルダの指定を受け付けられないことを表す。

    `reason` は契約 §1 の機械可読な理由（`invalid` / `not_found` / `not_directory` /
    `permission_denied` / `analysis_running`）。サーバ側はこれを HTTP 400 +
    `{"error": ..., "reason": ...}` に写す。`ValueError` 派生なので、
    既存の POST 例外マッピング（`ValueError` → 400）にもそのまま乗る。
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# キー生成 / 表示
# ---------------------------------------------------------------------------


def root_key_for(path: Path) -> str:
    """サムネイル名・キャッシュキー用のフォルダ識別子（sha1 先頭8文字）。

    `params_hash` に混ぜるパス文字列と**同じ正規化**を通すため、ここで必ず `resolve()`
    する（`/var` と `/private/var` のような表記揺れで片方だけキャッシュが残らないように）。
    """
    canonical = str(path.resolve())
    return hashlib.sha1(canonical.encode("utf-8"), usedforsecurity=False).hexdigest()[
        :ROOT_KEY_LENGTH
    ]


def source_root_key() -> str:
    """現在の現像元フォルダの `root_key`。"""
    return root_key_for(source_root())


def display_path(path: Path) -> str:
    """表示用のパス文字列。

    プロジェクト配下ならプロジェクト相対、ホーム配下なら `~/...`、それ以外は絶対パス。
    プロジェクト自体がホーム配下にあるため、**判定順（プロジェクト → ホーム）は
    入れ替えないこと**。

    `scan_pairs()` 経由で解析のたびに呼ばれる経路なので、ホーム解決の失敗
    （`HOME` 未設定・passwd エントリなし等）は絶対パス表示にフォールバックして
    絶対に例外を投げない。
    """
    resolved = path.resolve()
    project_root = PROJECT_ROOT.resolve()
    if resolved.is_relative_to(project_root):
        return str(resolved.relative_to(project_root))
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError, KeyError):
        return str(resolved)
    if resolved == home:
        return "~"
    if resolved.is_relative_to(home):
        return f"~/{resolved.relative_to(home)}"
    return str(resolved)


# ---------------------------------------------------------------------------
# 永続化
# ---------------------------------------------------------------------------


def _read_config() -> dict[str, Any]:
    """設定ファイルを読む。存在しない・壊れている場合は空 dict（例外は投げない）。"""
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_config(payload: dict[str, Any]) -> None:
    """tmp + `os.replace` のアトミック書き込み（`engine._save_recipe` と同方式）。"""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix(f".{os.getpid()}-{threading.get_ident()}.tmp.json")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, CONFIG_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def _stat_or_none(path: Path) -> os.stat_result | None:
    """`os.stat` の結果。存在しない・パスとして不正な場合は None。

    `Path.is_dir()` と違い `PermissionError` を握り潰さない用途で使う
    （フォルダ自体は stat できるが中身を読めない、という macOS の TCC 状態を残すため）。
    """
    try:
        return os.stat(path)
    except (OSError, ValueError):
        return None


def _probe_readable(path: Path) -> bool:
    """ディレクトリを実際に1エントリ読んで、読み取り可能かを確かめる。

    `Path.glob` は `PermissionError` を握り潰して空を返すため、これが無いと
    「0組」と「読めない」を区別できない。macOS は `~/Pictures` / `~/Desktop` /
    外部ボリュームへの初回アクセスで許可を要求し、拒否されるとここが `PermissionError`
    になる。遅延エラーを拾うため、イテレータを作るだけでなく1件消費する。
    """
    try:
        with os.scandir(path) as entries:
            next(iter(entries), None)
    except (OSError, ValueError):
        return False
    return True


def source_root() -> Path:
    """現在の現像元フォルダを解決して返す（**例外は投げない**）。

    設定ファイルが無い / 壊れている / 保存されたフォルダがもう存在しない場合は
    `DEFAULT_SOURCE_ROOT` にフォールバックする。フォールバック先自体が存在しなくても
    そのまま返す（存在チェックは `scan_pairs` などの呼び出し側の責務）。

    「読めないだけ」のフォルダ（TCC 拒否・権限なし）は**フォールバックしない**。
    既定に戻してしまうと UI が「許可すれば直る」ことを案内できなくなるため。
    """
    stored = _read_config().get(SOURCE_ROOT_KEY)
    if isinstance(stored, str) and stored:
        candidate = Path(stored)
        if candidate.is_absolute():
            info = _stat_or_none(candidate)
            if info is not None and stat_module.S_ISDIR(info.st_mode):
                return candidate.resolve()
    return DEFAULT_SOURCE_ROOT.resolve()


def _validate_source_root(path: Any) -> Path:
    """入力パスを検証して解決済み `Path` を返す。失敗は `SourceFolderError`。"""
    if not isinstance(path, str) or not path.strip():
        raise SourceFolderError("フォルダのパスを指定してください。", "invalid")
    # 貼り付け由来の前後空白は落とす（ネイティブのフォルダ選択は完全一致のパスを返す）。
    candidate = Path(path.strip())
    if not candidate.is_absolute():
        raise SourceFolderError("フォルダは絶対パスで指定してください。", "invalid")

    try:
        info = os.stat(candidate)
    except FileNotFoundError as error:
        raise SourceFolderError("指定されたフォルダが見つかりません。", "not_found") from error
    except PermissionError as error:
        raise SourceFolderError(PERMISSION_MESSAGE, "permission_denied") from error
    except (OSError, ValueError) as error:
        # NUL 文字（ValueError）・パス長超過（ENAMETOOLONG）・シンボリックリンクループ等。
        # 分類できない不正パスは invalid に寄せる（理由なしの 400 を作らない）。
        raise SourceFolderError("フォルダのパスが不正です。", "invalid") from error

    if not stat_module.S_ISDIR(info.st_mode):
        raise SourceFolderError("指定されたパスはフォルダではありません。", "not_directory")

    resolved = candidate.resolve()
    if not _probe_readable(resolved):
        raise SourceFolderError(PERMISSION_MESSAGE, "permission_denied")
    return resolved


def set_source_root(path: Any, *, analysis_running: bool = False) -> Path:
    """現像元フォルダを検証して保存し、解決済みパスを返す。

    検証に失敗した場合・解析ジョブ実行中の場合は `SourceFolderError` を投げ、
    **設定は保存しない**。保存する文字列は解決済み絶対パスに固定する
    （`root_key_for()` / `params_hash` と同じ正規化を共有するため）。

    `analysis_running` は呼び出し側（サーバ）が `analysis.is_analysis_running()` を
    渡す。config が analysis を import すると循環するので、判定は注入で受け取る。
    """
    if analysis_running:
        raise SourceFolderError(
            "解析の実行中はフォルダを変更できません。完了後にもう一度お試しください。",
            "analysis_running",
        )
    resolved = _validate_source_root(path)
    with _WRITE_LOCK:
        _write_config({"config_version": CONFIG_VERSION, SOURCE_ROOT_KEY: str(resolved)})
    return resolved


def source_folder_info(root: Path | None = None) -> dict[str, Any]:
    """契約 §1 の `SourceFolderInfo`。

    枚数は `analysis.scan_pairs()` の結果をそのまま使う（重複実装しない）。
    `analysis` は cv2 / numpy を引く重いモジュールなので、import は呼び出し時に遅延させる
    （`analysis` → `config` の import 方向と衝突させないためでもある）。
    """
    from . import analysis

    resolved = (root if root is not None else source_root()).resolve()
    source_info, _ = analysis.scan_pairs(resolved)
    info = _stat_or_none(resolved)
    exists = info is not None and stat_module.S_ISDIR(info.st_mode)
    return {
        "path": str(resolved),
        "display_path": display_path(resolved),
        "is_default": resolved == DEFAULT_SOURCE_ROOT.resolve(),
        "exists": exists,
        "readable": exists and _probe_readable(resolved),
        "pairs": source_info["pairs"],
        "jpg_only": source_info["jpg_only"],
        "arw_only": source_info["arw_only"],
        "total_bytes": source_info["total_bytes"],
    }
