"""Synthetic tests for the RAW half-decode development candidate optimizer.

A real ARW is never decoded here: ``generate_development_candidates`` accepts a
``decoder_factory`` and the tests inject a fake that returns synthetic float
arrays (and records every decode call).
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlparse

import numpy as np

from local_raw import config, engine
from local_raw import development_candidates as candidates
from local_raw import server
from local_raw.presets import resolve_adjustments


def make_scene(*, stars: bool, noisy: bool = False) -> np.ndarray:
    rng = np.random.default_rng(12)
    if stars:
        image = np.full((320, 480, 3), 0.04, dtype=np.float32)
        # 星空判定は「点光源が多数あること」を要求するので、格子状の白い星を敷き詰める
        # （20x30=600 点。密度 0.0039 は EXIF あり/なし両方のしきい値を超える）。
        image[8::16, 8::16] = 0.9
        for y, x, value in (
            (50, 60, (1.0, 0.96, 0.9)),
            (140, 250, (0.94, 0.98, 1.0)),
            (250, 400, (1.0, 0.88, 0.75)),
            (290, 170, (0.9, 0.94, 1.0)),
        ):
            image[y, x] = value
        return image
    ramp = np.linspace(0.25, 0.7, 480, dtype=np.float32)[None, :, None]
    image = np.repeat(np.repeat(ramp, 320, axis=0), 3, axis=2)
    if noisy:
        image = image + rng.normal(0.0, 0.12, image.shape).astype(np.float32)
    return np.clip(image, 0.0, 1.0)


def make_star_field(*, clipped: int, unclipped: int) -> np.ndarray:
    """暗い背景に1画素の星を格子状に置く（白飛びした星 clipped 個・していない星 unclipped 個）。"""

    image = np.full((320, 480, 3), 0.04, dtype=np.float32)
    total = clipped + unclipped
    for index in range(total):
        y = 30 + 55 * (index // 8)
        x = 30 + 55 * (index % 8)
        image[y, x] = (1.0, 1.0, 1.0) if index < clipped else (0.8, 0.8, 0.8)
    return image


class FakeDecoder:
    """Deterministic stand-in for ``open_raw_half_decoder``."""

    def __init__(
        self,
        scene: np.ndarray,
        *,
        worsen_full_nr: bool = False,
        break_noise_reduction: str | None = None,
    ) -> None:
        self.scene = scene
        self.worsen_full_nr = worsen_full_nr
        # 指定した noise_reduction のデコードだけ、黒つぶれした画像を返す。
        self.break_noise_reduction = break_noise_reduction
        self.calls: list[tuple[str, str, float]] = []
        self.base: dict | None = None

    def decode(self, category: str, noise_reduction: str, exposure: float) -> np.ndarray:
        self.calls.append((category, noise_reduction, round(exposure, 4)))
        image = self.scene * float(2.0 ** (0.5 * exposure))
        if category == "reconstruct":
            image = image * 0.8
        elif category == "clip":
            image = image * 1.2
        if self.worsen_full_nr and noise_reduction == "full":
            # 実RAWで見られた「低ノイズ設定がむしろノイズを悪化させる」ケースの再現。
            rng = np.random.default_rng(5)
            image = image + rng.normal(0.0, 0.25, image.shape).astype(np.float32)
        if self.break_noise_reduction is not None and noise_reduction == self.break_noise_reduction:
            image = image * 0.02
        return np.clip(image, 0.0, 1.0).astype(np.float32)

    def factory(self, path: Path, base: dict, exif: dict | None = None):
        self.base = base

        @contextmanager
        def opened():
            yield self.decode

        return opened()


class CandidateOptimizerTests(unittest.TestCase):
    def generate(
        self,
        decoder: FakeDecoder,
        *,
        seed: int | None = 7,
        budget: int = 32,
        preset: str = "neutral_recovery",
        capture_id: str = "DSC00001",
        exif: dict | None = None,
    ) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "DSC00001.ARW").write_bytes(b"")
            if exif is not None:
                # ペア JPG があるときだけ EXIF が読まれる（read_capture_exif は差し替える）。
                (root / "DSC00001.JPG").write_bytes(b"")
            with mock.patch.object(config, "source_root", return_value=root), mock.patch.object(
                candidates, "read_capture_exif", return_value=dict(exif or {})
            ):
                return candidates.generate_development_candidates(
                    capture_id, preset, seed=seed, budget=budget, decoder_factory=decoder.factory
                )

    def test_same_input_and_seed_is_deterministic(self):
        first = self.generate(FakeDecoder(make_scene(stars=False)))
        second = self.generate(FakeDecoder(make_scene(stars=False)))
        self.assertEqual(first, second)

    def test_omitted_seed_is_stable_but_separated_by_photo_and_preset(self):
        expected = candidates.default_seed("DSC00001", "neutral_recovery")
        self.assertNotEqual(expected, candidates.default_seed("DSC00002", "neutral_recovery"))
        self.assertNotEqual(expected, candidates.default_seed("DSC00001", "nightscape"))
        result = self.generate(FakeDecoder(make_scene(stars=False)), seed=None)
        self.assertEqual(result["seed"], expected)

    def test_values_stay_in_contract_and_wb_is_not_searched(self):
        preset = "sunset_glow"  # wb_temp=12, hr=70, exposure=-0.1
        base = resolve_adjustments(preset)
        result = self.generate(FakeDecoder(make_scene(stars=True)), preset=preset)
        for candidate in result["candidates"]:
            adjustments = candidate["adjustments"]
            for key, (minimum, maximum) in candidates.ADJUSTMENT_RANGES.items():
                self.assertGreaterEqual(adjustments[key], minimum, (candidate["id"], key))
                self.assertLessEqual(adjustments[key], maximum, (candidate["id"], key))
            self.assertIn(adjustments["noise_reduction"], {"off", "light", "full"})
            self.assertEqual(adjustments["wb_temp"], base["wb_temp"])
            if candidate["id"] != "natural":
                grid = adjustments["exposure"] / candidates.EXPOSURE_GRID
                self.assertAlmostEqual(grid, round(grid), places=6)

    def test_highlight_recovery_is_one_of_three_representative_values(self):
        for preset in ("neutral_recovery", "highlight_rescue", "monochrome_street"):
            base = resolve_adjustments(preset)
            result = self.generate(FakeDecoder(make_scene(stars=True, noisy=False)), preset=preset)
            allowed = {
                candidates.representative_highlight(category, base["highlight_recovery"])
                for category in candidates.HIGHLIGHT_CATEGORIES
            }
            for candidate in result["candidates"]:
                self.assertIn(candidate["adjustments"]["highlight_recovery"], allowed, (preset, candidate["id"]))

    def test_highlight_category_boundaries_match_engine(self):
        raw = SimpleNamespace(camera_whitebalance=[1.0, 1.0, 1.0, 1.0])
        import rawpy

        expected = {
            "clip": rawpy.HighlightMode.Clip,
            "blend": rawpy.HighlightMode.Blend,
            "reconstruct": rawpy.HighlightMode.ReconstructDefault,
        }
        for value in (0, 1, 30, 60, 74, 74.9, 75, 85, 100):
            category = candidates.highlight_category(value)
            adjustments = resolve_adjustments("neutral_recovery", {"highlight_recovery": value})
            params = engine._postprocess_params(raw, adjustments, False)
            self.assertEqual(params["highlight_mode"], expected[category], value)
        for category, value in candidates.HIGHLIGHT_REPRESENTATIVE.items():
            self.assertEqual(candidates.highlight_category(value), category)
        # プリセット値が同カテゴリならプリセット値を優先する。
        self.assertEqual(candidates.representative_highlight("blend", 60), 60)
        self.assertEqual(candidates.representative_highlight("reconstruct", 90), 90)
        self.assertEqual(candidates.representative_highlight("clip", 60), 0)
        self.assertEqual(candidates.representative_highlight("reconstruct", 60), 85)

    def test_metrics_do_not_change_within_a_highlight_category(self):
        decoder = FakeDecoder(make_scene(stars=False))
        base = resolve_adjustments("neutral_recovery")
        cache = candidates.DecodeCache(decoder.decode)
        evaluator = candidates._Evaluator(cache, {}, base, star_gate=False)
        results = {}
        for value in (30, 60, 74, 0, 85):
            adjustments = dict(base, highlight_recovery=value)
            results[value] = evaluator.evaluate(adjustments, False)
        self.assertEqual(results[30], results[60])
        self.assertEqual(results[60], results[74])
        self.assertNotEqual(results[60], results[85])
        self.assertNotEqual(results[60], results[0])

    def test_decode_cache_does_not_redecode_the_same_key(self):
        calls: list[tuple] = []

        def decode(category, noise_reduction, exposure):
            calls.append((category, noise_reduction, exposure))
            return np.full((8, 8, 3), 0.5, dtype=np.float32)

        cache = candidates.DecodeCache(decode)
        for _ in range(3):
            cache.get("blend", "light", 0.25)
        cache.get("blend", "light", 0.25000001)  # 丸めで同一キー
        self.assertEqual(len(calls), 1)
        cache.get("clip", "light", 0.25)
        cache.get("blend", "full", 0.25)
        cache.get("blend", "light", 0.5)
        self.assertEqual(len(calls), 4)
        self.assertEqual(cache.decode_count, 4)

    def test_generation_decodes_each_key_once(self):
        decoder = FakeDecoder(make_scene(stars=True))
        result = self.generate(decoder, budget=64)
        self.assertEqual(len(decoder.calls), len(set(decoder.calls)))
        self.assertEqual(result["proxy"]["decodes"], len(decoder.calls))
        self.assertEqual(result["proxy"]["source"], "raw_half")

    def test_baseline_decodes_at_exact_preset_exposure(self):
        decoder = FakeDecoder(make_scene(stars=False))
        self.generate(decoder, preset="sunset_glow")  # exposure -0.1 は格子外
        self.assertIn(("blend", "light", -0.1), decoder.calls)

    def test_render_proxy_matches_engine_post_decode_chain(self):
        rng = np.random.default_rng(3)
        decoded = rng.uniform(0.0, 1.0, (1300, 1000, 3)).astype(np.float32)
        adjustments = resolve_adjustments(
            "landscape_clear", {"shadow_lift": 40, "brightness": 1.1, "contrast": 1.2, "saturation": 1.3, "sharpness": 1.4}
        )
        for iso in (None, 3200.0):
            with self.subTest(iso=iso):
                expected = engine.apply_post_decode_chain(
                    decoded, adjustments, candidates.MAX_PROXY_EDGE, iso=iso
                )
                np.testing.assert_array_equal(
                    candidates.render_proxy(decoded, adjustments, iso=iso), expected
                )
                self.assertEqual(max(expected.shape[:2]), candidates.MAX_PROXY_EDGE)

    def test_raw_iso_reaches_render_proxy(self):
        """デコーダの ``iso`` 属性（RAW の ISO）が代理画像の後段まで渡る。"""
        scene = make_scene(stars=False)

        def factory(path, base):
            @contextmanager
            def opened():
                def decode(category, noise_reduction, exposure):
                    return scene

                decode.iso = 250.0
                yield decode

            return opened()

        seen: list = []
        real_render = candidates.render_proxy

        def render(decoded, adjustments, iso=None):
            seen.append(iso)
            return real_render(decoded, adjustments, iso=iso)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "DSC00001.ARW").write_bytes(b"")
            with (
                mock.patch.object(config, "source_root", return_value=root),
                mock.patch.object(candidates, "render_proxy", side_effect=render),
            ):
                candidates.generate_development_candidates(
                    "DSC00001", "neutral_recovery", seed=7, budget=32, decoder_factory=factory
                )
        self.assertTrue(seen)
        self.assertEqual(set(seen), {250.0})

    def test_unknown_iso_reaches_render_proxy_as_none(self):
        seen: list = []
        real_render = candidates.render_proxy

        def render(decoded, adjustments, iso=None):
            seen.append(iso)
            return real_render(decoded, adjustments, iso=iso)

        with mock.patch.object(candidates, "render_proxy", side_effect=render):
            self.generate(FakeDecoder(make_scene(stars=False)))
        self.assertTrue(seen)
        self.assertEqual(set(seen), {None})

    def test_baseline_deviation_is_against_natural_not_camera_jpg(self):
        decoder = FakeDecoder(make_scene(stars=False))
        result = self.generate(decoder)
        natural = result["candidates"][0]
        self.assertEqual(natural["metrics"]["source_deviation"], 0.0)
        others = [item for item in result["candidates"][1:]]
        self.assertTrue(others)
        self.assertTrue(any(item["metrics"]["source_deviation"] > 0.0 for item in others))

    def test_pareto_front_keeps_non_dominated_points(self):
        points = np.asarray([[1.0, 1.0], [0.5, 1.2], [1.2, 0.5], [2.0, 2.0]])
        self.assertEqual(candidates.pareto_front(points), [0, 1, 2])

    def test_star_style_is_gated_by_dark_small_points(self):
        result = self.generate(FakeDecoder(make_scene(stars=False)))
        self.assertFalse(result["star_scene"]["is_star_scene"])
        self.assertNotIn("stars", [item["id"] for item in result["candidates"]])

        result = self.generate(FakeDecoder(make_scene(stars=True)))
        self.assertTrue(result["star_scene"]["is_star_scene"])
        ids = [item["id"] for item in result["candidates"]] + [item["id"] for item in result["rejected"]]
        self.assertIn("stars", ids)

    def test_star_metrics_are_measured_on_natural_under_the_same_condition(self):
        decoder = FakeDecoder(make_scene(stars=True))
        result = self.generate(decoder)
        natural = result["candidates"][0]
        direct = candidates._star_measurements(
            candidates.render_proxy(decoder.decode("blend", "light", 0.0), resolve_adjustments("neutral_recovery"))
        )
        self.assertGreater(direct["star_count"], 0)
        # natural 側の star_count が 0 固定にならず、検出値と一致する。
        self.assertEqual(natural["metrics"]["star_count"], direct["star_count"])
        self.assertEqual(natural["metrics"]["star_count"], result["star_scene"]["star_count"])
        for item in result["candidates"]:
            if item["id"] == "stars":
                self.assertGreater(item["metrics"]["star_count"], 0)

    def test_large_bright_lamps_do_not_trigger_star_gate(self):
        image = np.full((1600, 2400, 3), 12, dtype=np.uint8)
        for y, x in ((180, 220), (180, 1700), (1160, 220), (1160, 1700)):
            image[y : y + 100, x : x + 140] = (255, 245, 220)
        star_scene = candidates.detect_star_scene(image)
        self.assertFalse(star_scene["is_star_scene"])
        self.assertGreater(star_scene["bright_point_ratio"], candidates.MAX_STAR_BRIGHT_RATIO)

    def test_natural_is_preset_and_other_styles_are_materially_different(self):
        base = resolve_adjustments("neutral_recovery")
        result = self.generate(FakeDecoder(make_scene(stars=True)))
        self.assertEqual(result["candidates"][0]["id"], "natural")
        natural = result["candidates"][0]["adjustments"]
        self.assertEqual(natural, base)
        self.assertTrue([item for item in result["candidates"][1:] if item["adjustments"] != natural])

    def test_worsened_candidates_are_rejected_with_reason(self):
        # 低ノイズ用の "full" デコードだけがノイズを悪化させる合成データ。
        decoder = FakeDecoder(make_scene(stars=False, noisy=True), worsen_full_nr=True)
        result = self.generate(decoder)
        ids = [item["id"] for item in result["candidates"]]
        self.assertIn("natural", ids)
        self.assertNotIn("low_noise", ids)
        reasons = {item["id"]: item["reason"] for item in result["rejected"]}
        self.assertIn("low_noise", reasons)
        self.assertIn("除外", reasons["low_noise"])
        self.assertTrue(any(word in reasons["low_noise"] for word in ("ノイズ", "白飛び", "黒つぶれ")))
        # 採用された候補は基準より黒つぶれ・白飛びが 0.10 以上悪化していない。
        natural = result["candidates"][0]["metrics"]
        for item in result["candidates"]:
            self.assertLess(item["metrics"]["shadow_clip"] - natural["shadow_clip"], candidates.CLIP_WORSEN_LIMIT)
            self.assertLess(item["metrics"]["highlight_clip"] - natural["highlight_clip"], candidates.CLIP_WORSEN_LIMIT)

    def test_guard_rules(self):
        base = {"highlight_clip": 0.02, "shadow_clip": 0.05, "noise_flat": 0.010, "shadow_mean": 0.10}
        ok = dict(base, noise_flat=0.009, shadow_mean=0.12)
        self.assertIsNone(candidates._guard_failure("low_noise", ok, base))
        self.assertIsNone(candidates._guard_failure("foreground", ok, base))
        self.assertIn("白飛び", candidates._guard_failure("social", dict(ok, highlight_clip=0.14), base))
        self.assertIn("黒つぶれ", candidates._guard_failure("social", dict(ok, shadow_clip=0.16), base))
        self.assertIsNone(candidates._guard_failure("social", dict(ok, shadow_clip=0.13), base))
        self.assertIn("ノイズ", candidates._guard_failure("low_noise", dict(ok, noise_flat=0.011), base))
        self.assertIn("暗部", candidates._guard_failure("foreground", dict(ok, shadow_mean=0.10), base))
        # ノイズ・暗部の条件は該当スタイルだけに適用される。
        self.assertIsNone(candidates._guard_failure("social", dict(ok, noise_flat=0.02, shadow_mean=0.05), base))

    def test_missing_arw_is_a_distinct_not_found_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # ペアJPGだけがあってもARWが無ければ404扱い。
            (root / "DSC00001.JPG").write_bytes(b"")
            with mock.patch.object(config, "source_root", return_value=root):
                with self.assertRaises(candidates.DevelopmentCandidateNotFound):
                    candidates.generate_development_candidates("DSC00001")

    def test_arw_outside_source_root_is_not_read(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            (Path(outside) / "DSC00001.ARW").write_bytes(b"")
            (root / "DSC00001.ARW").symlink_to(Path(outside) / "DSC00001.ARW")
            with mock.patch.object(config, "source_root", return_value=root):
                with self.assertRaises(candidates.DevelopmentCandidateNotFound):
                    candidates.generate_development_candidates("DSC00001")

    def test_request_validation_is_kept(self):
        decoder = FakeDecoder(make_scene(stars=False))
        for capture_id in ("../DSC00001", "DSC0001", "dsc00001", "DSC00001.ARW", "DSC000011"):
            with self.subTest(capture_id=capture_id), self.assertRaises(candidates.DevelopmentCandidateError):
                self.generate(decoder, capture_id=capture_id)
        for budget in (31, 513, True, 48.0, "48"):
            with self.subTest(budget=budget), self.assertRaises(candidates.DevelopmentCandidateError):
                self.generate(decoder, budget=budget)  # type: ignore[arg-type]
        for seed in (-1, 2**32, True):
            with self.subTest(seed=seed), self.assertRaises(candidates.DevelopmentCandidateError):
                self.generate(decoder, seed=seed)
        with self.assertRaises(candidates.DevelopmentCandidateError):
            self.generate(decoder, preset="no_such_preset")
        self.assertEqual(decoder.calls, [])

    # ------------------------------------------------------------------
    # star_color_saturation: 星ごとの白飛び割合の平均
    # ------------------------------------------------------------------

    def test_star_color_saturation_is_the_mean_over_stars_not_a_sum(self):
        # 40個の星のうち10個だけ白飛び。合計にすると1.0に張り付くが、平均なら 10/40。
        # 星の指標は候補評価と同じ evaluate_gate=False 経路で測る（判定に依らない）。
        measured = candidates._star_measurements(make_star_field(clipped=10, unclipped=30), evaluate_gate=False)
        self.assertEqual(measured["star_count"], 40)
        self.assertAlmostEqual(measured["star_color_saturation"], 0.25, places=6)

    def test_star_color_saturation_spans_zero_to_one_with_the_clipped_share(self):
        none_clipped = candidates._star_measurements(make_star_field(clipped=0, unclipped=40), evaluate_gate=False)
        all_clipped = candidates._star_measurements(make_star_field(clipped=40, unclipped=0), evaluate_gate=False)
        few_stars = candidates._star_measurements(make_star_field(clipped=3, unclipped=9), evaluate_gate=False)
        self.assertEqual(none_clipped["star_color_saturation"], 0.0)
        self.assertEqual(all_clipped["star_color_saturation"], 1.0)
        self.assertAlmostEqual(few_stars["star_color_saturation"], 0.25, places=6)

    def test_star_color_saturation_is_zero_when_no_star_is_detected(self):
        flat = np.full((320, 480, 3), 0.04, dtype=np.float32)
        measured = candidates._star_measurements(flat)
        self.assertEqual(measured["star_count"], 0)
        self.assertEqual(measured["star_color_saturation"], 0.0)

    # ------------------------------------------------------------------
    # 重複回避で動かした値も安全チェックを通す
    # ------------------------------------------------------------------

    def test_moved_adjustments_must_pass_the_safety_check(self):
        # 探索結果が natural とほぼ同じ値になり、重複回避で動かす経路を強制する。
        chosen = dict(resolve_adjustments("neutral_recovery"), contrast=1.05)

        def fake_optimise(evaluator, style, star_reference, budget, seed):
            return dict(chosen), evaluator.metrics, None

        def social_adjustments(safe: bool) -> dict:
            with (
                mock.patch.object(candidates, "_optimise_style", side_effect=fake_optimise),
                mock.patch.object(candidates, "_safe_vector", return_value=safe),
            ):
                result = self.generate(FakeDecoder(make_scene(stars=False)))
            return {item["id"]: item for item in result["candidates"]}["social"]["adjustments"]

        # 移動後の値が安全チェックを通るときだけ移動値を採用する。
        self.assertNotEqual(social_adjustments(True), chosen)
        self.assertEqual(social_adjustments(False), chosen)

    # ------------------------------------------------------------------
    # 本番デコーダ open_raw_half_decoder（rawpy をモック）
    # ------------------------------------------------------------------

    def open_decoder(self, raw: mock.MagicMock, base: dict | None = None):
        import rawpy

        return mock.patch.object(rawpy, "imread", return_value=raw), candidates.open_raw_half_decoder(
            Path("/nonexistent/DSC00001.ARW"), base or resolve_adjustments("neutral_recovery")
        )

    @staticmethod
    def fake_raw() -> mock.MagicMock:
        raw = mock.MagicMock()
        raw.camera_whitebalance = [1.0, 1.0, 1.0, 1.0]
        raw.postprocess.return_value = np.full((10, 12, 3), 32768, dtype=np.uint16)
        return raw

    def test_production_decoder_passes_half_size_and_engine_params(self):
        import rawpy

        raw = self.fake_raw()
        base = resolve_adjustments("neutral_recovery")  # highlight_recovery=60 (blend)
        with mock.patch.object(rawpy, "imread", return_value=raw):
            with candidates.open_raw_half_decoder(Path("/nonexistent/DSC00001.ARW"), base) as decode:
                result = decode("reconstruct", "full", 0.3)
        self.assertEqual(result.dtype, np.float32)
        self.assertEqual(result.shape, (10, 12, 3))
        raw.postprocess.assert_called_once()
        kwargs = raw.postprocess.call_args.kwargs
        self.assertIs(kwargs["half_size"], True)
        self.assertEqual(kwargs["highlight_mode"], rawpy.HighlightMode.ReconstructDefault)
        self.assertEqual(kwargs["fbdd_noise_reduction"], rawpy.FBDDNoiseReductionMode.Full)
        self.assertEqual(kwargs["median_filter_passes"], 1)
        # exposure は格子へスナップせず、渡された値そのものが bright になる。
        self.assertAlmostEqual(kwargs["bright"], 2.0**0.3, places=9)
        self.assertNotAlmostEqual(kwargs["bright"], 2.0**0.5, places=3)
        self.assertEqual(kwargs["exp_shift"], 1.0)
        self.assertIs(kwargs["no_auto_bright"], False)

    def test_production_decoder_exposes_raw_iso_and_adapts_fbdd(self):
        import rawpy

        raw = self.fake_raw()
        raw.other = SimpleNamespace(iso_speed=100.0)
        base = resolve_adjustments("neutral_recovery")
        with mock.patch.object(rawpy, "imread", return_value=raw):
            with candidates.open_raw_half_decoder(Path("/nonexistent/DSC00001.ARW"), base) as decode:
                self.assertEqual(decode.iso, 100.0)
                decode("blend", "light", 0.0)
        self.assertEqual(
            raw.postprocess.call_args.kwargs["fbdd_noise_reduction"], rawpy.FBDDNoiseReductionMode.Off
        )

        # MagicMock の other.iso_speed（数値ではない）は ISO 不明として扱う。
        raw = self.fake_raw()
        with mock.patch.object(rawpy, "imread", return_value=raw):
            with candidates.open_raw_half_decoder(Path("/nonexistent/DSC00001.ARW"), base) as decode:
                self.assertIsNone(decode.iso)

    def test_production_decoder_maps_categories_and_noise_reduction(self):
        import rawpy

        expected_highlight = {
            "clip": rawpy.HighlightMode.Clip,
            "blend": rawpy.HighlightMode.Blend,
            "reconstruct": rawpy.HighlightMode.ReconstructDefault,
        }
        expected_noise = {
            "off": (rawpy.FBDDNoiseReductionMode.Off, 0),
            "light": (rawpy.FBDDNoiseReductionMode.Light, 0),
            "full": (rawpy.FBDDNoiseReductionMode.Full, 1),
        }
        raw = self.fake_raw()
        base = resolve_adjustments("neutral_recovery")
        with mock.patch.object(rawpy, "imread", return_value=raw):
            with candidates.open_raw_half_decoder(Path("/nonexistent/DSC00001.ARW"), base) as decode:
                for category, highlight in expected_highlight.items():
                    for noise, (fbdd, median) in expected_noise.items():
                        raw.postprocess.reset_mock()
                        decode(category, noise, 0.0)
                        kwargs = raw.postprocess.call_args.kwargs
                        self.assertEqual(kwargs["highlight_mode"], highlight, (category, noise))
                        self.assertEqual(kwargs["fbdd_noise_reduction"], fbdd, (category, noise))
                        self.assertEqual(kwargs["median_filter_passes"], median, (category, noise))
                        self.assertIs(kwargs["half_size"], True)

    def test_production_decoder_dark_scene_uses_fixed_gain(self):
        import rawpy

        raw = self.fake_raw()
        base = resolve_adjustments("neutral_recovery")
        dark = {"f_number": 2.8, "exposure_time": 15.0, "iso": 3200}
        with mock.patch.object(rawpy, "imread", return_value=raw):
            with candidates.open_raw_half_decoder(
                Path("/nonexistent/DSC00001.ARW"), base, dark
            ) as decode:
                decode("blend", "off", 0.3)
        kwargs = raw.postprocess.call_args.kwargs
        self.assertIs(kwargs["half_size"], True)
        self.assertIs(kwargs["no_auto_bright"], True)
        self.assertNotIn("auto_bright_thr", kwargs)
        self.assertAlmostEqual(kwargs["bright"], 5.0 * 2.0**0.3, places=9)

    def test_production_decoder_bright_scene_keeps_auto_bright(self):
        import rawpy

        raw = self.fake_raw()
        base = resolve_adjustments("neutral_recovery")
        bright = {"f_number": 8.0, "exposure_time": 1 / 250, "iso": 100}
        with mock.patch.object(rawpy, "imread", return_value=raw):
            with candidates.open_raw_half_decoder(
                Path("/nonexistent/DSC00001.ARW"), base, bright
            ) as decode:
                decode("blend", "off", 0.0)
        self.assertIs(raw.postprocess.call_args.kwargs["no_auto_bright"], False)

    def test_generate_passes_pair_jpg_exif_to_default_decoder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "DSC00001.ARW").write_bytes(b"")
            (root / "DSC00001.JPG").write_bytes(b"")
            exif = {"f_number": 2.8, "exposure_time": 15.0, "iso": 3200}
            decoder = FakeDecoder(make_scene(stars=False))
            with (
                mock.patch.object(config, "source_root", return_value=root),
                mock.patch.object(candidates, "read_capture_exif", return_value=exif),
                mock.patch.object(candidates, "open_raw_half_decoder", side_effect=decoder.factory) as opener,
            ):
                candidates.generate_development_candidates("DSC00001", "neutral_recovery", seed=1, budget=32)
        self.assertEqual(opener.call_args.kwargs, {"exif": exif})

    def test_production_decoder_closes_raw_on_success_and_on_errors(self):
        import rawpy

        base = resolve_adjustments("neutral_recovery")
        path = Path("/nonexistent/DSC00001.ARW")

        raw = self.fake_raw()
        with mock.patch.object(rawpy, "imread", return_value=raw):
            with candidates.open_raw_half_decoder(path, base) as decode:
                decode("blend", "light", 0.0)
        raw.close.assert_called_once()

        # デコード失敗は DevelopmentCandidateError に包まれ、raw は閉じられる。
        raw = self.fake_raw()
        raw.postprocess.side_effect = rawpy.LibRawError("broken")
        with mock.patch.object(rawpy, "imread", return_value=raw):
            with self.assertRaises(candidates.DevelopmentCandidateError):
                with candidates.open_raw_half_decoder(path, base) as decode:
                    decode("blend", "light", 0.0)
        raw.close.assert_called_once()

        # 呼び出し側の例外でも close される。
        raw = self.fake_raw()
        with mock.patch.object(rawpy, "imread", return_value=raw):
            with self.assertRaises(RuntimeError):
                with candidates.open_raw_half_decoder(path, base):
                    raise RuntimeError("boom")
        raw.close.assert_called_once()

        # ARW を開けない場合は DevelopmentCandidateError（close する対象が無い）。
        with mock.patch.object(rawpy, "imread", side_effect=OSError("cannot open")):
            with self.assertRaises(candidates.DevelopmentCandidateError):
                with candidates.open_raw_half_decoder(path, base):
                    pass

    # ------------------------------------------------------------------
    # 安全策・ゲートの境界
    # ------------------------------------------------------------------

    def test_clip_worsen_limit_boundary_is_ten_points(self):
        # しきい値は 0.10、比較は「以上で除外」。期待値は定数ではなくリテラルで固定する。
        base = {"highlight_clip": 0.0, "shadow_clip": 0.0, "noise_flat": 0.01, "shadow_mean": 0.1}
        self.assertEqual(candidates.CLIP_WORSEN_LIMIT, 0.10)
        self.assertIn("白飛び", candidates._guard_failure("social", dict(base, highlight_clip=0.10), base))
        self.assertIsNone(candidates._guard_failure("social", dict(base, highlight_clip=0.099), base))
        self.assertIn("黒つぶれ", candidates._guard_failure("social", dict(base, shadow_clip=0.10), base))
        self.assertIsNone(candidates._guard_failure("social", dict(base, shadow_clip=0.099), base))

    def test_low_noise_guard_boundary(self):
        base = {"highlight_clip": 0.0, "shadow_clip": 0.0, "noise_flat": 0.0100, "shadow_mean": 0.1}
        # 同値は通過、わずかでも悪化なら除外。
        self.assertIsNone(candidates._guard_failure("low_noise", dict(base, noise_flat=0.0100), base))
        self.assertIsNotNone(candidates._guard_failure("low_noise", dict(base, noise_flat=0.010001), base))
        self.assertIsNone(candidates._guard_failure("low_noise", dict(base, noise_flat=0.0099), base))

    def test_decode_cache_is_lru(self):
        calls: list[float] = []

        def decode(category, noise_reduction, exposure):
            calls.append(exposure)
            return np.full((4, 4, 3), 0.5, dtype=np.float32)

        cache = candidates.DecodeCache(decode, limit=2)
        cache.get("blend", "light", 0.0)  # A
        cache.get("blend", "light", 0.5)  # B
        cache.get("blend", "light", 0.0)  # A（最近使用になる）
        cache.get("blend", "light", 1.0)  # C → 最も古い B が追い出される
        self.assertEqual(cache.decode_count, 3)
        cache.get("blend", "light", 0.0)  # A は残っている
        self.assertEqual(cache.decode_count, 3)
        cache.get("blend", "light", 0.5)  # B は再デコード
        self.assertEqual(cache.decode_count, 4)
        self.assertEqual(calls, [0.0, 0.5, 1.0, 0.5])

    def test_star_gate_without_exif_rejects_large_total_bright_area(self):
        def blobs(width: int, height: int) -> np.ndarray:
            image = np.full((320, 480, 3), 0.04, dtype=np.float32)
            for index in range(350):
                y = 10 + 22 * (index // 25)
                x = 10 + 18 * (index % 25)
                image[y : y + height, x : x + width] = 1.0
            return image

        # EXIF 無しは暗背景 + 点密度に加えて明部の合計面積 <= MAX_STAR_BRIGHT_RATIO を求める。
        # 点は小さく暗背景でも、明部の合計面積が上限を超えたら星空ではない。
        many = candidates.detect_star_scene(blobs(3, 3))
        self.assertEqual(many["star_count"], 350)
        self.assertGreater(many["dark_background_ratio"], 0.9)
        self.assertGreater(many["bright_point_ratio"], candidates.MAX_STAR_BRIGHT_RATIO)
        self.assertFalse(many["is_star_scene"])
        # 同じ点数で明部の合計面積だけ上限内なら星空と判定される（対照）。
        few = candidates.detect_star_scene(blobs(2, 1))
        self.assertLessEqual(few["bright_point_ratio"], candidates.MAX_STAR_BRIGHT_RATIO)
        self.assertTrue(few["is_star_scene"])

    def test_exposure_grid_snaps_to_half_ev(self):
        self.assertEqual(candidates.EXPOSURE_GRID, 0.5)
        for value, expected in (
            (0.0, 0.0),
            (0.3, 0.5),
            (0.26, 0.5),
            (0.24, 0.0),
            (0.2, 0.0),
            (-0.3, -0.5),
            (-0.2, 0.0),
            (0.74, 0.5),
            (0.8, 1.0),
            (1.9, 2.0),
            (5.0, 2.0),
            (-1.9, -2.0),
            (-5.0, -2.0),
        ):
            self.assertEqual(candidates.snap_exposure(value), expected, value)

    # ------------------------------------------------------------------
    # E2E のレスポンス形
    # ------------------------------------------------------------------

    def assert_rejected_shape(self, result: dict) -> None:
        for item in result["rejected"]:
            self.assertEqual(set(item), {"id", "reason"})
            self.assertNotEqual(item["id"], "natural")
            self.assertIsInstance(item["reason"], str)
            self.assertTrue(item["reason"])
        self.assertNotIn("natural", [item["id"] for item in result["rejected"]])
        rejected_ids = {item["id"] for item in result["rejected"]}
        self.assertFalse(rejected_ids & {item["id"] for item in result["candidates"]})

    def test_stars_candidate_is_adopted_on_a_star_scene(self):
        result = self.generate(FakeDecoder(make_scene(stars=True)))
        self.assertTrue(result["star_scene"]["is_star_scene"])
        self.assertIn("stars", [item["id"] for item in result["candidates"]])
        self.assertNotIn("stars", [item["id"] for item in result["rejected"]])
        self.assert_rejected_shape(result)

    def test_only_natural_when_every_other_style_is_rejected_or_not_targeted(self):
        # 平坦なグラデーション画像では social 以外は対象外。social のデコード
        # （noise_reduction=off）だけ黒つぶれさせ、全候補を落とす。
        decoder = FakeDecoder(make_scene(stars=False), break_noise_reduction="off")
        result = self.generate(decoder)
        self.assertEqual([item["id"] for item in result["candidates"]], ["natural"])
        self.assertEqual([item["id"] for item in result["rejected"]], ["social"])
        self.assert_rejected_shape(result)
        self.assertEqual(result["candidates"][0]["adjustments"], resolve_adjustments("neutral_recovery"))

    def test_candidate_that_worsens_shadow_clip_is_rejected_end_to_end(self):
        decoder = FakeDecoder(make_scene(stars=False), break_noise_reduction="off")
        result = self.generate(decoder)
        natural = result["candidates"][0]["metrics"]
        self.assertEqual(natural["shadow_clip"], 0.0)
        self.assertNotIn("social", [item["id"] for item in result["candidates"]])
        reasons = {item["id"]: item["reason"] for item in result["rejected"]}
        self.assertIn("黒つぶれ", reasons["social"])
        self.assertIn("除外", reasons["social"])
        for item in result["candidates"]:
            self.assertLess(item["metrics"]["shadow_clip"] - natural["shadow_clip"], 0.10)

    def test_candidate_that_worsens_highlight_clip_is_rejected_end_to_end(self):
        # 白飛びはトーン（brightness 0.7 まで下げられる）で回避されてしまうため、
        # 基準以外の調整値の代理画像を全面白にして「どの点も白飛びが悪化する」状況を作る。
        base = resolve_adjustments("neutral_recovery")
        real_render = candidates.render_proxy

        def render(decoded, adjustments, iso=None):
            image = real_render(decoded, adjustments, iso=iso)
            return image if adjustments == base else np.ones_like(image)

        with mock.patch.object(candidates, "render_proxy", side_effect=render):
            result = self.generate(FakeDecoder(make_scene(stars=False)))
        natural = result["candidates"][0]["metrics"]
        self.assertEqual(natural["highlight_clip"], 0.0)
        self.assertNotIn("social", [item["id"] for item in result["candidates"]])
        reasons = {item["id"]: item["reason"] for item in result["rejected"]}
        self.assertIn("白飛び", reasons["social"])
        for item in result["candidates"]:
            self.assertLess(item["metrics"]["highlight_clip"] - natural["highlight_clip"], 0.10)


class CandidateServerHandlerTests(unittest.TestCase):
    class Request:
        def __init__(self, payload):
            self.payload = payload
            self.status = None
            self.body = None
            self.logs = []

        def _read_json_body(self):
            return self.payload

        def _json(self, status, payload):
            self.status = status
            self.body = payload

        def log_message(self, format, *args):
            self.logs.append(format % args)

    def test_bad_seed_and_unknown_key_are_400(self):
        for payload in (
            {"capture_id": "DSC00001", "preset": "neutral_recovery", "seed": True},
            {"capture_id": "DSC00001", "preset": "neutral_recovery", "budget": True},
            {"capture_id": "DSC00001", "preset": "neutral_recovery", "unexpected": 1},
        ):
            with self.subTest(payload=payload):
                request = self.Request(payload)
                server.handle_development_candidates(request, urlparse("/api/development-candidates"))
                self.assertEqual(request.status, 400)

    def test_missing_source_is_404(self):
        request = self.Request({"capture_id": "DSC00001", "preset": "neutral_recovery"})
        with mock.patch.object(
            candidates,
            "generate_development_candidates",
            side_effect=candidates.DevelopmentCandidateNotFound("元ARWが見つかりません。"),
        ):
            server.handle_development_candidates(request, urlparse("/api/development-candidates"))
        self.assertEqual(request.status, 404)

    def test_unexpected_optimizer_error_is_500_without_detail(self):
        request = self.Request({"capture_id": "DSC00001", "preset": "neutral_recovery"})
        with mock.patch.object(
            candidates,
            "generate_development_candidates",
            side_effect=RuntimeError("/Users/private/photo.ARW"),
        ):
            server.handle_development_candidates(request, urlparse("/api/development-candidates"))
        self.assertEqual(request.status, 500)
        self.assertNotIn("/Users/private", request.body["error"])
        self.assertTrue(request.logs)

    def post(self, payload):
        request = self.Request(payload)
        server.handle_development_candidates(request, urlparse("/api/development-candidates"))
        return request

    def test_invalid_inputs_are_400_and_never_reach_the_generator(self):
        valid = {"capture_id": "DSC00001", "preset": "neutral_recovery"}
        cases = {
            "capture_id missing": {"preset": "neutral_recovery"},
            "capture_id empty": dict(valid, capture_id=""),
            "capture_id number": dict(valid, capture_id=1),
            "capture_id null": dict(valid, capture_id=None),
            "capture_id list": dict(valid, capture_id=["DSC00001"]),
            "preset missing": {"capture_id": "DSC00001"},
            "preset empty": dict(valid, preset=""),
            "preset number": dict(valid, preset=3),
            "preset null": dict(valid, preset=None),
            "seed float": dict(valid, seed=1.5),
            "seed integral float": dict(valid, seed=7.0),
            "seed string": dict(valid, seed="7"),
            "budget float": dict(valid, budget=48.0),
            "budget string": dict(valid, budget="48"),
            "payload list": ["DSC00001"],
            "payload null": None,
            "payload string": "DSC00001",
        }
        for name, payload in cases.items():
            with self.subTest(name), mock.patch.object(candidates, "generate_development_candidates") as generate:
                request = self.post(payload)
                self.assertEqual(request.status, 400)
                self.assertIn("error", request.body)
                generate.assert_not_called()

    def test_out_of_range_budget_is_400(self):
        for budget in (candidates.MIN_BUDGET - 1, candidates.MAX_BUDGET + 1, 0, -5):
            with self.subTest(budget=budget):
                request = self.post({"capture_id": "DSC00001", "preset": "neutral_recovery", "budget": budget})
                self.assertEqual(request.status, 400)
                self.assertIn("budget", request.body["error"])

    def test_valid_request_passes_arguments_through_and_returns_the_result(self):
        result = {"capture_id": "DSC00001", "candidates": [{"id": "natural"}], "rejected": []}
        with mock.patch.object(candidates, "generate_development_candidates", return_value=result) as generate:
            request = self.post({"capture_id": "DSC00001", "preset": "neutral_recovery", "seed": 5, "budget": 64})
        self.assertEqual(request.status, 200)
        self.assertIs(request.body, result)
        generate.assert_called_once_with("DSC00001", "neutral_recovery", seed=5, budget=64)

    def test_omitted_seed_and_budget_are_passed_as_none(self):
        with mock.patch.object(candidates, "generate_development_candidates", return_value={}) as generate:
            request = self.post({"capture_id": "DSC00001", "preset": "neutral_recovery"})
        self.assertEqual(request.status, 200)
        generate.assert_called_once_with("DSC00001", "neutral_recovery", seed=None, budget=None)


NIGHT_EXIF = {"exposure_time": 20.0, "f_number": 2.8, "iso": 6400}  # EV100 ≈ −7.4
HANDHELD_NIGHT_EXIF = {"exposure_time": 1 / 15, "f_number": 5.6, "iso": 3200}  # EV100 ≈ +3.9
BRACKET_EXIF = {"exposure_time": 10.0, "f_number": 8.0, "iso": 100}  # 三脚の長秒。EV100 ≈ +2.7


def make_sky(points: int, *, background: float = 0.04, patch: tuple[int, int] | None = None) -> np.ndarray:
    """背景に1画素の星を格子状（先頭から points 個）に置く。patch=(高さ, 幅) で大面積の白い光源も置く。"""

    image = np.full((320, 480, 3), background, dtype=np.float32)
    # 背景が中間調でもわずかな勾配を持たせる（天の川のように一様ではない空）。
    image += np.linspace(0.0, 0.05, 480, dtype=np.float32)[None, :, None]
    for index in range(points):
        image[8 + 16 * (index // 30), 8 + 16 * (index % 30)] = 0.95
    if patch is not None:
        image[100 : 100 + patch[0], 200 : 200 + patch[1]] = 1.0
    return np.clip(image, 0.0, 1.0)


class StarSceneGateTests(unittest.TestCase):
    """星空判定（#46）: EXIF（長秒・低EV100）を主信号に、画像は点光源の密度で確かめる。"""

    def gate(self, image: np.ndarray, exif: dict | None) -> dict:
        return candidates.detect_star_scene(image, exif)

    def test_long_exposure_with_many_points_is_a_star_scene(self):
        measured = self.gate(make_sky(400), NIGHT_EXIF)
        self.assertTrue(measured["is_star_scene"])
        self.assertEqual(measured["exif_state"], "night")
        self.assertTrue(measured["exif_night"])
        self.assertGreater(measured["star_point_density"], candidates.STAR_MIN_POINT_DENSITY)

    def test_milky_way_with_midtone_background_is_a_star_scene(self):
        # 背景が中間調（暗い背景の割合 ≈ 0）でも、点が多数あれば星空。
        image = make_sky(400, background=0.35)
        measured = self.gate(image, NIGHT_EXIF)
        self.assertLess(measured["dark_background_ratio"], 0.01)
        self.assertTrue(measured["is_star_scene"])
        # EXIF が無ければ、暗背景でない画像は安全側で星空と扱わない。
        self.assertFalse(self.gate(image, None)["is_star_scene"])

    def test_handheld_short_exposure_night_scene_is_not_a_star_scene(self):
        # 短秒なら点が多数あっても（街灯・ライトアップ・ノイズ）星空ではない。
        measured = self.gate(make_sky(400), HANDHELD_NIGHT_EXIF)
        self.assertEqual(measured["exif_state"], "daylight")
        self.assertFalse(measured["is_star_scene"])
        self.assertEqual(measured["star_point_density"], 0.0)

    def test_long_exposure_with_few_points_is_not_a_star_scene(self):
        for points in (0, 5, 60):
            measured = self.gate(make_sky(points), NIGHT_EXIF)
            self.assertFalse(measured["is_star_scene"], points)

    def test_bright_long_exposure_bracket_is_not_a_star_scene(self):
        # 長秒でも EV100 が高い（明るい風景のブラケット）なら、点が多くても星空ではない。
        measured = self.gate(make_sky(400), BRACKET_EXIF)
        self.assertEqual(measured["exif_state"], "daylight")
        self.assertFalse(measured["is_star_scene"])

    def test_large_light_source_blocks_the_gate_even_with_long_exposure(self):
        # 月・街灯・ライトアップのような大面積の光源（輝度0.85以上が2%超）は星空にしない。
        image = make_sky(400, patch=(60, 80))
        self.assertGreater(float(np.mean(image.mean(axis=2) >= candidates.STAR_BIG_LIGHT_LEVEL)), candidates.STAR_MAX_BIG_LIGHT_RATIO)
        self.assertFalse(self.gate(image, NIGHT_EXIF)["is_star_scene"])
        # 面積が上限内の光源（月程度）なら星空のまま。
        self.assertTrue(self.gate(make_sky(400, patch=(20, 30)), NIGHT_EXIF)["is_star_scene"])

    def test_without_exif_a_dark_sky_with_dense_points_is_a_star_scene(self):
        measured = self.gate(make_sky(400), None)
        self.assertEqual(measured["exif_state"], "unknown")
        self.assertFalse(measured["exif_night"])
        self.assertTrue(measured["is_star_scene"])
        # 点が少ない暗い画像（暗いアーチ越しのライトアップ建築など）は EXIF 無しでは判定しない。
        self.assertFalse(self.gate(make_sky(40), None)["is_star_scene"])
        # 露光時間かEV100が欠けている場合も EXIF 無しと同じ厳しい条件になる。
        self.assertTrue(self.gate(make_sky(400), {"exposure_time": 20.0})["is_star_scene"])
        self.assertFalse(self.gate(make_sky(40), {"exposure_time": 20.0})["is_star_scene"])
        for garbage in ({"exposure_time": "abc"}, {"exposure_time": 0}, {"exposure_time": float("nan")}):
            self.assertEqual(self.gate(make_sky(40), garbage)["exif_state"], "unknown")

    def test_exposure_time_boundary(self):
        base = {"f_number": 1.4, "iso": 6400}
        at_limit = self.gate(make_sky(400), {**base, "exposure_time": candidates.STAR_MIN_EXPOSURE_SEC})
        below = self.gate(make_sky(400), {**base, "exposure_time": candidates.STAR_MIN_EXPOSURE_SEC - 0.01})
        self.assertTrue(at_limit["is_star_scene"])
        self.assertFalse(below["is_star_scene"])

    def test_ev100_boundary(self):
        # EV100 = log2(N²/t) − log2(ISO/100)。N=4, t=4s なら ISO1600 でちょうど −2.0。
        at_limit = {"exposure_time": 4.0, "f_number": 4.0, "iso": 1600}
        self.assertEqual(engine.ev100(at_limit), candidates.STAR_MAX_EV100)
        self.assertTrue(self.gate(make_sky(400), at_limit)["is_star_scene"])
        brighter = {**at_limit, "iso": 800}  # EV100 = −1.0
        self.assertFalse(self.gate(make_sky(400), brighter)["is_star_scene"])

    def test_point_density_boundary(self):
        area = 320 * 480
        minimum = int(np.ceil(candidates.STAR_MIN_POINT_DENSITY * area))
        self.assertTrue(self.gate(make_sky(minimum + 5), NIGHT_EXIF)["is_star_scene"])
        self.assertFalse(self.gate(make_sky(minimum - 5), NIGHT_EXIF)["is_star_scene"])

    def test_generate_offers_stars_only_when_exif_and_points_agree(self):
        star_scene = make_sky(400, background=0.35)
        with_exif = self.generate_with(star_scene, NIGHT_EXIF)
        self.assertTrue(with_exif["star_scene"]["is_star_scene"])
        self.assertIn("stars", [item["id"] for item in with_exif["candidates"]] + [item["id"] for item in with_exif["rejected"]])
        handheld = self.generate_with(star_scene, HANDHELD_NIGHT_EXIF)
        self.assertFalse(handheld["star_scene"]["is_star_scene"])
        self.assertNotIn("stars", [item["id"] for item in handheld["candidates"]] + [item["id"] for item in handheld["rejected"]])

    def generate_with(self, scene: np.ndarray, exif: dict) -> dict:
        return CandidateOptimizerTests().generate(FakeDecoder(scene), exif=exif)

