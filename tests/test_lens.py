"""レンズ補正の解決とフォールバックの検証。"""

import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from local_raw import lens
from local_raw.lens import LensResolution, apply_lens_correction, lensfun_info, probe_lens

SONY_EXIF = {
    "make": "SONY",
    "model": "ILCE-6400",
    "lens_model": "10-18mm F2.8 DC DN | Contemporary 023",
    "focal_length": 10.7,
    "f_number": 3.5,
}

lensfunpy_available = lens.lensfunpy is not None


class LensfunInfoTests(unittest.TestCase):
    def test_reports_availability(self):
        info = lensfun_info()
        self.assertIn("available", info)
        self.assertIsInstance(info["available"], bool)

    @unittest.skipUnless(lensfunpy_available, "lensfunpy 未導入")
    def test_reports_db_version_when_available(self):
        info = lensfun_info()
        self.assertTrue(info["available"])
        self.assertRegex(info["db_version"], r"^\d+(\.\d+)*$")


class ProbeFallbackTests(unittest.TestCase):
    """どの失敗経路でも例外を投げず applied=false を返すこと。"""

    @unittest.skipUnless(lensfunpy_available, "lensfunpy 未導入")
    def test_unknown_lens_falls_back(self):
        resolution = probe_lens({**SONY_EXIF, "lens_model": "Nonexistent 999mm F0.5 Fantasy"})
        self.assertFalse(resolution.applicable)
        self.assertEqual(resolution.reason, "lens_not_in_db")
        self.assertEqual(resolution.unapplied()["applied"], False)

    @unittest.skipUnless(lensfunpy_available, "lensfunpy 未導入")
    def test_unknown_camera_falls_back(self):
        resolution = probe_lens({**SONY_EXIF, "make": "Nikon", "model": "NOT-A-CAMERA-XYZ"})
        self.assertFalse(resolution.applicable)
        self.assertEqual(resolution.reason, "camera_not_found")

    @unittest.skipUnless(lensfunpy_available, "lensfunpy 未導入")
    def test_missing_lens_model_falls_back(self):
        resolution = probe_lens({**SONY_EXIF, "lens_model": None})
        self.assertFalse(resolution.applicable)
        self.assertEqual(resolution.reason, "lens_not_in_db")

    @unittest.skipUnless(lensfunpy_available, "lensfunpy 未導入")
    def test_missing_focal_length_falls_back(self):
        resolution = probe_lens({**SONY_EXIF, "focal_length": None})
        self.assertFalse(resolution.applicable)
        self.assertTrue(resolution.reason.startswith("error:"))

    def test_empty_exif_falls_back(self):
        resolution = probe_lens({})
        self.assertFalse(resolution.applicable)
        self.assertIsNotNone(resolution.reason)

    def test_reports_unavailable_when_lensfunpy_missing(self):
        with mock.patch.object(lens, "lensfunpy", None):
            resolution = probe_lens(SONY_EXIF)
        self.assertFalse(resolution.applicable)
        self.assertEqual(resolution.reason, "lensfunpy_unavailable")

    def test_unapplied_payload_includes_lens_model(self):
        resolution = LensResolution(lens_model="Some Lens", reason="lens_not_in_db")
        payload = resolution.unapplied()
        self.assertEqual(
            payload, {"applied": False, "reason": "lens_not_in_db", "lens_model": "Some Lens"}
        )


class ApplyLensCorrectionTests(unittest.TestCase):
    def test_returns_input_unchanged_when_not_applicable(self):
        rgb = np.zeros((8, 12, 3), dtype=np.uint8)
        result, correction = apply_lens_correction(rgb, LensResolution(reason="lens_not_in_db"))
        self.assertIs(result, rgb)
        self.assertFalse(correction["applied"])
        self.assertEqual(correction["reason"], "lens_not_in_db")

    def test_survives_correction_failure(self):
        rgb = np.zeros((8, 12, 3), dtype=np.uint8)
        broken = LensResolution(applicable=True, lens_model="X", focal_length=10.0, aperture=2.8)
        # camera=None なので Modifier 構築で必ず例外になる
        result, correction = apply_lens_correction(rgb, broken)
        self.assertIs(result, rgb)
        self.assertFalse(correction["applied"])
        self.assertTrue(correction["reason"].startswith("error:"))


