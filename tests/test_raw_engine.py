"""現像エンジンの検証（サンプル画像に依存しない範囲）。"""

import os
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from local_raw import engine
from local_raw.engine import (
    RawDevelopmentError,
    _params_hash,
    _resolve_save_kwargs,
    _save_developed_jpeg,
    _shadow_lift_table,
    _temp_path,
    develop_raw,
)
from local_raw.presets import (
    ADJUSTMENT_KEYS,
    ADJUSTMENT_RANGES,
    DEFAULT_PRESET,
    PRESETS,
    preset_infos,
    resolve_adjustments,
)


class RawEngineValidationTests(unittest.TestCase):
    """Wave 0 から引き継いだ入力検証（回帰確認）。"""

    def test_rejects_path_traversal(self):
        with self.assertRaises(RawDevelopmentError):
            develop_raw("../DSC02943", "nightscape")

    def test_rejects_unknown_preset(self):
        with self.assertRaises(RawDevelopmentError):
            develop_raw("DSC02943", "unknown")

    def test_rejects_out_of_range_size(self):
        with self.assertRaises(RawDevelopmentError):
            develop_raw("DSC02943", "nightscape", max_edge=800)


class PresetDefinitionTests(unittest.TestCase):
    def test_has_nine_presets(self):
        self.assertEqual(len(PRESETS), 9)

    def test_default_preset_exists(self):
        self.assertIn(DEFAULT_PRESET, PRESETS)

    def test_every_preset_has_all_adjustment_keys(self):
        for name, preset in PRESETS.items():
            with self.subTest(preset=name):
                self.assertIn("label", preset)
                self.assertIn("description", preset)
                self.assertTrue(preset["description"], "description は空にしない")
                self.assertEqual(set(preset["adjustments"]), set(ADJUSTMENT_KEYS))

    def test_preset_values_are_within_contract_ranges(self):
        for name, preset in PRESETS.items():
            for key, (minimum, maximum) in ADJUSTMENT_RANGES.items():
                with self.subTest(preset=name, key=key):
                    self.assertGreaterEqual(preset["adjustments"][key], minimum)
                    self.assertLessEqual(preset["adjustments"][key], maximum)

    def test_preset_infos_matches_contract_shape(self):
        infos = preset_infos()
        self.assertEqual(len(infos), 9)
        for info in infos:
            self.assertEqual(set(info), {"name", "label", "description", "adjustments"})
            self.assertEqual(set(info["adjustments"]), set(ADJUSTMENT_KEYS))

    def test_preset_infos_is_a_copy(self):
        infos = preset_infos()
        infos[0]["adjustments"]["contrast"] = 99.0
        self.assertNotEqual(PRESETS[infos[0]["name"]]["adjustments"]["contrast"], 99.0)


