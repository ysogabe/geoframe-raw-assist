"""scripts/compare_engines.py の純粋関数（選定・指標・ID 検証・time -l 解析）の検証。

実 RAW・Core Image CLI・/Volumes には依存しない。
"""

import json
import math
import sys
import unittest
from pathlib import Path

import cv2
import numpy as np

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import compare_engines as ce  # noqa: E402


def _feat(cid, **kw):
    base = {
        "capture_id": cid,
        "iso": 100,
        "exposure_time": 1 / 250,
        "f_number": 8.0,
        "lens_model": "LENS A",
        "orientation": 1,
        "captured_at": f"2026:01:01 12:{int(cid[-2:]):02d}:00",
        "bright_frac": 0.0,
        "dark_frac": 0.0,
    }
    base.update(kw)
    base["ev100"] = ce.ev100(base["f_number"], base["exposure_time"], base["iso"])
    base["hour"] = ce.capture_hour(base["captured_at"])
    return base


class CaptureIdTests(unittest.TestCase):
    def test_accepts_dsc_five_digits(self):
        self.assertEqual(ce.validate_capture_id("DSC04430"), "DSC04430")

    def test_rejects_malformed(self):
        for bad in ["DSC0443", "DSC044300", "../DSC04430", "DSC04430.ARW", "dsc04430", "", None]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ce.validate_capture_id(bad)


class Ev100AndHourTests(unittest.TestCase):
    def test_ev100_sunny16(self):
        # f/16, 1/100s, ISO100 → log2(256*100) ≈ 14.64
        self.assertAlmostEqual(ce.ev100(16, 0.01, 100), math.log2(25600), places=6)

    def test_ev100_invalid(self):
        self.assertIsNone(ce.ev100(None, 0.01, 100))
        self.assertIsNone(ce.ev100(2.8, 0, 100))
        self.assertIsNone(ce.ev100("x", 0.01, 100))

    def test_capture_hour(self):
        self.assertEqual(ce.capture_hour("2026:09:18 19:39:33"), 19)
        self.assertIsNone(ce.capture_hour("2026-09-18"))
        self.assertIsNone(ce.capture_hour(None))


class ClassifyTests(unittest.TestCase):
    def test_day_low_iso(self):
        self.assertEqual(ce.classify_scene(_feat("DSC00001")), ["day"])

    def test_star_by_long_exposure(self):
        f = _feat("DSC00002", exposure_time=15.0, f_number=2.8, iso=3200, captured_at="2026:01:01 23:00:00")
        cats = ce.classify_scene(f)
        self.assertIn("star", cats)
        self.assertIn("night", cats)
        self.assertIn("high_iso", cats)
        self.assertNotIn("day", cats)

    def test_dark_boundary_matches_engine(self):
        # EV100 ちょうど 6.0 は暗所に入る
        f = {"ev100": 6.0, "hour": 12, "iso": 100}
        self.assertIn("dark", ce.classify_scene(f))
        f = {"ev100": 6.01, "hour": 12, "iso": 100}
        self.assertNotIn("dark", ce.classify_scene(f))

    def test_night_needs_night_hour(self):
        self.assertIn("night", ce.classify_scene({"ev100": 3.0, "hour": 19}))
        self.assertIn("night", ce.classify_scene({"ev100": 3.0, "hour": 5}))
        cats = ce.classify_scene({"ev100": 3.0, "hour": 10})
        self.assertIn("dark", cats)
        self.assertNotIn("night", cats)

    def test_iso_bands(self):
        self.assertIn("high_iso", ce.classify_scene({"iso": 3200}))
        self.assertIn("mid_iso", ce.classify_scene({"iso": 800}))
        self.assertNotIn("mid_iso", ce.classify_scene({"iso": 640}))

    def test_backlit_and_portrait(self):
        cats = ce.classify_scene({"bright_frac": 0.05, "dark_frac": 0.2, "orientation": 8})
        self.assertIn("backlit", cats)
        self.assertIn("portrait", cats)
        self.assertNotIn("backlit", ce.classify_scene({"bright_frac": 0.05, "dark_frac": 0.01}))

    def test_missing_values_do_not_raise(self):
        self.assertEqual(ce.classify_scene({}), [])


class SelectAutoTests(unittest.TestCase):
    def test_deterministic_and_unique(self):
        feats = [_feat(f"DSC000{i:02d}") for i in range(30)]
        first = ce.select_auto(feats)
        second = ce.select_auto(list(reversed(feats)))
        self.assertEqual(first, second)
        ids = [r["capture_id"] for r in first]
        self.assertEqual(len(ids), len(set(ids)))

    def test_burst_dedupe_same_minute(self):
        same = "2026:01:01 19:30:00"
        feats = [
            _feat(f"DSC000{i:02d}", iso=3200, exposure_time=1 / 30, f_number=1.4, captured_at=same)
            for i in range(5)
        ]
        picked = ce.select_auto(feats, quotas=(("night", 4),))
        self.assertEqual(len(picked), 1)

    def test_session_cap_limits_same_hour(self):
        feats = [
            _feat(f"DSC000{i:02d}", iso=3200, captured_at=f"2026:01:01 11:{i:02d}:00")
            for i in range(10)
        ]
        picked = ce.select_auto(feats, quotas=(("high_iso", 5),))
        self.assertEqual(len(picked), 2)
        picked = ce.select_auto(feats, quotas=(("high_iso", 5),), max_per_session=5)
        self.assertEqual(len(picked), 5)

    def test_quota_order_assigns_first_category(self):
        night = _feat("DSC00010", iso=3200, exposure_time=1 / 30, f_number=1.4,
                      captured_at="2026:01:01 20:00:00")
        picked = ce.select_auto([night], quotas=(("night", 1), ("high_iso", 1)))
        self.assertEqual([(r["capture_id"], r["category"]) for r in picked], [("DSC00010", "night")])
        self.assertIn("夜景", picked[0]["reason"])

    def test_dark_sorted_darkest_first(self):
        a = _feat("DSC00020", exposure_time=1 / 10, f_number=2.8, captured_at="2026:01:01 10:00:00")
        b = _feat("DSC00021", exposure_time=1.0, f_number=2.8, captured_at="2026:01:01 10:05:00")
        picked = ce.select_auto([a, b], quotas=(("dark", 1),))
        self.assertEqual(picked[0]["capture_id"], "DSC00021")

    def test_lens_day_spreads_lenses(self):
        feats = [_feat(f"DSC001{i:02d}", lens_model="A", captured_at=f"2026:01:01 10:{i:02d}:00") for i in range(10)]
        feats += [_feat(f"DSC002{i:02d}", lens_model="B", captured_at=f"2026:01:02 10:{i:02d}:00") for i in range(10)]
        picked = ce.select_auto(feats, quotas=(("lens_day", 4),))
        lenses = sorted(f["lens_model"] for f in feats if f["capture_id"] in {p["capture_id"] for p in picked})
        self.assertEqual(lenses, ["A", "A", "B", "B"])

    def test_reason_has_no_location(self):
        f = _feat("DSC00030")
        reason = ce.selection_reason("lens_day", f)
        self.assertNotIn("GPS", reason)
        self.assertIn("ISO 100", reason)


class FullSubsetTests(unittest.TestCase):
    def test_prefers_distinct_categories(self):
        sel = [
            {"capture_id": "DSC00001", "category": "night"},
            {"capture_id": "DSC00002", "category": "night"},
            {"capture_id": "DSC00003", "category": "high_iso"},
            {"capture_id": "DSC00004", "category": "lens_day"},
        ]
        self.assertEqual(ce.full_subset(sel, 3), ["DSC00001", "DSC00003", "DSC00004"])
        self.assertEqual(ce.full_subset(sel, 4), ["DSC00001", "DSC00003", "DSC00004", "DSC00002"])
        self.assertEqual(ce.full_subset(sel, 0), [])


class TimeParseTests(unittest.TestCase):
    def test_parse_macos_time_l(self):
        stderr = (
            "some log line\n"
            "        1.23 real         0.50 user         0.10 sys\n"
            "           123456789  maximum resident set size\n"
            "                   0  average shared memory size\n"
            "            98765432  peak memory footprint\n"
        )
        stats = ce.parse_time_l(stderr)
        self.assertAlmostEqual(stats["time_real_s"], 1.23)
        self.assertEqual(stats["maxrss_bytes"], 123456789)
        self.assertEqual(stats["peak_footprint_bytes"], 98765432)

    def test_parse_empty(self):
        self.assertEqual(ce.parse_time_l(""), {})


class MetricTests(unittest.TestCase):
    def test_delta_e_identical_is_zero(self):
        img = np.random.default_rng(0).integers(0, 256, (32, 48, 3), dtype=np.uint8)
        self.assertAlmostEqual(ce.delta_e76(img, img), 0.0, places=5)

    def test_delta_e_black_white_is_100(self):
        black = np.zeros((8, 8, 3), np.uint8)
        white = np.full((8, 8, 3), 255, np.uint8)
        self.assertAlmostEqual(ce.delta_e76(black, white), 100.0, delta=0.5)

    def test_delta_e_shape_mismatch(self):
        with self.assertRaises(ValueError):
            ce.delta_e76(np.zeros((2, 2, 3), np.uint8), np.zeros((2, 3, 3), np.uint8))

    def test_flat_noise_increases_with_noise(self):
        rng = np.random.default_rng(1)
        flat = np.full((256, 256), 128.0, np.float32)
        quiet = np.clip(flat + rng.normal(0, 1, flat.shape), 0, 255).astype(np.uint8)
        noisy = np.clip(flat + rng.normal(0, 8, flat.shape), 0, 255).astype(np.uint8)
        self.assertLess(ce.flat_noise(quiet), ce.flat_noise(noisy))
        self.assertTrue(math.isnan(ce.flat_noise(np.zeros((16, 16), np.uint8))))

    def test_image_metrics_clipping_and_reference(self):
        img = np.zeros((10, 10, 3), np.uint8)
        img[:5] = 255  # 上半分を白飛び
        m = ce.image_metrics(img, img.copy())
        self.assertAlmostEqual(m["clip_high_all"], 0.5)
        self.assertAlmostEqual(m["clip_low"], 0.5)
        self.assertAlmostEqual(m["mean_luma"], 0.5, places=5)
        self.assertAlmostEqual(m["camera_delta_e76"], 0.0, places=5)
        self.assertAlmostEqual(m["camera_luma_diff"], 0.0, places=6)
        self.assertNotIn("camera_delta_e76", ce.image_metrics(img))

    def test_fit_size_and_aspect(self):
        self.assertEqual(ce.fit_size(6000, 4000, 2400), (2400, 1600))
        self.assertEqual(ce.fit_size(1000, 500, 2400), (1000, 500))
        self.assertAlmostEqual(ce.aspect_mismatch((2400, 1600), (1600, 2400)), 1.25)
        self.assertEqual(ce.aspect_mismatch((2400, 1600), (1200, 800)), 0.0)

    def test_crop_boxes(self):
        boxes = dict(ce.crop_boxes(2400, 1600, 512))
        self.assertEqual(boxes["center"], (944, 544, 1456, 1056))
        self.assertEqual(boxes["corner TL"], (0, 0, 512, 512))
        small = dict(ce.crop_boxes(300, 200, 512))
        self.assertEqual(small["center"], (50, 0, 250, 200))


class SummarizeTests(unittest.TestCase):
    def test_groups_and_skips_errors_and_nan(self):
        rows = [
            {"engine": "rawpy", "mode": "preview", "wall_s": 10.0, "flat_noise": float("nan")},
            {"engine": "rawpy", "mode": "preview", "wall_s": 20.0, "flat_noise": 2.0},
            {"engine": "rawpy", "mode": "preview", "error": "boom", "wall_s": 999.0},
            {"engine": "coreimage", "mode": "preview", "wall_s": 1.0},
        ]
        summary = {(s["engine"], s["mode"]): s for s in ce.summarize(rows, ["wall_s", "flat_noise"])}
        raw = summary[("rawpy", "preview")]
        self.assertEqual(raw["n"], 2)
        self.assertEqual(raw["wall_s"]["median"], 15.0)
        self.assertEqual(raw["flat_noise"]["n"], 1)
        self.assertEqual(summary[("coreimage", "preview")]["wall_s"]["max"], 1.0)


class DetailBoxTests(unittest.TestCase):
    def test_finds_textured_region(self):
        gray = np.full((400, 600), 128, np.uint8)
        rng = np.random.default_rng(2)
        gray[200:400, 400:600] = rng.integers(0, 256, (200, 200), dtype=np.uint8)
        x0, y0, x1, y1 = ce.detail_box(gray, 200)
        self.assertEqual((x1 - x0, y1 - y0), (200, 200))
        self.assertEqual((x0, y0), (400, 200))


if __name__ == "__main__":
    unittest.main()


# --- 解像感（--sharpness）の純粋関数 --------------------------------------------


def _blurred_step(sigma, size=96, angle_deg=0.0, low=40.0, high=200.0):
    """角度 angle_deg の直線エッジ（暗→明）を σ でぼかした 8bit スケール画像。"""
    import cv2

    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    theta = math.radians(angle_deg)
    # 4 倍で作ってから AREA 縮小（ピクセル積分）するとエイリアスの少ない斜めエッジになる
    big = 4
    yy, xx = np.mgrid[0:size * big, 0:size * big].astype(np.float64) / big
    dist = (xx - size / 2) * math.cos(theta) + (yy - size / 2) * math.sin(theta)
    img = np.where(dist >= 0, high, low).astype(np.float32)
    img = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
    if sigma > 0:
        img = cv2.GaussianBlur(img, (0, 0), sigma)
    return img


class EdgeRiseWidthTests(unittest.TestCase):
    def test_gaussian_edge_profile_matches_theory(self):
        # σ のガウスでぼかしたステップの 10–90% 幅は 2·1.2816·σ ≈ 2.563σ
        from math import erf, sqrt

        step = 0.25
        x = np.arange(-12, 12 + step / 2, step)
        for sigma in (1.0, 2.0, 3.0):
            with self.subTest(sigma=sigma):
                profile = 50 + 150 * np.array([0.5 * (1 + erf(v / (sigma * sqrt(2)))) for v in x])
                self.assertAlmostEqual(ce.edge_rise_width(profile, step=step), 2.563 * sigma, delta=0.05)

    def test_overshoot_does_not_shrink_width(self):
        # シャープのオーバーシュート（外側プラトーより中央寄りの山谷）があっても、
        # プラトーは外側の中央値なので 10/90% の基準は動かない。
        step = 0.25
        x = np.arange(-8, 8 + step / 2, step)
        base = 50 + 150 / (1 + np.exp(-x / 0.6))
        overshoot = base + 25 * np.exp(-((x - 1.5) ** 2)) - 25 * np.exp(-((x + 1.5) ** 2))
        w_base = ce.edge_rise_width(base, step=step)
        w_os = ce.edge_rise_width(overshoot, step=step)
        self.assertIsNotNone(w_os)
        self.assertLess(w_os, w_base)  # 立ち上がり自体は急になる
        self.assertGreater(w_os, 0.3 * w_base)

    def test_rejects_low_contrast_and_reversed(self):
        x = np.linspace(-8, 8, 65)
        self.assertIsNone(ce.edge_rise_width(100 + 5 * np.tanh(x)))
        self.assertIsNone(ce.edge_rise_width(100 - 80 * np.tanh(x)))
        self.assertIsNone(ce.edge_rise_width(np.array([1.0, 2.0, 3.0])))

    def test_sample_profiles_on_synthetic_edges(self):
        # 2D の斜めエッジでも、勾配方向に沿って測れば理論値に近い幅になる
        for angle in (0.0, 30.0, 90.0):
            with self.subTest(angle=angle):
                img = _blurred_step(2.0, angle_deg=angle)
                theta = math.radians(angle)
                pts = np.array([[48.0, 48.0, math.cos(theta), math.sin(theta)]], dtype=np.float32)
                prof = ce.sample_profiles(img, pts)[0]
                width = ce.edge_rise_width(prof)
                self.assertIsNotNone(width)
                # AREA 縮小の画素積分ぶん少し太い
                self.assertAlmostEqual(width, 2.563 * 2.0, delta=0.4)

    def test_find_edge_points_finds_straight_edge_with_correct_normal(self):
        img = _blurred_step(1.0, size=128, angle_deg=0.0)
        pts = ce.find_edge_points(img, (0, 0, 128, 128))
        self.assertGreater(len(pts), 3)
        self.assertTrue(np.all(np.abs(pts[:, 0] - 64) <= 1.5))
        self.assertTrue(np.all(pts[:, 2] > 0.95))  # 法線は +x（暗→明）

    def test_blur_widens_measured_edges(self):
        sharp = _blurred_step(1.0, size=128, angle_deg=20.0)
        soft = _blurred_step(2.5, size=128, angle_deg=20.0)
        pts = ce.find_edge_points(sharp, (0, 0, 128, 128))
        w_sharp = np.median([ce.edge_rise_width(p) for p in ce.sample_profiles(sharp, pts)])
        w_soft = np.median([w for w in (ce.edge_rise_width(p) for p in ce.sample_profiles(soft, pts)) if w])
        self.assertGreater(w_soft, w_sharp * 1.8)


class SharpnessMetricTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(0)
        self.texture = (rng.random((128, 128)) * 255).astype(np.float32)

    def test_tenengrad_and_laplacian_drop_with_blur(self):
        import cv2

        soft = cv2.GaussianBlur(self.texture, (0, 0), 1.5)
        self.assertLess(ce.tenengrad(soft), ce.tenengrad(self.texture) * 0.3)
        self.assertLess(ce.laplacian_var(soft), ce.laplacian_var(self.texture) * 0.1)
        self.assertEqual(ce.tenengrad(np.full((32, 32), 7.0)), 0.0)

    def test_highfreq_ratio(self):
        import cv2

        # 白色雑音の高周波帯（0.25–0.5 cyc/px の円環）比は面積比程度、ぼかすと激減
        white = ce.highfreq_energy_ratio(self.texture)
        self.assertGreater(white, 0.4)
        soft = ce.highfreq_energy_ratio(cv2.GaussianBlur(self.texture, (0, 0), 2.0))
        self.assertLess(soft, white * 0.05)
        self.assertTrue(math.isnan(ce.highfreq_energy_ratio(np.full((16, 16), 3.0))))

    def test_iso_band(self):
        self.assertEqual(ce.iso_band(100), "low")
        self.assertEqual(ce.iso_band(799), "low")
        self.assertEqual(ce.iso_band(800), "mid")
        self.assertEqual(ce.iso_band(1600), "mid")
        self.assertEqual(ce.iso_band(3200), "high")
        self.assertEqual(ce.iso_band(None), "unknown")

    def test_roi_boxes_prefers_detail_and_does_not_overlap(self):
        img = np.full((400, 600), 128.0, dtype=np.float32)
        img[250:350, 400:500] = self.texture[:100, :100]
        boxes = dict(ce.roi_boxes(img, 100, k=3))
        self.assertEqual(set(boxes), {"detail1", "detail2", "detail3", "center", "corner"})
        x0, y0, x1, y1 = boxes["detail1"]
        self.assertTrue(x0 >= 375 and y0 >= 225 and x1 <= 525 and y1 <= 375)
        details = [boxes[f"detail{i}"] for i in (1, 2, 3)]
        for i in range(3):
            for j in range(i + 1, 3):
                a, b = details[i], details[j]
                overlap = not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])
                self.assertFalse(overlap)
        self.assertEqual(boxes["center"], (250, 150, 350, 250))

    def test_roi_shift_recovers_translation(self):
        import cv2

        big = cv2.GaussianBlur((np.random.default_rng(1).random((300, 300)) * 255).astype(np.float32), (0, 0), 1.2)
        moved = np.roll(np.roll(big, 4, axis=1), -6, axis=0) * 0.7 + 20  # トーン差があっても
        dx, dy, response = ce.roi_shift(big, moved, (100, 100, 200, 200), margin=24)
        self.assertAlmostEqual(dx, 4.0, delta=0.3)
        self.assertAlmostEqual(dy, -6.0, delta=0.3)
        self.assertGreater(response, 0.3)
        self.assertEqual(ce.shift_box((100, 100, 200, 200), dx, dy, 300, 300), (104, 94, 204, 194))
        self.assertEqual(ce.shift_box((250, 0, 300, 50), 20, -5, 300, 300), (250, 0, 300, 50))

    def test_fit_to_target(self):
        rgb = np.zeros((1603, 2400, 3), dtype=np.uint8)
        out, how = ce.fit_to_target(rgb, (2400, 1600))
        self.assertEqual((out.shape, how), ((1600, 2400, 3), "crop"))
        out, how = ce.fit_to_target(np.zeros((1598, 2400, 3), dtype=np.uint8), (2400, 1600))
        self.assertEqual((out.shape, how), ((1600, 2400, 3), "crop"))
        out, how = ce.fit_to_target(np.zeros((800, 1200, 3), dtype=np.uint8), (2400, 1600))
        self.assertEqual((out.shape, how), ((1600, 2400, 3), "resampled"))

    def test_summarize_sharpness_medians_and_skips_errors(self):
        rows = [
            {"mode": "preview", "variant": "a", "x": 1.0},
            {"mode": "preview", "variant": "a", "x": 3.0},
            {"mode": "preview", "variant": "a", "x": float("nan")},
            {"mode": "preview", "variant": "a", "error": "boom", "x": 100.0},
            {"mode": "full", "variant": "a"},
        ]
        out = ce.summarize_sharpness(rows, ["x"], ("mode", "variant"))
        by = {(o["mode"], o["variant"]): o for o in out}
        self.assertEqual(by[("preview", "a")]["x"], 2.0)
        self.assertEqual(by[("preview", "a")]["n"], 3)
        self.assertEqual(by[("preview", "a")]["x_n"], 2)
        self.assertIsNone(by[("full", "a")]["x"])

    def test_rawpy_finish_reuses_engine_tone(self):
        # 後段仕上げは engine._apply_tone_array を通る（平坦画像はシャープの影響を受けない）
        flat = np.full((64, 64, 3), 128, dtype=np.uint8)
        out = ce.apply_rawpy_finish(flat, 100)
        self.assertEqual(out.dtype, np.uint8)
        self.assertLessEqual(int(np.abs(out.astype(int) - 128).max()), 6)
        edge = np.zeros((64, 64, 3), dtype=np.uint8)
        edge[:, 32:] = 200
        sharpened = ce.apply_rawpy_finish(edge, 100)
        # USM のオーバーシュートでエッジ直近の明側が 200 を超える
        self.assertGreater(int(sharpened[:, 32, 0].max()), int(sharpened[:, 50, 0].max()))


# --- 夜景・星空・ブラケット・ハイライト（--night / --bracket / --highlight）の純粋関数 ----------------


def _night_sky(seed=0, size=(240, 320), sky=0.06, noise=0.01, stars=40, star_level=0.6):
    """暗い空 + ガウスノイズ + 点状の星（2x2）の合成 RGB（0..1 float）と星の位置を返す。"""
    rng = np.random.default_rng(seed)
    h, w = size
    img = np.clip(rng.normal(sky, noise, (h, w, 3)), 0, 1).astype(np.float32)
    pts = []
    for _ in range(stars):
        y, x = int(rng.integers(8, h - 8)), int(rng.integers(8, w - 8))
        img[y:y + 2, x:x + 2] = star_level
        pts.append((y, x))
    return img, pts


class SrgbLinearTests(unittest.TestCase):
    def test_round_trip_known_values(self):
        self.assertAlmostEqual(float(ce.srgb_to_linear(np.array([0.0]))[0]), 0.0)
        self.assertAlmostEqual(float(ce.srgb_to_linear(np.array([1.0]))[0]), 1.0, places=5)
        # sRGB 0.5 → リニア 0.214
        self.assertAlmostEqual(float(ce.srgb_to_linear(np.array([0.5]))[0]), 0.2140, places=3)
        self.assertAlmostEqual(float(ce.srgb_to_linear(np.array([0.02]))[0]), 0.02 / 12.92, places=6)


class StarMetricTests(unittest.TestCase):
    def test_star_point_mask_and_metrics_count_synthetic_stars(self):
        img, pts = _night_sky(stars=80)
        gray = ce.luma_of(img)
        mask = ce.star_point_mask(gray)
        # 全ての星がマスクに入り、空はほぼ入らない
        self.assertTrue(all(mask[y, x] for y, x in pts))
        self.assertLess(mask.sum(), len(pts) * 12)
        rgb8 = (img * 255).astype(np.uint8)
        m = ce.star_metrics(rgb8, {"iso": 3200, "exposure_time": 10.0, "f_number": 2.8})
        self.assertEqual(m["point_count"], len(pts))
        self.assertTrue(m["is_star_scene"])
        self.assertGreaterEqual(m["star_count"], len(pts) * 0.9)
        # NR 相当（ぼかし）で微光星が消えると point_count が減る
        import cv2

        blurred = cv2.GaussianBlur(img, (0, 0), 2.5)
        m2 = ce.star_metrics((blurred * 255).astype(np.uint8), None)
        self.assertLess(m2["point_count"], m["point_count"])

    def test_star_metrics_without_stars_is_zero(self):
        img, _ = _night_sky(stars=0)
        m = ce.star_metrics((img * 255).astype(np.uint8), None)
        self.assertEqual(m["point_count"], 0)
        self.assertFalse(m["is_star_scene"])


class SkyAndImpulseTests(unittest.TestCase):
    def test_flat_tile_mask_picks_the_flat_region(self):
        rng = np.random.default_rng(3)
        gray = np.full((256, 256), 30, np.uint8)
        gray[128:, :] = rng.integers(0, 256, (128, 256), dtype=np.uint8)  # 下半分は構造あり
        mask = ce.flat_tile_mask(gray, block=32, fraction=0.5)
        self.assertEqual(mask.shape, (8, 8))
        self.assertTrue(mask[:4].all())
        self.assertFalse(mask[4:].any())
        px = ce.tile_mask_to_pixels(mask, gray.shape, 32)
        self.assertEqual(px.shape, gray.shape)
        self.assertTrue(px[:128].all() and not px[128:].any())
        self.assertFalse(ce.flat_tile_mask(np.zeros((16, 16), np.uint8)).any())

    def test_sky_stats_measures_noise_and_excludes_points(self):
        img, pts = _night_sky(noise=0.02)
        rgb8 = (img * 255).astype(np.uint8)
        sky_px = np.ones(img.shape[:2], dtype=bool)
        exclude = ce.dilate_mask(ce.star_point_mask(ce.luma_of(img)), 3)
        with_stars = ce.sky_stats(rgb8, sky_px, None)
        without = ce.sky_stats(rgb8, sky_px, exclude)
        self.assertLess(without["sky_noise"], with_stars["sky_noise"])
        self.assertAlmostEqual(without["sky_mean_luma"], 0.06, delta=0.01)
        quiet, _ = _night_sky(noise=0.003, stars=0)
        self.assertLess(ce.sky_stats((quiet * 255).astype(np.uint8), sky_px)["sky_noise"], without["sky_noise"])
        self.assertTrue(math.isnan(ce.sky_stats(rgb8, np.zeros_like(sky_px))["sky_noise"]))

    def test_impulse_count_counts_isolated_points_and_respects_exclusion(self):
        gray = np.full((200, 200), 20, np.uint8)
        pts = [(10, 10), (50, 120), (150, 30), (180, 180)]
        for y, x in pts:
            gray[y, x] = 200
        gray[100:104, 100:104] = 200  # 4x4 の塊（とその角）は数えない
        gray[60:62, 60:62] = 200      # 2x2 の孤立点は数える
        gray[70, 70] = 200
        gray[71, 70] = 150            # 明るい隣接画素が残る点は孤立していない → 数えない
        out = ce.impulse_count(gray)
        self.assertEqual(out["impulse_count"], 5)
        self.assertAlmostEqual(out["impulse_per_mpx"], 5 / 0.04)
        exclude = np.zeros_like(gray, dtype=bool)
        exclude[8:13, 8:13] = True
        self.assertEqual(ce.impulse_count(gray, exclude)["impulse_count"], 4)
        self.assertEqual(ce.impulse_count(np.full((50, 50), 20, np.uint8))["impulse_count"], 0)

    def test_impulse_count_ignores_points_present_in_reference(self):
        # 基準（原寸縮小）にも同じ場所に輝点があれば被写体の輝点として数えない。基準に無い点だけが残る
        gray = np.full((200, 200), 20, np.uint8)
        for y, x in [(10, 10), (50, 120), (150, 30)]:
            gray[y, x] = 200
        ref = np.full((200, 200), 20, np.uint8)
        ref[10, 10] = 200           # 同じ場所にある
        ref[51, 121] = 60           # 1px ずれていても近傍（3x3）で見る。min_diff × 0.5 = 12 以上なら「ある」
        self.assertEqual(ce.impulse_count(gray)["impulse_count"], 3)
        self.assertEqual(ce.impulse_count(gray, reference_gray8=ref)["impulse_count"], 1)
        faint = np.full((200, 200), 20, np.uint8)
        faint[10, 10] = 28          # 差 8 < 12 は「無い」扱い
        self.assertEqual(ce.impulse_count(gray, reference_gray8=faint)["impulse_count"], 3)
        with self.assertRaises(ValueError):
            ce.impulse_count(gray, reference_gray8=ref[:100])

    def test_corner_center_noise_ratio(self):
        rng = np.random.default_rng(4)
        gray = np.clip(rng.normal(100, 1.0, (400, 600)), 0, 255)
        # 四隅だけノイズを 4 倍に
        s = 60
        for ys, xs in ((slice(0, s), slice(0, s)), (slice(0, s), slice(-s, None)), (slice(-s, None), slice(0, s)), (slice(-s, None), slice(-s, None))):
            gray[ys, xs] = np.clip(rng.normal(100, 4.0, (s, s)), 0, 255)
        out = ce.corner_center_noise(gray.astype(np.uint8), fraction=0.15)
        self.assertGreater(out["corner_noise_ratio"], 2.5)
        self.assertAlmostEqual(out["corner_luma_ratio"], 1.0, delta=0.02)

    def test_global_shift_and_shift_mask(self):
        rng = np.random.default_rng(5)
        base = cv2_blur((rng.random((300, 400)) * 255).astype(np.float32), 1.5)
        moved = np.roll(np.roll(base, 6, axis=1), -4, axis=0)
        dx, dy, response = ce.global_shift(base, moved)
        self.assertAlmostEqual(dx, 6.0, delta=0.6)
        self.assertAlmostEqual(dy, -4.0, delta=0.6)
        self.assertGreater(response, 0.2)
        mask = np.zeros((20, 20), dtype=bool)
        mask[5, 5] = True
        shifted = ce.shift_mask(mask, 3, -2)
        self.assertTrue(shifted[3, 8])
        self.assertEqual(shifted.sum(), 1)
        self.assertEqual(ce.dilate_mask(mask, 1).sum(), 5)


class RecallAndPiecewiseShiftTests(unittest.TestCase):
    def test_point_recall(self):
        ref = np.zeros((50, 50), dtype=bool)
        for y, x in ((10, 10), (20, 30), (40, 40)):
            ref[y, x] = True
        tgt = np.zeros_like(ref)
        tgt[11, 11] = True  # 1px ずれても残っている扱い
        tgt[20, 30] = True
        out = ce.point_recall(ref, tgt, radius=2)
        self.assertEqual(out["n_ref"], 3)
        self.assertAlmostEqual(out["recall"], 2 / 3)
        self.assertTrue(math.isnan(ce.point_recall(np.zeros_like(ref), tgt)["recall"]))

    def test_piecewise_shift_follows_local_displacement(self):
        rng = np.random.default_rng(11)
        base = cv2_blur((rng.random((240, 360)) * 255).astype(np.float32), 1.5)
        # 左半分は +4px、右半分は −4px に動かした画像（局所的に違う平行移動）
        moved = base.copy()
        moved[:, :180] = np.roll(base, 4, axis=1)[:, :180]
        moved[:, 180:] = np.roll(base, -4, axis=1)[:, 180:]
        mask = np.zeros(base.shape, dtype=bool)
        mask[100, 60] = True
        mask[100, 300] = True
        out = ce.piecewise_shift_mask(mask, base, moved, grid=(2, 2))
        self.assertTrue(out[100, 64])
        self.assertTrue(out[100, 296])
        self.assertEqual(out.sum(), 2)


def cv2_blur(img, sigma):
    import cv2

    return cv2.GaussianBlur(img, (0, 0), sigma)


