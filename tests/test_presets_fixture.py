"""プリセット定義の一致フィクスチャ（Issue #80）と `local_raw/presets.py` の突き合わせ。

フィクスチャ `src-tauri/tests/fixtures/presets.json` は Rust（`src-tauri/src/presets.rs` の
テスト）とフロント（`src/lib/format.test.ts`）も読む。Python か Rust の片方だけ値を変えると、
どちらかのテストが落ちる。再生成: `.venv/bin/python scripts/export_presets_fixture.py --write`
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


class PresetsFixtureTest(unittest.TestCase):
    def setUp(self):
        self.text = fixture.FIXTURE_PATH.read_text(encoding="utf-8")
        self.data = json.loads(self.text)

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
        # Rust 側は serde_json で 15 と 15.0 を区別するので、フィクスチャの数値は必ず float 表記。
        for preset in self.data["presets"]:
            for key, value in preset["adjustments"].items():
                if key != "noise_reduction":
                    self.assertIsInstance(value, float, f"{preset['name']}.{key}")


if __name__ == "__main__":
    unittest.main()
