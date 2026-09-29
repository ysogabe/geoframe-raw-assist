"""現像元フォルダ内の ARW / JPG 解決（拡張子の大文字小文字を問わない）のテスト。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_raw import analysis, engine, source_files
from local_raw import development_candidates as candidates
from local_raw.engine import RawDevelopmentError, develop_raw


def _touch(root: Path, *names: str) -> None:
    for name in names:
        (root / name).write_bytes(b"x")


class FindCaptureFileTest(unittest.TestCase):
    def test_returns_actual_entry_name_for_any_extension_case(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _touch(root, "DSC00001.arw", "DSC00001.jpg", "DSC00002.ARW", "DSC00002.JPG", "DSC00003.Arw")
            self.assertEqual(source_files.find_capture_file(root, "DSC00001", "ARW").name, "DSC00001.arw")
            self.assertEqual(source_files.find_capture_file(root, "DSC00001", "JPG").name, "DSC00001.jpg")
            self.assertEqual(source_files.find_capture_file(root, "DSC00002", "ARW").name, "DSC00002.ARW")
            self.assertEqual(source_files.find_capture_file(root, "DSC00002", "JPG").name, "DSC00002.JPG")
            self.assertEqual(source_files.find_capture_file(root, "DSC00003", "arw").name, "DSC00003.Arw")

    def test_missing_or_invalid_returns_none(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _touch(root, "DSC00001.arw")
            self.assertIsNone(source_files.find_capture_file(root, "DSC00001", "JPG"))
            self.assertIsNone(source_files.find_capture_file(root, "DSC00009", "ARW"))
            self.assertIsNone(source_files.find_capture_file(root, "../DSC00001", "ARW"))
            self.assertIsNone(source_files.find_capture_file(root, "DSC00001", "PNG"))
            self.assertIsNone(source_files.find_capture_file(root / "nope", "DSC00001", "ARW"))

    def test_duplicate_case_variants_prefer_canonical_then_lexical(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _touch(root, "DSC00001.arw")
            # 大文字小文字を区別しないボリュームでは同居できないので、あれば検証する。
            (root / "DSC00001.ARW").write_bytes(b"y")
            names = sorted(p.name for p in root.iterdir())
            if len(names) < 2:
                self.skipTest("case-insensitive volume")
            self.assertEqual(source_files.find_capture_file(root, "DSC00001", "ARW").name, "DSC00001.ARW")

    def test_lexical_fallback_is_deterministic(self):
        with mock.patch.object(source_files.os, "listdir", return_value=["DSC00001.arw", "DSC00001.Arw"]):
            picked = source_files.find_capture_file(Path("/x"), "DSC00001", "ARW")
        self.assertEqual(picked, Path("/x/DSC00001.Arw"))


class ScanPairsTest(unittest.TestCase):
    def test_lowercase_extensions_are_paired(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _touch(root, "DSC00001.arw", "DSC00001.jpg", "DSC00002.ARW", "DSC00002.JPG", "DSC00003.jpg", "notes.txt")
            source, jpg_paths = analysis.scan_pairs(root)
        self.assertEqual((source["pairs"], source["jpg_only"], source["arw_only"]), (2, 1, 0))
        self.assertEqual([p.name for p in jpg_paths], ["DSC00001.jpg", "DSC00002.JPG", "DSC00003.jpg"])

    def test_signature_uses_actual_names_and_is_stable_for_uppercase(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _touch(root, "DSC00001.ARW", "DSC00001.JPG")
            upper = analysis._source_signature(root)
            (root / "DSC00001.ARW").rename(root / "DSC00001.arw")
            self.assertNotEqual(upper, analysis._source_signature(root))


class ResolutionTest(unittest.TestCase):
    def test_engine_opens_lowercase_arw(self):
        for arw_name in ("DSC00001.arw", "DSC00001.ARW"):
            with self.subTest(arw_name):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "src"
                    root.mkdir()
                    _touch(root, arw_name)
                    opened = []
                    with (
                        mock.patch.object(engine.config, "source_root", return_value=root),
                        mock.patch.object(engine, "OUTPUT_ROOT", Path(directory) / "out"),
                        mock.patch.object(engine, "PREVIEW_ROOT", Path(directory) / "preview"),
                        mock.patch.object(
                            engine.rawpy, "imread", side_effect=lambda path, *a, **k: opened.append(Path(path)) or (_ for _ in ()).throw(RuntimeError("stop"))
                        ),
                    ):
                        with self.assertRaises(Exception) as raised:
                            develop_raw("DSC00001", "neutral_recovery", max_edge=1200)
                    self.assertNotIn("見つかりません", str(raised.exception))
                    self.assertEqual([p.name for p in opened], [arw_name])

    def test_engine_missing_arw_still_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(engine.config, "source_root", return_value=root):
                with self.assertRaises(RawDevelopmentError):
                    develop_raw("DSC00001", "neutral_recovery", max_edge=1200)

    def test_engine_rejects_symlink_pointing_outside_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "src"
            root.mkdir()
            outside = base / "outside.arw"
            outside.write_bytes(b"x")
            (root / "DSC00001.arw").symlink_to(outside)
            with mock.patch.object(engine.config, "source_root", return_value=root):
                with self.assertRaises(RawDevelopmentError):
                    develop_raw("DSC00001", "neutral_recovery", max_edge=1200)

    def test_candidates_resolve_lowercase_and_reject_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "src"
            root.mkdir()
            _touch(root, "DSC00001.arw", "DSC00001.jpg", "DSC00002.ARW", "DSC00002.JPG")
            outside = base / "outside.arw"
            outside.write_bytes(b"x")
            (root / "DSC00003.arw").symlink_to(outside)
            with mock.patch.object(candidates.config, "source_root", return_value=root):
                arw, jpg = candidates._resolve_source_paths("DSC00001")
                self.assertEqual((arw.name, jpg.name), ("DSC00001.arw", "DSC00001.jpg"))
                arw, jpg = candidates._resolve_source_paths("DSC00002")
                self.assertEqual((arw.name, jpg.name), ("DSC00002.ARW", "DSC00002.JPG"))
                with self.assertRaises(candidates.DevelopmentCandidateNotFound):
                    candidates._resolve_source_paths("DSC00003")
                with self.assertRaises(candidates.DevelopmentCandidateNotFound):
                    candidates._resolve_source_paths("DSC00009")


if __name__ == "__main__":
    unittest.main()
