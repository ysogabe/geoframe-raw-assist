"""旧 rawpy（Python）のプリセット表の凍結 `tests/fixtures/presets_legacy_rawpy.json` を検査する（Issue #80 / #81 / #90）。

#81 でフィクスチャを 2 本に分けた（設計書 docs/design/coreimage-preset-retune.md §6）:

- `src-tauri/tests/fixtures/presets.json` … Core Image 向けの値（正は Rust `src-tauri/src/presets.rs`）。
  Rust のテストが一致を検証し、フロント `src/lib/format.test.ts` が値域を読む。再生成は Rust 側
  （`UPDATE_PRESETS_FIXTURE=1 cargo test write_presets_fixture -- --ignored`）。
- `tests/fixtures/presets_legacy_rawpy.json` … 旧 rawpy 経路の `local_raw/presets.py` の値の凍結。
  #90 / #117 で Python 本体を削除した後は**この JSON が旧値の正**で、更新しない。

数値は意図して違う。名前・ラベル・説明・並び・キー構成・値域・NR の選択肢の一致を、アプリ本体に依存せず
2 本の JSON だけで検査する（契約 docs/API_CONTRACT.md §5 の凍結）:

    python scripts/export_presets_fixture.py --check

旧経路（アーカイブ。local_raw/ が残る checkout だけ）:

    .venv/bin/python scripts/export_presets_fixture.py --check-source   # presets.py と文字列一致
    .venv/bin/python scripts/export_presets_fixture.py --write          # presets.py から再生成
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evaluation import legacy  # noqa: E402


def render_payload(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def structure_problems(legacy_text: str, ci_payload: dict) -> list[str]:
    """凍結 legacy 表と CI 表の構造検査（アプリ本体に依存しない）。問題の一覧を返す（空なら一致）。"""
    problems: list[str] = []
    data = json.loads(legacy_text)
    if legacy_text != render_payload(data):
        problems.append("legacy JSON が正規の書式（indent 2・末尾改行・float 表記）ではありません")
    if list(data) != ["presets", "adjustment_ranges", "noise_reduction_choices"]:
        problems.append(f"legacy JSON のトップレベルキーが違います: {list(data)}")
    ranges = data.get("adjustment_ranges") or {}
    choices = data.get("noise_reduction_choices") or []
    if ranges != ci_payload.get("adjustment_ranges"):
        problems.append("adjustment_ranges が CI 表と違います")
    if choices != ci_payload.get("noise_reduction_choices"):
        problems.append("noise_reduction_choices が CI 表と違います")

    def head(presets: list) -> list:
        return [(p.get("name"), p.get("label"), p.get("description")) for p in presets]

    if head(data.get("presets") or []) != head(ci_payload.get("presets") or []):
        problems.append("name / label / description / 並びが CI 表と違います")
    keys = set(ranges) | {"noise_reduction"}
    for preset in data.get("presets") or []:
        name = preset.get("name")
        adjustments = preset.get("adjustments") or {}
        if set(adjustments) != keys:
            problems.append(f"{name}: Adjustments のキー構成が違います")
            continue
        for key, value in adjustments.items():
            if key == "noise_reduction":
                if value not in choices:
                    problems.append(f"{name}.noise_reduction が選択肢外です: {value!r}")
            elif not isinstance(value, float) or not (ranges[key][0] <= value <= ranges[key][1]):
                problems.append(f"{name}.{key} が float の値域内ではありません: {value!r}")
    return problems


def fixture_payload() -> dict:
    """旧経路: `local_raw/presets.py` からフィクスチャの中身を作る（キーの並びは Python の定義順のまま）。"""
    presets = legacy.import_module("local_raw.presets", "presets_legacy_rawpy.json の再生成・突き合わせ")
    return {
        "presets": presets.preset_infos(),
        "adjustment_ranges": {key: [minimum, maximum] for key, (minimum, maximum) in presets.ADJUSTMENT_RANGES.items()},
        "noise_reduction_choices": list(presets.NOISE_REDUCTION_CHOICES),
    }


def render() -> str:
    return render_payload(fixture_payload())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="凍結 JSON と CI 表の構造を検査する（アプリ本体は不要）")
    group.add_argument("--check-source", action="store_true", help="旧経路: local_raw/presets.py と文字列一致するか確かめる")
    group.add_argument("--write", action="store_true", help="旧経路: local_raw/presets.py からフィクスチャを上書きする")
    args = parser.parse_args(argv)

    if args.check:
        problems = structure_problems(FIXTURE_PATH.read_text(encoding="utf-8"),
                                      json.loads(CI_FIXTURE_PATH.read_text(encoding="utf-8")))
        for problem in problems:
            print(problem, file=sys.stderr)
        if not problems:
            print("ok")
        return 1 if problems else 0
    try:
        text = render()
    except legacy.LegacyUnavailable as error:
        print(str(error), file=sys.stderr)
        return 2
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
