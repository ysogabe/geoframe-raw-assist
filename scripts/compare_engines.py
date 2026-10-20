#!/usr/bin/env python3
"""rawpy（現行エンジン）と Core Image CLI を同じ ARW 群で比較するハーネス（Issue #65 PR3）。

各カットについて次の 4 つを並べ、指標・比較シートを ``work/engine-comparison/<timestamp>/``
（gitignore 対象）へ出力する。

- camera   : 同名のカメラ JPEG（向きを正立に直してリサイズのみ）
- rawpy    : ``local_raw.engine.develop_raw``（既定プリセット・既定 Adjustments）
- coreimage: ``geoframe-ci-develop``（profile neutral, decoder_version "9", lens_correction false）
- coreimage_lens: 同上 + lens_correction true（レンズ補正差の把握用）

これは「どちらが良いか」の自動判定ではない。判断材料（処理時間・メモリ・明るさ分布・
クリップ・カメラ JPEG との距離・簡易シャープネス/ノイズ）を同じ入力で揃えるための道具である。

分離方針:
- rawpy は別プロセス（このスクリプトの ``--worker-rawpy``）で ``/usr/bin/time -l`` 越しに実行し、
  プロセス単位の最大 RSS を取る。ワーカー内では ``config.source_root`` と
  ``engine.OUTPUT_ROOT`` / ``engine.PREVIEW_ROOT`` を ``--out`` 配下へ差し替えるので、
  アプリ設定・既存の outputs/ キャッシュ・exports には書かない（``force=True`` で毎回現像）。
- Core Image CLI も ``/usr/bin/time -l`` 越しに 1 リクエスト 1 プロセスで呼ぶ。
- 原本 ARW/JPG は読むだけ。比較シート・クロップは EXIF を付けずに保存する（GPS を持ち出さない）。

例:
    .venv/bin/python scripts/compare_engines.py \\
      --source-root /Volumes/Backup/100MSDCF --select auto --full --full-limit 5

``--sharpness`` は解像感（ピント甘さ）評価モード。上の 4 枚の代わりに ``SHARPNESS_VARIANTS`` の
10 バリアント（rawpy 撮影時シャープ ON/OFF、Core Image decoder 8/9 × lens、apple_default、
CI + rawpy と同じ後段仕上げ）を作り、ROI のシャープネス比・エッジ 10–90% 幅・平坦部ノイズと
等倍クロップシートを ``sharpness_*.{csv,json,md}`` / ``sheets/`` に出す
（docs/coreimage-engine-comparison.md §9）。

``--night`` は夜景・星空・長秒の評価モード（Issue #70 / #77、設計書 §4.1 / §4.3）。``NIGHT_VARIANTS``
（camera / rawpy / CI apple_default v9 lens ON とその NR 0・シャープ 0・lens OFF・decoder 8 派生 / CI neutral v8・v9）
について、星の数（``development_candidates`` と同じ検出）・ハロー・星の色、空領域の明るさ・ノイズ・色ノイズ、
白点（インパルス）ノイズ、四隅と中央のノイズ比を測り、空と星の等倍（2 倍拡大）シートを出す。
``--night-scales`` を付けると白点の scaleFactor 依存（#77）も掃引する。
``--bracket`` は同一構図で露出の違うペア（EXIF から自動検出、または ``--bracket-pairs A,B``）について、
リニア輝度比が 2^ΔEV に沿うか・レンズ補正の幾何がフレーム間で一致するか・Mertens 合成の見え方を記録する。
``--highlight`` は逆光・ハイライトの評価モード（Issue #71、設計書 §4.2）。チャンネル別クリップ率・クリップ境界の
マゼンタ寄り画素率と、EDR / boost / boostShadow の掃引（#79 の個別上書き）を出す。
ARW / JPG の拡張子は大文字小文字を問わない（``local_raw.source_files`` で解決）。
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

CAPTURE_ID_PATTERN = re.compile(r"^DSC\d{5}$")
DEFAULT_CLI = PROJECT_ROOT / "native" / "coreimage-cli" / ".build" / "release" / "geoframe-ci-develop"
DEFAULT_OUT_ROOT = PROJECT_ROOT / "work" / "engine-comparison"
REC709_LUMA = (0.2126, 0.7152, 0.0722)

ENGINES = ("camera", "rawpy", "coreimage", "coreimage_lens")
ENGINE_LABELS = {
    "camera": "camera JPEG",
    "rawpy": "rawpy (current engine)",
    "coreimage": "Core Image neutral",
    "coreimage_lens": "Core Image neutral + lens",
}
CI_DECODER_VERSION = "9"

# --- シーン分類のしきい値 -------------------------------------------------------
#: 星空候補: 長秒露光。三脚・星景の典型（数秒〜数十秒）を下限 2 秒で拾う。
STAR_MIN_EXPOSURE_S = 2.0
#: 星空候補のもう一つの条件: EV100 が 0 以下（月明かり以下の暗さ）。
STAR_MAX_EV100 = 0.0
#: 暗所: engine.DARK_SCENE_MAX_EV100 と同じ 6.0（現行エンジンが暗シーン扱いにする境界）。
DARK_MAX_EV100 = 6.0
#: 夜景: 暗所かつ撮影時刻が夜間帯（ローカル時刻）。
NIGHT_HOURS = frozenset([*range(18, 24), *range(0, 6)])
HIGH_ISO_MIN = 3200
MID_ISO_MIN = 800
#: 逆光/ハイライト: カメラ JPEG 縮小版で、輝度 >0.95 が一定以上かつ暗部 <0.10 も一定以上。
BACKLIT_MIN_BRIGHT = 0.03
BACKLIT_MIN_DARK = 0.10
LOW_ISO_MAX = 400

#: --select auto のカテゴリ順と枠（合計で 20〜30 になるよう設定）。
DEFAULT_QUOTAS: tuple[tuple[str, int], ...] = (
    ("star", 3),
    ("night", 4),
    ("dark", 3),
    ("high_iso", 3),
    ("mid_iso", 3),
    ("backlit", 3),
    ("portrait", 3),
    ("lens_day", 6),  # 低 ISO の昼景をレンズごとに均等（6 / レンズ数）
)


# =============================================================================
# 純粋関数（tests/test_compare_engines.py で検証）
# =============================================================================


def validate_capture_id(capture_id: str) -> str:
    """撮影 ID（DSC + 5 桁）だけを通す。パス区切り等を含む入力はここで拒否する。"""
    if not isinstance(capture_id, str) or not CAPTURE_ID_PATTERN.fullmatch(capture_id):
        raise ValueError(f"撮影IDの形式が不正です: {capture_id!r}")
    return capture_id


def ev100(f_number: Any, exposure_time: Any, iso: Any) -> float | None:
    """EV100 = log2(N²/t) − log2(ISO/100)。不正値は None。"""
    try:
        n, t, s = float(f_number), float(exposure_time), float(iso)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) and v > 0 for v in (n, t, s)):
        return None
    return math.log2(n * n / t) - math.log2(s / 100.0)


def capture_hour(captured_at: Any) -> int | None:
    """EXIF DateTimeOriginal（'YYYY:MM:DD HH:MM:SS'）から時（0..23）を取る。"""
    if not isinstance(captured_at, str):
        return None
    match = re.match(r"^\d{4}:\d{2}:\d{2} (\d{2}):\d{2}:\d{2}", captured_at.strip())
    if not match:
        return None
    hour = int(match.group(1))
    return hour if 0 <= hour <= 23 else None


def classify_scene(features: dict[str, Any]) -> list[str]:
    """特徴量 dict から該当するシーン種別（複数可）を返す。

    features のキー: iso, exposure_time, ev100, hour, orientation, bright_frac, dark_frac。
    欠損は「該当しない」として扱う。
    """
    cats: list[str] = []
    iso = features.get("iso")
    exposure = features.get("exposure_time")
    ev = features.get("ev100")
    hour = features.get("hour")

    is_star = (
        isinstance(exposure, (int, float)) and exposure >= STAR_MIN_EXPOSURE_S
    ) or (isinstance(ev, (int, float)) and ev <= STAR_MAX_EV100)
    is_dark = isinstance(ev, (int, float)) and ev <= DARK_MAX_EV100
    if is_star:
        cats.append("star")
    if is_dark and hour is not None and hour in NIGHT_HOURS:
        cats.append("night")
    elif is_dark:
        cats.append("dark")
    if isinstance(iso, (int, float)):
        if iso >= HIGH_ISO_MIN:
            cats.append("high_iso")
        elif iso >= MID_ISO_MIN:
            cats.append("mid_iso")
    bright = features.get("bright_frac")
    darkf = features.get("dark_frac")
    if (
        isinstance(bright, (int, float))
        and isinstance(darkf, (int, float))
        and bright >= BACKLIT_MIN_BRIGHT
        and darkf >= BACKLIT_MIN_DARK
    ):
        cats.append("backlit")
    if features.get("orientation") in (5, 6, 7, 8):
        cats.append("portrait")
    if not is_dark and isinstance(iso, (int, float)) and iso <= LOW_ISO_MAX:
        cats.append("day")
    return cats


def _burst_key(features: dict[str, Any]) -> str:
    """連写・同一シーンの重複を避けるためのキー（撮影時刻の分単位）。無ければ ID。"""
    captured = features.get("captured_at")
    if isinstance(captured, str) and len(captured) >= 16:
        return captured[:16]
    return str(features.get("capture_id"))


def _session_key(features: dict[str, Any]) -> str:
    """同じ撮影セッション（日付 + 時）のキー。1 セッションへの偏りを抑えるのに使う。"""
    captured = features.get("captured_at")
    if isinstance(captured, str) and len(captured) >= 13:
        return captured[:13]
    return str(features.get("capture_id"))


def _category_sort_key(category: str, features: dict[str, Any]) -> tuple:
    """カテゴリ内の優先順。決定的になるよう最後は capture_id で並べる。"""
    cid = str(features.get("capture_id"))
    ev = features.get("ev100")
    ev = ev if isinstance(ev, (int, float)) else 99.0
    iso = features.get("iso") or 0
    if category in ("star", "night", "dark"):
        return (ev, cid)  # 暗い順
    if category in ("high_iso", "mid_iso"):
        return (-iso, ev, cid)  # 高 ISO 順
    if category == "backlit":
        score = (features.get("bright_frac") or 0.0) * (features.get("dark_frac") or 0.0)
        return (-score, cid)
    return (cid,)


def select_auto(
    features_list: Iterable[dict[str, Any]],
    quotas: Iterable[tuple[str, int]] = DEFAULT_QUOTAS,
    max_per_session: int = 2,
) -> list[dict[str, Any]]:
    """特徴量の一覧からカテゴリ枠に沿ってカットを選ぶ（決定的）。

    - 1 カットは 1 回だけ選ぶ（先に埋まったカテゴリが選定理由になる）。
    - 同じ分（_burst_key）に撮ったカットは 1 枚まで（連写の重複を避ける）。
    - 同じセッション（日付 + 時、_session_key）からは ``max_per_session`` 枚まで。
    - lens_day は「day」カテゴリをレンズごとに均等配分する。
    戻り値: [{capture_id, category, categories, reason}] を選定順で。
    """
    items = sorted(
        (dict(f, categories=classify_scene(f)) for f in features_list),
        key=lambda f: str(f.get("capture_id")),
    )
    chosen: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    used_bursts: set[str] = set()
    session_counts: dict[str, int] = {}

    def take(item: dict[str, Any], category: str) -> None:
        used_ids.add(str(item["capture_id"]))
        used_bursts.add(_burst_key(item))
        session = _session_key(item)
        session_counts[session] = session_counts.get(session, 0) + 1
        chosen.append({
            "capture_id": item["capture_id"],
            "category": category,
            "categories": item["categories"],
            "reason": selection_reason(category, item),
        })

    def available(item: dict[str, Any], session_cap: bool = True) -> bool:
        return (
            str(item["capture_id"]) not in used_ids
            and _burst_key(item) not in used_bursts
            and (not session_cap or session_counts.get(_session_key(item), 0) < max_per_session)
        )

    for category, quota in quotas:
        if quota <= 0:
            continue
        if category == "lens_day":
            lenses = sorted({str(i.get("lens_model")) for i in items if "day" in i["categories"]})
            if not lenses:
                continue
            per_lens = max(1, quota // len(lenses))
            for lens in lenses:
                pool = [
                    i for i in items
                    if "day" in i["categories"] and str(i.get("lens_model")) == lens
                ]
                # 撮影日をばらすため、まず per_lens 等分点、埋まらなければ細かい等分点で拾う。
                picked = 0
                if pool:
                    last = len(pool) - 1
                    coarse = np.rint(np.linspace(0, last, num=min(len(pool), per_lens))).astype(int)
                    fine = np.rint(np.linspace(0, last, num=len(pool))).astype(int)
                    order = list(dict.fromkeys(int(p) for p in [*coarse, *fine]))
                    # レンズの網羅を優先し、セッション上限で埋まらなければ上限を外して拾う。
                    for session_cap in (True, False):
                        for pos in order:
                            if picked >= per_lens:
                                break
                            if available(pool[pos], session_cap):
                                take(pool[pos], "lens_day")
                                picked += 1
            continue
        pool = sorted(
            (i for i in items if category in i["categories"]),
            key=lambda i: _category_sort_key(category, i),
        )
        picked = 0
        for item in pool:
            if picked >= quota:
                break
            if available(item):
                take(item, category)
                picked += 1
    return chosen


def selection_reason(category: str, features: dict[str, Any]) -> str:
    """選定理由の短い説明（撮影場所・GPS は含めない）。"""
    ev = features.get("ev100")
    ev_s = "n/a" if not isinstance(ev, (int, float)) else f"{ev:.1f}"
    exp = features.get("exposure_time")
    exp_s = "n/a" if not isinstance(exp, (int, float)) else (f"1/{round(1 / exp)}s" if exp < 1 else f"{exp:g}s")
    base = f"ISO {features.get('iso')}, {exp_s}, EV100 {ev_s}"
    labels = {
        "star": "星空候補（長秒または EV100<=0）",
        "night": "夜景（EV100<=6 かつ夜間帯の撮影）",
        "dark": "暗所（EV100<=6）",
        "high_iso": f"高 ISO（>= {HIGH_ISO_MIN}）",
        "mid_iso": f"中 ISO（{MID_ISO_MIN}〜{HIGH_ISO_MIN - 1}）",
        "backlit": (
            "逆光/ハイライト（JPEG 縮小版で白 "
            f"{(features.get('bright_frac') or 0) * 100:.1f}% / 暗部 {(features.get('dark_frac') or 0) * 100:.1f}%）"
        ),
        "portrait": "縦位置",
        "lens_day": "レンズ別の低 ISO 昼景",
    }
    return f"{labels.get(category, category)}: {base}"


def full_subset(selection: list[dict[str, Any]], limit: int) -> list[str]:
    """フル解像度計測に回すカットを、カテゴリが重ならないよう選定順から拾う。"""
    if limit <= 0:
        return []
    picked: list[str] = []
    seen_categories: set[str] = set()
    for row in selection:
        if row["category"] not in seen_categories:
            picked.append(row["capture_id"])
            seen_categories.add(row["category"])
        if len(picked) >= limit:
            return picked
    for row in selection:
        if row["capture_id"] not in picked:
            picked.append(row["capture_id"])
        if len(picked) >= limit:
            break
    return picked


def parse_time_l(stderr: str) -> dict[str, float]:
    """macOS ``/usr/bin/time -l`` の stderr から wall 秒と最大 RSS（byte）等を取り出す。"""
    out: dict[str, float] = {}
    match = re.search(r"^\s*([\d.]+)\s+real\s+([\d.]+)\s+user\s+([\d.]+)\s+sys", stderr, re.M)
    if match:
        out["time_real_s"] = float(match.group(1))
        out["time_user_s"] = float(match.group(2))
        out["time_sys_s"] = float(match.group(3))
    match = re.search(r"^\s*(\d+)\s+maximum resident set size", stderr, re.M)
    if match:
        out["maxrss_bytes"] = float(match.group(1))
    match = re.search(r"^\s*(\d+)\s+peak memory footprint", stderr, re.M)
    if match:
        out["peak_footprint_bytes"] = float(match.group(1))
    return out


def luma_of(rgb01: np.ndarray) -> np.ndarray:
    kr, kg, kb = REC709_LUMA
    return kr * rgb01[..., 0] + kg * rgb01[..., 1] + kb * rgb01[..., 2]


def delta_e76(rgb_a: np.ndarray, rgb_b: np.ndarray) -> float:
    """2 枚の 8bit sRGB 画像（同寸）の平均 ΔE76（OpenCV の Lab, D65）。"""
    if rgb_a.shape != rgb_b.shape:
        raise ValueError("delta_e76 は同寸の画像が必要です")
    lab_a = cv2.cvtColor(rgb_a.astype(np.float32) / 255.0, cv2.COLOR_RGB2Lab)
    lab_b = cv2.cvtColor(rgb_b.astype(np.float32) / 255.0, cv2.COLOR_RGB2Lab)
    return float(np.sqrt(((lab_a - lab_b) ** 2).sum(axis=2)).mean())


def flat_noise(gray: np.ndarray, block: int = 32, fraction: float = 0.1) -> float:
    """平坦部ノイズの簡易推定（8bit スケールの標準偏差）。

    輝度を ``block`` px のタイルに切り、ぼかし後の勾配（=構造）が小さいタイルの下位
    ``fraction`` を平坦部とみなす。そこでのハイパス（原画 − ぼかし）の標準偏差の中央値を返す。
    白飛び・黒つぶれのタイル（平均 <5 or >250）は除く。平坦タイルが無ければ NaN。
    """
    g = gray.astype(np.float32)
    blurred = cv2.GaussianBlur(g, (0, 0), 1.5)
    highpass = g - blurred
    gx = cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3)
    grad = np.sqrt(gx * gx + gy * gy)
    h, w = g.shape
    rows, cols = h // block, w // block
    if rows == 0 or cols == 0:
        return float("nan")
    crop = (slice(0, rows * block), slice(0, cols * block))

    def tiles(a: np.ndarray) -> np.ndarray:
        return a[crop].reshape(rows, block, cols, block).swapaxes(1, 2).reshape(rows * cols, block * block)

    t_mean = tiles(g).mean(axis=1)
    t_grad = tiles(grad).mean(axis=1)
    t_hp = tiles(highpass).std(axis=1)
    valid = (t_mean > 5.0) & (t_mean < 250.0)
    if not np.any(valid):
        return float("nan")
    grads = t_grad[valid]
    hps = t_hp[valid]
    count = max(1, int(round(len(grads) * fraction)))
    order = np.argsort(grads, kind="stable")[:count]
    return float(np.median(hps[order]))


def image_metrics(rgb: np.ndarray, reference: np.ndarray | None = None) -> dict[str, float]:
    """8bit RGB 画像 1 枚の指標。reference（同寸のカメラ JPEG）があれば差分も出す。"""
    rgb01 = rgb.astype(np.float32) / 255.0
    luma = luma_of(rgb01)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    out = {
        "mean_luma": float(luma.mean()),
        "p01_luma": float(np.percentile(luma, 1)),
        "p50_luma": float(np.percentile(luma, 50)),
        "p99_luma": float(np.percentile(luma, 99)),
        # 白飛び: いずれかのチャンネルが 255 / 全チャンネル 255。黒つぶれ: 輝度 < 2/255。
        "clip_high_any": float(np.any(rgb >= 255, axis=2).mean()),
        "clip_high_all": float(np.all(rgb >= 255, axis=2).mean()),
        "clip_low": float((luma < 2.0 / 255.0).mean()),
        "laplacian_variance": float(cv2.Laplacian(gray.astype(np.float32), cv2.CV_32F).var()),
        "flat_noise": flat_noise(gray),
        "mean_saturation": float((rgb01.max(axis=2) - rgb01.min(axis=2)).mean()),
    }
    if reference is not None:
        if reference.shape != rgb.shape:
            raise ValueError("reference は同寸である必要があります")
        ref01 = reference.astype(np.float32) / 255.0
        out["camera_delta_e76"] = delta_e76(rgb, reference)
        out["camera_rgb_mae"] = float(np.abs(rgb01 - ref01).mean())
        out["camera_luma_diff"] = float(luma.mean() - luma_of(ref01).mean())
    return out


def fit_size(width: int, height: int, max_edge: int) -> tuple[int, int]:
    """長辺を max_edge 以下に収める（拡大はしない）。"""
    scale = min(1.0, max_edge / max(width, height))
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def aspect_mismatch(a: tuple[int, int], b: tuple[int, int]) -> float:
    """2 つの (w, h) のアスペクト比の相対差。"""
    ra, rb = a[0] / a[1], b[0] / b[1]
    return abs(ra - rb) / rb


def summarize(rows: list[dict[str, Any]], keys: Iterable[str]) -> list[dict[str, Any]]:
    """(engine, mode) ごとに数値列の中央値・最小・最大・平均を出す。エラー行は除く。"""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("error"):
            continue
        groups.setdefault((str(row["engine"]), str(row["mode"])), []).append(row)
    result = []
    for (engine_name, mode), group in sorted(groups.items()):
        item: dict[str, Any] = {"engine": engine_name, "mode": mode, "n": len(group)}
        for key in keys:
            values = [
                float(r[key]) for r in group
                if isinstance(r.get(key), (int, float)) and math.isfinite(float(r[key]))
            ]
            if values:
                item[key] = {
                    "median": statistics.median(values),
                    "min": min(values),
                    "max": max(values),
                    "mean": statistics.fmean(values),
                    "n": len(values),
                }
        result.append(item)
    return result


# =============================================================================
# 解像感（--sharpness）の純粋関数
# =============================================================================

#: エッジ立ち上がり幅の計測パラメータ。プロファイルは法線方向 ±EDGE_HALF_PX を
#: EDGE_STEP_PX 刻みで双一次補間し、接線方向に EDGE_ALONG 本平均する（ノイズ抑制）。
EDGE_HALF_PX = 8.0
EDGE_STEP_PX = 0.25
EDGE_ALONG = 5
#: プラトー（エッジ両側の平坦部）として使う外側の長さ（px）。min/max ではなく外側の
#: 中央値を使うのは、カメラ JPEG のシャープによるオーバーシュートで幅が縮んで見えるのを避けるため。
EDGE_PLATEAU_PX = 2.0
#: エッジとして採用する最小コントラスト（8bit 階調）。
EDGE_MIN_CONTRAST = 20.0
#: カメラ JPEG のプロファイルで、プラトーの標準偏差がコントラストのこの割合を超える点は捨てる。
EDGE_MAX_PLATEAU_STD = 0.12
#: パワースペクトルの「高周波」帯（cycles/px。0.5 がナイキスト）。
HF_BAND = (0.25, 0.5)
#: ISO 帯（低 / 中 / 高）。FBDD Light の境界 800 と撮影時シャープのコアリング上限 1600 に合わせる。
ISO_BAND_MID_MIN = 800
ISO_BAND_HIGH_MIN = 1601


def laplacian_var(gray: np.ndarray) -> float:
    """輝度（8bit スケール）の Laplacian 分散。"""
    return float(cv2.Laplacian(gray.astype(np.float32), cv2.CV_32F).var())


def tenengrad(gray: np.ndarray) -> float:
    """Tenengrad: Sobel（ksize 3）勾配二乗 gx²+gy² の平均（8bit スケール）。"""
    g = gray.astype(np.float32)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    return float((gx * gx + gy * gy).mean())


def highfreq_energy_ratio(gray: np.ndarray, band: tuple[float, float] = HF_BAND) -> float:
    """Hann 窓付き 2D パワースペクトルで、半径 band（cycles/px）のエネルギー / 全エネルギー（DC 除く）。"""
    g = gray.astype(np.float64)
    g = g - g.mean()
    h, w = g.shape
    window = np.outer(np.hanning(h), np.hanning(w))
    power = np.abs(np.fft.fft2(g * window)) ** 2
    fy = np.fft.fftfreq(h)[:, None]
    fx = np.fft.fftfreq(w)[None, :]
    radius = np.sqrt(fx * fx + fy * fy)
    total = float(power[radius > 0].sum())
    if total <= 0:
        return float("nan")
    in_band = (radius >= band[0]) & (radius <= band[1])
    return float(power[in_band].sum() / total)


def iso_band(iso: Any) -> str:
    """ISO を low（<800）/ mid（800〜1600）/ high（>1600）/ unknown に分ける。"""
    if not isinstance(iso, (int, float)) or not math.isfinite(float(iso)):
        return "unknown"
    if iso >= ISO_BAND_HIGH_MIN:
        return "high"
    if iso >= ISO_BAND_MID_MIN:
        return "mid"
    return "low"


def edge_rise_width(
    profile: np.ndarray,
    step: float = EDGE_STEP_PX,
    plateau_px: float = EDGE_PLATEAU_PX,
    min_contrast: float = EDGE_MIN_CONTRAST,
) -> float | None:
    """1D エッジプロファイル（暗→明の向き）から 10–90% 立ち上がり幅（px）を返す。

    プラトーは両端 ``plateau_px`` の中央値。正規化後、中央に最も近い 50% 交差から
    左へ 10% を、右へ 90% を下回る/上回る最初の点を線形補間で探す。コントラスト不足・
    向きが逆・交差が見つからない場合は None。
    """
    p = np.asarray(profile, dtype=np.float64)
    m = max(2, int(round(plateau_px / step)))
    if p.size < 2 * m + 3:
        return None
    lo = float(np.median(p[:m]))
    hi = float(np.median(p[-m:]))
    if hi - lo < min_contrast:
        return None
    q = (p - lo) / (hi - lo)
    above = q >= 0.5
    crossings = np.nonzero(above[1:] & ~above[:-1])[0]  # q[i] < .5 <= q[i+1]
    if crossings.size == 0:
        return None
    mid = (p.size - 1) / 2.0
    i50 = int(crossings[np.argmin(np.abs(crossings - mid))])

    x10 = None
    for i in range(i50, 0, -1):
        if q[i - 1] <= 0.1 < q[i]:
            x10 = (i - 1) + (0.1 - q[i - 1]) / (q[i] - q[i - 1])
            break
    x90 = None
    for i in range(i50 + 1, p.size):
        if q[i - 1] < 0.9 <= q[i]:
            x90 = (i - 1) + (0.9 - q[i - 1]) / (q[i] - q[i - 1])
            break
    if x10 is None or x90 is None:
        return None
    return float((x90 - x10) * step)


def sample_profiles(
    gray: np.ndarray,
    points: np.ndarray,
    half: float = EDGE_HALF_PX,
    step: float = EDGE_STEP_PX,
    along: int = EDGE_ALONG,
) -> np.ndarray:
    """points（N×4: x, y, nx, ny。n は暗→明の単位法線）の法線方向プロファイル（N×L）。

    双一次補間で、接線方向に ``along`` 本（1px 間隔）を平均する。画像外は境界値で埋める。
    """
    pts = np.asarray(points, dtype=np.float32).reshape(-1, 4)
    t = np.arange(-half, half + step / 2, step, dtype=np.float32)
    offsets = np.arange(along, dtype=np.float32) - (along - 1) / 2.0
    x, y, nx, ny = (pts[:, i][:, None, None] for i in range(4))
    # 接線 = 法線を 90° 回したもの (-ny, nx)
    map_x = x + nx * t[None, None, :] - ny * offsets[None, :, None]
    map_y = y + ny * t[None, None, :] + nx * offsets[None, :, None]
    n, a, length = pts.shape[0], along, t.size
    sampled = cv2.remap(
        gray.astype(np.float32),
        map_x.reshape(n, a * length).astype(np.float32),
        map_y.reshape(n, a * length).astype(np.float32),
        interpolation=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return sampled.reshape(n, a, length).mean(axis=1)


def find_edge_points(
    gray: np.ndarray,
    box: tuple[int, int, int, int],
    max_points: int = 24,
    cell: int = 16,
    min_coherence: float = 0.8,
) -> np.ndarray:
    """box 内から ESF 計測に向く強い直線状エッジ点を選ぶ（N×4: x, y, nx, ny）。

    σ1 ぼかし後の Sobel 勾配で、構造テンソルのコヒーレンス（直線性）が高い点を
    ``cell`` px ごとに 1 点（勾配最大）拾い、プロファイルが計測可能（両側が平坦・
    コントラスト十分）なものだけ勾配の強い順に ``max_points`` 点返す。
    """
    x0, y0, x1, y1 = box
    margin = int(math.ceil(EDGE_HALF_PX)) + EDGE_ALONG
    g = cv2.GaussianBlur(gray.astype(np.float32), (0, 0), 1.0)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    jxx = cv2.GaussianBlur(gx * gx, (0, 0), 2.0)
    jyy = cv2.GaussianBlur(gy * gy, (0, 0), 2.0)
    jxy = cv2.GaussianBlur(gx * gy, (0, 0), 2.0)
    root = np.sqrt((jxx - jyy) ** 2 + 4 * jxy * jxy)
    coherence = np.where(jxx + jyy > 1e-6, root / np.maximum(jxx + jyy, 1e-6), 0.0) ** 2
    mag = np.sqrt(gx * gx + gy * gy)
    h, w = gray.shape[:2]
    cands: list[tuple[float, int, int]] = []
    for cy in range(max(y0, margin), min(y1, h - margin), cell):
        for cx in range(max(x0, margin), min(x1, w - margin), cell):
            ys = slice(cy, min(cy + cell, y1, h - margin))
            xs = slice(cx, min(cx + cell, x1, w - margin))
            m = mag[ys, xs] * (coherence[ys, xs] >= min_coherence)
            if m.size == 0 or float(m.max()) <= 0:
                continue
            iy, ix = np.unravel_index(int(np.argmax(m)), m.shape)
            cands.append((float(m[iy, ix]), cx + int(ix), cy + int(iy)))
    cands.sort(reverse=True)
    if not cands:
        return np.zeros((0, 4), dtype=np.float32)
    pts = []
    for _, px, py in cands:
        norm = float(mag[py, px])
        pts.append((px, py, gx[py, px] / norm, gy[py, px] / norm))
    pts_arr = np.asarray(pts, dtype=np.float32)
    profiles = sample_profiles(gray, pts_arr)
    m = max(2, int(round(EDGE_PLATEAU_PX / EDGE_STEP_PX)))
    keep = []
    for index, prof in enumerate(profiles):
        contrast = float(np.median(prof[-m:]) - np.median(prof[:m]))
        if contrast < EDGE_MIN_CONTRAST:
            continue
        if max(prof[:m].std(), prof[-m:].std()) > EDGE_MAX_PLATEAU_STD * contrast:
            continue
        # 点そのものがエッジ上にあること（勾配の裾の点を拾わない）: 中央の値が 30〜70% 内
        center = (float(prof[prof.size // 2]) - float(np.median(prof[:m]))) / contrast
        if not 0.3 <= center <= 0.7:
            continue
        if edge_rise_width(prof) is None:
            continue
        keep.append(index)
        if len(keep) >= max_points:
            break
    return pts_arr[keep]


def roi_boxes(gray: np.ndarray, size: int, k: int = 3) -> list[tuple[str, tuple[int, int, int, int]]]:
    """ディテール ROI: σ1 ぼかし後 Laplacian 平均の上位 k ブロック（重ならない）+ 中央 + 左上隅。

    ぼかしてから Laplacian を取るのは、高 ISO で画素ノイズのタイルを「ディテール」と誤認しないため。
    隅は画像端から size/8 だけ内側（JPEG ブロック・ビネット境界を避ける）。
    """
    h, w = gray.shape[:2]
    s = min(size, w, h)
    lap = np.abs(cv2.Laplacian(cv2.GaussianBlur(gray.astype(np.float32), (0, 0), 1.0), cv2.CV_32F))
    integral = cv2.integral(lap)
    step = max(1, s // 4)
    scored = []
    for y in range(0, h - s + 1, step):
        for x in range(0, w - s + 1, step):
            total = integral[y + s, x + s] - integral[y, x + s] - integral[y + s, x] + integral[y, x]
            scored.append((float(total), x, y))
    scored.sort(reverse=True)
    boxes: list[tuple[str, tuple[int, int, int, int]]] = []
    for _, x, y in scored:
        if len(boxes) >= k:
            break
        if all(x + s <= bx0 or bx1 <= x or y + s <= by0 or by1 <= y for _, (bx0, by0, bx1, by1) in boxes):
            boxes.append((f"detail{len(boxes) + 1}", (x, y, x + s, y + s)))
    cx, cy = (w - s) // 2, (h - s) // 2
    boxes.append(("center", (cx, cy, cx + s, cy + s)))
    inset = s // 8
    boxes.append(("corner", (inset, inset, inset + s, inset + s)))
    return boxes


def roi_shift(
    reference: np.ndarray,
    target: np.ndarray,
    box: tuple[int, int, int, int],
    margin: int = 32,
) -> tuple[float, float, float]:
    """box（reference 座標）周辺の位相相関で、target 側の平行移動 (dx, dy, response) を推定する。

    target の内容が reference の (x+dx, y+dy) にある、という向き。
    """
    h, w = reference.shape[:2]
    x0, y0, x1, y1 = box
    x0, y0 = max(0, x0 - margin), max(0, y0 - margin)
    x1, y1 = min(w, x1 + margin), min(h, y1 + margin)
    a = reference[y0:y1, x0:x1].astype(np.float32)
    b = target[y0:y1, x0:x1].astype(np.float32)
    a = (a - a.mean()) / (a.std() + 1e-6)
    b = (b - b.mean()) / (b.std() + 1e-6)
    window = cv2.createHanningWindow((a.shape[1], a.shape[0]), cv2.CV_32F)
    (dx, dy), response = cv2.phaseCorrelate(a, b, window)
    return float(dx), float(dy), float(response)


def shift_box(box: tuple[int, int, int, int], dx: float, dy: float, width: int, height: int) -> tuple[int, int, int, int]:
    """box を (dx, dy) だけ動かし、画像内に収める（寸法は保つ）。"""
    x0, y0, x1, y1 = box
    s_w, s_h = x1 - x0, y1 - y0
    nx0 = int(round(min(max(0, x0 + dx), width - s_w)))
    ny0 = int(round(min(max(0, y0 + dy), height - s_h)))
    return (nx0, ny0, nx0 + s_w, ny0 + s_h)


def median_or_nan(values: Iterable[Any]) -> float:
    vals = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(float(v))]
    return statistics.median(vals) if vals else float("nan")


def summarize_sharpness(
    rows: list[dict[str, Any]],
    keys: Iterable[str],
    group_keys: tuple[str, ...] = ("variant", "mode"),
) -> list[dict[str, Any]]:
    """group_keys ごとに数値列の中央値（と件数）を出す。エラー行は除く。"""
    keys = list(keys)
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("error"):
            continue
        groups.setdefault(tuple(str(row.get(k)) for k in group_keys), []).append(row)
    out = []
    for gkey, group in sorted(groups.items()):
        item: dict[str, Any] = dict(zip(group_keys, gkey))
        item["n"] = len(group)
        for key in keys:
            vals = [float(r[key]) for r in group if isinstance(r.get(key), (int, float)) and math.isfinite(float(r[key]))]
            item[key] = statistics.median(vals) if vals else None
            item[f"{key}_n"] = len(vals)
        out.append(item)
    return out


# =============================================================================
# I/O
# =============================================================================


def read_features(source_root: Path, capture_id: str) -> dict[str, Any]:
    """選定用の特徴量（EXIF 要約 + カメラ JPEG 縮小版の明暗比率）。GPS は読まない。"""
    from local_raw.exif import read_capture_exif

    jpg = source_file(source_root, capture_id, "JPG")
    exif = read_capture_exif(jpg)
    feats: dict[str, Any] = {
        "capture_id": capture_id,
        "iso": exif.get("iso"),
        "exposure_time": exif.get("exposure_time"),
        "f_number": exif.get("f_number"),
        "focal_length": exif.get("focal_length"),
        "lens_model": exif.get("lens_model"),
        "orientation": exif.get("orientation"),
        "captured_at": exif.get("captured_at"),
    }
    feats["ev100"] = ev100(feats["f_number"], feats["exposure_time"], feats["iso"])
    feats["hour"] = capture_hour(feats["captured_at"])
    try:
        with Image.open(jpg) as image:
            image.draft("RGB", (image.width // 8, image.height // 8))
            small = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
        luma = luma_of(small)
        feats["bright_frac"] = float((luma > 0.95).mean())
        feats["dark_frac"] = float((luma < 0.10).mean())
        feats["jpeg_mean_luma"] = float(luma.mean())
    except OSError:
        pass
    return feats


def paired_ids(source_root: Path) -> list[str]:
    """ARW と JPG が揃う撮影 ID（拡張子の大文字小文字は問わない。``local_raw.source_files`` と同じ規則）。"""
    from local_raw import source_files

    jpg = set(source_files.list_capture_files(source_root, "JPG"))
    arw = set(source_files.list_capture_files(source_root, "ARW"))
    return sorted(cid for cid in jpg & arw if CAPTURE_ID_PATTERN.fullmatch(cid))


def source_file(source_root: Path, capture_id: str, kind: str) -> Path:
    """撮影 ID の ARW / JPG を実エントリ名で返す（無ければ正規表記のパス。存在確認は呼び出し側）。"""
    from local_raw import source_files

    return source_files.find_capture_file(source_root, capture_id, kind) or source_root / f"{capture_id}.{kind}"


def read_rgb_upright(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(ImageOps.exif_transpose(image).convert("RGB"), dtype=np.uint8)


def resize_to(rgb: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    if (rgb.shape[1], rgb.shape[0]) == size:
        return rgb
    return cv2.resize(rgb, size, interpolation=cv2.INTER_AREA)


def save_plain_jpeg(rgb: np.ndarray, path: Path, quality: int = 90) -> None:
    """EXIF・ICC を付けずに保存する（比較シートに GPS 等を持ち込まない）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb).save(path, "JPEG", quality=quality)


