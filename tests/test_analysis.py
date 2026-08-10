"""Analysis pipeline tests. Synthetic data only — no dependency on sample/."""

import unittest

import cv2
import numpy as np

from local_raw import analysis


def record(
    capture_id,
    captured_at,
    *,
    lens="10-18mm F2.8 DC DN | Contemporary 023",
    focal=18.0,
    bias=-0.3,
    iso=100,
):
    return {
        "capture_id": capture_id,
        "captured_at": captured_at,
        "lens_model": lens,
        "focal_length": focal,
        "exposure_bias": bias,
        "iso": iso,
    }


class GroupPhotosTest(unittest.TestCase):
    def test_time_gap_splits_groups(self):
        groups = analysis.group_photos([
            record("DSC00001", "2025-06-07T10:00:00"),
            record("DSC00002", "2025-06-07T10:00:05"),
            # 13s > GROUP_GAP_SECONDS (12s)
            record("DSC00003", "2025-06-07T10:00:18"),
        ])
        self.assertEqual([group["capture_ids"] for group in groups],
                         [["DSC00001", "DSC00002"], ["DSC00003"]])

    def test_gap_exactly_at_threshold_stays_together(self):
        groups = analysis.group_photos([
            record("DSC00001", "2025-06-07T10:00:00"),
            record("DSC00002", "2025-06-07T10:00:12"),
        ])
        self.assertEqual(len(groups), 1)

    def test_lens_change_splits_groups(self):
        groups = analysis.group_photos([
            record("DSC00001", "2025-06-07T10:00:00", lens="10-18mm F2.8 DC DN | Contemporary 023"),
            record("DSC00002", "2025-06-07T10:00:01", lens="E 55-210mm F4.5-6.3 OSS", focal=18.0),
        ])
        self.assertEqual(len(groups), 2)

    def test_focal_ratio_splits_groups(self):
        groups = analysis.group_photos([
            record("DSC00001", "2025-06-07T10:00:00", focal=10.0),
            record("DSC00002", "2025-06-07T10:00:01", focal=13.5),  # ratio 1.35 > 1.3
        ])
        self.assertEqual(len(groups), 2)

    def test_focal_ratio_within_threshold_stays_together(self):
        groups = analysis.group_photos([
            record("DSC00001", "2025-06-07T10:00:00", focal=10.0),
            record("DSC00002", "2025-06-07T10:00:01", focal=13.0),  # ratio 1.3, not > 1.3
        ])
        self.assertEqual(len(groups), 1)

    def test_sorting_is_stable_at_one_second_resolution(self):
        # Same second: filename order decides (SubsecTime does not exist).
        groups = analysis.group_photos([
            record("DSC00009", "2025-06-07T10:00:00"),
            record("DSC00002", "2025-06-07T10:00:00"),
        ])
        self.assertEqual(groups[0]["capture_ids"], ["DSC00002", "DSC00009"])

    def test_burst_detected_from_one_second_gaps(self):
        groups = analysis.group_photos([
            record("DSC00001", "2025-06-07T10:00:00"),
            record("DSC00002", "2025-06-07T10:00:01"),
            record("DSC00003", "2025-06-07T10:00:02"),
            record("DSC00004", "2025-06-07T10:00:03"),
        ])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["kind"], "burst")

    def test_single_photo_group(self):
        groups = analysis.group_photos([record("DSC00001", "2025-06-07T10:00:00")])
        self.assertEqual(groups[0]["kind"], "single")

    def test_similar_when_gaps_are_slow(self):
        groups = analysis.group_photos([
            record("DSC00001", "2025-06-07T10:00:00"),
            record("DSC00002", "2025-06-07T10:00:08"),
        ])
        self.assertEqual(groups[0]["kind"], "similar")