@unittest.skipUnless(lensfunpy_available, "lensfunpy 未導入")
class SigmaLensHitTests(unittest.TestCase):
    """local_raw/lensfun_db/mil-sigma.xml が効いていることの確認。"""

    def test_sigma_10_18_resolves_for_ilce_6400(self):
        resolution = probe_lens(SONY_EXIF)
        if not resolution.applicable:
            self.skipTest(f"追加DB未導入のためスキップ: {resolution.reason}")
        self.assertIn("10-18mm", resolution.lens_name)
        self.assertIn("Sigma", resolution.lens_name)
        self.assertAlmostEqual(resolution.focal_length, 10.7, places=2)

    def test_bundled_sony_lens_resolves(self):
        resolution = probe_lens(
            {**SONY_EXIF, "lens_model": "E 55-210mm F4.5-6.3 OSS", "focal_length": 55.0, "f_number": 4.5}
        )
        self.assertTrue(resolution.applicable, resolution.reason)
        self.assertIn("55-210", resolution.lens_name)

    def test_correction_applies_to_real_array(self):
        resolution = probe_lens(SONY_EXIF)
        if not resolution.applicable:
            self.skipTest(f"追加DB未導入のためスキップ: {resolution.reason}")
        rgb = np.full((400, 600, 3), 128, dtype=np.uint8)
        result, correction = apply_lens_correction(rgb, resolution)
        self.assertTrue(correction["applied"], correction)
        self.assertEqual(result.shape, rgb.shape)
        self.assertEqual(result.dtype, np.uint8)
        # このレンズは distortion 較正のみ（vignetting データは無い）
        self.assertIn("distortion", correction["flags"])


class DatabaseRobustnessTests(unittest.TestCase):
    """DB ディレクトリの状態に関わらずロードが成功すること。"""

    def _load_with_dir(self, directory: Path):
        lens._database.cache_clear()
        try:
            with mock.patch.object(lens, "LENSFUN_DB_DIR", directory):
                return lens._database()
        finally:
            lens._database.cache_clear()

    @unittest.skipUnless(lensfunpy_available, "lensfunpy 未導入")
    def test_missing_directory_still_loads_bundled_db(self):
        import tempfile

        with tempfile.TemporaryDirectory() as base:
            self.assertIsNotNone(self._load_with_dir(Path(base) / "does-not-exist"))

    @unittest.skipUnless(lensfunpy_available, "lensfunpy 未導入")
    def test_empty_directory_still_loads_bundled_db(self):
        import tempfile

        with tempfile.TemporaryDirectory() as base:
            self.assertIsNotNone(self._load_with_dir(Path(base)))

    @unittest.skipUnless(lensfunpy_available, "lensfunpy 未導入")
    def test_unparsable_xml_is_skipped_instead_of_aborting(self):
        # DB version 2 の XML は lensfun 0.3.4 が読めず、Database(paths=[...]) 全体を
        # 失敗させる。壊れたファイルだけを読み飛ばして同梱DBで動き続けること。
        import tempfile

        with tempfile.TemporaryDirectory() as base:
            directory = Path(base)
            (directory / "broken.xml").write_text(
                '<!DOCTYPE lensdatabase SYSTEM "lensfun-database.dtd">\n'
                '<lensdatabase version="2">\n</lensdatabase>\n',
                encoding="utf-8",
            )
            database = self._load_with_dir(directory)

        self.assertIsNotNone(database)
        self.assertTrue(database.find_cameras("Sony", "ILCE-6400"))


if __name__ == "__main__":
    unittest.main()
