"""現像エンジンの検証（サンプル画像に依存しない範囲）。"""

import unittest

from local_raw.engine import RawDevelopmentError, _params_hash, _shadow_lift_table, develop_raw
from local_raw.presets import (
    ADJUSTMENT_KEYS,
    ADJUSTMENT_RANGES,
    DEFAULT_PRESET,
    PRESETS,
    preset_infos,
    resolve_adjustments,
)


class RawEngineValidationTests(unittest.TestCase):
    """Wave 0 から引き継いだ入力検証（回帰確認）。"""

    def test_rejects_path_traversal(self):
        with self.assertRaises(RawDevelopmentError):
            develop_raw("../DSC02943", "nightscape")

    def test_rejects_unknown_preset(self):
        with self.assertRaises(RawDevelopmentError):
            develop_raw("DSC02943", "unknown")

    def test_rejects_out_of_range_size(self):
        with self.assertRaises(RawDevelopmentError):
            develop_raw("DSC02943", "nightscape", max_edge=800)


class PresetDefinitionTests(unittest.TestCase):
    def test_has_nine_presets(self):
        self.assertEqual(len(PRESETS), 9)

    def test_default_preset_exists(self):
        self.assertIn(DEFAULT_PRESET, PRESETS)

    def test_every_preset_has_all_adjustment_keys(self):
        for name, preset in PRESETS.items():
            with self.subTest(preset=name):
                self.assertIn("label", preset)
                self.assertIn("description", preset)
                self.assertTrue(preset["description"], "description は空にしない")
                self.assertEqual(set(preset["adjustments"]), set(ADJUSTMENT_KEYS))

    def test_preset_values_are_within_contract_ranges(self):
        for name, preset in PRESETS.items():
            for key, (minimum, maximum) in ADJUSTMENT_RANGES.items():
                with self.subTest(preset=name, key=key):
                    self.assertGreaterEqual(preset["adjustments"][key], minimum)
                    self.assertLessEqual(preset["adjustments"][key], maximum)

    def test_preset_infos_matches_contract_shape(self):
        infos = preset_infos()
        self.assertEqual(len(infos), 9)
        for info in infos:
            self.assertEqual(set(info), {"name", "label", "description", "adjustments"})
            self.assertEqual(set(info["adjustments"]), set(ADJUSTMENT_KEYS))

    def test_preset_infos_is_a_copy(self):
        infos = preset_infos()
        infos[0]["adjustments"]["contrast"] = 99.0
        self.assertNotEqual(PRESETS[infos[0]["name"]]["adjustments"]["contrast"], 99.0)


class ResolveAdjustmentsTests(unittest.TestCase):
    def test_returns_all_keys_without_overrides(self):
        resolved = resolve_adjustments("nightscape")
        self.assertEqual(set(resolved), set(ADJUSTMENT_KEYS))
        self.assertEqual(resolved["noise_reduction"], "full")

    def test_merges_partial_overrides(self):
        resolved = resolve_adjustments("nightscape", {"contrast": 1.25})
        self.assertEqual(resolved["contrast"], 1.25)
        # 未指定キーはプリセット既定値のまま
        self.assertEqual(resolved["shadow_lift"], PRESETS["nightscape"]["adjustments"]["shadow_lift"])

    def test_does_not_mutate_preset(self):
        before = dict(PRESETS["nightscape"]["adjustments"])
        resolve_adjustments("nightscape", {"contrast": 1.4})
        self.assertEqual(PRESETS["nightscape"]["adjustments"], before)

    def test_clamps_out_of_range_values(self):
        resolved = resolve_adjustments(DEFAULT_PRESET, {"exposure": 9.0, "saturation": -3.0})
        self.assertEqual(resolved["exposure"], 2.0)
        self.assertEqual(resolved["saturation"], 0.0)

    def test_clamps_each_numeric_key_at_both_ends(self):
        for key, (minimum, maximum) in ADJUSTMENT_RANGES.items():
            with self.subTest(key=key):
                self.assertEqual(resolve_adjustments(DEFAULT_PRESET, {key: -1e6})[key], minimum)
                self.assertEqual(resolve_adjustments(DEFAULT_PRESET, {key: 1e6})[key], maximum)

    def test_rejects_unknown_key(self):
        with self.assertRaises(RawDevelopmentError):
            resolve_adjustments(DEFAULT_PRESET, {"gamma": 1.0})

    def test_rejects_unknown_preset(self):
        with self.assertRaises(RawDevelopmentError):
            resolve_adjustments("nope")

    def test_rejects_non_numeric_value(self):
        with self.assertRaises(RawDevelopmentError):
            resolve_adjustments(DEFAULT_PRESET, {"contrast": "1.2"})

    def test_rejects_boolean_value(self):
        # bool は int のサブクラスなので、素通しすると True が 1.0 になってしまう
        with self.assertRaises(RawDevelopmentError):
            resolve_adjustments(DEFAULT_PRESET, {"exposure": True})

    def test_rejects_non_finite_value(self):
        with self.assertRaises(RawDevelopmentError):
            resolve_adjustments(DEFAULT_PRESET, {"exposure": float("nan")})

    def test_rejects_invalid_noise_reduction(self):
        with self.assertRaises(RawDevelopmentError):
            resolve_adjustments(DEFAULT_PRESET, {"noise_reduction": "medium"})

    def test_accepts_every_noise_reduction_choice(self):
        for choice in ("off", "light", "full"):
            with self.subTest(choice=choice):
                resolved = resolve_adjustments(DEFAULT_PRESET, {"noise_reduction": choice})
                self.assertEqual(resolved["noise_reduction"], choice)

    def test_rejects_non_dict_overrides(self):
        with self.assertRaises(RawDevelopmentError):
            resolve_adjustments(DEFAULT_PRESET, [("contrast", 1.2)])

    def test_numbers_are_normalised_to_float(self):
        resolved = resolve_adjustments(DEFAULT_PRESET, {"shadow_lift": 20})
        self.assertIsInstance(resolved["shadow_lift"], float)