class BracketMetricTests(unittest.TestCase):
    def test_exposure_ratio_recovers_one_stop(self):
        rng = np.random.default_rng(6)
        lin = rng.uniform(0.02, 0.3, (120, 160, 3)).astype(np.float32)
        srgb = lambda v: np.where(v <= 0.0031308, 12.92 * v, 1.055 * v ** (1 / 2.4) - 0.055)  # noqa: E731
        a = (srgb(lin) * 255).astype(np.uint8)
        b = (srgb(np.clip(lin * 2.0, 0, 1)) * 255).astype(np.uint8)
        out = ce.exposure_ratio_stops(a, b)
        self.assertAlmostEqual(out["stops"], 1.0, delta=0.05)
        self.assertGreater(out["pixel_fraction"], 0.9)
        # float 0..1 入力も同じ
        self.assertAlmostEqual(ce.exposure_ratio_stops(a / 255.0, b / 255.0)["stops"], 1.0, delta=0.05)
        with self.assertRaises(ValueError):
            ce.exposure_ratio_stops(a, a[:, :100])
        # クリップした画素ばかりなら NaN
        white = np.full_like(a, 255)
        self.assertTrue(math.isnan(ce.exposure_ratio_stops(white, white)["stops"]))

    def test_corner_shift_consistency_detects_radial_mismatch(self):
        rng = np.random.default_rng(7)
        base = cv2_blur((rng.random((400, 600)) * 255).astype(np.float32), 1.5)
        same = np.roll(base, 3, axis=1)
        out = ce.corner_shift_consistency(base, same)
        self.assertEqual(out["n_valid"], 5)
        self.assertLess(out["max_corner_deviation_px"], 0.6)
        self.assertAlmostEqual(out["shift_center"][0], 3.0, delta=0.5)
        # 中央はそのまま、四隅だけ放射状に 5px ずれた画像
        import cv2

        h, w = base.shape
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        r = np.sqrt((xx - w / 2) ** 2 + (yy - h / 2) ** 2) / math.hypot(w / 2, h / 2)
        k = 1.0 + 0.02 * r ** 2
        radial = cv2.remap(base, ((xx - w / 2) * k + w / 2).astype(np.float32), ((yy - h / 2) * k + h / 2).astype(np.float32), cv2.INTER_LINEAR)
        out = ce.corner_shift_consistency(base, radial)
        self.assertGreater(out["max_corner_deviation_px"], 2.0)
        # カメラ側（same）と CI 側（radial）の各パッチの差: 中央は一致し隅がずれる
        dev = ce.patch_shift_deviation(ce.corner_shift_consistency(base, same), out)
        self.assertGreater(dev, 2.0)
        self.assertTrue(math.isnan(ce.patch_shift_deviation({}, out)))

    def test_bracket_pairs_from_exif(self):
        feats = [
            _feat("DSC00010", exposure_time=10.0, f_number=1.4, iso=6400, captured_at="2026:01:01 22:43:49"),
            _feat("DSC00011", exposure_time=10.0, f_number=1.4, iso=3200, captured_at="2026:01:01 22:46:39"),
            _feat("DSC00012", exposure_time=10.0, f_number=1.6, iso=3200, captured_at="2026:01:01 22:47:09"),
            _feat("DSC00013", exposure_time=10.0, f_number=1.6, iso=3200, captured_at="2026:01:01 22:47:40"),  # 同露出
            _feat("DSC00014", exposure_time=20.0, f_number=2.8, iso=3200, lens_model="LENS B", captured_at="2026:01:01 22:50:00"),
            _feat("DSC00015", exposure_time=10.0, f_number=1.4, iso=3200, captured_at="2026:01:02 05:00:00"),  # 時間が離れる
        ]
        pairs = ce.bracket_pairs(feats)
        self.assertEqual([(p["a"], p["b"]) for p in pairs], [("DSC00010", "DSC00011"), ("DSC00011", "DSC00012")])
        # ISO 6400 → 3200 は b が 1 段暗い（ΔEV = ev100_a − ev100_b = −1）
        self.assertAlmostEqual(pairs[0]["delta_ev"], -1.0, places=6)
        self.assertAlmostEqual(pairs[1]["delta_ev"], -math.log2((1.6 / 1.4) ** 2), places=6)
        self.assertEqual(pairs[0]["gap_s"], 170.0)
        self.assertEqual(ce.bracket_pairs(feats, max_gap_s=60), [pairs[1]])
        self.assertIsNone(ce.captured_at_seconds("2026-01-01"))


class HighlightMetricTests(unittest.TestCase):
    def test_channel_clip_rates(self):
        img = np.zeros((10, 10, 3), np.uint8)
        img[:2, :, 0] = 255           # R だけ 2 行
        img[2:3, :, :] = 255          # 全チャンネル 1 行
        out = ce.channel_clip_rates(img)
        self.assertAlmostEqual(out["clip_r"], 0.3)
        self.assertAlmostEqual(out["clip_g"], 0.1)
        self.assertAlmostEqual(out["clip_any"], 0.3)
        self.assertAlmostEqual(out["clip_all"], 0.1)

    def test_clip_boundary_magenta_rate(self):
        img = np.full((40, 40, 3), 120, np.uint8)
        img[10:30, 10:30] = 255  # 白飛び領域
        # 外側 3px の帯の左側だけマゼンタ寄り（R, B > G）
        img[10:30, 7:10] = (200, 120, 200)
        out = ce.clip_boundary_magenta_rate(img, band=3)
        self.assertGreater(out["band_pixels"], 0)
        self.assertAlmostEqual(out["magenta_rate"], (20 * 3) / out["band_pixels"], places=6)
        none = ce.clip_boundary_magenta_rate(np.full((8, 8, 3), 100, np.uint8))
        self.assertTrue(math.isnan(none["magenta_rate"]))
        self.assertEqual(none["band_pixels"], 0)

    def test_highlight_measure_uses_camera_as_reference(self):
        cam = np.full((32, 32, 3), 128, np.uint8)
        var = cam.copy()
        var[:8] = 255
        out = ce.highlight_measure({"camera": cam, "x": var}, ["camera", "x"])
        self.assertAlmostEqual(out["x"]["clip_any"], 0.25)
        self.assertNotIn("camera_delta_e76", out["camera"])
        self.assertGreater(out["x"]["camera_luma_diff"], 0)


class NightMeasureTests(unittest.TestCase):
    def test_night_measure_on_synthetic_variants(self):
        import cv2

        img, pts = _night_sky(noise=0.015, stars=60)
        cam = (img * 255).astype(np.uint8)
        # NR0 相当: 同じ画像に白点ノイズを 30 個足す（星の無い場所）
        noisy = cam.copy()
        rng = np.random.default_rng(9)
        added = 0
        star_mask = ce.dilate_mask(ce.star_point_mask(ce.luma_of(img)), 6)
        while added < 30:
            y, x = int(rng.integers(4, cam.shape[0] - 4)), int(rng.integers(4, cam.shape[1] - 4))
            if not star_mask[y - 2:y + 3, x - 2:x + 3].any():
                noisy[y, x] = 255
                star_mask[y - 2:y + 3, x - 2:x + 3] = True
                added += 1
        # NR 相当: ぼかして星を弱める
        smooth = (cv2.GaussianBlur(img, (0, 0), 2.0) * 255).astype(np.uint8)
        metrics, ref = ce.night_measure({"camera": cam, "noisy": noisy, "smooth": smooth}, {"iso": 3200, "exposure_time": 10, "f_number": 2.8}, ["camera", "noisy", "smooth"])
        self.assertEqual(ref["camera_points"], 60)
        self.assertAlmostEqual(metrics["camera"]["point_ratio"], 1.0)
        self.assertEqual(metrics["camera"]["impulse_count"], 0)
        self.assertGreaterEqual(metrics["noisy"]["impulse_count"], 25)
        self.assertLess(metrics["smooth"]["point_ratio"], 0.8)
        self.assertLess(metrics["smooth"]["sky_noise_ratio"], 0.6)
        self.assertAlmostEqual(metrics["noisy"]["sky_luma_diff"], 0.0, delta=0.01)
        self.assertTrue(ref["sky_fraction"] > 0.2)
        self.assertNotIn("impulse_vs_full", metrics["noisy"])
        # 原寸縮小の基準（= 白点を足す前の画像）があれば、足した白点だけが impulse_vs_full に数えられる
        metrics2, _ = ce.night_measure({"camera": cam, "noisy": noisy}, None, ["camera", "noisy"], {"noisy": cam})
        self.assertGreaterEqual(metrics2["noisy"]["impulse_vs_full"], 25)
        self.assertLessEqual(metrics2["noisy"]["impulse_vs_full"], 30)

    def test_best_box_and_table_md(self):
        mask = np.zeros((100, 100), dtype=bool)
        mask[60:80, 20:40] = True
        self.assertEqual(ce.best_box_by_mask(mask, 20), (20, 60, 40, 80))
        md = ce.table_md([{"mode": "preview", "variant": "a", "n": 2, "x": 1.23456, "y": None, "z": 3}], ("mode", "variant"), ("x", "y", "z"))
        self.assertIn("| preview | a | 2 | 1.235 | — | 3 |", md)
        self.assertEqual(ce.magnify(np.zeros((3, 4, 3), np.uint8), 2).shape, (6, 8, 3))


class VariantTableConsistencyTests(unittest.TestCase):
    """バリアント表・CLI リクエスト表・シートの組が食い違わないこと（表を増やしたときの取りこぼし防止）。"""

    def test_highlight_variants_have_requests_and_sheet_groups(self):
        names = [v for v, _ in ce.HIGHLIGHT_VARIANTS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(ce.CI_HIGHLIGHT_REQUESTS), set(names) - {"camera", "rawpy"})
        # シートの組は比較用に同じバリアント（rawpy）を複数の組に置いてよい
        grouped = {v for group in ce.HIGHLIGHT_SHEET_GROUPS for v in group}
        self.assertEqual(grouped, set(names))
        for decoder, profile, _lens, overrides in ce.CI_HIGHLIGHT_REQUESTS.values():
            self.assertIn(decoder, ("8", "9"))
            self.assertIn(profile, ("neutral", "apple_default"))
            self.assertTrue(set(overrides) <= {"exposure_ev", "extended_dynamic_range", "boost_amount", "boost_shadow_amount",
                                               "luminance_noise_reduction", "color_noise_reduction", "sharpness_amount", "detail_amount"})

    def test_night_variants_have_requests_and_sheet_groups(self):
        names = [v for v, _ in ce.NIGHT_VARIANTS]
        self.assertEqual(set(ce.CI_NIGHT_REQUESTS), set(names) - {"camera", "rawpy"})
        grouped = [v for group in ce.NIGHT_SHEET_GROUPS for v in group]
        self.assertEqual(sorted(grouped), sorted(names))
        self.assertTrue(set(ce.NIGHT_SCALE_VARIANTS) <= set(ce.CI_SCALE_REQUESTS))


# --- Adjustments 評価モード（--adjustments、設計書 coreimage-preview-adjustments.md §3.7 / §4.1 / §4.2）--------


def _srgb8_from_linear(lin):
    """リニア 0..1 → sRGB 8bit（IEC 61966-2-1）。"""
    lin = np.clip(np.asarray(lin, dtype=np.float64), 0, 1)
    srgb = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)
    return np.rint(srgb * 255).astype(np.uint8)


