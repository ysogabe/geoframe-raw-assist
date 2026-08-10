"""EXIF継承の検証（合成JPEGのみ。実機レイアウトは Wave 3 の目視確認で担保）。"""

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from local_raw.exif import (
    SOFTWARE_TAG,
    SRGB_ICC_BYTES,
    TAG_DATETIME_ORIGINAL,
    TAG_EXIF_IFD,
    TAG_F_NUMBER,
    TAG_FOCAL_LENGTH,
    TAG_ISO,
    TAG_LENS_MODEL,
    TAG_MAKE,
    TAG_MAKERNOTE,
    TAG_MODEL,
    TAG_ORIENTATION,
    TAG_PIXEL_X_DIMENSION,
    TAG_PIXEL_Y_DIMENSION,
    TAG_SOFTWARE,
    build_inherited_exif,
    read_capture_exif,
)

LENS_MODEL = "10-18mm F2.8 DC DN | Contemporary 023"
CAPTURED_AT = "2025:06:09 00:35:11"


def _write_source_jpeg(path: Path) -> None:
    """Orientation=8 / MakerNote 付きの合成 JPEG を作る。"""
    image = Image.new("RGB", (120, 80), (90, 110, 130))
    exif = image.getexif()
    exif[TAG_ORIENTATION] = 8
    exif[TAG_MAKE] = "SONY"
    exif[TAG_MODEL] = "ILCE-6400"

    ifd = exif.get_ifd(TAG_EXIF_IFD)
    ifd[TAG_DATETIME_ORIGINAL] = CAPTURED_AT
    ifd[TAG_ISO] = 3200
    ifd[TAG_LENS_MODEL] = LENS_MODEL
    ifd[TAG_FOCAL_LENGTH] = 10.7
    ifd[TAG_F_NUMBER] = 3.5
    ifd[TAG_PIXEL_X_DIMENSION] = 120
    ifd[TAG_PIXEL_Y_DIMENSION] = 80
    ifd[TAG_MAKERNOTE] = b"SONY DSC \x00\x00\x00fake-makernote-payload"

    image.save(path, "JPEG", exif=exif.tobytes())


class BuildInheritedExifTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.source = Path(cls._directory.name) / "source.JPG"
        _write_source_jpeg(cls.source)

        cls.output = Path(cls._directory.name) / "developed.jpg"
        blob = build_inherited_exif(cls.source, 3000, 2000)
        developed = Image.new("RGB", (3000, 2000), (10, 20, 30))
        developed.save(cls.output, "JPEG", exif=blob, icc_profile=SRGB_ICC_BYTES, quality=92)

        with Image.open(cls.output) as image:
            cls.exif = image.getexif()
            cls.ifd = cls.exif.get_ifd(TAG_EXIF_IFD)
            cls.info = dict(image.info)

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_orientation_is_normalised_to_one(self):
        # rawpy 出力は正立なので、元の 8 を継承すると二重回転になる
        self.assertEqual(self.exif[TAG_ORIENTATION], 1)

    def test_pixel_dimensions_are_updated(self):
        self.assertEqual(self.ifd[TAG_PIXEL_X_DIMENSION], 3000)
        self.assertEqual(self.ifd[TAG_PIXEL_Y_DIMENSION], 2000)

    def test_makernote_is_dropped(self):
        self.assertNotIn(TAG_MAKERNOTE, self.ifd)

    def test_software_records_provenance(self):
        self.assertEqual(self.exif[TAG_SOFTWARE], SOFTWARE_TAG)
        self.assertIn("GeoFrame RAW Assist", SOFTWARE_TAG)

    def test_lens_model_is_preserved(self):
        self.assertEqual(self.ifd[TAG_LENS_MODEL], LENS_MODEL)

    def test_capture_time_is_preserved(self):
        self.assertEqual(self.ifd[TAG_DATETIME_ORIGINAL], CAPTURED_AT)

    def test_iso_is_preserved(self):
        self.assertEqual(self.ifd[TAG_ISO], 3200)

    def test_camera_identity_is_preserved(self):
        self.assertEqual(self.exif[TAG_MAKE], "SONY")
        self.assertEqual(self.exif[TAG_MODEL], "ILCE-6400")

    def test_icc_profile_is_attached(self):
        self.assertEqual(self.info.get("icc_profile"), SRGB_ICC_BYTES)


class SrgbIccTests(unittest.TestCase):
    def test_profile_is_non_empty_bytes(self):
        self.assertIsInstance(SRGB_ICC_BYTES, bytes)
        self.assertGreater(len(SRGB_ICC_BYTES), 100)


class ReadCaptureExifTests(unittest.TestCase):
    def test_reads_summary_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.JPG"
            _write_source_jpeg(source)
            summary = read_capture_exif(source)

        self.assertEqual(summary["make"], "SONY")
        self.assertEqual(summary["model"], "ILCE-6400")
        self.assertEqual(summary["orientation"], 8)
        self.assertEqual(summary["lens_model"], LENS_MODEL)
        self.assertEqual(summary["captured_at"], CAPTURED_AT)
        self.assertEqual(summary["iso"], 3200)
        self.assertAlmostEqual(summary["focal_length"], 10.7, places=2)
        self.assertAlmostEqual(summary["f_number"], 3.5, places=2)

    def test_returns_empty_dict_for_unreadable_file(self):
        with tempfile.TemporaryDirectory() as directory:
            broken = Path(directory) / "broken.JPG"
            broken.write_bytes(b"not a jpeg")
            self.assertEqual(read_capture_exif(broken), {})


if __name__ == "__main__":
    unittest.main()
