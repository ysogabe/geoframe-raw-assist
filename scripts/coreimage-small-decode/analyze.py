#!/usr/bin/env python3
"""Issue #102 の計測ハーネス（main.swift）の出力を集計する。

  python3 scripts/coreimage-small-decode/analyze.py timing work/small-decode/res
  python3 scripts/coreimage-small-decode/analyze.py quality work/small-decode/dump
  python3 scripts/coreimage-small-decode/analyze.py exposure work/small-decode/dump

timing: timing.jsonl / sweep.jsonl / io.jsonl のウォームアップを除いた反復から、方式ごとの中央値と p90
（全サンプルをまとめたもの）とサンプル別の中央値を出す。
quality: dump の float32（extended linear sRGB）・PNG・tiff16 から ΔE76（compare_engines.delta_e76 と同じ式。
8bit sRGB に直してから OpenCV の Lab）を出す。画像は読むだけ。
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compare_engines import delta_e76, image_metrics, luma_of  # noqa: E402


# ---------------------------------------------------------------------------
# timing
# ---------------------------------------------------------------------------


def percentile(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), q))


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def summarize_timing(rows: list[dict], keys: tuple[str, ...] = ("total",)) -> None:
    rows = [r for r in rows if not r.get("warm")]
    variants: list[str] = []
    for r in rows:
        if r["variant"] not in variants:
            variants.append(r["variant"])
    samples = sorted({r["sample"] for r in rows})
    print("| variant | n | " + " | ".join(f"{k} 中央値 | {k} p90" for k in keys) + " |")
    print("|---|---|" + "---|---|" * len(keys))
    for v in variants:
        vr = [r for r in rows if r["variant"] == v]
        cells = []
        for k in keys:
            vals = [float(r[k]) for r in vr if k in r and float(r[k]) >= 0]
            cells.append(f"{statistics.median(vals):.1f} | {percentile(vals, 90):.1f}" if vals else "- | -")
        print(f"| {v} | {len(vr)} | " + " | ".join(cells) + " |")
    print()
    print("サンプル別の total 中央値（ms）")
    print("| variant | " + " | ".join(samples) + " |")
    print("|---|" + "---|" * len(samples))
    for v in variants:
        cells = []
        for s in samples:
            vals = [float(r["total"]) for r in rows if r["variant"] == v and r["sample"] == s]
            cells.append(f"{statistics.median(vals):.0f}" if vals else "-")
        print(f"| {v} | " + " | ".join(cells) + " |")
    # 寸法（w×h）が記録されている方式は、寸法の一覧も出す。
    dims: dict[str, set[str]] = {}
    for r in rows:
        if "w" in r and "h" in r:
            dims.setdefault(r["variant"], set()).add(f"{r['w']}x{r['h']}")
    if dims:
        print()
        print("出力寸法: " + "; ".join(f"{v}: {', '.join(sorted(d))}" for v, d in dims.items()))
    print()


def cmd_timing(res: Path) -> None:
    timing = load_jsonl(res / "timing.jsonl")
    if timing:
        print("## timing（ローカルコピー）\n")
        summarize_timing(timing, ("total", "create", "create_configure", "graph", "render", "cli_decode", "cli_render_encode"))
    sweep = load_jsonl(res / "sweep.jsonl")
    if sweep:
        print("## sweep（scaleFactor / draft、レンズ補正 ON・後段なし・RGBAh レンダ）\n")
        summarize_timing(sweep, ("total", "create", "render"))
    io = load_jsonl(res / "io.jsonl")
    if io:
        print("## io（SMB / ローカル / メモリ）\n")
        summarize_timing(io, ("total", "create", "render"))


# ---------------------------------------------------------------------------
# quality
# ---------------------------------------------------------------------------


def srgb_encode(linear: np.ndarray) -> np.ndarray:
    x = np.clip(linear, 0.0, 1.0)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def to8(linear: np.ndarray) -> np.ndarray:
    return np.round(srgb_encode(linear) * 255.0).astype(np.uint8)


def load_f32(directory: Path, key: str, meta: dict) -> np.ndarray:
    w, h = int(meta[key]["w"]), int(meta[key]["h"])
    data = np.fromfile(directory / f"{key}.f32", dtype="<f4").reshape(h, w, 4)
    # CIContext.render(toBitmap:) は左上原点の行順で書く（PNG と同じ向き）。
    return data[..., :3].astype(np.float64)


def load_tiff16(path: Path) -> np.ndarray:
    bgr = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if bgr is None:
        raise FileNotFoundError(path)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def load_png(path: Path) -> np.ndarray:
    bgra = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if bgra is None:
        raise FileNotFoundError(path)
    return cv2.cvtColor(bgra, cv2.COLOR_BGRA2RGB if bgra.shape[2] == 4 else cv2.COLOR_BGR2RGB)


def de_map(a8: np.ndarray, b8: np.ndarray) -> np.ndarray:
    lab_a = cv2.cvtColor(a8.astype(np.float32) / 255.0, cv2.COLOR_RGB2Lab)
    lab_b = cv2.cvtColor(b8.astype(np.float32) / 255.0, cv2.COLOR_RGB2Lab)
    return np.sqrt(((lab_a - lab_b) ** 2).sum(axis=2))


def de_stats(approx8: np.ndarray, truth8: np.ndarray) -> dict[str, float]:
    """平均 ΔE76（delta_e76）、p95、真値の輝度上位 10% の画素での平均 ΔE。"""
    m = de_map(approx8, truth8)
    luma = luma_of(truth8.astype(np.float32) / 255.0)
    top = luma >= np.percentile(luma, 90)
    return {
        "mean": delta_e76(approx8, truth8),
        "p95": float(np.percentile(m, 95)),
        "top10": float(m[top].mean()),
    }


def lap_var(rgb8: np.ndarray) -> float:
    return float(cv2.Laplacian(cv2.cvtColor(rgb8, cv2.COLOR_RGB2GRAY).astype(np.float32), cv2.CV_32F).var())


def diag_gain_fit(ref: np.ndarray, truth: np.ndarray) -> np.ndarray:
    """ref·g ≈ truth のチャンネル別ゲイン（最小二乗。どちらかが 0.95 以上の画素は除く）。楽観的な下限。"""
    mask = (ref.max(axis=2) < 0.95) & (truth.max(axis=2) < 0.95) & (ref.min(axis=2) > 1e-4)
    gains = []
    for c in range(3):
        r, t = ref[..., c][mask], truth[..., c][mask]
        gains.append(float((r * t).sum() / max(1e-12, (r * r).sum())))
    return np.asarray(gains)


def fmt(stats: dict[str, float]) -> str:
    return f"{stats['mean']:.2f} / {stats['p95']:.2f} / {stats['top10']:.2f}"


def cmd_quality(dump: Path) -> None:
    samples = sorted(p for p in dump.iterdir() if (p / "meta.json").exists())
    out: dict[str, dict] = {}
    for d in samples:
        meta = json.loads((d / "meta.json").read_text())
        name = d.name
        ref = load_f32(d, "dec_ref", meta)
        ref8 = to8(ref)
        row: dict[str, object] = {"iso": meta.get("iso")}

        # exposure: デコード段の真値 vs 基準（EV 0）の線形ゲイン 2^EV。
        for ev in (-2.0, -1.0, -0.5, -0.25, 0.25, 0.5, 1.0, 2.0):
            key = f"dec_ev{ev:+.2f}"
            truth = load_f32(d, key, meta)
            truth8 = to8(truth)
            row[f"ev{ev:+.2f}_gain"] = de_stats(to8(ref * 2.0**ev), truth8)
            row[f"ev{ev:+.2f}_none"] = de_stats(ref8, truth8)
            row[f"ev{ev:+.2f}_mean_luma_truth_vs_gain"] = (
                float(luma_of(truth8 / 255.0).mean()),
                float(luma_of(to8(ref * 2.0**ev) / 255.0).mean()),
            )
        # 格子 0.5 EV のセル内（±0.25）を隣の格子点からゲインで出す: +0.25 は +0.5 から 2^-0.25、等。
        e05 = load_f32(d, "dec_ev+0.50", meta)
        row["grid_+0.25_from_+0.50"] = de_stats(to8(e05 * 2.0**-0.25), to8(load_f32(d, "dec_ev+0.25", meta)))
        em05 = load_f32(d, "dec_ev-0.50", meta)
        row["grid_-0.25_from_-0.50"] = de_stats(to8(em05 * 2.0**0.25), to8(load_f32(d, "dec_ev-0.25", meta)))

        # wb_temp: 何もしない場合と、チャンネル別ゲイン（真値に最小二乗で合わせた楽観的な下限）。
        for wb in (-50, -20, 20, 50):
            key = f"dec_wb{wb:+d}"
            truth = load_f32(d, key, meta)
            truth8 = to8(truth)
            g = diag_gain_fit(ref, truth)
            row[f"wb{wb:+d}_none"] = de_stats(ref8, truth8)
            row[f"wb{wb:+d}_diag_fit"] = de_stats(to8(ref * g), truth8)
            row[f"wb{wb:+d}_gains"] = [round(float(x), 4) for x in g]

        # sharpness ≤ 1 / noise_reduction / draft / レンズ補正: 基準との差。
        for key in ("dec_sharp0.80", "dec_sharp0.90", "dec_nr_off", "dec_nr_full", "dec_draft", "dec_lens_off"):
            img8 = to8(load_f32(d, key, meta))
            row[key] = de_stats(img8, ref8)
            row[f"{key}_lapvar_ratio"] = lap_var(img8) / max(1e-9, lap_var(ref8))
            row[f"{key}_flat_noise"] = (image_metrics(img8)["flat_noise"], image_metrics(ref8)["flat_noise"])

        # 埋め込みプレビュー: CI 既定（dec_ref = apple_default・レンズ補正 ON・後段なし）の寸法に縮小して比べる。
        for key in ("preview_imageio", "preview_ciraw"):
            path = d / f"{key}.png"
            if not path.exists():
                continue
            prev = load_png(path)
            resized = cv2.resize(prev, (ref8.shape[1], ref8.shape[0]), interpolation=cv2.INTER_AREA)
            m_prev = image_metrics(resized)
            m_ref = image_metrics(ref8)
            row[key] = {
                "size": f"{prev.shape[1]}x{prev.shape[0]}",
                "vs_ref": de_stats(resized, ref8),
                "vs_lens_off": de_stats(resized, to8(load_f32(d, "dec_lens_off", meta))),
                "mean_luma_prev": m_prev["mean_luma"],
                "mean_luma_ref": m_ref["mean_luma"],
                "mean_luma_ev+1": image_metrics(to8(load_f32(d, "dec_ev+1.00", meta)))["mean_luma"],
                "mean_luma_ev-1": image_metrics(to8(load_f32(d, "dec_ev-1.00", meta)))["mean_luma"],
                "clip_high_any_prev": m_prev["clip_high_any"],
                "clip_high_any_ref": m_ref["clip_high_any"],
                "sat_prev": m_prev["mean_saturation"],
                "sat_ref": m_ref["mean_saturation"],
                "lapvar_ratio": lap_var(resized) / max(1e-9, lap_var(ref8)),
            }

        # キャッシュ経路（RGBAh / RGBAf → 後段）と本番 develop()（tiff16 sRGB）の一致。
        for key in ("post_preset", "post_strong"):
            truth16 = load_tiff16(d / f"{key}_develop.tiff")
            for suffix in ("cacheH", "cacheF"):
                cand16 = load_tiff16(d / f"{key}_{suffix}.tiff")
                diff = np.abs(cand16.astype(np.int64) - truth16.astype(np.int64))
                row[f"{key}_{suffix}"] = {
                    "max_abs_16bit": int(diff.max()),
                    "mean_abs_16bit": float(diff.mean()),
                    "identical": bool(diff.max() == 0),
                    "de": de_stats(np.round(cand16 / 257.0).astype(np.uint8), np.round(truth16 / 257.0).astype(np.uint8)),
                }
        # 後段（preset）まで通した exposure のゲイン近似。
        for ev in (-1.0, -0.5, -0.25, 0.25, 0.5, 1.0):
            truth16 = load_tiff16(d / f"postev{ev:+.2f}_develop.tiff")
            cand16 = load_tiff16(d / f"postev{ev:+.2f}_gain.tiff")
            row[f"postev{ev:+.2f}"] = de_stats(np.round(cand16 / 257.0).astype(np.uint8), np.round(truth16 / 257.0).astype(np.uint8))
        out[name] = row

    (dump / "quality.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))

    def table(title: str, keys: list[str]) -> None:
        print(f"### {title}（平均 ΔE76 / p95 / 輝度上位10%の平均）\n")
        print("| 比較 | " + " | ".join(f"{n} (ISO {out[n]['iso']})" for n in out) + " | 中央値(mean) |")
        print("|---|" + "---|" * (len(out) + 1))
        for k in keys:
            cells = []
            means = []
            for n in out:
                v = out[n][k]
                stats = v["de"] if isinstance(v, dict) and "de" in v else (v["vs_ref"] if isinstance(v, dict) and "vs_ref" in v else v)
                cells.append(fmt(stats))
                means.append(stats["mean"])
            print(f"| {k} | " + " | ".join(cells) + f" | {statistics.median(means):.2f} |")
        print()

    table("exposure: 線形ゲイン 2^EV で近似（gain）と何もしない（none）", [
        f"ev{ev:+.2f}_{kind}" for ev in (-2.0, -1.0, -0.5, -0.25, 0.25, 0.5, 1.0, 2.0) for kind in ("gain", "none")
    ] + ["grid_+0.25_from_+0.50", "grid_-0.25_from_-0.50"])
    table("exposure: 後段（neutral_recovery）まで通した場合のゲイン近似", [f"postev{ev:+.2f}" for ev in (-1.0, -0.5, -0.25, 0.25, 0.5, 1.0)])
    table("wb_temp: 何もしない / チャンネル別ゲイン（真値に当てはめた楽観的下限）", [
        f"wb{wb:+d}_{kind}" for wb in (-50, -20, 20, 50) for kind in ("none", "diag_fit")
    ])
    table("デコード段の他の設定と基準（dec_ref）の差", ["dec_sharp0.80", "dec_sharp0.90", "dec_nr_off", "dec_nr_full", "dec_draft", "dec_lens_off"])
    table("キャッシュ経路 vs 本番 develop()（tiff16 sRGB を 8bit 化）", ["post_preset_cacheH", "post_preset_cacheF", "post_strong_cacheH", "post_strong_cacheF"])
    table("埋め込みプレビュー vs CI 既定（dec_ref）", ["preview_imageio", "preview_ciraw"])

    print("### 補助指標\n")
    for n, row in out.items():
        cache = {k: (row[k]["max_abs_16bit"], round(row[k]["mean_abs_16bit"], 2)) for k in row if k.startswith("post_") and k.endswith(("cacheH", "cacheF"))}
        lap = {k: round(row[f"{k}_lapvar_ratio"], 3) for k in ("dec_sharp0.80", "dec_sharp0.90", "dec_nr_off", "dec_nr_full", "dec_draft", "dec_lens_off")}
        prev = {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in row[k].items() if kk != "vs_ref" and kk != "vs_lens_off"} for k in ("preview_imageio", "preview_ciraw") if k in row}
        wbg = {k: row[k] for k in row if k.endswith("_gains")}
        print(f"- {n}: cache max/mean abs(16bit)={cache}; lapvar 比={lap}")
        print(f"  - preview: {prev}")
        print(f"  - preview vs lens_off: " + ", ".join(f"{k}={fmt(row[k]['vs_lens_off'])}" for k in ('preview_imageio', 'preview_ciraw') if k in row))
        print(f"  - wb gains: {wbg}")


# ---------------------------------------------------------------------------
# exposure（デコード後に exposure を近似する方法の比較）
# ---------------------------------------------------------------------------

LUT_EDGES = np.concatenate([[0.0], np.geomspace(1e-4, 1.2, 400)])


def build_lut(pairs: list[tuple[np.ndarray, np.ndarray]]) -> list[tuple[np.ndarray, np.ndarray]]:
    """(入力, 真値) の組からチャンネル別の 1 次元 LUT（ビンごとの中央値、単調化）を作る。"""
    luts = []
    for c in range(3):
        xs = np.concatenate([a[..., c].ravel() for a, _ in pairs])
        ys = np.concatenate([b[..., c].ravel() for _, b in pairs])
        idx = np.digitize(xs, LUT_EDGES)
        centers, values = [], []
        for i in range(1, len(LUT_EDGES)):
            m = idx == i
            if m.sum() >= 20:
                centers.append(float(np.median(xs[m])))
                values.append(float(np.median(ys[m])))
        luts.append((np.asarray(centers), np.maximum.accumulate(np.asarray(values))))
    return luts


def apply_lut(luts: list[tuple[np.ndarray, np.ndarray]], image: np.ndarray) -> np.ndarray:
    out = np.empty_like(image)
    for c, (cx, cy) in enumerate(luts):
        out[..., c] = np.interp(image[..., c], cx, cy)
    return out


def cmd_exposure(dump: Path) -> None:
    """exposure の近似: 線形ゲイン 2^ΔEV / leave-one-out の LUT（他のサンプルで作ったチャンネル別 1D LUT）/
    隣り合う 2 つの格子点デコードの補間（算術・幾何平均）。いずれも dec_* は後段なしのデコード出力。"""
    names = sorted(p.name for p in dump.iterdir() if (p / "meta.json").exists())
    keys = ["dec_ref"] + [f"dec_ev{ev:+.2f}" for ev in (-2.0, -1.0, -0.5, -0.25, 0.25, 0.5, 1.0, 2.0)]
    data = {}
    for n in names:
        meta = json.loads((dump / n / "meta.json").read_text())
        data[n] = {k: load_f32(dump / n, k, meta) for k in keys}

    def row(label: str, stats: list[dict[str, float]]) -> None:
        means = [s["mean"] for s in stats]
        tops = [s["top10"] for s in stats]
        print(f"| {label} | {statistics.median(means):.2f} | {max(means):.2f} | {statistics.median(tops):.2f} | {max(tops):.2f} |")

    print("### exposure の近似（7 サンプル。平均 ΔE76 の中央値 / 最大、輝度上位10% の平均 ΔE の中央値 / 最大）\n")
    print("| 近似（元 → 目標） | ΔE 中央値 | ΔE 最大 | 上位10% 中央値 | 上位10% 最大 |")
    print("|---|---|---|---|---|")
    pairs = [("dec_ref", "dec_ev+0.25", 0.25), ("dec_ref", "dec_ev-0.25", -0.25), ("dec_ref", "dec_ev+0.50", 0.5),
             ("dec_ref", "dec_ev-0.50", -0.5), ("dec_ref", "dec_ev+1.00", 1.0), ("dec_ref", "dec_ev-1.00", -1.0),
             ("dec_ref", "dec_ev+2.00", 2.0), ("dec_ev-1.00", "dec_ref", 1.0), ("dec_ev-0.50", "dec_ev+0.50", 1.0)]
    for src, dst, ev in pairs:
        label = f"{src.replace('dec_ref', 'EV0').replace('dec_ev', 'EV')} → {dst.replace('dec_ref', 'EV0').replace('dec_ev', 'EV')}"
        row(f"{label} 何もしない", [de_stats(to8(data[n][src]), to8(data[n][dst])) for n in names])
        row(f"{label} ゲイン 2^{ev:+.2f}", [de_stats(to8(data[n][src] * 2.0**ev), to8(data[n][dst])) for n in names])
        loo = []
        for n in names:
            luts = build_lut([(data[o][src], data[o][dst]) for o in names if o != n])
            loo.append(de_stats(to8(apply_lut(luts, data[n][src])), to8(data[n][dst])))
        row(f"{label} LUT（leave-one-out）", loo)
    for mid, lo, hi in (("dec_ev+0.25", "dec_ref", "dec_ev+0.50"), ("dec_ev-0.25", "dec_ev-0.50", "dec_ref")):
        label = f"{lo.replace('dec_ref', 'EV0').replace('dec_ev', 'EV')} と {hi.replace('dec_ref', 'EV0').replace('dec_ev', 'EV')} から {mid.replace('dec_ev', 'EV')}"
        row(f"{label} 算術平均", [de_stats(to8(0.5 * (data[n][lo] + data[n][hi])), to8(data[n][mid])) for n in names])
        row(f"{label} 幾何平均", [
            de_stats(to8(np.sqrt(np.clip(data[n][lo], 0, None) * np.clip(data[n][hi], 0, None))), to8(data[n][mid])) for n in names
        ])
    print()


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("timing", "quality", "exposure"):
        sys.exit(__doc__)
    {"timing": cmd_timing, "quality": cmd_quality, "exposure": cmd_exposure}[sys.argv[1]](Path(sys.argv[2]))