class AdjustmentsContractTests(unittest.TestCase):
    def test_adjustments_for_overrides_one_key_on_identity(self):
        self.assertEqual(ce.adjustments_for("identity"), ce.ADJ_IDENTITY)
        adj = ce.adjustments_for("wb_p60")
        self.assertEqual(adj["wb_temp"], 60.0)
        self.assertEqual({k: v for k, v in adj.items() if k != "wb_temp"},
                         {k: v for k, v in ce.ADJ_IDENTITY.items() if k != "wb_temp"})
        self.assertEqual(ce.adjustments_for("nr_off_sh0")["noise_reduction"], "off")
        with self.assertRaises(KeyError):
            ce.adjustments_for("nope")

    def test_identity_keeps_decode_stage_at_defaults(self):
        # §3.3: exposure 0 / wb 0 / sharpness 1.0（= apple_default 既定）/ NR light（= 既定）でデコード段は恒等
        self.assertEqual(ce.ADJ_IDENTITY["exposure"], 0.0)
        self.assertEqual(ce.ADJ_IDENTITY["wb_temp"], 0.0)
        self.assertEqual(ce.ADJ_IDENTITY["sharpness"], 1.0)
        self.assertEqual(ce.ADJ_IDENTITY["noise_reduction"], "light")

    def test_request_without_adjustments_is_unchanged(self):
        req = ce.coreimage_request(Path("/x/DSC00001.ARW"), Path("/o/a.jpg"), "preview", True,
                                   profile="apple_default", max_edge=2400)
        self.assertEqual(req, {"input": "/x/DSC00001.ARW", "output": "/o/a.jpg", "mode": "preview", "decoder_version": "9",
                               "settings": {"profile": "apple_default", "lens_correction": True}, "max_edge": 2400})
        full = ce.coreimage_request(Path("/x/a.ARW"), Path("/o/a.tif"), "full", False, output_format="tiff16",
                                    output_color_space="srgb", max_edge=2400)
        self.assertNotIn("max_edge", full)
        self.assertEqual((full["format"], full["output_color_space"]), ("tiff16", "srgb"))

    def test_every_planned_request_satisfies_frozen_contract(self):
        for variant, *_ in ce.ADJ_SWEEP_VARIANTS:
            req = ce.coreimage_request(Path("/x/a.ARW"), Path("/o/a.jpg"), "preview", True, profile="apple_default",
                                       adjustments=ce.adjustments_for(variant), max_edge=2400)
            self.assertEqual(ce.validate_adjustments_request(req), [], variant)
            self.assertEqual(len(req["adjustments"]), 9)
        preset = {"shadow_lift": 15.0, "brightness": 1.0, "contrast": 1.03, "saturation": 1.02, "sharpness": 1.04,
                  "highlight_recovery": 60.0, "exposure": 0.5, "wb_temp": -3.0, "noise_reduction": "full"}
        for variant, post, _kind in ce.ADJ_ORACLE_VARIANTS:
            adj = ce.oracle_adjustments(post, preset)
            req = ce.coreimage_request(Path("/x/a.ARW"), Path("/o/a.tif"), "preview", True, profile="apple_default",
                                       adjustments=adj, output_format="tiff16", output_color_space="srgb", max_edge=2400)
            self.assertEqual(ce.validate_adjustments_request(req), [], variant)

    def test_validate_adjustments_request_reports_violations(self):
        good = {"settings": {"profile": "apple_default"}, "adjustments": dict(ce.ADJ_IDENTITY)}
        self.assertEqual(ce.validate_adjustments_request(good), [])
        self.assertEqual(ce.validate_adjustments_request({"settings": {"profile": "neutral"}}), [])  # adjustments 無しは対象外

        def problems(settings=None, **adj):
            a = {**ce.ADJ_IDENTITY, **adj}
            a = {k: v for k, v in a.items() if v is not None}
            return ce.validate_adjustments_request({"settings": settings or {"profile": "apple_default"}, "adjustments": a})

        self.assertTrue(problems({"profile": "neutral"}))
        self.assertTrue(problems({"profile": "apple_default", "exposure_ev": 0.5}))
        self.assertTrue(problems({"profile": "apple_default", "neutral_temperature": 5000}))
        self.assertEqual(problems({"profile": "apple_default", "luminance_noise_reduction": 0.0}), [])  # 個別上書きは可
        self.assertTrue(problems(exposure=2.5))
        self.assertTrue(problems(saturation=True))  # bool は数値として受けない
        self.assertTrue(problems(noise_reduction="strong"))
        self.assertTrue(problems(sharpness=None))  # 9 キー必須
        self.assertTrue(problems(extra=1.0))  # 未知キー

    def test_oracle_adjustments_keeps_decode_identity(self):
        preset = {"exposure": 1.0, "wb_temp": 30.0, "noise_reduction": "full", "shadow_lift": 15.0, "brightness": 1.0,
                  "contrast": 1.03, "saturation": 1.02, "sharpness": 1.04, "highlight_recovery": 60.0}
        adj = ce.oracle_adjustments("preset", preset)
        self.assertEqual((adj["exposure"], adj["wb_temp"], adj["noise_reduction"]), (0.0, 0.0, "light"))
        self.assertEqual((adj["shadow_lift"], adj["contrast"], adj["sharpness"]), (15.0, 1.03, 1.04))
        # CLI 0.6.0 から hr はデコード段（exposure）にも効くので、オラクルでは常に 0（#81）。
        self.assertEqual(adj["highlight_recovery"], 0.0)
        self.assertEqual(ce.oracle_adjustments({"brightness": 1.4})["brightness"], 1.4)
        with self.assertRaises(ValueError):
            ce.oracle_adjustments("preset", None)

    def test_cli_version_gate(self):
        self.assertEqual(ce.parse_cli_version("0.3.0"), (0, 3, 0))
        self.assertEqual(ce.parse_cli_version("0.10"), (0, 10, 0))
        self.assertIsNone(ce.parse_cli_version(None))
        caps = {"adjustments": {"supported": True, "ignored": ["highlight_recovery"], "post_kernel": "metal"}}
        self.assertIsNone(ce.adjustments_cli_problem({"cli_version": "0.3.0"}, caps))
        self.assertIsNone(ce.adjustments_cli_problem({"cli_version": "0.4.1"}, caps))
        old = ce.adjustments_cli_problem({"cli_version": "0.2.0"}, {})
        self.assertIn("0.2.0", old)
        self.assertIn("0.3.0", old)
        self.assertIn("adjustments.supported", ce.adjustments_cli_problem({"cli_version": "0.3.0"}, {"ok": True}))
        self.assertIn("取得できません", ce.adjustments_cli_problem({"error": "CLI がありません"}, {}))

    def test_parse_worker_adjustments(self):
        self.assertIsNone(ce.parse_worker_adjustments(None))
        self.assertEqual(ce.parse_worker_adjustments('{"exposure": 1}'), {"exposure": 1})
        with self.assertRaises(SystemExit):
            ce.parse_worker_adjustments("[1]")
        with self.assertRaises(SystemExit):
            ce.parse_worker_adjustments("{bad")

    def test_adjustments_applied_summary_flattens_response(self):
        payload = {"adjustments_applied": {
            "defaults": {"sharpness_amount": 1.0, "luminance_noise_reduction_amount": 0.241},
            "post": {"shadow_lift": 0.15, "usm": {"sigma": 0.6, "gain": 0.12, "coring": 0.0, "long_edge": 2400, "iso": 100},
                     "wb": {"k0": 5749.2, "mired_shift": 0.0, "mired_per_unit": 0.85}, "noise_reduction": "light"},
            "ignored": ["highlight_recovery"], "guards": ["white_dot_sharpness_floor"]}}
        flat = ce.adjustments_applied_summary(payload)
        self.assertTrue(flat["ci_adjustments_applied"])
        self.assertEqual(flat["ci_adj_guards"], "white_dot_sharpness_floor")
        self.assertEqual(flat["ci_adj_ignored"], "highlight_recovery")
        self.assertEqual((flat["ci_usm_gain"], flat["ci_wb_k0"], flat["ci_def_lum_nr"]), (0.12, 5749.2, 0.241))
        self.assertEqual(ce.adjustments_applied_summary({"ok": True}), {"ci_adjustments_applied": False})

    def test_applied_diff(self):
        a = {"sharpness_amount": 1.0, "neutral_temperature": 5749.221, "scale_factor": 0.4, "profile": "apple_default"}
        self.assertEqual(ce.applied_diff(a, dict(a, scale_factor=0.5)), [])  # scale_factor は見ない
        self.assertEqual(len(ce.applied_diff(a, dict(a, neutral_temperature=5749.2211))), 1)
        self.assertEqual(ce.applied_diff(a, dict(a, neutral_temperature=5749.2211), rel_tol=1e-4), [])
        self.assertEqual(len(ce.applied_diff(a, {**a, "new_key": 1})), 1)  # キー集合の差も数える
        self.assertEqual(len(ce.applied_diff(a, dict(a, profile="neutral"), rel_tol=1e-4)), 1)

    def test_adjustments_plan_builds_valid_requests_without_cli(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "src"
            root.mkdir()
            (root / "DSC00001.arw").write_bytes(b"")
            (root / "DSC00001.JPG").write_bytes(b"")
            ctx = ce.RunContext(source_root=root, out=Path(tmp) / "out", cli=Path(tmp) / "missing", max_edge=2400)
            plan = ce.adjustments_plan(ctx, ["DSC00001"])
            self.assertFalse((Path(tmp) / "out").exists())  # 計画だけ（何も書かない）
        self.assertTrue(all(not p["problems"] for p in plan))
        ci = [p for p in plan if p["engine"] == "coreimage"]
        rawpy = [p for p in plan if p["engine"] == "rawpy"]
        self.assertEqual(len(rawpy), len(ce.ADJ_SWEEP_VARIANTS))
        self.assertEqual(len(ci), 1 + len(ce.ADJ_SWEEP_VARIANTS) + 1 + len(ce.ADJ_WHITE_DOT_VARIANTS) + 1 + len(ce.ADJ_ORACLE_VARIANTS))
        default = next(p for p in ci if p["variant"] == "adj_default")
        self.assertNotIn("adjustments", default["request"])
        self.assertTrue(default["request"]["input"].endswith("DSC00001.arw"))
        sh0 = next(p for p in ci if p["variant"] == "adj_nr_off_sh0" and p["mode"] == "preview")
        self.assertEqual(sh0["request"]["settings"]["sharpness_amount"], 0.0)
        self.assertEqual(sh0["request"]["adjustments"]["noise_reduction"], "off")
        oracle = [p for p in ci if p["variant"].startswith("or_")]
        self.assertTrue(all(p["request"].get("format") == "tiff16" and p["request"].get("output_color_space") == "srgb" for p in oracle))
        # 出力ファイル名が衝突しない
        outputs = [p["request"]["output"] for p in ci]
        self.assertEqual(len(outputs), len(set(outputs)))


class AdjustmentMetricTests(unittest.TestCase):
    def test_one_stop_is_one_in_log2_mean_linear(self):
        rng = np.random.default_rng(1)
        lin = rng.uniform(0.02, 0.4, (64, 64, 1)).repeat(3, axis=2)
        a, b = _srgb8_from_linear(lin), _srgb8_from_linear(lin * 2)
        diff = math.log2(ce.mean_linear_luma(b)) - math.log2(ce.mean_linear_luma(a))
        self.assertAlmostEqual(diff, 1.0, delta=0.02)
        mask = np.zeros((64, 64), dtype=bool)
        mask[:8] = True
        self.assertGreater(ce.mean_linear_luma(a, mask), 0)

    def test_neutral_tiles_and_rb_ratio(self):
        img = np.zeros((64, 128, 3), dtype=np.uint8)
        img[:, :64] = (120, 120, 120)  # 中立
        img[:, 64:] = (200, 60, 40)    # 高彩度
        mask = ce.neutral_tile_mask(img, block=32, fraction=0.5)
        self.assertTrue(mask[:, :64].all())
        self.assertFalse(mask[:, 64:].any())
        self.assertAlmostEqual(ce.rb_log_ratio(img, mask), 0.0, places=6)
        warm = img.copy()
        warm[:, :64] = (140, 120, 100)
        self.assertGreater(ce.rb_log_ratio(warm, mask), 0.1)
        self.assertTrue(math.isnan(ce.rb_log_ratio(img, np.zeros((64, 128), dtype=bool))))
        # 測る画像で飽和・黒つぶれした画素は除く（WB を動かしても R/B が動かない）
        clipped = warm.copy()
        clipped[:32, :64] = (255, 255, 255)
        self.assertAlmostEqual(ce.rb_log_ratio(clipped, mask), ce.rb_log_ratio(warm, mask), places=6)
        self.assertTrue(math.isnan(ce.rb_log_ratio(np.full((64, 128, 3), 255, dtype=np.uint8), mask)))
        # 黒つぶれ・白飛びタイルは近中立に選ばない
        dark = np.zeros((64, 64, 3), dtype=np.uint8)
        self.assertFalse(ce.neutral_tile_mask(dark, block=32).any())

    def test_dark_mask_and_tone_stats(self):
        ramp = np.tile(np.linspace(0, 255, 100).astype(np.uint8)[None, :, None], (10, 1, 3))
        dark = ce.dark_pixel_mask(ramp, 0.25)
        self.assertAlmostEqual(float(dark.mean()), 0.25, delta=0.02)
        self.assertTrue(dark[:, :20].all() and not dark[:, 30:].any())
        gray = ce.tone_stats(ramp)
        self.assertAlmostEqual(gray["ab_std"], 0.0, delta=0.5)
        color = ramp.copy()
        color[:, :50, 0] = 255
        self.assertGreater(ce.tone_stats(color)["ab_std"], 5.0)
        m = ce.adjustment_image_metrics(ramp, np.ones((10, 100), dtype=bool), dark)
        self.assertLess(m["dark_mean_luma"], 0.15)
        self.assertTrue({"log2_mean_linear", "rb_ln_neutral", "mean_luma", "std_luma", "ab_std", "flat_noise"} <= set(m))

    def test_oracle_compare_thresholds(self):
        rng = np.random.default_rng(2)
        img = rng.uniform(0.05, 0.95, (32, 32, 3)).astype(np.float32)
        same = ce.oracle_compare(img, img.copy())
        self.assertTrue(same["pass"])
        self.assertEqual(same["de76_median"], 0.0)
        half_lsb = ce.oracle_compare(img, img + 0.5 / 255)
        self.assertTrue(half_lsb["pass_chan_p99"])
        off = ce.oracle_compare(img, np.clip(img + 3 / 255, 0, 1))
        self.assertFalse(off["pass_chan_p99"])
        self.assertFalse(off["pass"])
        with self.assertRaises(ValueError):
            ce.delta_e76_map(img, img[:16])

    def test_post_chain_oracle_matches_rawpy_formula(self):
        x = np.tile(np.linspace(0, 1, 256, dtype=np.float32)[None, :, None], (4, 1, 3))
        x[..., 0] *= 0.8  # 色を少し付ける（彩度の式を通す）
        adj = {**ce.ADJ_IDENTITY, "shadow_lift": 45.0, "brightness": 1.1, "contrast": 1.2, "saturation": 1.3}
        out = ce.post_chain_oracle(x, adj)
        # 設計書 §3.2 の geoframe_tone と同じ式（lift = v/100、2.0 = SHADOW_LIFT_GAIN/255）
        y = np.clip(x + 0.45 * 2.0 * x * (1 - x) ** 2, 0, 1) * 1.1
        y = (y - 0.5) * 1.2 + 0.5
        luma = (y * np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)).sum(axis=2, keepdims=True)
        y = np.clip(luma + (y - luma) * 1.3, 0, 1)
        self.assertLess(float(np.abs(out - y).max()), 1e-5)
        # identity は恒等（USM 無し）
        self.assertLess(float(np.abs(ce.post_chain_oracle(x, ce.ADJ_IDENTITY) - x).max()), 1e-6)
        # USM を渡すとエッジで値が変わる（CLI の post.usm を再現する経路）
        edge = np.zeros((32, 32, 3), dtype=np.float32)
        edge[:, 16:] = 0.6
        sharp = ce.post_chain_oracle(edge, ce.ADJ_IDENTITY, {"sigma": 0.8, "gain": 0.5, "coring": 0.0})
        self.assertGreater(float(sharp[:, 16, 0].max()), 0.6)


def _sweep_metrics(ci_scale=1.0, wb_sign=1.0):
    """sweep_deltas / adjustment_checks 用の合成指標（rawpy と CI）。"""
    def engine(scale, sign):
        out = {"identity": {"exposure_stops": 0.0, "log2_mean_linear": -3.0, "rb_ln_neutral": 0.0, "dark_mean_luma": 0.1,
                            "mean_luma": 0.4, "std_luma": 0.2, "ab_std": 10.0, "edge_width_ratio": 0.95, "flat_noise": 2.0,
                            "sky_noise": 3.0, "star_recall": 1.0, "star_recall_n": 30, "sky_hf_ratio": 0.5,
                            "impulse_vs_full": 0, "ci_adj_guards": "", "ci_adj_ignored": ""}}
        base = out["identity"]
        out["exp_m1"] = {**base, "exposure_stops": -1.0 * scale, "log2_mean_linear": -4.0}
        out["exp_p1"] = {**base, "exposure_stops": 1.0 * scale, "log2_mean_linear": -2.0}
        out["wb_m60"] = {**base, "rb_ln_neutral": -0.2 * sign * scale}
        out["wb_p60"] = {**base, "rb_ln_neutral": 0.2 * sign * scale}
        out["lift_45"] = {**base, "dark_mean_luma": 0.15}
        out["bright_07"] = {**base, "mean_luma": 0.3}
        out["bright_14"] = {**base, "mean_luma": 0.55}
        out["contrast_08"] = {**base, "std_luma": 0.16}
        out["contrast_14"] = {**base, "std_luma": 0.28}
        out["sat_0"] = {**base, "ab_std": 0.0}
        out["sat_15"] = {**base, "ab_std": 15.0}
        out["sharp_08"] = {**base, "edge_width_ratio": 1.05}
        out["sharp_16"] = {**base, "edge_width_ratio": 0.85, "flat_noise": 2.6}
        out["nr_off"] = {**base, "sky_noise": 4.0, "impulse_vs_full": 2}
        out["nr_full"] = {**base, "sky_noise": 2.0, "star_recall": 0.995, "sky_hf_ratio": 0.4}
        out["nr_off_sh0"] = {**base, "impulse_vs_full": 3, "ci_adj_guards": "white_dot_sharpness_floor"}
        return out
    return {"rawpy": engine(1.0, 1.0), "ci": engine(ci_scale, wb_sign)}


class AdjustmentCheckTests(unittest.TestCase):
    def test_direction_match(self):
        self.assertTrue(ce.direction_match(0.2, 0.1))
        self.assertFalse(ce.direction_match(0.2, -0.1))
        self.assertFalse(ce.direction_match(0.2, 0.0))  # rawpy は効いて CI は効かない = 不一致
        self.assertIsNone(ce.direction_match(0.0, 0.3))  # 基準が動かなければ判定しない
        self.assertIsNone(ce.direction_match(float("nan"), 0.3))

    def test_sweep_deltas_ratio_and_direction(self):
        rows = ce.sweep_deltas(_sweep_metrics(ci_scale=1.2))
        by = {(r["variant"], r["metric"]): r for r in rows}
        wb = by[("wb_p60", "rb_ln_neutral")]
        self.assertAlmostEqual(wb["delta_rawpy"], 0.2)
        self.assertAlmostEqual(wb["ratio"], 1.2)
        self.assertTrue(wb["direction_match"])
        self.assertAlmostEqual(by[("exp_m1", "exposure_stops")]["delta_ci"], -1.2)
        # 掃引した全パラメータが行を持つ
        self.assertEqual({r["param"] for r in rows}, set(ce.ADJ_PARAM_METRICS))
        reversed_wb = {(r["variant"], r["metric"]): r for r in ce.sweep_deltas(_sweep_metrics(wb_sign=-1.0))}
        self.assertFalse(reversed_wb[("wb_p60", "rb_ln_neutral")]["direction_match"])

    def _checks(self, metrics, camera=None):
        deltas = ce.sweep_deltas(metrics)
        checks = ce.adjustment_checks(metrics["ci"], metrics["rawpy"], camera or {"flat_noise": 2.0, "sky_hf_ratio": 0.5}, deltas)
        return {c["check"]: c for c in checks}

    def test_checks_pass_on_good_synthetic_metrics(self):
        checks = self._checks(_sweep_metrics())
        failed = [k for k, c in checks.items() if c["pass"] is False]
        self.assertEqual(failed, [])
        self.assertTrue(checks["white_dot_nr_off_sh0_guard"]["pass"])
        self.assertTrue(checks["white_dot_identity_guard"]["pass"])
        self.assertTrue(checks["adjustments_ignored_empty"]["pass"])
        self.assertTrue(checks["sharpness_ci_edge_monotonic"]["pass"])
        self.assertTrue(checks["nr_ci_monotonic"]["pass"])
        self.assertTrue(checks["exposure_ci_m1_vs_rawpy"]["pass"])
        self.assertTrue(checks["sharpness_ci_16_flat_noise_vs_identity"]["pass"])

    def test_nr_checks_accept_relative_criteria(self):
        # 昼カット: light の時点で star_recall が低くても full が減らさなければ可。空の高周波も light 比で可
        metrics = _sweep_metrics()
        metrics["ci"]["identity"]["star_recall"] = 0.92
        metrics["ci"]["nr_full"]["star_recall"] = 0.915
        metrics["ci"]["identity"]["sky_hf_ratio"] = 0.2
        metrics["ci"]["nr_full"]["sky_hf_ratio"] = 0.19
        checks = self._checks(metrics, camera={"flat_noise": 2.0, "sky_hf_ratio": 0.5})
        self.assertTrue(checks["nr_ci_full_star_recall"]["pass"])
        self.assertTrue(checks["nr_ci_full_sky_hf_vs_camera"]["pass"])
        # 基準点が少なければ 1.5 点分までの低下は揺らぎとして許す（n=30: 0.05）
        metrics["ci"]["nr_full"]["star_recall"] = 0.88
        self.assertTrue(self._checks(metrics)["nr_ci_full_star_recall"]["pass"])
        metrics["ci"]["nr_full"]["star_recall"] = 0.90
        metrics["ci"]["nr_full"]["star_recall_n"] = 1000
        metrics["ci"]["nr_full"]["sky_hf_ratio"] = 0.15
        checks = self._checks(metrics, camera={"flat_noise": 2.0, "sky_hf_ratio": 0.5})
        self.assertIs(checks["nr_ci_full_star_recall"]["pass"], False)
        self.assertIs(checks["nr_ci_full_sky_hf_vs_camera"]["pass"], False)

    def test_checks_fail_on_wrong_direction_magnitude_and_impulses(self):
        bad = _sweep_metrics(ci_scale=1.6, wb_sign=-1.0)
        bad["ci"]["nr_off_sh0"]["impulse_vs_full"] = 40
        bad["ci"]["nr_off_sh0"]["ci_adj_guards"] = ""
        bad["ci"]["nr_full"]["star_recall"] = 0.9
        checks = self._checks(bad)
        for name in ("exposure_ci_m1_stops", "exposure_ci_p1_stops", "exposure_ci_m1_vs_rawpy", "exposure_ci_p1_vs_rawpy",
                     "wb_p60_ci_direction", "wb_p60_direction_vs_rawpy",
                     "wb_p60_ratio_vs_rawpy", "white_dot_nr_off_sh0_impulse_vs_full", "white_dot_nr_off_sh0_guard",
                     "nr_ci_full_star_recall"):
            self.assertIs(checks[name]["pass"], False, name)

    def test_checks_unmeasurable_are_none(self):
        metrics = _sweep_metrics()
        for v in ("identity", "sharp_08", "sharp_16"):
            metrics["ci"][v]["edge_width_ratio"] = float("nan")
        metrics["ci"]["nr_full"]["star_recall_n"] = 0
        checks = self._checks(metrics, camera={"flat_noise": float("nan"), "sky_hf_ratio": float("nan")})
        self.assertIsNone(checks["sharpness_ci_edge_monotonic"]["pass"])
        self.assertIsNone(checks["sharpness_ci_10_edge_ratio"]["pass"])
        metrics["ci"]["identity"]["flat_noise"] = float("nan")
        checks = self._checks(metrics, camera={"flat_noise": float("nan"), "sky_hf_ratio": float("nan")})
        self.assertIsNone(checks["sharpness_ci_16_flat_noise_vs_identity"]["pass"])
        self.assertIsNone(checks["nr_ci_full_star_recall"]["pass"])
        self.assertIsNone(checks["nr_ci_full_sky_hf_vs_camera"]["pass"])

    def test_check_summary_counts(self):
        rows = [{"check": "a", "pass": True, "capture_id": "DSC00001"}, {"check": "a", "pass": False, "capture_id": "DSC00002"},
                {"check": "b", "pass": None, "capture_id": "DSC00001"}]
        summary = {s["check"]: s for s in ce.check_summary(rows)}
        self.assertEqual((summary["a"]["pass"], summary["a"]["fail"], summary["a"]["failed"]), (1, 1, ["DSC00002"]))
        self.assertEqual(summary["b"]["n_a"], 1)


