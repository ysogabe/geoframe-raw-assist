"""scripts/evaluate_candidates.py の集計・ケース指定パース・拡張子解決の検証。

実データには依存しない（合成レコードと一時ディレクトリのみ）。
"""

import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import evaluate_candidates as ec  # noqa: E402

try:  # 旧 rawpy / local_raw 経路（アーカイブ）。Python 本体の削除後は DevelopParityTests だけ skip する。
    import importlib.util as _ilu

    LEGACY_AVAILABLE = _ilu.find_spec("local_raw") is not None and _ilu.find_spec("rawpy") is not None
except (ImportError, ValueError):
    LEGACY_AVAILABLE = False
engine = None  # DevelopParityTests.setUpClass が旧経路のときだけ読み込む


def _metrics(**overrides):
    base = {
        "highlight_clip": 0.01, "shadow_clip": 0.01, "noise_flat": 0.02, "detail": 0.3, "halo": 0.1,
        "color_saturation_clip": 0.0, "tonal_range": 0.5, "source_deviation": 0.0, "shadow_mean": 0.10,
        "star_count": 0,
    }
    base.update(overrides)
    return base


def _cand(cid, raw=None, proxy=None, adj=None, ungated=None):
    return {
        "id": cid,
        "adjustments": adj or {"exposure": 0.0, "highlight_recovery": 15.0},
        "metrics": _metrics(**(proxy or {})),
        "raw_metrics": _metrics(**(raw or {})),
        "raw_star_ungated": ungated or {"star_count": 0},
    }


def _record(key, *cands, rejected=None):
    return {
        "key": key, "capture_id": key.split("__")[0], "preset": "neutral_recovery", "gen_elapsed": 10.0,
        "proxy": {"decodes": 5}, "star_scene": {"is_star_scene": False, "star_count": 0},
        "candidates": list(cands), "rejected": rejected or [],
    }


class ParseTests(unittest.TestCase):
    def test_case_spec(self):
        self.assertEqual(
            ec.parse_case_spec("DSC02849:nightscape", "stars"),
            {"root": "stars", "capture_id": "DSC02849", "preset": "nightscape"},
        )
        self.assertEqual(ec.parse_case_spec("DSC02849")["preset"], "neutral_recovery")

    def test_case_spec_invalid(self):
        for bad in ("XYZ1:nightscape", "DSC02849:nope", "DSC02849:", "DSC1234"):
            with self.assertRaises(ValueError, msg=bad):
                ec.parse_case_spec(bad)

    def test_cases_file_and_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            path.write_text(json.dumps({"sets": [
                {"root": "a", "cases": ["DSC00001:neutral_recovery"]},
                {"root": "b", "cases": ["DSC00002:nightscape", "DSC00003"]},
            ]}))
            cases = ec.load_cases_file(path)
            self.assertEqual([c["root"] for c in cases], ["a", "b", "b"])
            roots = ec.parse_root_args(["a=" + tmp], None)
            with self.assertRaises(ValueError):
                ec.check_roots(cases, roots)  # b が未指定
            roots = ec.parse_root_args(["a=" + tmp, "b=" + tmp], None)
            ec.check_roots(cases, roots)

    def test_bad_cases_file_and_root_arg(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{}")
            with self.assertRaises(ValueError):
                ec.load_cases_file(path)
        with self.assertRaises(ValueError):
            ec.parse_root_args(["novalue"], None)

    def test_source_root_is_default_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn(ec.DEFAULT_ROOT_KEY, ec.parse_root_args([], Path(tmp)))

    def test_shipped_baseline_file_is_valid(self):
        cases = ec.load_cases_file(SCRIPTS_DIR / "eval_cases" / "candidates_baseline.json")
        self.assertEqual(len(cases), 24)
        self.assertEqual({c["root"] for c in cases}, {"image_work", "stars"})
        self.assertNotIn("/", (SCRIPTS_DIR / "eval_cases" / "candidates_baseline.json").read_text().split('"sets"')[1])


class FindSourceFileTests(unittest.TestCase):
    def test_lowercase_extension_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "DSC07402.arw").write_bytes(b"x")
            (root / "DSC07402.JPG").write_bytes(b"x")
            (root / "DSC00001.ARW").write_bytes(b"x")
            self.assertEqual(ec.find_source_file(root, "DSC07402", ("ARW",)).name, "DSC07402.arw")
            self.assertEqual(ec.find_source_file(root, "DSC07402", ("JPG", "JPEG")).name, "DSC07402.JPG")
            self.assertEqual(ec.find_source_file(root, "DSC00001", ("ARW",)).name, "DSC00001.ARW")
            self.assertIsNone(ec.find_source_file(root, "DSC09999", ("ARW",)))


