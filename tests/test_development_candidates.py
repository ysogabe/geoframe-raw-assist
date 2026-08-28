"""Synthetic tests for the JPG-proxy development candidate optimizer."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

import numpy as np
from PIL import Image

from local_raw import config
from local_raw import development_candidates as candidates
from local_raw import server


class CandidateOptimizerTests(unittest.TestCase):
    def make_jpg(self, root: Path, *, stars: bool) -> None:
        rng = np.random.default_rng(12)
        if stars:
            image = np.full((180, 260, 3), 10, dtype=np.uint8)
            for y, x, value in ((26, 34, (255, 245, 230)), (72, 142, (240, 250, 255)), (128, 220, (255, 224, 190)), (150, 90, (230, 240, 255))):
                image[y, x] = value
        else:
            image = np.uint8(np.clip(rng.normal(145, 17, (180, 260, 3)), 0, 255))
        Image.fromarray(image, mode="RGB").save(root / "DSC00001.JPG", format="JPEG", quality=92)

    def generate(self, root: Path, *, stars: bool, seed: int = 7, budget: int = 32) -> dict:
        self.make_jpg(root, stars=stars)
        with mock.patch.object(config, "source_root", return_value=root):
            return candidates.generate_development_candidates(
                "DSC00001", "neutral_recovery", seed=seed, budget=budget
            )

    def test_same_input_and_seed_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.generate(root, stars=False)
            second = self.generate(root, stars=False)
        self.assertEqual(first, second)

    def test_all_numeric_candidate_values_stay_inside_existing_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.generate(Path(directory), stars=True)
        for candidate in result["candidates"]:
            adjustments = candidate["adjustments"]
            for key, (minimum, maximum) in candidates.ADJUSTMENT_RANGES.items():
                self.assertGreaterEqual(adjustments[key], minimum, (candidate["id"], key))
                self.assertLessEqual(adjustments[key], maximum, (candidate["id"], key))
            self.assertIn(adjustments["noise_reduction"], {"off", "light", "full"})

    def test_pareto_front_keeps_non_dominated_points(self):
        points = np.asarray([[1.0, 1.0], [0.5, 1.2], [1.2, 0.5], [2.0, 2.0]])
        self.assertEqual(candidates.pareto_front(points), [0, 1, 2])

    def test_star_style_is_gated_by_dark_small_points(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.generate(Path(directory), stars=False)
        self.assertFalse(result["star_scene"]["is_star_scene"])
        self.assertNotIn("stars", [item["id"] for item in result["candidates"]])

        with tempfile.TemporaryDirectory() as directory:
            result = self.generate(Path(directory), stars=True)
        self.assertTrue(result["star_scene"]["is_star_scene"])
        self.assertIn("stars", [item["id"] for item in result["candidates"]])

    def test_natural_is_present_and_other_styles_are_materially_different(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.generate(Path(directory), stars=True)
        self.assertEqual(result["candidates"][0]["id"], "natural")
        natural = result["candidates"][0]["adjustments"]
        different = [
            item for item in result["candidates"][1:]
            if item["adjustments"] != natural
        ]
        self.assertTrue(different)

    def test_missing_jpg_is_a_distinct_not_found_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(config, "source_root", return_value=Path(directory)):
                with self.assertRaises(candidates.DevelopmentCandidateNotFound):
                    candidates.generate_development_candidates("DSC00001")


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
            side_effect=candidates.DevelopmentCandidateNotFound("元JPGが見つかりません。"),
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