class AdjustmentVariantTableTests(unittest.TestCase):
    def test_sweep_table_is_consistent(self):
        names = [v for v, *_ in ce.ADJ_SWEEP_VARIANTS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names[0], "identity")
        params = {p for _, p, _, _ in ce.ADJ_SWEEP_VARIANTS if p is not None}
        self.assertEqual(params, set(ce.ADJ_PARAM_METRICS))
        self.assertNotIn("highlight_recovery", params)  # 受理するが無効（§3.1）。掃引しない
        self.assertTrue(set(ce.ADJ_WHITE_DOT_VARIANTS) <= set(names) | {"nr_off_sh0"})
        oracle = [v for v, _, _ in ce.ADJ_ORACLE_VARIANTS]
        self.assertEqual(len(oracle), len(set(oracle)))
        self.assertTrue(all(k in ("tone", "tone+usm") for _, _, k in ce.ADJ_ORACLE_VARIANTS))
        # "tone" のオラクルは USM を使わない（sharpness 1.0 のまま）
        for _v, post, kind in ce.ADJ_ORACLE_VARIANTS:
            if kind == "tone":
                self.assertEqual(ce.oracle_adjustments(post)["sharpness"], 1.0)


# =============================================================================
# 診断モード --diagnosis（#92、設計書 coreimage-highlight-diagnosis.md §5〜§10）
# =============================================================================


def _bayer(h, w):
    """RGGB の raw_colors（0=R 1=G 2=B 3=G、color_desc 'RGBG'）。"""
    colors = np.zeros((h, w), np.uint8)
    colors[0::2, 1::2] = 1
    colors[1::2, 0::2] = 3
    colors[1::2, 1::2] = 2
    return colors


class CfaSaturationTests(unittest.TestCase):
    def test_quantization_step(self):
        values = np.array([100, 9000, 9032, 9064, 16348, 16380], np.uint16)
        self.assertEqual(ce.cfa_quantization_step(values, 16380), 32)
        self.assertEqual(ce.cfa_quantization_step(np.array([10000, 10001, 10003], np.uint16), 16383), 1)
        self.assertEqual(ce.cfa_quantization_step(np.array([1, 2], np.uint16), 16380), 1)  # 上半分に 2 値未満

    def test_rates_cells_and_thresholds(self):
        h, w = 8, 8
        colors = _bayer(h, w)
        vis = np.full((h, w), 4000, np.uint16)
        vis[0, 1] = 16380          # セル (0,0) の G が飽和（T_libraw = 16348）
        vis[3, 3] = 16348          # セル (1,1) の B がちょうど閾値
        vis[4, 4] = 15400          # セル (2,2) の R は camera 閾値（15360 − 32）だけ超える
        vis[8 - 2:, 8 - 2:] = 16380  # セル (3,3) は 4 画素とも飽和
        vis[2, 2] = 9000; vis[2, 4] = 9032  # 上半分の値でステップ 32 を作る
        out = ce.cfa_analyze_arrays(vis, colors, ce.cfa_color_letters(b"RGBG"), [512] * 4, 16380, [15360] * 4)
        m = out["metrics"]
        self.assertEqual(m["cfa_step"], 32)
        self.assertEqual(m["cfa_t_libraw"], 16348)
        self.assertAlmostEqual(m["cfa_sat_r"], 1 / 16)   # R 16 画素中セル (3,3) の 1
        self.assertAlmostEqual(m["cfa_sat_g"], 3 / 32)   # G 32 画素中 (0,1) と (3,3) の 2
        self.assertAlmostEqual(m["cfa_sat_b"], 2 / 16)
        self.assertAlmostEqual(m["cfa_sat_cell_any"], 3 / 16)
        self.assertAlmostEqual(m["cfa_sat_cell_all"], 1 / 16)
        self.assertAlmostEqual(m["cfa_satc_r"], 2 / 16)  # camera 閾値では (2,2) の R も飽和
        bits = out["map_libraw"]
        self.assertEqual(bits.shape, (4, 4))
        self.assertEqual(int(bits[0, 0]), ce.CFA_BIT["g"])
        self.assertEqual(int(bits[1, 1]), ce.CFA_BIT["b"])
        self.assertEqual(int(bits[2, 2]), 0)
        self.assertEqual(int(out["map_camera"][2, 2]), ce.CFA_BIT["r"])
        self.assertEqual(int(bits[3, 3]), 7)
        self.assertEqual(out["thumb"].shape, (4, 4))
        self.assertGreater(m["cfa_headroom_g"], 0.2)

    def test_crop_drops_margins_and_keeps_even(self):
        class Sizes:
            crop_top_margin, crop_left_margin, crop_height, crop_width = 2, 3, 9, 11

        out = ce.cfa_crop(np.arange(20 * 20).reshape(20, 20), Sizes)
        self.assertEqual(out.shape, (8, 10))
        self.assertEqual(int(out[0, 0]), 2 * 20 + 3)

    def test_orient_array_matches_pil(self):
        from PIL import Image

        base = np.arange(3 * 5, dtype=np.uint8).reshape(3, 5)
        methods = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180, 4: Image.Transpose.FLIP_TOP_BOTTOM,
                   5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
                   8: Image.Transpose.ROTATE_90}
        np.testing.assert_array_equal(ce.orient_array(base, 1), base)
        np.testing.assert_array_equal(ce.orient_array(base, None), base)
        for o, method in methods.items():
            with self.subTest(orientation=o):
                expected = np.asarray(Image.fromarray(base).transpose(method))
                np.testing.assert_array_equal(ce.orient_array(base, o), expected)

    def test_resize_cell_map_or_downscale_and_nearest_upscale(self):
        bits = np.zeros((10, 15), np.uint8)
        bits[4, 7] = ce.CFA_BIT["g"]
        bits[9, 14] = ce.CFA_BIT["b"] | ce.CFA_BIT["r"]
        small = ce.resize_cell_map(bits, (12, 8))  # 1.25 倍縮小（非整数）
        self.assertEqual(small.shape, (8, 12))
        self.assertGreaterEqual(int(((small & ce.CFA_BIT["g"]) > 0).sum()), 1)  # 単独のセルも OR で残る
        self.assertEqual(int(small[-1, -1]), ce.CFA_BIT["b"] | ce.CFA_BIT["r"])
        self.assertEqual(int(((small & ce.CFA_BIT["r"]) > 0).sum()), 1)
        big = ce.resize_cell_map(bits, (30, 20))  # 2 倍拡大（full）
        self.assertEqual(int(((big & ce.CFA_BIT["g"]) > 0).sum()), 4)
        np.testing.assert_array_equal(ce.resize_cell_map(bits, (15, 10)), bits)


class DiagnosisDumpAndColorTests(unittest.TestCase):
    def test_load_dump_checks_meta(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            arr = np.random.default_rng(0).random((4, 6, 3)).astype("<f4") * 3
            path = Path(tmp) / "x.f32"
            arr.tofile(path)
            meta = {"path": str(path), "width": 6, "height": 4, "channels": 3, "dtype": "float32", "byte_order": "little",
                    "layout": "interleaved_rgb_row_major", "color_space": "linear_srgb", "bytes": 4 * 6 * 12}
            np.testing.assert_array_equal(ce.load_dump(meta), arr)
            for key, bad in (("dtype", "float16"), ("byte_order", "big"), ("layout", "planar"), ("width", 5), ("bytes", 1)):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    ce.load_dump({**meta, key: bad})

    def test_srgb_encode_inverts_decode(self):
        v = np.linspace(0, 1, 101, dtype=np.float32)
        np.testing.assert_allclose(ce.srgb_encode(ce.srgb_to_linear(v)), v, atol=1e-5)

    def test_p3_to_srgb(self):
        np.testing.assert_allclose(ce.p3_to_srgb(np.array([[1.0, 1.0, 1.0]], np.float32)), [[1, 1, 1]], atol=1e-4)
        np.testing.assert_allclose(ce.p3_to_srgb(np.array([[0.5, 0.5, 0.5]], np.float32)), [[0.5, 0.5, 0.5]], atol=1e-4)
        red = ce.p3_to_srgb(np.array([[1.0, 0.0, 0.0]], np.float32))[0]
        self.assertGreater(red[0], 1.0)   # P3 の純赤は sRGB の外（R が 1 を超える）
        self.assertLess(red[1], 0.0)


def _diag_scene(w=600, h=400, seed=1):
    """空（明るい・一部白飛び）/ 雲の階調 / 中間調 / 暗い前景を持つ合成カメラ JPEG（uint8）。"""
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 3), np.float32)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    sky = slice(0, int(h * 0.4))
    img[sky] = np.stack([0.62 + 0.1 * xx[sky] / w, 0.80 + 0.08 * xx[sky] / w, 0.97 + 0 * xx[sky]], axis=2)
    clouds = cv2.GaussianBlur(rng.random((h, w)).astype(np.float32), (0, 0), 6)
    clouds = (clouds - clouds.min()) / (clouds.max() - clouds.min())
    img[sky] += (clouds[sky] * 0.35)[..., None]
    img[: int(h * 0.12), : int(w * 0.3)] = 1.0           # 白飛びした太陽周り
    mid = slice(int(h * 0.4), int(h * 0.7))
    img[mid] = 0.45 + 0.05 * cv2.GaussianBlur(rng.standard_normal((h, w)).astype(np.float32), (0, 0), 2)[mid][..., None]
    fg = slice(int(h * 0.7), h)
    tex = cv2.GaussianBlur(rng.random((h, w)).astype(np.float32), (0, 0), 1.5)
    img[fg] = (0.05 + 0.12 * tex[fg])[..., None]
    return (np.clip(img, 0, 1) * 255).round().astype(np.uint8)