class StatsTests(unittest.TestCase):
    def test_spearman(self):
        self.assertAlmostEqual(ec.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertAlmostEqual(ec.spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)
        self.assertIsNone(ec.spearman([1, 2], [1, 2]))
        self.assertIsNone(ec.spearman([1, 1, 1], [1, 2, 3]))

    def test_rankdata_ties(self):
        self.assertEqual(list(ec.rankdata([5, 5, 1])), [2.5, 2.5, 1.0])

    def test_compare_proxy_vs_raw(self):
        def rec(key, vals):
            return _record(key, *[_cand(f"s{i}", raw={"noise_flat": v * 2 + 0.01}, proxy={"noise_flat": v}) for i, v in enumerate(vals)],
                           )
        r1 = rec("DSC00001__x", [0.01, 0.02, 0.03])
        # proxy と同順位・raw = 2*proxy+0.01
        for c, i in zip(r1["candidates"], range(3)):
            c["id"] = ["natural", "a", "b"][i]
        summary = ec.compare_proxy_vs_raw([r1])
        row = summary["noise_flat"]
        self.assertAlmostEqual(row["within_photo_spearman_mean"], 1.0)
        self.assertAlmostEqual(row["pooled_spearman"], 1.0)
        self.assertAlmostEqual(row["bias_raw_minus_proxy"], 0.03, places=6)
        self.assertAlmostEqual(row["mae"], 0.03, places=6)

    def test_distribution(self):
        d = ec.distribution([1.0, 2.0, 3.0])
        self.assertEqual((d["n"], d["median"], d["min"], d["max"]), (3, 2.0, 1.0, 3.0))
        self.assertIsNone(ec.distribution([])["mean"])


class JudgementTests(unittest.TestCase):
    def test_broken_candidates(self):
        nat = _cand("natural", raw={"shadow_clip": 0.02, "highlight_clip": 0.01})
        bad_shadow = _cand("foreground", raw={"shadow_clip": 0.12, "highlight_clip": 0.01})
        bad_high = _cand("social", raw={"shadow_clip": 0.02, "highlight_clip": 0.11})
        ok = _cand("low_noise", raw={"shadow_clip": 0.05, "highlight_clip": 0.05})
        broken = ec.find_broken_candidates([_record("DSC00001__x", nat, bad_shadow, bad_high, ok)])
        self.assertEqual([b["style"] for b in broken], ["foreground", "social"])
        self.assertAlmostEqual(broken[0]["raw_delta_shadow_clip"], 0.10)

    def test_style_goals(self):
        nat = _cand("natural", raw={"shadow_mean": 0.10, "noise_flat": 0.03, "tonal_range": 0.5},
                    proxy={"shadow_mean": 0.10, "noise_flat": 0.03, "tonal_range": 0.5}, ungated={"star_count": 10})
        fg_ok = _cand("foreground", raw={"shadow_mean": 0.15}, proxy={"shadow_mean": 0.15})
        ln_bad = _cand("low_noise", raw={"noise_flat": 0.04}, proxy={"noise_flat": 0.02})
        so_ok = _cand("social", raw={"tonal_range": 0.6}, proxy={"tonal_range": 0.4})
        st_bad = _cand("stars", raw={}, proxy={}, ungated={"star_count": 8})
        goals = ec.summarize_style_goals([_record("DSC00001__x", nat, fg_ok, ln_bad, so_ok, st_bad)])
        self.assertEqual(goals["foreground"]["raw_achieved"], 1)
        self.assertEqual(goals["low_noise"]["raw_achieved"], 0)
        self.assertEqual(goals["low_noise"]["proxy_achieved"], 1)
        self.assertEqual(goals["low_noise"]["failed_cases"], ["DSC00001__x"])
        self.assertEqual(goals["social"]["raw_achieved"], 1)
        self.assertEqual(goals["stars"]["raw_achieved"], 0)
        self.assertEqual(goals["stars"]["mean_raw_delta"], -2.0)

    def test_near_natural_and_summary(self):
        nat = _cand("natural")
        same = _cand("social", raw={"source_deviation": 0.004})
        diff = _cand("foreground", raw={"source_deviation": 0.05}, adj={"exposure": 1.0, "highlight_recovery": 85.0})
        rec = _record("DSC00001__x", nat, same, diff, rejected=[{"id": "stars", "reason": "r"}])
        near = ec.find_near_natural([rec])
        self.assertEqual([n["style"] for n in near], ["social"])
        summary = ec.build_summary([rec])
        self.assertEqual(summary["rejected_by_style"], {"stars": 1})
        self.assertEqual(summary["highlight_category_non_natural"]["reconstruct"], 1)
        self.assertEqual(summary["exposure_non_natural"], {"+0.0": 1, "+1.0": 1})
        self.assertEqual(summary["candidate_count_histogram"], {"3": 1})
        self.assertIn("代理 vs 実RAW", ec.render_markdown(summary))


class RootKeyTests(unittest.TestCase):
    def test_case_key_includes_root(self):
        a = ec.parse_case_spec("DSC00001:nightscape", "a")
        b = ec.parse_case_spec("DSC00001:nightscape", "b")
        self.assertEqual(ec.case_key(a), "a__DSC00001__nightscape")
        self.assertNotEqual(ec.case_key(a), ec.case_key(b))
        self.assertEqual(ec.case_key(ec.parse_case_spec("DSC00001")), "default__DSC00001__neutral_recovery")

    def test_same_capture_in_two_roots_do_not_collide_in_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            keys = {ec.case_key(ec.parse_case_spec("DSC00001", root)) for root in ("a", "b")}
            self.assertEqual(len(keys), 2)
            for key in keys:
                (out / "cases").mkdir(exist_ok=True)
                (out / "cases" / f"{key}.json").write_text("{}")
            self.assertEqual(len(list((out / "cases").glob("*.json"))), 2)

    def test_root_key_validation(self):
        for good in ("default", "image_work", "stars-2", "A1"):
            self.assertEqual(ec.validate_root_key(good), good)
        for bad in ("", "a/b", "a b", "../x", "a.b", "日本語"):
            with self.assertRaises(ValueError, msg=bad):
                ec.validate_root_key(bad)
            with self.assertRaises(ValueError, msg=bad):
                ec.parse_case_spec("DSC00001", bad)
            with self.assertRaises(ValueError, msg=bad):
                ec.parse_root_args([f"{bad}=/tmp"], None)

    def test_cases_file_rejects_bad_root_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps({"sets": [{"root": "a/b", "cases": ["DSC00001"]}]}))
            with self.assertRaises(ValueError):
                ec.load_cases_file(path)


