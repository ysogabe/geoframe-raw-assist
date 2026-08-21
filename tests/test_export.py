"""書き出し（export）と Apple「写真」取り込みの検証。

- 実ファイルは合成データのみ（サンプル写真に依存しない）。
- `osascript` は必ず mock する（Photos.app は呼ばない）。
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from local_raw import analysis as analysis_module
from local_raw import config as config_module
from local_raw import export as export_module
from local_raw import photos_import as photos_module
from local_raw.exif import (
    TAG_DATETIME_ORIGINAL,
    TAG_EXIF_IFD,
    TAG_FOCAL_LENGTH,
    TAG_ISO,
    TAG_LENS_MODEL,
)
from local_raw.export import export_batch, normalize_batch_name

CAPTURE = "DSC02943"
OTHER_CAPTURE = "DSC02944"
DEVELOPED = f"{CAPTURE}-nightscape-a1b2c3d4e5.jpg"
MERGED = f"{CAPTURE}-merged5-neutral_recovery-b2c3d4e5f6.jpg"


class ExportTestCase(unittest.TestCase):
    """tmpdir に SOURCE / OUTPUT / EXPORT ルートを作って export_batch を検証する。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # macOS の /var → /private/var シンボリックリンクで相対パス化が壊れないよう resolve する。
        self.project_root = Path(temporary.name).resolve()
        self.source_root = self.project_root / "sample" / "100MSDCF"
        self.output_root = self.project_root / "outputs" / "developed"
        self.export_root = self.project_root / "outputs" / "export"
        self.merged_root = self.project_root / "outputs" / "merged"
        self.analysis_path = self.project_root / "outputs" / "analysis" / "analysis.json"
        for directory in (self.source_root, self.output_root, self.export_root, self.merged_root):
            directory.mkdir(parents=True, exist_ok=True)
        self.analysis_path.parent.mkdir(parents=True, exist_ok=True)

        (self.source_root / f"{CAPTURE}.JPG").write_bytes(b"camera-jpeg-bytes")
        (self.source_root / f"{CAPTURE}.ARW").write_bytes(b"raw-bytes")
        (self.output_root / DEVELOPED).write_bytes(b"developed-jpeg-bytes")
        (self.output_root / f"{Path(DEVELOPED).stem}.json").write_text(
            json.dumps({"recipe_version": 2, "capture_id": CAPTURE, "params_hash": "a1b2c3d4e5"}),
            encoding="utf-8",
        )
        (self.merged_root / MERGED).write_bytes(b"merged-jpeg-bytes")
        (self.merged_root / f"{Path(MERGED).stem}.json").write_text(
            json.dumps(
                {
                    "recipe_version": 1,
                    "kind": "exposure_fusion",
                    "capture_ids": [CAPTURE, OTHER_CAPTURE],
                    "params_hash": "b2c3d4e5f6",
                    "aligned": True,
                }
            ),
            encoding="utf-8",
        )

        patcher = patch.multiple(
            export_module,
            PROJECT_ROOT=self.project_root,
            OUTPUT_ROOT=self.output_root,
            EXPORT_ROOT=self.export_root,
            MERGED_ROOT=self.merged_root,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # 解析キャッシュの実体は `analysis` 側が持つ（export は
        # `analysis.read_cached_analysis()` 経由でしか読まない）。
        analysis_patcher = patch.object(analysis_module, "ANALYSIS_PATH", self.analysis_path)
        analysis_patcher.start()
        self.addCleanup(analysis_patcher.stop)
        # 現像元フォルダはモジュール定数ではなく `config.source_root()` で
        # 呼び出し時に解決されるので、定数差し替えではなく関数を差し替える。
        self.patch_source_root(self.source_root)

    def patch_source_root(self, root: Path):
        source_patcher = patch.object(config_module, "source_root", return_value=root)
        source_patcher.start()
        self.addCleanup(source_patcher.stop)

    def write_analysis(self, root: Path | None = None):
        """`root`（既定は現在の現像元フォルダ）の解析結果としてキャッシュを書く。

        `analysis.read_cached_analysis()` はフォルダ署名で有効性を判定するので、
        署名を入れずに書くと常に無効扱いになる。署名は書き込み時点のフォルダ内容から
        計算されるため、写真を足す場合は `write_analysis()` より**先に**置くこと。
        """
        target = root if root is not None else self.source_root
        self.analysis_path.write_text(
            json.dumps(
                {
                    "analysis_version": analysis_module.ANALYSIS_VERSION,
                    analysis_module.SIGNATURE_KEY: analysis_module._source_signature(target),
                    "photos": {
                        CAPTURE: {
                            "capture_id": CAPTURE,
                            "captured_at": "2025-06-09T00:35:11",
                            "iso": 3200,
                            "lens_model": "10-18mm F2.8 DC DN | Contemporary 023",
                            "focal_length": 10.7,
                            "overall_score": 88.0,
                        }
                    },
                    "groups": [
                        {
                            "id": 41,
                            "kind": "similar",
                            "capture_ids": [CAPTURE, OTHER_CAPTURE],
                            "recommended_capture_id": CAPTURE,
                            "raw": {"level": "strong", "label": "RAW推奨"},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def both_items(self):
        return [
            {"capture_id": CAPTURE, "variant": "camera_jpg"},
            {"capture_id": CAPTURE, "variant": "developed", "developed_filename": DEVELOPED},
        ]

    def read_manifest(self, result):
        return json.loads((self.project_root / result["manifest_path"]).read_text(encoding="utf-8"))


class ExportCopyTests(ExportTestCase):
    def test_copies_both_variants_into_batch_directory(self):
        result = export_batch("trip08", self.both_items())

        batch_dir = self.export_root / "trip08"
        self.assertEqual(result["export_dir"], "outputs/export/trip08")
        self.assertEqual(result["skipped"], [])
        self.assertEqual(
            sorted(result["files"]),
            sorted([f"outputs/export/trip08/{CAPTURE}.JPG", f"outputs/export/trip08/{DEVELOPED}"]),
        )
        self.assertEqual((batch_dir / f"{CAPTURE}.JPG").read_bytes(), b"camera-jpeg-bytes")
        self.assertEqual((batch_dir / DEVELOPED).read_bytes(), b"developed-jpeg-bytes")

    def test_leaves_no_temporary_files(self):
        export_batch("trip08", self.both_items())
        leftovers = [path.name for path in (self.export_root / "trip08").iterdir()
                     if path.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_source_files_are_untouched(self):
        watched = [
            self.source_root / f"{CAPTURE}.JPG",
            self.source_root / f"{CAPTURE}.ARW",
            self.output_root / DEVELOPED,
        ]
        before = {path: (path.stat().st_mtime_ns, path.stat().st_size) for path in watched}

        export_batch("trip08", self.both_items())

        for path in watched:
            with self.subTest(path=path.name):
                self.assertEqual((path.stat().st_mtime_ns, path.stat().st_size), before[path])

    def test_copy2_preserves_source_mtime_on_the_copy(self):
        export_batch("trip08", self.both_items())
        source = self.source_root / f"{CAPTURE}.JPG"
        copied = self.export_root / "trip08" / f"{CAPTURE}.JPG"
        self.assertEqual(int(copied.stat().st_mtime), int(source.stat().st_mtime))

    def test_rerun_overwrites_without_duplicating_entries(self):
        export_batch("trip08", self.both_items())
        result = export_batch("trip08", self.both_items())
        self.assertEqual(len(result["files"]), 2)

    def test_duplicate_items_are_skipped_once(self):
        items = self.both_items() + [{"capture_id": CAPTURE, "variant": "camera_jpg"}]
        result = export_batch("trip08", items)
        self.assertEqual(len(result["files"]), 2)
        self.assertEqual(len(result["skipped"]), 1)


class ManifestTests(ExportTestCase):
    def test_manifest_top_level_shape(self):
        self.write_analysis()
        result = export_batch("trip08", self.both_items())
        manifest = self.read_manifest(result)

        self.assertEqual(manifest["manifest_version"], 1)
        self.assertEqual(manifest["app"], "geoframe-raw-assist")
        self.assertEqual(manifest["batch_name"], "trip08")
        self.assertEqual(manifest["source_root"], "sample/100MSDCF")
        self.assertTrue(manifest["created_at"])
        self.assertEqual(len(manifest["items"]), 2)

    def test_developed_item_inlines_recipe_group_and_exif(self):
        self.write_analysis()
        result = export_batch("trip08", self.both_items())
        item = next(i for i in self.read_manifest(result)["items"] if i["variant"] == "developed")

        self.assertEqual(item["capture_id"], CAPTURE)
        self.assertEqual(item["exported_file"], DEVELOPED)
        self.assertEqual(item["recipe"]["params_hash"], "a1b2c3d4e5")
        self.assertEqual(item["source"]["jpg"], f"sample/100MSDCF/{CAPTURE}.JPG")
        self.assertEqual(item["source"]["arw"], f"sample/100MSDCF/{CAPTURE}.ARW")
        self.assertEqual(len(item["source"]["jpg_sha256"]), 64)
        self.assertEqual(
            item["group"],
            {
                "id": 41,
                "kind": "similar",
                "raw_level": "strong",
                "was_recommended": True,
                "user_selected": True,
            },
        )
        self.assertEqual(item["exif"]["iso"], 3200)
        self.assertEqual(item["exif"]["focal_length"], 10.7)

    def test_camera_jpg_item_has_null_recipe(self):
        result = export_batch("trip08", [self.both_items()[0]])
        item = self.read_manifest(result)["items"][0]
        self.assertIsNone(item["recipe"])
        self.assertEqual(item["variant"], "camera_jpg")

    def test_group_and_exif_are_null_without_analysis(self):
        result = export_batch("trip08", self.both_items())
        for item in self.read_manifest(result)["items"]:
            with self.subTest(variant=item["variant"]):
                self.assertIsNone(item["group"])
                self.assertIsNone(item["exif"])

    def test_was_recommended_is_false_for_non_recommended_capture(self):
        # 解析キャッシュの署名はフォルダ内容から計算されるので、写真を足してから書く。
        (self.source_root / f"{OTHER_CAPTURE}.JPG").write_bytes(b"other-camera-jpeg")
        self.write_analysis()
        result = export_batch("trip08", [{"capture_id": OTHER_CAPTURE, "variant": "camera_jpg"}])
        self.assertFalse(self.read_manifest(result)["items"][0]["group"]["was_recommended"])

    def test_manifest_is_not_listed_as_an_exported_file(self):
        result = export_batch("trip08", self.both_items())
        self.assertNotIn("outputs/export/trip08/manifest.json", result["files"])
        self.assertEqual(result["manifest_path"], "outputs/export/trip08/manifest.json")

    def test_broken_analysis_json_is_ignored(self):
        self.analysis_path.write_text("{ not json", encoding="utf-8")
        result = export_batch("trip08", [self.both_items()[0]])
        self.assertIsNone(self.read_manifest(result)["items"][0]["group"])

    def test_analysis_with_invalid_utf8_is_ignored(self):
        """壊れ方が「不正な UTF-8」でも書き出しは続行する（500 にしない）。"""
        self.analysis_path.write_bytes(b"\xff\xfe not utf-8 \x00\x80")
        result = export_batch("trip08", [self.both_items()[0]])
        self.assertIsNone(self.read_manifest(result)["items"][0]["group"])


class OutsideProjectSourceRootTests(ExportTestCase):
    """現像元フォルダがプロジェクト外（`~/Pictures`・外部ボリューム等）でも壊れないこと。

    フォルダ選択機能の本題のケース。`_relative_path()` が相対化できないパスを
    絶対パスのまま返す既存挙動に依存している。
    """

    def setUp(self):
        super().setUp()
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        self.outside_root = Path(outside.name).resolve() / "DCIM" / "100MSDCF"
        self.outside_root.mkdir(parents=True, exist_ok=True)
        (self.outside_root / f"{CAPTURE}.JPG").write_bytes(b"external-volume-jpeg")
        (self.outside_root / f"{CAPTURE}.ARW").write_bytes(b"external-volume-raw")
        self.patch_source_root(self.outside_root)

    def test_copies_from_a_folder_outside_the_project(self):
        result = export_batch("trip08", [{"capture_id": CAPTURE, "variant": "camera_jpg"}])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(len(result["files"]), 1)
        copied = self.export_root / "trip08" / f"{CAPTURE}.JPG"
        self.assertEqual(copied.read_bytes(), (self.outside_root / f"{CAPTURE}.JPG").read_bytes())

    def test_manifest_records_absolute_paths_for_an_external_folder(self):
        result = export_batch("trip08", [{"capture_id": CAPTURE, "variant": "camera_jpg"}])
        manifest = self.read_manifest(result)
        self.assertEqual(manifest["source_root"], str(self.outside_root))
        item = manifest["items"][0]
        self.assertEqual(item["source"]["jpg"], str(self.outside_root / f"{CAPTURE}.JPG"))
        self.assertEqual(item["source"]["arw"], str(self.outside_root / f"{CAPTURE}.ARW"))

    def test_project_relative_files_are_not_reachable_after_switching(self):
        """切り替え後は旧フォルダ（プロジェクト内 sample/）の写真は見えない。"""
        (self.source_root / f"{OTHER_CAPTURE}.JPG").write_bytes(b"old-folder-jpeg")
        result = export_batch("trip08", [{"capture_id": OTHER_CAPTURE, "variant": "camera_jpg"}])
        self.assertEqual(result["files"], [])
        self.assertEqual(result["skipped"][0]["capture_id"], OTHER_CAPTURE)


class StaleAnalysisAfterFolderSwitchTests(ExportTestCase):
    """フォルダ A の解析結果が残ったまま B を書き出しても A のメタデータを載せないこと。

    `capture_id` はカメラの連番なので、カードが違えば別の写真に同じ ID が付く。
    B の解析を走らせないまま（または解析が失敗したまま）書き出すと、コピーされる実体は
    B なのに manifest の `exif` / `group` だけ A のもの、という取り違えが起きうる。
    """

    #: B の実ファイルに埋める EXIF。A の解析値（3200 / 10.7）と混同しない値にする。
    B_CAPTURED_AT = "2026:03:04 05:06:07"
    B_ISO = 125
    B_LENS_MODEL = "FE 24mm F1.4 GM"
    B_FOCAL_LENGTH = 24.0
    #: A の解析結果（`write_analysis()` が書く値）。
    A_CAPTURED_AT = "2025-06-09T00:35:11"
    A_ISO = 3200

    def setUp(self):
        super().setUp()
        # 1. フォルダ A（setUp の source_root）の解析結果をキャッシュに残す。
        self.write_analysis()
        # 2. 同じ capture_id を持つ別カード B へ切り替える（B の解析は走らせない）。
        card_b = tempfile.TemporaryDirectory()
        self.addCleanup(card_b.cleanup)
        self.card_b = Path(card_b.name).resolve() / "DCIM" / "101MSDCF"
        self.card_b.mkdir(parents=True, exist_ok=True)
        self.write_jpeg_with_exif(self.card_b / f"{CAPTURE}.JPG")
        (self.card_b / f"{CAPTURE}.ARW").write_bytes(b"card-b-raw")
        self.patch_source_root(self.card_b)

    def write_jpeg_with_exif(self, path: Path):
        """B の写真として EXIF 付きの合成 JPEG を書く（`tests/test_exif.py` と同じ流儀）。"""
        image = Image.new("RGB", (16, 12), (30, 60, 90))
        exif = image.getexif()
        ifd = exif.get_ifd(TAG_EXIF_IFD)
        ifd[TAG_DATETIME_ORIGINAL] = self.B_CAPTURED_AT
        ifd[TAG_ISO] = self.B_ISO
        ifd[TAG_LENS_MODEL] = self.B_LENS_MODEL
        ifd[TAG_FOCAL_LENGTH] = self.B_FOCAL_LENGTH
        image.save(path, "JPEG", exif=exif.tobytes())

    def export_one(self):
        result = export_batch("trip09", [{"capture_id": CAPTURE, "variant": "camera_jpg"}])
        return self.read_manifest(result)["items"][0]

    def test_exif_comes_from_the_new_folder_not_the_stale_analysis(self):
        exif = self.export_one()["exif"]

        self.assertEqual(exif["iso"], self.B_ISO)
        self.assertEqual(exif["lens_model"], self.B_LENS_MODEL)
        self.assertEqual(exif["focal_length"], self.B_FOCAL_LENGTH)
        self.assertEqual(exif["captured_at"], self.B_CAPTURED_AT)
        self.assertNotEqual(exif["captured_at"], self.A_CAPTURED_AT)

    def test_group_is_dropped_because_the_analysis_is_for_another_folder(self):
        """`group` は実ファイルへのフォールバックが無いので、混入すれば必ず A 由来。"""
        self.assertIsNone(self.export_one()["group"])

    def test_copied_bytes_are_from_the_new_folder(self):
        item = self.export_one()
        copied = self.export_root / "trip09" / f"{CAPTURE}.JPG"
        self.assertEqual(copied.read_bytes(), (self.card_b / f"{CAPTURE}.JPG").read_bytes())
        self.assertEqual(item["source"]["jpg"], str(self.card_b / f"{CAPTURE}.JPG"))

    def test_analysis_for_the_current_folder_is_still_used(self):
        """B を解析し直せば（署名が一致すれば）これまでどおり解析結果が載る。"""
        self.write_analysis(root=self.card_b)
        item = self.export_one()

        self.assertEqual(item["exif"]["iso"], self.A_ISO)
        self.assertEqual(item["exif"]["captured_at"], self.A_CAPTURED_AT)
        self.assertEqual(item["group"]["id"], 41)


class BatchNameTests(ExportTestCase):
    def test_normalizes_disallowed_characters(self):
        self.assertEqual(normalize_batch_name("2025-06 旅行 08"), "2025-06____08")
        self.assertEqual(normalize_batch_name("  trip08  "), "trip08")

    def test_rejects_parent_directory_traversal(self):
        for name in ("../evil", "..", "a/../b", "..\\evil", "outputs/export/x"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                normalize_batch_name(name)

    def test_rejects_empty_or_non_string(self):
        for name in ("", "   ", None, 12, ["a"]):
            with self.subTest(name=name), self.assertRaises(ValueError):
                normalize_batch_name(name)

    def test_rejects_overlong_name(self):
        with self.assertRaises(ValueError):
            normalize_batch_name("x" * 65)

    def test_export_stays_inside_export_root(self):
        with self.assertRaises(ValueError):
            export_batch("../evil", self.both_items())
        self.assertFalse((self.project_root / "outputs" / "evil").exists())

    def test_normalized_name_is_used_for_the_directory(self):
        result = export_batch("trip 08", self.both_items())
        self.assertEqual(result["export_dir"], "outputs/export/trip_08")
        self.assertTrue((self.export_root / "trip_08").is_dir())


class MergedVariantTests(ExportTestCase):
    """`variant: "merged"`（Exposure Fusion の出力）の書き出し。"""

    def merged_item(self, filename=MERGED, capture_id=CAPTURE):
        return {"capture_id": capture_id, "variant": "merged", "merged_filename": filename}

    def test_copies_the_merged_jpeg(self):
        result = export_batch("trip08", [self.merged_item()])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["files"], [f"outputs/export/trip08/{MERGED}"])
        self.assertEqual(
            (self.export_root / "trip08" / MERGED).read_bytes(), b"merged-jpeg-bytes"
        )

    def test_manifest_inlines_the_merge_recipe(self):
        item = self.read_manifest(export_batch("trip08", [self.merged_item()]))["items"][0]
        self.assertEqual(item["variant"], "merged")
        self.assertEqual(item["exported_file"], MERGED)
        self.assertEqual(item["recipe"]["kind"], "exposure_fusion")
        self.assertEqual(item["recipe"]["capture_ids"], [CAPTURE, OTHER_CAPTURE])
        self.assertTrue(item["recipe"]["aligned"])

    def test_merged_and_developed_can_be_exported_together(self):
        result = export_batch("trip08", self.both_items() + [self.merged_item()])
        self.assertEqual(len(result["files"]), 3)
        self.assertEqual(result["skipped"], [])

    def test_missing_merged_file_is_skipped_not_raised(self):
        result = export_batch(
            "trip08", [self.merged_item(f"{CAPTURE}-merged5-neutral_recovery-0000000000.jpg")]
        )
        self.assertEqual(result["files"], [])
        self.assertEqual(result["skipped"][0]["capture_id"], CAPTURE)

    def test_rejects_escapes_and_wrong_shapes(self):
        for filename in (
            "../../etc/passwd",
            f"subdir/{MERGED}",
            "/etc/passwd",
            f"{Path(MERGED).stem}.json",
            DEVELOPED,  # `-merged{枚数}-` が無い現像済みファイル名
            f"{CAPTURE}-merged-neutral_recovery-b2c3d4e5f6.jpg",  # 枚数が無い
            None,
            "",
            123,
        ):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                export_batch("trip08", [self.merged_item(filename)])

    def test_rejects_another_captures_merge(self):
        with self.assertRaises(ValueError):
            export_batch("trip08", [self.merged_item(capture_id=OTHER_CAPTURE)])

    def test_rejects_mismatched_capture_id_and_writes_nothing(self):
        """capture_id が合成ファイル名の先頭ID（capture_ids[0]）と違えば 400 で1件も書かない。"""
        with self.assertRaises(ValueError):
            export_batch("trip08", [self.merged_item(capture_id=OTHER_CAPTURE)])
        # 検証はコピーより前に走るので、書き出し先ディレクトリ自体が作られない。
        self.assertFalse((self.export_root / "trip08").exists())

    def test_exports_when_capture_id_matches_the_first_merged_frame(self):
        """capture_id を合成ファイル名の先頭ID（capture_ids[0]）にすれば合成JPEGが書き出される。"""
        result = export_batch("trip08", [self.merged_item(capture_id=CAPTURE)])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["files"], [f"outputs/export/trip08/{MERGED}"])
        self.assertEqual((self.export_root / "trip08" / MERGED).read_bytes(), b"merged-jpeg-bytes")

    def write_merged(self, filename, recipe):
        """`outputs/merged/` に合成JPEGとレシピJSONを1組置く。"""
        (self.merged_root / filename).write_bytes(b"merged-jpeg-bytes")
        (self.merged_root / f"{Path(filename).stem}.json").write_text(
            json.dumps(recipe), encoding="utf-8"
        )

    def test_rejects_a_preview_quality_merge(self):
        """プレビュー合成（長辺1600px）はマスターとして書き出さない（二段目の防御）。

        プレビューは `outputs/merged_preview/` に分けたので通常ここへは来ないが、
        分離前の出力が `outputs/merged/` に残っていても素通りしないことを固定する。
        レシピの形は `bracket.py` が実際に書くもの（フラグは `output.preview`）。
        """
        filename = f"{CAPTURE}-merged5-neutral_recovery-c3d4e5f6a7.jpg"
        self.write_merged(
            filename,
            {
                "recipe_version": 1,
                "kind": "exposure_fusion",
                "capture_ids": [CAPTURE, OTHER_CAPTURE],
                "params_hash": "c3d4e5f6a7",
                "output": {"max_edge": 1600, "preview": True},
            },
        )
        with self.assertRaises(ValueError):
            export_batch("trip08", [self.merged_item(filename)])
        # 400 で終わるので1件も書き出されないこと（コピーはループを抜けた後）。
        self.assertFalse((self.export_root / "trip08" / filename).exists())

    def test_does_not_reach_into_the_merged_preview_directory(self):
        """一段目の防御: プレビュー合成は `outputs/merged_preview/` にあり参照されない。

        実物のプレビュー合成（`outputs/merged/` には存在しない）を名指ししても、
        「見つからない」として skip されるだけでコピーは起きない。
        `..` で隣のディレクトリへ出ようとするものはファイル名検証で 400。
        """
        merged_preview_root = self.project_root / "outputs" / "merged_preview"
        merged_preview_root.mkdir(parents=True, exist_ok=True)
        filename = f"{CAPTURE}-merged5-neutral_recovery-a7b8c9d0e1.jpg"
        (merged_preview_root / filename).write_bytes(b"preview-merged-jpeg-bytes")

        result = export_batch("trip08", [self.merged_item(filename)])
        self.assertEqual(result["files"], [])
        self.assertEqual(result["skipped"][0]["reason"], "合成済みJPEGが見つかりません。")
        self.assertFalse((self.export_root / "trip08" / filename).exists())

        with self.assertRaises(ValueError):
            export_batch("trip08", [self.merged_item(f"../merged_preview/{filename}")])

    def test_rejects_a_preview_flag_at_the_recipe_root(self):
        """レシピ直下に `preview` があっても拒否する（形が変わっても防御が死なないこと）。"""
        filename = f"{CAPTURE}-merged5-neutral_recovery-d4e5f6a7b8.jpg"
        self.write_merged(filename, {"recipe_version": 1, "preview": True})
        with self.assertRaises(ValueError):
            export_batch("trip08", [self.merged_item(filename)])

    def test_full_merge_with_an_explicit_preview_false_is_exported(self):
        """`preview: false` の本合成は従来どおり通ること（過剰に弾かないこと）。"""
        filename = f"{CAPTURE}-merged5-neutral_recovery-e5f6a7b8c9.jpg"
        self.write_merged(
            filename,
            {
                "recipe_version": 1,
                "kind": "exposure_fusion",
                "params_hash": "e5f6a7b8c9",
                "output": {"max_edge": 6000, "preview": False},
            },
        )
        result = export_batch("trip08", [self.merged_item(filename)])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["files"], [f"outputs/export/trip08/{filename}"])

    def test_missing_merged_file_with_a_preview_recipe_is_still_skipped(self):
        """レシピだけがあってJPEGが無い場合は「無い」として skip する（400 にしない）。"""
        filename = f"{CAPTURE}-merged5-neutral_recovery-f6a7b8c9d0.jpg"
        (self.merged_root / f"{Path(filename).stem}.json").write_text(
            json.dumps({"output": {"preview": True}}), encoding="utf-8"
        )
        result = export_batch("trip08", [self.merged_item(filename)])
        self.assertEqual(result["files"], [])
        self.assertEqual(result["skipped"][0]["capture_id"], CAPTURE)

    def test_merged_filename_is_not_accepted_as_developed(self):
        """`merged` と `developed` のルートが取り違えられないこと。"""
        with self.assertRaises(ValueError):
            export_batch(
                "trip08",
                [{"capture_id": CAPTURE, "variant": "developed", "developed_filename": MERGED}],
            )


class DevelopedPreviewQualityTests(ExportTestCase):
    """プレビュー品質の**現像**もマスターとして書き出さないこと。

    プレビュー現像は `outputs/preview/` に分けてあるので通常ここへは来ないが、
    レシピには合成と同じ `output.preview` が書かれる（`engine.develop_raw`）。
    合成側（`MergedVariantTests.test_rejects_a_preview_quality_merge`）と同じ防御が
    `developed` にも効いていることを固定する。
    """

    def write_developed(self, filename, recipe):
        (self.output_root / filename).write_bytes(b"developed-jpeg-bytes")
        (self.output_root / f"{Path(filename).stem}.json").write_text(
            json.dumps(recipe), encoding="utf-8"
        )

    def developed_item(self, filename):
        return {"capture_id": CAPTURE, "variant": "developed", "developed_filename": filename}

    def test_rejects_a_preview_quality_develop(self):
        filename = f"{CAPTURE}-nightscape-c3d4e5f6a7.jpg"
        self.write_developed(
            filename,
            {
                "recipe_version": 2,
                "capture_id": CAPTURE,
                "params_hash": "c3d4e5f6a7",
                "output": {"max_edge": 1600, "preview": True},
            },
        )
        with self.assertRaises(ValueError):
            export_batch("trip08", [self.developed_item(filename)])
        # 400 で終わるので1件も書き出されないこと（コピーはループを抜けた後）。
        self.assertFalse((self.export_root / "trip08" / filename).exists())

    def test_rejects_a_preview_flag_at_the_recipe_root(self):
        """レシピの形が変わっても防御が黙って死なないこと（合成側と同じ二重見）。"""
        filename = f"{CAPTURE}-nightscape-d4e5f6a7b8.jpg"
        self.write_developed(filename, {"recipe_version": 2, "preview": True})
        with self.assertRaises(ValueError):
            export_batch("trip08", [self.developed_item(filename)])

    def test_full_develop_with_an_explicit_preview_false_is_exported(self):
        """`preview: false` の本現像は従来どおり通ること（過剰に弾かないこと）。"""
        filename = f"{CAPTURE}-nightscape-e5f6a7b8c9.jpg"
        self.write_developed(
            filename,
            {
                "recipe_version": 2,
                "capture_id": CAPTURE,
                "params_hash": "e5f6a7b8c9",
                "output": {"max_edge": 6000, "preview": False},
            },
        )
        result = export_batch("trip08", [self.developed_item(filename)])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["files"], [f"outputs/export/trip08/{filename}"])

    def test_camera_jpg_has_no_recipe_and_is_unaffected(self):
        """`camera_jpg` はレシピを持たないので、この防御は素通りすること。"""
        result = export_batch("trip08", [{"capture_id": CAPTURE, "variant": "camera_jpg"}])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["files"], [f"outputs/export/trip08/{CAPTURE}.JPG"])


class DevelopedFilenameValidationTests(ExportTestCase):
    def _expect_reject(self, filename, capture_id=CAPTURE):
        with self.assertRaises(ValueError):
            export_batch(
                "trip08",
                [
                    {
                        "capture_id": capture_id,
                        "variant": "developed",
                        "developed_filename": filename,
                    }
                ],
            )

    def test_rejects_absolute_and_relative_escapes(self):
        for filename in (
            "../../etc/passwd",
            "../analysis/analysis.json",
            f"../../sample/100MSDCF/{CAPTURE}.JPG",
            "/etc/passwd",
            f"subdir/{DEVELOPED}",
        ):
            with self.subTest(filename=filename):
                self._expect_reject(filename)

    def test_rejects_recipe_json(self):
        self._expect_reject(f"{CAPTURE}-nightscape-a1b2c3d4e5.json")
        self._expect_reject("DSC99999-x.json", capture_id="DSC99999")

    def test_rejects_other_captures_output(self):
        # DSC02944 として DSC02943 の現像結果を持ち出そうとする
        self._expect_reject(DEVELOPED, capture_id=OTHER_CAPTURE)

    def test_rejects_missing_or_empty_filename(self):
        self._expect_reject(None)
        self._expect_reject("")
        self._expect_reject(123)

    def test_rejects_invalid_capture_id(self):
        for capture_id in ("../DSC02943", "DSC1", "", None, "DSC02943.jpg"):
            with self.subTest(capture_id=capture_id), self.assertRaises(ValueError):
                export_batch("trip08", [{"capture_id": capture_id, "variant": "camera_jpg"}])

    def test_rejects_unknown_variant_and_malformed_items(self):
        for items in (
            [{"capture_id": CAPTURE, "variant": "original"}],
            [{"capture_id": CAPTURE}],
            ["DSC02943"],
            [],
            None,
            "DSC02943",
        ):
            with self.subTest(items=items), self.assertRaises(ValueError):
                export_batch("trip08", items)


class SkippedTests(ExportTestCase):
    def test_missing_camera_jpg_is_skipped_not_raised(self):
        result = export_batch(
            "trip08",
            [
                {"capture_id": OTHER_CAPTURE, "variant": "camera_jpg"},
                {"capture_id": CAPTURE, "variant": "camera_jpg"},
            ],
        )
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["skipped"][0]["capture_id"], OTHER_CAPTURE)

    def test_missing_developed_file_is_skipped(self):
        result = export_batch(
            "trip08",
            [
                {
                    "capture_id": CAPTURE,
                    "variant": "developed",
                    "developed_filename": f"{CAPTURE}-nightscape-0000000000.jpg",
                }
            ],
        )
        self.assertEqual(result["files"], [])
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(self.read_manifest(result)["items"], [])

    def test_manifest_is_written_even_when_everything_is_skipped(self):
        result = export_batch("trip08", [{"capture_id": OTHER_CAPTURE, "variant": "camera_jpg"}])
        self.assertTrue((self.project_root / result["manifest_path"]).is_file())


class PhotosImportTests(ExportTestCase):
    def setUp(self):
        super().setUp()
        self.batch_dir = self.export_root / "trip08"
        self.batch_dir.mkdir(parents=True, exist_ok=True)
        (self.batch_dir / f"{CAPTURE}.JPG").write_bytes(b"camera-jpeg-bytes")
        (self.batch_dir / DEVELOPED).write_bytes(b"developed-jpeg-bytes")
        (self.batch_dir / "manifest.json").write_text("{}", encoding="utf-8")
        (self.batch_dir / f"{Path(DEVELOPED).stem}.json").write_text("{}", encoding="utf-8")

    def _completed(self, returncode=0, stdout="2\n", stderr=""):
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)

    def test_calls_osascript_with_array_arguments(self):
        with patch.object(photos_module.subprocess, "run", return_value=self._completed()) as run:
            result = photos_module.import_batch("trip08")

        command = run.call_args.args[0]
        self.assertEqual(command[0], "osascript")
        self.assertEqual(command[1], str(photos_module.SCRIPT_PATH))
        self.assertEqual(command[2], "GeoFrame RAW Assist trip08")
        self.assertEqual(
            sorted(command[3:]),
            sorted([str(self.batch_dir / f"{CAPTURE}.JPG"), str(self.batch_dir / DEVELOPED)]),
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 180)
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertTrue(run.call_args.kwargs["text"])
        self.assertEqual(result, {"status": "ok", "imported": 2, "album": "GeoFrame RAW Assist trip08"})

    def test_only_jpeg_files_are_passed(self):
        with patch.object(photos_module.subprocess, "run", return_value=self._completed()) as run:
            photos_module.import_batch("trip08")
        passed = run.call_args.args[0][3:]
        self.assertTrue(all(path.lower().endswith(".jpg") for path in passed))
        self.assertFalse(any("manifest.json" in path for path in passed))

    def test_uppercase_jpg_extension_is_included(self):
        # posix flavour の glob("*.jpg") ではカメラJPG(.JPG)を取りこぼす
        with patch.object(photos_module.subprocess, "run", return_value=self._completed()) as run:
            photos_module.import_batch("trip08")
        self.assertIn(str(self.batch_dir / f"{CAPTURE}.JPG"), run.call_args.args[0])

    def test_custom_album_is_used(self):
        with patch.object(photos_module.subprocess, "run", return_value=self._completed()) as run:
            result = photos_module.import_batch("trip08", album="旅行2025")
        self.assertEqual(run.call_args.args[0][2], "旅行2025")
        self.assertEqual(result["album"], "旅行2025")

    def test_falls_back_to_file_count_when_stdout_is_not_a_number(self):
        with patch.object(photos_module.subprocess, "run", return_value=self._completed(stdout="")):
            self.assertEqual(photos_module.import_batch("trip08")["imported"], 2)

    def test_permission_failure_is_reported_with_guidance(self):
        stderr = "execution error: Photos got an error: Not authorized to send Apple events. (-1743)"
        with patch.object(
            photos_module.subprocess, "run", return_value=self._completed(1, "", stderr)
        ):
            result = photos_module.import_batch("trip08")
        self.assertEqual(result["status"], "error")
        self.assertIn("オートメーション", result["error"])
        self.assertIn("-1743", result["stderr"])

    def test_stderr_is_truncated(self):
        with patch.object(
            photos_module.subprocess, "run", return_value=self._completed(1, "", "x" * 2000)
        ):
            self.assertEqual(len(photos_module.import_batch("trip08")["stderr"]), 500)

    def test_timeout_is_reported_as_error_status(self):
        with patch.object(
            photos_module.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="osascript", timeout=180),
        ):
            result = photos_module.import_batch("trip08")
        self.assertEqual(result["status"], "error")
        self.assertIn("オートメーション", result["error"])

    def test_rejects_unknown_batch(self):
        with patch.object(photos_module.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                photos_module.import_batch("missing_batch")
        run.assert_not_called()

    def test_rejects_batch_without_jpeg(self):
        (self.export_root / "empty").mkdir()
        with patch.object(photos_module.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                photos_module.import_batch("empty")
        run.assert_not_called()

    def test_rejects_traversing_batch_name(self):
        with patch.object(photos_module.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                photos_module.import_batch("../evil")
        run.assert_not_called()

    def test_rejects_invalid_album_name(self):
        with patch.object(photos_module.subprocess, "run") as run:
            for album in ("", "   ", "a\nb", 5, "x" * 121):
                with self.subTest(album=album), self.assertRaises(ValueError):
                    photos_module.import_batch("trip08", album=album)
        run.assert_not_called()


class AppleScriptTests(unittest.TestCase):
    def test_script_exists_and_uses_run_argv(self):
        script = photos_module.SCRIPT_PATH
        self.assertTrue(script.is_file(), f"{script} がありません")
        source = script.read_text(encoding="utf-8")
        self.assertIn("on run argv", source)
        self.assertIn("make new album named albumName", source)


if __name__ == "__main__":
    unittest.main()
