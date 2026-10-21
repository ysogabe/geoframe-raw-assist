"""rawpy（Python）のプリセット表の写し `tests/fixtures/presets_legacy_rawpy.json` を書き出す（Issue #80 / #81）。

#81 でフィクスチャを 2 本に分けた（設計書 docs/design/coreimage-preset-retune.md §6）:

- `src-tauri/tests/fixtures/presets.json` … Core Image 向けの値（正は Rust `src-tauri/src/presets.rs`）。
  Rust のテストが一致を検証し、フロント `src/lib/format.test.ts` が値域を読む。再生成は Rust 側
  （`UPDATE_PRESETS_FIXTURE=1 cargo test write_presets_fixture -- --ignored`）。
- `tests/fixtures/presets_legacy_rawpy.json` … **このスクリプトが書く**。rawpy の本現像・書き出しが使う
  `local_raw/presets.py` の値（移行完了 #90 まで旧値のまま）。

数値は意図して違う。名前・ラベル・説明・並び・キー構成・値域・NR の選択肢の一致は
`tests/test_presets_fixture.py` が 2 本を突き合わせて守る（契約 docs/API_CONTRACT.md §5 の凍結）。

再生成（presets.py の値を意図して変えたとき）:

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
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "presets_legacy_rawpy.json"
#: Core Image 向けの値（正は Rust）。構造の突き合わせにだけ使う（このスクリプトは書かない）。
CI_FIXTURE_PATH = REPO_ROOT / "src-tauri" / "tests" / "fixtures" / "presets.json"

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
        print(f"{FIXTURE_PATH.relative_to(REPO_ROOT)} が local_raw/presets.py と一致しません（--write で再生成）", file=sys.stderr)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
