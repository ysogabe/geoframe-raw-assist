"""評価経路の独立性の結合テスト（#57 / #90 / #117）。

Python 本体を削除した状態を一時ディレクトリに再現して、評価スクリプトを**サブプロセスで実際に**回す:

- ``scripts/`` の評価スクリプト・``scripts/evaluation/``・凍結 JSON だけをコピーし、``local_raw/`` は置かない。
- ``sitecustomize`` で ``local_raw`` / ``rawpy`` / ``lensfunpy`` の import を遮断する（環境に入っていても使えない）。
- Core Image CLI は偽 CLI（JSON リクエストを読み、決まった画像を書く）。

凍結画像の照合（ハッシュ・寸法・欠落）、凍結基準との比較と CI 出力の凍結、Core Image 候補 cases/*.json の
集計、evaluate_raw の現像済み画像評価、凍結監査・選定参照値・プリセット表の検査、旧経路の明示エラーを確かめる。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = (
    "compare_engines.py", "evaluate_candidates.py", "evaluate_raw.py",
    "freeze_baseline.py", "freeze_pso_parity.py", "export_presets_fixture.py",
)
COPY_TREES = ("scripts/evaluation", "scripts/eval_cases", "tests/fixtures/baseline")
COPY_FILES = (
    "tests/fixtures/pso-selection.json", "tests/fixtures/presets_legacy_rawpy.json",
    "src-tauri/tests/fixtures/presets.json",
)

BLOCKER = '''
import importlib.abc, sys
BLOCK = ("local_raw", "rawpy", "lensfunpy")
class _Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCK:
            raise ModuleNotFoundError(f"blocked for the post-removal test: {name}", name=name)
sys.meta_path.insert(0, _Block())
'''

#: 偽 Core Image CLI: --version / --capabilities に答え、リクエストの寸法で決まった画像を書く。
FAKE_CLI = '''#!{python}
import json, sys
import numpy as np
from PIL import Image
if "--version" in sys.argv:
    print(json.dumps({{"version": "0.6.0"}})); sys.exit(0)
if "--capabilities" in sys.argv:
    print(json.dumps({{"adjustments": True}})); sys.exit(0)
req = json.loads(sys.stdin.read())
w, h = 150, 100
if req["mode"] == "preview" and req.get("max_edge"):
    s = min(1.0, req["max_edge"] / max(w, h)); w, h = round(w * s), round(h * s)
y, x = np.indices((h, w))
shift = int(req.get("adjustments", {{}}).get("exposure", 0) * 10)
rgb = np.stack([(x * 255 // max(1, w - 1)), (y * 255 // max(1, h - 1)), np.full_like(x, 90 + shift)], axis=2)
Image.fromarray(rgb.astype("uint8")).save(req["output"], quality=95)
print(json.dumps({{"ok": True, "elapsed_ms": {{"total": 5, "decode": 1, "render_encode": 4}},
                  "applied": {{"lens_correction_enabled": req["settings"].get("lens_correction")}}}}))
'''


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry(root: Path, rel: str, capture_id: str, mode: str) -> dict:
    path = root / rel
    with Image.open(path) as image:
        rgb = np.asarray(image.convert("RGB"))
        size = list(image.size)
    return {"capture_id": capture_id, "mode": mode, "path": rel, "sha256": _sha(path),
            "rgb8_sha256": hashlib.sha256(rgb.tobytes()).hexdigest(), "dimensions": size}


class PostRemovalWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = cls.root = Path(cls.tmp.name) / "repo"
        for name in SCRIPTS:
            (root / "scripts").mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / "scripts" / name, root / "scripts" / name)
        for tree in COPY_TREES:
            shutil.copytree(REPO / tree, root / tree, ignore=shutil.ignore_patterns("__pycache__"))
        for rel in COPY_FILES:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, root / rel)
        assert not (root / "local_raw").exists()
        block = Path(cls.tmp.name) / "block"
        block.mkdir()
        (block / "sitecustomize.py").write_text(BLOCKER, encoding="utf-8")
        cls.env = {**os.environ, "PYTHONPATH": str(block), "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}

        # 現像元: 撮影 ID 2 件（ARW は中身を読まない偽物、JPG はカメラ JPEG 相当）
        cls.source = Path(cls.tmp.name) / "raw"
        cls.source.mkdir()
        for index, cid in enumerate(("DSC00001", "DSC00002")):
            (cls.source / f"{cid}.ARW").write_bytes(b"not a raw file")
            y, x = np.indices((100, 150))
            rgb = np.stack([x * 255 // 149, y * 255 // 99, np.full_like(x, 60 + 40 * index)], axis=2).astype("uint8")
            Image.fromarray(rgb).save(cls.source / f"{cid}.JPG", quality=92)
        cls.cli = Path(cls.tmp.name) / "fake-ci"
        cls.cli.write_text(FAKE_CLI.format(python=sys.executable), encoding="utf-8")
        cls.cli.chmod(0o755)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_script(self, name: str, *args: object, ok: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run([sys.executable, str(self.root / "scripts" / name), *map(str, args)],
                              cwd=self.root, env=self.env, capture_output=True, text=True, timeout=300)
        detail = f"{name} {args}\nstdout:\n{proc.stdout[-3000:]}\nstderr:\n{proc.stderr[-3000:]}"
        if ok:
            self.assertEqual(proc.returncode, 0, detail)
        else:
            self.assertNotEqual(proc.returncode, 0, detail)
        self.assertNotIn("Traceback", proc.stderr, detail)
        return proc

    def frozen_set(self, name: str) -> tuple[Path, Path]:
        """凍結ルート（work/baseline/rawpy/{preview,full}）とマニフェストを作る。"""
        base = Path(self.tmp.name) / name
        rows = []
        for cid in ("DSC00001", "DSC00002"):
            for mode, size in (("preview", (120, 80)), ("full", (150, 100))):
                rel = f"work/baseline/rawpy/{mode}/{cid}-neutral_recovery.jpg"
                (base / rel).parent.mkdir(parents=True, exist_ok=True)
                with Image.open(self.source / f"{cid}.JPG") as camera:
                    camera.convert("RGB").resize(size).save(base / rel, quality=90)
                rows.append(_entry(base, rel, cid, mode))
        manifest = base / "rawpy_images.json"
        manifest.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        return base, manifest

    # --- 環境そのもの ---------------------------------------------------------------

    def test_app_and_raw_libraries_are_unavailable(self):
        proc = subprocess.run(
            [sys.executable, "-c", textwrap.dedent("""
                import importlib, sys
                for name in ("local_raw", "rawpy", "lensfunpy"):
                    try:
                        importlib.import_module(name)
                    except ModuleNotFoundError:
                        continue
                    sys.exit(f"{name} importable")
                sys.path.insert(0, "scripts")
                import compare_engines, evaluate_candidates, evaluate_raw, freeze_pso_parity, export_presets_fixture
                import freeze_baseline
                print("ok")
            """)], cwd=self.root, env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    # --- 凍結基準との比較（compare_engines --frozen-manifest） -------------------------

    def test_frozen_compare_capture_and_replay(self):
        base, manifest = self.frozen_set("frozen-ok")
        out = Path(self.tmp.name) / "cmp-out"
        capture = Path(self.tmp.name) / "capture" / "ci_frozen.json"
        proc = self.run_script("compare_engines.py", "--frozen-manifest", manifest, "--frozen-root", base,
                               "--source-root", self.source, "--cli", self.cli, "--out", out, "--frozen-capture", capture)
        self.assertIn("凍結物の照合: PASS（4 件）", proc.stdout)
        report = json.loads((out / "frozen_comparison.json").read_text())
        self.assertEqual(report["errors"], [])
        self.assertEqual(len(report["rows"]), 4)
        for row in report["rows"]:
            self.assertGreater(row["frozen_delta_e76"], 0.0)  # 偽 CI とカメラ JPEG は違う画像
            self.assertEqual((row["ci_width"], row["ci_height"]), (row["frozen_width"], row["frozen_height"]))
            self.assertEqual(row["ci_profile"], "apple_default")
        self.assertTrue((out / "sheets" / "DSC00001_preview_frozen.jpg").is_file())
        self.assertTrue(json.loads((out / "frozen_verification.json").read_text())["ok"])
        # 凍結した CI 出力を基準に再実行すると完全一致（OS 更新前後の回帰確認の形）
        captured = json.loads(capture.read_text())
        self.assertEqual(len(captured), 4)
        self.assertEqual(captured[0]["request"]["adjustments"]["exposure"], 0.0)
        out2 = Path(self.tmp.name) / "cmp-replay"
        self.run_script("compare_engines.py", "--frozen-manifest", capture, "--frozen-root", out,
                        "--source-root", self.source, "--cli", self.cli, "--out", out2)
        replay = json.loads((out2 / "frozen_comparison.json").read_text())
        for row in replay["rows"]:
            self.assertEqual(row["frozen_identical_fraction"], 1.0)
            self.assertEqual(row["frozen_delta_e76"], 0.0)
        # 凍結ルート自身を --out にすると上書きを拒否する
        self.run_script("compare_engines.py", "--frozen-manifest", capture, "--frozen-root", out,
                        "--source-root", self.source, "--cli", self.cli, "--out", out, ok=False)

    def test_frozen_missing_modified_and_resized_artifacts_fail(self):
        for case in ("missing", "modified", "dimensions", "pixels"):
            with self.subTest(case=case):
                base, manifest = self.frozen_set(f"frozen-{case}")
                rows = json.loads(manifest.read_text())
                target = base / rows[0]["path"]
                if case == "missing":
                    target.unlink()
                elif case == "modified":
                    target.write_bytes(target.read_bytes() + b"\0")
                elif case == "dimensions":
                    rows[0]["dimensions"] = [121, 80]
                    manifest.write_text(json.dumps(rows), encoding="utf-8")
                else:
                    rows[0]["rgb8_sha256"] = "0" * 64
                    manifest.write_text(json.dumps(rows), encoding="utf-8")
                out = Path(self.tmp.name) / f"bad-{case}"
                capture = Path(self.tmp.name) / f"bad-{case}-capture.json"
                capture.write_text("[\"complete previous baseline\"]", encoding="utf-8")
                proc = self.run_script("compare_engines.py", "--frozen-manifest", manifest, "--frozen-root", base,
                                       "--source-root", self.source, "--cli", self.cli, "--out", out,
                                       "--frozen-capture", capture, ok=False)
                self.assertIn("DSC00001 preview", proc.stderr)
                self.assertFalse(json.loads((out / "frozen_verification.json").read_text())["ok"])
                self.assertFalse((out / "frozen_comparison.json").exists())
                self.assertEqual(capture.read_text(), "[\"complete previous baseline\"]")
                self.run_script("compare_engines.py", "--frozen-manifest", manifest, "--frozen-root", base,
                                "--frozen-verify-only", "--out", out, ok=False)

    def test_render_failure_does_not_replace_existing_capture(self):
        base, manifest = self.frozen_set("frozen-render-fail")
        source = Path(self.tmp.name) / "raw-partial"
        shutil.copytree(self.source, source)
        (source / "DSC00002.ARW").unlink()  # 2 件目は描画できない
        capture = Path(self.tmp.name) / "partial-capture.json"
        capture.write_text("[\"complete previous baseline\"]", encoding="utf-8")
        out = Path(self.tmp.name) / "render-fail-out"
        self.run_script("compare_engines.py", "--frozen-manifest", manifest, "--frozen-root", base,
                        "--source-root", source, "--cli", self.cli, "--out", out, "--frozen-capture", capture, ok=False)
        report = json.loads((out / "frozen_comparison.json").read_text())
        self.assertEqual(len(report["errors"]), 2)
        self.assertEqual(capture.read_text(), "[\"complete previous baseline\"]")

    # --- compare_engines の既定比較（rawpy 列なし）と旧経路の明示エラー --------------------

    def test_default_compare_has_no_rawpy_column_and_legacy_modes_need_opt_in(self):
        out = Path(self.tmp.name) / "default-compare"
        self.run_script("compare_engines.py", "--source-root", self.source, "--captures", "DSC00001",
                        "--cli", self.cli, "--out", out, "--max-edge", "120")
        summary = json.loads((out / "summary.json").read_text())
        self.assertEqual(summary["errors"], [])
        engines = {line.split(",")[2] for line in (out / "metrics.csv").read_text().splitlines()[1:]}
        self.assertEqual(engines, {"camera", "coreimage", "coreimage_lens"})
        self.assertFalse(summary["environment"]["legacy_rawpy"])
        # CI 列だけで回るモード: rawpy 列は欠測エラーではなく「省略」
        for mode, csv_name in (("--sharpness", "sharpness_metrics.csv"), ("--night", "night_metrics.csv"),
                               ("--highlight", "highlight_metrics.csv")):
            mode_out = Path(self.tmp.name) / f"mode{mode}"
            proc = self.run_script("compare_engines.py", "--source-root", self.source, "--captures", "DSC00001",
                                   "--cli", self.cli, "--out", mode_out, "--max-edge", "120", mode)
            self.assertNotIn("ERROR", proc.stdout, mode)
            rawpy_rows = [line for line in (mode_out / csv_name).read_text().splitlines() if ",rawpy," in line]
            self.assertTrue(rawpy_rows and all("legacy_rawpy" in line for line in rawpy_rows), mode)
        for mode in ("--bracket", "--adjustments"):
            proc = self.run_script("compare_engines.py", "--source-root", self.source, "--captures", "DSC00001",
                                   "--cli", self.cli, "--out", Path(self.tmp.name) / f"legacy{mode}", mode, ok=False)
            self.assertIn("削除前のコミットの checkout", proc.stderr)
        # 旧経路を opt-in しても本体が無ければ前提を説明して落ちる（rawpy 列のワーカー）
        proc = self.run_script("compare_engines.py", "--source-root", self.source, "--worker-rawpy", "DSC00001",
                               "--worker-out", Path(self.tmp.name) / "worker", ok=False)
        self.assertIn("local_raw", proc.stderr)

    # --- 候補評価（Rust ハーネスの cases/*.json を集計） -----------------------------------

    def test_candidate_aggregate_of_coreimage_cases(self):
        out = Path(self.tmp.name) / "candidates"
        (out / "cases").mkdir(parents=True)
        metric_keys = ("highlight_clip", "shadow_clip", "noise_flat", "detail", "halo", "color_saturation_clip",
                       "tonal_range", "source_deviation", "shadow_mean", "star_count")

        def metrics(base: float, **override):
            return {**{k: base for k in metric_keys}, "star_count": 10, **override}

        for index, cid in enumerate(("DSC00001", "DSC00002")):
            record = {
                "root": "stars", "capture_id": cid, "preset": "nightscape", "seed": 1, "budget": 48,
                "proxy": {"source": "coreimage_proxy", "decodes": 1, "renders": 12, "max_edge": 640},
                "elapsed_ms": {"total": 3000.0 + index * 1000},
                "star_scene": {"is_star_scene": True, "star_count": 10},
                "rejected": [{"id": "foreground", "reason": "暗部が基準より明るくならないため除外"}],
                "candidates": [
                    {"id": "natural", "adjustments": {"highlight_recovery": 55.0, "exposure": 0.0},
                     "metrics": metrics(0.1), "raw_metrics": metrics(0.1),
                     "raw_star_ungated": {"star_count": 10}},
                    {"id": "stars", "adjustments": {"highlight_recovery": 85.0, "exposure": 0.5},
                     "metrics": metrics(0.12), "raw_metrics": metrics(0.15, highlight_clip=0.3),
                     "raw_star_ungated": {"star_count": 12}},
                    {"id": "low_noise", "adjustments": {"highlight_recovery": 55.0, "exposure": 0.0},
                     "metrics": metrics(0.09, noise_flat=0.05), "raw_metrics": metrics(0.11, noise_flat=0.04)},
                ],
            }
            (out / "cases" / f"stars__{cid}__nightscape.json").write_text(json.dumps(record), encoding="utf-8")
        self.run_script("evaluate_candidates.py", "--aggregate-only", "--out", out)
        payload = json.loads((out / "summary.json").read_text())
        summary = payload["summary"]
        self.assertEqual(summary["cases"], 2)
        self.assertEqual(summary["cases_with_raw_metrics"], 2)
        self.assertEqual(summary["acceptance"]["engine_sources"], ["coreimage_proxy"])
        self.assertIsNone(summary["acceptance"]["authoritative_ci_acceptance"])
        self.assertEqual(summary["acceptance"]["broken_candidates"], "inherited_python_threshold_diagnostic")
        self.assertEqual({row["style"] for row in summary["broken_candidates"]}, {"stars"})
        self.assertEqual(summary["generation_seconds"]["n"], 2)
        self.assertAlmostEqual(summary["generation_seconds"]["mean"], 3.5)
        self.assertEqual(summary["proxy_vs_raw"]["highlight_clip"]["n"], 6)
        self.assertEqual(summary["rejected_by_style"], {"foreground": 2})
        markdown = (out / "summary.md").read_text()
        self.assertIn("参考診断", markdown)
        # 生成・現像はこのスクリプトでは行わない（旧経路は明示 opt-in で、本体が無ければ止まる）
        proc = self.run_script("evaluate_candidates.py", "--case", "DSC00001:nightscape", "--source-root", self.source,
                               "--out", Path(self.tmp.name) / "cand-gen", ok=False)
        self.assertIn("--aggregate-only", proc.stderr)

    # --- evaluate_raw（現像済み画像・凍結画像をカメラ JPEG と比べる） ------------------------

    def test_evaluate_raw_developed_and_frozen(self):
        base, manifest = self.frozen_set("frozen-evalraw")
        developed = Path(self.tmp.name) / "developed"
        developed.mkdir()
        for cid in ("DSC00001", "DSC00002"):
            with Image.open(self.source / f"{cid}.JPG") as camera:
                camera.convert("RGB").resize((120, 80)).save(developed / f"{cid}_preview_ci.jpg")
        out = Path(self.tmp.name) / "evalraw"
        proc = self.run_script("evaluate_raw.py", "--source-root", self.source, "--output-root", out,
                               "--developed", f"ci={developed}", "--frozen-manifest", manifest, "--frozen-root", base)
        result = json.loads(proc.stdout)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["rows"], 6)
        labels = {row["preset"] for row in result["summary"]}
        self.assertEqual(labels, {"ci", "frozen:preview", "frozen:full"})
        # 欠落した凍結画像は失敗
        (base / json.loads(manifest.read_text())[1]["path"]).unlink()
        self.run_script("evaluate_raw.py", "--source-root", self.source, "--output-root", out,
                        "--frozen-manifest", manifest, "--frozen-root", base, ok=False)
        # 旧経路（プリセット現像）は opt-in。本体が無ければ前提を説明して止まる
        proc = self.run_script("evaluate_raw.py", "--source-root", self.source, "--output-root", out, ok=False)
        self.assertIn("--legacy-rawpy", proc.stderr)

    # --- 凍結スクリプトの監査・参照値・プリセット表 ---------------------------------------

    def test_freeze_scripts_audit_and_reference_checks(self):
        proc = self.run_script("freeze_baseline.py", "--audit-only")
        self.assertIn("privacy audit: PASS", proc.stdout)
        # --verify-frozen-root: rawpy_images.json の凍結画像を照合する（コピー先の JSON を合成データに差し替え）
        base, manifest = self.frozen_set("frozen-freeze")
        rawpy_images = self.root / "tests/fixtures/baseline/rawpy_images.json"
        original = rawpy_images.read_text()
        try:
            shutil.copy2(manifest, rawpy_images)
            proc = self.run_script("freeze_baseline.py", "--verify-frozen-root", base)
            self.assertIn("frozen rawpy images: PASS (4 verified)", proc.stdout)
            (base / json.loads(manifest.read_text())[2]["path"]).unlink()
            self.run_script("freeze_baseline.py", "--verify-frozen-root", base, ok=False)
        finally:
            rawpy_images.write_text(original)
        proc = self.run_script("freeze_baseline.py", ok=False)
        self.assertIn("--legacy-generate", proc.stderr)
        proc = self.run_script("freeze_baseline.py", "--legacy-generate", ok=False)
        self.assertIn("削除前のコミットの checkout", proc.stderr)
        before = {p.name: _sha(p) for p in (self.root / "tests/fixtures/baseline").iterdir()}
        self.assertEqual(self.run_script("freeze_pso_parity.py", "--check").stdout.strip(), "pso-selection: ok")
        self.run_script("freeze_pso_parity.py", "--real-images", ok=False)
        self.assertEqual(self.run_script("export_presets_fixture.py", "--check").stdout.strip(), "ok")
        self.run_script("export_presets_fixture.py", "--check-source", ok=False)
        after = {p.name: _sha(p) for p in (self.root / "tests/fixtures/baseline").iterdir()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