class BracketDetectionTest(unittest.TestCase):
    def test_constant_minus_zero_three_bias_is_not_bracket(self):
        """96.4% of the sample carries a constant -0.3EV. It must never read as a bracket."""
        groups = analysis.group_photos([
            record("DSC00001", "2025-06-07T10:00:00", bias=-0.3),
            record("DSC00002", "2025-06-07T10:00:01", bias=-0.3),
            record("DSC00003", "2025-06-07T10:00:02", bias=-0.3),
        ])
        self.assertEqual(len(groups), 1)
        self.assertNotEqual(groups[0]["kind"], "bracket")
        self.assertEqual(groups[0]["kind"], "burst")

    def test_constant_plus_zero_three_bias_is_not_bracket(self):
        groups = analysis.group_photos([
            record("DSC00001", "2025-06-07T10:00:00", bias=0.3),
            record("DSC00002", "2025-06-07T10:00:01", bias=0.3),
        ])
        self.assertNotEqual(groups[0]["kind"], "bracket")

    def test_varying_bias_spanning_two_ev_is_bracket(self):
        groups = analysis.group_photos([
            record("DSC00001", "2025-06-07T10:00:00", bias=-1.0),
            record("DSC00002", "2025-06-07T10:00:01", bias=0.0),
            record("DSC00003", "2025-06-07T10:00:02", bias=1.0),
        ])
        self.assertEqual(groups[0]["kind"], "bracket")

    def test_bracket_wins_over_burst(self):
        """One-second gaps would say burst; a real bracket takes priority."""
        groups = analysis.group_photos([
            record("DSC00001", "2025-06-07T10:00:00", bias=-1.0),
            record("DSC00002", "2025-06-07T10:00:01", bias=1.0),
        ])
        self.assertEqual(groups[0]["kind"], "bracket")

    def test_distinct_but_narrow_span_is_not_bracket(self):
        # Two distinct values but only 0.3EV apart -> below BRACKET_MIN_SPAN_EV.
        groups = analysis.group_photos([
            record("DSC00001", "2025-06-07T10:00:00", bias=-0.3),
            record("DSC00002", "2025-06-07T10:00:01", bias=0.0),
        ])
        self.assertNotEqual(groups[0]["kind"], "bracket")

    def test_span_exactly_at_threshold_counts(self):
        """-0.3/+0.3 spans exactly 0.6EV; '>= 0.6' includes it by definition."""
        self.assertTrue(analysis._is_bracket([
            {"exposure_bias": -0.3},
            {"exposure_bias": 0.3},
        ]))


class RawRecommendationTest(unittest.TestCase):
    def level(self, kind="similar", **metrics):
        representative = {
            "iso": 100,
            "mean_luma": 120.0,
            "highlight_clip_ratio": 0.0,
            "shadow_clip_ratio": 0.0,
            "exposure_bias": -0.3,
        }
        representative.update(metrics)
        return analysis.raw_recommendation(kind, representative)

    def test_strong_from_night_scene(self):
        result = self.level(iso=3200, mean_luma=40.0)
        self.assertEqual(result["level"], "strong")
        self.assertIn("night_scene", result["reason_codes"])
        self.assertTrue(result["reasons"])

    def test_strong_from_heavy_highlight_clipping(self):
        result = self.level(highlight_clip_ratio=0.05)
        self.assertEqual(result["level"], "strong")
        self.assertIn("highlight_clipping", result["reason_codes"])

    def test_high_iso_alone_is_not_strong(self):
        # ISO high but the scene is bright -> not a night scene.
        self.assertEqual(self.level(iso=3200, mean_luma=140.0)["level"], "recommended")

    def test_recommended_from_highlight(self):
        self.assertEqual(self.level(highlight_clip_ratio=0.02)["level"], "recommended")

    def test_recommended_from_shadow(self):
        result = self.level(shadow_clip_ratio=0.25)
        self.assertEqual(result["level"], "recommended")
        self.assertIn("deep_shadows", result["reason_codes"])

    def test_recommended_from_iso(self):
        result = self.level(iso=800)
        self.assertEqual(result["level"], "recommended")
        self.assertIn("high_iso", result["reason_codes"])

    def test_maybe_from_small_highlight(self):
        self.assertEqual(self.level(highlight_clip_ratio=0.008)["level"], "maybe")

    def test_maybe_from_moderate_shadow(self):
        self.assertEqual(self.level(shadow_clip_ratio=0.12)["level"], "maybe")

    def test_maybe_from_strong_negative_bias(self):
        self.assertEqual(self.level(exposure_bias=-1.0)["level"], "maybe")

    def test_none_for_clean_photo(self):
        result = self.level()
        self.assertEqual(result["level"], "none")
        self.assertEqual(result["reason_codes"], [])
        self.assertTrue(result["reasons"])

    def test_everyday_minus_zero_three_bias_does_not_trigger_maybe(self):
        """The plan's 'ExposureBias != 0' rule would fire on 96% of the sample."""
        self.assertEqual(self.level(exposure_bias=-0.3)["level"], "none")
        self.assertEqual(self.level(exposure_bias=0.3)["level"], "none")

    def test_exposure_bracket_code_only_for_bracket_groups(self):
        self.assertIn("exposure_bracket", self.level(kind="bracket")["reason_codes"])
        self.assertNotIn("exposure_bracket", self.level(kind="burst")["reason_codes"])

    def test_every_level_has_a_label(self):
        for level in ("none", "maybe", "recommended", "strong"):
            self.assertIn(level, analysis.RAW_LEVEL_LABELS)


