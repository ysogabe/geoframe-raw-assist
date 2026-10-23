#!/usr/bin/env python3
"""現像候補自動生成の質を測る評価スクリプト（集計は Core Image 候補・旧 rawpy 候補の共通形式）。

**既定経路（Core Image / 現行）**: Rust の候補生成ハーネスが書いた ``<out>/cases/*.json``
（``metrics`` = CI 640px 代理、``raw_metrics`` = 同じ adjustments の CI 原寸を 640px に縮小した指標）を
``--aggregate-only --out <out>`` で集計する。アプリ本体（local_raw）・rawpy・lensfunpy は使わない。
旧 Python の安全策閾値（``CLIP_WORSEN_LIMIT`` 等）による列は「旧閾値の参考診断」と明記し、
CI の受け入れ判定には使わない（summary の ``acceptance``）。

**旧経路（アーカイブ、``--legacy-rawpy`` の明示 opt-in）**: 以下は ``local_raw`` と rawpy が残る
checkout でだけ動く旧 rawpy 候補生成の評価（docs/development/evaluation-paths.md）。

候補生成は ARW を ``half_size`` デコードした代理画像で指標を計算する。このスクリプトは
各候補の調整値で **本番相当の実RAW現像**（full-size デコード → レンズ補正 →
``engine.apply_post_decode_chain``）を行い、候補生成と同じ指標関数・同じ star_gate 条件で
実RAW側の指標を計算して、代理と実RAWのずれ・壊れた候補・スタイルの狙い達成度などを集計する。
チューニング前後で同じケース集合を回し、``summary.json`` を比較するための基準値取りに使う。

- 現像は engine の関数だけで行い（独自実装しない）、``engine.develop_raw`` は呼ばない
  （``outputs/`` へ書き込まないため）。現像元フォルダにも書き込まない。
- ``config.source_root`` はこのプロセス内だけ差し替える（アプリ設定は変更しない）。
- 出力は既定で ``outputs/eval_candidates/<timestamp>/``（gitignore 対象）。

評価ケースは ``--case DSC02849:neutral_recovery``（繰り返し可。プリセット省略時は
neutral_recovery）か ``--cases-file``（``scripts/eval_cases/*.json``）で与える。
ケース定義ファイルは絶対パスを持たず、フォルダ名（root キー）だけを参照する。
実フォルダは ``--root キー=パス``（繰り返し可）で渡す。``--source-root`` は ``default`` キーの別名。

例:
    # 既定: Rust ハーネスの cases/*.json（Core Image 候補）を集計する
    python scripts/evaluate_candidates.py --aggregate-only --out work/issue57/<run>

    # --contact-sheets は <out>/frames/<key>__jpg.jpg と <out>/frames/<key>__<style>.jpg（と raw_metrics）が
    # 揃っているときだけ一覧を作る。Rust ハーネスは frames/ を書かないので、その出力では一覧はできない
    # （指定すると警告して集計だけ行う）。frames/ を書くのは旧経路の生成。

    # 旧経路（アーカイブ。削除前の checkout + requirements-raw.txt）
    .venv/bin/python scripts/evaluate_candidates.py --legacy-rawpy \
      --source-root /Volumes/KIOXIA/image_work \
      --case DSC02849:neutral_recovery --case DSC03203:nightscape --contact-sheets

    .venv/bin/python scripts/evaluate_candidates.py --legacy-rawpy \
      --cases-file scripts/eval_cases/candidates_baseline.json \
      --root image_work=/Volumes/KIOXIA/image_work --root stars=work/raw --contact-sheets

    # 既存の出力ディレクトリの cases/*.json から summary だけ作り直す
    .venv/bin/python scripts/evaluate_candidates.py --aggregate-only --out outputs/eval_candidates/<timestamp>
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import sys
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence
from unittest import mock

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 評価用の独立サポート（scripts/evaluation/）。アプリ本体・rawpy・lensfunpy に依存しない。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from evaluation import candidate_metrics as cm  # noqa: E402
from evaluation import legacy  # noqa: E402
from evaluation.presets import DEFAULT_PRESET, PRESET_NAMES as PRESETS  # noqa: E402

#: 旧 Python 安全策の閾値に基づく列の位置づけ（CI の受け入れ判定ではない）。
INHERITED_DIAGNOSTIC = "inherited_python_threshold_diagnostic"

CAPTURE_ID_PATTERN = re.compile(r"^DSC\d{5}$")
#: root キー名はケースキー（= 出力ファイル名）に入るため、安全な文字だけを許す。
ROOT_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
DEFAULT_ROOT_KEY = "default"
DEFAULT_OUT_PARENT = PROJECT_ROOT / "outputs" / "eval_candidates"

#: 代理 vs 実RAW を比べる指標。
COMPARE_METRICS: tuple[str, ...] = (
    "highlight_clip",
    "shadow_clip",
    "noise_flat",
    "detail",
    "halo",
    "color_saturation_clip",
    "tonal_range",
    "source_deviation",
    "shadow_mean",
    "star_count",
)
#: 実RAWで natural とほぼ同じ画像とみなす平均絶対画素差（0..1）。
NEAR_NATURAL_DEVIATION = 0.01
#: 実RAW指標の計算に使う長辺（代理画像と同じ）。
COMPARE_EDGE = cm.MAX_PROXY_EDGE
#: コンタクトシートの1コマの高さ・キャプション高さ。
SHEET_FRAME_HEIGHT = 380
SHEET_CAPTION_HEIGHT = 62
FRAME_EDGE = 480


# ---------------------------------------------------------------------------
# ケース指定のパース・ファイル解決（実データ不要で単体テストできる部分）
# ---------------------------------------------------------------------------


def validate_root_key(key: str) -> str:
    """root キー名を検証する（英数字・``_``・``-`` のみ。ファイル名に使うため）。"""

    if not isinstance(key, str) or not ROOT_KEY_PATTERN.fullmatch(key):
        raise ValueError(f"root キー名は英数字・_・- だけで指定してください: {key!r}")
    return key


def parse_case_spec(spec: str, root: str = DEFAULT_ROOT_KEY) -> dict[str, str]:
    """``DSC02849:neutral_recovery`` → ``{"root", "capture_id", "preset"}``（プリセット省略可）。"""

    validate_root_key(root)
    parts = [part.strip() for part in str(spec).split(":")]
    if len(parts) == 1:
        capture_id, preset = parts[0], DEFAULT_PRESET
    elif len(parts) == 2 and parts[1]:
        capture_id, preset = parts
    else:
        raise ValueError(f"ケース指定は CAPTURE_ID[:PRESET] の形式で指定してください: {spec!r}")
    if not CAPTURE_ID_PATTERN.fullmatch(capture_id):
        raise ValueError(f"撮影IDが不正です（DSC + 5桁）: {capture_id!r}")
    if preset not in PRESETS:
        raise ValueError(f"未対応のプリセットです: {preset!r}（{', '.join(sorted(PRESETS))}）")
    return {"root": root, "capture_id": capture_id, "preset": preset}


def load_cases_file(path: Path) -> list[dict[str, str]]:
    """ケース定義 JSON を読む。

    形式: ``{"sets": [{"root": "<キー>", "cases": ["DSC04267:neutral_recovery", ...]}, ...]}``
    """

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    sets = payload.get("sets") if isinstance(payload, dict) else None
    if not isinstance(sets, list) or not sets:
        raise ValueError("ケース定義には空でない sets 配列が必要です。")
    cases: list[dict[str, str]] = []
    for entry in sets:
        root = entry.get("root") if isinstance(entry, dict) else None
        specs = entry.get("cases") if isinstance(entry, dict) else None
        if not isinstance(root, str) or not root or not isinstance(specs, list):
            raise ValueError("sets の各要素には root（文字列）と cases（配列）が必要です。")
        validate_root_key(root)
        cases.extend(parse_case_spec(spec, root) for spec in specs)
    return cases


def parse_root_args(root_args: Sequence[str] | None, source_root: Path | None) -> dict[str, Path]:
    """``--root キー=パス`` と ``--source-root`` から root キー→ディレクトリの表を作る。"""

    roots: dict[str, Path] = {}
    if source_root is not None:
        roots[DEFAULT_ROOT_KEY] = Path(source_root).expanduser().resolve()
    for item in root_args or []:
        key, sep, value = str(item).partition("=")
        if not sep or not key.strip() or not value.strip():
            raise ValueError(f"--root は キー=パス の形式で指定してください: {item!r}")
        roots[validate_root_key(key.strip())] = Path(value.strip()).expanduser().resolve()
    return roots


def check_roots(cases: Iterable[dict[str, str]], roots: dict[str, Path]) -> None:
    """ケースが参照する root キーがすべて解決でき、ディレクトリが存在することを確認する。"""

    needed = sorted({case["root"] for case in cases})
    missing = [key for key in needed if key not in roots]
    if missing:
        hint = " ".join(f"--root {key}=<フォルダ>" for key in missing)
        raise ValueError(f"root キーが未指定です: {', '.join(missing)}（例: {hint}）")
    for key in needed:
        if not roots[key].is_dir():
            raise ValueError(f"root {key!r} のフォルダがありません: {roots[key]}")


def find_source_file(root: Path, capture_id: str, extensions: Sequence[str]) -> Path | None:
    """``capture_id`` + 拡張子のファイルを、大文字小文字を問わず実ファイル名で解決する。"""

    wanted = {f"{capture_id}.{ext.lstrip('.')}".lower() for ext in extensions}
    try:
        entries = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return None
    for path in entries:
        if path.name.lower() in wanted and path.is_file():
            return path
    return None


# ---------------------------------------------------------------------------
# 集計（純粋関数。records は run_case が返す dict のリスト）
# ---------------------------------------------------------------------------


def rankdata(values: Sequence[float]) -> np.ndarray:
    """平均順位（1始まり、同順位は平均）。scipy に依存しない。"""

    array = np.asarray(values, dtype=np.float64)
    order = np.argsort(array, kind="mergesort")
    ranks = np.empty(len(array), dtype=np.float64)
    sorted_values = array[order]
    i = 0
    while i < len(array):
        j = i
        while j < len(array) - 1 and sorted_values[j + 1] == sorted_values[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """スピアマン順位相関。3点未満・どちらかが定数のときは None。"""

    if len(x) != len(y):
        raise ValueError("x と y の長さが違います。")
    if len(x) < 3:
        return None
    xa = np.asarray(x, dtype=np.float64)
    ya = np.asarray(y, dtype=np.float64)
    if np.ptp(xa) == 0 or np.ptp(ya) == 0:
        return None
    return float(np.corrcoef(rankdata(xa), rankdata(ya))[0, 1])


def _valid_candidates(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in record.get("candidates", []) if c.get("raw_metrics")]


def _natural(record: dict[str, Any]) -> dict[str, Any] | None:
    return next((c for c in _valid_candidates(record) if c["id"] == "natural"), None)


def compare_proxy_vs_raw(records: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """指標ごとの代理 vs 実RAW: 写真内スピアマン平均・プールしたスピアマン・MAE・bias(raw-proxy)。"""

    summary: dict[str, dict[str, Any]] = {}
    for metric in COMPARE_METRICS:
        pooled_proxy: list[float] = []
        pooled_raw: list[float] = []
        within: list[float] = []
        for record in records:
            candidates = _valid_candidates(record)
            proxy = [float(c["metrics"][metric]) for c in candidates]
            raw = [float(c["raw_metrics"][metric]) for c in candidates]
            pooled_proxy.extend(proxy)
            pooled_raw.extend(raw)
            rho = spearman(proxy, raw)
            if rho is not None:
                within.append(rho)
        if pooled_proxy:
            diff = np.asarray(pooled_raw) - np.asarray(pooled_proxy)
            mae, bias = float(np.mean(np.abs(diff))), float(np.mean(diff))
        else:
            mae = bias = None
        summary[metric] = {
            "n": len(pooled_proxy),
            "within_photo_spearman_mean": float(np.mean(within)) if within else None,
            "within_photo_n": len(within),
            "pooled_spearman": spearman(pooled_proxy, pooled_raw),
            "mae": mae,
            "bias_raw_minus_proxy": bias,
        }
    return summary


def find_broken_candidates(
    records: Sequence[dict[str, Any]], limit: float = cm.CLIP_WORSEN_LIMIT
) -> list[dict[str, Any]]:
    """実RAWで natural 比 shadow_clip / highlight_clip が ``limit`` 以上悪化した候補。

    ``limit`` の既定は旧 Python の安全策閾値。Core Image 候補では参考診断であって合否ではない。
    """

    broken: list[dict[str, Any]] = []
    for record in records:
        natural = _natural(record)
        if natural is None:
            continue
        for candidate in _valid_candidates(record):
            if candidate["id"] == "natural":
                continue
            d_shadow = candidate["raw_metrics"]["shadow_clip"] - natural["raw_metrics"]["shadow_clip"]
            d_high = candidate["raw_metrics"]["highlight_clip"] - natural["raw_metrics"]["highlight_clip"]
            # 浮動小数の丸めで境界値が落ちないよう微小な許容を入れる。
            if d_shadow >= limit - 1e-9 or d_high >= limit - 1e-9:
                broken.append(
                    {
                        "case": record["key"],
                        "style": candidate["id"],
                        "raw_delta_shadow_clip": round(d_shadow, 6),
                        "raw_delta_highlight_clip": round(d_high, 6),
                        "proxy_delta_shadow_clip": round(
                            candidate["metrics"]["shadow_clip"] - natural["metrics"]["shadow_clip"], 6
                        ),
                        "proxy_delta_highlight_clip": round(
                            candidate["metrics"]["highlight_clip"] - natural["metrics"]["highlight_clip"], 6
                        ),
                    }
                )
    return broken


#: スタイルごとの狙いと、(実RAW/代理の) natural 比の判定。
STYLE_GOALS: dict[str, str] = {
    "foreground": "暗部平均(shadow_mean)が natural より上がる",
    "low_noise": "平坦部ノイズ(noise_flat)が natural より下がる",
    "social": "階調幅(tonal_range)が natural より上がる",
    "stars": "星の数が natural より減らない（ゲート無しで測定）",
}


def style_goal_delta(style: str, candidate: dict[str, Any], natural: dict[str, Any]) -> dict[str, float | bool] | None:
    """1候補の狙い達成を natural 比で判定する。``raw_star_ungated`` が無い stars は None。"""

    raw, raw_n = candidate["raw_metrics"], natural["raw_metrics"]
    proxy, proxy_n = candidate["metrics"], natural["metrics"]
    if style == "foreground":
        raw_d = raw["shadow_mean"] - raw_n["shadow_mean"]
        proxy_d = proxy["shadow_mean"] - proxy_n["shadow_mean"]
        return {"raw_delta": raw_d, "proxy_delta": proxy_d, "raw_achieved": raw_d > 0, "proxy_achieved": proxy_d > 0}
    if style == "low_noise":
        raw_d = raw["noise_flat"] - raw_n["noise_flat"]
        proxy_d = proxy["noise_flat"] - proxy_n["noise_flat"]
        return {"raw_delta": raw_d, "proxy_delta": proxy_d, "raw_achieved": raw_d < 0, "proxy_achieved": proxy_d < 0}
    if style == "social":
        raw_d = raw["tonal_range"] - raw_n["tonal_range"]
        proxy_d = proxy["tonal_range"] - proxy_n["tonal_range"]
        return {"raw_delta": raw_d, "proxy_delta": proxy_d, "raw_achieved": raw_d > 0, "proxy_achieved": proxy_d > 0}
    if style == "stars":
        ungated, ungated_n = candidate.get("raw_star_ungated"), natural.get("raw_star_ungated")
        if not ungated or not ungated_n:
            return None
        raw_d = float(ungated["star_count"]) - float(ungated_n["star_count"])
        # 代理側は natural(ゲート無しなら0) との比較になるため、達成判定は実RAW(ゲート無し)を主とする。
        proxy_d = float(proxy["star_count"]) - float(proxy_n["star_count"])
        return {"raw_delta": raw_d, "proxy_delta": proxy_d, "raw_achieved": raw_d >= 0, "proxy_achieved": proxy_d >= 0}
    return None


def summarize_style_goals(records: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """スタイル別の狙い達成（件数・達成率・平均差）。"""

    rows: dict[str, list[dict[str, Any]]] = {style: [] for style in STYLE_GOALS}
    for record in records:
        natural = _natural(record)
        if natural is None:
            continue
        for candidate in _valid_candidates(record):
            style = candidate["id"]
            if style not in STYLE_GOALS:
                continue
            delta = style_goal_delta(style, candidate, natural)
            if delta is not None:
                rows[style].append({"case": record["key"], **delta})
    out: dict[str, dict[str, Any]] = {}
    for style, items in rows.items():
        n = len(items)
        out[style] = {
            "goal": STYLE_GOALS[style],
            "n": n,
            "raw_achieved": sum(bool(i["raw_achieved"]) for i in items),
            "proxy_achieved": sum(bool(i["proxy_achieved"]) for i in items),
            "raw_achieved_rate": (sum(bool(i["raw_achieved"]) for i in items) / n) if n else None,
            "proxy_achieved_rate": (sum(bool(i["proxy_achieved"]) for i in items) / n) if n else None,
            "mean_raw_delta": float(np.mean([i["raw_delta"] for i in items])) if items else None,
            "mean_proxy_delta": float(np.mean([i["proxy_delta"] for i in items])) if items else None,
            "failed_cases": [i["case"] for i in items if not i["raw_achieved"]],
        }
    return out


def find_near_natural(
    records: Sequence[dict[str, Any]], threshold: float = NEAR_NATURAL_DEVIATION
) -> list[dict[str, Any]]:
    """実RAWの natural との平均絶対画素差が ``threshold`` 未満の候補（実質 natural と同じ）。"""

    found: list[dict[str, Any]] = []
    for record in records:
        if _natural(record) is None:
            continue
        for candidate in _valid_candidates(record):
            if candidate["id"] == "natural":
                continue
            deviation = float(candidate["raw_metrics"]["source_deviation"])
            if deviation < threshold:
                found.append({"case": record["key"], "style": candidate["id"], "raw_mean_abs_diff": deviation})
    return found


def deviation_by_style(records: Sequence[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """スタイル別の natural との平均画素差（実RAW）の平均・最小・最大。"""

    values: dict[str, list[float]] = {}
    for record in records:
        for candidate in _valid_candidates(record):
            if candidate["id"] != "natural":
                values.setdefault(candidate["id"], []).append(float(candidate["raw_metrics"]["source_deviation"]))
    return {
        style: {"n": len(v), "mean": float(np.mean(v)), "min": float(min(v)), "max": float(max(v))}
        for style, v in sorted(values.items())
    }


def distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "mean": None, "median": None, "p90": None, "min": None, "max": None}
    array = np.asarray(values, dtype=np.float64)
    return {
        "n": len(values),
        "mean": float(np.mean(array)),
        "median": float(statistics.median(values)),
        "p90": float(np.percentile(array, 90)),
        "min": float(array.min()),
        "max": float(array.max()),
    }


def _generation_seconds(record: dict[str, Any]) -> float | None:
    """候補生成の所要秒（旧 rawpy は ``gen_elapsed``、Rust は ``elapsed_ms.total``）。"""

    if isinstance(record.get("gen_elapsed"), (int, float)):
        return float(record["gen_elapsed"])
    total = (record.get("elapsed_ms") or {}).get("total")
    return float(total) / 1000.0 if isinstance(total, (int, float)) else None


def summarize_counts(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """候補数・rejected 内訳・highlight カテゴリ・exposure の分布・star_scene・所要時間。"""

    candidate_counts = Counter(len(r["candidates"]) for r in records)
    rejected: Counter[str] = Counter()
    rejected_details: list[dict[str, str]] = []
    categories_all: Counter[str] = Counter()
    categories_non_natural: Counter[str] = Counter()
    exposures: Counter[str] = Counter()
    style_counts: Counter[str] = Counter()
    for record in records:
        for item in record.get("rejected", []):
            rejected[item["id"]] += 1
            rejected_details.append({"case": record["key"], "style": item["id"], "reason": item["reason"]})
        for candidate in record["candidates"]:
            style_counts[candidate["id"]] += 1
            category = cm.highlight_category(float(candidate["adjustments"]["highlight_recovery"]))
            categories_all[category] += 1
            if candidate["id"] != "natural":
                categories_non_natural[category] += 1
                exposures[f"{float(candidate['adjustments']['exposure']):+.1f}"] += 1
    star_rows = []
    for record in records:
        star = record.get("star_scene") or {}
        star_rows.append(
            {
                "case": record["key"],
                "is_star_scene": bool(star.get("is_star_scene")),
                "star_count": star.get("star_count"),
                "dark_background_ratio": star.get("dark_background_ratio"),
                "bright_point_ratio": star.get("bright_point_ratio"),
                "exif_night": star.get("exif_night"),
                "exif_state": star.get("exif_state"),
                "star_point_density": star.get("star_point_density"),
                "has_stars_candidate": any(c["id"] == "stars" for c in record["candidates"]),
                "stars_rejected": any(r["id"] == "stars" for r in record.get("rejected", [])),
            }
        )
    return {
        "candidate_count_histogram": {str(k): v for k, v in sorted(candidate_counts.items())},
        "style_counts": dict(sorted(style_counts.items())),
        "rejected_by_style": dict(sorted(rejected.items())),
        "rejected_details": rejected_details,
        "highlight_category_all": dict(categories_all),
        "highlight_category_non_natural": dict(categories_non_natural),
        "exposure_non_natural": dict(sorted(exposures.items(), key=lambda kv: float(kv[0]))),
        "star_scene": star_rows,
        "star_scene_true": sum(row["is_star_scene"] for row in star_rows),
        "generation_seconds": distribution([v for v in (_generation_seconds(r) for r in records) if v is not None]),
        "proxy_decodes": distribution(
            [float(r["proxy"]["decodes"]) for r in records if isinstance((r.get("proxy") or {}).get("decodes"), (int, float))]
        ),
        "real_develop_seconds": distribution(
            [
                float(c["raw_timing"]["t_total"])
                for r in records
                for c in r["candidates"]
                if c.get("raw_timing")
            ]
        ),
    }


def engine_sources(records: Sequence[dict[str, Any]]) -> list[str]:
    """レコードの候補生成経路（``proxy.source``。旧 rawpy レコードは source を持たない）。"""

    return sorted({str((r.get("proxy") or {}).get("source") or "legacy_rawpy_proxy") for r in records})


def acceptance_basis(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """summary の判定列の位置づけ。旧 Python の閾値・狙い判定は参考診断で、CI の受け入れ判定ではない。"""

    return {
        "engine_sources": engine_sources(records),
        "authoritative_ci_acceptance": None,
        "broken_candidates": INHERITED_DIAGNOSTIC,
        "style_goals": INHERITED_DIAGNOSTIC,
        "near_natural": INHERITED_DIAGNOSTIC,
        "clip_worsen_limit": cm.CLIP_WORSEN_LIMIT,
        "note": "broken_candidates / style_goals / near_natural は旧 Python の閾値・狙いを流用した参考診断。"
                "Core Image 候補の合否は実測に基づいて別途決める（この集計は合否を出さない）。",
    }


def build_summary(records: Sequence[dict[str, Any]], errors: Sequence[dict[str, Any]] | None = ()) -> dict[str, Any]:
    """評価レコード群から summary.json の内容を作る。

    ``errors`` が None のときは「エラー件数は不明」（元の summary が無い再集計）を表し、
    ``summary["errors"]`` も None にする（0件と区別する）。
    """

    ok = [r for r in records if _natural(r) is not None]
    return {
        "cases": len(records),
        "cases_with_raw_metrics": len(ok),
        "errors": None if errors is None else list(errors),
        "candidates_total": sum(len(r["candidates"]) for r in records),
        "acceptance": acceptance_basis(records),
        "proxy_vs_raw": compare_proxy_vs_raw(ok),
        "broken_candidates": find_broken_candidates(ok),
        "style_goals": summarize_style_goals(ok),
        "near_natural": find_near_natural(ok),
        "deviation_by_style": deviation_by_style(ok),
        **summarize_counts(records),
    }


def _fmt(value: Any, digits: int = 3, signed: bool = False) -> str:
    if value is None:
        return "-"
    if isinstance(value, (int, np.integer)):
        return str(value)
    return f"{value:+.{digits}f}" if signed else f"{value:.{digits}f}"


def render_markdown(summary: dict[str, Any], meta: dict[str, Any] | None = None) -> str:
    """summary を人が読める Markdown にする。"""

    lines: list[str] = ["# 現像候補 評価サマリ", ""]
    for key, value in (meta or {}).items():
        lines.append(f"- {key}: {value}")
    lines += [
        f"- ケース数: {summary['cases']}（実RAW指標あり {summary['cases_with_raw_metrics']}）",
        f"- 候補総数: {summary['candidates_total']}",
        f"- エラー: {'不明（再集計のため）' if summary['errors'] is None else len(summary['errors'])}",
        "",
        "## 代理 vs 実RAW",
        "",
        "| 指標 | n | 写真内ρ平均 | (写真数) | プールρ | MAE | bias(raw-proxy) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for metric, row in summary["proxy_vs_raw"].items():
        lines.append(
            f"| {metric} | {row['n']} | {_fmt(row['within_photo_spearman_mean'])} | {row['within_photo_n']} "
            f"| {_fmt(row['pooled_spearman'])} | {_fmt(row['mae'], 4)} | {_fmt(row['bias_raw_minus_proxy'], 4, True)} |"
        )
    acceptance = summary.get("acceptance") or {}
    if acceptance:
        lines += ["", "## 判定の位置づけ", "", f"- 経路: {acceptance.get('engine_sources')}", f"- {acceptance.get('note')}"]
    lines += ["", f"## 壊れた候補（参考診断: 旧 Python 閾値。実RAW/原寸で natural 比 shadow/highlight_clip が {cm.CLIP_WORSEN_LIMIT:.2f} 以上悪化）", ""]
    if summary["broken_candidates"]:
        lines += [
            "| ケース | スタイル | raw Δshadow | raw Δhighlight | proxy Δshadow | proxy Δhighlight |",
            "|---|---|---:|---:|---:|---:|",
        ]
        for row in summary["broken_candidates"]:
            lines.append(
                f"| {row['case']} | {row['style']} | {_fmt(row['raw_delta_shadow_clip'], 3, True)} "
                f"| {_fmt(row['raw_delta_highlight_clip'], 3, True)} | {_fmt(row['proxy_delta_shadow_clip'], 3, True)} "
                f"| {_fmt(row['proxy_delta_highlight_clip'], 3, True)} |"
            )
    else:
        lines.append("なし")
    lines += ["", "## スタイル別の狙い達成（natural 比。参考診断: 旧 Python の狙い）", "", "| スタイル | 狙い | n | 実RAW達成 | 代理達成 | 実RAW平均Δ | 代理平均Δ |", "|---|---|---:|---:|---:|---:|---:|"]
    for style, row in summary["style_goals"].items():
        lines.append(
            f"| {style} | {row['goal']} | {row['n']} | {row['raw_achieved']}/{row['n']} "
            f"| {row['proxy_achieved']}/{row['n']} | {_fmt(row['mean_raw_delta'], 4, True)} | {_fmt(row['mean_proxy_delta'], 4, True)} |"
        )
    for style, row in summary["style_goals"].items():
        if row["failed_cases"]:
            lines.append(f"\n- {style} 未達（実RAW）: {', '.join(row['failed_cases'])}")
    lines += ["", "## natural との差（実RAW平均絶対画素差）", "", "| スタイル | n | 平均 | 最小 | 最大 |", "|---|---:|---:|---:|---:|"]
    for style, row in summary["deviation_by_style"].items():
        lines.append(f"| {style} | {row['n']} | {_fmt(row['mean'], 4)} | {_fmt(row['min'], 4)} | {_fmt(row['max'], 4)} |")
    lines.append("")
    if summary["near_natural"]:
        lines.append(f"natural とほぼ同じ候補（参考診断。差 < {NEAR_NATURAL_DEVIATION}）:")
        for row in summary["near_natural"]:
            lines.append(f"- {row['case']} / {row['style']}（{row['raw_mean_abs_diff']:.4f}）")
    else:
        lines.append(f"natural とほぼ同じ候補（差 < {NEAR_NATURAL_DEVIATION}）: なし")
    lines += [
        "",
        "## 候補数・rejected",
        "",
        f"- 候補数の分布（候補数: 写真数）: {summary['candidate_count_histogram']}",
        f"- スタイル別の採用数: {summary['style_counts']}",
        f"- rejected（スタイル別）: {summary['rejected_by_style'] or 'なし'}",
    ]
    for row in summary["rejected_details"]:
        lines.append(f"  - {row['case']} / {row['style']}: {row['reason']}")
    lines += [
        "",
        "## highlight カテゴリ・exposure の分布",
        "",
        f"- highlight カテゴリ（全候補）: {summary['highlight_category_all']}",
        f"- highlight カテゴリ（natural 以外）: {summary['highlight_category_non_natural']}",
        f"- exposure（natural 以外, EV: 件数）: {summary['exposure_non_natural']}",
        "",
        f"## star_scene 判定（true: {summary['star_scene_true']}/{summary['cases']}）",
        "",
        "注: exif_night は exif_state == night のこと。point_density は測っていないとき（daylight 等）は - 表示。",
        "",
        "| ケース | is_star | star_count | dark_bg | bright_pt | exif_state | exif_night | point_density | stars候補 | stars却下 |",
        "|---|---|---:|---:|---:|---|---|---:|---|---|",
    ]
    for row in summary["star_scene"]:
        lines.append(
            f"| {row['case']} | {row['is_star_scene']} | {row['star_count']} | {_fmt(row['dark_background_ratio'])} "
            f"| {_fmt(row['bright_point_ratio'], 5)} | {row['exif_state']} | {row['exif_night']} "
            f"| {_fmt(row['star_point_density'], 6)} | {row['has_stars_candidate']} | {row['stars_rejected']} |"
        )
    lines += ["", "## 所要時間（秒）", "", "| 項目 | n | 平均 | 中央値 | p90 | 最小 | 最大 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for label, key in (
        ("候補生成", "generation_seconds"),
        ("代理デコード回数", "proxy_decodes"),
        ("実RAW現像(1候補)", "real_develop_seconds"),
    ):
        d = summary[key]
        lines.append(
            f"| {label} | {d['n']} | {_fmt(d['mean'], 1)} | {_fmt(d['median'], 1)} | {_fmt(d['p90'], 1)} | {_fmt(d['min'], 1)} | {_fmt(d['max'], 1)} |"
        )
    if summary["errors"] is None:
        lines += ["", "## エラー", "", "不明（元の summary.json が無い再集計のため）"]
    elif summary["errors"]:
        lines += ["", "## エラー", ""]
        lines += [f"- {e['case']}: {e['error']}" for e in summary["errors"]]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 実RAW現像・ケース実行
# ---------------------------------------------------------------------------


def develop_real_array(
    raw: Any,
    adjustments: dict[str, Any],
    lens_resolution: Any,
    dark_scene_bright_k: float | None = None,
    capture_exif: dict[str, Any] | None = None,
) -> tuple[np.ndarray, dict[str, Any], dict[str, float]]:
    """旧経路（アーカイブ）: engine.develop_raw と同じ手順（full デコード → レンズ補正 → 後段）を配列のまま行う。

    ``capture_exif`` はペアJPG の EXIF 要約（無ければ None）。RAW から ISO が取れないときの
    フォールバックにだけ使う。

    ファイルには何も書かない。戻り値の配列は長辺 ``COMPARE_EDGE`` に揃える前の PREVIEW サイズ。
    """

    engine = legacy.import_module("local_raw.engine", "旧 rawpy 候補の実RAW現像")
    lens = legacy.import_module("local_raw.lens", "旧 rawpy 候補の実RAW現像")
    started = time.monotonic()
    rgb = raw.postprocess(**engine._postprocess_params(raw, adjustments, True, dark_scene_bright_k))
    decode_seconds = time.monotonic() - started
    rgb = engine._decode_16bit_to_float01(rgb)
    rgb, lens_correction = lens.apply_lens_correction(
        rgb, lens_resolution, force_skip_vignetting=dark_scene_bright_k is not None
    )
    # ISO は develop_raw と同じ規則: RAW のメタデータ → 取れなければペアJPG の EXIF。
    iso = engine._raw_iso(raw)
    if iso is None:
        iso = engine._coerce_iso((capture_exif or {}).get("iso"))
    rgb = engine.apply_post_decode_chain(rgb, adjustments, engine.PREVIEW_MAX_EDGE, iso=iso)
    return rgb, lens_correction, {"t_decode": decode_seconds, "t_total": time.monotonic() - started}


def save_frame(rgb: np.ndarray, path: Path, edge: int = FRAME_EDGE) -> None:
    image = Image.fromarray(np.rint(np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8), mode="RGB")
    image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "JPEG", quality=90)


def case_key(case: dict[str, str]) -> str:
    """ケースの識別子（case JSON / frames / コンタクトシートのファイル名）。root を含めて衝突を避ける。"""

    return f"{validate_root_key(case['root'])}__{case['capture_id']}__{case['preset']}"


def save_pair_frame(jpg_path: Path, dest: Path, edge: int = FRAME_EDGE) -> None:
    """カメラ JPG を EXIF の向きを反映して縮小保存する（実RAW側のフレームと向きを揃える）。"""

    with Image.open(jpg_path) as pair:
        pair = ImageOps.exif_transpose(pair)
        pair = pair.convert("RGB")
        pair.thumbnail((edge, edge))
        dest.parent.mkdir(parents=True, exist_ok=True)
        pair.save(dest, "JPEG", quality=90)


#: --force で消す出力ディレクトリ・ファイル（--out 配下のこれらだけ。他は消さない）。
OUT_SUBDIRS: tuple[str, ...] = ("cases", "frames", "contact_sheets")
OUT_FILES: tuple[str, ...] = ("summary.json", "summary.md")


def prepare_out_dir(out_dir: Path, force: bool) -> None:
    """通常実行の出力先を準備する。既存の cases/*.json があれば、古い結果が混ざらないよう停止。

    ``force`` のときだけ ``cases/`` ``frames/`` ``contact_sheets/`` と summary.* を消す。
    """

    existing = sorted((out_dir / "cases").glob("*.json")) if (out_dir / "cases").is_dir() else []
    if existing and not force:
        raise SystemExit(
            f"出力先に既存の cases/*.json があります（{len(existing)}件）: {out_dir}\n"
            "古い結果が混ざるため停止しました。別の --out を指定するか、--force で"
            " cases/ frames/ contact_sheets/ と summary.* を消して再実行してください。"
        )
    if force:
        for name in OUT_SUBDIRS:
            target = out_dir / name
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
        for name in OUT_FILES:
            (out_dir / name).unlink(missing_ok=True)
    (out_dir / "cases").mkdir(parents=True, exist_ok=True)


def run_case(case: dict[str, str], source_root: Path, out_dir: Path) -> dict[str, Any]:
    """旧経路（アーカイブ）1ケース: 旧 rawpy 候補生成 → 各候補を実RAW現像 → 実RAW指標。"""

    feature = "旧 rawpy 候補生成の評価"
    rawpy = legacy.import_module("rawpy", feature)
    config = legacy.import_module("local_raw.config", feature)
    dc = legacy.import_module("local_raw.development_candidates", feature)
    engine = legacy.import_module("local_raw.engine", feature)
    read_capture_exif = legacy.import_module("local_raw.exif", feature).read_capture_exif
    probe_lens = legacy.import_module("local_raw.lens", feature).probe_lens

    key = case_key(case)
    capture_id, preset = case["capture_id"], case["preset"]
    arw_path = find_source_file(source_root, capture_id, ("ARW",))
    if arw_path is None:
        raise FileNotFoundError(f"ARWが見つかりません: {capture_id}（{source_root}）")
    jpg_path = find_source_file(source_root, capture_id, ("JPG", "JPEG"))
    exif = read_capture_exif(jpg_path) if jpg_path is not None else {}
    lens_resolution = probe_lens(exif)

    # 現像元フォルダはプロセス内でだけ差し替える（アプリ設定 config.json は変更しない）。
    with mock.patch.object(config, "source_root", return_value=source_root):
        started = time.monotonic()
        result = dc.generate_development_candidates(capture_id, preset)
        gen_elapsed = time.monotonic() - started

    frames_dir = out_dir / "frames"
    if jpg_path is not None:
        save_pair_frame(jpg_path, frames_dir / f"{key}__jpg.jpg")

    is_star = bool(result["star_scene"]["is_star_scene"])
    candidates = [dict(c) for c in result["candidates"]]
    reals: dict[str, np.ndarray] = {}
    with rawpy.imread(str(arw_path)) as raw:
        dark_scene = engine.detect_dark_scene(exif, raw=raw)
        for candidate in candidates:
            try:
                rgb, lens_correction, timing = develop_real_array(
                    raw,
                    candidate["adjustments"],
                    lens_resolution,
                    dark_scene["bright_k"],
                    exif,
                )
            except Exception as error:  # 1候補の失敗で他を巻き込まない
                traceback.print_exc()
                candidate["raw_error"] = f"{type(error).__name__}: {error}"
                continue
            save_frame(rgb, frames_dir / f"{key}__{candidate['id']}.jpg")
            reals[candidate["id"]] = engine._resize_array(rgb, COMPARE_EDGE)
            candidate["raw_timing"] = {**timing, "lens_applied": bool(lens_correction.get("applied"))}

    natural = reals.get("natural")
    if natural is not None:
        gray = np.sum(natural * np.array([0.2126, 0.7152, 0.0722], dtype=np.float32), axis=2)
        shadow_mask = gray <= np.quantile(gray, dc.SHADOW_QUANTILE)
        for candidate in candidates:
            image = reals.get(candidate["id"])
            if image is None:
                continue
            # 候補生成と同じゲート: natural は star_scene 判定、それ以外は stars のみ True。
            gate = is_star if candidate["id"] == "natural" else candidate["id"] == "stars"
            candidate["raw_metrics"] = dc._image_metrics(natural, image, exif, gate, shadow_mask)
            ungated = dc._star_measurements(image, exif, evaluate_gate=False)
            candidate["raw_star_ungated"] = {
                name: ungated.get(name)
                for name in ("star_count", "star_halo", "star_color_saturation", "is_star_scene")
            }

    return {
        "key": key,
        "root": case["root"],
        "capture_id": capture_id,
        "preset": preset,
        "gen_elapsed": gen_elapsed,
        "proxy": result["proxy"],
        "star_scene": result["star_scene"],
        "candidates": candidates,
        "rejected": result["rejected"],
        "seed": result["seed"],
        "budget": result["budget"],
        "dark_scene": dark_scene,
        "exif": exif,
    }


# ---------------------------------------------------------------------------
# コンタクトシート
# ---------------------------------------------------------------------------


def _load_font(size: int) -> Any:
    for candidate in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf"):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _panel(image: Image.Image, title: str, line1: str, line2: str) -> Image.Image:
    width = max(round(image.width * SHEET_FRAME_HEIGHT / image.height), 300)
    frame = image.resize((round(image.width * SHEET_FRAME_HEIGHT / image.height), SHEET_FRAME_HEIGHT), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, SHEET_FRAME_HEIGHT + SHEET_CAPTION_HEIGHT), (20, 20, 20))
    canvas.paste(frame, (0, 0))
    draw = ImageDraw.Draw(canvas)
    big, small = _load_font(15), _load_font(12)
    draw.text((6, SHEET_FRAME_HEIGHT + 3), title, fill="white", font=big)
    draw.text((6, SHEET_FRAME_HEIGHT + 24), line1, fill=(210, 210, 210), font=small)
    draw.text((6, SHEET_FRAME_HEIGHT + 42), line2, fill=(210, 210, 210), font=small)
    return canvas


def build_contact_sheet(record: dict[str, Any], out_dir: Path) -> Path | None:
    """1写真の一覧: ペアJPG｜natural 実RAW｜各候補の実RAW（キャプション付き）。"""

    frames_dir = out_dir / "frames"
    key = record["key"]
    panels: list[Image.Image] = []
    pair_path = frames_dir / f"{key}__jpg.jpg"
    if pair_path.is_file():
        with Image.open(pair_path) as pair:
            panels.append(_panel(pair.convert("RGB"), "camera JPG (pair)", record["capture_id"], record["preset"]))
    # natural を先頭、以降は STYLE_IDS の順。
    order = {style: index for index, style in enumerate(cm.STYLE_IDS)}
    for candidate in sorted(record["candidates"], key=lambda c: order.get(c["id"], 99)):
        frame_path = frames_dir / f"{key}__{candidate['id']}.jpg"
        metrics = candidate.get("raw_metrics")
        if not frame_path.is_file() or not metrics:
            continue
        adj = candidate["adjustments"]
        with Image.open(frame_path) as frame:
            panels.append(
                _panel(
                    frame.convert("RGB"),
                    f"{candidate['id']}{' (baseline)' if candidate['id'] == 'natural' else ''} [real RAW]",
                    f"exp={adj['exposure']:+.1f} sl={adj['shadow_lift']:.0f} hr={adj['highlight_recovery']:.0f} "
                    f"br={adj['brightness']:.2f} ct={adj['contrast']:.2f} nr={adj['noise_reduction']}",
                    f"RAW hi={metrics['highlight_clip']:.3f} sh={metrics['shadow_clip']:.3f} tr={metrics['tonal_range']:.2f}",
                )
            )
    if not panels:
        return None
    sheet = Image.new("RGB", (sum(p.width for p in panels), SHEET_FRAME_HEIGHT + SHEET_CAPTION_HEIGHT))
    x = 0
    for panel in panels:
        sheet.paste(panel, (x, 0))
        x += panel.width
    path = out_dir / "contact_sheets" / f"{key}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, "JPEG", quality=88)
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write_summary(out_dir: Path, records: list[dict[str, Any]], errors: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    summary = build_summary(records, errors)
    (out_dir / "summary.json").write_text(
        json.dumps({"meta": meta, "summary": summary}, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    (out_dir / "summary.md").write_text(render_markdown(summary, meta), encoding="utf-8")
    return summary


def _load_records(out_dir: Path) -> list[dict[str, Any]]:
    """``cases/*.json`` を読む。``key`` の無いレコード（外部ハーネス出力）はファイル名を key にする。"""

    records = []
    for path in sorted((out_dir / "cases").glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or not isinstance(record.get("candidates"), list):
            raise SystemExit(f"候補レコードの形式が不正です（candidates 配列が必要）: {path}")
        record.setdefault("key", path.stem)
        records.append(record)
    return records


def load_previous_run_info(out_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    """再集計用に、元の summary.json の meta と errors を引き継ぐ。

    summary.json が無い/壊れているときは meta は最小限、errors は None（不明）を返す。
    """

    try:
        payload = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
        meta = payload["meta"]
        errors = payload["summary"]["errors"]
    except (OSError, ValueError, KeyError, TypeError):
        return {"aggregated_from": str(out_dir)}, None
    if not isinstance(meta, dict):
        meta = {}
    if errors is not None and not isinstance(errors, list):
        errors = None
    return dict(meta), errors


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="現像候補自動生成を、実RAW現像との比較で評価します。")
    parser.add_argument("--source-root", type=Path, help="root キー default のフォルダ（--case 用の既定）")
    parser.add_argument("--root", action="append", default=[], metavar="KEY=DIR", help="root キーの実フォルダ（繰り返し可）")
    parser.add_argument("--case", action="append", default=[], metavar="ID[:PRESET]", help="評価ケース（繰り返し可）")
    parser.add_argument("--cases-file", type=Path, help="ケース定義 JSON（scripts/eval_cases/*.json）")
    parser.add_argument("--out", type=Path, help="出力先（既定: outputs/eval_candidates/<timestamp>）")
    parser.add_argument("--contact-sheets", action="store_true",
                        help="写真ごとの一覧画像を作る（<out>/frames/ の画像が必要。Rust ハーネスの出力には無い）")
    parser.add_argument("--force", action="store_true", help="--out に既存の cases/*.json があっても、cases/ frames/ contact_sheets/ と summary.* を消して実行する")
    parser.add_argument("--aggregate-only", action="store_true",
                        help="--out の cases/*.json（Rust の Core Image 候補ハーネス出力、または旧経路の出力）から summary（と一覧）を作る。"
                             "local_raw / rawpy は使わない")
    parser.add_argument("--legacy-rawpy", action="store_true",
                        help="旧 rawpy 候補生成 + 実RAW現像（アーカイブ）を実行する。local_raw/ が残る checkout と requirements-raw.txt の環境が前提")
    args = parser.parse_args(argv)

    if args.aggregate_only:
        if args.out is None:
            raise SystemExit("--aggregate-only には --out が必要です。")
        out_dir = args.out.expanduser().resolve()
        records = _load_records(out_dir)
        if not records:
            raise SystemExit(f"cases/*.json がありません: {out_dir}")
        if args.contact_sheets:
            built = [record["key"] for record in records if build_contact_sheet(record, out_dir) is not None]
            missing = [str(record["key"]) for record in records if record["key"] not in built]
            if missing:
                print(
                    f"警告: --contact-sheets の一覧を {len(missing)}/{len(records)} 件で作れませんでした"
                    f"（{', '.join(missing[:5])}{' ほか' if len(missing) > 5 else ''}）。一覧には "
                    f"{out_dir / 'frames'}/<key>__jpg.jpg と <key>__<style>.jpg（と候補の raw_metrics）が必要です。"
                    "Rust の Core Image 候補ハーネスは frames/ を書かないため、その出力では一覧は作れません"
                    "（集計 summary はそのまま作ります）。",
                    file=sys.stderr,
                )
        meta, errors = load_previous_run_info(out_dir)
        meta["aggregated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_summary(out_dir, records, errors, meta)
        print(f"summary を更新しました: {out_dir}")
        return
    if not args.legacy_rawpy:
        raise SystemExit(
            "候補の生成・現像はこのスクリプトでは行いません。Core Image 候補は Rust の候補ハーネスで cases/*.json を作り、"
            "--aggregate-only --out <dir> で集計してください。旧 rawpy 候補生成を回すなら --legacy-rawpy: "
            + legacy.PREREQUISITE
        )

    try:
        cases: list[dict[str, str]] = [parse_case_spec(spec) for spec in args.case]
        if args.cases_file:
            cases.extend(load_cases_file(args.cases_file))
        if not cases:
            raise ValueError("評価ケースがありません（--case か --cases-file を指定）。")
        roots = parse_root_args(args.root, args.source_root)
        check_roots(cases, roots)
    except (ValueError, OSError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error

    out_dir = (args.out or DEFAULT_OUT_PARENT / time.strftime("%Y%m%d-%H%M%S")).expanduser().resolve()
    prepare_out_dir(out_dir, args.force)
    meta = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "engine_version": legacy.import_module("local_raw.engine", "旧 rawpy 候補生成の評価").ENGINE_VERSION,
        "roots": {key: str(path) for key, path in sorted(roots.items()) if key in {c["root"] for c in cases}},
        "cases_file": str(args.cases_file) if args.cases_file else None,
    }

    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        key = case_key(case)
        print(f"[{index}/{len(cases)}] {key} ({case['root']})", flush=True)
        try:
            record = run_case(case, roots[case["root"]], out_dir)
        except Exception as error:  # 1ケースの失敗で全体を止めない
            traceback.print_exc()
            errors.append({"case": key, "error": f"{type(error).__name__}: {error}"})
            continue
        (out_dir / "cases" / f"{key}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8"
        )
        records.append(record)
        print(
            f"    gen={record['gen_elapsed']:.1f}s decodes={record['proxy']['decodes']} "
            f"cands={[c['id'] for c in record['candidates']]} rejected={[r['id'] for r in record['rejected']]} "
            f"star={record['star_scene']['is_star_scene']}",
            flush=True,
        )
        if args.contact_sheets:
            build_contact_sheet(record, out_dir)

    summary = _write_summary(out_dir, records, errors, meta)
    print(
        json.dumps(
            {
                "out_dir": str(out_dir),
                "cases": len(cases),
                "errors": len(errors),
                "broken_candidates": len(summary["broken_candidates"]),
                "summary_md": str(out_dir / "summary.md"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except legacy.LegacyUnavailable as error:
        raise SystemExit(str(error)) from None