def run_timed(cmd: list[str], stdin: str | None = None, timeout: float = 600.0) -> dict[str, Any]:
    started = time.monotonic()
    proc = subprocess.run(
        ["/usr/bin/time", "-l", *cmd],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    wall = time.monotonic() - started
    stats = parse_time_l(proc.stderr)
    stdout_lines = [line for line in proc.stdout.splitlines() if line.strip()]
    payload: dict[str, Any] | None = None
    if stdout_lines:
        try:
            payload = json.loads(stdout_lines[-1])
        except json.JSONDecodeError:
            payload = None
    return {
        "returncode": proc.returncode,
        "wall_s": wall,
        "payload": payload,
        "stderr_tail": proc.stderr[-2000:],
        **stats,
    }


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # 古い Pillow
        return ImageFont.load_default()


def labeled_row(panels: list[tuple[str, np.ndarray]], header: str | None = None) -> np.ndarray:
    """同寸パネルを横に並べ、各パネルの上にラベルを付けた RGB 配列を返す。"""
    lines = max(1, max(str(label).count("\n") + 1 for label, _ in panels))
    label_h = 34 + 24 * (lines - 1)
    header_h = 36 if header else 0
    h, w = panels[0][1].shape[:2]
    gap = 8
    canvas = Image.new("RGB", (len(panels) * w + (len(panels) - 1) * gap, h + label_h + header_h), (24, 24, 24))
    draw = ImageDraw.Draw(canvas)
    if header:
        draw.text((8, 6), header, fill=(240, 240, 240), font=_font(22))
    for index, (label, rgb) in enumerate(panels):
        x = index * (w + gap)
        draw.text((x + 6, header_h + 6), label, fill=(255, 220, 120), font=_font(20))
        canvas.paste(Image.fromarray(rgb), (x, header_h + label_h))
    return np.asarray(canvas)


def crop_boxes(width: int, height: int, size: int) -> list[tuple[str, tuple[int, int, int, int]]]:
    """中央と左上隅の正方クロップ（size px、画像より大きければ縮める）。"""
    s = min(size, width, height)
    cx, cy = (width - s) // 2, (height - s) // 2
    return [("center", (cx, cy, cx + s, cy + s)), ("corner TL", (0, 0, s, s))]


def detail_box(gray: np.ndarray, size: int) -> tuple[int, int, int, int]:
    """Laplacian 分散が最大の正方領域（size px、半分ずつずらして探索）。解像感の比較用。"""
    h, w = gray.shape[:2]
    s = min(size, w, h)
    lap = np.abs(cv2.Laplacian(gray.astype(np.float32), cv2.CV_32F))
    step = max(1, s // 2)
    best: tuple[float, int, int] = (-1.0, 0, 0)
    for y in range(0, h - s + 1, step):
        for x in range(0, w - s + 1, step):
            score = float(lap[y:y + s, x:x + s].mean())
            if score > best[0]:
                best = (score, x, y)
    _, x, y = best
    return (x, y, x + s, y + s)


# =============================================================================
# rawpy ワーカー（別プロセス）
# =============================================================================


def worker_rawpy(args: argparse.Namespace) -> None:
    from unittest import mock

    from local_raw import config, engine

    source_root = Path(args.source_root).resolve()
    out_dir = Path(args.worker_out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    capture_id = validate_capture_id(args.worker_rawpy)
    preview = args.worker_mode == "preview"
    original_sharpen_params = engine._capture_sharpen_params

    def no_capture_sharpen(long_edge: int, sharpness: float, iso: float | None) -> tuple[float, float, float]:
        # 撮影時シャープだけを無効化する（gain=0 で _apply_tone_array が USM を飛ばす）。
        # sharpness は ADJUSTMENT_RANGES で 0.8 未満にできないため、engine は変更せずここで差し替える。
        sigma, _gain, threshold = original_sharpen_params(long_edge, sharpness, iso)
        return sigma, 0.0, threshold

    sharpen_patch = (
        mock.patch.object(engine, "_capture_sharpen_params", no_capture_sharpen)
        if args.worker_nosharp else contextlib.nullcontext()
    )
    with mock.patch.object(config, "source_root", return_value=source_root), \
         mock.patch.object(engine, "OUTPUT_ROOT", out_dir), \
         mock.patch.object(engine, "PREVIEW_ROOT", out_dir), \
         sharpen_patch:
        kwargs: dict[str, Any] = {"preview": preview, "force": True}
        if preview:
            kwargs["max_edge"] = args.max_edge  # 明示しないと preview は 1600px に落ちる
        result = engine.develop_raw(capture_id, engine.DEFAULT_PRESET, **kwargs)
    recipe = json.loads((out_dir / result["recipe_filename"]).read_text(encoding="utf-8"))
    print(json.dumps({
        "ok": True,
        "output": str(out_dir / result["filename"]),
        "elapsed_seconds": result["elapsed_seconds"],
        "width": result["width"],
        "height": result["height"],
        "preset": result["preset"],
        "lens_correction": result.get("lens_correction"),
        "dark_scene": recipe.get("dark_scene"),
        "iso": recipe.get("iso"),
        "engine_version": engine.ENGINE_VERSION,
        "capture_sharpen": not args.worker_nosharp,
    }, ensure_ascii=False, default=str))


def worker_noop(_: argparse.Namespace) -> None:
    """rawpy ワーカーの常駐コスト（インタプリタ + numpy/cv2/rawpy import）の基準値。"""
    from local_raw import engine  # noqa: F401

    print(json.dumps({"ok": True}))


# =============================================================================
# オーケストレータ
# =============================================================================


@dataclass
class RunContext:
    source_root: Path
    out: Path
    cli: Path
    max_edge: int
    log: list[str] = field(default_factory=list)

    def say(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        print(line, flush=True)
        self.log.append(line)


def run_rawpy(ctx: RunContext, capture_id: str, mode: str, *, nosharp: bool = False) -> dict[str, Any]:
    # nosharp は params_hash が変わらず同名ファイルになるので、出力ディレクトリを分ける。
    out_dir = ctx.out / ("rawpy_nosharp" if nosharp else "rawpy") / mode
    cmd = [
        sys.executable, str(Path(__file__).resolve()),
        "--source-root", str(ctx.source_root),
        "--worker-rawpy", capture_id,
        "--worker-mode", mode,
        "--worker-out", str(out_dir),
        "--max-edge", str(ctx.max_edge),
    ]
    if nosharp:
        cmd.append("--worker-nosharp")
    res = run_timed(cmd)
    payload = res["payload"] or {}
    row: dict[str, Any] = {
        "wall_s": res["wall_s"],
        "maxrss_bytes": res.get("maxrss_bytes"),
        "peak_footprint_bytes": res.get("peak_footprint_bytes"),
    }
    if res["returncode"] != 0 or not payload.get("ok"):
        row["error"] = f"rawpy worker failed rc={res['returncode']}: {res['stderr_tail'][-400:]}"
        return row
    lens = payload.get("lens_correction") or {}
    dark = payload.get("dark_scene") or {}
    row.update({
        "output": payload["output"],
        "engine_elapsed_s": payload.get("elapsed_seconds"),
        "lens_applied": bool(lens.get("applied")) if isinstance(lens, dict) else None,
        "dark_scene": bool(dark.get("dark_scene")) if isinstance(dark, dict) else None,
    })
    return row


def run_coreimage(
    ctx: RunContext,
    capture_id: str,
    mode: str,
    lens: bool,
    *,
    decoder_version: str = CI_DECODER_VERSION,
    profile: str = "neutral",
    variant: str | None = None,
    overrides: dict[str, float] | None = None,
    output_format: str = "jpeg",
    output_color_space: str | None = None,
    max_edge: int | None = None,
) -> dict[str, Any]:
    """Core Image CLI を 1 回呼ぶ。

    ``overrides`` は CLI の settings 個別上書きキー（``luminance_noise_reduction`` 等。Issue #79）。
    ``output_format`` が ``tiff16`` のときは ``.tif`` に書き、``output_color_space``（srgb / display_p3）を渡せる。
    ``max_edge`` は preview の長辺（省略時 ``ctx.max_edge``）。既定引数のままなら従来と同じリクエストになる。
    """
    out_dir = ctx.out / "coreimage" / mode
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_{variant}" if variant else ("_lens" if lens else "")
    edge = max_edge if (mode == "preview" and max_edge and max_edge != ctx.max_edge) else None
    if edge:
        suffix += f"_e{edge}"
    output = out_dir / f"{capture_id}_{mode}{suffix}.{'tif' if output_format == 'tiff16' else 'jpg'}"
    settings: dict[str, Any] = {"profile": profile, "lens_correction": lens}
    if overrides:
        settings.update(overrides)
    request: dict[str, Any] = {
        "input": str(source_file(ctx.source_root, capture_id, "ARW")),
        "output": str(output),
        "mode": mode,
        "decoder_version": decoder_version,
        "settings": settings,
    }
    if output_format != "jpeg":
        request["format"] = output_format
    if output_color_space:
        request["output_color_space"] = output_color_space
    if mode == "preview":
        request["max_edge"] = max_edge or ctx.max_edge
    res = run_timed([str(ctx.cli)], stdin=json.dumps(request))
    payload = res["payload"] or {}
    row: dict[str, Any] = {
        "wall_s": res["wall_s"],
        "maxrss_bytes": res.get("maxrss_bytes"),
        "peak_footprint_bytes": res.get("peak_footprint_bytes"),
    }
    if res["returncode"] != 0 or not payload.get("ok"):
        row["error"] = f"coreimage failed rc={res['returncode']}: {json.dumps(payload.get('error'))}"
        return row
    # 来歴として CLI の応答 JSON（applied を含む）を出力の隣に残す。
    output.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elapsed = payload.get("elapsed_ms") or {}
    applied = payload.get("applied") or {}
    row.update({
        "output": str(output),
        "engine_elapsed_s": (elapsed.get("total") or 0.0) / 1000.0,
        "ci_decode_ms": elapsed.get("decode"),
        "ci_render_encode_ms": elapsed.get("render_encode"),
        "lens_applied": applied.get("lens_correction_enabled"),
        "ci_baseline_exposure": applied.get("baseline_exposure"),
        # 評価では「上書きしていない値が何だったか」を残す（apple_default の既定は OS 更新で変わりうる）。
        "ci_sharpness": applied.get("sharpness_amount"),
        "ci_lum_nr": applied.get("luminance_noise_reduction_amount"),
        "ci_color_nr": applied.get("color_noise_reduction_amount"),
        "ci_edr": applied.get("extended_dynamic_range_amount"),
        "ci_boost": applied.get("boost_amount"),
        "ci_boost_shadow": applied.get("boost_shadow_amount"),
    })
    return row


def compare_capture(
    ctx: RunContext,
    capture_id: str,
    mode: str,
    features: dict[str, Any],
    category: str,
    with_lens: bool,
) -> list[dict[str, Any]]:
    """1 カット × 1 モードを 3〜4 エンジンで回し、指標とシートを作る。"""
    engine_rows: dict[str, dict[str, Any]] = {
        "rawpy": run_rawpy(ctx, capture_id, mode),
        "coreimage": run_coreimage(ctx, capture_id, mode, lens=False),
    }
    if with_lens:
        engine_rows["coreimage_lens"] = run_coreimage(ctx, capture_id, mode, lens=True)

    camera = read_rgb_upright(source_file(ctx.source_root, capture_id, "JPG"))
    # 比較の基準寸法: Core Image の出力（無ければカメラ JPEG を max_edge に収めた寸法）。
    target: tuple[int, int] | None = None
    images: dict[str, np.ndarray] = {}
    for name in ("coreimage", "coreimage_lens", "rawpy"):
        row = engine_rows.get(name)
        if row and not row.get("error"):
            images[name] = read_rgb_upright(Path(row["output"]))
            if target is None:
                target = (images[name].shape[1], images[name].shape[0])
    if target is None:
        h, w = camera.shape[:2]
        target = fit_size(w, h, ctx.max_edge) if mode == "preview" else (w, h)
    images["camera"] = camera

    base = {
        "capture_id": capture_id,
        "mode": mode,
        "category": category,
        "iso": features.get("iso"),
        "lens_model": features.get("lens_model"),
        "ev100": features.get("ev100"),
        "orientation": features.get("orientation"),
    }
    aligned: dict[str, np.ndarray] = {}
    rows: list[dict[str, Any]] = []
    for name in ENGINES:
        if name == "camera":
            row: dict[str, Any] = {}
        else:
            if name not in engine_rows:
                continue
            row = dict(engine_rows[name])
        if name in images:
            src = images[name]
            row["native_width"], row["native_height"] = src.shape[1], src.shape[0]
            row["aspect_mismatch"] = aspect_mismatch((src.shape[1], src.shape[0]), target)
            aligned[name] = resize_to(src, target)
        rows.append({**base, "engine": name, **row})

    reference = aligned.get("camera")
    for row in rows:
        name = row["engine"]
        if name not in aligned or row.get("error"):
            continue
        ref = None if name == "camera" else reference
        row.update(image_metrics(aligned[name], ref))

    # --- シート（EXIF 無し） ---
    sheet_dir = ctx.out / "sheets"
    caption = (
        f"{capture_id}  {mode}  ISO {features.get('iso')}  {features.get('lens_model')}  "
        f"[{category}]"
    )
    order = [n for n in ENGINES if n in aligned]
    if mode == "preview":
        panels = []
        for name in order:
            w, h = fit_size(target[0], target[1], 640)
            panels.append((ENGINE_LABELS[name], resize_to(aligned[name], (w, h))))
        save_plain_jpeg(labeled_row(panels, caption), sheet_dir / f"{capture_id}_{mode}_side.jpg")
    crop_rows = []
    boxes = crop_boxes(target[0], target[1], 512)
    if "camera" in aligned:
        boxes.append(("detail", detail_box(cv2.cvtColor(aligned["camera"], cv2.COLOR_RGB2GRAY), 512)))
    for label, (x0, y0, x1, y1) in boxes:
        crop_rows.append(labeled_row(
            [(f"{ENGINE_LABELS[n]} / {label}", aligned[n][y0:y1, x0:x1]) for n in order],
        ))
    width = max(r.shape[1] for r in crop_rows)
    stacked = np.vstack([
        np.pad(r, ((0, 0), (0, width - r.shape[1]), (0, 0)), constant_values=24) for r in crop_rows
    ])
    header = labeled_row([("", stacked)], f"{caption}  crops at {target[0]}x{target[1]} (1:1 of that size)")
    save_plain_jpeg(header, sheet_dir / f"{capture_id}_{mode}_crops.jpg")
    return rows


METRIC_KEYS = (
    "wall_s", "engine_elapsed_s", "maxrss_bytes", "peak_footprint_bytes",
    "mean_luma", "p01_luma", "p50_luma", "p99_luma",
    "clip_high_any", "clip_high_all", "clip_low",
    "laplacian_variance", "flat_noise", "mean_saturation",
    "camera_delta_e76", "camera_rgb_mae", "camera_luma_diff",
)


# =============================================================================
# 解像感（ピント甘さ）評価モード --sharpness
# =============================================================================

#: (variant, ラベル)。camera が基準。*_usm は CI 出力に rawpy と同じ後段仕上げ
#: （engine._apply_tone_array = 撮影時シャープ込み）を Python で適用した派生。
SHARPNESS_VARIANTS: tuple[tuple[str, str], ...] = (
    ("camera", "camera JPEG"),
    ("rawpy", "rawpy current (sharpen ON)"),
    ("rawpy_nosharp", "rawpy sharpen OFF"),
    ("ci_v9_l0", "CI neutral v9 lens OFF"),
    ("ci_v9_l1", "CI neutral v9 lens ON"),
    ("ci_v8_l0", "CI neutral v8 lens OFF"),
    ("ci_v8_l1", "CI neutral v8 lens ON"),
    ("ci_apple_v9_l1", "CI apple_default v9 lens ON"),
    ("ci_v9_l1_usm", "CI v9 lens ON + rawpy finish"),
    ("ci_v8_l0_usm", "CI v8 lens OFF + rawpy finish"),
)
SHARPNESS_LABELS = dict(SHARPNESS_VARIANTS)
#: CLI を呼ぶバリアント: variant -> (decoder_version, profile, lens_correction)
CI_SHARPNESS_REQUESTS: dict[str, tuple[str, str, bool]] = {
    "ci_v9_l0": ("9", "neutral", False),
    "ci_v9_l1": ("9", "neutral", True),
    "ci_v8_l0": ("8", "neutral", False),
    "ci_v8_l1": ("8", "neutral", True),
    "ci_apple_v9_l1": ("9", "apple_default", True),
}
#: 派生バリアント: variant -> 元にする CI バリアント
CI_USM_DERIVED: dict[str, str] = {"ci_v9_l1_usm": "ci_v9_l1", "ci_v8_l0_usm": "ci_v8_l0"}
#: ROI 1 辺 = 出力長辺 × この比（preview 2400px で 240px、full 6000px で 600px = 同じ画角）。
ROI_FRACTION = 0.1
#: ROI の位相相関で採用する最小 response。これ未満・ずれが ROI/4 超は「位置合わせ不能」として除外。
ROI_MIN_RESPONSE = 0.08

SHARPNESS_KEYS = (
    "lap_ratio_detail", "ten_ratio_detail", "hf_ratio_detail",
    "lap_ratio_center", "ten_ratio_center", "lap_ratio_corner", "ten_ratio_corner",
    "edge_width_px", "edge_width_ratio", "edge_n",
    "flat_noise", "flat_noise_ratio", "roi_excluded",
)


def fit_to_target(rgb: np.ndarray, target: tuple[int, int]) -> tuple[np.ndarray, str]:
    """各エンジンの縮小経路を評価対象に含めるため、再標本化は避けて寸法だけ合わせる。

    2% 以内の差は中央クロップ / 端の複製で合わせる（"crop"）。それ以上違えば AREA で
    リサイズし "resampled" を返す（比較の公平性が崩れるので記録する）。
    """
    tw, th = target
    h, w = rgb.shape[:2]
    if (w, h) == (tw, th):
        return rgb, "exact"
    if abs(w - tw) / tw <= 0.02 and abs(h - th) / th <= 0.02:
        x0, y0 = max(0, (w - tw) // 2), max(0, (h - th) // 2)
        out = rgb[y0:y0 + th, x0:x0 + tw]
        pad_y, pad_x = th - out.shape[0], tw - out.shape[1]
        if pad_y or pad_x:
            out = np.pad(out, ((pad_y // 2, pad_y - pad_y // 2), (pad_x // 2, pad_x - pad_x // 2), (0, 0)), mode="edge")
        return np.ascontiguousarray(out), "crop"
    return resize_to(rgb, target), "resampled"


def apply_rawpy_finish(rgb: np.ndarray, iso: Any) -> np.ndarray:
    """CI 出力（8bit sRGB）へ rawpy 既定プリセットの後段トーン（撮影時シャープ込み）を適用する。

    ロジックは複製せず ``engine._apply_tone_array`` をそのまま呼ぶ（brightness / contrast /
    saturation / sharpness は DEFAULT_PRESET の値。shadow_lift はリサイズ前段の処理なので含めない）。
    """
    from local_raw import engine

    adjustments = engine.resolve_adjustments(engine.DEFAULT_PRESET, None)
    iso_value = float(iso) if isinstance(iso, (int, float)) else None
    toned = engine._apply_tone_array(rgb.astype(np.float32) / 255.0, adjustments, iso=iso_value)
    return np.rint(toned * 255.0).astype(np.uint8)


def sharpness_capture(
    ctx: RunContext,
    capture_id: str,
    mode: str,
    features: dict[str, Any],
    category: str,
) -> list[dict[str, Any]]:
    """1 カット × 1 モードを全バリアントで現像し、解像感指標と等倍クロップシートを作る。"""
    from concurrent.futures import ThreadPoolExecutor

    jobs: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs["rawpy"] = pool.submit(run_rawpy, ctx, capture_id, mode)
        jobs["rawpy_nosharp"] = pool.submit(run_rawpy, ctx, capture_id, mode, nosharp=True)
        for variant, (decoder, profile, lens) in CI_SHARPNESS_REQUESTS.items():
            jobs[variant] = pool.submit(
                run_coreimage, ctx, capture_id, mode, lens,
                decoder_version=decoder, profile=profile, variant=variant,
            )
        engine_rows = {name: future.result() for name, future in jobs.items()}

    camera = read_rgb_upright(source_file(ctx.source_root, capture_id, "JPG"))
    ch, cw = camera.shape[:2]
    target = fit_size(cw, ch, ctx.max_edge) if mode == "preview" else (cw, ch)
    images: dict[str, np.ndarray] = {"camera": resize_to(camera, target)}
    rows_by_variant: dict[str, dict[str, Any]] = {"camera": {"fit": "camera"}}
    for name, row in engine_rows.items():
        row = dict(row)
        if not row.get("error"):
            src = read_rgb_upright(Path(row["output"]))
            row["native_width"], row["native_height"] = src.shape[1], src.shape[0]
            images[name], row["fit"] = fit_to_target(src, target)
        rows_by_variant[name] = row
    iso = features.get("iso")
    for derived, base_name in CI_USM_DERIVED.items():
        if base_name in images:
            images[derived] = apply_rawpy_finish(images[base_name], iso)
            rows_by_variant[derived] = {"fit": rows_by_variant[base_name].get("fit"), "derived_from": base_name}
        else:
            rows_by_variant[derived] = {"error": f"base variant {base_name} unavailable"}

    grays = {name: cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32) for name, rgb in images.items()}
    cam_gray = grays["camera"]
    roi_size = max(96, int(round(max(target) * ROI_FRACTION)))
    boxes = roi_boxes(cam_gray, roi_size, k=3)
    max_shift = roi_size / 4.0
    edge_points = {
        label: find_edge_points(cam_gray, box)
        for label, box in boxes if label != "corner"
    }
    cam_profiles = {label: sample_profiles(cam_gray, pts) for label, pts in edge_points.items() if len(pts)}
    cam_widths = {label: [edge_rise_width(p) for p in prof] for label, prof in cam_profiles.items()}

    def roi_stats(gray: np.ndarray, box: tuple[int, int, int, int]) -> tuple[float, float, float]:
        x0, y0, x1, y1 = box
        patch = gray[y0:y1, x0:x1]
        return laplacian_var(patch), tenengrad(patch), highfreq_energy_ratio(patch)

    cam_roi = {label: roi_stats(cam_gray, box) for label, box in boxes}
    cam_noise = flat_noise(np.clip(cam_gray, 0, 255).astype(np.uint8))

    base = {
        "capture_id": capture_id,
        "mode": mode,
        "category": category,
        "iso": iso,
        "iso_band": iso_band(iso),
        "lens_model": features.get("lens_model"),
        "focal_length": features.get("focal_length"),
        "width": target[0],
        "height": target[1],
        "roi_size": roi_size,
    }
    shifted_boxes: dict[str, dict[str, tuple[int, int, int, int]]] = {}
    rows: list[dict[str, Any]] = []
    for variant, _label in SHARPNESS_VARIANTS:
        row: dict[str, Any] = {**base, "variant": variant, **rows_by_variant.get(variant, {})}
        if variant not in grays:
            row.setdefault("error", "no image")
            rows.append(row)
            continue
        gray = grays[variant]
        per_label: dict[str, tuple[float, float, float]] = {}
        widths: list[float] = []
        width_ratios: list[float] = []
        excluded = 0
        shifted_boxes[variant] = {}
        for label, box in boxes:
            if variant == "camera":
                dx = dy = 0.0
                ok = True
            else:
                dx, dy, response = roi_shift(cam_gray, gray, box, margin=int(max_shift))
                ok = response >= ROI_MIN_RESPONSE and math.hypot(dx, dy) <= max_shift
            if not ok:
                excluded += 1
                continue
            vbox = shift_box(box, dx, dy, target[0], target[1])
            shifted_boxes[variant][label] = vbox
            per_label[label] = roi_stats(gray, vbox)
            pts = edge_points.get(label)
            if pts is not None and len(pts):
                moved = pts.copy()
                moved[:, 0] += dx
                moved[:, 1] += dy
                for w_var, w_cam in zip((edge_rise_width(p) for p in sample_profiles(gray, moved)), cam_widths[label]):
                    if w_var is not None:
                        widths.append(w_var)
                        if w_cam:
                            width_ratios.append(w_var / w_cam)

        def ratio(label_prefix: str, index: int) -> float:
            vals = [
                per_label[label][index] / cam_roi[label][index]
                for label in per_label
                if label.startswith(label_prefix) and cam_roi[label][index] > 0
            ]
            return median_or_nan(vals)

        noise = flat_noise(np.clip(gray, 0, 255).astype(np.uint8))
        row.update({
            "lap_ratio_detail": ratio("detail", 0),
            "ten_ratio_detail": ratio("detail", 1),
            "hf_ratio_detail": ratio("detail", 2),
            "lap_ratio_center": ratio("center", 0),
            "ten_ratio_center": ratio("center", 1),
            "lap_ratio_corner": ratio("corner", 0),
            "ten_ratio_corner": ratio("corner", 1),
            "lap_detail_abs": median_or_nan(v[0] for k, v in per_label.items() if k.startswith("detail")),
            "ten_detail_abs": median_or_nan(v[1] for k, v in per_label.items() if k.startswith("detail")),
            "hf_detail_abs": median_or_nan(v[2] for k, v in per_label.items() if k.startswith("detail")),
            "edge_width_px": median_or_nan(widths),
            "edge_width_ratio": median_or_nan(width_ratios),
            "edge_n": len(widths),
            "flat_noise": noise,
            "flat_noise_ratio": noise / cam_noise if cam_noise and math.isfinite(cam_noise) else float("nan"),
            "roi_excluded": excluded,
        })
        rows.append(row)

    # --- 等倍クロップシート（EXIF 無し）: ROI ごとに 2 段 × 5 枚 ---
    panel = min(roi_size, 320)
    caption = f"{capture_id}  {mode} {target[0]}x{target[1]}  ISO {iso}  {features.get('lens_model')}  {features.get('focal_length')}mm"
    for label in ("detail1", "detail2", "center", "corner"):
        panels = []
        for variant, vlabel in SHARPNESS_VARIANTS:
            vbox = shifted_boxes.get(variant, {}).get(label)
            if vbox is None:
                crop = np.full((panel, panel, 3), 40, dtype=np.uint8)
                vlabel = f"{vlabel} (n/a)"
            else:
                x0, y0, x1, y1 = vbox
                ox, oy = x0 + (x1 - x0 - panel) // 2, y0 + (y1 - y0 - panel) // 2
                crop = images[variant][oy:oy + panel, ox:ox + panel]
            panels.append((vlabel, crop))
        if not panels:
            continue
        top = labeled_row(panels[:5])
        bottom = labeled_row(panels[5:])
        width = max(top.shape[1], bottom.shape[1])
        stacked = np.vstack([np.pad(r, ((0, 0), (0, width - r.shape[1]), (0, 0)), constant_values=24) for r in (top, bottom)])
        sheet = labeled_row([("", stacked)], f"{caption}  ROI {label} 1:1 ({panel}px)")
        save_plain_jpeg(sheet, ctx.out / "sheets" / f"{capture_id}_{mode}_{label}.jpg", quality=95)
    return rows


def sharpness_table_md(summary: list[dict[str, Any]], group_cols: tuple[str, ...]) -> str:
    """summarize_sharpness の結果を Markdown 表にする（レポート転記用）。"""
    cols = ("lap_ratio_detail", "ten_ratio_detail", "hf_ratio_detail", "lap_ratio_corner",
            "edge_width_px", "edge_width_ratio", "flat_noise", "flat_noise_ratio")
    head = "| " + " | ".join([*group_cols, "n", *cols]) + " |"
    sep = "|" + "---|" * (len(group_cols) + 1 + len(cols))
    lines = [head, sep]
    for item in summary:
        cells = [str(item.get(c)) for c in group_cols] + [str(item.get("n"))]
        for c in cols:
            v = item.get(c)
            cells.append("—" if v is None else f"{v:.2f}")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def run_sharpness(
    ctx: RunContext,
    selection: list[dict[str, Any]],
    features_by_id: dict[str, dict[str, Any]],
    full_ids: list[str],
) -> None:
    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    cat_by_id = {r["capture_id"]: r["category"] for r in selection}
    jobs = [(r["capture_id"], "preview") for r in selection] + [(cid, "full") for cid in full_ids]
    for index, (cid, mode) in enumerate(jobs, 1):
        ctx.say(f"[sharpness] ({index}/{len(jobs)}) {cid} {mode}")
        try:
            new_rows = sharpness_capture(ctx, cid, mode, features_by_id[cid], cat_by_id.get(cid, "other"))
            rows.extend(new_rows)
            for r in new_rows:
                if r.get("error"):
                    ctx.say(f"  ERROR {r['variant']}: {str(r['error'])[:300]}")
        except Exception as error:  # 1 カットの失敗で全体を止めない
            rows.append({"capture_id": cid, "mode": mode, "variant": "harness", "error": f"{type(error).__name__}: {error}"})
            ctx.say(f"  ERROR {type(error).__name__}: {error}")
        # 途中経過も残す（長時間実行の中断に備える）
        if index % 5 == 0 or index == len(jobs):
            fields = sorted({k for r in rows for k in r}, key=lambda k: (k not in ("capture_id", "mode", "variant"), k))
            with (ctx.out / "sharpness_metrics.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)

    by_mode = summarize_sharpness(rows, SHARPNESS_KEYS, ("mode", "variant"))
    by_band = summarize_sharpness(rows, SHARPNESS_KEYS, ("mode", "iso_band", "variant"))
    errors = [r for r in rows if r.get("error")]
    summary = {
        "out": str(ctx.out),
        "variants": dict(SHARPNESS_VARIANTS),
        "ci_requests": {k: {"decoder_version": v[0], "profile": v[1], "lens_correction": v[2]} for k, v in CI_SHARPNESS_REQUESTS.items()},
        "selection_count": len(selection),
        "full_ids": full_ids,
        "elapsed_total_s": time.monotonic() - started,
        "errors": errors,
        "by_mode": by_mode,
        "by_mode_iso_band": by_band,
    }
    (ctx.out / "sharpness_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    (ctx.out / "sharpness_tables.md").write_text(
        "## by mode\n\n" + sharpness_table_md(by_mode, ("mode", "variant"))
        + "\n## by mode x ISO band\n\n" + sharpness_table_md(by_band, ("mode", "iso_band", "variant")),
        encoding="utf-8")
    (ctx.out / "run.log").write_text("\n".join(ctx.log) + "\n", encoding="utf-8")
    ctx.say(f"[sharpness] 完了: {ctx.out}（エラー {len(errors)} 件）")


# =============================================================================
# 夜景・星空・白点（--night）/ ブラケット（--bracket）/ ハイライト（--highlight）の純粋関数
# （tests/test_compare_engines.py で検証。設計書 docs/design/coreimage-feature-migration.md §4）
# =============================================================================

#: 空領域（平坦タイル）の選び方: flat_noise と同じタイル分割で、構造（ぼかし後の勾配）が小さい下位この割合。
SKY_TILE_BLOCK = 32
SKY_TILE_FRACTION = 0.25
#: 白点（インパルス）ノイズ: 3x3 メディアンとの差がこの階調（8bit）以上で、連結面積がこの画素数以下の孤立点。
IMPULSE_MIN_DIFF = 24.0
IMPULSE_MAX_AREA = 3
#: 星候補の除外マスクを膨らませる半径（px）。位置合わせの残差と星像の裾を覆う。
STAR_EXCLUDE_DILATE = 3
#: 四隅 / 中央の計測ボックスの 1 辺（短辺に対する比）。
CORNER_BOX_FRACTION = 0.15
#: 露出比の計測に使う画素（8bit の最大チャンネルがこの範囲）と中央領域の比。
EXPOSURE_PIXEL_RANGE = (8, 240)
EXPOSURE_CENTER_FRACTION = 0.6
#: ブラケット候補: 同一レンズ・焦点距離・向きで、この秒数以内に撮られ、EV 差がこの値以上の連続ペア。
BRACKET_MAX_GAP_S = 600.0
BRACKET_MIN_DELTA_EV = 0.3
#: クリップ境界の帯幅（px）と、マゼンタ寄りとみなす最小クロマ（min(R,B) − G、0..1）。
CLIP_BAND_PX = 3
MAGENTA_MIN_CHROMA = 0.06


def srgb_to_linear(rgb01: np.ndarray) -> np.ndarray:
    """sRGB（0..1、ガンマ後）をリニアへ（IEC 61966-2-1 の区分関数）。"""
    v = np.asarray(rgb01, dtype=np.float32)
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4).astype(np.float32)


def _dc():
    """``local_raw.development_candidates``（星判定の本体）。遅延 import（engine → rawpy を引く）。"""
    from local_raw import development_candidates

    return development_candidates


#: 「強い星」の最大局所コントラスト（STAR_PEAK_HIGH 0.08 の 2 倍）。高 ISO の粒状ノイズを星と数えないための補助基準。
STAR_PEAK_STRONG = 0.16


def star_point_mask(gray01: np.ndarray, peak: float | None = None) -> np.ndarray:
    """``development_candidates._point_light_count`` と同じ基準で星候補（小さな点光源）の画素マスクを返す。

    gray − σ3 ガウス ≥ STAR_PEAK_LOW の連結成分のうち、面積が上限以下で最大差 ≥ peak（既定 STAR_PEAK_HIGH）のもの。
    カメラ JPEG で作ったこのマスクを膨らませて除外すれば、「星ではない白点」だけを数えられる。
    """
    dc = _dc()
    g = np.asarray(gray01, dtype=np.float32)
    diff = g - cv2.GaussianBlur(g, (0, 0), dc.STAR_LOCAL_SIGMA)
    cand = (diff >= dc.STAR_PEAK_LOW).astype(np.uint8)
    h, w = g.shape
    max_area = max(dc.STAR_POINT_MIN_AREA_CAP, int(round(h * w * dc.STAR_POINT_MAX_AREA_RATIO)))
    high = dc.STAR_PEAK_HIGH if peak is None else peak
    count, labels, stats, _ = cv2.connectedComponentsWithStats(cand, 8)
    keep = np.zeros(count, dtype=bool)
    for index in range(1, count):
        x, y, cw, ch, area = (int(v) for v in stats[index])
        if area > max_area:
            continue
        if float(diff[y:y + ch, x:x + cw].max()) >= high:
            keep[index] = True
    return keep[labels]


def point_recall(reference_points: np.ndarray, target_points: np.ndarray, radius: int = 2) -> dict[str, float]:
    """基準（カメラ JPEG）の星候補成分のうち、対象側にも点が残っている割合（位置合わせ済みのマスク同士）。

    基準成分を radius px 膨らませた足跡に対象の点画素が 1 つでも重なれば「残っている」。
    戻り値: recall（0..1、基準成分が無ければ NaN）と基準成分数。
    """
    count, labels = cv2.connectedComponents(dilate_mask(reference_points, radius).astype(np.uint8), connectivity=8)
    n_ref = count - 1
    if n_ref <= 0:
        return {"recall": float("nan"), "n_ref": 0}
    hit = np.bincount(labels[target_points], minlength=count)[1:] > 0
    return {"recall": float(hit.mean()), "n_ref": int(n_ref)}


def piecewise_shift_mask(mask: np.ndarray, ref_gray: np.ndarray, tgt_gray: np.ndarray,
                         grid: tuple[int, int] = (6, 8), fallback: tuple[float, float] = (0.0, 0.0)) -> np.ndarray:
    """mask（ref 座標）を、画像を grid に区切った各セルの位相相関シフトで tgt 座標へ動かす。

    レンズ補正の有無で歪曲が違う画像同士（camera JPEG と CI lens OFF 等）でも、セル単位なら
    平行移動で近似できる。相関が取れないセルは fallback（全体シフト）を使う。
    """
    h, w = mask.shape
    rows, cols = grid
    out = np.zeros_like(mask, dtype=bool)
    a = cv2.GaussianBlur(ref_gray.astype(np.float32), (0, 0), 1.5)
    b = cv2.GaussianBlur(tgt_gray.astype(np.float32), (0, 0), 1.5)
    for r in range(rows):
        for c in range(cols):
            y0, y1 = h * r // rows, h * (r + 1) // rows
            x0, x1 = w * c // cols, w * (c + 1) // cols
            dx, dy, response = roi_shift(a, b, (x0, y0, x1, y1), margin=0)
            if response < ROI_MIN_RESPONSE or math.hypot(dx, dy) > min(y1 - y0, x1 - x0) / 4:
                dx, dy = fallback
            cell = np.zeros_like(mask, dtype=bool)
            cell[y0:y1, x0:x1] = mask[y0:y1, x0:x1]
            out |= shift_mask(cell, dx, dy)
    return out


def star_metrics(rgb8: np.ndarray, exif: dict[str, Any] | None) -> dict[str, Any]:
    """星の指標。``development_candidates`` の関数をそのまま使う（ロジックは複製しない）。

    - point_count / point_density: ``_point_light_count``（局所コントラストの絶対しきい値）。NR で微光星が
      消えると減る。**星の残り方の主指標**。
    - star_count / star_halo / star_color_saturation / is_star_scene: ``_star_measurements``。star_count は
      画像ごとの 99.35 パーセンタイルを閾値にするため、NR の有無では大きく動かない（相対指標）。
    画像は同じ画素寸法で比べること（σ3 は px 単位）。
    """
    dc = _dc()
    rgb01 = dc._normalise_rgb(rgb8)
    gray = np.dot(rgb01, np.array(REC709_LUMA, dtype=np.float32)).astype(np.float32)
    h, w = gray.shape
    point_count = dc._point_light_count(gray)
    measured = dc._star_measurements(rgb01, exif or {}, evaluate_gate=True)
    return {
        "point_count": int(point_count),
        "point_density": point_count / float(h * w),
        "star_count": int(measured["star_count"]),
        "star_halo": float(measured["star_halo"]),
        "star_color_saturation": float(measured["star_color_saturation"]),
        "is_star_scene": bool(measured["is_star_scene"]),
    }


def flat_tile_mask(gray8: np.ndarray, block: int = SKY_TILE_BLOCK, fraction: float = SKY_TILE_FRACTION) -> np.ndarray:
    """flat_noise と同じ分割で「構造が小さい下位 fraction」のタイルを True にした (rows, cols) マスク。

    白飛び・黒つぶれのタイル（平均 <5 or >250）は候補から外す。星空ではこれがほぼ空になる。
    """
    g = gray8.astype(np.float32)
    blurred = cv2.GaussianBlur(g, (0, 0), 1.5)
    gx = cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3)
    grad = np.sqrt(gx * gx + gy * gy)
    h, w = g.shape
    rows, cols = h // block, w // block
    mask = np.zeros((rows, cols), dtype=bool)
    if rows == 0 or cols == 0:
        return mask
    crop = (slice(0, rows * block), slice(0, cols * block))
    t_mean = g[crop].reshape(rows, block, cols, block).mean(axis=(1, 3))
    t_grad = grad[crop].reshape(rows, block, cols, block).mean(axis=(1, 3))
    valid = (t_mean > 5.0) & (t_mean < 250.0)
    if not np.any(valid):
        return mask
    count = max(1, int(round(int(valid.sum()) * fraction)))
    order = np.argsort(np.where(valid, t_grad, np.inf).ravel(), kind="stable")[:count]
    mask.ravel()[order] = True
    return mask


def tile_mask_to_pixels(tile_mask: np.ndarray, shape: tuple[int, int], block: int = SKY_TILE_BLOCK) -> np.ndarray:
    """(rows, cols) のタイルマスクを画像サイズの画素マスクへ広げる（端の余りは False）。"""
    h, w = shape
    out = np.zeros((h, w), dtype=bool)
    rows, cols = tile_mask.shape
    out[:rows * block, :cols * block] = np.repeat(np.repeat(tile_mask, block, axis=0), block, axis=1)
    return out


def sky_stats(rgb8: np.ndarray, sky_pixels: np.ndarray, exclude: np.ndarray | None = None) -> dict[str, float]:
    """空領域（sky_pixels）の明るさとノイズ。exclude（星候補など）は外す。

    - sky_mean_luma / sky_p50_luma: Rec.709 輝度（0..1）
    - sky_noise: 輝度ハイパス（原画 − σ1.5 ぼかし）の標準偏差（8bit）。flat_noise と同じ量
    - sky_chroma_noise: Lab の a*, b* のハイパス標準偏差の二乗和平方根（色ノイズ）
    - sky_hf_ratio: 空画素でのハイパス分散 / 全分散（0..1）。NR の塗りつぶしで小さくなる傾向（補助）
    画素が無ければ NaN。
    """
    mask = sky_pixels.copy()
    if exclude is not None:
        mask &= ~exclude
    out = {k: float("nan") for k in ("sky_mean_luma", "sky_p50_luma", "sky_noise", "sky_chroma_noise", "sky_hf_ratio")}
    out["sky_pixels"] = int(mask.sum())
    if out["sky_pixels"] < 64:
        return out
    rgb01 = rgb8.astype(np.float32) / 255.0
    luma = luma_of(rgb01)
    g = luma * 255.0
    hp = g - cv2.GaussianBlur(g, (0, 0), 1.5)
    lab = cv2.cvtColor(rgb01, cv2.COLOR_RGB2Lab)
    hp_a = lab[..., 1] - cv2.GaussianBlur(lab[..., 1], (0, 0), 1.5)
    hp_b = lab[..., 2] - cv2.GaussianBlur(lab[..., 2], (0, 0), 1.5)
    sel = luma[mask]
    out["sky_mean_luma"] = float(sel.mean())
    out["sky_p50_luma"] = float(np.median(sel))
    out["sky_noise"] = float(hp[mask].std())
    out["sky_chroma_noise"] = float(math.hypot(hp_a[mask].std(), hp_b[mask].std()))
    total = float(g[mask].var())
    out["sky_hf_ratio"] = float(hp[mask].var() / total) if total > 0 else float("nan")
    return out


def impulse_count(gray8: np.ndarray, exclude: np.ndarray | None = None,
                  min_diff: float = IMPULSE_MIN_DIFF, max_area: int = IMPULSE_MAX_AREA) -> dict[str, float]:
    """孤立した 1〜3 px の高輝度点（白点ノイズ）を数える。

    3x3 メディアンとの差が min_diff 以上の画素の連結成分で、面積 max_area 以下かつ exclude（星候補を
    膨らませたマスク）に触れないもの。count と 100 万画素あたりの数を返す。
    """
    g = gray8.astype(np.float32)
    med = cv2.medianBlur(gray8.astype(np.uint8), 3).astype(np.float32)
    cand = ((g - med) >= min_diff).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(cand, 8)
    hits = 0
    if count > 1:
        areas = stats[1:, cv2.CC_STAT_AREA]
        small = np.nonzero(areas <= max_area)[0] + 1
        if exclude is not None and len(small):
            touched = np.unique(labels[exclude])
            small = np.setdiff1d(small, touched)
        # 明るい塊の縁（角）も 3x3 メディアンより明るくなるので、成分の周囲 1px がすべて
        # 「成分の最大値 − min_diff」以下である（孤立している）ものだけを数える。
        h, w = g.shape
        for index in small:
            x, y, cw, ch, _ = (int(v) for v in stats[index])
            y0, y1, x0, x1 = max(0, y - 1), min(h, y + ch + 1), max(0, x - 1), min(w, x + cw + 1)
            patch = g[y0:y1, x0:x1]
            inside = labels[y0:y1, x0:x1] == index
            peak = float(patch[inside].max())
            if float(patch[~inside].max()) <= peak - min_diff:
                hits += 1
    h, w = g.shape
    return {"impulse_count": hits, "impulse_per_mpx": hits / (h * w / 1e6)}


def corner_center_noise(gray8: np.ndarray, fraction: float = CORNER_BOX_FRACTION) -> dict[str, float]:
    """四隅（4 箱の中央値）と中央の flat_noise と平均輝度。周辺減光補正によるノイズ増幅の指標。"""
    h, w = gray8.shape[:2]
    s = max(SKY_TILE_BLOCK * 2, int(round(min(h, w) * fraction)))
    boxes = {
        "center": ((w - s) // 2, (h - s) // 2),
        "tl": (0, 0), "tr": (w - s, 0), "bl": (0, h - s), "br": (w - s, h - s),
    }
    noise = {k: flat_noise(gray8[y:y + s, x:x + s], block=16, fraction=0.3) for k, (x, y) in boxes.items()}
    luma = {k: float(gray8[y:y + s, x:x + s].mean()) / 255.0 for k, (x, y) in boxes.items()}
    corner_noise = median_or_nan(noise[k] for k in ("tl", "tr", "bl", "br"))
    corner_luma = statistics.median(luma[k] for k in ("tl", "tr", "bl", "br"))
    return {
        "center_noise": noise["center"],
        "corner_noise": corner_noise,
        "corner_noise_ratio": corner_noise / noise["center"] if noise["center"] and math.isfinite(noise["center"]) else float("nan"),
        "center_luma": luma["center"],
        "corner_luma": corner_luma,
        "corner_luma_ratio": corner_luma / luma["center"] if luma["center"] > 0 else float("nan"),
    }


def global_shift(ref_gray: np.ndarray, tgt_gray: np.ndarray, work_edge: int = 600, blur: float = 2.0) -> tuple[float, float, float]:
    """画像全体の平行移動 (dx, dy, response) を、縮小・ぼかし後の位相相関で推定する（元画像の px 単位）。

    星や画素ノイズの寄与を落とすため σ=blur でぼかす。星の日周運動（数分で数 px）は前景が支配的なら影響しない。
    """
    h, w = ref_gray.shape[:2]
    scale = min(1.0, work_edge / max(h, w))
    size = (max(8, int(round(w * scale))), max(8, int(round(h * scale))))
    a = cv2.GaussianBlur(cv2.resize(ref_gray.astype(np.float32), size, interpolation=cv2.INTER_AREA), (0, 0), blur)
    b = cv2.GaussianBlur(cv2.resize(tgt_gray.astype(np.float32), size, interpolation=cv2.INTER_AREA), (0, 0), blur)
    dx, dy, response = roi_shift(a, b, (0, 0, size[0], size[1]), margin=0)
    return dx / scale, dy / scale, response


def shift_mask(mask: np.ndarray, dx: float, dy: float) -> np.ndarray:
    """bool マスクを (dx, dy) だけ平行移動する（はみ出しは False、最近傍）。"""
    h, w = mask.shape
    matrix = np.array([[1, 0, dx], [0, 1, dy]], dtype=np.float32)
    moved = cv2.warpAffine(mask.astype(np.uint8), matrix, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
    return moved.astype(bool)


def dilate_mask(mask: np.ndarray, radius: int = STAR_EXCLUDE_DILATE) -> np.ndarray:
    if radius <= 0 or not np.any(mask):
        return mask.astype(bool)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
    return cv2.dilate(mask.astype(np.uint8), kernel).astype(bool)


def exposure_ratio_stops(rgb_a: np.ndarray, rgb_b: np.ndarray,
                         pixel_range: tuple[int, int] = EXPOSURE_PIXEL_RANGE,
                         center_fraction: float = EXPOSURE_CENTER_FRACTION) -> dict[str, float]:
    """2 フレームの露出差（stop）: log2(リニア輝度平均 b / a)。

    両フレームとも最大チャンネルが pixel_range 内（クリップ・黒つぶれ無し）で、中央 center_fraction の
    領域にある画素だけを使う（絞りが違うと周辺減光が変わるため）。sRGB ガンマを外してから平均する。
    8bit RGB（同寸）か 0..1 float を受ける。
    """
    def to01(x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        return x / 255.0 if x.max() > 1.0 else x

    a, b = to01(rgb_a), to01(rgb_b)
    if a.shape != b.shape:
        raise ValueError("exposure_ratio_stops は同寸の画像が必要です")
    h, w = a.shape[:2]
    my, mx = int(h * (1 - center_fraction) / 2), int(w * (1 - center_fraction) / 2)
    a, b = a[my:h - my, mx:w - mx], b[my:h - my, mx:w - mx]
    lo, hi = pixel_range[0] / 255.0, pixel_range[1] / 255.0
    ok = (a.max(axis=2) >= lo) & (a.max(axis=2) <= hi) & (b.max(axis=2) >= lo) & (b.max(axis=2) <= hi)
    frac = float(ok.mean())
    if ok.sum() < 64:
        return {"stops": float("nan"), "pixel_fraction": frac}
    la = luma_of(srgb_to_linear(a))[ok].mean()
    lb = luma_of(srgb_to_linear(b))[ok].mean()
    return {"stops": float(math.log2(lb / la)) if la > 0 and lb > 0 else float("nan"), "pixel_fraction": frac}


def corner_shift_consistency(ref_gray: np.ndarray, tgt_gray: np.ndarray,
                             fraction: float = 0.12, blur: float = 2.0) -> dict[str, Any]:
    """中央と四隅の小パッチで位相相関シフトを測り、四隅が中央と何 px ずれるかを返す。

    レンズ補正の幾何がフレーム間で一致していれば、全パッチが同じ平行移動を示す（放射状の不一致が無い）。
    response が低いパッチ（星空・暗部で相関が取れない）は結果に含めず ``n_valid`` に出る。
    """
    h, w = ref_gray.shape[:2]
    s = max(64, int(round(min(h, w) * fraction)))
    a = cv2.GaussianBlur(ref_gray.astype(np.float32), (0, 0), blur)
    b = cv2.GaussianBlur(tgt_gray.astype(np.float32), (0, 0), blur)
    boxes = {
        "center": ((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2),
        "tl": (0, 0, s, s), "tr": (w - s, 0, w, s), "bl": (0, h - s, s, h), "br": (w - s, h - s, w, h),
    }
    shifts = {k: roi_shift(a, b, box, margin=0) for k, box in boxes.items()}
    out: dict[str, Any] = {f"shift_{k}": (round(v[0], 2), round(v[1], 2), round(v[2], 3)) for k, v in shifts.items()}
    cx, cy, cr = shifts["center"]
    deviations = [
        math.hypot(dx - cx, dy - cy) for k, (dx, dy, r) in shifts.items()
        if k != "center" and r >= ROI_MIN_RESPONSE
    ]
    out["n_valid"] = len(deviations) + (1 if cr >= ROI_MIN_RESPONSE else 0)
    out["max_corner_deviation_px"] = max(deviations) if deviations and cr >= ROI_MIN_RESPONSE else float("nan")
    return out


def patch_shift_deviation(reference: dict[str, Any], target: dict[str, Any]) -> float:
    """``corner_shift_consistency`` の結果 2 つで、両方で有効なパッチのシフト差の最大（px）。有効パッチが無ければ NaN。"""
    deviations = []
    for key in ("shift_center", "shift_tl", "shift_tr", "shift_bl", "shift_br"):
        a, b = reference.get(key), target.get(key)
        if not a or not b or a[2] < ROI_MIN_RESPONSE or b[2] < ROI_MIN_RESPONSE:
            continue
        deviations.append(math.hypot(a[0] - b[0], a[1] - b[1]))
    return max(deviations) if deviations else float("nan")


def channel_clip_rates(rgb8: np.ndarray) -> dict[str, float]:
    """チャンネル別（R/G/B）・いずれか・全部のクリップ（255）画素率。"""
    clipped = rgb8 >= 255
    return {
        "clip_r": float(clipped[..., 0].mean()),
        "clip_g": float(clipped[..., 1].mean()),
        "clip_b": float(clipped[..., 2].mean()),
        "clip_any": float(clipped.any(axis=2).mean()),
        "clip_all": float(clipped.all(axis=2).mean()),
    }


def clip_boundary_magenta_rate(rgb8: np.ndarray, band: int = CLIP_BAND_PX,
                               min_chroma: float = MAGENTA_MIN_CHROMA) -> dict[str, float]:
    """クリップ領域（いずれかのチャンネル 255）の外側 band px の帯で、マゼンタ寄りの画素率。

    マゼンタ寄り = R > G かつ B > G で (min(R,B) − G) ≥ min_chroma（0..1）。rawpy の境界の偽色（ADR 0002）
    と同じ見え方を数える。帯が無ければ rate は NaN。
    """
    clipped = (rgb8 >= 255).any(axis=2)
    ring = dilate_mask(clipped, band) & ~clipped
    n = int(ring.sum())
    if n == 0:
        return {"magenta_rate": float("nan"), "band_pixels": 0}
    rgb01 = rgb8.astype(np.float32) / 255.0
    r, g, b = rgb01[..., 0], rgb01[..., 1], rgb01[..., 2]
    magenta = (r > g) & (b > g) & ((np.minimum(r, b) - g) >= min_chroma)
    return {"magenta_rate": float(magenta[ring].mean()), "band_pixels": n}


def captured_at_seconds(captured_at: Any) -> float | None:
    """EXIF DateTimeOriginal（'YYYY:MM:DD HH:MM:SS'）を秒に。日付をまたぐ比較はしない前提の簡易版。"""
    if not isinstance(captured_at, str):
        return None
    match = re.match(r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})", captured_at.strip())
    if not match:
        return None
    y, mo, d, hh, mm, ss = (int(v) for v in match.groups())
    try:
        import datetime as _dt

        return _dt.datetime(y, mo, d, hh, mm, ss).timestamp()
    except ValueError:
        return None


def bracket_pairs(features_list: Iterable[dict[str, Any]], max_gap_s: float = BRACKET_MAX_GAP_S,
                  min_delta_ev: float = BRACKET_MIN_DELTA_EV) -> list[dict[str, Any]]:
    """露出違いの連続ペア候補を EXIF から拾う（同一レンズ・焦点距離・向き、時間差 ≤ max_gap_s、|ΔEV| ≥ min_delta_ev）。

    撮影順（capture_id 順）に隣り合うものだけを見る。構図が同じかは分からないので、実行時に位相相関で確かめる。
    ΔEV は EV100 の差（b が明るいほど正: ev100_a − ev100_b）。
    """
    items = sorted((f for f in features_list if f.get("capture_id")), key=lambda f: str(f["capture_id"]))
    pairs = []
    for a, b in zip(items, items[1:]):
        if (a.get("lens_model"), a.get("focal_length"), a.get("orientation")) != (b.get("lens_model"), b.get("focal_length"), b.get("orientation")):
            continue
        ta, tb = captured_at_seconds(a.get("captured_at")), captured_at_seconds(b.get("captured_at"))
        if ta is None or tb is None or not (0 <= tb - ta <= max_gap_s):
            continue
        ea, eb = a.get("ev100"), b.get("ev100")
        if not isinstance(ea, (int, float)) or not isinstance(eb, (int, float)):
            continue
        delta = float(ea - eb)
        if abs(delta) < min_delta_ev:
            continue
        pairs.append({"a": a["capture_id"], "b": b["capture_id"], "delta_ev": delta, "gap_s": tb - ta})
    return pairs


def best_box_by_mask(mask: np.ndarray, size: int, prefer: str = "max") -> tuple[int, int, int, int]:
    """mask の True が最も多い（prefer="max"）正方ボックス（size px、size/2 刻み）。"""
    h, w = mask.shape
    s = min(size, h, w)
    integral = cv2.integral(mask.astype(np.uint8))
    step = max(1, s // 2)
    best: tuple[float, int, int] | None = None
    for y in range(0, h - s + 1, step):
        for x in range(0, w - s + 1, step):
            total = float(integral[y + s, x + s] - integral[y, x + s] - integral[y + s, x] + integral[y, x])
            if best is None or (total > best[0] if prefer == "max" else total < best[0]):
                best = (total, x, y)
    if best is None:
        return (0, 0, s, s)
    _, x, y = best
    return (x, y, x + s, y + s)


def magnify(rgb: np.ndarray, factor: int = 2) -> np.ndarray:
    """最近傍で整数倍に拡大（目視用。補間で見え方を変えない）。"""
    return cv2.resize(rgb, (rgb.shape[1] * factor, rgb.shape[0] * factor), interpolation=cv2.INTER_NEAREST)


def table_md(summary: list[dict[str, Any]], group_cols: tuple[str, ...], cols: tuple[str, ...], digits: int = 3) -> str:
    """summarize_sharpness 形式の集計を Markdown 表にする（レポート転記用）。"""
    head = "| " + " | ".join([*group_cols, "n", *cols]) + " |"
    sep = "|" + "---|" * (len(group_cols) + 1 + len(cols))
    lines = [head, sep]
    for item in summary:
        cells = [str(item.get(c)) for c in group_cols] + [str(item.get("n"))]
        for c in cols:
            v = item.get(c)
            if v is None:
                cells.append("—")
            elif isinstance(v, float) and not v.is_integer():
                cells.append(f"{v:.{digits}f}")
            else:
                cells.append(f"{v:g}" if isinstance(v, float) else str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


# =============================================================================
# 夜景・星空・白点 評価モード --night（Issue #70 / #77）
# =============================================================================

#: 輝度・色 NR を 0 にする個別上書き（#79）。apple_default は ISO 3200/6400 で color NR が既に 0 なので実質は輝度 NR。
NR0: dict[str, float] = {"luminance_noise_reduction": 0.0, "color_noise_reduction": 0.0}
NIGHT_VARIANTS: tuple[tuple[str, str], ...] = (
    ("camera", "camera JPEG"),
    ("rawpy", "rawpy current"),
    ("ci_apple_v9_l1", "CI apple v9 lens ON"),
    ("ci_apple_v9_l1_nr0", "CI apple v9 lens ON NR0"),
    ("ci_apple_v9_l1_nr0_sh0", "CI apple v9 lens ON NR0 sharp0"),
    ("ci_apple_v9_l0", "CI apple v9 lens OFF"),
    ("ci_apple_v9_l0_nr0", "CI apple v9 lens OFF NR0"),
    ("ci_apple_v8_l1", "CI apple v8 lens ON"),
    ("ci_apple_v8_l1_nr0", "CI apple v8 lens ON NR0"),
    ("ci_v9_l1", "CI neutral v9 lens ON"),
    ("ci_v8_l1", "CI neutral v8 lens ON"),
)
NIGHT_LABELS = dict(NIGHT_VARIANTS)
#: CLI を呼ぶバリアント: variant -> (decoder_version, profile, lens_correction, overrides)
CI_NIGHT_REQUESTS: dict[str, tuple[str, str, bool, dict[str, float]]] = {
    "ci_apple_v9_l1": ("9", "apple_default", True, {}),
    "ci_apple_v9_l1_nr0": ("9", "apple_default", True, NR0),
    "ci_apple_v9_l1_nr0_sh0": ("9", "apple_default", True, {**NR0, "sharpness_amount": 0.0}),
    "ci_apple_v9_l0": ("9", "apple_default", False, {}),
    "ci_apple_v9_l0_nr0": ("9", "apple_default", False, NR0),
    "ci_apple_v8_l1": ("8", "apple_default", True, {}),
    "ci_apple_v8_l1_nr0": ("8", "apple_default", True, NR0),
    "ci_v9_l1": ("9", "neutral", True, {}),
    "ci_v8_l1": ("8", "neutral", True, {}),
}
#: 目視シートの組（4 枚ずつ。Read で縮小されても見えるように小さく保つ）。
NIGHT_SHEET_GROUPS: tuple[tuple[str, ...], ...] = (
    ("camera", "rawpy", "ci_apple_v9_l1", "ci_apple_v9_l1_nr0"),
    ("ci_apple_v9_l1_nr0_sh0", "ci_apple_v9_l0", "ci_apple_v8_l1", "ci_v9_l1"),
    ("ci_apple_v9_l0_nr0", "ci_apple_v8_l1_nr0", "ci_v8_l1"),
)
#: 白点の scaleFactor 依存（#77）を見るバリアントと preview 長辺。
NIGHT_SCALE_VARIANTS = ("ci_v9_l1", "ci_v9_l0", "ci_v8_l1", "ci_apple_v9_l1", "ci_apple_v9_l1_nr0", "ci_apple_v9_l0", "ci_apple_v8_l1")
NIGHT_SCALE_EDGES = (1200, 1600, 2400, 3000, 4000)
CI_SCALE_REQUESTS: dict[str, tuple[str, str, bool, dict[str, float]]] = {
    **{v: CI_NIGHT_REQUESTS[v] for v in NIGHT_SCALE_VARIANTS if v in CI_NIGHT_REQUESTS},
    "ci_v9_l0": ("9", "neutral", False, {}),
    # 白点の原因切り分け（#77）: neutral v9 に sharpness だけ / 輝度 NR だけを少し戻す、apple_default の sharpness だけ 0
    "ci_v9_l1_sh01": ("9", "neutral", True, {"sharpness_amount": 0.1}),
    "ci_v9_l1_nr024": ("9", "neutral", True, {"luminance_noise_reduction": 0.24}),
    "ci_apple_v9_l1_sh0": ("9", "apple_default", True, {"sharpness_amount": 0.0}),
}
NIGHT_KEYS = (
    "point_count", "point_ratio", "point_count_strong", "point_strong_ratio", "star_recall", "star_recall_n",
    "star_count", "star_halo", "star_color_saturation",
    "sky_mean_luma", "sky_luma_diff", "sky_p50_luma", "sky_noise", "sky_noise_ratio", "sky_chroma_noise", "sky_hf_ratio",
    "impulse_count", "impulse_per_mpx", "impulse_vs_full", "impulse_vs_full_per_mpx",
    "center_noise", "corner_noise", "corner_noise_ratio", "corner_luma_ratio",
    "flat_noise", "mean_luma", "camera_luma_diff", "camera_delta_e76", "shift_response",
)


def develop_variants(
    ctx: RunContext,
    capture_id: str,
    mode: str,
    ci_requests: dict[str, tuple[str, str, bool, dict[str, float]]],
    *,
    with_rawpy: bool = True,
    max_edge: int | None = None,
    workers: int = 4,
) -> dict[str, dict[str, Any]]:
    """rawpy と CI バリアント群を並列に現像し、variant -> 実行結果（output / error / applied 要約）を返す。"""
    from concurrent.futures import ThreadPoolExecutor

    jobs: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        if with_rawpy:
            jobs["rawpy"] = pool.submit(run_rawpy, ctx, capture_id, mode)
        for variant, (decoder, profile, lens, overrides) in ci_requests.items():
            jobs[variant] = pool.submit(
                run_coreimage, ctx, capture_id, mode, lens,
                decoder_version=decoder, profile=profile, variant=variant,
                overrides=overrides or None, max_edge=max_edge,
            )
        return {name: future.result() for name, future in jobs.items()}


def load_variant_images(engine_rows: dict[str, dict[str, Any]], target: tuple[int, int]) -> tuple[dict[str, np.ndarray], dict[str, dict[str, Any]]]:
    """現像結果を読み、target 寸法へ ``fit_to_target`` で揃える（再標本化は避ける）。"""
    images: dict[str, np.ndarray] = {}
    rows: dict[str, dict[str, Any]] = {}
    for name, row in engine_rows.items():
        row = dict(row)
        if not row.get("error"):
            src = read_rgb_upright(Path(row["output"]))
            row["native_width"], row["native_height"] = src.shape[1], src.shape[0]
            images[name], row["fit"] = fit_to_target(src, target)
        rows[name] = row
    return images, rows


def night_measure(
    images: dict[str, np.ndarray],
    exif: dict[str, Any],
    variants: Iterable[str],
    full_refs: dict[str, np.ndarray] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """camera を基準に、各バリアントの夜景・星空指標を測る（純粋。I/O 無し）。

    空タイルと星候補マスクはカメラ JPEG で 1 回だけ決め、全バリアントを同じ画素で測る（星候補の除外マスクは
    バリアントごとにセル単位の位置合わせで動かす）。戻り値は (variant -> 指標, 基準情報{sky_pixels, star_mask, ...})。

    ``full_refs``（variant -> 同じ設定の原寸レンダを INTER_AREA で同寸に縮めた RGB）があれば、
    「preview にあって原寸縮小には無い孤立点」を ``impulse_vs_full`` として数える。幾何が完全に同じ
    （同じレンズ設定）なので位置合わせの残差に影響されず、scaleFactor 経路の生む白点だけを取り出せる（#77）。
    """
    camera = images["camera"]
    cam_gray8 = cv2.cvtColor(camera, cv2.COLOR_RGB2GRAY)
    cam_gray01 = luma_of(camera.astype(np.float32) / 255.0)
    tile_mask = flat_tile_mask(cam_gray8)
    sky_px = tile_mask_to_pixels(tile_mask, cam_gray8.shape)
    cam_points = star_point_mask(cam_gray01)
    cam_strong = star_point_mask(cam_gray01, STAR_PEAK_STRONG)
    cam_exclude = dilate_mask(cam_points)
    cam_sky = sky_stats(camera, sky_px, cam_exclude)
    cam_noise = flat_noise(cam_gray8)
    cam_metrics: dict[str, dict[str, Any]] = {}
    for variant in variants:
        if variant not in images:
            continue
        rgb = images[variant]
        gray8 = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        if variant == "camera":
            dx = dy = 0.0
            response = 1.0
        else:
            dx, dy, response = global_shift(cam_gray8, gray8)
            if response < ROI_MIN_RESPONSE or math.hypot(dx, dy) > max(rgb.shape[:2]) * 0.05:
                dx = dy = 0.0
        if variant == "camera":
            exclude, strong = cam_exclude, cam_strong
        else:
            # 星候補の除外マスクはセル単位の位置合わせで動かす（lens OFF の歪曲差を吸収する）
            exclude = dilate_mask(piecewise_shift_mask(cam_exclude, cam_gray8, gray8, fallback=(dx, dy)), 1)
            strong = piecewise_shift_mask(cam_strong, cam_gray8, gray8, fallback=(dx, dy))
        sky = sky_stats(rgb, sky_px, exclude)
        row: dict[str, Any] = {"shift_dx": round(dx, 2), "shift_dy": round(dy, 2), "shift_response": response}
        row.update(star_metrics(rgb, exif))
        gray01 = luma_of(rgb.astype(np.float32) / 255.0)
        var_points = star_point_mask(gray01)
        row["point_count_strong"] = int(cv2.connectedComponents(star_point_mask(gray01, STAR_PEAK_STRONG).astype(np.uint8), connectivity=8)[0] - 1)
        recall = point_recall(strong, var_points)
        row["star_recall"] = recall["recall"]
        row["star_recall_n"] = recall["n_ref"]
        row.update(sky)
        row.update(impulse_count(gray8, exclude))
        if full_refs and variant in full_refs:
            ref_gray01 = luma_of(full_refs[variant].astype(np.float32) / 255.0)
            ref_points = dilate_mask(star_point_mask(ref_gray01), 2)
            row["impulse_vs_full"] = impulse_count(gray8, ref_points)["impulse_count"]
            row["impulse_vs_full_per_mpx"] = row["impulse_vs_full"] / (gray8.shape[0] * gray8.shape[1] / 1e6)
        row.update(corner_center_noise(gray8))
        row.update(image_metrics(rgb, None if variant == "camera" else camera))
        row["sky_luma_diff"] = row["sky_mean_luma"] - cam_sky["sky_mean_luma"]
        row["sky_noise_ratio"] = row["sky_noise"] / cam_sky["sky_noise"] if cam_sky["sky_noise"] else float("nan")
        row["flat_noise_ratio"] = row["flat_noise"] / cam_noise if cam_noise and math.isfinite(cam_noise) else float("nan")
        cam_metrics[variant] = row
    cam_pts = cam_metrics.get("camera", {}).get("point_count") or 0
    cam_strong_n = cam_metrics.get("camera", {}).get("point_count_strong") or 0
    for row in cam_metrics.values():
        row["point_ratio"] = row["point_count"] / cam_pts if cam_pts else float("nan")
        row["point_strong_ratio"] = row["point_count_strong"] / cam_strong_n if cam_strong_n else float("nan")
    reference = {
        "sky_pixels": sky_px, "star_mask": cam_points, "exclude": cam_exclude,
        "sky_fraction": float(sky_px.mean()), "camera_points": cam_pts,
    }
    return cam_metrics, reference


def night_sheets(
    ctx: RunContext,
    capture_id: str,
    mode: str,
    images: dict[str, np.ndarray],
    metrics: dict[str, dict[str, Any]],
    reference: dict[str, Any],
    caption: str,
    panel: int = 160,
    factor: int = 2,
) -> None:
    """空 / 星 / 隅の小パッチを 2 倍（最近傍）に拡大した目視シート（EXIF 無し）。4 バリアントずつ 1 枚。"""
    camera = images["camera"]
    h, w = camera.shape[:2]
    s = min(panel, h, w)
    sky_only = reference["sky_pixels"] & ~dilate_mask(reference["star_mask"], 6)
    boxes = {
        "sky": best_box_by_mask(sky_only, s),
        "stars": best_box_by_mask(reference["star_mask"], s),
        "corner": (0, 0, s, s),
    }
    for label, (x0, y0, x1, y1) in boxes.items():
        for gindex, group in enumerate(NIGHT_SHEET_GROUPS, 1):
            panels = []
            for variant in group:
                if variant not in images:
                    continue
                m = metrics.get(variant, {})
                dx, dy = int(round(m.get("shift_dx", 0.0))), int(round(m.get("shift_dy", 0.0)))
                bx0, by0, bx1, by1 = shift_box((x0, y0, x1, y1), dx, dy, w, h)
                crop = images[variant][by0:by1, bx0:bx1]
                text = (f"{NIGHT_LABELS[variant]}\nrecall {m.get('star_recall', float('nan')):.2f} imp {m.get('impulse_count')}"
                        f"/{m.get('impulse_vs_full', '-')} sky {m.get('sky_noise', float('nan')):.2f}")
                panels.append((text, magnify(crop, factor)))
            if not panels:
                continue
            sheet = labeled_row(panels, f"{caption}  {label} x{factor} ({s}px)")
            save_plain_jpeg(sheet, ctx.out / "sheets" / f"{capture_id}_{mode}_{label}_g{gindex}.jpg", quality=95)


def night_capture(
    ctx: RunContext,
    capture_id: str,
    mode: str,
    features: dict[str, Any],
    category: str,
) -> list[dict[str, Any]]:
    """1 カット × 1 モードを NIGHT_VARIANTS で現像し、指標とシートを作る。

    preview では CI 各バリアントの原寸レンダも作り（``full_refs``）、scaleFactor 由来の白点を
    ``impulse_vs_full`` で数える（原寸レンダは ``coreimage/full/`` に残り、full モードでも再利用される）。
    """
    engine_rows = develop_variants(ctx, capture_id, mode, CI_NIGHT_REQUESTS)
    camera = read_rgb_upright(source_file(ctx.source_root, capture_id, "JPG"))
    ch, cw = camera.shape[:2]
    target = fit_size(cw, ch, ctx.max_edge) if mode == "preview" else (cw, ch)
    images, rows_by_variant = load_variant_images(engine_rows, target)
    images["camera"] = resize_to(camera, target)
    rows_by_variant["camera"] = {"fit": "camera"}
    exif = {k: features.get(k) for k in ("iso", "exposure_time", "f_number")}
    full_refs = full_references(ctx, capture_id, CI_NIGHT_REQUESTS, target) if mode == "preview" else None
    metrics, reference = night_measure(images, exif, [v for v, _ in NIGHT_VARIANTS], full_refs)
    base = {
        "capture_id": capture_id, "mode": mode, "category": category, "iso": features.get("iso"),
        "exposure_time": features.get("exposure_time"), "lens_model": features.get("lens_model"),
        "focal_length": features.get("focal_length"), "width": target[0], "height": target[1],
        "sky_fraction": reference["sky_fraction"],
    }
    rows = []
    for variant, _label in NIGHT_VARIANTS:
        row: dict[str, Any] = {**base, "variant": variant, **rows_by_variant.get(variant, {})}
        if variant in metrics:
            row.update(metrics[variant])
        else:
            row.setdefault("error", "no image")
        rows.append(row)
    caption = f"{capture_id}  {mode} {target[0]}x{target[1]}  ISO {features.get('iso')}  {features.get('exposure_time')}s  {features.get('lens_model')}"
    night_sheets(ctx, capture_id, mode, images, metrics, reference, caption)
    return rows


def full_references(
    ctx: RunContext,
    capture_id: str,
    requests: dict[str, tuple[str, str, bool, dict[str, float]]],
    target: tuple[int, int],
    full_images: dict[str, np.ndarray] | None = None,
) -> dict[str, np.ndarray]:
    """CI 各バリアントの原寸レンダを INTER_AREA で target に縮めたもの（``impulse_vs_full`` の基準）。

    ``full_images`` に原寸 RGB を渡せば再レンダしない（scale 掃引で使い回す）。
    """
    if full_images is None:
        full_rows = develop_variants(ctx, capture_id, "full", requests, with_rawpy=False)
        full_images = {}
        for name, row in full_rows.items():
            if not row.get("error"):
                full_images[name] = read_rgb_upright(Path(row["output"]))
    return {name: resize_to(img, target) for name, img in full_images.items()}


def night_scale_sweep(ctx: RunContext, capture_id: str, features: dict[str, Any]) -> list[dict[str, Any]]:
    """白点ノイズの scaleFactor 依存（#77）: preview 長辺を変えて impulse_count / impulse_vs_full と空ノイズを測る。"""
    camera = read_rgb_upright(source_file(ctx.source_root, capture_id, "JPG"))
    ch, cw = camera.shape[:2]
    exif = {k: features.get(k) for k in ("iso", "exposure_time", "f_number")}
    rows = []
    requests = CI_SCALE_REQUESTS
    full_rows = develop_variants(ctx, capture_id, "full", requests, with_rawpy=False)
    full_images = {name: read_rgb_upright(Path(row["output"])) for name, row in full_rows.items() if not row.get("error")}
    for edge in NIGHT_SCALE_EDGES:
        engine_rows = develop_variants(ctx, capture_id, "preview", requests, with_rawpy=False, max_edge=edge)
        target = fit_size(cw, ch, edge)
        images, rows_by_variant = load_variant_images(engine_rows, target)
        images["camera"] = resize_to(camera, target)
        refs = full_references(ctx, capture_id, requests, target, full_images)
        metrics, _ = night_measure(images, exif, ["camera", *requests], refs)
        for variant in ("camera", *requests):
            row = {"capture_id": capture_id, "mode": "preview", "scale_edge": edge, "variant": variant,
                   "iso": features.get("iso"), **rows_by_variant.get(variant, {})}
            if variant in metrics:
                row.update({k: metrics[variant].get(k) for k in (
                    "impulse_count", "impulse_per_mpx", "impulse_vs_full", "impulse_vs_full_per_mpx",
                    "sky_noise", "sky_noise_ratio", "point_count", "star_recall", "flat_noise")})
            rows.append(row)
    return rows


def write_rows_csv(path: Path, rows: list[dict[str, Any]], first: tuple[str, ...]) -> None:
    fields = sorted({k for r in rows for k in r}, key=lambda k: (k not in first, first.index(k) if k in first else 0, k))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run_night(
    ctx: RunContext,
    selection: list[dict[str, Any]],
    features_by_id: dict[str, dict[str, Any]],
    full_ids: list[str],
    scale_ids: list[str],
) -> None:
    rows: list[dict[str, Any]] = []
    scale_rows: list[dict[str, Any]] = []
    started = time.monotonic()
    cat_by_id = {r["capture_id"]: r["category"] for r in selection}
    jobs = [(r["capture_id"], "preview") for r in selection] + [(cid, "full") for cid in full_ids]
    for index, (cid, mode) in enumerate(jobs, 1):
        ctx.say(f"[night] ({index}/{len(jobs)}) {cid} {mode}")
        try:
            new_rows = night_capture(ctx, cid, mode, features_by_id[cid], cat_by_id.get(cid, "other"))
            rows.extend(new_rows)
            for r in new_rows:
                if r.get("error"):
                    ctx.say(f"  ERROR {r['variant']}: {str(r['error'])[:300]}")
        except Exception as error:  # 1 カットの失敗で全体を止めない
            rows.append({"capture_id": cid, "mode": mode, "variant": "harness", "error": f"{type(error).__name__}: {error}"})
            ctx.say(f"  ERROR {type(error).__name__}: {error}")
        write_rows_csv(ctx.out / "night_metrics.csv", rows, ("capture_id", "mode", "variant"))
    for index, cid in enumerate(scale_ids, 1):
        ctx.say(f"[night] scale sweep ({index}/{len(scale_ids)}) {cid}")
        try:
            scale_rows.extend(night_scale_sweep(ctx, cid, features_by_id[cid]))
        except Exception as error:
            scale_rows.append({"capture_id": cid, "variant": "harness", "error": f"{type(error).__name__}: {error}"})
            ctx.say(f"  ERROR {type(error).__name__}: {error}")
        write_rows_csv(ctx.out / "night_scale_metrics.csv", scale_rows, ("capture_id", "scale_edge", "variant"))

    by_mode = summarize_sharpness(rows, NIGHT_KEYS, ("mode", "variant"))
    by_iso = summarize_sharpness(rows, NIGHT_KEYS, ("mode", "iso", "variant"))
    scale_keys = ("impulse_count", "impulse_per_mpx", "impulse_vs_full", "impulse_vs_full_per_mpx", "sky_noise", "sky_noise_ratio", "point_count", "flat_noise")
    by_scale = summarize_sharpness(scale_rows, scale_keys, ("scale_edge", "variant")) if scale_rows else []
    errors = [r for r in rows + scale_rows if r.get("error")]
    summary = {
        "out": str(ctx.out),
        "variants": dict(NIGHT_VARIANTS),
        "ci_requests": {k: {"decoder_version": v[0], "profile": v[1], "lens_correction": v[2], "overrides": v[3]} for k, v in CI_NIGHT_REQUESTS.items()},
        "selection_count": len(selection),
        "full_ids": full_ids,
        "scale_ids": scale_ids,
        "scale_edges": list(NIGHT_SCALE_EDGES),
        "elapsed_total_s": time.monotonic() - started,
        "errors": errors,
        "by_mode": by_mode,
        "by_mode_iso": by_iso,
        "by_scale": by_scale,
    }
    (ctx.out / "night_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    star_cols = ("point_count", "point_ratio", "point_count_strong", "point_strong_ratio", "star_recall",
                 "star_count", "star_halo", "star_color_saturation", "impulse_count", "impulse_per_mpx", "impulse_vs_full")
    sky_cols = ("sky_mean_luma", "sky_luma_diff", "sky_noise", "sky_noise_ratio", "sky_chroma_noise", "sky_hf_ratio",
                "corner_noise_ratio", "corner_luma_ratio", "camera_delta_e76")
    md = "## stars / impulses by mode\n\n" + table_md(by_mode, ("mode", "variant"), star_cols)
    md += "\n## sky by mode\n\n" + table_md(by_mode, ("mode", "variant"), sky_cols)
    md += "\n## stars / impulses by mode x ISO\n\n" + table_md(by_iso, ("mode", "iso", "variant"), star_cols)
    md += "\n## sky by mode x ISO\n\n" + table_md(by_iso, ("mode", "iso", "variant"), sky_cols)
    if by_scale:
        md += "\n## impulse noise by preview edge (scale sweep)\n\n" + table_md(by_scale, ("scale_edge", "variant"), scale_keys)
    (ctx.out / "night_tables.md").write_text(md, encoding="utf-8")
    (ctx.out / "run.log").write_text("\n".join(ctx.log) + "\n", encoding="utf-8")
    ctx.say(f"[night] 完了: {ctx.out}（エラー {len(errors)} 件）")


# =============================================================================
# ブラケット（露出差の保存・幾何一致・Mertens 合成）--bracket（設計書 §4.1）
# =============================================================================


def read_tiff16_rgb(path: Path) -> np.ndarray:
    """CLI の 16bit TIFF を 0..1 float32 RGB で読む（cv2。PIL は 16bit RGB を扱えない）。"""
    bgr = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if bgr is None:
        raise OSError(f"TIFF を読めません: {path}")
    if bgr.ndim == 3 and bgr.shape[2] >= 3:
        rgb = cv2.cvtColor(bgr[..., :3], cv2.COLOR_BGR2RGB)
    else:
        rgb = np.repeat(bgr[..., None], 3, axis=2)
    scale = 65535.0 if rgb.dtype == np.uint16 else 255.0
    return rgb.astype(np.float32) / scale


def rawpy_bracket_frame(source_root: Path, capture_id: str, max_edge: int) -> np.ndarray:
    """``bracket._decode_frame``（no_auto_bright、露出差を保つ）で 1 フレームを 0..1 float32 に。長辺 max_edge へ縮小。"""
    from local_raw import bracket, engine

    adjustments = engine.resolve_adjustments(engine.DEFAULT_PRESET, None)
    frame = bracket._decode_frame(source_file(source_root, capture_id, "ARW"), adjustments, preview=True)
    h, w = frame.shape[:2]
    size = fit_size(w, h, max_edge)
    if size != (w, h):
        frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    return np.clip(frame, 0.0, 1.0).astype(np.float32)


def mertens_merge(frames: list[np.ndarray]) -> tuple[np.ndarray, bool]:
    """``bracket._align_frames`` + ``_merge_frames``（アプリと同じ OpenCV AlignMTB / MergeMertens）。"""
    from local_raw import bracket

    aligned, ok = bracket._align_frames(frames)
    return bracket._merge_frames(aligned), ok


def to8(rgb01: np.ndarray) -> np.ndarray:
    return np.rint(np.clip(rgb01, 0.0, 1.0) * 255.0).astype(np.uint8)


def bracket_pair_eval(ctx: RunContext, pair: dict[str, Any], features_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """ペア (a, b) を camera / rawpy（no_auto_bright）/ CI apple v9 lens ON（sRGB tiff16）で現像し、
    露出比・幾何一致・Mertens 合成（目視シート）を記録する。"""
    a, b = pair["a"], pair["b"]
    fa, fb = features_by_id[a], features_by_id[b]
    edge = ctx.max_edge
    out_rows: list[dict[str, Any]] = []
    frames: dict[str, list[np.ndarray]] = {}
    cam = [read_rgb_upright(source_file(ctx.source_root, cid, "JPG")) for cid in (a, b)]
    ch, cw = cam[0].shape[:2]
    target = fit_size(cw, ch, edge)
    frames["camera"] = [resize_to(c, target).astype(np.float32) / 255.0 for c in cam]
    frames["rawpy"] = [rawpy_bracket_frame(ctx.source_root, cid, edge) for cid in (a, b)]
    ci_paths = []
    for cid in (a, b):
        row = run_coreimage(ctx, cid, "preview", True, decoder_version="9", profile="apple_default",
                            variant="ci_apple_v9_l1_srgb", output_format="tiff16", output_color_space="srgb")
        if row.get("error"):
            raise RuntimeError(row["error"])
        ci_paths.append(Path(row["output"]))
    frames["ci_apple_v9_l1"] = [fit_to_target(read_tiff16_rgb(p), target)[0] for p in ci_paths]
    delta_ev = float(pair["delta_ev"])
    base = {"pair": f"{a}-{b}", "a": a, "b": b, "delta_ev_exif": delta_ev, "gap_s": pair.get("gap_s"),
            "iso_a": fa.get("iso"), "iso_b": fb.get("iso"), "exposure_a": fa.get("exposure_time"), "exposure_b": fb.get("exposure_time"),
            "f_a": fa.get("f_number"), "f_b": fb.get("f_number"), "width": target[0], "height": target[1]}
    merged: dict[str, np.ndarray] = {}
    geometries: dict[str, dict[str, Any]] = {}
    for name, (fr_a, fr_b) in frames.items():
        ratio = exposure_ratio_stops(fr_a, fr_b)
        ga = cv2.cvtColor(to8(fr_a), cv2.COLOR_RGB2GRAY)
        gb = cv2.cvtColor(to8(fr_b), cv2.COLOR_RGB2GRAY)
        geometry = corner_shift_consistency(ga, gb)
        geometries[name] = geometry
        row = {**base, "variant": name, "stops_measured": ratio["stops"], "stops_error": ratio["stops"] - delta_ev,
               "pixel_fraction": ratio["pixel_fraction"], **geometry}
        # フレーム間の実際の動き（三脚の揺れ・星の日周運動）はカメラ JPEG にも同じく出るので、
        # 「CI の各パッチのシフトがカメラのそれと何 px 違うか」をレンズ補正の幾何一致の指標にする。
        row["max_patch_deviation_from_camera_px"] = patch_shift_deviation(geometries["camera"], geometry)
        if name != "camera":
            merged_rgb, aligned = mertens_merge([fr_a, fr_b])
            merged[name] = to8(merged_rgb)
            row["mertens_aligned"] = aligned
            row.update({f"merged_{k}": v for k, v in image_metrics(merged[name]).items()})
            save_plain_jpeg(merged[name], ctx.out / "bracket" / f"{a}-{b}_{name}_mertens.jpg", quality=92)
        out_rows.append(row)
    # 目視シート: 各フレームと合成（縮小）、中央・隅の等倍クロップ
    caption = f"{a} + {b}  dEV {delta_ev:+.2f}  ISO {fa.get('iso')}/{fb.get('iso')}  {fa.get('exposure_time')}s/{fb.get('exposure_time')}s  f{fa.get('f_number')}/{fb.get('f_number')}"
    small = fit_size(target[0], target[1], 480)
    for name in ("rawpy", "ci_apple_v9_l1"):
        panels = [(f"{name} {a}", resize_to(to8(frames[name][0]), small)), (f"{name} {b}", resize_to(to8(frames[name][1]), small)),
                  (f"{name} Mertens", resize_to(merged[name], small))]
        save_plain_jpeg(labeled_row(panels, caption), ctx.out / "sheets" / f"bracket_{a}-{b}_{name}_overview.jpg", quality=92)
    s = 200
    for label, box in (("center", ((target[0] - s) // 2, (target[1] - s) // 2, (target[0] + s) // 2, (target[1] + s) // 2)),
                       ("corner", (0, 0, s, s))):
        x0, y0, x1, y1 = box
        panels = [(f"{n} Mertens {label}", magnify(merged[n][y0:y1, x0:x1], 2)) for n in ("rawpy", "ci_apple_v9_l1")]
        panels.insert(0, (f"camera {b} {label}", magnify(to8(frames['camera'][1])[y0:y1, x0:x1], 2)))
        save_plain_jpeg(labeled_row(panels, f"{caption}  {label} x2"), ctx.out / "sheets" / f"bracket_{a}-{b}_{label}.jpg", quality=95)
    return out_rows


def run_bracket(ctx: RunContext, pairs: list[dict[str, Any]], features_by_id: dict[str, dict[str, Any]]) -> None:
    rows: list[dict[str, Any]] = []
    (ctx.out / "bracket").mkdir(parents=True, exist_ok=True)
    for index, pair in enumerate(pairs, 1):
        ctx.say(f"[bracket] ({index}/{len(pairs)}) {pair['a']} + {pair['b']} dEV {pair['delta_ev']:+.2f}")
        try:
            rows.extend(bracket_pair_eval(ctx, pair, features_by_id))
        except Exception as error:
            rows.append({"pair": f"{pair['a']}-{pair['b']}", "variant": "harness", "error": f"{type(error).__name__}: {error}"})
            ctx.say(f"  ERROR {type(error).__name__}: {error}")
    write_rows_csv(ctx.out / "bracket_metrics.csv", rows, ("pair", "variant"))
    cols = ("delta_ev_exif", "stops_measured", "stops_error", "pixel_fraction", "max_corner_deviation_px", "n_valid",
            "max_patch_deviation_from_camera_px", "merged_clip_high_any", "merged_mean_luma")
    md = "## bracket pairs\n\n" + table_md(
        [{**r, "n": 1} for r in rows if not r.get("error")], ("pair", "variant"), cols)
    (ctx.out / "bracket_tables.md").write_text(md, encoding="utf-8")
    (ctx.out / "bracket_summary.json").write_text(json.dumps({"pairs": pairs, "rows": rows}, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    ctx.say(f"[bracket] 完了: {ctx.out}（{len(rows)} 行）")


# =============================================================================
# 逆光・ハイライト 評価モード --highlight（Issue #71、設計書 §4.2）
# =============================================================================

HIGHLIGHT_VARIANTS: tuple[tuple[str, str], ...] = (
    ("camera", "camera JPEG"),
    ("rawpy", "rawpy current"),
    ("ci_v9_l1", "CI neutral v9 lens ON"),
    ("ci_apple_v9_l1", "CI apple v9 lens ON"),
    ("ci_apple_edr05", "CI apple EDR 0.5"),
    ("ci_apple_edr10", "CI apple EDR 1.0"),
    ("ci_apple_edr20", "CI apple EDR 2.0"),
    ("ci_apple_boost06", "CI apple boost 0.6"),
    ("ci_apple_boost08", "CI apple boost 0.8"),
    ("ci_apple_bshadow05", "CI apple boostShadow 0.5"),
    ("ci_apple_bshadow13", "CI apple boostShadow 1.3"),
    ("ci_apple_ev-07", "CI apple exposure -0.7EV"),
    ("ci_apple_ev-10", "CI apple exposure -1.0EV"),
    ("ci_apple_ev-15", "CI apple exposure -1.5EV"),
    ("ci_apple_boost06_ev-07", "CI apple boost 0.6 + -0.7EV"),
)
HIGHLIGHT_LABELS = dict(HIGHLIGHT_VARIANTS)
CI_HIGHLIGHT_REQUESTS: dict[str, tuple[str, str, bool, dict[str, float]]] = {
    "ci_v9_l1": ("9", "neutral", True, {}),
    "ci_apple_v9_l1": ("9", "apple_default", True, {}),
    "ci_apple_edr05": ("9", "apple_default", True, {"extended_dynamic_range": 0.5}),
    "ci_apple_edr10": ("9", "apple_default", True, {"extended_dynamic_range": 1.0}),
    "ci_apple_edr20": ("9", "apple_default", True, {"extended_dynamic_range": 2.0}),
    "ci_apple_boost06": ("9", "apple_default", True, {"boost_amount": 0.6}),
    "ci_apple_boost08": ("9", "apple_default", True, {"boost_amount": 0.8}),
    "ci_apple_bshadow05": ("9", "apple_default", True, {"boost_shadow_amount": 0.5}),
    "ci_apple_bshadow13": ("9", "apple_default", True, {"boost_shadow_amount": 1.3}),
    "ci_apple_ev-07": ("9", "apple_default", True, {"exposure_ev": -0.7}),
    "ci_apple_ev-10": ("9", "apple_default", True, {"exposure_ev": -1.0}),
    "ci_apple_ev-15": ("9", "apple_default", True, {"exposure_ev": -1.5}),
    "ci_apple_boost06_ev-07": ("9", "apple_default", True, {"boost_amount": 0.6, "exposure_ev": -0.7}),
}
HIGHLIGHT_SHEET_GROUPS: tuple[tuple[str, ...], ...] = (
    ("camera", "rawpy", "ci_v9_l1", "ci_apple_v9_l1"),
    ("ci_apple_edr05", "ci_apple_edr10", "ci_apple_edr20", "ci_apple_ev-07"),
    ("ci_apple_boost06", "ci_apple_boost08", "ci_apple_bshadow05", "ci_apple_bshadow13"),
    ("rawpy", "ci_apple_ev-10", "ci_apple_ev-15", "ci_apple_boost06_ev-07"),
)
HIGHLIGHT_KEYS = (
    "clip_r", "clip_g", "clip_b", "clip_any", "clip_all", "magenta_rate", "band_pixels",
    "clip_low", "mean_luma", "p50_luma", "p99_luma", "camera_luma_diff", "camera_delta_e76", "mean_saturation",
)


def highlight_measure(images: dict[str, np.ndarray], variants: Iterable[str]) -> dict[str, dict[str, Any]]:
    """各バリアントのクリップ・境界偽色・トーン指標（純粋）。"""
    camera = images["camera"]
    out: dict[str, dict[str, Any]] = {}
    for variant in variants:
        if variant not in images:
            continue
        rgb = images[variant]
        row: dict[str, Any] = {}
        row.update(channel_clip_rates(rgb))
        row.update(clip_boundary_magenta_rate(rgb))
        row.update(image_metrics(rgb, None if variant == "camera" else camera))
        out[variant] = row
    return out


def highlight_capture(ctx: RunContext, capture_id: str, mode: str, features: dict[str, Any], category: str) -> list[dict[str, Any]]:
    engine_rows = develop_variants(ctx, capture_id, mode, CI_HIGHLIGHT_REQUESTS)
    camera = read_rgb_upright(source_file(ctx.source_root, capture_id, "JPG"))
    ch, cw = camera.shape[:2]
    target = fit_size(cw, ch, ctx.max_edge) if mode == "preview" else (cw, ch)
    images, rows_by_variant = load_variant_images(engine_rows, target)
    images["camera"] = resize_to(camera, target)
    rows_by_variant["camera"] = {"fit": "camera"}
    metrics = highlight_measure(images, [v for v, _ in HIGHLIGHT_VARIANTS])
    base = {"capture_id": capture_id, "mode": mode, "category": category, "iso": features.get("iso"),
            "lens_model": features.get("lens_model"), "width": target[0], "height": target[1]}
    rows = []
    for variant, _label in HIGHLIGHT_VARIANTS:
        row: dict[str, Any] = {**base, "variant": variant, **rows_by_variant.get(variant, {})}
        if variant in metrics:
            row.update(metrics[variant])
        else:
            row.setdefault("error", "no image")
        rows.append(row)
    # シート: カメラ JPEG のクリップ領域が最も多い 200px 箱（境界を含む）を 2 倍で並べる
    clipped = (images["camera"] >= 255).any(axis=2)
    ring = dilate_mask(clipped, CLIP_BAND_PX) & ~clipped
    s = min(200, target[0], target[1])
    box = best_box_by_mask(ring if ring.any() else clipped, s)
    x0, y0, x1, y1 = box
    caption = f"{capture_id}  {mode} {target[0]}x{target[1]}  ISO {features.get('iso')}  clip-boundary x2 ({s}px)"
    for gindex, group in enumerate(HIGHLIGHT_SHEET_GROUPS, 1):
        panels = []
        for variant in group:
            if variant not in images:
                continue
            m = metrics.get(variant, {})
            text = f"{HIGHLIGHT_LABELS[variant]}\nclip {m.get('clip_any', 0) * 100:.2f}% magenta {m.get('magenta_rate', float('nan')):.2f}"
            panels.append((text, magnify(images[variant][y0:y1, x0:x1], 2)))
        if panels:
            save_plain_jpeg(labeled_row(panels, caption), ctx.out / "sheets" / f"{capture_id}_{mode}_clip_g{gindex}.jpg", quality=95)
    small = fit_size(target[0], target[1], 400)
    panels = [(HIGHLIGHT_LABELS[v], resize_to(images[v], small)) for v in ("camera", "rawpy", "ci_apple_v9_l1", "ci_apple_edr10") if v in images]
    if panels:
        save_plain_jpeg(labeled_row(panels, f"{capture_id} {mode} overview"), ctx.out / "sheets" / f"{capture_id}_{mode}_overview.jpg", quality=90)
    return rows


def run_highlight(ctx: RunContext, selection: list[dict[str, Any]], features_by_id: dict[str, dict[str, Any]], full_ids: list[str]) -> None:
    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    cat_by_id = {r["capture_id"]: r["category"] for r in selection}
    jobs = [(r["capture_id"], "preview") for r in selection] + [(cid, "full") for cid in full_ids]
    for index, (cid, mode) in enumerate(jobs, 1):
        ctx.say(f"[highlight] ({index}/{len(jobs)}) {cid} {mode}")
        try:
            new_rows = highlight_capture(ctx, cid, mode, features_by_id[cid], cat_by_id.get(cid, "other"))
            rows.extend(new_rows)
            for r in new_rows:
                if r.get("error"):
                    ctx.say(f"  ERROR {r['variant']}: {str(r['error'])[:300]}")
        except Exception as error:
            rows.append({"capture_id": cid, "mode": mode, "variant": "harness", "error": f"{type(error).__name__}: {error}"})
            ctx.say(f"  ERROR {type(error).__name__}: {error}")
        write_rows_csv(ctx.out / "highlight_metrics.csv", rows, ("capture_id", "mode", "variant"))
    by_mode = summarize_sharpness(rows, HIGHLIGHT_KEYS, ("mode", "variant"))
    errors = [r for r in rows if r.get("error")]
    summary = {
        "out": str(ctx.out), "variants": dict(HIGHLIGHT_VARIANTS),
        "ci_requests": {k: {"decoder_version": v[0], "profile": v[1], "lens_correction": v[2], "overrides": v[3]} for k, v in CI_HIGHLIGHT_REQUESTS.items()},
        "selection_count": len(selection), "full_ids": full_ids, "elapsed_total_s": time.monotonic() - started,
        "errors": errors, "by_mode": by_mode,
    }
    (ctx.out / "highlight_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    cols = ("clip_any", "clip_r", "clip_g", "clip_b", "clip_all", "magenta_rate", "clip_low", "mean_luma", "p99_luma", "camera_luma_diff", "camera_delta_e76")
    (ctx.out / "highlight_tables.md").write_text("## by mode\n\n" + table_md(by_mode, ("mode", "variant"), cols, digits=4), encoding="utf-8")
    (ctx.out / "run.log").write_text("\n".join(ctx.log) + "\n", encoding="utf-8")
    ctx.say(f"[highlight] 完了: {ctx.out}（エラー {len(errors)} 件）")



def environment_info(ctx: RunContext, probe: Path | None) -> dict[str, Any]:
    from local_raw import engine

    def cli_json(*args: str) -> Any:
        try:
            proc = subprocess.run([str(ctx.cli), *args], capture_output=True, text=True, timeout=60)
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception as error:  # noqa: BLE001
            return {"error": str(error)}

    cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    mem = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip()
    import PIL
    import rawpy

    return {
        "machine": {"cpu": cpu, "memory_bytes": int(mem) if mem.isdigit() else mem, "platform": platform.platform()},
        "macos": platform.mac_ver()[0],
        "python": platform.python_version(),
        "rawpy": rawpy.__version__,
        "libraw": getattr(rawpy, "libraw_version", None),
        "pillow": PIL.__version__,
        "opencv": cv2.__version__,
        "engine_version": engine.ENGINE_VERSION,
        "rawpy_preset": engine.DEFAULT_PRESET,
        "coreimage_cli": str(ctx.cli),
        "coreimage_version": cli_json("--version"),
        "coreimage_capabilities": cli_json("--capabilities", *( [str(probe)] if probe else [] )),
        "coreimage_decoder_version": CI_DECODER_VERSION,
        "max_edge": ctx.max_edge,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="rawpy と Core Image CLI を同じ ARW 群で比較します。")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--captures", nargs="*", help="比較する撮影ID（例 DSC04430）")
    parser.add_argument("--select", choices=["auto"], help="EXIF からカットを自動選定する")
    parser.add_argument("--out", type=Path, help="出力先（既定 work/engine-comparison/<timestamp>/）")
    parser.add_argument("--max-edge", type=int, default=2400)
    parser.add_argument("--full", action="store_true", help="フル解像度でも計測する")
    parser.add_argument("--full-limit", type=int, default=5, help="--full で回すカット数（0 で全件）")
    parser.add_argument("--full-ids", nargs="*", help="--full で回すカットを明示する")
    parser.add_argument("--no-lens", action="store_true", help="Core Image + lens を省く")
    parser.add_argument(
        "--sharpness", action="store_true",
        help="解像感（ピント甘さ）評価モード: 10 バリアントのシャープネス/エッジ幅/ノイズと等倍クロップを出す",
    )
    parser.add_argument(
        "--night", action="store_true",
        help="夜景・星空・長秒の評価モード（#70 / #77）: 星の数・空のノイズ・白点・四隅ノイズと 2 倍拡大シートを出す",
    )
    parser.add_argument("--night-scales", nargs="*", help="--night で白点の scaleFactor 掃引を回す撮影 ID（#77）")
    parser.add_argument(
        "--bracket", action="store_true",
        help="露出違いペアの評価（露出差の保存・レンズ補正の幾何一致・Mertens 合成）。ペアは EXIF から自動検出",
    )
    parser.add_argument("--bracket-pairs", nargs="*", help="--bracket のペアを明示する（例 DSC07415,DSC07416）")
    parser.add_argument(
        "--highlight", action="store_true",
        help="逆光・ハイライトの評価モード（#71）: チャンネル別クリップ率・境界のマゼンタ率・EDR / boost 掃引",
    )
    parser.add_argument("--dry-run", action="store_true", help="選定だけ行って終了する")
    parser.add_argument(
        "--features-cache", type=Path,
        help="選定用特徴量の JSON キャッシュ（あれば読み、無ければ書く。work/ 配下を推奨）",
    )
    parser.add_argument("--cli", type=Path, default=DEFAULT_CLI)
    # 内部用（別プロセスのワーカー）
    parser.add_argument("--worker-rawpy", help=argparse.SUPPRESS)
    parser.add_argument("--worker-noop", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-mode", choices=["preview", "full"], default="preview", help=argparse.SUPPRESS)
    parser.add_argument("--worker-out", help=argparse.SUPPRESS)
    parser.add_argument("--worker-nosharp", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.worker_noop:
        worker_noop(args)
        return
    if args.worker_rawpy:
        worker_rawpy(args)
        return

    source_root = args.source_root.expanduser().resolve()
    if not source_root.is_dir():
        raise SystemExit(f"現像元フォルダがありません: {source_root}")
    if not (64 <= args.max_edge <= 6048):
        raise SystemExit("--max-edge は 64〜6048 で指定してください")
    if not args.captures and not args.select:
        raise SystemExit("--captures か --select auto のどちらかを指定してください")

    available = paired_ids(source_root)
    out = (args.out or DEFAULT_OUT_ROOT / time.strftime("%Y%m%d-%H%M%S")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    ctx = RunContext(source_root=source_root, out=out, cli=args.cli.resolve(), max_edge=args.max_edge)

    if args.select == "auto":
        cache = args.features_cache
        if cache and cache.is_file():
            all_features = json.loads(cache.read_text(encoding="utf-8"))
            all_features = [f for f in all_features if f.get("capture_id") in set(available)]
            ctx.say(f"選定用特徴量をキャッシュから読みました: {cache}（{len(all_features)} カット）")
        else:
            ctx.say(f"選定用に {len(available)} カットの EXIF を読みます")
            all_features = [read_features(source_root, cid) for cid in available]
            if cache:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(all_features, ensure_ascii=False) + "\n", encoding="utf-8")
        selection = select_auto(all_features)
        features_by_id = {f["capture_id"]: f for f in all_features}
        cat_counts: dict[str, int] = {}
        for f in all_features:
            for c in classify_scene(f):
                cat_counts[c] = cat_counts.get(c, 0) + 1
        selection_payload = {
            "source_count": len(available),
            "category_counts_in_source": cat_counts,
            "thresholds": {
                "star_min_exposure_s": STAR_MIN_EXPOSURE_S, "star_max_ev100": STAR_MAX_EV100,
                "dark_max_ev100": DARK_MAX_EV100, "night_hours": sorted(NIGHT_HOURS),
                "high_iso_min": HIGH_ISO_MIN, "mid_iso_min": MID_ISO_MIN, "low_iso_max": LOW_ISO_MAX,
                "backlit_min_bright": BACKLIT_MIN_BRIGHT, "backlit_min_dark": BACKLIT_MIN_DARK,
            },
            "quotas": dict(DEFAULT_QUOTAS),
            # 撮影場所・GPS は含めない（EXIF 要約は GPS IFD を読まない）
            "selection": [
                {**row, **{k: features_by_id[row["capture_id"]].get(k) for k in (
                    "iso", "exposure_time", "f_number", "focal_length", "lens_model",
                    "orientation", "ev100", "hour", "bright_frac", "dark_frac")}}
                for row in selection
            ],
        }
    else:
        ids = [validate_capture_id(c) for c in args.captures]
        missing = sorted(set(ids) - set(available))
        if missing:
            raise SystemExit(f"ARW/JPG のペアが見つからない撮影IDです: {', '.join(missing)}")
        features_by_id = {cid: read_features(source_root, cid) for cid in ids}
        selection = [
            {"capture_id": cid, "category": (classify_scene(features_by_id[cid]) or ["other"])[0],
             "categories": classify_scene(features_by_id[cid]), "reason": "--captures で指定"}
            for cid in ids
        ]
        selection_payload = {"selection": selection}
    (out / "selection.json").write_text(json.dumps(selection_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ctx.say(f"選定 {len(selection)} カット: " + ", ".join(f"{r['capture_id']}({r['category']})" for r in selection))
    if args.dry_run:
        return

    if not ctx.cli.is_file():
        raise SystemExit(f"Core Image CLI がありません（npm run build:coreimage）: {ctx.cli}")

    if args.sharpness or args.night or args.highlight:
        full_ids = [validate_capture_id(c) for c in (args.full_ids or [])] if args.full else []
        if args.full and not args.full_ids:
            full_ids = full_subset(selection, args.full_limit if args.full_limit > 0 else len(selection))
        unknown = sorted(set(full_ids) - set(features_by_id))
        if unknown:
            raise SystemExit(f"--full-ids は選定済みのカットから指定してください: {', '.join(unknown)}")
        if args.sharpness:
            run_sharpness(ctx, selection, features_by_id, full_ids)
        if args.night:
            scale_ids = [validate_capture_id(c) for c in (args.night_scales or [])]
            unknown = sorted(set(scale_ids) - set(features_by_id))
            if unknown:
                raise SystemExit(f"--night-scales は選定済みのカットから指定してください: {', '.join(unknown)}")
            run_night(ctx, selection, features_by_id, full_ids, scale_ids)
        if args.highlight:
            run_highlight(ctx, selection, features_by_id, full_ids)
        if not args.bracket:
            return
    if args.bracket:
        if args.bracket_pairs:
            pairs = []
            for spec in args.bracket_pairs:
                a, _, b = spec.partition(",")
                a, b = validate_capture_id(a), validate_capture_id(b)
                for cid in (a, b):
                    if cid not in features_by_id:
                        features_by_id[cid] = read_features(source_root, cid)
                ea, eb = features_by_id[a].get("ev100"), features_by_id[b].get("ev100")
                delta = float(ea - eb) if isinstance(ea, (int, float)) and isinstance(eb, (int, float)) else float("nan")
                ta, tb = captured_at_seconds(features_by_id[a].get("captured_at")), captured_at_seconds(features_by_id[b].get("captured_at"))
                pairs.append({"a": a, "b": b, "delta_ev": delta, "gap_s": (tb - ta) if ta is not None and tb is not None else None})
        else:
            pairs = bracket_pairs(features_by_id.values())
        ctx.say("[bracket] ペア: " + (", ".join(f"{p['a']}+{p['b']}({p['delta_ev']:+.2f}EV)" for p in pairs) or "無し"))
        if pairs:
            run_bracket(ctx, pairs, features_by_id)
        else:
            (ctx.out / "bracket_summary.json").write_text(json.dumps({"pairs": [], "rows": []}, indent=2) + "\n", encoding="utf-8")
        return

    env = environment_info(ctx, source_file(source_root, selection[0]["capture_id"], "ARW"))
    # 基準値: rawpy ワーカーの常駐コスト、Core Image の初回起動（計測から除外するウォームアップ）
    noop = run_timed([sys.executable, str(Path(__file__).resolve()), "--source-root", str(source_root), "--worker-noop"])
    env["rawpy_worker_baseline"] = {k: noop.get(k) for k in ("wall_s", "maxrss_bytes", "peak_footprint_bytes")}
    warm = run_coreimage(ctx, selection[0]["capture_id"], "preview", lens=False)
    env["coreimage_warmup"] = {k: warm.get(k) for k in ("wall_s", "engine_elapsed_s", "maxrss_bytes", "error")}

    full_ids: list[str] = []
    if args.full:
        if args.full_ids:
            full_ids = [validate_capture_id(c) for c in args.full_ids]
        else:
            full_ids = full_subset(selection, args.full_limit if args.full_limit > 0 else len(selection))

    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    jobs = [(r["capture_id"], "preview", r["category"]) for r in selection]
    cat_by_id = {r["capture_id"]: r["category"] for r in selection}
    jobs += [(cid, "full", cat_by_id.get(cid, "other")) for cid in full_ids]
    for index, (cid, mode, category) in enumerate(jobs, 1):
        ctx.say(f"({index}/{len(jobs)}) {cid} {mode} [{category}]")
        try:
            rows.extend(compare_capture(ctx, cid, mode, features_by_id[cid], category, not args.no_lens))
        except Exception as error:  # 1 カットの失敗で全体を止めない
            rows.append({"capture_id": cid, "mode": mode, "engine": "harness", "error": f"{type(error).__name__}: {error}"})
            ctx.say(f"  ERROR {type(error).__name__}: {error}")

    fields = sorted({k for r in rows for k in r}, key=lambda k: (k not in ("capture_id", "mode", "engine", "category"), k))
    with (out / "metrics.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    by_category: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("mode") == "preview":
            by_category.setdefault(str(row.get("category")), []).append(row)
    summary = {
        "out": str(out),
        "source_root": str(source_root),
        "environment": env,
        "selection_count": len(selection),
        "full_ids": full_ids,
        "elapsed_total_s": time.monotonic() - started,
        "errors": [r for r in rows if r.get("error")],
        "summary": summarize(rows, METRIC_KEYS),
        "summary_by_category": {c: summarize(rs, METRIC_KEYS) for c, rs in sorted(by_category.items())},
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    (out / "run.log").write_text("\n".join(ctx.log) + "\n", encoding="utf-8")
    ctx.say(f"完了: {out}（エラー {len(summary['errors'])} 件）")


if __name__ == "__main__":
    main()
