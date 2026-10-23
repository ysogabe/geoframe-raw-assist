"""旧 rawpy / ``local_raw`` 経路（アーカイブ）の明示 opt-in ゲート。

既定の評価経路はアプリ本体にも rawpy / lensfunpy にも依存しない。旧エンジンで基準画像・
候補を作り直す経路だけが ``local_raw`` を要し、呼び出し側の ``--legacy-rawpy`` 等の明示フラグと
``requirements-raw.txt`` の環境・``local_raw/`` の checkout を前提とする（docs/evaluation-paths.md）。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PREREQUISITE = (
    "旧 rawpy/local_raw 経路（アーカイブ、明示 opt-in）です。Python 本体（local_raw/）は #90 / #117 で削除されるため、"
    "削除前のコミットの checkout（local_raw/ と requirements-raw.txt が残るもの）で、その環境を用意して "
    "--legacy-rawpy 等の明示フラグを付けて実行してください。既定の評価は凍結基準（--frozen-manifest）と "
    "Core Image の出力を使います（docs/evaluation-paths.md）"
)


class LegacyUnavailable(RuntimeError):
    """旧経路を opt-in せずに呼んだ、または前提（local_raw / rawpy）が無い。"""


def require(feature: str, enabled: bool) -> None:
    """``enabled`` でなければ ``LegacyUnavailable``。opt-in の有無だけを見る（import はしない）。"""
    if not enabled:
        raise LegacyUnavailable(f"{feature}: {PREREQUISITE}")


def import_module(name: str, feature: str) -> ModuleType:
    """opt-in 済みの旧経路から ``local_raw.*`` / ``rawpy`` を読む。無ければ前提を説明して止める。"""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        return importlib.import_module(name)
    except ImportError as error:
        raise LegacyUnavailable(f"{feature}: {name} を読めません（{error}）。{PREREQUISITE}") from error
