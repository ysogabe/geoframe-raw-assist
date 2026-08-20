"""ブラケット露出合成（Exposure Fusion）の検証。

- **実写ファイルには依存しない**。RAW デコードは `bracket._decode_frame` を差し替え、
  合成段（AlignMTB → Mertens → クリップ → トーン → 保存）だけを検証する。
- 合成の中身は「広いダイナミックレンジのシーンを2通りの露出で撮った」合成データで見る。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from local_raw import bracket as bracket_module
from local_raw import config as config_module
from local_raw import engine
from local_raw.bracket import MAX_FRAMES, MIN_FRAMES, _merge_frames, _merge_params_hash, merge_bracket
from local_raw.presets import DEFAULT_PRESET, RawDevelopmentError, resolve_adjustments

#: 合成対象の実在ブラケット（ユーザーの実データと同じ並び）。テストでは中身を使わない。
CAPTURES = ["DSC04510", "DSC04513", "DSC04514", "DSC04515", "DSC04516"]

HIGHLIGHT_LEVEL = 250  # `analysis.HIGHLIGHT_LEVEL` と同じ定義
SHADOW_LEVEL = 5  # `analysis.SHADOW_LEVEL` と同じ定義


def scene_radiance(width: int = 128, height: int = 96) -> np.ndarray:
    """ダイナミックレンジ 4 桁のシーンを「線形の放射輝度」として作る。

    横方向に 1e-4〜1.0 の対数ランプ。加えて縦方向へブロック状の濃淡を入れ、
    位置合わせ（MTB）が横縞だけの縮退した画像にならないようにする。
    """
    ramp = np.logspace(-4.0, 0.0, width, dtype=np.float32)
    radiance = np.repeat(ramp[None, :], height, axis=0)
    blocks = np.ones((height, width), dtype=np.float32)
    blocks[height // 4 : height // 2, : width // 3] *= 0.35
    blocks[height // 2 : 3 * height // 4, width // 2 :] *= 2.5
    return radiance * blocks


def expose(radiance: np.ndarray, gain: float) -> np.ndarray:
    """露出（ゲイン）を掛けて 8bit sRGB 相当へ焼き込む。1.0 を超える分は白飛びする。"""
    exposed = np.clip(radiance * gain, 0.0, 1.0) ** (1.0 / 2.2)
    gray = (exposed * 255.0 + 0.5).astype(np.uint8)
    return np.repeat(gray[:, :, None], 3, axis=2)


def apply_preset_tone(rgb: np.ndarray, preset_name: str = DEFAULT_PRESET) -> np.ndarray:
    """`bracket.merge_bracket` の第6段（トーン調整）だけを取り出して掛ける。

    合成の効果を比べるとき、比較相手のフレームにも同じトーンを掛けて条件を揃えるために使う。
    実装と同じ `engine` の関数を呼ぶので、トーン段の定義が変わってもテストは追随する。
    """
    resolved = resolve_adjustments(preset_name)
    image = Image.fromarray(rgb, mode="RGB")
    table = engine._shadow_lift_table(float(resolved["shadow_lift"]))
    if table is not None:
        image = image.point(table * 3)
    return np.asarray(engine._apply_tone(image, resolved), dtype=np.uint8)


def clip_metrics(rgb: np.ndarray) -> dict[str, float]:
    """`analysis.score_array` と同じ定義の白飛び率・黒つぶれ率・平均輝度。"""
    return {
        "highlight": float((rgb >= HIGHLIGHT_LEVEL).any(axis=2).mean()),
        "shadow": float((rgb <= SHADOW_LEVEL).all(axis=2).mean()),
        "mean": float(rgb.mean()),
    }


class BracketTestCase(unittest.TestCase):
    """tmpdir に現像元フォルダと `outputs/merged/` を作り、デコードを差し替える。"""

    #: 露出違いの2枚（暗い＝ハイライトが残る / 明るい＝暗部が残る）。
    DARK_GAIN = 0.2
    BRIGHT_GAIN = 8.0

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # macOS の /var → /private/var シンボリックリンク対策に resolve する。
        self.project_root = Path(temporary.name).resolve()
        self.source_root = self.project_root / "DCIM" / "100MSDCF"
        self.merged_root = self.project_root / "outputs" / "merged"
        self.source_root.mkdir(parents=True, exist_ok=True)
        self.merged_root.mkdir(parents=True, exist_ok=True)
        for capture_id in CAPTURES:
            (self.source_root / f"{capture_id}.ARW").write_bytes(f"raw-{capture_id}".encode())

        merged_patcher = mock.patch.object(bracket_module, "MERGED_ROOT", self.merged_root)
        merged_patcher.start()
        self.addCleanup(merged_patcher.stop)
        # 現像元フォルダはモジュール定数ではなく呼び出し時に解決されるので関数を差し替える。
        source_patcher = mock.patch.object(
            config_module, "source_root", return_value=self.source_root
        )
        source_patcher.start()
        self.addCleanup(source_patcher.stop)

        self.radiance = scene_radiance()
        self.decoded: list[str] = []
        decode_patcher = mock.patch.object(
            bracket_module, "_decode_frame", side_effect=self.fake_decode
        )
        self.fake_decode_mock = decode_patcher.start()
        self.addCleanup(decode_patcher.stop)

    def fake_decode(self, source_path: Path, adjustments: dict, preview: bool) -> np.ndarray:
        """撮影IDごとに違う露出のフレームを返す（rawpy は呼ばない）。

        先頭IDを最も明るく、以降を段階的に暗くする。実データの AEB と同じ向き
        （明るいフレームが暗部を、暗いフレームがハイライトを持つ）。
        """
        capture_id = source_path.stem
        self.decoded.append(capture_id)
        index = CAPTURES.index(capture_id) if capture_id in CAPTURES else 0
        step = (self.BRIGHT_GAIN / self.DARK_GAIN) ** (1.0 / max(1, len(CAPTURES) - 1))
        return expose(self.radiance, self.BRIGHT_GAIN / (step**index))

    def merged_array(self, result: dict) -> np.ndarray:
        with Image.open(self.merged_root / result["filename"]) as image:
            return np.asarray(image.convert("RGB"), dtype=np.uint8)

    def read_recipe(self, result: dict) -> dict:
        return json.loads(
            (self.merged_root / result["recipe_filename"]).read_text(encoding="utf-8")
        )


class FrameCountTests(BracketTestCase):
    def test_rejects_a_single_frame(self):
        with self.assertRaises(RawDevelopmentError):
            merge_bracket([CAPTURES[0]], DEFAULT_PRESET, preview=True)

    def test_accepts_two_frames(self):
        result = merge_bracket(CAPTURES[:2], DEFAULT_PRESET, preview=True)
        self.assertEqual(result["frames"], 2)
        self.assertTrue((self.merged_root / result["filename"]).is_file())

    def test_rejects_ten_frames(self):
        ten = [f"DSC0{4510 + offset}" for offset in range(10)]
        with self.assertRaises(RawDevelopmentError):
            merge_bracket(ten, DEFAULT_PRESET, preview=True)

    def test_boundaries_match_the_documented_range(self):
        self.assertEqual((MIN_FRAMES, MAX_FRAMES), (2, 9))

    def test_rejects_empty_list(self):
        with self.assertRaises(RawDevelopmentError):
            merge_bracket([], DEFAULT_PRESET, preview=True)

    def test_rejects_non_list(self):
        with self.assertRaises(RawDevelopmentError):
            merge_bracket("DSC04510", DEFAULT_PRESET, preview=True)


class CaptureIdValidationTests(BracketTestCase):
    def test_rejects_duplicate_capture_ids(self):
        with self.assertRaises(RawDevelopmentError):
            merge_bracket([CAPTURES[0], CAPTURES[0]], DEFAULT_PRESET, preview=True)

    def test_rejects_duplicates_that_differ_only_by_suffix(self):
        # `_validate_capture_id` は `.ARW` / `.JPG` を落とすので正規化後に重複する。
        with self.assertRaises(RawDevelopmentError):
            merge_bracket([CAPTURES[0], f"{CAPTURES[0]}.ARW"], DEFAULT_PRESET, preview=True)

    def test_rejects_unknown_capture_id(self):
        with self.assertRaises(RawDevelopmentError):
            merge_bracket([CAPTURES[0], "DSC09999"], DEFAULT_PRESET, preview=True)

    def test_rejects_path_traversal(self):
        for malicious in ("../DSC04510", "../../etc/passwd", "sub/DSC04510", "/etc/passwd"):
            with self.subTest(capture_id=malicious):
                with self.assertRaises(RawDevelopmentError):
                    merge_bracket([CAPTURES[0], malicious], DEFAULT_PRESET, preview=True)

    def test_rejects_a_symlink_that_escapes_the_source_folder(self):
        """現像元フォルダ直下に見えても、実体が外にあるものは合成しない。

        `_validate_capture_id` は `DSC#####` しか通さないのでID経由の脱出は不可能だが、
        フォルダ内に張られたシンボリックリンクは名前の検証をすり抜ける。
        `resolve()` 後の親一致（`engine.develop_raw` と同じ検査）で弾けること。
        """
        outside = self.project_root / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "secret.ARW").write_bytes(b"not-yours")
        link = self.source_root / "DSC09998.ARW"
        link.symlink_to(outside / "secret.ARW")

        with self.assertRaises(RawDevelopmentError):
            merge_bracket([CAPTURES[0], "DSC09998"], DEFAULT_PRESET, preview=True)

    def test_rejects_unknown_preset(self):
        with self.assertRaises(RawDevelopmentError):
            merge_bracket(CAPTURES[:2], "nope", preview=True)

    def test_rejects_out_of_range_max_edge(self):
        with self.assertRaises(RawDevelopmentError):
            merge_bracket(CAPTURES[:2], DEFAULT_PRESET, preview=True, max_edge=800)

    def test_does_not_decode_anything_when_validation_fails(self):
        with self.assertRaises(RawDevelopmentError):
            merge_bracket([CAPTURES[0], "DSC09999"], DEFAULT_PRESET, preview=True)
        self.assertEqual(self.decoded, [], "検証前に RAW を読み始めないこと")


class FusionContentTests(BracketTestCase):
    """合成そのものの効き方（露出の中間化・白飛びと黒つぶれの同時低減）。"""

    def setUp(self):
        super().setUp()
        self.dark = expose(self.radiance, self.DARK_GAIN)
        self.bright = expose(self.radiance, self.BRIGHT_GAIN)

    def test_inputs_have_the_expected_opposite_problems(self):
        """前提の確認: 暗い方は黒つぶれ、明るい方は白飛びを抱えていること。"""
        dark, bright = clip_metrics(self.dark), clip_metrics(self.bright)
        self.assertGreater(dark["shadow"], 0.1, "暗いフレームに黒つぶれが無いと検証にならない")
        self.assertLess(dark["highlight"], 0.01)
        self.assertGreater(bright["highlight"], 0.1, "明るいフレームに白飛びが無いと検証にならない")
        self.assertLess(bright["shadow"], 0.01)

    def test_merge_reduces_both_clipping_and_crush(self):
        """白飛びと黒つぶれが、それぞれを苦手とするフレームより減っていること。

        ⚠️ 「両方とも**全**フレームより減る」ことは**原理的にあり得ない**。合成結果は
        各フレームの凸結合なので、白飛びは最も暗いフレーム未満にはならず、黒つぶれは
        最も明るいフレーム未満にはならない（実測: 暗 0.00%/27.54%、明 25.78%/0.00% に対し
        合成 0.00%/18.17%）。合成の値打ちは**両方を同時に満たす1枚が得られる**ことなので、
        白飛び+黒つぶれの合計がどのフレームより小さいことで確認する。
        """
        fused = clip_metrics(_merge_frames([self.dark, self.bright]))
        dark, bright = clip_metrics(self.dark), clip_metrics(self.bright)

        self.assertLess(fused["highlight"], bright["highlight"], "白飛びが明るいフレームより減る")
        self.assertLess(fused["shadow"], dark["shadow"], "黒つぶれが暗いフレームより減る")
        self.assertLess(
            fused["highlight"] + fused["shadow"],
            min(dark["highlight"] + dark["shadow"], bright["highlight"] + bright["shadow"]),
            "どちらか一方を犠牲にしただけでなく、合計でどのフレームよりも良いこと",
        )

    def test_merge_lands_between_the_two_exposures(self):
        fused = clip_metrics(_merge_frames([self.dark, self.bright]))
        dark, bright = clip_metrics(self.dark), clip_metrics(self.bright)
        self.assertGreater(fused["mean"], dark["mean"])
        self.assertLess(fused["mean"], bright["mean"])

    def test_merge_returns_uint8_within_range(self):
        fused = _merge_frames([self.dark, self.bright])
        self.assertEqual(fused.dtype, np.uint8)
        self.assertEqual(fused.shape, self.dark.shape)
        self.assertGreaterEqual(int(fused.min()), 0)
        self.assertLessEqual(int(fused.max()), 255)

    def test_out_of_range_merge_output_is_clipped_not_wrapped(self):
        """Mertens はピラミッド再構成で 0..1 を外すことがある。

        素で uint8 にすると 1.06 → 270 → **14 に巻き戻る**（黒い斑点になる）。
        クリップが先に効いていることを、範囲外を返す偽 Mertens で確かめる。
        """
        overshoot = np.full((8, 8, 3), 1.06, dtype=np.float32)
        overshoot[0, 0] = -0.04
        with mock.patch.object(bracket_module.cv2, "createMergeMertens") as factory:
            factory.return_value.process.return_value = overshoot
            fused = _merge_frames([self.dark, self.bright])
        self.assertEqual(int(fused[0, 0, 0]), 0)
        self.assertEqual(int(fused[4, 4, 0]), 255)

    def test_non_finite_merge_output_is_rejected(self):
        """`np.clip` は NaN を素通しするので、クリップ前に有限性を確かめること。"""
        broken = np.full((8, 8, 3), 0.5, dtype=np.float32)
        broken[2, 2] = np.nan
        with mock.patch.object(bracket_module.cv2, "createMergeMertens") as factory:
            factory.return_value.process.return_value = broken
            with self.assertRaises(RawDevelopmentError):
                _merge_frames([self.dark, self.bright])

    def test_end_to_end_merge_keeps_the_fusion_benefit_in_the_saved_jpeg(self):
        """保存された JPEG でも、白飛び・黒つぶれが両端のフレームより減っていること。

        比較相手のフレームには**合成結果と同じトーン調整**を掛ける。トーン段（暗部持ち上げ
        LUT・明るさ・コントラスト）は合成の有無に関係なく効くので、素のフレームと比べると
        合成を過大評価してしまうため。

        ⚠️ ここでは「白飛び+黒つぶれの合計がどのフレームよりも小さい」ことは**主張しない**。
        Mertens が最適化するのは局所的な露出適正度であって、この合計値ではない。露出段の
        真ん中に「そこそこ良い妥協点」のフレームが含まれていると、合成結果の合計はその1枚に
        僅かに負けうる（実写でも同じ傾向: 合成 0.10%+36.50% に対し中間フレーム相当の
        単独現像 1.41%+34.37%）。合成の値打ちは**両端の弱点を同時に潰す**ことにある。
        """
        result = merge_bracket(CAPTURES, DEFAULT_PRESET, preview=True)
        fused = clip_metrics(self.merged_array(result))
        frames = [
            clip_metrics(
                apply_preset_tone(self.fake_decode(self.source_root / f"{capture_id}.ARW", {}, True))
            )
            for capture_id in CAPTURES
        ]
        brightest = max(frames, key=lambda frame: frame["mean"])
        darkest = min(frames, key=lambda frame: frame["mean"])

        self.assertLess(fused["highlight"], brightest["highlight"], "白飛びが最も明るいフレームより減る")
        self.assertLess(fused["shadow"], darkest["shadow"], "黒つぶれが最も暗いフレームより減る")
        # 露出は一番暗いフレームと一番明るいフレームの中間に収まる。
        self.assertGreater(fused["mean"], darkest["mean"])
        self.assertLess(fused["mean"], brightest["mean"])

    def test_rejects_frames_with_different_sizes(self):
        """向きが混ざったグループは `cv2.error`(500) ではなく日本語の 400 にする。"""
        def mixed(source_path, adjustments, preview):
            if source_path.stem == CAPTURES[1]:
                return expose(scene_radiance(96, 128), self.DARK_GAIN)
            return expose(self.radiance, self.BRIGHT_GAIN)

        with mock.patch.object(bracket_module, "_decode_frame", side_effect=mixed):
            with self.assertRaises(RawDevelopmentError):
                merge_bracket(CAPTURES[:2], DEFAULT_PRESET, preview=True)


class MergeParamsHashTests(unittest.TestCase):
    """キャッシュキー。`engine._params_hash` と同じ思想で、取り違えを防ぐ要素を含む。"""

    def _hash(self, **kwargs):
        base = {
            "capture_ids": ["DSC04510", "DSC04513"],
            "adjustments": resolve_adjustments(DEFAULT_PRESET),
            "preset_name": DEFAULT_PRESET,
            "max_edge": 6000,
            "preview": False,
            "lens_applicable": True,
            "source_root": "/Volumes/CARD-A/DCIM/100MSDCF",
        }
        base.update(kwargs)
        return _merge_params_hash(**base)

    def test_is_ten_characters(self):
        self.assertEqual(len(self._hash()), 10)

    def test_is_stable_for_identical_input(self):
        self.assertEqual(self._hash(), self._hash())

    def test_changes_with_source_root(self):
        """フォルダが違えばキャッシュキーは別。

        `capture_id` はカメラの連番なのでカードをまたぐと衝突する。ここが同一だと
        別フォルダの同じIDに対して他人の写真の合成結果が `cached: true` で返る。
        """
        baseline = self._hash()
        self.assertNotEqual(baseline, self._hash(source_root="/Volumes/CARD-B/DCIM/100MSDCF"))
        self.assertNotEqual(baseline, self._hash(source_root="/Volumes/CARD-A/DCIM/100MSDCF/"))

    def test_changes_with_the_set_of_capture_ids(self):
        baseline = self._hash()
        self.assertNotEqual(baseline, self._hash(capture_ids=["DSC04510", "DSC04514"]))
        self.assertNotEqual(baseline, self._hash(capture_ids=["DSC04510", "DSC04513", "DSC04514"]))
        self.assertNotEqual(baseline, self._hash(capture_ids=["DSC04513"]))

    def test_changes_with_preset_max_edge_preview_and_lens(self):
        baseline = self._hash()
        self.assertNotEqual(baseline, self._hash(preset_name="nightscape"))
        self.assertNotEqual(baseline, self._hash(max_edge=3000))
        self.assertNotEqual(baseline, self._hash(preview=True))
        self.assertNotEqual(baseline, self._hash(lens_applicable=False))

    def test_changes_when_any_single_adjustment_changes(self):
        baseline = self._hash()
        adjustments = resolve_adjustments(DEFAULT_PRESET, {"contrast": 1.31})
        self.assertNotEqual(baseline, self._hash(adjustments=adjustments))


class MergedOutputTests(BracketTestCase):
    def test_result_shape_matches_the_contract(self):
        result = merge_bracket(CAPTURES, DEFAULT_PRESET, preview=True)
        self.assertEqual(
            set(result),
            {
                "capture_ids", "frames", "preset", "preset_label", "filename",
                "recipe_filename", "params_hash", "adjustments", "width", "height",
                "elapsed_seconds", "cached", "preview", "aligned", "lens_correction",
                "exif_inherited",
            },
        )
        self.assertEqual(result["capture_ids"], CAPTURES)
        self.assertEqual(result["frames"], 5)
        self.assertFalse(result["cached"])
        self.assertTrue(result["preview"])

    def test_filename_follows_the_naming_rule(self):
        result = merge_bracket(CAPTURES, DEFAULT_PRESET, preview=True)
        self.assertEqual(
            result["filename"],
            f"{CAPTURES[0]}-merged5-{DEFAULT_PRESET}-{result['params_hash']}.jpg",
        )
        self.assertEqual(result["recipe_filename"], result["filename"].replace(".jpg", ".json"))

    def test_capture_ids_are_normalised_to_sorted_order(self):
        """順序を入れ替えても同じ出力（ハッシュがソート済み前提のため）。"""
        forward = merge_bracket(CAPTURES[:3], DEFAULT_PRESET, preview=True)
        reversed_result = merge_bracket(list(reversed(CAPTURES[:3])), DEFAULT_PRESET, preview=True)
        self.assertEqual(forward["params_hash"], reversed_result["params_hash"])
        self.assertEqual(forward["filename"], reversed_result["filename"])
        self.assertEqual(reversed_result["capture_ids"], sorted(CAPTURES[:3]))

    def test_leaves_no_temporary_files(self):
        merge_bracket(CAPTURES[:2], DEFAULT_PRESET, preview=True)
        self.assertEqual([path.name for path in self.merged_root.glob("*tmp*")], [])

    def test_second_call_is_served_from_cache(self):
        first = merge_bracket(CAPTURES[:2], DEFAULT_PRESET, preview=True)
        self.decoded.clear()
        second = merge_bracket(CAPTURES[:2], DEFAULT_PRESET, preview=True)
        self.assertTrue(second["cached"])
        self.assertEqual(self.decoded, [], "キャッシュヒット時は RAW を読み直さない")
        self.assertEqual(second["params_hash"], first["params_hash"])
        self.assertEqual(second["width"], first["width"])

    def test_force_regenerates_even_when_cached(self):
        merge_bracket(CAPTURES[:2], DEFAULT_PRESET, preview=True)
        self.decoded.clear()
        again = merge_bracket(CAPTURES[:2], DEFAULT_PRESET, preview=True, force=True)
        self.assertFalse(again["cached"])
        self.assertEqual(sorted(self.decoded), sorted(CAPTURES[:2]))

    def test_a_different_folder_does_not_reuse_the_cached_merge(self):
        first = merge_bracket(CAPTURES[:2], DEFAULT_PRESET, preview=True)

        other_root = self.project_root / "CARD-B" / "100MSDCF"
        other_root.mkdir(parents=True, exist_ok=True)
        for capture_id in CAPTURES[:2]:
            (other_root / f"{capture_id}.ARW").write_bytes(b"card-b-raw")
        with mock.patch.object(config_module, "source_root", return_value=other_root):
            second = merge_bracket(CAPTURES[:2], DEFAULT_PRESET, preview=True)

        self.assertNotEqual(first["params_hash"], second["params_hash"])
        self.assertFalse(second["cached"], "別フォルダの同じIDでキャッシュを共有しないこと")

    def test_respects_max_edge(self):
        result = merge_bracket(CAPTURES[:2], DEFAULT_PRESET, preview=True, max_edge=1200)
        self.assertLessEqual(max(result["width"], result["height"]), 1200)


class MergedRecipeTests(BracketTestCase):
    def test_recipe_records_every_frame_with_its_hash(self):
        result = merge_bracket(CAPTURES, DEFAULT_PRESET, preview=True)
        recipe = self.read_recipe(result)

        self.assertEqual(recipe["capture_ids"], CAPTURES)
        self.assertEqual(recipe["params_hash"], result["params_hash"])
        self.assertEqual(recipe["kind"], "exposure_fusion")
        self.assertEqual(recipe["adjustments"], result["adjustments"])
        self.assertIn("aligned", recipe)
        self.assertEqual([frame["capture_id"] for frame in recipe["frames"]], CAPTURES)
        for frame in recipe["frames"]:
            with self.subTest(capture_id=frame["capture_id"]):
                self.assertEqual(len(frame["sha256"]), 64)
                self.assertIn("lens_correction", frame)
        # 内容の違うARWなので、フレームごとに別のハッシュになる。
        self.assertEqual(len({frame["sha256"] for frame in recipe["frames"]}), len(CAPTURES))

    def test_recipe_records_engine_versions(self):
        recipe = self.read_recipe(merge_bracket(CAPTURES[:2], DEFAULT_PRESET, preview=True))
        engine = recipe["engine"]
        self.assertEqual(engine["bracket_version"], bracket_module.BRACKET_VERSION)
        self.assertEqual(engine["align_version"], bracket_module.ALIGN_VERSION)
        for key in ("rawpy_version", "pillow_version", "opencv_version"):
            self.assertTrue(engine[key])

    def test_lens_correction_is_summarised_per_frame_and_overall(self):
        result = merge_bracket(CAPTURES[:2], DEFAULT_PRESET, preview=True)
        recipe = self.read_recipe(result)
        # ペアJPGが無い合成データではレンズが解決できないので不適用になる。
        self.assertFalse(result["lens_correction"]["applied"])
        self.assertIn("reason", result["lens_correction"])
        self.assertEqual(recipe["lens_correction"], result["lens_correction"])

    def test_inconsistent_lens_correction_is_not_reported_as_applied(self):
        """1枚でも補正結果が食い違ったら適用扱いにしない（記録は残す）。

        歪曲補正は放射状のワープなので、補正済みと未補正が混ざると位置合わせでは
        取り除けないゴーストになる。
        """
        results = iter(
            [
                (None, {"applied": True, "lens": "Sigma / 10-18mm"}),
                (None, {"applied": False, "reason": "lens_not_in_db"}),
            ]
        )

        def flaky(rgb, resolution):
            _, correction = next(results)
            return rgb, correction

        with mock.patch.object(bracket_module, "apply_lens_correction", side_effect=flaky):
            result = merge_bracket(CAPTURES[:2], DEFAULT_PRESET, preview=True)

        self.assertEqual(
            result["lens_correction"], {"applied": False, "reason": "inconsistent_across_frames"}
        )
        recipe = self.read_recipe(result)
        self.assertTrue(recipe["frames"][0]["lens_correction"]["applied"])
        self.assertFalse(recipe["frames"][1]["lens_correction"]["applied"])


class AlignmentTests(BracketTestCase):
    def test_alignment_failure_does_not_stop_the_merge(self):
        with mock.patch.object(
            bracket_module.cv2, "createAlignMTB", side_effect=RuntimeError("boom")
        ):
            result = merge_bracket(CAPTURES[:2], DEFAULT_PRESET, preview=True)
        self.assertFalse(result["aligned"])
        self.assertTrue((self.merged_root / result["filename"]).is_file())

    def test_empty_alignment_output_is_treated_as_failure(self):
        """空リストを渡すと OpenCV は何も書かずに戻る（長さ0で「成功」してしまう）。"""
        with mock.patch.object(bracket_module.cv2, "createAlignMTB") as factory:
            factory.return_value.process.side_effect = lambda src, dst: dst.clear()
            result = merge_bracket(CAPTURES[:2], DEFAULT_PRESET, preview=True)
        self.assertFalse(result["aligned"])
        self.assertTrue((self.merged_root / result["filename"]).is_file())

    def test_alignment_is_attempted_and_reported(self):
        result = merge_bracket(CAPTURES[:2], DEFAULT_PRESET, preview=True)
        self.assertTrue(result["aligned"])

    def test_an_all_black_aligned_frame_is_rejected(self):
        """位置合わせが1枚を真っ黒にしたら、その結果ごと捨てて元フレームで合成する。

        OpenCV 5.0.0 は `process(src, dst)` に**事前確保したリスト**を渡すと 1 枚を
        全面 0 にして返す（実測: 2〜5枚のいずれでも発生し、位置は枚数で変わる）。
        真っ黒なフレームが混ざると合成の重みが狂うので、安全網として弾く。
        """
        sources = [expose(self.radiance, gain) for gain in (0.2, 8.0)]

        def zero_one(src, dst):
            dst[1][:] = 0

        with mock.patch.object(bracket_module.cv2, "createAlignMTB") as factory:
            factory.return_value.process = zero_one
            aligned, ok = bracket_module._align_frames(sources)

        self.assertFalse(ok, "真っ黒なフレームが出たら位置合わせは失敗扱いにする")
        for result, source in zip(aligned, sources):
            self.assertTrue(np.array_equal(result, source), "元のフレームがそのまま返ること")

    def test_alignment_does_not_mutate_the_input_frames(self):
        """OpenCV には in-place で渡すが、呼び出し元のフレームは壊さないこと。"""
        sources = [expose(self.radiance, gain) for gain in (0.2, 8.0)]
        before = [frame.copy() for frame in sources]
        bracket_module._align_frames(sources)
        for source, original in zip(sources, before):
            self.assertTrue(np.array_equal(source, original))

    def test_alignment_search_is_bounded_by_image_size(self):
        """探索幅は短辺に比例させる（既定の ±64px はプレビューには広すぎる）。"""
        self.assertEqual(bracket_module._align_max_bits((2012, 3012, 3)), 5)
        self.assertLessEqual(bracket_module._align_max_bits((96, 128, 3)), 2)
        self.assertEqual(bracket_module._align_max_bits((8000, 8000, 3)), bracket_module.ALIGN_MAX_BITS)

    def test_repeated_merges_are_byte_identical(self):
        """同じ入力なら毎回同じ JPEG になること（キャッシュの再現性の前提）。"""
        first = merge_bracket(CAPTURES[:3], DEFAULT_PRESET, preview=True, force=True)
        first_bytes = (self.merged_root / first["filename"]).read_bytes()
        second = merge_bracket(CAPTURES[:3], DEFAULT_PRESET, preview=True, force=True)
        self.assertEqual((self.merged_root / second["filename"]).read_bytes(), first_bytes)


class MergePostprocessParamsTests(unittest.TestCase):
    """合成用デコードが**露出差を保つ**こと（本モジュールの要）。"""

    class FakeRaw:
        camera_whitebalance = [2.0, 1.0, 1.5, 1.0]

    def _params(self, **overrides):
        adjustments = resolve_adjustments(DEFAULT_PRESET, overrides or None)
        return bracket_module._merge_postprocess_params(self.FakeRaw(), adjustments, False)

    def test_disables_auto_brightness(self):
        """`no_auto_bright=False` だとフレームごとに露出が正規化され合成が無意味になる。"""
        self.assertIs(self._params()["no_auto_bright"], True)

    def test_does_not_pass_exposure_or_highlight_parameters(self):
        params = self._params(exposure=1.5, highlight_recovery=100)
        for key in ("exp_shift", "bright", "exp_preserve_highlights", "highlight_mode"):
            with self.subTest(key=key):
                self.assertNotIn(key, params)

    def test_matches_the_existing_develop_for_demosaic_colour_and_depth(self):
        from local_raw import engine

        params = self._params()
        reference = engine._postprocess_params(
            self.FakeRaw(), resolve_adjustments(DEFAULT_PRESET), False
        )
        for key in ("output_color", "output_bps", "demosaic_algorithm", "use_auto_wb"):
            with self.subTest(key=key):
                self.assertEqual(params[key], reference[key])

    def test_uses_camera_white_balance_when_wb_temp_is_zero(self):
        params = self._params()
        self.assertTrue(params["use_camera_wb"])
        self.assertNotIn("user_wb", params)

    def test_scales_red_and_blue_gains_for_wb_temp(self):
        params = self._params(wb_temp=50)
        self.assertFalse(params["use_camera_wb"])
        self.assertAlmostEqual(params["user_wb"][0], 2.0 * (1.0 + 0.003 * 50))
        self.assertAlmostEqual(params["user_wb"][2], 1.5 * (1.0 - 0.003 * 50))

    def test_half_size_follows_preview(self):
        adjustments = resolve_adjustments(DEFAULT_PRESET)
        raw = self.FakeRaw()
        self.assertTrue(bracket_module._merge_postprocess_params(raw, adjustments, True)["half_size"])
        self.assertFalse(
            bracket_module._merge_postprocess_params(raw, adjustments, False)["half_size"]
        )


class ToneAfterMergeTests(BracketTestCase):
    """トーン調整はフレームごとではなく合成結果に1回だけ効くこと。"""

    def test_saturation_zero_produces_a_monochrome_result(self):
        result = merge_bracket(
            CAPTURES[:2], DEFAULT_PRESET, preview=True, adjustments={"saturation": 0.0}
        )
        rgb = self.merged_array(result)
        self.assertTrue(np.array_equal(rgb[:, :, 0], rgb[:, :, 1]))
        self.assertTrue(np.array_equal(rgb[:, :, 1], rgb[:, :, 2]))

    def test_shadow_lift_brightens_the_merged_result(self):
        low = merge_bracket(
            CAPTURES[:2], DEFAULT_PRESET, preview=True, adjustments={"shadow_lift": 0.0}
        )
        high = merge_bracket(
            CAPTURES[:2], DEFAULT_PRESET, preview=True, adjustments={"shadow_lift": 100.0}
        )
        self.assertGreater(self.merged_array(high).mean(), self.merged_array(low).mean())

    def test_adjustments_are_returned_resolved_in_full(self):
        result = merge_bracket(
            CAPTURES[:2], DEFAULT_PRESET, preview=True, adjustments={"contrast": 1.2}
        )
        self.assertEqual(result["adjustments"], resolve_adjustments(DEFAULT_PRESET, {"contrast": 1.2}))

    def test_rejects_unknown_adjustment_key(self):
        with self.assertRaises(RawDevelopmentError):
            merge_bracket(CAPTURES[:2], DEFAULT_PRESET, preview=True, adjustments={"gamma": 1.0})


if __name__ == "__main__":
    unittest.main()
