"""プリセット定義の一致フィクスチャ `src-tauri/tests/fixtures/presets.json` を書き出す（Issue #80）。

プリセット表と Adjustments の値域は、移行期間中 Python（`local_raw/presets.py`）と
Rust（`src-tauri/src/presets.rs` / `adjustments.rs`）の両方にある。定義の正は Rust 側だが、
両者がずれないようにこのフィクスチャを介して 3 か所をテストで突き合わせる。

- Python: `tests/test_presets_fixture.py`（`preset_infos()` / `ADJUSTMENT_RANGES` と一致するか）
- Rust:   `presets.rs` の `#[cfg(test)]`（`preset_infos()` / `ADJUSTMENT_RANGES` と一致するか）
- TS:     `src/lib/format.test.ts`（`SLIDER_SPECS` の min/max と一致するか）

再生成（プリセットの値を意図して変えたとき。Python と Rust の両方を同じ値に直してから実行する）:

    .venv/bin/python scripts/export_presets_fixture.py --write

一致確認だけ（書き込まない。ずれていれば終了コード 1）:

    .venv/bin/python scripts/export_presets_fixture.py --check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "src-tauri" / "tests" / "fixtures" / "presets.json"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_raw.presets import (  # noqa: E402
    ADJUSTMENT_RANGES,
    NOISE_REDUCTION_CHOICES,
    preset_infos,
)


def fixture_payload() -> dict:
    """フィクスチャの中身。キーの並びは Python の定義順のまま（sort しない）。"""
    return {
        "presets": preset_infos(),
        "adjustment_ranges": {key: [minimum, maximum] for key, (minimum, maximum) in ADJUSTMENT_RANGES.items()},
        "noise_reduction_choices": list(NOISE_REDUCTION_CHOICES),
    }


def render() -> str:
    return json.dumps(fixture_payload(), ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="フィクスチャを上書きする")
    group.add_argument("--check", action="store_true", help="フィクスチャと一致するか確かめる")
    args = parser.parse_args(argv)

    text = render()
    if args.write:
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE_PATH.write_text(text, encoding="utf-8")
        print(f"wrote {FIXTURE_PATH.relative_to(REPO_ROOT)}")
        return 0
    current = FIXTURE_PATH.read_text(encoding="utf-8") if FIXTURE_PATH.is_file() else ""
    if current != text:
        print(f"{FIXTURE_PATH.relative_to(REPO_ROOT)} が presets.py と一致しません（--write で再生成）", file=sys.stderr)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