class ResolveAdjustmentsTests(unittest.TestCase):
    def test_returns_all_keys_without_overrides(self):
        resolved = resolve_adjustments("nightscape")
        self.assertEqual(set(resolved), set(ADJUSTMENT_KEYS))
        self.assertEqual(resolved["noise_reduction"], "full")

    def test_merges_partial_overrides(self):
        resolved = resolve_adjustments("nightscape", {"contrast": 1.25})
        self.assertEqual(resolved["contrast"], 1.25)
        # 未指定キーはプリセット既定値のまま
        self.assertEqual(resolved["shadow_lift"], PRESETS["nightscape"]["adjustments"]["shadow_lift"])

    def test_does_not_mutate_preset(self):
        before = dict(PRESETS["nightscape"]["adjustments"])
        resolve_adjustments("nightscape", {"contrast": 1.4})
        self.assertEqual(PRESETS["nightscape"]["adjustments"], before)

    def test_clamps_out_of_range_values(self):
        resolved = resolve_adjustments(DEFAULT_PRESET, {"exposure": 9.0, "saturation": -3.0})
        self.assertEqual(resolved["exposure"], 2.0)
        self.assertEqual(resolved["saturation"], 0.0)

    def test_clamps_each_numeric_key_at_both_ends(self):
        for key, (minimum, maximum) in ADJUSTMENT_RANGES.items():
            with self.subTest(key=key):
                self.assertEqual(resolve_adjustments(DEFAULT_PRESET, {key: -1e6})[key], minimum)
                self.assertEqual(resolve_adjustments(DEFAULT_PRESET, {key: 1e6})[key], maximum)

    def test_rejects_unknown_key(self):
        with self.assertRaises(RawDevelopmentError):
            resolve_adjustments(DEFAULT_PRESET, {"gamma": 1.0})

    def test_rejects_unknown_preset(self):
        with self.assertRaises(RawDevelopmentError):
            resolve_adjustments("nope")

    def test_rejects_non_numeric_value(self):
        with self.assertRaises(RawDevelopmentError):
            resolve_adjustments(DEFAULT_PRESET, {"contrast": "1.2"})

    def test_rejects_boolean_value(self):
        # bool は int のサブクラスなので、素通しすると True が 1.0 になってしまう
        with self.assertRaises(RawDevelopmentError):
            resolve_adjustments(DEFAULT_PRESET, {"exposure": True})

    def test_rejects_non_finite_value(self):
        with self.assertRaises(RawDevelopmentError):
            resolve_adjustments(DEFAULT_PRESET, {"exposure": float("nan")})

    def test_rejects_invalid_noise_reduction(self):
        with self.assertRaises(RawDevelopmentError):
            resolve_adjustments(DEFAULT_PRESET, {"noise_reduction": "medium"})

    def test_accepts_every_noise_reduction_choice(self):
        for choice in ("off", "light", "full"):
            with self.subTest(choice=choice):
                resolved = resolve_adjustments(DEFAULT_PRESET, {"noise_reduction": choice})
                self.assertEqual(resolved["noise_reduction"], choice)

    def test_rejects_non_dict_overrides(self):
        with self.assertRaises(RawDevelopmentError):
            resolve_adjustments(DEFAULT_PRESET, [("contrast", 1.2)])

    def test_numbers_are_normalised_to_float(self):
        resolved = resolve_adjustments(DEFAULT_PRESET, {"shadow_lift": 20})
        self.assertIsInstance(resolved["shadow_lift"], float)


class ParamsHashTests(unittest.TestCase):
    def _hash(self, **kwargs):
        base = {
            "adjustments": resolve_adjustments(DEFAULT_PRESET),
            "preset_name": DEFAULT_PRESET,
            "max_edge": 6000,
            "preview": False,
            "lens_applied": True,
        }
        base.update(kwargs)
        return _params_hash(**base)

    def test_is_ten_characters(self):
        self.assertEqual(len(self._hash()), 10)

    def test_is_stable_for_identical_input(self):
        self.assertEqual(self._hash(), self._hash())

    def test_changes_when_any_single_adjustment_changes(self):
        baseline = self._hash()
        for key in ADJUSTMENT_KEYS:
            override = "full" if key == "noise_reduction" else 0.9
            if key == "noise_reduction" and PRESETS[DEFAULT_PRESET]["adjustments"][key] == "full":
                override = "off"
            adjustments = resolve_adjustments(DEFAULT_PRESET, {key: override})
            with self.subTest(key=key):
                self.assertNotEqual(
                    baseline, self._hash(adjustments=adjustments), f"{key} がハッシュに反映されていない"
                )

    def test_changes_with_preset_max_edge_preview_and_lens(self):
        baseline = self._hash()
        self.assertNotEqual(baseline, self._hash(preset_name="nightscape"))
        self.assertNotEqual(baseline, self._hash(max_edge=3000))
        self.assertNotEqual(baseline, self._hash(preview=True))
        self.assertNotEqual(baseline, self._hash(lens_applied=False))

    def test_int_and_float_adjustments_hash_identically(self):
        # resolve_adjustments が float 正規化するので 0 と 0.0 は同じキーになる
        via_int = resolve_adjustments(DEFAULT_PRESET, {"wb_temp": 0})
        via_float = resolve_adjustments(DEFAULT_PRESET, {"wb_temp": 0.0})
        self.assertEqual(self._hash(adjustments=via_int), self._hash(adjustments=via_float))


class ShadowLiftTableTests(unittest.TestCase):
    def test_returns_none_when_disabled(self):
        self.assertIsNone(_shadow_lift_table(0))

    def test_has_256_entries_within_byte_range(self):
        table = _shadow_lift_table(100)
        self.assertEqual(len(table), 256)
        self.assertTrue(all(0 <= value <= 255 for value in table))

    def test_lifts_shadows_more_than_highlights(self):
        table = _shadow_lift_table(50)
        self.assertGreater(table[64] - 64, table[224] - 224)

    def test_endpoints_are_preserved(self):
        table = _shadow_lift_table(100)
        self.assertEqual(table[0], 0)
        self.assertEqual(table[255], 255)

    def test_is_monotonic(self):
        table = _shadow_lift_table(100)
        self.assertEqual(table, sorted(table))

    def test_stronger_lift_brightens_more(self):
        self.assertGreater(_shadow_lift_table(100)[85], _shadow_lift_table(25)[85])


