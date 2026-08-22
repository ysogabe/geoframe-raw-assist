"""レンズ補正の解決とフォールバックの検証。"""

import types
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

#: ビネット補正が効く、lensfun 同梱DBに実在するレンズ（追加DBが無くても解決する）。
BUNDLED_VIGNETTING_EXIF = {
    "make": "SONY",
    "model": "ILCE-6400",
    "lens_model": "E 55-210mm F4.5-6.3 OSS",
    "focal_length": 55.0,
    "f_number": 4.5,
}

#: 実効ゲインの上限。これを超えたら「補正」ではなく破綻とみなす。
#:
#: 正しい pixel_format での実測最大ゲインは ILCE-6400 で
#:   Sigma 30mm f/1.4 @f/1.4        → 2.09（四隅）
#:   Sony E 55-210mm @55mm f/4.5    → 2.35（四隅）
#: 歪曲補正（autoscale 込み）の Lanczos 再標本化で縁のリンギングがわずかに乗るため、
#: apply_lens_correction 通過後の全画素最大は 2.91 まで上がる。他レンズの余地を
#: 見て 4.0 を上限とした。pixel_format 取り違え時は 1e24 オーダーまで飛ぶので、
#: この閾値なら正常値は通し異常値は必ず落ちる。
MAX_PLAUSIBLE_VIGNETTING_GAIN = 4.0

#: 補正が実際に効いていることの下限。ゲインが恒等（全画素 1.0）だと有限・有界・
#: 左右対称のいずれも自明に成立してしまい、テストが空振りする。
MIN_NONTRIVIAL_VIGNETTING_GAIN = 1.05

#: ゲイン測定用の塗りつぶし値（uint16）。65535 の 1/8 なので、最大ゲインが
#: かかっても 1.0 に飽和せず、クリップではなくゲインそのものを測れる。
GAIN_PROBE_FILL = 8192

lensfunpy_available = lens.lensfunpy is not None
cv2_available = lens.cv2 is not None


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


class _FakeModifyFlags:
    VIGNETTING = 1
    DISTORTION = 2
    SCALE = 4


class _NonFiniteModifier:
    """ビネット補正の結果に NaN を書き込む偽 Modifier。

    `apply_color_modification` が非有限値を返した場合の防御を、DB のデータに
    依存せず直接検証するために使う。
    """

    #: apply_geometry_distortion の戻り値（None なら歪曲補正なし）
    distortion_map: np.ndarray | None = None

    def __init__(self, *args, **kwargs):
        pass

    def initialize(self, *args, **kwargs):
        pass

    def apply_color_modification(self, working):
        working[:] = np.nan
        return True

    def apply_geometry_distortion(self):
        return self.distortion_map


def _fake_lensfunpy(modifier_cls):
    return types.SimpleNamespace(Modifier=modifier_cls, ModifyFlags=_FakeModifyFlags)


def _applicable_stub_resolution(lens_obj=None):
    """Modifier を差し替える前提の、最低限埋めた LensResolution。

    既定の lens は「歪曲較正あり」の体（calib_distortion が非空）にして、
    apply_lens_correction が apply_geometry_distortion() まで到達するようにする。
    """
    if lens_obj is None:
        lens_obj = types.SimpleNamespace(calib_distortion=[object()])
    return LensResolution(
        applicable=True,
        lens_model="Fake Lens",
        lens_name="Fake / Lens",
        focal_length=55.0,
        aperture=4.5,
        camera=types.SimpleNamespace(crop_factor=1.5),
        lens=lens_obj,
    )