class ScoringTest(unittest.TestCase):
    def test_sharp_image_scores_above_blurred(self):
        rng = np.random.default_rng(1234)
        sharp = rng.integers(0, 256, size=(120, 160, 3), dtype=np.uint8)
        blurred = cv2.GaussianBlur(sharp, (11, 11), 0)
        self.assertGreater(
            analysis.score_array(sharp)["sharpness_raw"],
            analysis.score_array(blurred)["sharpness_raw"],
        )

    def test_white_patch_is_detected_as_highlight_clipping(self):
        image = np.full((100, 100, 3), 128, dtype=np.uint8)
        image[:10, :, :] = 255  # 10% of the frame blown out
        scored = analysis.score_array(image)
        self.assertAlmostEqual(scored["highlight_clip_ratio"], 0.10, places=3)
        self.assertEqual(scored["shadow_clip_ratio"], 0.0)

    def test_black_patch_is_detected_as_shadow_clipping(self):
        image = np.full((100, 100, 3), 128, dtype=np.uint8)
        image[:20, :, :] = 0
        scored = analysis.score_array(image)
        self.assertAlmostEqual(scored["shadow_clip_ratio"], 0.20, places=3)

    def test_partial_channel_clipping_counts_as_highlight(self):
        # Only the red channel is blown -> "any channel >= 250" must catch it.
        image = np.full((100, 100, 3), 100, dtype=np.uint8)
        image[:50, :, 0] = 255
        self.assertAlmostEqual(analysis.score_array(image)["highlight_clip_ratio"], 0.50, places=3)

    def test_mean_luma_tracks_brightness(self):
        dark = np.full((50, 50, 3), 20, dtype=np.uint8)
        bright = np.full((50, 50, 3), 200, dtype=np.uint8)
        self.assertLess(analysis.score_array(dark)["mean_luma"], analysis.score_array(bright)["mean_luma"])


def scored(capture_id, sharpness, highlight=0.0):
    return {
        "capture_id": capture_id,
        "sharpness_raw": sharpness,
        "highlight_clip_ratio": highlight,
    }


