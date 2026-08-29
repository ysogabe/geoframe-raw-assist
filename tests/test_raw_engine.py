"""現像エンジンの検証（サンプル画像に依存しない範囲）。"""

import os
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from local_raw import engine
from local_raw.engine import (
    RawDevelopmentError,
    _apply_highlight_recovery_array,
    _apply_shadow_lift_array,
    _apply_tone_array,
    _params_hash,
    _resolve_save_kwargs,
    _save_developed_jpeg,
    _save_recipe,
    _shadow_lift_table,
    _temp_path,
    _resize_array,
    develop_raw,
)
from local_raw.presets import (
    ADJUSTMENT_KEYS,
    ADJUSTMENT_RANGES,
    DEFAULT_PRESET,
    NOISE_REDUCTION_CHOICES,
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
    def test_has_eleven_presets(self):
        self.assertEqual(len(PRESETS), 11)

    def test_contains_rescue_presets(self):
        """暗部・逆光の救済プリセット（契約 §5）。UI は PRESETS の順序で並ぶ。"""
        for name in ("highlight_rescue", "shadow_rescue", "backlit_rescue"):
            with self.subTest(preset=name):
                self.assertIn(name, PRESETS)
        order = list(PRESETS)
        self.assertEqual(
            order[order.index("highlight_rescue") : order.index("highlight_rescue") + 3],
            ["highlight_rescue", "shadow_rescue", "backlit_rescue"],
        )

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

    def test_preset_noise_reduction_is_a_valid_choice(self):
        for name, preset in PRESETS.items():
            with self.subTest(preset=name):
                self.assertIn(
                    preset["adjustments"]["noise_reduction"], NOISE_REDUCTION_CHOICES
                )

    def test_preset_infos_matches_contract_shape(self):
        infos = preset_infos()
        self.assertEqual(len(infos), len(PRESETS))
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
            "source_root": "/Volumes/CARD-A/DCIM/100MSDCF",
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

    def test_changes_with_source_root(self):
        """同じ capture_id・同じプリセットでも、フォルダが違えばキャッシュキーは別。

        capture_id はカメラの連番なのでカードをまたぐと衝突する。ここが同一だと
        別フォルダの写真に対して他人の現像結果が cached:true で返る。
        """
        baseline = self._hash()
        self.assertNotEqual(baseline, self._hash(source_root="/Volumes/CARD-B/DCIM/100MSDCF"))
        # 末尾スラッシュのような表記揺れも別キーになる（解決済みパスを渡す前提）。
        self.assertNotEqual(baseline, self._hash(source_root="/Volumes/CARD-A/DCIM/100MSDCF/"))

    def test_is_stable_for_the_same_source_root(self):
        self.assertEqual(
            self._hash(source_root="/Volumes/CARD-B/DCIM/100MSDCF"),
            self._hash(source_root="/Volumes/CARD-B/DCIM/100MSDCF"),
        )

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


class FloatPipelineTests(unittest.TestCase):
    class FakeRaw:
        camera_whitebalance = [2.0, 1.0, 1.5, 1.0]

    def test_shadow_lift_keeps_fractional_values_before_jpeg_quantization(self):
        source = np.array([[[0.1234, 0.2345, 0.3456]]], dtype=np.float32)
        lifted = _apply_shadow_lift_array(source, 50)
        self.assertEqual(lifted.dtype, np.float32)
        self.assertTrue(np.all(lifted >= source))
        self.assertTrue(np.all(lifted <= 1.0))
        self.assertNotEqual(float(lifted[0, 0, 0]), round(float(lifted[0, 0, 0]) * 255) / 255)

    def test_tone_zero_saturation_is_rec709_monochrome(self):
        source = np.array([[[0.1, 0.4, 0.8]]], dtype=np.float32)
        adjustments = resolve_adjustments(DEFAULT_PRESET, {"saturation": 0.0})
        toned = _apply_tone_array(source, adjustments)
        self.assertEqual(toned.dtype, np.float32)
        self.assertAlmostEqual(float(toned[0, 0, 0]), float(toned[0, 0, 1]), places=6)
        self.assertAlmostEqual(float(toned[0, 0, 1]), float(toned[0, 0, 2]), places=6)

    def test_tone_sharpness_does_not_amplify_channel_differences(self):
        """シャープネスは色差ではなく輝度差を強める。"""
        source = np.zeros((9, 9, 3), dtype=np.float32)
        source[:, :4] = [0.22, 0.34, 0.46]
        source[:, 4:] = [0.58, 0.47, 0.36]
        adjustments = resolve_adjustments(
            DEFAULT_PRESET,
            {
                "brightness": 1.0,
                "contrast": 1.0,
                "saturation": 1.0,
                "sharpness": 1.5,
            },
        )
        toned = _apply_tone_array(source, adjustments)
        # クリップしない値域なので、輝度差を全チャンネルへ同量加えた結果、各色差は
        # シャープネスの前後で不変であるべき（RGB別unsharpならここが変わる）。
        np.testing.assert_allclose(
            toned[..., 0] - toned[..., 1], source[..., 0] - source[..., 1], atol=1e-6
        )
        np.testing.assert_allclose(
            toned[..., 1] - toned[..., 2], source[..., 1] - source[..., 2], atol=1e-6
        )

    def test_raw_postprocess_uses_single_exposure_stage_and_16bit(self):
        adjustments = resolve_adjustments(DEFAULT_PRESET, {"exposure": 1.0, "highlight_recovery": 100})
        params = engine._postprocess_params(self.FakeRaw(), adjustments, False)
        self.assertEqual(params["output_bps"], 16)
        self.assertEqual(params["exp_shift"], 1.0)
        self.assertEqual(params["bright"], 2.0)
        self.assertNotIn("exp_preserve_highlights", params)
        self.assertNotIn("highlight_mode", params)

    def test_highlight_recovery_changes_near_white_and_overrange_values(self):
        """rawpy の露出段ではなくfloat32段で白飛び付近を圧縮する。"""
        source = np.full((8, 8, 3), 0.45, dtype=np.float32)
        source[2:6, 2:6] = [0.96, 1.0, 1.2]

        without_recovery = _apply_highlight_recovery_array(source, 0)
        with_recovery = _apply_highlight_recovery_array(source, 100)

        self.assertEqual(with_recovery.dtype, np.float32)
        np.testing.assert_array_equal(without_recovery, source)
        self.assertGreater(
            float(np.max(np.abs(with_recovery - without_recovery))),
            0.02,
        )
        self.assertLess(float(with_recovery[3, 3, 0]), float(without_recovery[3, 3, 0]))
        self.assertLess(float(with_recovery[3, 3, 2]), 1.0)
        np.testing.assert_array_equal(with_recovery[:2, :2], source[:2, :2])

    def test_preview_keeps_full_demosaic_resolution(self):
        """プレビューでも先にhalf_sizeへ落とさず、最後に1600pxへ縮小する。"""
        adjustments = resolve_adjustments(DEFAULT_PRESET)
        params = engine._postprocess_params(self.FakeRaw(), adjustments, True)
        self.assertFalse(params["half_size"])

    def test_minor_resize_crops_instead_of_averaging(self):
        source = np.arange(100 * 102 * 3, dtype=np.float32).reshape(100, 102, 3)
        result = _resize_array(source, 100)
        self.assertEqual(result.shape, (98, 100, 3))
        np.testing.assert_array_equal(result, source[1:99, 1:101])

    def test_preview_and_full_resize_share_the_same_source_frame(self):
        """6024→6000の微小クロップをプレビューでも先に共有する。"""
        height, width = 4000, 6024
        source = np.empty((height, width, 3), dtype=np.float32)
        source[..., 0] = np.arange(width, dtype=np.float32)[None, :]
        source[..., 1] = np.arange(height, dtype=np.float32)[:, None]
        source[..., 2] = 1.0

        full = _resize_array(source, 6000)
        preview = _resize_array(source, 1600)

        self.assertEqual(full.shape, (3984, 6000, 3))
        self.assertEqual(float(full[0, 0, 0]), 12.0)
        self.assertEqual(float(full[-1, -1, 0]), 6011.0)

        # プレビューを元画像から直接縮小した結果ではなく、同じ先行クロップ済み
        # フレームを縮小した結果と一致することを確認する。
        expected_preview = _resize_array(full, 1600)
        np.testing.assert_allclose(preview, expected_preview, rtol=0.0, atol=1e-6)


class TempPathTests(unittest.TestCase):
    """修正1: 同一 params_hash の同時リクエストで一時ファイル名が衝突しない。"""

    def test_encodes_pid_and_a_per_call_unique_component(self):
        result = _temp_path(Path("/outputs/DSC00001-neutral_recovery-abc123.jpg"), "tmp.jpg")
        self.assertRegex(
            result.name,
            rf"^DSC00001-neutral_recovery-abc123\.{os.getpid()}-[0-9a-f]{{32}}\.tmp\.jpg$",
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

    def test_repeated_call_on_same_thread_returns_distinct_paths(self):
        # 呼び出しごとに UUID を振るので、同一スレッド内で連続して呼んでも別のパスになる。
        # （旧実装は同一スレッドで同じパスを返しており、それはスレッドID再利用で衝突し得る
        #   依存だった。呼び出し元はどちらも「一時パスへ書いて os.replace する」の1回完結なので、
        #   同一スレッド内の安定性に依存したロジックは無い。）
        path = Path("/outputs/DSC00001-neutral_recovery-abc123.jpg")
        self.assertNotEqual(_temp_path(path, "tmp.jpg"), _temp_path(path, "tmp.jpg"))


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


class SaveRecipeTests(unittest.TestCase):
    """修正4: _save_recipe も兄弟の _save_developed_jpeg と同じく一時ファイルを残さない。"""

    RECIPE = {"capture_id": "DSC00001", "preset": "neutral_recovery", "adjustments": {}}

    def test_saves_and_leaves_no_tmp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            recipe_path = Path(directory) / "DSC00001-neutral_recovery-abc123.json"
            _save_recipe(recipe_path, self.RECIPE)
            self.assertTrue(recipe_path.is_file())
            self.assertEqual(list(Path(directory).glob("*tmp.json")), [])

    def test_cleans_tmp_when_replace_fails(self):
        """一時ファイルが**既に書けている**状態で落とすのが要点。

        write_text を失敗させると一時ファイルがそもそも作られず、掃除の有無に関わらず
        テストが通ってしまう（修正前のコードでも緑になる）。os.replace を失敗させれば
        一時ファイルが実在する状態で例外が飛ぶので、finally の掃除だけを見られる。
        """
        with tempfile.TemporaryDirectory() as directory:
            recipe_path = Path(directory) / "DSC00001-neutral_recovery-abc123.json"
            with mock.patch.object(engine.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    _save_recipe(recipe_path, self.RECIPE)
            self.assertFalse(recipe_path.exists(), "置換に失敗したのだから本ファイルは出来ない")
            self.assertEqual(
                list(Path(directory).glob("*tmp.json")), [], "例外経路でも一時ファイルが残らない"
            )


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
