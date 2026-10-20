"""scripts/compare_engines.py の純粋関数（選定・指標・ID 検証・time -l 解析）の検証。

実 RAW・Core Image CLI・/Volumes には依存しない。
"""

import math
import sys
import unittest
from pathlib import Path

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
                            "impulse_vs_full": 0, "ci_adj_guards": "", "ci_adj_ignored": "highlight_recovery"}}
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
        self.assertTrue(checks["highlight_recovery_ignored"]["pass"])
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