@unittest.skipUnless(lensfunpy_available, "lensfunpy 未導入")
class VignettingGainTests(unittest.TestCase):
    """ビネット補正の実効ゲインが健全であること（回帰: pixel_format の取り違え）。

    `Modifier.initialize()` の `pixel_format` 既定値は np.uint8 だが、
    `apply_color_modification()` に渡すのは float32 のバッファなので、明示的に
    合わせないと lensfun が float32 を uint8 として走査し、**各行の左1/4だけ**に
    桁外れのゲイン（実測 3.4e38 / NaN / 負値）が乗る。
    """

    def _effective_gain(self, exif):
        """一様配列に補正をかけ、(実効ゲイン配列, correction) を返す。"""
        resolution = probe_lens(exif)
        if not resolution.applicable:
            self.skipTest(f"レンズ未解決のためスキップ: {resolution.reason}")

        rgb = np.full((400, 600, 3), GAIN_PROBE_FILL, dtype=np.uint16)
        corrected, correction = apply_lens_correction(rgb, resolution)
        self.assertTrue(correction["applied"], correction)
        self.assertIn("vignetting", correction["flags"], correction)
        gain = corrected[:, :, 0].astype(np.float64) / GAIN_PROBE_FILL
        return gain, correction

    def test_gain_is_finite_everywhere(self):
        gain, _ = self._effective_gain(BUNDLED_VIGNETTING_EXIF)
        self.assertTrue(np.isfinite(gain).all(), "非有限のゲインが発生している")

    def test_gain_stays_in_plausible_range(self):
        gain, _ = self._effective_gain(BUNDLED_VIGNETTING_EXIF)
        height, width = gain.shape
        self.assertLess(float(gain.max()), MAX_PLAUSIBLE_VIGNETTING_GAIN)
        self.assertGreaterEqual(float(gain.min()), 0.0)
        # 中心は減光しないので素通し。
        self.assertAlmostEqual(float(gain[height // 2, width // 2]), 1.0, places=2)
        # 恒等ゲインだと他の assert が全部自明に通ってしまうので、効いていることを確かめる。
        self.assertGreater(float(gain.max()), MIN_NONTRIVIAL_VIGNETTING_GAIN)

    def test_gain_is_left_right_symmetric(self):
        """本バグは左25%だけを壊すので、左右対称性がそのまま検出器になる。

        比較列は 1/8 だけ内側を使う（壊れた帯＝左25%の内側なので診断力は落ちない）。
        かつては画像端が歪曲補正の画像外参照で落ち込むための回避でもあったが、
        autoscale 導入で落ち込み自体は消えている。
        """
        gain, _ = self._effective_gain(BUNDLED_VIGNETTING_EXIF)
        height, width = gain.shape
        inset = width // 8
        left = gain[:, inset]
        right = gain[:, width - 1 - inset]
        np.testing.assert_allclose(left, right[::1], rtol=1e-3, atol=1e-3)

        row = gain[height // 2]
        self.assertAlmostEqual(float(row[inset]), float(row[width - 1 - inset]), places=3)


@unittest.skipUnless(lensfunpy_available, "lensfunpy 未導入")
@unittest.skipUnless(cv2_available, "OpenCV 未導入")
class DistortionBlackBorderTests(unittest.TestCase):
    """歪曲補正後に黒縁が残らないこと（回帰: ModifyFlags.SCALE の欠落）。

    SCALE フラグが無いと `initialize()` の `scale=0.0` 既定（autoscale 要求）が
    実行されず、糸巻き型歪曲（望遠側）の補正 remap が入力フレーム外を参照して
    cv2.remap の既定 BORDER_CONSTANT の黒が縁に残る（実測: Sony E 55-210mm で
    画素の約 2% が黒縁・Lanczos リンギング）。autoscale があれば画像外参照は 0。
    """

    def test_no_black_border_after_distortion(self):
        resolution = probe_lens(BUNDLED_VIGNETTING_EXIF)
        if not resolution.applicable:
            self.skipTest(f"レンズ未解決のためスキップ: {resolution.reason}")

        rgb = np.full((400, 600, 3), GAIN_PROBE_FILL, dtype=np.uint16)
        corrected, correction = apply_lens_correction(rgb, resolution)
        self.assertTrue(correction["applied"], correction)
        self.assertIn("distortion", correction["flags"], correction)

        gain = corrected[:, :, 0].astype(np.float64) / GAIN_PROBE_FILL
        height, width = gain.shape

        # 四隅と辺中央のサンプル点が黒（未定義領域）でないこと。一様塗りに
        # ビネット補正が乗るので実効ゲインは 1.0 付近以上になるはず。黒縁なら
        # ほぼ 0 に落ちるので、補間の揺れを見込んだ 0.9 を下限とする。
        probes = [
            (0, 0),
            (0, width - 1),
            (height - 1, 0),
            (height - 1, width - 1),
            (0, width // 2),
            (height - 1, width // 2),
            (height // 2, 0),
            (height // 2, width - 1),
        ]
        for y, x in probes:
            self.assertGreaterEqual(
                float(gain[y, x]), 0.9, f"画素 ({y}, {x}) が黒縁に食われている"
            )

        # 全画素で 0.5×fill 未満の暗部がゼロであること。黒縁そのもの（gain≈0）に
        # 加え、画像外の黒を巻き込んだ Lanczos リンギング（undershoot）も検出する。
        dark = int((gain < 0.5).sum())
        self.assertEqual(dark, 0, f"{dark} 画素が 0.5×fill 未満（黒縁またはリンギング）")


class UncalibratedDistortionGuardTests(unittest.TestCase):
    """歪曲較正の無いレンズに "distortion" フラグが偽って付かないこと。

    SCALE（autoscale）は幾何補正がひとつも無くても恒等マップを返すため、
    較正の有無（lens.calib_distortion）を見ずにマップを要求すると、未較正の
    レンズが「distortion 適用済み」として報告され、"lens_not_calibrated"
    フォールバックにも到達しなくなる。
    """

    def test_no_spurious_distortion_without_calibration(self):
        height, width = 32, 48
        identity_map = np.dstack(
            np.meshgrid(
                np.arange(width, dtype=np.float32), np.arange(height, dtype=np.float32)
            )
        )

        class _IdentityMapNoVignetting(_NonFiniteModifier):
            # SCALE が返しうる恒等マップ。ガードが効いていればここまで到達しない。
            distortion_map = identity_map

            def apply_color_modification(self, working):
                return False  # ビネット較正なし

        resolution = _applicable_stub_resolution(
            lens_obj=types.SimpleNamespace(calib_distortion=[])
        )
        rgb = np.full((height, width, 3), 100, dtype=np.uint8)
        with mock.patch.object(lens, "lensfunpy", _fake_lensfunpy(_IdentityMapNoVignetting)):
            corrected, correction = apply_lens_correction(rgb, resolution)

        self.assertIs(corrected, rgb)
        self.assertFalse(correction["applied"])
        self.assertEqual(correction["reason"], "lens_not_calibrated")


class NonFiniteVignettingTests(unittest.TestCase):
    """非有限値が出たらビネット補正を破棄する（安全網）。

    np.clip は NaN を素通しする（np.clip(nan, 0, 1) は nan）ので、クリップだけでは
    壊れた画像の出力を防げない。
    """

    def test_nan_result_discards_vignetting(self):
        rgb = np.full((32, 48, 3), 100, dtype=np.uint8)
        with mock.patch.object(lens, "lensfunpy", _fake_lensfunpy(_NonFiniteModifier)):
            corrected, correction = apply_lens_correction(rgb, _applicable_stub_resolution())

        # 歪曲補正も無いので適用フラグが空になり、無補正として返る。
        self.assertIs(corrected, rgb)
        self.assertFalse(correction["applied"])
        self.assertEqual(correction["reason"], "lens_not_calibrated")

    @unittest.skipUnless(cv2_available, "OpenCV 未導入")
    def test_nan_vignetting_does_not_block_distortion(self):
        """歪曲補正が効く場合でも、破棄されるのはビネットだけで NaN は残らない。"""
        height, width = 32, 48
        identity_map = np.dstack(
            np.meshgrid(
                np.arange(width, dtype=np.float32), np.arange(height, dtype=np.float32)
            )
        )

        class _WithDistortion(_NonFiniteModifier):
            distortion_map = identity_map

        rgb = np.full((height, width, 3), 100, dtype=np.uint8)
        with mock.patch.object(lens, "lensfunpy", _fake_lensfunpy(_WithDistortion)):
            corrected, correction = apply_lens_correction(rgb, _applicable_stub_resolution())

        self.assertTrue(correction["applied"], correction)
        self.assertEqual(correction["flags"], ["distortion"])
        self.assertNotIn("vignetting", correction["flags"])
        self.assertTrue(np.isfinite(corrected.astype(np.float64)).all())


if __name__ == "__main__":
    unittest.main()