class TempPathTests(unittest.TestCase):
    """修正1: 同一 params_hash の同時リクエストで一時ファイル名が衝突しない。"""

    def test_encodes_pid_and_current_thread_ident(self):
        result = _temp_path(Path("/outputs/DSC00001-neutral_recovery-abc123.jpg"), "tmp.jpg")
        self.assertEqual(
            result.name,
            f"DSC00001-neutral_recovery-abc123.{os.getpid()}-{threading.get_ident()}.tmp.jpg",
        )

    def test_differs_across_concurrent_threads(self):
        path = Path("/outputs/DSC00001-neutral_recovery-abc123.jpg")
        results: list[Path] = []
        lock = threading.Lock()

        def worker():
            value = _temp_path(path, "tmp.jpg")
            with lock:
                results.append(value)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), 4)
        self.assertEqual(len(set(results)), 4, "同時に呼ばれた別スレッドの一時パスは全て別々であるべき")

    def test_repeated_call_on_same_thread_is_stable(self):
        # 同一スレッド内の呼び出しは同じ pid-tid になる(衝突対策はスレッド単位で十分)。
        path = Path("/outputs/DSC00001-neutral_recovery-abc123.jpg")
        self.assertEqual(_temp_path(path, "tmp.jpg"), _temp_path(path, "tmp.jpg"))