def _write_case(out, key="a__DSC00001__neutral_recovery"):
    record = _record(key, _cand("natural"))
    (out / "cases").mkdir(parents=True, exist_ok=True)
    (out / "cases" / f"{key}.json").write_text(json.dumps(record), encoding="utf-8")


class AggregateOnlyTests(unittest.TestCase):
    def test_meta_and_errors_carried_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _write_case(out)
            meta = {"started_at": "2026-01-01 00:00:00", "engine_version": "v9", "roots": {"a": "/x"}}
            errors = [{"case": "a__DSC00002__neutral_recovery", "error": "boom"}]
            ec._write_summary(out, [json.loads(p.read_text()) for p in (out / "cases").glob("*.json")], errors, meta)
            ec.main(["--aggregate-only", "--out", str(out)])
            data = json.loads((out / "summary.json").read_text())
            self.assertEqual(data["meta"]["started_at"], "2026-01-01 00:00:00")
            self.assertEqual(data["meta"]["engine_version"], "v9")
            self.assertEqual(data["meta"]["roots"], {"a": "/x"})
            self.assertIn("aggregated_at", data["meta"])
            self.assertEqual(data["summary"]["errors"], errors)
            self.assertIn("エラー: 1", (out / "summary.md").read_text())

    def test_missing_summary_means_errors_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _write_case(out)
            ec.main(["--aggregate-only", "--out", str(out)])
            data = json.loads((out / "summary.json").read_text())
            self.assertIsNone(data["summary"]["errors"])
            self.assertIn("aggregated_at", data["meta"])
            md = (out / "summary.md").read_text()
            self.assertIn("エラー: 不明（再集計のため）", md)
            self.assertNotIn("エラー: 0", md)

    def test_broken_summary_is_treated_as_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "summary.json").write_text("{not json")
            self.assertIsNone(ec.load_previous_run_info(out)[1])

    def test_zero_errors_still_rendered_as_zero(self):
        summary = ec.build_summary([], [])
        self.assertIn("エラー: 0", ec.render_markdown(summary))