class DiagnosisRoiTests(unittest.TestCase):
    def test_rois_found_and_disjoint(self):
        cam = _diag_scene()
        rois, info = ce.diagnosis_rois(cam, 60)
        self.assertEqual(info["size"], 60)
        for name in ce.DIAG_ROI_NAMES:
            self.assertIsNotNone(rois[name], name)
        boxes = [b for b in rois.values() if b]
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                self.assertFalse(ce._overlaps(a, [b]), (a, b))
        self.assertLess(rois["sky_bright"][1], 400 * 0.4)            # 空の中
        self.assertGreaterEqual(rois["fg_dark"][1], int(400 * 0.7) - 30)  # 前景
        mid_y = (rois["midtone"][1] + rois["midtone"][3]) / 2
        self.assertTrue(400 * 0.4 <= mid_y <= 400 * 0.7)

    def test_rois_missing_when_no_bright_or_clip(self):
        dark = np.full((200, 300, 3), 40, np.uint8)
        rois, _ = ce.diagnosis_rois(dark, 40)
        self.assertIsNone(rois["sky_bright"])
        self.assertIsNone(rois["clip_edge"])
        self.assertIsNone(rois["cloud_grad"])

    @staticmethod
    def _cloud_scene(colored_rgb: tuple[int, int, int], gray_cloud: bool = True) -> np.ndarray:
        """左半分に起伏の強い有彩色の明るい被写体、右半分に起伏の弱い灰色の雲（帯 0.6〜0.98 に入る明るさ）。"""
        h, w = 240, 480
        yy, xx = np.mgrid[0:h, 0:w]
        img = np.full((h, w, 3), 30, np.float32)  # 暗い背景（帯の外）
        stripes = ((xx // 6) % 2).astype(np.float32)
        base = np.array(colored_rgb, np.float32)
        left = base[None, None, :] * (0.85 + 0.15 * stripes[..., None])  # 大きな起伏
        img[:, : w // 2] = left[:, : w // 2]
        if gray_cloud:
            g = 200 + 8 * np.sin(xx / 15.0)  # 小さな起伏の灰色
            img[:, w // 2:] = np.repeat(g[..., None], 3, axis=2)[:, w // 2:]
        return np.clip(img, 0, 255).astype(np.uint8)

    def test_cloud_grad_prefers_low_saturation(self):
        # 青い被写体（HSV S ≈ 0.4、L 0.65〜0.76 で帯に入る）は起伏が大きくても cloud_grad にしない
        cam = self._cloud_scene((150, 200, 255))
        rois, info = ce.diagnosis_rois(cam, 60)
        self.assertTrue(info["cloud_grad_low_sat"])
        self.assertGreaterEqual(rois["cloud_grad"][0], 240)
        # 桃色の雲（HSV S ≈ 0.15、DSC04532 相当）は低彩度として残る
        pink = self._cloud_scene((235, 215, 205), gray_cloud=False)
        rois, info = ce.diagnosis_rois(pink, 60)
        self.assertTrue(info["cloud_grad_low_sat"])
        self.assertLess(rois["cloud_grad"][0], 240)

    def test_cloud_grad_falls_back_to_band_without_low_sat(self):
        # 帯に低彩度の箱が無ければ従来の帯で選ぶ（cloud_grad を欠測にしない）
        cam = self._cloud_scene((150, 200, 255), gray_cloud=False)
        rois, info = ce.diagnosis_rois(cam, 60)
        self.assertFalse(info["cloud_grad_low_sat"])
        self.assertIsNotNone(rois["cloud_grad"])
        self.assertLess(rois["cloud_grad"][0], 240)

    def test_align_rois_shift_and_sky_borrow(self):
        cam = _diag_scene()
        moved = np.roll(cam, (3, 5), axis=(0, 1))
        rois, _ = ce.diagnosis_rois(cam, 60)
        g0, g1 = cv2.cvtColor(cam, cv2.COLOR_RGB2GRAY), cv2.cvtColor(moved, cv2.COLOR_RGB2GRAY)
        al = ce.align_rois(g0, g1, rois, 60)
        mid = al["midtone"]
        self.assertTrue(mid["ok"])
        self.assertAlmostEqual(mid["dx"], 5, delta=0.6)
        self.assertAlmostEqual(mid["dy"], 3, delta=0.6)
        self.assertEqual(al["sky_bright"]["shift_source"], "midtone")
        self.assertEqual(al["sky_bright"]["box"][0], rois["sky_bright"][0] + 5)
        # 無関係な画像では位置合わせ失敗 → 欠測
        noise = (np.random.default_rng(9).random(g0.shape) * 255).astype(np.uint8)
        bad = ce.align_rois(g0, noise, rois, 60)
        self.assertTrue(any(v["box"] is None and v["reason"] == "align_failed" for v in bad.values()))


class DiagnosisMetricTests(unittest.TestCase):
    def test_stage_rates_and_loss(self):
        img = np.zeros((2, 2, 3), np.float32)
        img[0, 0] = (1.2, 0.5, 0.5)
        img[0, 1] = (1.5, 1.5, 1.5)
        img[1, 0] = (1.0, 1.0, 1.0)
        out = ce.stage_rates(img, "s1_gt1")
        self.assertAlmostEqual(out["s1_gt1_r"], 0.5)
        self.assertAlmostEqual(out["s1_gt1_any"], 0.5)
        self.assertAlmostEqual(out["s1_gt1_all"], 0.25)
        ge = ce.stage_rates(img, "s3_ge1", ce.WHITE16_LINEAR, strict=False)
        self.assertAlmostEqual(ge["s3_ge1_all"], 0.5)
        prev = np.array([[True, False], [False, False]])
        cur = np.array([[True, True], [False, True]])
        self.assertAlmostEqual(ce.stage_loss(prev, cur), 0.5)
        with self.assertRaises(ValueError):
            ce.stage_loss(prev, cur[:1])

    def test_blue_only_clip(self):
        w = np.zeros((2, 2, 3), bool)
        w[0, 0, 2] = True
        w[0, 1] = True
        self.assertAlmostEqual(ce.blue_only_clip(w), 0.25)

    def test_texture_levels_and_slope(self):
        ramp = np.tile(np.linspace(0, 1, 64, dtype=np.float32), (32, 1))
        self.assertEqual(ce.distinct_levels(ramp, None, 255), 64)
        self.assertEqual(ce.distinct_levels(np.ones((8, 8), np.float32), None, 255), 1)
        flat = np.full((32, 32), 0.9, np.float32)
        self.assertAlmostEqual(ce.grad_texture(flat, flat > 0.8), 0.0, places=6)
        self.assertTrue(math.isnan(ce.grad_texture(flat, flat > 2)))
        noisy = flat + np.random.default_rng(0).normal(0, 0.02, flat.shape).astype(np.float32)
        self.assertGreater(ce.grad_texture(noisy, noisy > 0.8), 0.005)
        edge = np.tile(np.clip((np.arange(64) - 22) / 20.0, 0, 1).astype(np.float32), (40, 1))  # 22..42 で 0→1
        angle = ce.dominant_gradient_angle(edge)
        self.assertAlmostEqual(abs(math.cos(angle)), 1.0, places=3)
        self.assertAlmostEqual(ce.rise_width_10_90(ce.projected_profile(edge, angle)), 16.0, delta=0.6)
        self.assertTrue(math.isnan(ce.rise_width_10_90(np.full(10, 0.5))))

    def test_pchip_and_interp(self):
        f = ce.pchip_curve(ce.TONE_CURVE_BOOST1)
        xs = np.array([p[0] for p in ce.TONE_CURVE_BOOST1])
        np.testing.assert_allclose(f(xs), [p[1] for p in ce.TONE_CURVE_BOOST1], atol=1e-9)
        dense = f(np.linspace(0, 2.7, 500))
        self.assertTrue(np.all(np.diff(dense) >= -1e-9))  # 単調
        self.assertAlmostEqual(float(f(np.array([2.7]))[0]), 1.145 + 0.145 * 0.7, places=3)
        pts = [(0.30, 0.10), (0.20, 0.02), (0.25, 0.05)]
        self.assertAlmostEqual(ce.interp_at(pts, 0.225), 0.035)
        self.assertTrue(math.isnan(ce.interp_at(pts, 0.35)))  # 外挿しない
        self.assertTrue(math.isnan(ce.interp_at([(0.2, 1.0), (float("nan"), 2.0)], 0.2)))

    def test_sky_color(self):
        rgb = np.zeros((10, 10, 3), np.float32)
        rgb[...] = (0.5, 0.7, 0.95)
        white = np.zeros_like(rgb, bool)
        out = ce.sky_color(rgb, white)
        self.assertTrue(200 < out["sky_hue"] < 300)  # 青
        self.assertGreater(out["sky_chroma"], 10)
        white[...] = True
        self.assertTrue(math.isnan(ce.sky_color(rgb, white)["sky_hue"]))


class DiagnosisAttributionTests(unittest.TestCase):
    def test_attribute_pixels_order(self):
        k = np.array([True, True, True, True, False])
        sensor = np.array([True, False, False, False, True])
        s2_over = np.array([True, True, False, False, True])
        s3_white = np.array([True, True, True, False, True])
        out = ce.attribute_pixels(k, sensor, s2_over, s3_white)
        # s3_near_white が無ければ規則 4 は分けない（旧 unverifiable と同じ）
        self.assertEqual(out, {"k": 4, "sensor": 1, "decoder_tone": 1, "quant8": 1, "jpeg_encode": 0, "unverifiable": 1})
        bad = ce.attribute_pixels(k, None, s2_over, s3_white)
        self.assertEqual(bad["unverifiable"], 4)
        self.assertEqual(ce.attribute_pixels(k, sensor, s2_over, s3_white, valid=False)["unverifiable"], 4)
        # dump が無い経路は S3 が分からないので JPEG 符号化とは言わない
        near = np.ones(5, bool)
        self.assertEqual(ce.attribute_pixels(k, sensor, s2_over, None, s3_near_white=near)["jpeg_encode"], 0)

    def test_attribute_pixels_splits_jpeg_encode(self):
        # K の 6 画素: sensor / tone / quant8 / jpeg_encode ×2 / unverifiable
        k = np.array([True, True, True, True, True, True, False])
        sensor = np.array([True, False, False, False, False, False, False])
        s2_over = np.array([False, True, False, False, False, False, False])
        s3_white = np.array([False, False, True, False, False, False, False])
        near = np.array([False, False, True, True, True, False, False])
        out = ce.attribute_pixels(k, sensor, s2_over, s3_white, s3_near_white=near)
        self.assertEqual(out, {"k": 6, "sensor": 1, "decoder_tone": 1, "quant8": 1, "jpeg_encode": 2, "unverifiable": 1})
        # 規則の順序: quant8 は jpeg_encode より先（S3 が 8bit 白なら near でも quant8）
        self.assertEqual(sum(out[c] for c in ce.ATTR_CATEGORIES), out["k"])

    def test_attribute_pixels_k_from_s3_has_no_rule4(self):
        # K を S3 の 8bit 白から取ると、sensor / tone 以外は全部 quant8（jpeg_encode / unverifiable は構造上 0）
        rng = np.random.default_rng(3)
        s3_white = rng.random(200) > 0.5
        sensor, s2_over = rng.random(200) > 0.8, rng.random(200) > 0.6
        out = ce.attribute_pixels(s3_white, sensor, s2_over, s3_white, s3_near_white=np.ones(200, bool))
        self.assertEqual(out["jpeg_encode"] + out["unverifiable"], 0)
        self.assertEqual(out["sensor"] + out["decoder_tone"] + out["quant8"], out["k"])

    def test_classify_attribution(self):
        def c(**kw):
            return {"k": sum(kw.values()), **{n: kw.get(n, 0) for n in ce.ATTR_CATEGORIES}}

        self.assertEqual(ce.classify_attribution(c(sensor=5), 1000), "no_clip")
        self.assertEqual(ce.classify_attribution(c(decoder_tone=80, sensor=20), 1000), "decoder_tone")
        self.assertEqual(ce.classify_attribution(c(decoder_tone=60, sensor=40), 1000), "mixed")
        self.assertEqual(ce.classify_attribution(c(decoder_tone=40, sensor=25, quant8=20, unverifiable=15), 1000), "mixed")
        self.assertEqual(ce.classify_attribution(None, 1000), "unverifiable")
        self.assertEqual(ce.classify_attribution(c(jpeg_encode=80, decoder_tone=20), 1000), "jpeg_encode")

    def test_classify_scene(self):
        counts = {"sky_bright": {"k": 10, "sensor": 0, "decoder_tone": 10, "quant8": 0, "jpeg_encode": 0, "unverifiable": 0}}
        scene = ce.classify_scene_diagnosis
        self.assertEqual(scene({"sky_bright": "no_clip", "cloud_grad": "no_clip"}, {}, [], 1.0, 0.0), "no_clip")
        self.assertEqual(scene({"sky_bright": "sensor", "cloud_grad": "sensor"}, {}, [], 1.0, 0.5), "sensor")
        self.assertEqual(scene({"sky_bright": "decoder_tone", "cloud_grad": "decoder_tone"}, counts, [0.3, 0.6], 1.0, 0.0), "decoder_tone")
        # 階調が戻らない decoder_tone は mixed へ落ちる（S1 に階調がある）
        self.assertEqual(scene({"sky_bright": "decoder_tone", "cloud_grad": "decoder_tone"}, counts, [0.2, 0.3], 0.9, 0.0), "mixed")
        # S1 にも階調が無く S0 も飽和していない → 検証不能（API 境界）
        self.assertEqual(scene({"sky_bright": "decoder_tone", "cloud_grad": "decoder_tone"}, counts, [0.2, 0.3], 0.2, 0.0), "unverifiable")
        gam = {"cloud_grad": {"k": 10, "sensor": 1, "decoder_tone": 2, "quant8": 7, "jpeg_encode": 0, "unverifiable": 0}}
        self.assertEqual(scene({"sky_bright": "no_clip", "cloud_grad": "quant8"}, gam, [], 1.0, 0.0), "quant8")
        # jpeg_encode と unverifiable は合算して旧 unverifiable として比べる（単独では quant8 より少なくても）
        split = {"cloud_grad": {"k": 10, "sensor": 0, "decoder_tone": 0, "quant8": 4, "jpeg_encode": 3, "unverifiable": 3}}
        self.assertEqual(scene({"sky_bright": "no_clip", "cloud_grad": "mixed"}, split, [], 1.0, 0.0), "unverifiable")
        self.assertEqual(scene({"sky_bright": "sensor", "cloud_grad": "mixed"}, counts, [0.9], 1.0, 0.3), "mixed")

    def test_candidate_checks_and_best(self):
        cam = {"sky_bright_clip8_any": 0.10}
        good = {"mid_p50_diff": 0.01, "sky_bright_clip8_any": 0.05, "grad_texture_ratio": 0.8, "fg_p01_diff": 0.0,
                "magenta_rate": 0.0, "band_pixels": 10}
        out = ce.candidate_checks(good, cam)
        self.assertTrue(out["c_pass_auto"])
        self.assertIsNone(out["c_pass_visual"])
        dark = {**good, "mid_p50_diff": -0.05}
        self.assertFalse(ce.candidate_checks(dark, cam)["c_pass_auto"])
        self.assertIsNone(ce.candidate_checks({**good, "grad_texture_ratio": None}, cam)["c_pass_auto"])
        self.assertTrue(ce.candidate_checks({**good, "magenta_rate": float("nan"), "band_pixels": 0}, cam)["c_pass_magenta"])
        rows = {"c1_ev-07_lift": {**good, "sky_bright_clip8_any": 0.04}, "c3_lin_curve": {**good, "sky_bright_clip8_any": 0.02},
                "c2_ev-07_hsa": {**good, "mid_p50_diff": -0.2, "sky_bright_clip8_any": 0.0}}
        self.assertEqual(ce.choose_c_best(rows), {"best": "c3_lin_curve", "within_tol": True})
        out_tol = {"c1_ev-07_lift": {"mid_p50_diff": -0.05}, "c3_lin_curve": {"mid_p50_diff": -0.2}}
        self.assertEqual(ce.choose_c_best(out_tol), {"best": "c1_ev-07_lift", "within_tol": False})
        self.assertEqual(ce.choose_c_best({}), {"best": None, "within_tol": False})


class DiagnosisContractTests(unittest.TestCase):
    def test_default_request_unchanged(self):
        req = ce.coreimage_request(Path("/x/a.ARW"), Path("/x/o.jpg"), "preview", True, max_edge=2400)
        self.assertEqual(set(req), {"input", "output", "mode", "decoder_version", "settings", "max_edge"})
        self.assertEqual(req["settings"], {"profile": "neutral", "lens_correction": True})
        req2 = ce.coreimage_request(Path("/x/a.ARW"), Path("/x/o.jpg"), "preview", False, extra_settings={"gamut_mapping": False},
                                    diagnostics={"skip_write": True})
        self.assertIs(req2["settings"]["gamut_mapping"], False)
        self.assertEqual(req2["diagnostics"], {"skip_write": True})

    def test_all_specs_satisfy_contract(self):
        specs = ce.diagnosis_specs()
        self.assertEqual(len(specs), 25)  # §6.2 の 26 から、preview では別リクエストにしない c_best を除いた数
        for variant, spec in specs.items():
            with self.subTest(variant=variant):
                dump = Path(f"/tmp/w/{variant}.f32") if spec["dump"] else None
                kwargs = ce.diagnosis_request_kwargs(spec, dump)
                req = ce.coreimage_request(Path("/src/DSC00001.ARW"), Path(f"/tmp/w/{variant}.jpg"), "preview", spec["lens"],
                                           max_edge=2400, **kwargs)
                self.assertEqual(ce.validate_diagnostics_request(req), [])
                if spec["dump"]:
                    self.assertFalse(spec["lens"])
                    self.assertTrue(req["diagnostics"]["skip_write"])
                    self.assertEqual(req["diagnostics"]["dump_float"]["path"], str(dump))
                if spec["adjustments"] is not None:
                    self.assertNotIn("exposure_ev", req["settings"])
                if variant in ("d_base", "d_lens_off") or variant.startswith(("d_boost", "d_ev", "d_edr")):
                    self.assertNotIn("diagnostics", req)  # 通常モードの経路（§4.1 不変条件 1）
        self.assertEqual(specs["d_s1_linear"]["overrides"], {"boost_amount": 0.0})
        self.assertIs(specs["d_s1_linear"]["extra_settings"]["gamut_mapping"], False)
        # T3（2026-10-21）: C1 の brightness は 1.0 固定（ガンマ空間の乗算で白側が戻るため。レポート §4）。
        for name in ce.C1_SPECS:
            self.assertEqual(specs[name]["adjustments"]["brightness"], 1.0)
            self.assertLess(specs[name]["adjustments"]["exposure"], 0.0)
        # C3 の CIToneCurve は sRGB ガンマ座標の制御点で、x・y とも単調増加（レポート §4）。
        pts = [ce.C3_TONE_CURVE[f"inputPoint{i}"] for i in range(5)]
        self.assertEqual(pts[0], [0.0, 0.0])
        self.assertEqual(pts[4], [1.0, 1.0])
        self.assertTrue(all(a[0] < b[0] and a[1] < b[1] for a, b in zip(pts, pts[1:])))
        full = ce.diagnosis_mode_specs("full", "c3_lin_curve")
        self.assertIn("d_lens_off", full)
        self.assertEqual(full["c_best"]["diagnostics"], specs["c3_lin_curve"]["diagnostics"])
        self.assertNotIn("c_best", ce.diagnosis_mode_specs("full", None))

    def test_validator_rejects(self):
        base = {"input": "/a.ARW", "output": "/o.jpg", "mode": "preview", "decoder_version": "9",
                "settings": {"profile": "apple_default", "lens_correction": True}}
        cases = {
            "unknown_top": {**base, "foo": 1},
            "gamut_str": {**base, "settings": {**base["settings"], "gamut_mapping": "no"}},
            "baseline_range": {**base, "settings": {**base["settings"], "baseline_exposure": 9}},
            "diag_key": {**base, "diagnostics": {"roi": [0, 0, 1, 1]}},
            "dump_rel": {**base, "diagnostics": {"dump_float": {"path": "x.f32"}}},
            "dump_ext": {**base, "diagnostics": {"dump_float": {"path": "/x.bin"}}},
            "dump_eq_out": {**base, "output": "/o.f32", "diagnostics": {"dump_float": {"path": "/o.f32"}}},
            "lsf_value": {**base, "diagnostics": {"linear_space_filter": {"kind": "scale", "value": 0}}},
            "lsf_identity_value": {**base, "diagnostics": {"linear_space_filter": {"kind": "identity", "value": 1}}},
            "pf_name": {**base, "diagnostics": {"post_filters": [{"name": "CIGaussianBlur", "inputRadius": 2}]}},
            "pf_param": {**base, "diagnostics": {"post_filters": [{"name": "CIExposureAdjust", "inputEV": 1, "x": 2}]}},
            "pf_range": {**base, "diagnostics": {"post_filters": [{"name": "CIHighlightShadowAdjust", "inputHighlightAmount": 2}]}},
            "curve_x": {**base, "diagnostics": {"post_filters": [{"name": "CIToneCurve", "inputPoint0": [0, 0], "inputPoint1": [1.5, 1]}]}},
            "curve_mono": {**base, "diagnostics": {"post_filters": [{"name": "CIToneCurve", "inputPoint0": [0.5, 0], "inputPoint1": [0.2, 1]}]}},
            "pf_too_many": {**base, "diagnostics": {"post_filters": [{"name": "CIExposureAdjust", "inputEV": 0}] * 17}},
            "adj_with_ev": {**base, "settings": {**base["settings"], "exposure_ev": -1}, "adjustments": dict(ce.ADJ_IDENTITY)},
        }
        for name, req in cases.items():
            with self.subTest(case=name):
                self.assertTrue(ce.validate_diagnostics_request(req), name)
        ok = {**base, "diagnostics": {"stats": True, "linear_space_filter": {"kind": "identity"},
                                      "post_filters": [{"name": "CIColorClamp", "inputMinComponents": [0, 0, 0, 0],
                                                        "inputMaxComponents": [1, 1, 1, 1]}]}}
        self.assertEqual(ce.validate_diagnostics_request(ok), [])

    def test_cli_problem(self):
        caps = {"diagnostics": {"supported": True, "post_filters": list(ce.POST_FILTER_ALLOWLIST)}}
        self.assertIsNone(ce.diagnosis_cli_problem({"cli_version": "0.4.0"}, caps))
        self.assertIn("0.4.0", ce.diagnosis_cli_problem({"cli_version": "0.3.0"}, caps))
        self.assertIn("diagnostics.supported", ce.diagnosis_cli_problem({"cli_version": "0.4.1"}, {}))
        self.assertIn("取得できません", ce.diagnosis_cli_problem({"error": "x"}, caps))

    def test_response_summary(self):
        payload = {"applied": {"gamut_mapping_enabled": False}, "diagnostics": {
            "cli_diagnostics_version": 1, "written": False, "stats_color_space": "linear_srgb", "stats_source": "render_input",
            "stats": {"width": 4, "height": 2, "r": {"p50": 0.1, "max": 2.7, "frac_gt_1": 0.3}, "any_gt_1": 0.38, "all_gt_1": 0.25},
            "readback": {"gamut_mapping_enabled": False, "baseline_exposure": 0.4, "extent": [0, 0, 4, 2]},
            "dump_float": {"path": "/x.f32"}}}
        out = ce.diagnostics_response_summary(payload)
        self.assertIs(out["ci_gamut"], False)
        self.assertEqual(out["ci_stats_any_gt_1"], 0.38)
        self.assertEqual(out["ci_stats_r_max"], 2.7)
        self.assertIsNone(out["ci_stats_g_max"])
        self.assertEqual(out["ci_readback_extent"], "[0, 0, 4, 2]")
        self.assertFalse(ce.diagnostics_response_summary({"applied": {}})["ci_diag_present"])


def _linear_scene(cam8, headroom=1.6):
    return ce.srgb_to_linear(cam8.astype(np.float32) / 255.0) * headroom


class DiagnosisMeasureTests(unittest.TestCase):
    """ファイル I/O 無しの 1 カット分のフロー（帰属 → ROI 分類 → シーン判定 → 内挿 → c_best）。"""

    def test_measure_flow_on_synthetic_stages(self):
        cam8 = _diag_scene()
        h, w = cam8.shape[:2]
        s1 = _linear_scene(cam8)
        curve = ce.pchip_curve(ce.TONE_CURVE_BOOST1)
        s2 = curve(s1.astype(np.float64)).astype(np.float32)
        s3 = np.clip(s2, 0, 1)
        out8 = (ce.srgb_encode(s3) * 255).round().astype(np.uint8)

        def as_img(rgb8):
            return rgb8.astype(np.float32) / 255.0

        images = {"camera": as_img(cam8), "d_base": as_img(out8), "d_lens_off": as_img(out8)}
        for name, ev in ce.DIAG_EV_SWEEP[1:]:
            images[name] = as_img((ce.srgb_encode(np.clip(curve(s1 * 2 ** ev), 0, 1)) * 255).round().astype(np.uint8))
        images["c1_ev-07_lift"] = images["d_ev-07"]
        white = {k: (v * 255).round().astype(np.uint8) >= 255 for k, v in images.items()}
        levels = {k: 255 for k in images}
        # センサー飽和: 白飛び矩形（左上）の左半分だけ
        sensor = np.zeros((h // 2, w // 2), np.uint8)
        sensor[: int(h * 0.12) // 2, : int(w * 0.15) // 2] = ce.CFA_BIT["g"]
        cfa = {"metrics": {"cfa_sat_g": 0.01}, "map_libraw": ce.resize_cell_map(sensor, (w, h)),
               "map_camera": ce.resize_cell_map(sensor, (w, h)), "thumb": ce.luma_of(s1 / 1.6)}
        dumps = {"d_s1_linear": s1, "d_s2_tone": s2, "d_s3_default": s3}
        rois, _ = ce.diagnosis_rois(cam8, 60)
        rows, summary = ce.diagnosis_measure(images, white, levels, dumps, cfa, rois, 60,
                                             cli_any_gt_1={"d_s1_linear": float((s1 > 1).any(axis=2).mean())})
        st = rows["stages"]
        self.assertEqual(st["k_source"], "s3_dump")  # K は S3 dump の 8bit 白（§6.4）
        self.assertEqual(st["attr_full_jpeg_encode"] + st["attr_full_unverifiable"], 0.0)
        self.assertIn("attr_full_k_jpeg8_frac", st)
        jfr = [st[f"attr_full_jpeg8_{c}"] for c in ce.ATTR_CATEGORIES]  # 参考: JPEG の K の内訳も合計 1
        self.assertAlmostEqual(sum(jfr), 1.0, places=6)
        self.assertTrue(st["cfa_align_ok"])
        self.assertAlmostEqual(st["s1_cli_any_gt_1_diff"], 0.0, places=6)
        self.assertGreater(st["s1_gt1_any"], 0.05)
        self.assertAlmostEqual(st["stage_loss_s2s3"], 0.0, places=6)
        self.assertLess(st["curve_check_dl_p99_midtone"], 1e-4)  # 同じ曲線で作ったので大域的
        self.assertTrue(st["curve_global"])
        for roi in ("sky_bright", "full"):
            fr = [st[f"attr_{roi}_{c}"] for c in ce.ATTR_CATEGORIES]
            self.assertAlmostEqual(sum(fr), 1.0, places=6)
        self.assertGreater(st["attr_full_sensor"], 0.0)
        self.assertGreater(st["attr_full_decoder_tone"], 0.0)
        self.assertIn(st["scene_class"], {"no_clip", "sensor", "decoder_tone", "quant8", "unverifiable", "mixed"})
        self.assertIn(summary["roi_class"]["sky_bright"], {"sensor", "decoder_tone", "mixed"})
        self.assertAlmostEqual(rows["camera"]["mid_p50_diff"], 0.0)
        interp = rows["d_interp_camera_p50"]
        self.assertTrue(interp["interpolated"])
        self.assertIn("c_pass_auto", rows["c1_ev-07_lift"])
        self.assertEqual(st["c_best"], "c1_ev-07_lift")
        self.assertIn("cfa", rows)

    def test_measure_without_dumps_is_unverifiable(self):
        cam8 = _diag_scene()
        img = cam8.astype(np.float32) / 255.0
        images = {"camera": img, "d_lens_off": img, "d_base": img}
        white = {k: cam8 >= 255 for k in images}
        rois, _ = ce.diagnosis_rois(cam8, 60)
        rows, summary = ce.diagnosis_measure(images, white, {k: 255 for k in images}, {}, None, rois, 60)
        self.assertEqual(rows["stages"]["attr_full_unverifiable"], 1.0)
        self.assertFalse(summary["stages_ok"])
        # dump が無ければ K は d_lens_off の 8bit JPEG から取る
        self.assertEqual(rows["stages"]["k_source"], "jpeg8")
        self.assertAlmostEqual(rows["stages"]["attr_full_k_frac"], rows["stages"]["attr_full_k_jpeg8_frac"])


MOCK_CLI = r'''#!{python}
"""テスト用の偽 CLI 0.4.0（設計書 §4 の応答形だけ真似る）。入力 ARW と同名の JPG を読んで段階を合成する。"""
import json, os, sys
import cv2
import numpy as np
from PIL import Image, ImageOps

XS = [0.0, 0.05, 0.18, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0]
YS = [0.0, 0.0744, 0.3875, 0.805, 0.9573, 1.0, 1.029, 1.0725, 1.145]

def lin(v):
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)

def enc(v):
    v = np.clip(v, 0, 1)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * v ** (1 / 2.4) - 0.055)

def curve(v):
    out = np.interp(v, XS, YS)
    return np.where(v > 2.0, 1.145 + 0.145 * (v - 2.0), out)

args = sys.argv[1:]
if args[:1] == ["--version"]:
    print(json.dumps({"cli_version": "0.4.0"})); sys.exit(0)
if args[:1] == ["--capabilities"]:
    print(json.dumps({"adjustments": {"supported": True}, "diagnostics": {"supported": True, "post_filters": ["CIHighlightShadowAdjust", "CIToneCurve", "CIExposureAdjust", "CIColorClamp"]}})); sys.exit(0)
req = json.load(sys.stdin)
with Image.open(os.path.splitext(req["input"])[0] + ".JPG") as im:
    rgb = np.asarray(ImageOps.exif_transpose(im).convert("RGB"), dtype=np.float32) / 255.0
if req["mode"] == "preview":
    h, w = rgb.shape[:2]
    s = req.get("max_edge", 2400) / max(h, w)
    if s < 1:
        rgb = cv2.resize(rgb, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
st = req["settings"]
adj = req.get("adjustments") or {}
diag = req.get("diagnostics")
x = lin(rgb) * 1.6 * 2.0 ** (st.get("exposure_ev", adj.get("exposure", 0.0)))
boost = st.get("boost_amount", 1.0)
x = (1 - boost) * x + boost * curve(x)
for f in (diag or {}).get("post_filters", []):
    if f["name"] == "CIExposureAdjust":
        x = x * 2.0 ** f["inputEV"]
if st.get("gamut_mapping", True):
    x = np.clip(x, 0, 1)
if adj:
    x = np.clip(x, 0, 1) * adj.get("brightness", 1.0)
x = x.astype(np.float32)
h, w = x.shape[:2]
resp = {"ok": True, "output": req["output"], "width": w, "height": h, "elapsed_ms": {"total": 5, "decode": 2, "render_encode": 3},
        "applied": {"gamut_mapping_enabled": st.get("gamut_mapping", True), "baseline_exposure": 0.4, "boost_amount": boost,
                    "lens_correction_enabled": st.get("lens_correction")}}
written = not (diag or {}).get("skip_write", False)
if written:
    if req.get("format") == "tiff16":
        cv2.imwrite(req["output"], cv2.cvtColor((enc(x) * 65535).round().astype(np.uint16), cv2.COLOR_RGB2BGR))
    else:
        Image.fromarray((enc(x) * 255).round().astype(np.uint8)).save(req["output"], "JPEG", quality=95)
if diag is not None:
    block = {"cli_diagnostics_version": 1, "written": written, "stats_color_space": "linear_srgb", "stats_source": "render_input",
             "stats": {"width": w, "height": h, "any_gt_1": float((x > 1).any(axis=2).mean()), "all_gt_1": float((x > 1).all(axis=2).mean())},
             "readback": {"gamut_mapping_enabled": st.get("gamut_mapping", True), "baseline_exposure": 0.4, "extent": [0, 0, w, h]}}
    for i, c in enumerate("rgb"):
        block["stats"][c] = {"mean": float(x[..., i].mean()), "p50": float(np.median(x[..., i])), "max": float(x[..., i].max()),
                             "frac_gt_1": float((x[..., i] > 1).mean())}
    if "dump_float" in diag:
        path = diag["dump_float"]["path"]
        x.astype("<f4").tofile(path)
        block["dump_float"] = {"path": path, "width": w, "height": h, "channels": 3, "dtype": "float32", "byte_order": "little",
                               "layout": "interleaved_rgb_row_major", "color_space": "linear_srgb", "bytes": w * h * 12}
    resp["diagnostics"] = block
print(json.dumps(resp))
'''


class DiagnosisMockCliIntegrationTests(unittest.TestCase):
    """CLI をモック（応答 JSON と .f32 ダンプを偽装）して --diagnosis を 1 カット preview + full まで通す。"""

    def setUp(self):
        import os
        import tempfile

        from PIL import Image

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src = root / "src"
        self.src.mkdir()
        Image.fromarray(_diag_scene(1200, 800)).save(self.src / "DSC00001.JPG", "JPEG", quality=95)
        (self.src / "DSC00001.ARW").write_bytes(b"not a raw file")
        self.cli = root / "mock-cli"
        self.cli.write_text(MOCK_CLI.replace("{python}", sys.executable), encoding="utf-8")
        os.chmod(self.cli, 0o755)
        self.out = root / "out"
        self.out.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_cfa(self, cid, orientation):
        from PIL import Image

        with Image.open(self.src / f"{cid}.JPG") as im:
            cam = np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0
        h, w = cam.shape[:2]
        small = cv2.resize(cam, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
        bits = np.zeros((h // 2, w // 2), np.uint8)
        bits[: int(h * 0.12) // 2, : int(w * 0.15) // 2] = ce.CFA_BIT["g"]
        return {"metrics": {"cfa_sat_g": float((bits > 0).mean()), "cfa_step": 32}, "map_libraw": bits, "map_camera": bits,
                "thumb": ce.luma_of(ce.srgb_to_linear(small))}

    def _ctx(self):
        return ce.RunContext(source_root=self.src, out=self.out, cli=self.cli, max_edge=600)

    def test_diagnosis_flow_preview_and_full(self):
        import csv

        ctx = self._ctx()
        features = {"DSC00001": {"capture_id": "DSC00001", "iso": 100, "orientation": 1, "lens_model": "TEST"}}
        selection = [{"capture_id": "DSC00001", "category": "backlit"}]
        ce.run_diagnosis(ctx, selection, features, ["DSC00001"], with_rawpy=False, cfa_loader=self._fake_cfa)
        with (self.out / "diagnosis_metrics.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        errors = [r for r in rows if r.get("error")]
        self.assertEqual(errors, [], errors[:3])
        by = {(r["mode"], r["variant"]): r for r in rows}
        for variant in ("camera", "d_base", "d_s1_linear", "d_tiff_p3", "c3_lin_curve", "c_best", "stages", "cfa",
                        "d_interp_camera_p50"):
            self.assertIn(("preview", variant), by)
        for variant in ("d_base", "d_lens_off", "c_best", "stages"):
            self.assertIn(("full", variant), by)
        stages = by[("preview", "stages")]
        self.assertEqual(stages["width"], "600")
        self.assertEqual(stages["cfa_align_ok"], "True")
        self.assertLess(abs(float(stages["s1_cli_any_gt_1_diff"])), 1e-6)  # dump の読み方（byte order / layout）が CLI 統計と一致
        self.assertGreater(float(stages["s1_gt1_any"]), 0.0)
        self.assertIn(stages["scene_class"], {"no_clip", "sensor", "decoder_tone", "quant8", "unverifiable", "mixed"})
        self.assertEqual(by[("preview", "d_s1_linear")]["ci_gamut"], "False")
        self.assertEqual(by[("full", "c_best")]["c_best_of"], stages["c_best"])
        self.assertEqual(by[("full", "stages")]["width"], "1200")
        summary = json.loads((self.out / "diagnosis_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["errors"], [])
        self.assertEqual({b["mode"] for b in summary["by_capture"]}, {"preview", "full"})
        rois = json.loads((self.out / "rois.json").read_text(encoding="utf-8"))
        self.assertIn("preview", rois["DSC00001"])
        self.assertIn("シーン判定", (self.out / "diagnosis_tables.md").read_text(encoding="utf-8"))
        self.assertEqual(list((self.out / "dumps").rglob("*.f32")), [])  # 指標の後に削除
        self.assertTrue((self.out / "sheets" / "DSC00001_preview_rois.jpg").is_file())
        text = (self.out / "diagnosis_summary.json").read_text(encoding="utf-8").lower()
        self.assertNotIn("gps", text)

    def test_keep_dumps(self):
        ctx = self._ctx()
        features = {"DSC00001": {"capture_id": "DSC00001", "orientation": 1}}
        ce.run_diagnosis(ctx, [{"capture_id": "DSC00001", "category": "other"}], features, [], with_rawpy=False,
                         cfa_loader=self._fake_cfa, keep_dumps=True)
        self.assertEqual(len(list((self.out / "dumps").rglob("*.f32"))), 3)

    def test_main_dry_run_writes_plan(self):
        ce.main(["--source-root", str(self.src), "--captures", "DSC00001", "--diagnosis", "--dry-run",
                 "--out", str(self.out), "--cli", str(self.cli), "--full", "--full-ids", "DSC00001"])
        plan = json.loads((self.out / "diagnosis_plan.json").read_text(encoding="utf-8"))
        self.assertTrue(plan["cli_ready"])
        self.assertEqual(plan["contract_problems"], [])
        self.assertEqual(plan["count"], 25 + 12)  # preview 25 + full 12（c_best は preview の結果で決まるので計画に出ない）


# =============================================================================
# --presets（Issue #81）
# =============================================================================

MOCK_CLI_060 = r"""#!{python}
# テスト用の偽 CLI 0.6.0: 入力 ARW と同名の JPG を読み、exposure（hr の写像を加算）・lift・brightness を当てる。
import json, os, sys
import cv2
import numpy as np
from PIL import Image, ImageOps

NODES = [(0.0, 0.0, 0.0), (60.0, -0.7, 40.0), (90.0, -1.0, 55.0), (100.0, -1.1, 60.0)]

def delta(hr):
    if not hr > 0:
        return 0.0, 0.0
    for (h0, e0, l0), (h1, e1, l1) in zip(NODES, NODES[1:]):
        if hr == h1:
            return e1, l1
        if hr < h1:
            t = (hr - h0) / (h1 - h0)
            return e0 + (e1 - e0) * t, l0 + (l1 - l0) * t
    return NODES[-1][1], NODES[-1][2]

args = sys.argv[1:]
if args[:1] == ["--version"]:
    print(json.dumps({"cli_version": "0.6.0"})); sys.exit(0)
if args[:1] == ["--capabilities"]:
    print(json.dumps({"adjustments": {"supported": True, "ignored": []}})); sys.exit(0)
req = json.load(sys.stdin)
with Image.open(os.path.splitext(req["input"])[0] + ".JPG") as im:
    rgb = np.asarray(ImageOps.exif_transpose(im).convert("RGB"), dtype=np.float32) / 255.0
if req["mode"] == "preview":
    h, w = rgb.shape[:2]
    s = req.get("max_edge", 2400) / max(h, w)
    if s < 1:
        rgb = cv2.resize(rgb, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
adj = req.get("adjustments")
resp = {"ok": True, "output": req["output"], "elapsed_ms": {"total": 5, "decode": 2, "render_encode": 3}}
x = rgb.copy()
exposure = 0.0
if adj:
    d_ev, d_lift = delta(adj["highlight_recovery"])
    exposure = adj["exposure"] + d_ev
    lift = min(100.0, max(0.0, adj["shadow_lift"] + d_lift))
    x = np.clip(x * 2.0 ** exposure, 0, 1)
    x = np.clip(x + (lift / 100.0) * 2.0 * x * (1 - x) ** 2, 0, 1)
    x = np.clip(x * adj["brightness"], 0, 1)
    y = x.mean(axis=2, keepdims=True)
    x = np.clip(y + (x - y) * adj["saturation"], 0, 1)
    resp["adjustments_applied"] = {"ignored": [], "guards": [], "post": {"shadow_lift": lift / 100.0},
                                   "highlight_recovery": {"value": adj["highlight_recovery"], "exposure_delta": d_ev,
                                                          "shadow_lift_delta": d_lift, "effective_exposure": exposure,
                                                          "effective_shadow_lift": lift,
                                                          "shadow_lift_clamped": lift != adj["shadow_lift"] + d_lift}}
resp["applied"] = {"exposure": round(exposure, 6), "lens_correction_enabled": True, "profile": req["settings"]["profile"]}
h, w = x.shape[:2]
resp.update({"width": w, "height": h})
if req.get("format") == "tiff16":
    cv2.imwrite(req["output"], cv2.cvtColor((x * 65535).round().astype(np.uint16), cv2.COLOR_RGB2BGR))
else:
    Image.fromarray((x * 255).round().astype(np.uint8)).save(req["output"], "JPEG", quality=95)
print(json.dumps(resp))
"""


class PresetsModeTests(unittest.TestCase):
    def setUp(self):
        self.presets = ce.load_ci_presets()

    def test_ci_presets_have_hr_only_on_rescue(self):
        self.assertEqual(len(self.presets), 11)
        with_hr = {p["name"] for p in self.presets if p["adjustments"]["highlight_recovery"] > 0}
        self.assertEqual(with_hr, set(ce.RESCUE_PRESETS))

    def test_hr_delta_matches_cli_nodes(self):
        self.assertEqual(ce.hr_delta(0), (0.0, 0.0))
        self.assertEqual(ce.hr_delta(60), (-0.7, 40.0))
        self.assertEqual(ce.hr_delta(90), (-1.0, 55.0))
        self.assertEqual(ce.hr_delta(100), (-1.1, 60.0))
        ev, lift = ce.hr_delta(30)
        self.assertAlmostEqual(ev, -0.35)
        self.assertAlmostEqual(lift, 20.0)
        ev, lift = ce.hr_delta(70)
        self.assertAlmostEqual(ev, -0.8)
        self.assertAlmostEqual(lift, 45.0)

    def test_effective_values_of_rescue_presets(self):
        eff = {p["name"]: ce.effective_exposure_lift(p["adjustments"]) for p in self.presets}
        self.assertEqual(eff["highlight_rescue"], (-1.0, 55.0))  # = C1' 強め（設計書 §11-4）
        self.assertEqual(eff["backlit_rescue"], (-0.7, 70.0))
        self.assertEqual(eff["sunset_glow"], (-0.7, 44.0))  # run2 で lift 8 → 4
        self.assertEqual(eff["neutral_recovery"], (0.0, 15.0))
        self.assertEqual(ce.effective_exposure_lift({"exposure": 0.0, "shadow_lift": 50.0, "highlight_recovery": 90.0}), (-1.0, 100.0))

    def test_variant_specs(self):
        specs = ce.preset_variant_specs(self.presets)
        self.assertIsNone(specs["d_base"]["adjustments"])
        for preset in self.presets:
            self.assertEqual(specs[preset["name"]]["adjustments"], preset["adjustments"])
        for name in ce.RESCUE_PRESETS:
            hr0 = specs[f"{name}_hr0"]["adjustments"]
            self.assertEqual(hr0["highlight_recovery"], 0.0)
            self.assertEqual({k: v for k, v in hr0.items() if k != "highlight_recovery"},
                             {k: v for k, v in specs[name]["adjustments"].items() if k != "highlight_recovery"})
        self.assertNotIn("neutral_recovery_hr0", specs)
        self.assertEqual(specs["neutral_recovery_hr30"]["adjustments"]["highlight_recovery"], 30.0)
        self.assertEqual(specs["sunset_glow_hr70"]["adjustments"]["highlight_recovery"], 70.0)
        self.assertEqual(specs["highlight_rescue_hr100"]["adjustments"]["highlight_recovery"], 100.0)
        self.assertEqual(specs["landscape_clear_weak"]["adjustments"]["contrast"], 1.06)
        full = ce.preset_variant_specs(self.presets, "full")
        self.assertEqual(set(full), {"d_base", *ce.RESCUE_PRESETS, *(f"{n}_hr0" for n in ce.RESCUE_PRESETS)})

    def test_every_planned_request_satisfies_frozen_contract(self):
        for spec in ce.preset_variant_specs(self.presets).values():
            if spec["adjustments"] is None:
                continue
            req = ce.coreimage_request(Path("/x/a.ARW"), Path("/o/a.jpg"), "preview", True, profile="apple_default",
                                       adjustments=spec["adjustments"], max_edge=2400)
            self.assertEqual(ce.validate_adjustments_request(req), [], spec["label"])
        for _name, via_hr, direct in ce.equivalence_requests():
            for adj in (via_hr, direct):
                req = ce.coreimage_request(Path("/x/a.ARW"), Path("/o/a.tif"), "preview", True, profile="apple_default",
                                           adjustments=adj, output_format="tiff16", output_color_space="srgb")
                self.assertEqual(ce.validate_adjustments_request(req), [])

    def test_equivalence_pairs_match_mapping(self):
        for _name, via_hr, direct in ce.equivalence_requests():
            self.assertEqual(ce.effective_exposure_lift(via_hr), ce.effective_exposure_lift(direct))

    def test_presets_cli_gate(self):
        ok_caps = {"adjustments": {"supported": True, "ignored": []}}
        self.assertIsNone(ce.presets_cli_problem({"cli_version": "0.6.0"}, ok_caps))
        self.assertIn("0.6.0", ce.presets_cli_problem({"cli_version": "0.5.0"}, ok_caps))
        old_caps = {"adjustments": {"supported": True, "ignored": ["highlight_recovery"]}}
        self.assertIn("ignored", ce.presets_cli_problem({"cli_version": "0.6.0"}, old_caps))

    def test_highlight_recovery_summary(self):
        payload = {"adjustments_applied": {"highlight_recovery": {
            "value": 90.0, "exposure_delta": -1.0, "shadow_lift_delta": 55.0, "effective_exposure": -1.0,
            "effective_shadow_lift": 55.0, "shadow_lift_clamped": False}}}
        flat = ce.highlight_recovery_summary(payload)
        self.assertEqual((flat["ci_hr_effective_exposure"], flat["ci_hr_effective_shadow_lift"]), (-1.0, 55.0))
        self.assertEqual(ce.highlight_recovery_summary({"adjustments_applied": {}}), {})
        self.assertEqual(ce.adjustments_applied_summary({"adjustments_applied": payload["adjustments_applied"]})["ci_hr_value"], 90.0)

    def test_equivalence_compare(self):
        a = np.full((4, 4, 3), 0.5, np.float32)
        self.assertTrue(ce.equivalence_compare(a, a.copy(), {"exposure": -0.7}, {"exposure": -0.7})["pass"])
        b = a.copy()
        b[0, 0, 0] = 0.5 + 1 / 65535
        self.assertFalse(ce.equivalence_compare(a, b, {}, {})["pass"])
        self.assertFalse(ce.equivalence_compare(a, a, {"exposure": -0.7}, {"exposure": -1.0})["pass"])

    def _metrics(self, clip_p=0.02, clip_hr0=0.05, clip_base=0.06, mid=0.45, mid_hr0=0.46, mid_base=0.46, fg=0.05, fg_hr0=0.06):
        base = {"clip8_any": clip_base, "mid_p50": mid_base, "fg_p01": 0.06, "flat_noise": 2.0, "rb_ln_neutral": 0.0,
                "mean_saturation": 0.2, "std_luma": 0.2, "mean_luma": 0.4, "dark_mean_luma": 0.1, "edge_width_px": 2.0}
        out = {"d_base": base}
        for name in ce.RESCUE_PRESETS:
            out[name] = {**base, "clip8_any": clip_p, "mid_p50": mid, "fg_p01": fg, "magenta_rate": 0.0,
                         "rb_ln_neutral": 0.05, "mean_saturation": 0.25}
            out[f"{name}_hr0"] = {**base, "clip8_any": clip_hr0, "mid_p50": mid_hr0, "fg_p01": fg_hr0}
        return out

    def test_capture_checks_pass_and_fail(self):
        checks = ce.preset_capture_checks("DSC04473", "preview", self._metrics(), 100, self.presets)
        by = {(c["condition"], c["preset"], c["check"]): c for c in checks}
        for name in ce.RESCUE_PRESETS:
            for cond, check in ((1, "hr_clip8_lt_hr0"), (2, "hr_mid_p50_vs_hr0"), (3, "hr_fg_p01_vs_hr0"),
                                (4, "magenta_rate"), (5, "dbase_clip8_and_mid")):
                self.assertTrue(by[(cond, name, check)]["pass"], (cond, name))
        self.assertTrue(by[(6, "sunset_glow", "direction_rb_ln_neutral")]["pass"])
        # ISO 100 ではノイズの条件は n/a
        self.assertTrue(all(c["pass"] is None for c in checks if c["condition"] == 7))
        bad = ce.preset_capture_checks("DSC04473", "preview", self._metrics(clip_p=0.06, mid=0.40, fg=0.0), 3200, self.presets)
        byb = {(c["condition"], c["preset"], c["check"]): c for c in bad}
        self.assertFalse(byb[(1, "highlight_rescue", "hr_clip8_lt_hr0")]["pass"])
        self.assertFalse(byb[(2, "highlight_rescue", "hr_mid_p50_vs_hr0")]["pass"])
        self.assertFalse(byb[(3, "highlight_rescue", "hr_fg_p01_vs_hr0")]["pass"])
        # P_hr0 の白飛びが 1% 未満なら hr の寄与は n/a
        na = ce.preset_capture_checks("DSC04473", "preview", self._metrics(clip_hr0=0.005), 100, self.presets)
        self.assertIsNone(next(c for c in na if c["check"] == "hr_clip8_lt_hr0")["pass"])

    def test_dbase_checks_mid_exempt_and_low_clip(self):
        # 中間調が d_base から +0.09: backlit_rescue だけは中間調を問わない（被写体向けの lift）
        checks = ce.preset_capture_checks("DSC04473", "preview", self._metrics(mid=0.55, mid_hr0=0.55), 100, self.presets)
        by = {(c["preset"], c["check"]): c for c in checks if c["condition"] in (5, 6)}
        self.assertTrue(by[("backlit_rescue", "dbase_clip8_and_mid")]["pass"])
        self.assertFalse(by[("highlight_rescue", "dbase_clip8_and_mid")]["pass"])
        # d_base の白飛びが 1% 未満なら条件 5 と条件 6 の clip8 の方向は n/a
        low = ce.preset_capture_checks("DSC04872", "preview", self._metrics(clip_p=0.00001, clip_base=0.00002), 100, self.presets)
        for c in low:
            if c["check"] in ("dbase_clip8_and_mid", "direction_clip8_any"):
                self.assertIsNone(c["pass"], (c["preset"], c["check"]))

    def test_check_summary_rules(self):
        checks = []
        for i, cid in enumerate(ce.PRESET_SKY_IDS):
            ok_mid = i != 0  # 1 カットだけ外れる → 9/10 で合格
            checks.append({"capture_id": cid, "mode": "preview", "condition": 2, "check": "hr_mid_p50_vs_hr0",
                           "preset": "highlight_rescue", "sky_cut": True, "pass": ok_mid})
            checks.append({"capture_id": cid, "mode": "preview", "condition": 1, "check": "hr_clip8_lt_hr0",
                           "preset": "highlight_rescue", "sky_cut": True, "pass": None if i < 3 else (i != 5)})
        checks.append({"capture_id": "DSC07415", "mode": "preview", "condition": 2, "check": "hr_mid_p50_vs_hr0",
                       "preset": "highlight_rescue", "sky_cut": False, "pass": False})  # 空以外は 1〜5 に数えない
        summary = {(c["condition"], c["preset"]): c for c in ce.preset_check_summary(checks)}
        self.assertTrue(summary[(2, "highlight_rescue")]["pass"])
        self.assertEqual(summary[(2, "highlight_rescue")]["decided_n"], 10)
        self.assertFalse(summary[(1, "highlight_rescue")]["pass"])
        self.assertEqual(summary[(1, "highlight_rescue")]["n_a"], 3)
        self.assertIsNone(ce.preset_check_summary([{**checks[1], "pass": None}])[0]["pass"])

    def test_sheet_columns(self):
        cols = ce.presets_sheet_columns("sunset_glow", ["d_base", "sunset_glow", "sunset_glow_hr0", "sunset_glow_hr70",
                                                        "rawpy_sunset_glow", "nightscape"])
        self.assertEqual(cols, ["d_base", "sunset_glow", "sunset_glow_hr0", "sunset_glow_hr70", "rawpy_sunset_glow"])
        self.assertEqual(ce.presets_sheet_columns("nightscape", ["d_base", "nightscape"]), ["d_base", "nightscape"])

    def test_owner_groups_reference_known_presets_and_cuts(self):
        names = {p["name"] for p in self.presets}
        cuts = set(ce.PRESET_SKY_IDS) | set(ce.PRESET_SCENE_IDS)
        for _group, pairs, _point in ce.PRESET_OWNER_GROUPS:
            for cid, preset in pairs:
                self.assertIn(preset, names)
                self.assertIn(cid, cuts)
        self.assertTrue(set(ce.PRESET_DIRECTIONS) <= names)


class PresetsMockCliIntegrationTests(unittest.TestCase):
    """偽 CLI 0.6.0 で --presets を 1 カット preview + full まで通す（rawpy の参考列は省く）。"""

    def setUp(self):
        import os
        import tempfile

        from PIL import Image

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src = root / "src"
        self.src.mkdir()
        Image.fromarray(_diag_scene(1200, 800)).save(self.src / "DSC04473.JPG", "JPEG", quality=95)
        (self.src / "DSC04473.ARW").write_bytes(b"not a raw file")
        self.cli = root / "mock-cli"
        self.cli.write_text(MOCK_CLI_060.replace("{python}", sys.executable), encoding="utf-8")
        os.chmod(self.cli, 0o755)
        self.out = root / "out"
        self.out.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_presets_flow(self):
        import csv

        ctx = ce.RunContext(source_root=self.src, out=self.out, cli=self.cli, max_edge=600)
        features = {"DSC04473": {"capture_id": "DSC04473", "iso": 100, "orientation": 1}}
        ce.run_presets(ctx, [{"capture_id": "DSC04473", "category": "backlit"}], features, ["DSC04473"], with_rawpy=False)
        summary = json.loads((self.out / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["errors"], [])
        self.assertTrue(all(e["pass"] for e in summary["equivalence"]), summary["equivalence"])
        with (self.out / "metrics.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        by = {(r["mode"], r["variant"]): r for r in rows}
        self.assertIn(("preview", "highlight_rescue_hr100"), by)
        self.assertIn(("full", "backlit_rescue_hr0"), by)
        self.assertNotIn(("full", "nightscape"), by)
        self.assertEqual(float(by[("preview", "highlight_rescue")]["ci_hr_effective_exposure"]), -1.0)
        self.assertEqual(float(by[("preview", "backlit_rescue")]["ci_hr_effective_shadow_lift"]), 70.0)
        self.assertTrue((self.out / "sheets" / "DSC04473_highlight_rescue.jpg").is_file())
        self.assertTrue((self.out / "sheets" / "DSC04473_highlight_rescue_full.jpg").is_file())
        index = (self.out / "index.md").read_text(encoding="utf-8")
        self.assertIn("sheets/DSC04473_highlight_rescue.jpg", index)
        self.assertIn("§2.2", index)
        self.assertTrue((self.out / "tables.md").is_file())
        self.assertNotIn("gps", (self.out / "summary.json").read_text(encoding="utf-8").lower())

    def test_main_dry_run_writes_plan(self):
        ce.main(["--source-root", str(self.src), "--captures", "DSC04473", "--presets", "--dry-run",
                 "--out", str(self.out), "--cli", str(self.cli), "--full"])
        plan = json.loads((self.out / "presets_plan.json").read_text(encoding="utf-8"))
        self.assertEqual([p for p in plan if p["problems"]], [])
        self.assertEqual({p["mode"] for p in plan}, {"preview", "full"})