class ParamsHashTests(unittest.TestCase):
    def _hash(self, **kwargs):
        base = {
            "adjustments": resolve_adjustments(DEFAULT_PRESET),
            "preset_name": DEFAULT_PRESET,
            "max_edge": 6000,
            "preview": False,
            "lens_applied": True,
        }
        base.update(kwargs)
        return _params_hash(**base)

    def test_is_ten_characters(self):
        self.assertEqual(len(self._hash()), 10)

    def test_is_stable_for_identical_input(self):
        self.assertEqual(self._hash(), self._hash())

    def test_changes_when_any_single_adjustment_changes(self):
        baseline = self._hash()
        for key in ADJUSTMENT_KEYS:
            override = "full" if key == "noise_reduction" else 0.9
            if key == "noise_reduction" and PRESETS[DEFAULT_PRESET]["adjustments"][key] == "full":
                override = "off"
            adjustments = resolve_adjustments(DEFAULT_PRESET, {key: override})
            with self.subTest(key=key):
                self.assertNotEqual(
                    baseline, self._hash(adjustments=adjustments), f"{key} がハッシュに反映されていない"
                )

    def test_changes_with_preset_max_edge_preview_and_lens(self):
        baseline = self._hash()
        self.assertNotEqual(baseline, self._hash(preset_name="nightscape"))
        self.assertNotEqual(baseline, self._hash(max_edge=3000))
        self.assertNotEqual(baseline, self._hash(preview=True))
        self.assertNotEqual(baseline, self._hash(lens_applied=False))

    def test_int_and_float_adjustments_hash_identically(self):
        # resolve_adjustments が float 正規化するので 0 と 0.0 は同じキーになる
        via_int = resolve_adjustments(DEFAULT_PRESET, {"wb_temp": 0})
        via_float = resolve_adjustments(DEFAULT_PRESET, {"wb_temp": 0.0})
        self.assertEqual(self._hash(adjustments=via_int), self._hash(adjustments=via_float))


class ShadowLiftTableTests(unittest.TestCase):
    def test_returns_none_when_disabled(self):
        self.assertIsNone(_shadow_lift_table(0))

    def test_has_256_entries_within_byte_range(self):
        table = _shadow_lift_table(100)
        self.assertEqual(len(table), 256)
        self.assertTrue(all(0 <= value <= 255 for value in table))

    def test_lifts_shadows_more_than_highlights(self):
        table = _shadow_lift_table(50)
        self.assertGreater(table[64] - 64, table[224] - 224)

    def test_endpoints_are_preserved(self):
        table = _shadow_lift_table(100)
        self.assertEqual(table[0], 0)
        self.assertEqual(table[255], 255)

    def test_is_monotonic(self):
        table = _shadow_lift_table(100)
        self.assertEqual(table, sorted(table))

    def test_stronger_lift_brightens_more(self):
        self.assertGreater(_shadow_lift_table(100)[85], _shadow_lift_table(25)[85])


if __name__ == "__main__":
    unittest.main()
