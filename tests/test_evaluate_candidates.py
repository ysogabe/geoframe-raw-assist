"""scripts/evaluate_candidates.py の集計・ケース指定パース・拡張子解決の検証。

実データには依存しない（合成レコードと一時ディレクトリのみ）。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import evaluate_candidates as ec  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