class RecommendTest(unittest.TestCase):
    def test_single_photo_is_confident(self):
        result = analysis.recommend({}, [scored("DSC00001", 100.0)])
        self.assertEqual(result["recommended_capture_id"], "DSC00001")
        self.assertEqual(result["confidence"], 1.0)
        self.assertEqual(result["sharpness_scores"]["DSC00001"], 100.0)

    def test_clear_winner_gets_full_confidence(self):
        result = analysis.recommend({}, [
            scored("DSC00001", 500.0, highlight=0.0),
            scored("DSC00002", 100.0, highlight=0.2),
        ])
        self.assertEqual(result["recommended_capture_id"], "DSC00001")
        self.assertEqual(result["confidence"], 1.0)

    def test_split_winner_lowers_confidence(self):
        # Sharpest and cleanest-highlights are different photos.
        result = analysis.recommend({}, [
            scored("DSC00001", 500.0, highlight=0.30),
            scored("DSC00002", 100.0, highlight=0.01),
        ])
        self.assertEqual(result["recommended_capture_id"], "DSC00001")  # 0.6 beats 0.4
        self.assertLess(result["confidence"], 1.0)
        self.assertGreaterEqual(result["confidence"], analysis.CONFIDENCE_FLOOR)

    def test_identical_photos_hit_the_confidence_floor_and_need_review(self):
        result = analysis.recommend({}, [
            scored("DSC00001", 100.0, highlight=0.05),
            scored("DSC00002", 100.0, highlight=0.05),
        ])
        self.assertEqual(result["confidence"], analysis.CONFIDENCE_FLOOR)
        self.assertLess(result["confidence"], analysis.NEEDS_REVIEW_CONFIDENCE)

    def test_close_call_in_a_three_photo_group_is_flagged_for_review(self):
        """needs_review must be reachable: the sharpest and the cleanest differ by one rank."""
        result = analysis.recommend({}, [
            scored("DSC00001", 300.0, highlight=0.02),   # sharpest, 2nd cleanest
            scored("DSC00002", 200.0, highlight=0.01),   # 2nd sharpest, cleanest
            scored("DSC00003", 100.0, highlight=0.03),
        ])
        self.assertLess(result["confidence"], analysis.NEEDS_REVIEW_CONFIDENCE)

    def test_confidence_never_leaves_its_band(self):
        for sharpness in (100.0, 150.0, 900.0):
            result = analysis.recommend({}, [
                scored("DSC00001", sharpness, highlight=0.02),
                scored("DSC00002", 100.0, highlight=0.03),
            ])
            self.assertGreaterEqual(result["confidence"], analysis.CONFIDENCE_FLOOR)
            self.assertLessEqual(result["confidence"], 1.0)

    def test_sharpness_score_is_normalised_within_the_group(self):
        result = analysis.recommend({}, [
            scored("DSC00001", 100.0),
            scored("DSC00002", 300.0),
            scored("DSC00003", 200.0),
        ])
        self.assertEqual(result["sharpness_scores"]["DSC00002"], 100.0)
        self.assertEqual(result["sharpness_scores"]["DSC00001"], 0.0)
        self.assertEqual(result["sharpness_scores"]["DSC00003"], 50.0)

    def test_identical_sharpness_avoids_divide_by_zero(self):
        result = analysis.recommend({}, [scored("DSC00001", 42.0), scored("DSC00002", 42.0)])
        self.assertEqual(result["sharpness_scores"]["DSC00001"], 100.0)
        self.assertEqual(result["sharpness_scores"]["DSC00002"], 100.0)

    def test_representative_holds_the_highest_overall_score(self):
        result = analysis.recommend({}, [
            scored("DSC00001", 500.0, highlight=0.30),
            scored("DSC00002", 100.0, highlight=0.01),
            scored("DSC00003", 300.0, highlight=0.02),
        ])
        best = result["recommended_capture_id"]
        self.assertEqual(max(result["overall_scores"], key=result["overall_scores"].get), best)

    def test_tie_breaks_on_earliest_capture_id(self):
        result = analysis.recommend({}, [
            scored("DSC00009", 100.0, highlight=0.05),
            scored("DSC00002", 100.0, highlight=0.05),
        ])
        self.assertEqual(result["recommended_capture_id"], "DSC00002")

    def test_reasons_are_present_and_shaped(self):
        result = analysis.recommend({}, [scored("DSC00001", 500.0), scored("DSC00002", 100.0)])
        self.assertTrue(result["recommendation_reasons"])
        for reason in result["recommendation_reasons"]:
            self.assertIn("code", reason)
            self.assertIn("text", reason)


class ScanPairsTest(unittest.TestCase):
    def test_counts_pairs_and_orphans(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("DSC00001.JPG", "DSC00001.ARW", "DSC00002.JPG", "DSC00003.ARW"):
                (root / name).write_bytes(b"x" * 10)
            source, jpg_paths = analysis.scan_pairs(root)
            self.assertEqual(source["pairs"], 1)
            self.assertEqual(source["jpg_only"], 1)
            self.assertEqual(source["arw_only"], 1)
            self.assertEqual(source["total_bytes"], 40)
            self.assertEqual([path.name for path in jpg_paths], ["DSC00001.JPG", "DSC00002.JPG"])

    def test_missing_directory_is_empty_not_an_error(self):
        from pathlib import Path

        source, jpg_paths = analysis.scan_pairs(Path("/nonexistent/geoframe/source"))
        self.assertEqual(source["pairs"], 0)
        self.assertEqual(jpg_paths, [])


class ThumbnailTest(unittest.TestCase):
    def test_thumbnail_is_written_with_long_edge_640(self):
        import tempfile
        from pathlib import Path

        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = Image.new("RGB", (2000, 1000), (120, 60, 30))
            url = analysis.make_thumbnail(image, "DSC00001", root)
            self.assertEqual(url, "/thumbs/DSC00001.webp")
            written = root / "DSC00001.webp"
            self.assertTrue(written.is_file())
            with Image.open(written) as thumb:
                self.assertEqual(max(thumb.size), analysis.THUMB_MAX_EDGE)
                self.assertEqual(thumb.size, (640, 320))
            self.assertEqual(list(root.glob("*.tmp.webp")), [])


if __name__ == "__main__":
    unittest.main()