class SaveDevelopedJpegTests(unittest.TestCase):
    """修正1(一時ファイル衝突)・修正2(EXIF保存失敗フォールバックとtmp掃除)。"""

    def _image(self, fill=(120, 60, 30)):
        return Image.new("RGB", (16, 16), fill)

    def test_saves_and_leaves_no_tmp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "DSC00001-neutral_recovery-abc123.jpg"
            exif_inherited = _save_developed_jpeg(self._image(), output_path, {"quality": 90}, False)
            self.assertFalse(exif_inherited)
            self.assertTrue(output_path.is_file())
            self.assertEqual(list(Path(directory).glob("*tmp.jpg")), [])

    def test_falls_back_to_no_exif_when_exif_save_fails_and_cleans_up_tmp(self):
        """exif.tobytes() 以外の場所(image.save 内)で起きる EXIF 関連の失敗も拾えること。

        Pillow の EXIF サイズ超過検知は image.save() 呼び出し中に起きるため、
        再現には save() そのものを失敗させる必要がある。
        """
        original_save = Image.Image.save
        calls: list[dict] = []

        def flaky_save(self, fp, format=None, **kwargs):
            calls.append(dict(kwargs))
            if "exif" in kwargs:
                raise struct.error("simulated oversized/invalid EXIF payload")
            return original_save(self, fp, format, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "DSC00001-neutral_recovery-abc123.jpg"
            with mock.patch.object(Image.Image, "save", flaky_save):
                exif_inherited = _save_developed_jpeg(
                    self._image(), output_path, {"quality": 90, "exif": b"bogus-exif-bytes"}, True
                )

            self.assertFalse(exif_inherited, "exif付き保存が失敗したら exif_inherited は False に落ちる")
            self.assertEqual(len(calls), 2, "1回目(exif付き)失敗後、exif無しで1回だけ再保存する")
            self.assertIn("exif", calls[0])
            self.assertNotIn("exif", calls[1])
            self.assertTrue(output_path.is_file())
            self.assertEqual(list(Path(directory).glob("*tmp.jpg")), [], "成功経路で一時ファイルが残らない")

    def test_raises_raw_development_error_and_cleans_tmp_when_fallback_also_fails(self):
        def always_fail(self, fp, format=None, **kwargs):
            raise struct.error("still broken even without exif")

        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "DSC00001-neutral_recovery-abc123.jpg"
            with mock.patch.object(Image.Image, "save", always_fail):
                with self.assertRaises(RawDevelopmentError):
                    _save_developed_jpeg(
                        self._image(), output_path, {"quality": 90, "exif": b"bogus"}, True
                    )
            self.assertFalse(output_path.is_file())
            self.assertEqual(
                list(Path(directory).glob("*tmp.jpg")), [], "例外経路でも一時ファイルが残らない"
            )

    def test_raises_raw_development_error_when_save_fails_without_exif(self):
        def always_fail(self, fp, format=None, **kwargs):
            raise OSError("disk full")

        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "DSC00001-neutral_recovery-abc123.jpg"
            with mock.patch.object(Image.Image, "save", always_fail):
                with self.assertRaises(RawDevelopmentError):
                    _save_developed_jpeg(self._image(), output_path, {"quality": 90}, False)
            self.assertEqual(list(Path(directory).glob("*tmp.jpg")), [])

    def test_concurrent_saves_to_the_same_output_path_both_succeed(self):
        """同一 params_hash の同時現像(ThreadingHTTPServer での二重クリック)の回帰確認。

        一時ファイル名が衝突していた頃は、片方の書き込み中の一時ファイルをもう片方の
        os.replace が拾ってしまい、破損 JPEG が cached:true として恒久化し得た。
        """
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "DSC00001-neutral_recovery-abc123.jpg"
            barrier = threading.Barrier(2)
            errors: list[Exception] = []
            lock = threading.Lock()

            def worker(fill):
                image = self._image((fill, fill, fill))
                barrier.wait()
                try:
                    _save_developed_jpeg(image, output_path, {"quality": 90}, False)
                except Exception as error:  # pragma: no cover - failure path asserted via `errors`
                    with lock:
                        errors.append(error)

            threads = [threading.Thread(target=worker, args=(fill,)) for fill in (10, 220)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            with Image.open(output_path) as result:
                result.load()  # 破損していれば例外になる
                pixel = result.getpixel((8, 8))
            # 圧縮誤差はあるが、2色のどちらか一方をほぼそのまま反映しているはず(混ざった破損ではない)。
            self.assertTrue(
                all(abs(channel - 10) < 20 for channel in pixel)
                or all(abs(channel - 220) < 20 for channel in pixel),
                f"破損した可能性のあるピクセル値: {pixel}",
            )
            self.assertEqual(list(Path(directory).glob("*tmp.jpg")), [])


class ResolveSaveKwargsTests(unittest.TestCase):
    """修正2: build_inherited_exif の失敗捕捉を Exception まで広げる(struct.error 含む)。"""

    def test_no_pair_jpg_skips_exif_and_has_no_exif_key(self):
        save_kwargs, exif_inherited = _resolve_save_kwargs(Path("/nonexistent/DSC00001.JPG"), 100, 100)
        self.assertFalse(exif_inherited)
        self.assertNotIn("exif", save_kwargs)

    def test_falls_back_when_build_inherited_exif_raises_struct_error(self):
        """exif.tobytes() が投げる struct.error は元々 except 節に含まれておらず漏れていた。"""
        with tempfile.TemporaryDirectory() as directory:
            source_jpg = Path(directory) / "DSC00001.JPG"
            source_jpg.write_bytes(b"not-decoded-because-build_inherited_exif-is-mocked")
            with mock.patch.object(
                engine, "build_inherited_exif", side_effect=struct.error("bad pack format")
            ):
                save_kwargs, exif_inherited = _resolve_save_kwargs(source_jpg, 100, 100)
        self.assertFalse(exif_inherited)
        self.assertNotIn("exif", save_kwargs)

    def test_still_falls_back_on_previously_handled_exceptions(self):
        for exc in (OSError("nope"), ValueError("bad"), KeyError("missing")):
            with self.subTest(exc=type(exc).__name__):
                with tempfile.TemporaryDirectory() as directory:
                    source_jpg = Path(directory) / "DSC00001.JPG"
                    source_jpg.write_bytes(b"x")
                    with mock.patch.object(engine, "build_inherited_exif", side_effect=exc):
                        save_kwargs, exif_inherited = _resolve_save_kwargs(source_jpg, 100, 100)
                self.assertFalse(exif_inherited)
                self.assertNotIn("exif", save_kwargs)

    def test_succeeds_when_build_inherited_exif_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            source_jpg = Path(directory) / "DSC00001.JPG"
            source_jpg.write_bytes(b"x")
            with mock.patch.object(engine, "build_inherited_exif", return_value=b"exif-bytes"):
                save_kwargs, exif_inherited = _resolve_save_kwargs(source_jpg, 100, 100)
        self.assertTrue(exif_inherited)
        self.assertEqual(save_kwargs["exif"], b"exif-bytes")


if __name__ == "__main__":
    unittest.main()
