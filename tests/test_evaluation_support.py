"""scripts/evaluation/（評価用の独立サポート）の検証（#57 / #90 / #117）。

純関数の写しは凍結 JSON（#107 の metrics.json / candidate_series.json と #86 の pso-selection.json）と
完全一致することで旧 Python 実装との一致を担保する。アプリ本体（local_raw）・rawpy は使わない。
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from evaluation import candidate_metrics as cm  # noqa: E402
from evaluation import frozen, legacy, presets, sources  # noqa: E402

BASELINE = REPO / "tests" / "fixtures" / "baseline"


class CandidateMetricsParityTest(unittest.TestCase):
    def test_metrics_and_star_measurements_match_frozen_json(self):
        fixture = json.loads((BASELINE / "metrics.json").read_text())
        for record in fixture["images"]:
            with self.subTest(input=record["input"]):
                with Image.open(BASELINE / record["input"]) as image:
                    baseline = cm._normalise_rgb(image)
                transformed = np.clip(baseline * np.float32(1.2), 0, 1)
                self.assertEqual(cm._star_measurements(baseline, record["exif"]), record["star_measurements"])
                self.assertEqual(cm._star_measurements(baseline, record["exif"], evaluate_gate=False), record["star_ungated"])
                for gate in (False, True):
                    self.assertEqual(cm._image_metrics(baseline, transformed, record["exif"], gate), record["metrics"][str(gate)])
        for record in fixture["pareto"]:
            self.assertEqual(cm.pareto_front(record["points"]), record["indices"])

    def test_selection_matches_frozen_pso_selection(self):
        selection = json.loads((REPO / "tests" / "fixtures" / "pso-selection.json").read_text())
        series = json.loads((BASELINE / "candidate_series.json").read_text())
        base = series["expected"]["candidates"][0]["metrics"]
        self.assertEqual(len(selection["series"]), 5 * len(series["evaluations"]))
        for row in selection["series"]:
            metrics = series["evaluations"][row["evaluation"]]["metrics"]
            style = row["style"]
            self.assertEqual(cm._guard_failure(style, metrics, base), row["guard"])
            self.assertEqual(cm._score(metrics, style, base), row["score"])
            self.assertEqual(cm._reasons(style, metrics, base, True), row["reasons"])
        for row in selection["edges"]:
            self.assertEqual(cm._guard_failure(row["style"], row["metrics"], row["base"]), row["guard"])
            self.assertEqual(cm._score(row["metrics"], row["style"], row["base"]), row["score"])
            self.assertEqual(cm._reasons(row["style"], row["metrics"], row["base"], True), row["reasons"])

    def test_frozen_candidate_scores_are_reproduced(self):
        records = json.loads((BASELINE / "candidates.json").read_text())["records"]
        self.assertEqual(len(records), 11)
        for record in records:
            candidates = record["result"]["candidates"]
            base = candidates[0]["metrics"]
            star = record["result"]["star_scene"]["is_star_scene"]
            for candidate in candidates:
                with self.subTest(case=record["case"]["capture_id"], style=candidate["id"]):
                    self.assertEqual(cm._score(candidate["metrics"], candidate["id"], base), candidate["score"])
                    self.assertEqual(cm._reasons(candidate["id"], candidate["metrics"], base, star), candidate["reasons"])
                    self.assertIsNone(cm._guard_failure(candidate["id"], candidate["metrics"], base))
        self.assertEqual([cm.highlight_category(v) for v in (0.0, 0.1, 74.9, 75.0, 100.0)],
                         ["clip", "blend", "blend", "reconstruct", "reconstruct"])

    def test_cv2_fallback_counts_the_same_components(self):
        mask = np.zeros((20, 20), dtype=bool)
        mask[2:4, 2:4] = True
        mask[10, 10:13] = True
        with_cv2 = sorted(cm._connected_bright_components(mask))
        original = cm.cv2
        try:
            cm.cv2 = None
            without = sorted(cm._connected_bright_components(mask))
        finally:
            cm.cv2 = original
        self.assertEqual(with_cv2, without)


class SourcesTest(unittest.TestCase):
    def test_pairs_and_case_insensitive_extensions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("DSC00001.arw", "DSC00001.JPG", "DSC00002.ARW", "DSC00003.jpg", "IMG_0001.JPG", "DSC1.ARW"):
                (root / name).write_bytes(b"x")
            self.assertEqual(sources.paired_ids(root), ["DSC00001"])
            self.assertEqual(sources.find_capture_file(root, "DSC00001", "ARW"), root / "DSC00001.arw")
            self.assertIsNone(sources.find_capture_file(root, "DSC00002", "JPG"))
            self.assertIsNone(sources.find_capture_file(root, "../DSC00001", "ARW"))

    def test_exif_summary_has_no_gps_and_tolerates_unreadable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "DSC00001.JPG"
            exif = Image.Exif()
            exif[0x0112] = 6
            exif.get_ifd(0x8769)[0x8827] = 3200
            exif.get_ifd(0x8769)[0x829A] = 20.0
            exif.get_ifd(0x8769)[0x829D] = 2.8
            exif.get_ifd(0x8825)[2] = (35.0, 1.0, 2.0)
            Image.new("RGB", (8, 8)).save(path, exif=exif)
            summary = sources.read_capture_exif(path)
            self.assertEqual((summary["orientation"], summary["iso"], summary["exposure_time"], summary["f_number"]),
                             (6, 3200, 20.0, 2.8))
            self.assertFalse(any("gps" in key.lower() or "lat" in key.lower() for key in summary))
            self.assertAlmostEqual(sources.ev100(summary), np.log2(2.8**2 / 20.0) - np.log2(32.0))
            (Path(directory) / "broken.JPG").write_bytes(b"not jpeg")
            self.assertEqual(sources.read_capture_exif(Path(directory) / "broken.JPG"), {})
        self.assertIsNone(sources.ev100({"f_number": 0, "exposure_time": 1, "iso": 100}))


class PresetsTest(unittest.TestCase):
    def test_frozen_tables(self):
        self.assertEqual(presets.DEFAULT_PRESET, presets.PRESET_NAMES[0])
        self.assertEqual(len(presets.PRESET_NAMES), 11)
        self.assertEqual(presets.ADJUSTMENT_KEYS[-1], "noise_reduction")
        self.assertEqual(presets.ADJUSTMENT_RANGES["exposure"], (-2.0, 2.0))
        self.assertEqual(presets.NOISE_REDUCTION_CHOICES, ("off", "light", "full"))
        self.assertEqual(presets.preset_adjustments("nightscape")["noise_reduction"], "full")
        self.assertIn("exposure", presets.preset_adjustments("nightscape", presets.CI_FIXTURE))
        with self.assertRaises(KeyError):
            presets.preset_adjustments("unknown")


class FrozenTest(unittest.TestCase):
    def _manifest(self, root: Path) -> Path:
        rel = "work/baseline/rawpy/preview/DSC00001.jpg"
        (root / rel).parent.mkdir(parents=True)
        Image.new("RGB", (12, 8), (10, 20, 30)).save(root / rel)
        with Image.open(root / rel) as image:
            rgb = np.asarray(image.convert("RGB"))
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps([{
            "capture_id": "DSC00001", "mode": "preview", "path": rel, "dimensions": [12, 8],
            "sha256": hashlib.sha256((root / rel).read_bytes()).hexdigest(),
            "rgb8_sha256": hashlib.sha256(rgb.tobytes()).hexdigest()}]))
        return manifest

    def test_verify_pass_and_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._manifest(root)
            [image] = frozen.verify_manifest(manifest, root)
            self.assertEqual(image.dimensions, (12, 8))
            rows = json.loads(manifest.read_text())
            for broken, needle in ((dict(rows[0], dimensions=[13, 8]), "寸法"),
                                   (dict(rows[0], rgb8_sha256="0" * 64), "RGB 画素"),
                                   (dict(rows[0], sha256="0" * 64), "ファイル sha256"),
                                   (dict(rows[0], path="work/missing.jpg"), "ありません"),
                                   (dict(rows[0], path="../escape.jpg"), "相対パス")):
                manifest.write_text(json.dumps([broken]))
                with self.assertRaisesRegex(frozen.FrozenArtifactError, needle):
                    frozen.verify_manifest(manifest, root)
            manifest.write_text("[]")
            with self.assertRaises(frozen.FrozenArtifactError):
                frozen.verify_manifest(manifest, root)

    def test_committed_rawpy_manifest_is_well_formed(self):
        rows = frozen.load_manifest(BASELINE / "rawpy_images.json")
        self.assertEqual({(r["capture_id"], r["mode"]) for r in rows},
                         {("DSC07402", "preview"), ("DSC07402", "full"), ("DSC07423", "preview"), ("DSC07423", "full")})


class LegacyGateTest(unittest.TestCase):
    def test_require_and_import_errors_explain_the_archive(self):
        with self.assertRaisesRegex(legacy.LegacyUnavailable, "削除前のコミットの checkout"):
            legacy.require("x", False)
        legacy.require("x", True)
        with self.assertRaisesRegex(legacy.LegacyUnavailable, "no_such_legacy_module"):
            legacy.import_module("no_such_legacy_module", "x")


if __name__ == "__main__":
    unittest.main()