class PrepareOutDirTests(unittest.TestCase):
    def test_existing_cases_stop_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _write_case(out)
            with self.assertRaises(SystemExit):
                ec.prepare_out_dir(out, force=False)
            self.assertTrue(any((out / "cases").glob("*.json")))

    def test_fresh_dir_is_ok_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "new"
            ec.prepare_out_dir(out, force=False)
            self.assertTrue((out / "cases").is_dir())

    def test_force_removes_only_known_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _write_case(out)
            for sub in ("frames", "contact_sheets"):
                (out / sub).mkdir()
                (out / sub / "x.jpg").write_bytes(b"x")
            (out / "summary.json").write_text("{}")
            (out / "summary.md").write_text("x")
            (out / "keep_dir").mkdir()
            (out / "keep_dir" / "f.txt").write_text("k")
            (out / "notes.txt").write_text("k")
            ec.prepare_out_dir(out, force=True)
            self.assertEqual(list((out / "cases").iterdir()), [])
            self.assertFalse((out / "frames").exists())
            self.assertFalse((out / "contact_sheets").exists())
            self.assertFalse((out / "summary.json").exists())
            self.assertFalse((out / "summary.md").exists())
            self.assertTrue((out / "keep_dir" / "f.txt").is_file())
            self.assertTrue((out / "notes.txt").is_file())


class PairFrameTests(unittest.TestCase):
    def test_exif_orientation_is_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dest = Path(tmp) / "p.jpg", Path(tmp) / "out" / "f.jpg"
            image = Image.new("RGB", (60, 40), (200, 100, 50))
            exif = Image.Exif()
            exif[0x0112] = 6  # 90度回転して表示する向き
            image.save(src, "JPEG", exif=exif)
            ec.save_pair_frame(src, dest)
            with Image.open(dest) as frame:
                self.assertEqual(frame.size, (40, 60))  # 横長 60x40 → 縦長 40x60

    def test_no_orientation_keeps_size_and_shrinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dest = Path(tmp) / "p.jpg", Path(tmp) / "f.jpg"
            Image.new("RGB", (1200, 800)).save(src, "JPEG")
            ec.save_pair_frame(src, dest)
            with Image.open(dest) as frame:
                self.assertEqual(frame.size, (ec.FRAME_EDGE, 320))


