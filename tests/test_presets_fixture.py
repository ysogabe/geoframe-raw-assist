"""プリセット定義のフィクスチャと `local_raw/presets.py` の突き合わせ（Issue #80 / #81）。

#81 でフィクスチャを 2 本に分けた（設計書 docs/design/coreimage-preset-retune.md §6）:

- `tests/fixtures/presets_legacy_rawpy.json` … rawpy（Python）の旧値。`export_presets_fixture.py --write` が書き、
  このテストが `presets.py` と数値まで突き合わせる。
- `src-tauri/tests/fixtures/presets.json` … Core Image 向けの値（正は Rust）。数値は意図して違うので、
  ここでは**構造**（name / label / description / 並び / キー構成 / 値域 / NR の選択肢）の一致だけを見る。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import export_presets_fixture as fixture  # noqa: E402

from local_raw.presets import ADJUSTMENT_RANGES, NOISE_REDUCTION_CHOICES, preset_infos  # noqa: E402


class LegacyPresetsFixtureTest(unittest.TestCase):
    def setUp(self):
        self.text = fixture.FIXTURE_PATH.read_text(encoding="utf-8")
        self.data = json.loads(self.text)

    def test_fixture_path_is_the_legacy_file(self):
        self.assertEqual(fixture.FIXTURE_PATH.name, "presets_legacy_rawpy.json")
        self.assertNotEqual(fixture.FIXTURE_PATH, fixture.CI_FIXTURE_PATH)

    def test_fixture_text_is_exactly_what_the_exporter_renders(self):
        # 文字列で比べる（json.loads 後の比較では 15 と 15.0 が同一視されてしまう）。
        self.assertEqual(self.text, fixture.render(), "export_presets_fixture.py --write で再生成すること")

    def test_presets_match_preset_infos(self):
        self.assertEqual(self.data["presets"], preset_infos())
        self.assertEqual(len(self.data["presets"]), 11)

    def test_ranges_and_choices_match(self):
        self.assertEqual(
            self.data["adjustment_ranges"],
            {key: list(value) for key, value in ADJUSTMENT_RANGES.items()},
        )
        self.assertEqual(self.data["noise_reduction_choices"], list(NOISE_REDUCTION_CHOICES))

    def test_every_number_is_written_as_float(self):
        for preset in self.data["presets"]:
            for key, value in preset["adjustments"].items():
                if key != "noise_reduction":
                    self.assertIsInstance(value, float, f"{preset['name']}.{key}")


class PresetsFixtureStructureTest(unittest.TestCase):
    """CI 値（Rust）と legacy 値（Python）の 2 本で、契約 §5 の凍結部分が同一であること。"""

    def setUp(self):
        self.legacy = json.loads(fixture.FIXTURE_PATH.read_text(encoding="utf-8"))
        self.ci = json.loads(fixture.CI_FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_names_labels_descriptions_and_order_match(self):
        def head(data):
            return [(p["name"], p["label"], p["description"]) for p in data["presets"]]

        self.assertEqual(head(self.ci), head(self.legacy))

    def test_adjustment_keys_match(self):
        for ci, legacy in zip(self.ci["presets"], self.legacy["presets"]):
            self.assertEqual(set(ci["adjustments"]), set(legacy["adjustments"]), ci["name"])
            self.assertEqual(set(ci["adjustments"]), set(ADJUSTMENT_RANGES) | {"noise_reduction"}, ci["name"])

    def test_ranges_and_choices_match(self):
        self.assertEqual(self.ci["adjustment_ranges"], self.legacy["adjustment_ranges"])
        self.assertEqual(self.ci["noise_reduction_choices"], self.legacy["noise_reduction_choices"])

    def test_ci_values_are_floats_within_ranges(self):
        for preset in self.ci["presets"]:
            for key, value in preset["adjustments"].items():
                if key == "noise_reduction":
                    self.assertIn(value, NOISE_REDUCTION_CHOICES, preset["name"])
                    continue
                self.assertIsInstance(value, float, f"{preset['name']}.{key}")
                lo, hi = ADJUSTMENT_RANGES[key]
                self.assertTrue(lo <= value <= hi, f"{preset['name']}.{key} = {value}")


if __name__ == "__main__":
    unittest.main()
