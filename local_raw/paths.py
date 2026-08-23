"""アプリの状態ファイル（設定・認証トークン）の保存先ディレクトリ解決と、旧パスからの移行。

このモジュールは他のどのモジュールにも依存しない葉ノード（config.py はほぼ全エントリ
ポイントの最上流にあるため、ここに重い依存や循環importを持ち込まない）。
外部ライブラリは使わない（標準ライブラリのみ）。
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

#: tauri.conf.json の `identifier` と一致させること。
APP_IDENTIFIER = "com.geoframe.rawassist"

__all__ = ["APP_IDENTIFIER", "app_data_dir", "migrate_legacy_file"]


def app_data_dir() -> Path:
    """OS標準のアプリデータディレクトリを返す（このディレクトリ自体は作成しない）。

    - macOS: ~/Library/Application Support/<APP_IDENTIFIER>
    - Windows: %APPDATA%/<APP_IDENTIFIER>（APPDATA未設定時は ~/AppData/Roaming/<APP_IDENTIFIER>）
    - それ以外（Linux等）: $XDG_DATA_HOME/<APP_IDENTIFIER>（未設定時は ~/.local/share/<APP_IDENTIFIER>）

    ホーム解決に失敗した場合（HOME 未設定・passwd エントリなし等、極めて稀だが実例がある）は
    カレントディレクトリ配下へフォールバックする。`config.CONFIG_PATH` のように **import 時**に
    ここが評価される予定なので、例外を外に漏らすと `import local_raw.config` 自体が失敗して
    アプリが一切起動できなくなる（`config.display_path()` の `Path.home()` ガードと同じ理由）。
    """

    def _home() -> Path:
        try:
            return Path.home()
        except (OSError, RuntimeError, KeyError):
            # ⚠️ この行き先は `.gitignore` の守備範囲外になりうる。Tauri は
            # `current_dir(repo_root())` でエンジンを起動するため、ここでの `Path.cwd()` は
            # リポジトリルートになり、`<repo>/Library/Application Support/...` へ
            # 生のリフレッシュトークンが書かれる（`.gitignore` の
            # `/Library/` `/.local/` `/AppData/` はそのための保険）。
            # ⚠️ 防げるのは「意図しないコミット」だけで、**書き込みそのものは防げない**。
            # 警告は出すが**移行は止めない**
            # （`migrate_legacy_file` の「例外は一切投げない」契約を保つ）。
            fallback = Path.cwd()
            print(
                f"[paths] HOME を解決できないため {fallback} へフォールバックします。",
                file=sys.stderr,
            )
            return fallback

    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support" / APP_IDENTIFIER
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return (Path(base) if base else _home() / "AppData" / "Roaming") / APP_IDENTIFIER
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else _home() / ".local" / "share") / APP_IDENTIFIER


def migrate_legacy_file(old_path: Path, new_path: Path, *, mode: int | None = None) -> bool:
    """old_path にだけ実体があれば new_path へ一度だけ移す。例外は一切投げない。

    冪等性の条件は「new_path が既に存在するか」**だけ**。old_path が（削除権限が無い等の
    理由で）消せずに残っていても、new_path が存在する限り再移行はしない。

    mode を指定した場合、new_path は os.open(..., O_CREAT|O_EXCL, mode) で作成時から
    そのパーミッションにする（drive_upload._write_token_payload と同じ、write→chmod間の
    窓を作らない方式）。mode を省略した場合は通常の書き込み（既定パーミッション）。

    書き込みは tmp → os.replace のアトミックパターン（new_path と同じディレクトリに
    一時ファイルを作る。config._write_config / drive_upload._write_token_payload と同方式）。
    親ディレクトリが無ければ作成する（mkdir(parents=True, exist_ok=True)）。

    戻り値: 実際に移行が発生した（old_path を new_path へ移せた）なら True。
    それ以外（old_path が無い / new_path が既にある / 途中で失敗した）は False。
    """
    if new_path.exists():
        # 冪等性の判定はここだけ。old_path が消せずに残っていても再移行しない。
        return False
    if not old_path.is_file():
        return False

    try:
        data = old_path.read_bytes()
    except OSError:
        return False

    # ⚠️ 一時ファイル名は `try` に入る**前**に束縛する。mkdir が失敗したときに
    # `finally` が未束縛の名前へ触れて NameError（= OSError ではないので外へ漏れる）に
    # なるのを防ぐ。`Path.with_suffix` は拡張子を丸ごと置換してしまうため使わない
    # （`drive_auth.json` → `drive_auth.tmp` になり、元の名前が消える）。
    temporary = new_path.with_name(f"{new_path.name}.{os.getpid()}-{uuid.uuid4().hex}.tmp")
    try:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        if mode is not None:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
        else:
            temporary.write_bytes(data)
        os.replace(temporary, new_path)
    except OSError:
        return False
    finally:
        # `Path.unlink(missing_ok=True)` が握り潰すのは FileNotFoundError だけ。
        # 親ディレクトリを作れなかった場合はここが NotADirectoryError / PermissionError に
        # なりうるので、後始末そのものも例外を漏らさないようにする。
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass

    try:
        old_path.unlink()
    except OSError:
        # 消せなくても移行は成功扱い。new_path が既に正なので、次回は冪等性の判定で止まる。
        pass
    return True
