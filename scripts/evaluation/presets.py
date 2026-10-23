"""凍結済みプリセット表の読み込み（アプリのプリセット実装には依存しない）。

- ``LEGACY_FIXTURE``（``tests/fixtures/presets_legacy_rawpy.json``）… 旧 rawpy 経路の
  ``local_raw/presets.py`` を #81 で凍結した写し。値域・NR の選択肢・プリセット名はここを正とする
  （Python 本体の削除後もこの JSON は更新しない）。
- ``CI_FIXTURE``（``src-tauri/tests/fixtures/presets.json``）… Core Image 向けの現行値（正は Rust）。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "presets_legacy_rawpy.json"
CI_FIXTURE = PROJECT_ROOT / "src-tauri" / "tests" / "fixtures" / "presets.json"

#: 既定プリセット（契約 docs/API_CONTRACT.md §5 の先頭。旧 rawpy・Core Image 共通）。
DEFAULT_PRESET = "neutral_recovery"


@lru_cache(maxsize=None)
def load_table(path: Path = LEGACY_FIXTURE) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("presets"), list):
        raise ValueError(f"プリセット表の形式が不正です: {path}")
    return payload


def _legacy() -> dict[str, Any]:
    return load_table(LEGACY_FIXTURE)


#: 数値 Adjustments の許容範囲（契約 §2。凍結 JSON の並び順のまま）。
ADJUSTMENT_RANGES: dict[str, tuple[float, float]] = {
    key: (float(lo), float(hi)) for key, (lo, hi) in _legacy()["adjustment_ranges"].items()
}
NOISE_REDUCTION_CHOICES: tuple[str, ...] = tuple(_legacy()["noise_reduction_choices"])
ADJUSTMENT_KEYS: tuple[str, ...] = (*ADJUSTMENT_RANGES.keys(), "noise_reduction")
#: 凍結プリセット名（契約 §5 の並び）。
PRESET_NAMES: tuple[str, ...] = tuple(p["name"] for p in _legacy()["presets"])


def preset_adjustments(name: str, path: Path = LEGACY_FIXTURE) -> dict[str, Any]:
    """凍結表 ``path`` のプリセット ``name`` の Adjustments（コピー）。無ければ KeyError。"""
    for preset in load_table(path)["presets"]:
        if preset.get("name") == name:
            return dict(preset["adjustments"])
    raise KeyError(f"{Path(path).name} に無いプリセットです: {name}")