@unittest.skipUnless(LEGACY_AVAILABLE, "旧 rawpy / local_raw 経路（アーカイブ）が無い環境")
class DevelopParityTests(unittest.TestCase):
    """旧経路: develop_real_array が engine.develop_raw と同じ関数を同じ順序・同じ引数で呼ぶこと。"""

    @classmethod
    def setUpClass(cls):
        global engine
        engine = ec.legacy.import_module("local_raw.engine", "test")
        cls.lens = ec.legacy.import_module("local_raw.lens", "test")

    class FakeRaw:
        camera_whitebalance = [1.0, 1.0, 1.0, 1.0]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def postprocess(self, **kwargs):
            return np.full((40, 60, 3), 30000, dtype=np.uint16)

    def _recorded(self, calls):
        """engine の各段を wraps で記録する patch 群。lens は ec 側の import 名も差し替える。"""

        def wrap(target, name, label):
            original = getattr(target, name)

            def recorder(*args, **kwargs):
                calls.append((label, args[1:] if label != "decode16" else (), dict(kwargs)))
                return original(*args, **kwargs)

            return mock.patch.object(target, name, side_effect=recorder)

        return [
            wrap(engine, "_postprocess_params", "postprocess_params"),
            wrap(engine, "_decode_16bit_to_float01", "decode16"),
            wrap(engine, "apply_lens_correction", "lens"),
            wrap(self.lens, "apply_lens_correction", "lens"),
            wrap(engine, "apply_post_decode_chain", "chain"),
        ]

    def _run(self, runner):
        calls: list = []
        with contextlib.ExitStack() as stack:
            for patch in self._recorded(calls):
                stack.enter_context(patch)
            runner()
        return calls

    def test_call_order_is_fixed(self):
        adjustments = engine.resolve_adjustments("neutral_recovery")
        calls = self._run(lambda: ec.develop_real_array(self.FakeRaw(), adjustments, self.lens.probe_lens({})))
        self.assertEqual([c[0] for c in calls], ["postprocess_params", "decode16", "lens", "chain"])
        self.assertEqual(calls[0][1], (adjustments, True, None))  # preview 相当（full デコード）
        self.assertEqual(calls[2][2], {"force_skip_vignetting": False})
        self.assertEqual(calls[3][1], (adjustments, engine.PREVIEW_MAX_EDGE))

    def test_dark_scene_passes_fixed_gain_and_skips_vignetting(self):
        adjustments = engine.resolve_adjustments("neutral_recovery")
        calls = self._run(
            lambda: ec.develop_real_array(self.FakeRaw(), adjustments, self.lens.probe_lens({}), 5.0)
        )
        self.assertEqual(calls[0][1], (adjustments, True, 5.0))
        self.assertEqual(calls[2][2], {"force_skip_vignetting": True})

    def test_iso_falls_back_to_pair_jpg_exif(self):
        adjustments = engine.resolve_adjustments("neutral_recovery")
        calls = self._run(
            lambda: ec.develop_real_array(
                self.FakeRaw(), adjustments, self.lens.probe_lens({}), None, {"iso": 3200}
            )
        )
        self.assertEqual(calls[3][2], {"iso": 3200})

    def _compare_with_engine(self, exif: dict | None):
        """exif が None ならペアJPG無し。dict ならその EXIF を持つペアJPGがある状況を再現。"""
        adjustments = engine.resolve_adjustments("neutral_recovery")
        bright_k = engine.detect_dark_scene(exif or {})["bright_k"]
        mine = self._run(
            lambda: ec.develop_real_array(
                self.FakeRaw(), adjustments, self.lens.probe_lens({}), bright_k, exif
            )
        )

        def run_engine():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "src"
                root.mkdir()
                (root / "DSC00001.ARW").write_bytes(b"")
                patches = [
                    mock.patch.object(engine.config, "source_root", return_value=root),
                    mock.patch.object(engine, "OUTPUT_ROOT", Path(directory) / "out"),
                    mock.patch.object(engine, "PREVIEW_ROOT", Path(directory) / "preview"),
                    mock.patch.object(engine.rawpy, "imread", return_value=self.FakeRaw()),
                ]
                if exif is not None:
                    (root / "DSC00001.JPG").write_bytes(b"")
                    patches.append(mock.patch.object(engine, "read_capture_exif", return_value=exif))
                with contextlib.ExitStack() as stack:
                    for patch in patches:
                        stack.enter_context(patch)
                    engine.develop_raw("DSC00001", "neutral_recovery", preview=True)

        theirs = self._run(run_engine)
        self.assertEqual(mine, theirs)
        return mine

    def test_same_calls_as_engine_develop_raw(self):
        self._compare_with_engine(None)

    def test_same_calls_as_engine_develop_raw_bright_scene(self):
        calls = self._compare_with_engine({"f_number": 8.0, "exposure_time": 1 / 250, "iso": 100})
        self.assertEqual(calls[0][1], (engine.resolve_adjustments("neutral_recovery"), True, None))

    def test_same_calls_as_engine_develop_raw_dark_scene(self):
        calls = self._compare_with_engine({"f_number": 2.8, "exposure_time": 15.0, "iso": 3200})
        self.assertEqual(calls[0][1][2], 5.0)
        self.assertEqual(calls[2][2], {"force_skip_vignetting": True})


if __name__ == "__main__":
    unittest.main()
