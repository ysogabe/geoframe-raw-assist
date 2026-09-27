"""scripts/evaluate_raw.py の集計・サンプリング・輝度計算の検証。

実RAW群には依存しない（評価ロジックの単位テスト）。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

# ``python -m unittest tests.test_evaluate_raw`` から scripts/ 配下のモジュールを
# import できるようにする。evaluate_raw 自身も import 時に PROJECT_ROOT を
# sys.path へ足すため、local_raw の解決はここで担保される。
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import evaluate_raw  # noqa: E402


def _ids(count: int) -> list[str]:
    return [f"DSC{i:05d}" for i in range(count)]


class SummarizeTests(unittest.TestCase):
    def test_numeric_keys_survive_when_first_row_is_error(self):
        """rows[0] がエラー行でも、他の正常行の数値列で summary を集計する。"""
        rows = [
            {"capture_id": "DSC00001", "preset": "std", "error": "RawDevelopmentError: boom"},
            {"capture_id": "DSC00002", "preset": "std", "mean_luma": 0.5, "p50_luma": 0.4,
             "elapsed_seconds": 1.2},
            {"capture_id": "DSC00003", "preset": "std", "mean_luma": 0.7, "p50_luma": 0.6,
             "elapsed_seconds": 1.1},
        ]
        summary = evaluate_raw._summarize(rows)
        self.assertEqual(len(summary), 1)
        item = summary[0]
        self.assertEqual(item["preset"], "std")
        self.assertEqual(item["images"], 2)
        self.assertAlmostEqual(item["mean_mean_luma"], 0.6)
        self.assertAlmostEqual(item["median_mean_luma"], 0.6)
        self.assertAlmostEqual(item["mean_p50_luma"], 0.5)
        self.assertAlmostEqual(item["median_p50_luma"], 0.5)

    def test_numeric_keys_are_union_of_all_valid_rows(self):
        """先頭行に無い数値キーも、後続の正常行にあれば集計列に含める。"""
        rows = [
            {"capture_id": "DSC00001", "preset": "std", "mean_luma": 0.5, "elapsed_seconds": 1.0},
            {"capture_id": "DSC00002", "preset": "std", "p50_luma": 0.4, "elapsed_seconds": 1.1},
        ]
        summary = evaluate_raw._summarize(rows)
        item = summary[0]
        self.assertIn("mean_mean_luma", item)
        self.assertIn("median_mean_luma", item)
        self.assertIn("mean_p50_luma", item)
        self.assertIn("median_p50_luma", item)

    def test_error_rows_excluded_from_images_and_values(self):
        """エラー行は images 数にも集計値にも混ぜない。"""
        rows = [
            {"capture_id": "DSC00001", "preset": "std", "error": "E: boom"},
            {"capture_id": "DSC00002", "preset": "std", "mean_luma": 0.5, "elapsed_seconds": 1.0},
            {"capture_id": "DSC00003", "preset": "other", "mean_luma": 0.9, "elapsed_seconds": 1.0},
        ]
        summary = evaluate_raw._summarize(rows)
        by_preset = {item["preset"]: item for item in summary}
        self.assertEqual(by_preset["std"]["images"], 1)
        self.assertAlmostEqual(by_preset["std"]["mean_mean_luma"], 0.5)
        self.assertAlmostEqual(by_preset["other"]["mean_mean_luma"], 0.9)

    def test_preset_with_only_error_rows_gets_no_summary_item(self):
        """正常行が無いプリセットは summary に現れない。"""
        rows = [
            {"capture_id": "DSC00001", "preset": "broken", "error": "E: boom"},
            {"capture_id": "DSC00002", "preset": "broken", "error": "E: boom"},
        ]
        self.assertEqual(evaluate_raw._summarize(rows), [])

    def test_empty_rows(self):
        self.assertEqual(evaluate_raw._summarize([]), [])

    def test_elapsed_seconds_is_not_aggregated(self):
        rows = [
            {"capture_id": "DSC00001", "preset": "std", "elapsed_seconds": 1.0},
            {"capture_id": "DSC00002", "preset": "std", "elapsed_seconds": 2.0},
        ]
        item = evaluate_raw._summarize(rows)[0]
        self.assertNotIn("mean_elapsed_seconds", item)
        self.assertNotIn("median_elapsed_seconds", item)


class SelectIdsTests(unittest.TestCase):
    def test_rounds_instead_of_truncating(self):
        """np.linspace の理想位置を四捨五入して使う（floor の前方偏りを解消）。"""
        available = _ids(6)
        selected = evaluate_raw._select_ids(available, None, 4)
        # np.linspace(0, 5, 4) -> [0.0, 1.667, 3.333, 5.0]
        # floor なら [0, 1, 3, 5]、四捨五入なら [0, 2, 3, 5]。
        self.assertEqual(selected, [available[0], available[2], available[3], available[5]])

    def test_matches_rounded_ideal_positions(self):
        """返るインデックスが round(np.linspace(...)) と一致する。"""
        available = _ids(10)
        for limit in (1, 2, 3, 4, 5, 7, 8, 9):
            selected = evaluate_raw._select_ids(available, None, limit)
            ideal = np.linspace(0, len(available) - 1, num=limit)
            expected = list(dict.fromkeys(available[int(round(float(p)))] for p in ideal))
            self.assertEqual(selected, expected, f"limit={limit}")

    def test_endpoints_always_included(self):
        available = _ids(6)
        selected = evaluate_raw._select_ids(available, None, 4)
        self.assertEqual(selected[0], available[0])
        self.assertEqual(selected[-1], available[-1])

    def test_requested_ids_returned_verbatim(self):
        available = _ids(6)
        requested = ["DSC00003", "DSC00001"]
        self.assertEqual(evaluate_raw._select_ids(available, requested, 4), requested)
        with self.assertRaises(SystemExit):
            evaluate_raw._select_ids(available, ["DSC99999"], 4)

    def test_limit_bounds_return_all(self):
        available = _ids(6)
        self.assertEqual(evaluate_raw._select_ids(available, None, 0), available)
        self.assertEqual(evaluate_raw._select_ids(available, None, 6), available)
        self.assertEqual(evaluate_raw._select_ids(available, None, 10), available)


class Rec709Tests(unittest.TestCase):
    def test_constant_defined_once_with_standard_coefficients(self):
        self.assertEqual(evaluate_raw.REC709_LUMA, (0.2126, 0.7152, 0.0722))
        source = Path(evaluate_raw.__file__).resolve().read_text(encoding="utf-8")
        for literal in ("0.2126", "0.7152", "0.0722"):
            self.assertEqual(
                source.count(literal),
                1,
                f"{literal} は定数定義の1箇所のみに出現すること（_metrics 内の重複がないこと）",
            )

    def test_metrics_references_module_constant(self):
        """REC709_LUMA を差し替えると _metrics の両 luma（出力・参照JPEG）に反映される。"""
        rng = np.random.default_rng(12345)
        output = rng.integers(0, 256, size=(6, 64, 3), dtype=np.uint8)
        camera_jpg = rng.integers(0, 256, size=(12, 128, 3), dtype=np.uint8)

        with mock.patch.object(evaluate_raw, "REC709_LUMA", (1.0, 0.0, 0.0)):
            metrics = evaluate_raw._metrics(output, camera_jpg)

        rgb = output.astype(np.float32) / 255.0
        expected_luma = rgb[:, :, 0]
        self.assertAlmostEqual(metrics["mean_luma"], float(expected_luma.mean()), places=6)

        reference = cv2.resize(
            camera_jpg,
            (output.shape[1], output.shape[0]),
            interpolation=cv2.INTER_AREA,
        ).astype(np.float32) / 255.0
        expected_reference_luma = reference[:, :, 0]
        expected_mae = float(np.abs(expected_luma - expected_reference_luma).mean())
        self.assertAlmostEqual(metrics["camera_jpg_luma_mae"], expected_mae, places=6)


if __name__ == "__main__":
    unittest.main()
