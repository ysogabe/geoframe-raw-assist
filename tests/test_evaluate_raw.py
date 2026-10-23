"""scripts/evaluate_raw.py の集計・サンプリング・輝度計算の検証。

実RAW群には依存しない（評価ロジックの単位テスト）。
"""

import sys
import tempfile
import types
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



def _write_image(path: Path, size=(24, 16), color=(120, 80, 40), exif=None) -> None:
    from PIL import Image

    image = Image.new("RGB", size, color)
    image.save(path, **({"exif": exif} if exif is not None else {}))


class DevelopedImagesTests(unittest.TestCase):
    """--developed の 1 LABEL に同じ撮影 ID を重複して混ぜない（平均を黙って偏らせない）。"""

    def test_distinct_captures_with_any_suffix_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("DSC00001_preview_lens.jpg", "DSC00002.tif", "DSC00003-final.PNG", "notes.txt", "IMG_0001.jpg"):
                _write_image(root / name) if not name.endswith(".txt") else (root / name).write_text("x")
            found = evaluate_raw.developed_images(root)
            self.assertEqual([cid for cid, _ in found], ["DSC00001", "DSC00002", "DSC00003"])
            self.assertEqual(found[0][1].name, "DSC00001_preview_lens.jpg")

    def test_capture_stem_boundary_rejects_longer_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_image(root / "DSC07402.jpg")
            _write_image(root / "DSC074020.jpg")  # 6 桁: DSC07402 の別バリアントとして数えない
            self.assertEqual([(cid, p.name) for cid, p in evaluate_raw.developed_images(root)],
                             [("DSC07402", "DSC07402.jpg")])
            self.assertIsNone(evaluate_raw.DEVELOPED_NAME.match("DSC074020.jpg"))

    def test_multiple_variants_of_one_capture_fail_with_guidance(self):
        """compare_engines の coreimage/preview そのもの（同じ撮影の複数バリアント）は失敗にする。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("DSC00001_preview.jpg", "DSC00001_preview_lens.jpg", "DSC00001_preview_frozen01.jpg",
                         "DSC00002_preview.jpg"):
                _write_image(root / name)
            with self.assertRaises(SystemExit) as caught:
                evaluate_raw.developed_images(root)
            message = str(caught.exception)
            self.assertIn("DSC00001", message)
            self.assertIn("DSC00001_preview_lens.jpg", message)
            self.assertNotIn("DSC00002", message)
            self.assertIn("1 バリアント", message)

    def test_main_rejects_ambiguous_directory_without_writing_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, developed, out = root / "raw", root / "dev", root / "out"
            source.mkdir(); developed.mkdir()
            _write_image(source / "DSC00001.JPG")
            _write_image(developed / "DSC00001_preview.jpg")
            _write_image(developed / "DSC00001_preview_lens.jpg")
            with self.assertRaises(SystemExit) as caught:
                evaluate_raw.main(["--source-root", str(source), "--output-root", str(out), "--developed", f"ci={developed}"])
            self.assertIn("複数", str(caught.exception))
            self.assertEqual(list(out.rglob("evaluation.json")), [])

    def test_duplicate_label_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b = Path(directory) / "a", Path(directory) / "b"
            a.mkdir(); b.mkdir()
            self.assertEqual([label for label, _ in evaluate_raw.parse_developed([f"ci_a={a}", f"ci_b={b}"])],
                             ["ci_a", "ci_b"])
            with self.assertRaises(SystemExit) as caught:
                evaluate_raw.parse_developed([f"ci={a}", f"ci={b}"])
            self.assertIn("重複", str(caught.exception))
            self.assertIn("別の LABEL", str(caught.exception))


class LegacyCameraReadingTests(unittest.TestCase):
    """旧経路もカメラ JPEG を既定経路と同じく解決する（拡張子の大小を問わない・EXIF の向きを反映）。"""

    def test_lowercase_extension_and_orientation_match_standalone_path(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raw"
            source.mkdir()
            (source / "DSC00001.arw").write_bytes(b"raw")
            exif = Image.Exif()
            exif[0x0112] = 6  # 90° 回転: 24x16 のカメラ JPEG は正立で 16x24
            rng = np.random.default_rng(7)
            Image.fromarray(rng.integers(0, 256, size=(16, 24, 3), dtype=np.uint8)).save(source / "DSC00001.jpg", exif=exif)

            def develop_raw(capture_id, preset, *, preview, force):
                name = f"{capture_id}-{preset}.jpg"
                _write_image(engine.PREVIEW_ROOT / name, size=(16, 24), color=(90, 100, 110))
                return {"filename": name, "elapsed_seconds": 0.5}

            config = types.SimpleNamespace(source_root=lambda: None)
            engine = types.SimpleNamespace(develop_raw=develop_raw, OUTPUT_ROOT=None, PREVIEW_ROOT=None, ENGINE_VERSION="test")
            modules = {"local_raw.config": config, "local_raw.engine": engine}
            args = types.SimpleNamespace(ids=None, limit=0, presets=["std"], preview=True)
            with mock.patch.object(evaluate_raw.legacy, "import_module", side_effect=lambda name, feature: modules[name]):
                self.assertEqual(evaluate_raw._paired_ids(source), ["DSC00001"])
                rows, ids, meta = evaluate_raw.run_legacy(args, source, root / "run")
            self.assertEqual(ids, ["DSC00001"])
            self.assertNotIn("error", rows[0])
            output = evaluate_raw._read_rgb(Path(rows[0]["output"]))
            expected = evaluate_raw._metrics(output, evaluate_raw._read_camera_rgb(source / "DSC00001.jpg"))
            for key, value in expected.items():
                self.assertAlmostEqual(rows[0][key], value, places=6, msg=key)
            self.assertEqual(evaluate_raw._read_camera_rgb(source / "DSC00001.jpg").shape[:2], (24, 16))


if __name__ == "__main__":
    unittest.main()
