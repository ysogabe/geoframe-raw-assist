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
import json, os, sys
import numpy as np
from PIL import Image
OLD = bool(os.environ.get("FAKE_CLI_OLD"))
if "--version" in sys.argv:
    print(json.dumps({{"version": "0.6.0", "cli_version": "0.2.0" if OLD else "0.6.0"}})); sys.exit(0)
if "--capabilities" in sys.argv:
    print(json.dumps({{"adjustments": {{"supported": not OLD}}}})); sys.exit(0)
req = json.loads(sys.stdin.read())
fail = os.environ.get("FAKE_CLI_FAIL")
if fail and fail in req["input"]:
    print(json.dumps({{"ok": False, "error": {{"code": "render_failed", "message": "fake failure"}}}})); sys.exit(2)
w, h = 150, 100
if req["mode"] == "preview" and req.get("max_edge"):
    s = min(1.0, req["max_edge"] / max(w, h)); w, h = round(w * s), round(h * s)
y, x = np.indices((h, w))
shift = int(req.get("adjustments", {{}}).get("exposure", 0) * 10)
rgb = np.stack([(x * 255 // max(1, w - 1)), (y * 255 // max(1, h - 1)), np.full_like(x, 90 + shift)], axis=2)
Image.fromarray(rgb.astype("uint8")).save(req["output"], quality=95)
if os.environ.get("FAKE_CLI_MOVE_SOURCE") and req["mode"] == "full":  # 描画後に現像元が消える（共有の切断・移動）状況の再現
    os.rename(req["input"], req["input"] + ".moved")
print(json.dumps({{"ok": True, "cli_version": "0.6.0", "macos_version": "fake-os", "decoder_version": req.get("decoder_version"),
                  "input": req["input"], "output": req["output"], "width": w, "height": h,
                  "elapsed_ms": {{"total": 5, "decode": 1, "render_encode": 4}},
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

    def run_script(self, name: str, *args: object, ok: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
        proc = subprocess.run([sys.executable, str(self.root / "scripts" / name), *map(str, args)],
                              cwd=self.root, env={**self.env, **(env or {})}, capture_output=True, text=True, timeout=300)
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
        self.assertTrue((out / "sheets" / "DSC00001_preview_01_frozen.jpg").is_file())
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

    def test_frozen_duplicate_capture_mode_entries_keep_distinct_outputs(self):
        """同じ撮影 ID・mode の行が request 違いで複数あっても、出力・シート・凍結ハッシュが上書きで壊れない。"""
        base, manifest = self.frozen_set("frozen-dup")
        rows = json.loads(manifest.read_text())
        preview = next(r for r in rows if r["capture_id"] == "DSC00001" and r["mode"] == "preview")
        presets = json.loads((REPO / "src-tauri/tests/fixtures/presets.json").read_text())["presets"]
        adjustments = dict(next(p for p in presets if p["name"] == "neutral_recovery")["adjustments"])
        requests = []
        for exposure in (0.0, 1.0):
            requests.append({"profile": "apple_default", "lens": True, "decoder_version": 9,
                             "adjustments": {**adjustments, "exposure": exposure}})
        manifest.write_text(json.dumps([dict(preview, request=r) for r in requests]), encoding="utf-8")
        out = Path(self.tmp.name) / "dup-out"
        capture = Path(self.tmp.name) / "dup-capture" / "ci_frozen.json"
        self.run_script("compare_engines.py", "--frozen-manifest", manifest, "--frozen-root", base,
                        "--source-root", self.source, "--cli", self.cli, "--out", out, "--frozen-capture", capture)
        report = json.loads((out / "frozen_comparison.json").read_text())
        self.assertEqual(report["errors"], [])
        outputs = [row["output"] for row in report["rows"]]
        self.assertEqual(len(set(outputs)), 2)
        self.assertEqual(len(list((out / "sheets").glob("DSC00001_preview_*_frozen.jpg"))), 2)
        captured = json.loads(capture.read_text())
        self.assertEqual([c["request"]["adjustments"]["exposure"] for c in captured], [0.0, 1.0])
        self.assertEqual(len({c["path"] for c in captured}), 2)
        self.assertEqual(len({c["sha256"] for c in captured}), 2)  # 画素も違う（後の行で上書きされていない）
        for entry in captured:
            self.assertEqual(_sha(out / entry["path"]), entry["sha256"])
        out2 = Path(self.tmp.name) / "dup-replay"
        self.run_script("compare_engines.py", "--frozen-manifest", capture, "--frozen-root", out,
                        "--source-root", self.source, "--cli", self.cli, "--out", out2)
        replay = json.loads((out2 / "frozen_comparison.json").read_text())
        self.assertEqual(len(replay["rows"]), 2)
        for row in replay["rows"]:
            self.assertEqual(row["frozen_identical_fraction"], 1.0)

    def test_frozen_outputs_overlapping_inputs_are_rejected_before_any_write(self):
        """--out / --frozen-capture が凍結入力と重なるなら、照合レポートも含めて何も書かずに拒否する。"""

        def snapshot(root: Path) -> dict[str, str]:
            return {p.relative_to(root).as_posix(): _sha(p) for p in sorted(root.rglob("*")) if p.is_file()}

        base, manifest = self.frozen_set("frozen-guard")
        malformed = base / "malformed.json"
        malformed.write_text("{not json", encoding="utf-8")
        nested_out = base / "work" / "out"
        cases = {
            "out-is-root": ("--out", base),
            "out-inside-root": ("--out", nested_out),
            "out-contains-root": ("--out", base.parent),
        }
        for name, (flag, out) in cases.items():
            for manifest_path in (manifest, malformed):
                for verify_only in (False, True):
                    with self.subTest(case=name, manifest=manifest_path.name, verify_only=verify_only):
                        before = snapshot(base)
                        extra = ["--frozen-verify-only"] if verify_only else [
                            "--source-root", self.source, "--cli", self.cli]
                        proc = self.run_script("compare_engines.py", "--frozen-manifest", manifest_path,
                                               "--frozen-root", base, flag, out, *extra, ok=False)
                        self.assertIn("凍結入力を上書きしうる出力先", proc.stderr)
                        self.assertEqual(snapshot(base), before)
                        self.assertFalse(nested_out.exists())
        # --out が別でも、--frozen-capture が凍結マニフェスト自身・凍結ルートの中なら拒否する
        for capture in (manifest, base / "work" / "ci_frozen.json"):
            with self.subTest(capture=str(capture)):
                before = snapshot(base)
                out = Path(self.tmp.name) / "guard-capture-out"
                proc = self.run_script("compare_engines.py", "--frozen-manifest", manifest, "--frozen-root", base,
                                       "--source-root", self.source, "--cli", self.cli, "--out", out,
                                       "--frozen-capture", capture, ok=False)
                self.assertIn("--frozen-capture", proc.stderr)
                self.assertEqual(snapshot(base), before)
                self.assertFalse(out.exists())

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

    # --- 凍結画像ゼロからの CI 基準作成（compare_engines --frozen-bootstrap-ci, #57 PR A） ---------

    def bootstrap(self, out: Path, *extra: object, ok: bool = True, env: dict | None = None,
                  captures: tuple[str, ...] = ("DSC00001", "DSC00002"), source: Path | None = None):
        return self.run_script("compare_engines.py", "--frozen-bootstrap-ci", "--source-root", source or self.source,
                               "--captures", *captures, "--cli", self.cli, "--out", out, "--max-edge", "120", *extra,
                               ok=ok, env=env)

    @staticmethod
    def tree(root: Path) -> dict[str, str]:
        return {p.relative_to(root).as_posix(): _sha(p) for p in sorted(root.rglob("*")) if p.is_file()}

    def test_bootstrap_creates_baseline_then_verifies_and_replays(self):
        out = Path(self.tmp.name) / "bootstrap-ok" / "ci"
        proc = self.bootstrap(out)
        self.assertIn("自己照合 PASS", proc.stdout)
        self.assertEqual([p.name for p in out.parent.iterdir()], ["ci"])  # 一時ディレクトリは残らない
        entries = json.loads((out / "ci_frozen.json").read_text())
        self.assertEqual([(e["capture_id"], e["mode"]) for e in entries],
                         [("DSC00001", "preview"), ("DSC00001", "full"), ("DSC00002", "preview"), ("DSC00002", "full")])
        presets = json.loads((REPO / "src-tauri/tests/fixtures/presets.json").read_text())["presets"]
        neutral = next(p for p in presets if p["name"] == "neutral_recovery")["adjustments"]
        for entry in entries:
            request = entry["request"]
            self.assertEqual((request["profile"], request["lens"], request["decoder_version"]), ("apple_default", True, "9"))
            self.assertEqual(request["adjustments"], neutral)
            self.assertEqual(len(request["adjustments"]), 9)
            self.assertEqual(request.get("max_edge"), 120 if entry["mode"] == "preview" else None)
            self.assertFalse(Path(entry["path"]).is_absolute())
            self.assertEqual(entry["source"]["sha256"], _sha(self.source / f"{entry['capture_id']}.ARW"))
            self.assertEqual(entry["source"]["file"], f"{entry['capture_id']}.ARW")
        self.assertEqual(entries[0]["dimensions"], [120, 80])
        self.assertEqual(entries[1]["dimensions"], [150, 100])
        # 来歴・応答・マニフェストに絶対パス（一時ディレクトリ）を持ち込まない
        provenance = json.loads((out / "ci_frozen_provenance.json").read_text())
        self.assertEqual(provenance["cli"]["version"]["cli_version"], "0.6.0")
        self.assertEqual(provenance["cli"]["sha256"], _sha(self.cli))
        self.assertEqual(provenance["cli"]["file"], "fake-ci")
        self.assertEqual(provenance["cli"]["response_versions"][0]["macos_version"], "fake-os")
        self.assertEqual(provenance["requests"]["preview"]["max_edge"], 120)
        for path in out.rglob("*.json"):
            text = path.read_text()
            self.assertNotIn(self.tmp.name, text, path.name)
            self.assertNotIn("GPS", text.upper().replace("GPS_", ""), path.name)
        # 自己完結: 照合 → 同じ現像元での再比較は完全一致（preview の長辺も request から再現）
        self.run_script("compare_engines.py", "--frozen-manifest", out / "ci_frozen.json", "--frozen-root", out,
                        "--frozen-verify-only", "--out", Path(self.tmp.name) / "bootstrap-verify")
        replay_out = Path(self.tmp.name) / "bootstrap-replay"
        before = self.tree(out)
        self.run_script("compare_engines.py", "--frozen-manifest", out / "ci_frozen.json", "--frozen-root", out,
                        "--source-root", self.source, "--cli", self.cli, "--out", replay_out)
        replay = json.loads((replay_out / "frozen_comparison.json").read_text())
        self.assertEqual(len(replay["rows"]), 4)
        for row in replay["rows"]:
            self.assertEqual((row["frozen_identical_fraction"], row["frozen_delta_e76"]), (1.0, 0.0))
        self.assertEqual(self.tree(out), before)  # 基準は読むだけ

    def test_bootstrap_accepts_existing_empty_directory_and_preset_choice(self):
        out = Path(self.tmp.name) / "bootstrap-empty"
        out.mkdir()
        self.bootstrap(out, "--frozen-preset", "nightscape", captures=("DSC00002",))
        entries = json.loads((out / "ci_frozen.json").read_text())
        self.assertEqual(len(entries), 2)
        presets = json.loads((REPO / "src-tauri/tests/fixtures/presets.json").read_text())["presets"]
        night = next(p for p in presets if p["name"] == "nightscape")["adjustments"]
        self.assertEqual(entries[0]["request"]["adjustments"], night)

    def test_bootstrap_never_overwrites_or_uses_unsafe_outputs(self):
        base = Path(self.tmp.name) / "bootstrap-unsafe"
        base.mkdir()
        # 空でない出力先（既存の基準）は一切変えずに拒否する
        existing = base / "existing"
        self.bootstrap(existing)
        before = self.tree(existing)
        proc = self.bootstrap(existing, ok=False)
        self.assertIn("空ではありません", proc.stderr)
        self.assertEqual(self.tree(existing), before)
        stray = base / "stray"
        stray.mkdir()
        (stray / "keep.txt").write_text("keep")
        self.bootstrap(stray, ok=False)
        self.assertEqual(self.tree(stray), {"keep.txt": _sha(stray / "keep.txt")})
        # ファイル、現像元と同じ・現像元の中・現像元を含む場所
        (base / "file").write_text("x")
        self.assertIn("ディレクトリではありません", self.bootstrap(base / "file", ok=False).stderr)
        for label, target in (("same", self.source), ("inside", self.source / "out"), ("parent", self.source.parent)):
            with self.subTest(source=label):
                proc = self.bootstrap(target, ok=False)
                self.assertTrue("重なります" in proc.stderr or "空ではありません" in proc.stderr, proc.stderr)
        self.assertFalse((self.source / "out").exists())
        self.assertEqual(sorted(p.name for p in self.source.iterdir()),
                         ["DSC00001.ARW", "DSC00001.JPG", "DSC00002.ARW", "DSC00002.JPG"])
        # Git 管理下（work/ 以外のリポジトリ内）は拒否、work/ 配下は許可
        proc = self.bootstrap(self.root / "tests" / "ci-baseline", ok=False)
        self.assertIn("Git 管理下", proc.stderr)
        self.assertFalse((self.root / "tests" / "ci-baseline").exists())
        self.bootstrap(self.root / "work" / "ci-baseline")
        self.assertTrue((self.root / "work" / "ci-baseline" / "ci_frozen.json").is_file())

    def test_bootstrap_rejects_bad_inputs_before_creating_anything(self):
        base = Path(self.tmp.name) / "bootstrap-bad"
        missing_arw = Path(self.tmp.name) / "raw-no-arw"
        shutil.copytree(self.source, missing_arw)
        (missing_arw / "DSC00002.ARW").unlink()
        cases = {
            "duplicate": (dict(captures=("DSC00001", "DSC00001")), None, "重複"),
            "malformed-id": (dict(captures=("DSC1",)), None, "撮影IDの形式"),
            "missing-arw": (dict(source=missing_arw), None, "ARW がありません"),
            "old-cli": ({}, {"FAKE_CLI_OLD": "1"}, "未対応"),
        }
        for name, (kwargs, env, message) in cases.items():
            with self.subTest(case=name):
                out = base / name
                proc = self.bootstrap(out, ok=False, env=env, **kwargs)
                self.assertIn(message, proc.stderr)
                self.assertFalse(out.exists())
                self.assertEqual(list(base.glob(".*")) if base.exists() else [], [])  # 一時ディレクトリも作らない
        self.assertFalse((base / "bad-preset").exists())
        proc = self.bootstrap(base / "bad-preset", "--frozen-preset", "no_such_preset", ok=False)
        self.assertIn("no_such_preset", proc.stderr)
        self.assertFalse((base / "bad-preset").exists())

    def test_bootstrap_render_failure_publishes_nothing(self):
        base = Path(self.tmp.name) / "bootstrap-fail"
        fresh = base / "fresh"
        proc = self.bootstrap(fresh, ok=False, env={"FAKE_CLI_FAIL": "DSC00002"})
        self.assertIn("DSC00002", proc.stderr)
        self.assertFalse(fresh.exists())
        leftovers = [p for p in base.iterdir()]
        self.assertEqual(len(leftovers), 1)  # 診断用の一時ディレクトリだけ。マニフェストは無い
        self.assertEqual(list(leftovers[0].rglob("ci_frozen.json")), [])
        self.assertFalse(json.loads((leftovers[0] / "bootstrap_report.json").read_text())["ok"])
        # 空の既存ディレクトリを指定した場合も空のまま
        empty = Path(self.tmp.name) / "bootstrap-fail-empty"
        empty.mkdir()
        self.bootstrap(empty, ok=False, env={"FAKE_CLI_FAIL": "DSC00001"})
        self.assertEqual(list(empty.iterdir()), [])
        # 現像元が描画中に変わっても公開しない場合の前提: 失敗後に同じ名前で再試行できる
        self.bootstrap(fresh)
        self.assertTrue((fresh / "ci_frozen.json").is_file())

    def test_bootstrap_source_lost_after_render_is_reported_not_traceback(self):
        """描画後の ARW 再確認で OSError（移動・共有切断）になっても、成功扱いにせず report / log を残して失敗する。"""
        base = Path(self.tmp.name) / "bootstrap-source-lost"
        source = base / "raw"
        shutil.copytree(self.source, source)
        out = base / "ci"
        proc = self.bootstrap(out, ok=False, source=source, captures=("DSC00001",), env={"FAKE_CLI_MOVE_SOURCE": "1"})
        self.assertIn("再確認できません", proc.stderr)
        self.assertFalse(out.exists())  # 基準は公開しない
        leftovers = [p for p in base.iterdir() if p.name.startswith(".ci.bootstrap-")]
        self.assertEqual(len(leftovers), 1)
        staging = leftovers[0]
        self.assertEqual(list(staging.rglob("ci_frozen.json")), [])
        report = json.loads((staging / "bootstrap_report.json").read_text())
        self.assertFalse(report["ok"])
        self.assertTrue(any("再確認できません" in e["error"] for e in report["errors"]))
        self.assertIn("失敗", (staging / "run.log").read_text())

    def test_bootstrap_rejects_incompatible_and_stray_options(self):
        out = Path(self.tmp.name) / "bootstrap-options"
        for extra in (("--frozen-manifest", self.root / "tests/fixtures/baseline/rawpy_images.json"),
                      ("--frozen-root", self.root), ("--frozen-capture", out / "x.json"), ("--frozen-verify-only",),
                      ("--select", "auto"), ("--full",), ("--full-limit", "5"), ("--full-limit", "0"),
                      ("--full-ids", "DSC00001"), ("--no-lens",), ("--sharpness",), ("--dry-run",),
                      ("--legacy-rawpy",), ("--adjustments",)):
            with self.subTest(extra=extra):
                proc = self.bootstrap(out, *extra, ok=False)
                self.assertIn("併用できない", proc.stderr)
                self.assertIn(extra[0], proc.stderr)
                self.assertFalse(out.exists())
        for args in (("--frozen-capture", out / "x.json"), ("--frozen-root", self.root), ("--frozen-preset", "nightscape")):
            with self.subTest(stray=args[0]):
                proc = self.run_script("compare_engines.py", "--source-root", self.source, "--captures", "DSC00001",
                                       "--cli", self.cli, "--out", Path(self.tmp.name) / "stray-out", *args, ok=False)
                self.assertIn("黙って無視しません", proc.stderr)
        base, manifest = self.frozen_set("frozen-verify-capture")
        proc = self.run_script("compare_engines.py", "--frozen-manifest", manifest, "--frozen-root", base,
                               "--frozen-verify-only", "--frozen-capture", out / "x.json", ok=False)
        self.assertIn("併用できません", proc.stderr)
        for flag in ("--source-root", "--captures", "--out"):
            args = {"--source-root": ["--captures", "DSC00001", "--out", out],
                    "--captures": ["--source-root", self.source, "--out", out],
                    "--out": ["--source-root", self.source, "--captures", "DSC00001"]}[flag]
            proc = self.run_script("compare_engines.py", "--frozen-bootstrap-ci", "--cli", self.cli, *args, ok=False)
            self.assertIn("が必要です", proc.stderr)
        self.assertFalse(out.exists())

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
        # Rust ハーネスの出力には frames/ が無い: --contact-sheets は黙って空にせず、警告して集計だけ行う
        proc = self.run_script("evaluate_candidates.py", "--aggregate-only", "--out", out, "--contact-sheets")
        self.assertIn("frames", proc.stderr)
        self.assertIn("2/2", proc.stderr)
        self.assertFalse((out / "contact_sheets").exists())
        self.assertEqual(json.loads((out / "summary.json").read_text())["summary"]["cases"], 2)
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
