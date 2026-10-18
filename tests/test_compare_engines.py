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
