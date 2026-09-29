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


class FakeDecoder:
    """Deterministic stand-in for ``open_raw_half_decoder``."""

    def __init__(self, scene: np.ndarray, *, worsen_full_nr: bool = False) -> None:
        self.scene = scene
        self.worsen_full_nr = worsen_full_nr
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
        return np.clip(image, 0.0, 1.0).astype(np.float32)

    def factory(self, path: Path, base: dict):
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
    ) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "DSC00001.ARW").write_bytes(b"")
            with mock.patch.object(config, "source_root", return_value=root):
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

    def test_render_proxy_matches_engine_postprocess_chain(self):
        rng = np.random.default_rng(3)
        decoded = rng.uniform(0.0, 1.0, (1300, 1000, 3)).astype(np.float32)
        adjustments = resolve_adjustments(
            "landscape_clear", {"shadow_lift": 40, "brightness": 1.1, "contrast": 1.2, "saturation": 1.3, "sharpness": 1.4}
        )
        expected = engine._apply_tone_array(
            engine._resize_array(engine._apply_shadow_lift_array(decoded, 40.0), candidates.MAX_PROXY_EDGE),
            adjustments,
        )
        np.testing.assert_array_equal(candidates.render_proxy(decoded, adjustments), expected)
        self.assertEqual(max(expected.shape[:2]), candidates.MAX_PROXY_EDGE)

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
