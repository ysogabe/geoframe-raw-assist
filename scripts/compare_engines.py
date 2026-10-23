#!/usr/bin/env python3
"""Core Image CLI の出力を同じ ARW 群で比較するハーネス（Issue #65 PR3 / #57 / #90）。

各カットについて次を並べ、指標・比較シートを ``work/engine-comparison/<timestamp>/``
（gitignore 対象）へ出力する。

- camera   : 同名のカメラ JPEG（向きを正立に直してリサイズのみ）
- coreimage: ``geoframe-ci-develop``（profile neutral, decoder_version "9", lens_correction false）
- coreimage_lens: 同上 + lens_correction true（レンズ補正差の把握用）
- rawpy    : 旧エンジン ``local_raw.engine.develop_raw``。**``--legacy-rawpy`` の明示 opt-in だけ**
  （アーカイブ経路。``local_raw/`` と requirements-raw.txt の環境が前提）

既定経路はアプリ本体（``local_raw``）にも rawpy / lensfunpy にも依存しない（``scripts/evaluation/``）。
旧エンジンの比較列は、凍結済みの基準画像と ``--frozen-manifest`` で比べる（docs/evaluation-paths.md）。
``--frozen-manifest`` は凍結画像のハッシュ・寸法を照合し（欠落・改変は失敗）、現在の CI で描画して同寸で比べる。

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
``--adjustments`` は Adjustments の評価モード（Issue #72 / #73、設計書 coreimage-preview-adjustments.md §3.7）。
CLI 0.3.0 の ``adjustments`` を ``ADJ_SWEEP_VARIANTS`` で 1 パラメータずつ掃引し、rawpy（``--worker-adjustments`` で
同じ値を渡す）と効く方向・大きさを比べる。後段（shadow_lift / brightness / contrast / saturation / USM）は
tiff16 sRGB 同士で Python の後段をオラクルにして ΔE76 を測り（``ADJ_ORACLE_VARIANTS``）、白点の ``impulse_vs_full``・
既定リクエスト（adjustments 無し）の ``applied`` 回帰も確かめる。CLI が 0.3.0 未満なら開始前に止まる。
``--dry-run`` ではリクエストを組んで §4.1 の契約を検査し ``adjustments_plan.json`` に書くだけ（CLI は呼ばない）。
ARW / JPG の拡張子は大文字小文字を問わない（``evaluation.sources`` で解決）。
``--bracket`` と ``--adjustments`` の実行、``--diagnosis`` の CFA 計測は rawpy を使う旧経路で ``--legacy-rawpy`` が必要。
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
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
# 評価用の独立サポート（scripts/evaluation/）。アプリ本体（local_raw）・rawpy・lensfunpy に依存しない。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from evaluation import candidate_metrics, frozen, legacy, sources  # noqa: E402
from evaluation import presets as frozen_presets  # noqa: E402

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
    jpg = source_file(source_root, capture_id, "JPG")
    exif = sources.read_capture_exif(jpg)
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
    """ARW と JPG が揃う撮影 ID（拡張子の大文字小文字は問わない。``evaluation.sources`` の規則）。"""
    return [cid for cid in sources.paired_ids(source_root) if CAPTURE_ID_PATTERN.fullmatch(cid)]


def source_file(source_root: Path, capture_id: str, kind: str) -> Path:
    """撮影 ID の ARW / JPG を実エントリ名で返す（無ければ正規表記のパス。存在確認は呼び出し側）。"""
    return sources.find_capture_file(source_root, capture_id, kind) or source_root / f"{capture_id}.{kind}"


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
    """旧 rawpy 経路（アーカイブ）のワーカー。親が ``--legacy-rawpy`` のときだけ起動する。"""
    from unittest import mock

    config = legacy.import_module("local_raw.config", "rawpy ワーカー")
    engine = legacy.import_module("local_raw.engine", "rawpy ワーカー")

    source_root = Path(args.source_root).resolve()
    out_dir = Path(args.worker_out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    capture_id = validate_capture_id(args.worker_rawpy)
    preview = args.worker_mode == "preview"
    overrides = parse_worker_adjustments(args.worker_adjustments)
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
        if overrides is not None:
            kwargs["adjustments"] = overrides  # 既定プリセットへの上書き（engine.resolve_adjustments が検証・クランプ）
        if preview:
            kwargs["max_edge"] = args.max_edge  # 明示しないと preview は 1600px に落ちる
        result = engine.develop_raw(capture_id, args.worker_preset or engine.DEFAULT_PRESET, **kwargs)
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
        "adjustments": result.get("adjustments"),
    }, ensure_ascii=False, default=str))


def parse_worker_adjustments(text: str | None) -> dict[str, Any] | None:
    """``--worker-adjustments`` の JSON（Adjustments の部分 / 全量の上書き）を dict にする。省略時は None。"""
    if text is None:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise SystemExit(f"--worker-adjustments が JSON ではありません: {error}") from None
    if not isinstance(value, dict):
        raise SystemExit("--worker-adjustments は JSON オブジェクトで指定してください")
    return value


def worker_noop(_: argparse.Namespace) -> None:
    """rawpy ワーカーの常駐コスト（インタプリタ + numpy/cv2/rawpy import）の基準値（旧経路のみ）。"""
    legacy.import_module("local_raw.engine", "rawpy ワーカー基準値")

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
    #: 旧 rawpy / local_raw 経路（アーカイブ）を使うか。``--legacy-rawpy`` の明示 opt-in だけで True。
    legacy_rawpy: bool = False

    def say(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        print(line, flush=True)
        self.log.append(line)


def run_rawpy(
    ctx: RunContext,
    capture_id: str,
    mode: str,
    *,
    nosharp: bool = False,
    adjustments: dict[str, Any] | None = None,
    variant: str | None = None,
    preset: str | None = None,
) -> dict[str, Any]:
    """rawpy ワーカーを 1 回呼ぶ。``adjustments`` は既定プリセットへの上書き（``--worker-adjustments``）。

    ``preset`` は rawpy（Python ``local_raw/presets.py``）のプリセット名（既定 DEFAULT_PRESET。--presets の legacy 参考）。
    旧経路（アーカイブ）なので ``ctx.legacy_rawpy``（``--legacy-rawpy``）が無ければ ``LegacyUnavailable``。
    """
    legacy.require("rawpy 現像列", ctx.legacy_rawpy)
    # nosharp は params_hash が変わらず同名ファイルになるので、出力ディレクトリを分ける。
    # adjustments 指定時もバリアントごとに分ける（レシピ JSON を取り違えないため）。
    if preset is not None:
        out_dir = ctx.out / "rawpy_preset" / preset / mode
    elif adjustments is not None:
        out_dir = ctx.out / "rawpy_adj" / (variant or "adjusted") / mode
    else:
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
    if adjustments is not None:
        cmd += ["--worker-adjustments", json.dumps(adjustments, ensure_ascii=False, sort_keys=True)]
    if preset is not None:
        cmd += ["--worker-preset", preset]
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


def coreimage_request(
    input_path: Path,
    output: Path,
    mode: str,
    lens: bool,
    *,
    decoder_version: str = CI_DECODER_VERSION,
    profile: str = "neutral",
    overrides: dict[str, float] | None = None,
    output_format: str = "jpeg",
    output_color_space: str | None = None,
    max_edge: int | None = None,
    adjustments: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
    extra_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """CLI リクエスト JSON を組み立てる（純粋）。既定引数のままなら 0.2.0 と同じリクエスト。

    ``adjustments`` は CLI 0.3.0 のトップレベル ``adjustments``（設計書 §4.1。9 キー全量）。
    ``diagnostics`` は CLI 0.4.0 のトップレベル ``diagnostics``、``extra_settings`` は数値以外も取る settings の
    追加キー（``gamut_mapping`` / ``baseline_exposure``）。#92 設計書 §4.2〜4.3。省略時はリクエストに出さない。
    """
    settings: dict[str, Any] = {"profile": profile, "lens_correction": lens}
    if overrides:
        settings.update(overrides)
    if extra_settings:
        settings.update(extra_settings)
    request: dict[str, Any] = {
        "input": str(input_path),
        "output": str(output),
        "mode": mode,
        "decoder_version": decoder_version,
        "settings": settings,
    }
    if output_format != "jpeg":
        request["format"] = output_format
    if output_color_space:
        request["output_color_space"] = output_color_space
    if mode == "preview" and max_edge:
        request["max_edge"] = max_edge
    if adjustments is not None:
        request["adjustments"] = dict(adjustments)
    if diagnostics is not None:
        request["diagnostics"] = json.loads(json.dumps(diagnostics))
    return request


def coreimage_output_path(ctx: "RunContext", capture_id: str, mode: str, lens: bool, *, variant: str | None = None,
                          output_format: str = "jpeg", max_edge: int | None = None) -> Path:
    """run_coreimage の出力パス（ディレクトリは作らない）。"""
    suffix = f"_{variant}" if variant else ("_lens" if lens else "")
    edge = max_edge if (mode == "preview" and max_edge and max_edge != ctx.max_edge) else None
    if edge:
        suffix += f"_e{edge}"
    return ctx.out / "coreimage" / mode / f"{capture_id}_{mode}{suffix}.{'tif' if output_format == 'tiff16' else 'jpg'}"


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
    adjustments: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
    extra_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Core Image CLI を 1 回呼ぶ。

    ``overrides`` は CLI の settings 個別上書きキー（``luminance_noise_reduction`` 等。Issue #79）。
    ``output_format`` が ``tiff16`` のときは ``.tif`` に書き、``output_color_space``（srgb / display_p3）を渡せる。
    ``max_edge`` は preview の長辺（省略時 ``ctx.max_edge``）。既定引数のままなら従来と同じリクエストになる。
    ``adjustments`` は CLI 0.3.0 の ``adjustments``（9 キー全量、``profile="apple_default"`` が必要。#72 / #73）。
    ``diagnostics`` / ``extra_settings`` は CLI 0.4.0 の診断モード（#92）。指定したときだけ行に ``ci_gamut`` と
    応答の ``diagnostics`` ブロックの要約を足す（省略時の行のキーは従来と同じ）。
    """
    output = coreimage_output_path(ctx, capture_id, mode, lens, variant=variant, output_format=output_format, max_edge=max_edge)
    output.parent.mkdir(parents=True, exist_ok=True)
    request = coreimage_request(
        source_file(ctx.source_root, capture_id, "ARW"), output, mode, lens,
        decoder_version=decoder_version, profile=profile, overrides=overrides, output_format=output_format,
        output_color_space=output_color_space, max_edge=(max_edge or ctx.max_edge), adjustments=adjustments,
        diagnostics=diagnostics, extra_settings=extra_settings,
    )
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
    if adjustments is not None:
        row.update(adjustments_applied_summary(payload))
    if diagnostics is not None or extra_settings is not None:
        row.update(diagnostics_response_summary(payload))
    return row


def adjustments_applied_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """CLI 0.3.0 応答の ``adjustments_applied``（設計書 §4.2）を CSV 向けの平らな列にする（純粋）。"""
    adj = payload.get("adjustments_applied")
    if not isinstance(adj, dict):
        return {"ci_adjustments_applied": False}
    defaults = adj.get("defaults") or {}
    post = adj.get("post") or {}
    usm = post.get("usm") or {}
    wb = post.get("wb") or {}
    return {
        "ci_adjustments_applied": True,
        "ci_adj_guards": ";".join(str(g) for g in adj.get("guards") or []),
        "ci_adj_ignored": ";".join(str(g) for g in adj.get("ignored") or []),
        "ci_def_sharpness": defaults.get("sharpness_amount"),
        "ci_def_lum_nr": defaults.get("luminance_noise_reduction_amount"),
        "ci_def_color_nr": defaults.get("color_noise_reduction_amount"),
        "ci_def_temperature": defaults.get("neutral_temperature"),
        "ci_post_shadow_lift": post.get("shadow_lift"),
        "ci_post_brightness": post.get("brightness"),
        "ci_post_contrast": post.get("contrast"),
        "ci_post_saturation": post.get("saturation"),
        "ci_post_nr": post.get("noise_reduction"),
        "ci_usm_sigma": usm.get("sigma"),
        "ci_usm_gain": usm.get("gain"),
        "ci_usm_coring": usm.get("coring"),
        "ci_usm_long_edge": usm.get("long_edge"),
        "ci_usm_iso": usm.get("iso"),
        "ci_wb_k0": wb.get("k0"),
        "ci_wb_mired_shift": wb.get("mired_shift"),
        "ci_wb_mired_per_unit": wb.get("mired_per_unit"),
        **highlight_recovery_summary(payload),
    }


def compare_capture(
    ctx: RunContext,
    capture_id: str,
    mode: str,
    features: dict[str, Any],
    category: str,
    with_lens: bool,
) -> list[dict[str, Any]]:
    """1 カット × 1 モードを 3〜4 エンジンで回し、指標とシートを作る。"""
    engine_rows: dict[str, dict[str, Any]] = {}
    if ctx.legacy_rawpy:
        engine_rows["rawpy"] = run_rawpy(ctx, capture_id, mode)
    engine_rows["coreimage"] = run_coreimage(ctx, capture_id, mode, lens=False)
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
    旧経路（アーカイブ）。呼び出し側が ``--legacy-rawpy`` のときだけ使う。
    """
    engine = legacy.import_module("local_raw.engine", "rawpy 後段仕上げ（*_usm）")

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
        if ctx.legacy_rawpy:
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
    if not ctx.legacy_rawpy:
        # rawpy 列と rawpy 後段仕上げの派生（*_usm）は旧経路だけ。既定では欠測ではなく「省略」と記録する。
        for name in ("rawpy", "rawpy_nosharp", *CI_USM_DERIVED):
            rows_by_variant[name] = {"skipped": "legacy_rawpy"}
    for derived, base_name in CI_USM_DERIVED.items():
        if not ctx.legacy_rawpy:
            continue
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
            if not row.get("skipped"):
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
    """候補評価の純関数（旧 ``development_candidates`` の星判定・指標の写し。``evaluation.candidate_metrics``）。"""
    return candidate_metrics


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


#: ``impulse_count(reference_gray8=...)`` で「基準画像にも同じ場所に局所的な明点がある」と見なす閾値（min_diff 比）。
#: 基準（原寸を INTER_AREA で縮めたもの）の 3x3 メディアンとの差が min_diff × この比以上なら、その候補は
#: 白点ではなく被写体の輝点（葉の照り返し・草の穂など）として数えない。
#: T5（2026-10-20、比較レポート §16）の実測: 昼 6 カットの identity で 1818 / 660 / 74 / 150 / 1 / 2 が
#: 0.5 で 1 / 0 / 0 / 0 / 0 / 0 になり、本物の白点（neutral・NR 0・sharpness 0、DSC07415 2400px）は 209 → 208 残る
#: （0.33 でも 204 残る。基準側の輝点と本物の白点は分離できる）。
IMPULSE_REFERENCE_RATIO = 0.5


def impulse_count(gray8: np.ndarray, exclude: np.ndarray | None = None,
                  min_diff: float = IMPULSE_MIN_DIFF, max_area: int = IMPULSE_MAX_AREA,
                  reference_gray8: np.ndarray | None = None) -> dict[str, float]:
    """孤立した 1〜3 px の高輝度点（白点ノイズ）を数える。

    3x3 メディアンとの差が min_diff 以上の画素の連結成分で、面積 max_area 以下かつ exclude（星候補を
    膨らませたマスク）に触れないもの。count と 100 万画素あたりの数を返す。

    ``reference_gray8``（同寸の基準。``impulse_vs_full`` では原寸レンダを INTER_AREA で縮めたもの）を渡すと、
    基準側でも同じ場所（3x3 近傍の最大）が 3x3 メディアンより ``min_diff × IMPULSE_REFERENCE_RATIO`` 以上明るい
    候補は「基準にもある輝点」として数えない。星候補マスク（``exclude``）は暗い空の点にしか効かず、昼の葉や草の
    照り返し（1〜3 px の輝点が密集し、preview の縮小経路の方が INTER_AREA より鋭い）を白点として数えてしまうため。
    """
    g = gray8.astype(np.float32)
    med = cv2.medianBlur(gray8.astype(np.uint8), 3).astype(np.float32)
    cand = ((g - med) >= min_diff).astype(np.uint8)
    ref_excess: np.ndarray | None = None
    if reference_gray8 is not None:
        if reference_gray8.shape != gray8.shape:
            raise ValueError("impulse_count の reference_gray8 は同寸が必要です")
        rg = reference_gray8.astype(np.float32)
        rmed = cv2.medianBlur(reference_gray8.astype(np.uint8), 3).astype(np.float32)
        ref_excess = cv2.dilate(rg - rmed, np.ones((3, 3), np.uint8))
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
            if float(patch[~inside].max()) > peak - min_diff:
                continue
            if ref_excess is not None and float(ref_excess[y0:y1, x0:x1][inside].max()) >= min_diff * IMPULSE_REFERENCE_RATIO:
                continue
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
        if with_rawpy and ctx.legacy_rawpy:
            jobs["rawpy"] = pool.submit(run_rawpy, ctx, capture_id, mode)
        for variant, (decoder, profile, lens, overrides) in ci_requests.items():
            jobs[variant] = pool.submit(
                run_coreimage, ctx, capture_id, mode, lens,
                decoder_version=decoder, profile=profile, variant=variant,
                overrides=overrides or None, max_edge=max_edge,
            )
        rows = {name: future.result() for name, future in jobs.items()}
    if with_rawpy and not ctx.legacy_rawpy:
        # rawpy 列は旧経路（--legacy-rawpy）だけ。既定では欠測ではなく「省略」と記録する。
        rows["rawpy"] = {"skipped": "legacy_rawpy"}
    return rows


def load_variant_images(engine_rows: dict[str, dict[str, Any]], target: tuple[int, int]) -> tuple[dict[str, np.ndarray], dict[str, dict[str, Any]]]:
    """現像結果を読み、target 寸法へ ``fit_to_target`` で揃える（再標本化は避ける）。"""
    images: dict[str, np.ndarray] = {}
    rows: dict[str, dict[str, Any]] = {}
    for name, row in engine_rows.items():
        row = dict(row)
        if not row.get("error") and not row.get("skipped"):
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
            # 基準側にも同じ場所に輝点があれば数えない（昼の葉・草の照り返しの誤検出を除く。IMPULSE_REFERENCE_RATIO）
            ref_gray8 = cv2.cvtColor(full_refs[variant], cv2.COLOR_RGB2GRAY)
            row["impulse_vs_full"] = impulse_count(gray8, ref_points, reference_gray8=ref_gray8)["impulse_count"]
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
        elif not row.get("skipped"):
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
    """``bracket._decode_frame``（no_auto_bright、露出差を保つ）で 1 フレームを 0..1 float32 に。長辺 max_edge へ縮小。

    旧経路（アーカイブ）。``--bracket`` は ``--legacy-rawpy`` のときだけ実行できる。
    """
    bracket = legacy.import_module("local_raw.bracket", "--bracket")
    engine = legacy.import_module("local_raw.engine", "--bracket")

    adjustments = engine.resolve_adjustments(engine.DEFAULT_PRESET, None)
    frame = bracket._decode_frame(source_file(source_root, capture_id, "ARW"), adjustments, preview=True)
    h, w = frame.shape[:2]
    size = fit_size(w, h, max_edge)
    if size != (w, h):
        frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    return np.clip(frame, 0.0, 1.0).astype(np.float32)


def mertens_merge(frames: list[np.ndarray]) -> tuple[np.ndarray, bool]:
    """``bracket._align_frames`` + ``_merge_frames``（旧アプリと同じ OpenCV AlignMTB / MergeMertens。旧経路のみ）。"""
    bracket = legacy.import_module("local_raw.bracket", "--bracket")

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
        elif not row.get("skipped"):
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



# =============================================================================
# Adjustments 評価モード --adjustments（Issue #72 / #73、設計書 coreimage-preview-adjustments.md §3.7）
# =============================================================================

#: adjustments を受けられる CLI の最小版（設計書 §4.1 / §4.2）。
ADJUSTMENTS_MIN_CLI = (0, 3, 0)
#: 既定リクエストの回帰比較に使う baseline（scripts/coreimage-regression-applied.sh と同じ）。
ADJ_BASELINE_JSON = PROJECT_ROOT / "docs" / "coreimage-baseline" / "applied-params-macos-27.0.1.json"
#: baseline 比較で見ないキー（回帰スクリプトと同じ。preview の長辺で変わる / 来歴だけの値）。
ADJ_BASELINE_SKIP = ("orientation_handling", "scale_factor")
#: 掃引の起点。デコード段は恒等（exposure 0・wb 0・sharpness 1.0 = apple_default 既定・NR light = 既定）、
#: 後段は無効（lift 0・b/c/s 1.0）。1 パラメータずつ動かして効きを分離する。
ADJ_IDENTITY: dict[str, Any] = {
    "exposure": 0.0, "wb_temp": 0.0, "shadow_lift": 0.0, "highlight_recovery": 0.0,
    "brightness": 1.0, "contrast": 1.0, "saturation": 1.0, "sharpness": 1.0, "noise_reduction": "light",
}
#: 掃引バリアント: (variant, パラメータ, 値, ラベル)。identity は全パラメータの基準。
#: 値は設計書 §3.7 の表（exposure ±1、wb ±60、lift 45、b 0.7/1.4、c 0.8/1.4、s 0/1.5、sharp 0.8/1.6、NR off/full）。
ADJ_SWEEP_VARIANTS: tuple[tuple[str, str | None, Any, str], ...] = (
    ("identity", None, None, "identity"),
    ("exp_m1", "exposure", -1.0, "exposure -1"),
    ("exp_p1", "exposure", 1.0, "exposure +1"),
    ("wb_m60", "wb_temp", -60.0, "wb_temp -60"),
    ("wb_p60", "wb_temp", 60.0, "wb_temp +60"),
    ("lift_45", "shadow_lift", 45.0, "shadow_lift 45"),
    ("bright_07", "brightness", 0.7, "brightness 0.7"),
    ("bright_14", "brightness", 1.4, "brightness 1.4"),
    ("contrast_08", "contrast", 0.8, "contrast 0.8"),
    ("contrast_14", "contrast", 1.4, "contrast 1.4"),
    ("sat_0", "saturation", 0.0, "saturation 0"),
    ("sat_15", "saturation", 1.5, "saturation 1.5"),
    ("sharp_08", "sharpness", 0.8, "sharpness 0.8"),
    ("sharp_16", "sharpness", 1.6, "sharpness 1.6"),
    ("nr_off", "noise_reduction", "off", "NR off"),
    ("nr_full", "noise_reduction", "full", "NR full"),
)
ADJ_SWEEP_LABELS = {v: label for v, _, _, label in ADJ_SWEEP_VARIANTS}
#: パラメータ -> rawpy と方向・大きさを比べる指標（各エンジン内で identity との差を取る）。
ADJ_PARAM_METRICS: dict[str, tuple[str, ...]] = {
    "exposure": ("exposure_stops", "log2_mean_linear"),
    "wb_temp": ("rb_ln_neutral",),
    "shadow_lift": ("dark_mean_luma",),
    "brightness": ("mean_luma", "std_luma", "ab_std"),
    "contrast": ("mean_luma", "std_luma", "ab_std"),
    "saturation": ("mean_luma", "std_luma", "ab_std"),
    "sharpness": ("edge_width_ratio", "flat_noise"),
    "noise_reduction": ("sky_noise", "sky_chroma_noise", "sky_hf_ratio", "star_recall", "flat_noise"),
}
#: 白点（impulse_vs_full）を数えるバリアント: variant -> settings 個別上書き。
#: nr_off_sh0 は adjustments + sharpness_amount 0 の明示上書きで CLI の白点ガードを発動させる（§3.4）。
ADJ_WHITE_DOT_VARIANTS: dict[str, dict[str, float]] = {
    "identity": {},
    "nr_off": {},
    "nr_off_sh0": {"sharpness_amount": 0.0},
}
ADJ_WHITE_DOT_GUARD = "white_dot_sharpness_floor"
#: 後段オラクル: (variant, 後段の上書き, 種別)。デコード段は identity のまま tiff16 sRGB で出し、
#: adjustments 無しの tiff16 sRGB に Python の後段（_apply_shadow_lift_array + _apply_tone_array）を掛けたものと比べる。
#: "tone" が合否対象（USM 無し）。"tone+usm" は CLI が応答した post.usm（σ・gain・coring）で Python の USM を
#: 再現した参考比較（ガウス実装差があるので合否は参考扱い）。"preset" は既定プリセットの値（実行時に解決）。
ADJ_ORACLE_VARIANTS: tuple[tuple[str, dict[str, Any] | str, str], ...] = (
    ("or_lift_45", {"shadow_lift": 45.0}, "tone"),
    ("or_lift_100", {"shadow_lift": 100.0}, "tone"),
    ("or_bright_07", {"brightness": 0.7}, "tone"),
    ("or_bright_14", {"brightness": 1.4}, "tone"),
    ("or_contrast_08", {"contrast": 0.8}, "tone"),
    ("or_contrast_14", {"contrast": 1.4}, "tone"),
    ("or_sat_0", {"saturation": 0.0}, "tone"),
    ("or_sat_15", {"saturation": 1.5}, "tone"),
    ("or_combo", {"shadow_lift": 30.0, "brightness": 1.1, "contrast": 1.2, "saturation": 1.3}, "tone"),
    ("or_sharp_16", {"sharpness": 1.6}, "tone+usm"),
    ("or_preset", "preset", "tone+usm"),
)
#: オラクルの暫定閾値（設計書 §3.7）: ΔE76 の中央値 ≤ 0.3、チャンネル差の p99 ≤ 1/255。
ORACLE_DE76_MEDIAN_MAX = 0.3
ORACLE_CHANNEL_P99_MAX = 1.0 / 255.0
#: その他の合否の目安（設計書 §3.7 の暫定値を T5（2026-10-20、昼 6 + 夜 3 カット。比較レポート §16）で確定したもの）。
#: exposure: ±1 EV の log2 差の許容。§3.7 の 0.15 → 0.4。rawpy は `bright = 2**exposure`（リニアの純粋な 2 倍）なのに
#: 同じ指標が全 9 カットで ±1.15〜1.21 を読む（出力ガンマ・非クリップ画素の選別による指標自身の偏り）。CI は
#: apple_default のトーンカーブ越しに ±1.0〜1.38 と読める。絶対値は粗い健全性確認に下げ、大きさは rawpy 比で見る。
ADJ_EXPOSURE_STOP_TOL = 0.4
#: exposure: CI の log2 差 / rawpy の log2 差。実測 0.81〜1.18（昼・夜）。
ADJ_EXPOSURE_RATIO_RANGE = (0.8, 1.25)
ADJ_WB_RATIO_RANGE = (0.7, 1.4)
#: sharpness 1.0（= apple_default のデコーダ内シャープ既定）のエッジ幅のカメラ比。較正できる定数が無く
#: （デコーダの既定値）、カット・レンズで 0.87〜1.02 に散るので §3.7 の ±0.05 → ±0.15（0.80..1.10）。
ADJ_SHARP_BASELINE_WIDTH = 0.95
ADJ_SHARP_BASELINE_TOL = 0.15
#: sharpness 1.6 の flat_noise。§3.7 は「カメラ比 1.5 倍以内」だったが、identity（1.0）の時点でカメラ比 0.9〜1.41 と
#: カット差が大きい（apple_default は低 ISO で NR ≈ 0）。USM が足すノイズを見るのが目的なので identity 比にする。
#: 実測 1.02〜1.21。
ADJ_SHARP16_NOISE_MAX = 1.5
#: NR full の星の残存率。星空カットの絶対値（§3.7 の 0.99）に加え、昼カットでは「light より減らない」で判定する
#: （昼の「強い点」は葉の照り返しで、identity の時点で 0.92〜0.99。full はそれを 1 つも減らしていない）。
ADJ_NR_FULL_RECALL_MIN = 0.99
#: light からの残存率の低下の許容。基準点が少ないカットでは 1 点分（1/n）の揺らぎが 0.01 を超える
#: （DSC07402: 81 点中 1 点 = 0.012。輝度 NR 0.5 で 1 点落ち、0.6 では落ちない = 測定の揺らぎ）ので、
#: 1.5 点分までは許す（max(0.01, 1.5/n)）。
ADJ_NR_FULL_RECALL_DROP_MAX = 0.01
ADJ_NR_FULL_RECALL_DROP_POINTS = 1.5
#: NR full の空の高周波比（塗りつぶし防止）。§3.7 は camera 比 0.7 だったが、輝度 NR の掃引（0.3〜0.7、6 カット）では
#: ISO 3200 で 0.5 以上・ISO 6400 で 0.42 以上にすると 0.7 を割る一方、星の残存率は全段 0.995〜1.0 で塗りつぶしは
#: 目視でも出ない（§16）。full の表を 0.5 で頭打ちにし、閾値は camera 比 0.5 にする。light の時点で camera 比 0.7 を
#: 割るカット（昼の望遠 DSC04872: 0.57）は「空」が空でないので、light 比 0.9 以上なら可とする。
ADJ_NR_FULL_HF_MIN = 0.5
ADJ_NR_FULL_HF_VS_LIGHT_MIN = 0.9
ADJ_IMPULSE_MAX = 10
ADJ_ELAPSED_RATIO_MAX = 1.5
#: 近中立タイル（カメラ JPEG で彩度下位 10%）と暗部（輝度下位 25%）
NEUTRAL_TILE_BLOCK = 32
NEUTRAL_TILE_FRACTION = 0.1
NEUTRAL_TILE_LUMA = (0.08, 0.92)
DARK_FRACTION = 0.25
ADJ_KEYS = (
    "exposure_stops", "log2_mean_linear", "rb_ln_neutral", "dark_mean_luma", "mean_luma", "std_luma", "ab_std",
    "edge_width_ratio", "edge_width_px", "flat_noise", "flat_noise_ratio",
    "sky_noise", "sky_noise_ratio", "sky_chroma_noise", "sky_hf_ratio", "star_recall", "impulse_count", "impulse_vs_full",
    "engine_elapsed_s", "wall_s",
)


def adjustments_for(variant: str) -> dict[str, Any]:
    """掃引バリアントの Adjustments 9 キー全量（identity に 1 パラメータだけ上書き）。"""
    for name, param, value, _label in ADJ_SWEEP_VARIANTS:
        if name == variant:
            out = dict(ADJ_IDENTITY)
            if param is not None:
                out[param] = value
            return out
    if variant in ADJ_WHITE_DOT_VARIANTS and variant.startswith("nr_off"):
        return {**ADJ_IDENTITY, "noise_reduction": "off"}
    raise KeyError(f"未知の adjustments バリアントです: {variant}")


def oracle_adjustments(post: dict[str, Any] | str, preset: dict[str, Any] | None = None) -> dict[str, Any]:
    """オラクル用 Adjustments: デコード段は identity に固定し、後段（lift / b / c / s / sharpness）だけ動かす。

    ``post == "preset"`` のときは ``preset``（既定プリセットの解決済み値）の後段キーを使う。
    exposure / wb_temp / noise_reduction は identity のまま（デコード段を恒等に保つ）。
    ``highlight_recovery`` は常に 0（CLI 0.6.0 から exposure / shadow_lift に写像されてデコード段にも効き、
    Python の ``post_chain_oracle`` と合わなくなるため。#81 設計書 §9 T3）。
    """
    source = preset if post == "preset" else post
    if not isinstance(source, dict):
        raise ValueError("preset のオラクルには既定プリセットの値が必要です")
    out = dict(ADJ_IDENTITY)
    for key in ("shadow_lift", "brightness", "contrast", "saturation", "sharpness"):
        if key in source:
            out[key] = float(source[key])
    out["highlight_recovery"] = 0.0
    return out


def parse_cli_version(value: Any) -> tuple[int, ...] | None:
    """"0.3.0" -> (0, 3, 0)。読めなければ None。"""
    if not isinstance(value, str):
        return None
    match = re.match(r"^\s*(\d+)\.(\d+)(?:\.(\d+))?", value)
    if not match:
        return None
    return tuple(int(g or 0) for g in match.groups())


def adjustments_cli_problem(version: Any, capabilities: Any) -> str | None:
    """CLI の ``--version`` / ``--capabilities`` 応答から、--adjustments を回せない理由を返す（回せれば None）。"""
    if not isinstance(version, dict) or version.get("error") or not version.get("cli_version"):
        detail = version.get("error") if isinstance(version, dict) else version
        return f"Core Image CLI のバージョンを取得できません（--version: {detail}）"
    parsed = parse_cli_version(version.get("cli_version"))
    need = ".".join(str(v) for v in ADJUSTMENTS_MIN_CLI)
    if parsed is None or parsed < ADJUSTMENTS_MIN_CLI:
        return (f"Core Image CLI {version.get('cli_version')} は adjustments に未対応です（{need} 以上が必要）。"
                "CLI を更新して npm run build:coreimage で再ビルドしてください")
    adj = capabilities.get("adjustments") if isinstance(capabilities, dict) else None
    if not isinstance(adj, dict) or adj.get("supported") is not True:
        return (f"Core Image CLI {version.get('cli_version')} の --capabilities に adjustments.supported: true がありません"
                "（設計書 §4.2）")
    return None


def validate_adjustments_request(request: dict[str, Any]) -> list[str]:
    """ハーネスが組んだ CLI リクエストが設計書 §4.1 の凍結契約に沿うかを確かめる（純粋。問題の一覧を返す）。"""
    ADJUSTMENT_KEYS = frozen_presets.ADJUSTMENT_KEYS
    ADJUSTMENT_RANGES = frozen_presets.ADJUSTMENT_RANGES
    NOISE_REDUCTION_CHOICES = frozen_presets.NOISE_REDUCTION_CHOICES

    adj = request.get("adjustments")
    if adj is None:
        return []
    problems: list[str] = []
    settings = request.get("settings") or {}
    if not isinstance(adj, dict):
        return ["adjustments がオブジェクトではありません"]
    if settings.get("profile") != "apple_default":
        problems.append("adjustments は profile apple_default のときだけ指定できます")
    for key in ("exposure_ev", "neutral_temperature", "neutral_tint"):
        if key in settings:
            problems.append(f"adjustments と settings.{key} は同時に指定できません")
    missing = sorted(set(ADJUSTMENT_KEYS) - set(adj))
    unknown = sorted(set(adj) - set(ADJUSTMENT_KEYS))
    if missing:
        problems.append(f"adjustments のキーが足りません: {', '.join(missing)}")
    if unknown:
        problems.append(f"adjustments に未知のキーがあります: {', '.join(unknown)}")
    for key, (lo, hi) in ADJUSTMENT_RANGES.items():
        value = adj.get(key)
        if key not in adj:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            problems.append(f"adjustments.{key} が数値ではありません: {value!r}")
        elif not (lo <= float(value) <= hi):
            problems.append(f"adjustments.{key}={value} が値域 {lo}..{hi} の外です")
    if "noise_reduction" in adj and adj["noise_reduction"] not in NOISE_REDUCTION_CHOICES:
        problems.append(f"adjustments.noise_reduction が不正です: {adj['noise_reduction']!r}")
    return problems


def mean_linear_luma(rgb8: np.ndarray, mask: np.ndarray | None = None) -> float:
    """sRGB 8bit をリニア化した Rec.709 輝度の平均（mask があればその画素だけ）。"""
    lin = luma_of(srgb_to_linear(rgb8.astype(np.float32) / 255.0))
    sel = lin[mask] if mask is not None else lin
    return float(sel.mean()) if sel.size else float("nan")


def neutral_tile_mask(camera_rgb8: np.ndarray, block: int = NEUTRAL_TILE_BLOCK, fraction: float = NEUTRAL_TILE_FRACTION,
                      luma_range: tuple[float, float] = NEUTRAL_TILE_LUMA) -> np.ndarray:
    """カメラ JPEG で彩度（max−min の平均）の下位 ``fraction`` のタイル = 近中立パッチの画素マスク。

    黒つぶれ・白飛びに近いタイル（平均輝度が ``luma_range`` の外）は R/B 比が不安定なので除く。
    """
    rgb01 = camera_rgb8.astype(np.float32) / 255.0
    h, w = rgb01.shape[:2]
    rows, cols = h // block, w // block
    mask = np.zeros((h, w), dtype=bool)
    if rows == 0 or cols == 0:
        return mask
    crop = rgb01[: rows * block, : cols * block]
    tiles = crop.reshape(rows, block, cols, block, 3).swapaxes(1, 2)
    sat = (tiles.max(axis=4) - tiles.min(axis=4)).mean(axis=(2, 3))
    luma = luma_of(tiles.reshape(rows, cols, block * block, 3)).mean(axis=2)
    valid = (luma >= luma_range[0]) & (luma <= luma_range[1])
    if not np.any(valid):
        return mask
    count = max(1, int(round(valid.sum() * fraction)))
    order = np.argsort(np.where(valid, sat, np.inf), axis=None, kind="stable")[:count]
    for index in order:
        r, c = divmod(int(index), cols)
        mask[r * block:(r + 1) * block, c * block:(c + 1) * block] = True
    return mask


#: ``rb_log_ratio`` で測る画素の最大チャンネルの範囲（測る画像自身で判定）。白飛び・黒つぶれに近い画素は WB を
#: 動かしても R/B 比が動かない（apple_default は 0.9 付近からハイライトを圧縮し、rawpy の明るい現像は 0.98 で飽和する）。
#: T5（2026-10-20）: 近中立タイルをカメラ JPEG だけで決めていたため、rawpy が明るく現像する DSC04453 では rawpy 側の
#: 37〜42% が飽和して rawpy の効きが半分に、逆光の DSC04473 では CI 側の 19〜22% が飽和して CI の効きが 1/3 に見えた
#: （比 1.93 / 0.37）。上限 0.98 では 0.71 / 0.47〜0.76、0.9 で 0.84〜1.03 に収まる。
RB_PIXEL_RANGE = (0.05, 0.9)


def rb_log_ratio(rgb8: np.ndarray, mask: np.ndarray, pixel_range: tuple[float, float] = RB_PIXEL_RANGE) -> float:
    """mask 画素のリニア R 平均 / B 平均の自然対数（暖色ほど大きい）。

    測る画像自身で最大チャンネルが ``pixel_range`` の外（飽和・黒つぶれ）の画素は除く（``RB_PIXEL_RANGE``）。
    """
    if not mask.any():
        return float("nan")
    rgb01 = rgb8.astype(np.float32) / 255.0
    peak = rgb01.max(axis=2)
    valid = mask & (peak > pixel_range[0]) & (peak < pixel_range[1])
    if valid.sum() < 64:
        return float("nan")
    lin = srgb_to_linear(rgb01)
    r = float(lin[..., 0][valid].mean())
    b = float(lin[..., 2][valid].mean())
    return math.log(r / b) if r > 0 and b > 0 else float("nan")


def dark_pixel_mask(camera_rgb8: np.ndarray, fraction: float = DARK_FRACTION) -> np.ndarray:
    """カメラ JPEG の輝度下位 ``fraction`` の画素（暗部）。全バリアントを同じ画素で測るため基準画像で 1 回決める。"""
    luma = luma_of(camera_rgb8.astype(np.float32) / 255.0)
    return luma <= np.percentile(luma, fraction * 100.0)


def tone_stats(rgb8: np.ndarray) -> dict[str, float]:
    """平均輝度・輝度の標準偏差（sRGB 0..1）と a*b* の標準偏差（Lab、hypot(std a, std b)）。"""
    rgb01 = rgb8.astype(np.float32) / 255.0
    luma = luma_of(rgb01)
    lab = cv2.cvtColor(rgb01, cv2.COLOR_RGB2Lab)
    return {
        "mean_luma": float(luma.mean()),
        "std_luma": float(luma.std()),
        "ab_std": float(math.hypot(float(lab[..., 1].std()), float(lab[..., 2].std()))),
    }


def adjustment_image_metrics(rgb8: np.ndarray, neutral_mask: np.ndarray, dark_mask: np.ndarray) -> dict[str, float]:
    """掃引の方向・大きさを見る基本指標（純粋）。差は呼び出し側が identity と取る。"""
    lin_mean = mean_linear_luma(rgb8)
    out = {
        "log2_mean_linear": math.log2(lin_mean) if lin_mean > 0 else float("nan"),
        "rb_ln_neutral": rb_log_ratio(rgb8, neutral_mask),
        "dark_mean_luma": float(luma_of(rgb8.astype(np.float32) / 255.0)[dark_mask].mean()) if dark_mask.any() else float("nan"),
    }
    out.update(tone_stats(rgb8))
    out["flat_noise"] = flat_noise(cv2.cvtColor(rgb8, cv2.COLOR_RGB2GRAY))
    return out


def edge_reference(cam_gray: np.ndarray) -> dict[str, Any]:
    """カメラ JPEG のエッジ点（ROI ごと）と 10–90% 幅。``edge_width_vs_camera`` の基準（--sharpness と同じ手順）。"""
    h, w = cam_gray.shape[:2]
    roi_size = max(96, int(round(max(w, h) * ROI_FRACTION)))
    boxes = roi_boxes(cam_gray, roi_size, k=3)
    edge_points = {label: find_edge_points(cam_gray, box) for label, box in boxes if label != "corner"}
    cam_widths = {label: [edge_rise_width(p) for p in sample_profiles(cam_gray, pts)]
                  for label, pts in edge_points.items() if len(pts)}
    return {"boxes": boxes, "edge_points": edge_points, "cam_widths": cam_widths, "max_shift": roi_size / 4.0}


def edge_width_vs_camera(ref: dict[str, Any], cam_gray: np.ndarray, gray: np.ndarray) -> dict[str, Any]:
    """エッジ 10–90% 幅の中央値と、カメラ JPEG との比の中央値（ROI ごとに位相相関で位置合わせ）。"""
    widths: list[float] = []
    ratios: list[float] = []
    for label, box in ref["boxes"]:
        pts = ref["edge_points"].get(label)
        if pts is None or not len(pts) or label not in ref["cam_widths"]:
            continue
        dx, dy, response = roi_shift(cam_gray, gray, box, margin=int(ref["max_shift"]))
        if response < ROI_MIN_RESPONSE or math.hypot(dx, dy) > ref["max_shift"]:
            continue
        moved = pts.copy()
        moved[:, 0] += dx
        moved[:, 1] += dy
        for w_var, w_cam in zip((edge_rise_width(p) for p in sample_profiles(gray, moved)), ref["cam_widths"][label]):
            if w_var is not None:
                widths.append(w_var)
                if w_cam:
                    ratios.append(w_var / w_cam)
    return {"edge_width_px": median_or_nan(widths), "edge_width_ratio": median_or_nan(ratios), "edge_n": len(widths)}


def delta_e76_map(rgb01_a: np.ndarray, rgb01_b: np.ndarray) -> np.ndarray:
    """0..1 float sRGB 2 枚の画素ごとの ΔE76（OpenCV の Lab, D65）。"""
    if rgb01_a.shape != rgb01_b.shape:
        raise ValueError("delta_e76_map は同寸の画像が必要です")
    lab_a = cv2.cvtColor(np.clip(rgb01_a, 0, 1).astype(np.float32), cv2.COLOR_RGB2Lab)
    lab_b = cv2.cvtColor(np.clip(rgb01_b, 0, 1).astype(np.float32), cv2.COLOR_RGB2Lab)
    return np.sqrt(((lab_a - lab_b) ** 2).sum(axis=2))


def oracle_compare(ci01: np.ndarray, oracle01: np.ndarray) -> dict[str, Any]:
    """CI の後段出力とオラクル（Python 後段）の差（純粋）。閾値は ORACLE_* （設計書 §3.7 の暫定値）。"""
    de = delta_e76_map(ci01, oracle01)
    diff = np.abs(ci01.astype(np.float32) - oracle01.astype(np.float32))
    out = {
        "de76_median": float(np.median(de)),
        "de76_p99": float(np.percentile(de, 99)),
        "de76_max": float(de.max()),
        "chan_abs_mean": float(diff.mean()),
        "chan_abs_p99": float(np.percentile(diff, 99)),
        "chan_abs_max": float(diff.max()),
    }
    out["pass_de76_median"] = out["de76_median"] <= ORACLE_DE76_MEDIAN_MAX
    out["pass_chan_p99"] = out["chan_abs_p99"] <= ORACLE_CHANNEL_P99_MAX + 1e-9
    out["pass"] = out["pass_de76_median"] and out["pass_chan_p99"]
    return out


def post_chain_oracle(base01: np.ndarray, adjustments: dict[str, Any], usm: dict[str, Any] | None = None) -> np.ndarray:
    """adjustments 無しの CI 出力（tiff16 sRGB、0..1）に rawpy の後段を Python で掛けたオラクル。

    ``engine._apply_shadow_lift_array`` → ``engine._apply_tone_array``（ロジックは複製しない）。USM は
    ``usm``（CLI が ``adjustments_applied.post.usm`` で返した σ・gain・coring）で差し替え、無ければ gain 0
    （``rawpy_nosharp`` と同じ手法）。CI の写像（sharpness ≤ 1 はデコーダ側、>1 の増分だけ USM）に合わせるため、
    rawpy の撮影時シャープの式は使わない。
    """
    from unittest import mock

    engine = legacy.import_module("local_raw.engine", "--adjustments の後段オラクル")

    sigma = float((usm or {}).get("sigma") or 0.6)
    gain = float((usm or {}).get("gain") or 0.0)
    coring = float((usm or {}).get("coring") or 0.0)

    def ci_usm_params(_long_edge: int, _sharpness: float, _iso: float | None) -> tuple[float, float, float]:
        return sigma, gain, coring

    lifted = engine._apply_shadow_lift_array(base01, float(adjustments["shadow_lift"]))
    with mock.patch.object(engine, "_capture_sharpen_params", ci_usm_params):
        return engine._apply_tone_array(lifted, adjustments, iso=None)


def applied_diff(expected: dict[str, Any], actual: dict[str, Any], skip: Iterable[str] = ADJ_BASELINE_SKIP,
                 rel_tol: float = 0.0) -> list[str]:
    """``applied`` 2 つの差分（キー集合の差も数える。回帰スクリプトと同じ規則）。rel_tol > 0 なら数値は相対誤差で比べる。"""
    skip = set(skip)
    diffs = []
    for key in sorted((set(expected) | set(actual)) - skip):
        a, b = expected.get(key), actual.get(key)
        if rel_tol > 0 and isinstance(a, (int, float)) and isinstance(b, (int, float)) \
                and not isinstance(a, bool) and not isinstance(b, bool):
            if math.isclose(float(a), float(b), rel_tol=rel_tol, abs_tol=rel_tol):
                continue
        elif a == b:
            continue
        diffs.append(f"{key}: {a!r} -> {b!r}")
    return diffs


def sweep_deltas(metrics: dict[str, dict[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """各エンジン内で identity との差を取り、rawpy と CI の方向・大きさを並べる（純粋）。

    ``metrics``: engine（"rawpy" / "ci"）-> variant -> 指標。戻り値は (variant, metric) ごとの行:
    delta_rawpy, delta_ci, ratio（= delta_ci / delta_rawpy）, direction_match。
    """
    rows = []
    for variant, param, value, _label in ADJ_SWEEP_VARIANTS:
        if param is None:
            continue
        for metric in ADJ_PARAM_METRICS[param]:
            row: dict[str, Any] = {"variant": variant, "param": param, "value": value, "metric": metric}
            for engine_name in ("rawpy", "ci"):
                base = metrics.get(engine_name, {}).get("identity", {}).get(metric)
                cur = metrics.get(engine_name, {}).get(variant, {}).get(metric)
                ok = all(isinstance(v, (int, float)) and math.isfinite(float(v)) for v in (base, cur))
                row[f"delta_{engine_name}"] = float(cur) - float(base) if ok else float("nan")
            dr, dc = row["delta_rawpy"], row["delta_ci"]
            row["ratio"] = dc / dr if math.isfinite(dr) and math.isfinite(dc) and dr != 0 else float("nan")
            row["direction_match"] = direction_match(dr, dc)
            rows.append(row)
    return rows


#: 「効いていない」とみなす差の大きさ（指標の単位。8bit 量子化・JPEG の揺れより十分小さい）。
DIRECTION_EPS = 1e-4


def direction_match(delta_ref: float, delta: float, eps: float = DIRECTION_EPS) -> bool | None:
    """基準（rawpy）の差と同じ向きに動いたか。基準が動かなければ None、基準だけ動いて対象が動かなければ False。"""
    if not (math.isfinite(delta_ref) and math.isfinite(delta)) or abs(delta_ref) <= eps:
        return None
    if abs(delta) <= eps:
        return False
    return (delta_ref > 0) == (delta > 0)


def _num(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else float("nan")


def _check(name: str, value: Any, threshold: str, passed: bool | None, **extra: Any) -> dict[str, Any]:
    return {"check": name, "value": value, "threshold": threshold, "pass": passed, **extra}


def adjustment_checks(
    ci: dict[str, dict[str, Any]],
    rawpy: dict[str, dict[str, Any]],
    camera: dict[str, Any],
    deltas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """設計書 §3.7 の合否の目安（暫定）を 1 カット分評価する（純粋）。測れない項目は pass=None。

    ``ci`` / ``rawpy``: variant -> 指標（white dot の ``nr_off_sh0`` と ``ci_adj_guards`` を含みうる）。
    ``camera``: カメラ JPEG の指標（flat_noise / sky_hf_ratio）。``deltas``: ``sweep_deltas`` の結果。
    """
    checks: list[dict[str, Any]] = []
    d = {(r["variant"], r["metric"]): r for r in deltas}

    def fin(*values: float) -> bool:
        return all(math.isfinite(v) for v in values)

    # exposure: CI の log2 差が ±1 の ±0.15 以内・両エンジンで単調（exposure_ratio_stops = 中央 60%・非クリップ画素の
    # リニア輝度比。測れなければ全画素の log2_mean_linear の差で代える）
    def stops(variant: str, engine_name: str) -> float:
        value = _num(d.get((variant, "exposure_stops"), {}).get(f"delta_{engine_name}"))
        return value if math.isfinite(value) else _num(d.get((variant, "log2_mean_linear"), {}).get(f"delta_{engine_name}"))

    m1, p1 = stops("exp_m1", "ci"), stops("exp_p1", "ci")
    checks.append(_check("exposure_ci_m1_stops", m1, f"-1±{ADJ_EXPOSURE_STOP_TOL}", abs(m1 + 1) <= ADJ_EXPOSURE_STOP_TOL if fin(m1) else None))
    checks.append(_check("exposure_ci_p1_stops", p1, f"+1±{ADJ_EXPOSURE_STOP_TOL}", abs(p1 - 1) <= ADJ_EXPOSURE_STOP_TOL if fin(p1) else None))
    # 大きさは rawpy 比（指標自身の偏りが打ち消える。ADJ_EXPOSURE_RATIO_RANGE）
    rlo, rhi = ADJ_EXPOSURE_RATIO_RANGE
    for variant in ("exp_m1", "exp_p1"):
        rm = _num(d.get((variant, "exposure_stops"), {}).get("ratio"))
        checks.append(_check(f"exposure_ci_{variant[-2:]}_vs_rawpy", rm, f"{rlo}..{rhi}", (rlo <= rm <= rhi) if fin(rm) else None))
    for engine_name in ("ci", "rawpy"):
        a, b = stops("exp_m1", engine_name), stops("exp_p1", engine_name)
        checks.append(_check(f"exposure_{engine_name}_monotonic", f"{a:.3f} < 0 < {b:.3f}", "-1 < 0 < +1", (a < 0 < b) if fin(a, b) else None))
    # wb_temp: rawpy と同方向（+ で R/B 増 = 暖色）、大きさの比 0.7〜1.4
    lo, hi = ADJ_WB_RATIO_RANGE
    for variant in ("wb_m60", "wb_p60"):
        row = d.get((variant, "rb_ln_neutral"), {})
        dc, ratio = _num(row.get("delta_ci")), _num(row.get("ratio"))
        expect_sign = 1 if variant == "wb_p60" else -1
        checks.append(_check(f"{variant}_ci_direction", dc, "+ で R/B 増（暖）" if expect_sign > 0 else "− で R/B 減（青）",
                             (dc * expect_sign > 0) if fin(dc) else None))
        checks.append(_check(f"{variant}_direction_vs_rawpy", row.get("direction_match"), "rawpy と同方向", row.get("direction_match")))
        checks.append(_check(f"{variant}_ratio_vs_rawpy", ratio, f"{lo}..{hi}", (lo <= ratio <= hi) if fin(ratio) else None))
    # shadow_lift / brightness / contrast / saturation: rawpy と同方向（大きさはオラクルで見る）
    for variant, metric in (("lift_45", "dark_mean_luma"), ("bright_07", "mean_luma"), ("bright_14", "mean_luma"),
                            ("contrast_08", "std_luma"), ("contrast_14", "std_luma"), ("sat_0", "ab_std"), ("sat_15", "ab_std")):
        row = d.get((variant, metric), {})
        checks.append(_check(f"{variant}_{metric}_direction_vs_rawpy", _num(row.get("ratio")), "同方向（値は ci/rawpy の比）",
                             row.get("direction_match")))
    # sharpness: エッジ幅 0.8 > 1.0 > 1.6（単調）、1.6 の flat_noise がカメラ比 1.5 倍以内、1.0 は 0.95±0.05
    w08, w10, w16 = (_num(ci.get(v, {}).get("edge_width_ratio")) for v in ("sharp_08", "identity", "sharp_16"))
    checks.append(_check("sharpness_ci_edge_monotonic", f"{w08:.3f} > {w10:.3f} > {w16:.3f}", "0.8 > 1.0 > 1.6",
                         (w08 > w10 > w16) if fin(w08, w10, w16) else None))
    checks.append(_check("sharpness_ci_10_edge_ratio", w10, f"{ADJ_SHARP_BASELINE_WIDTH}±{ADJ_SHARP_BASELINE_TOL}",
                         abs(w10 - ADJ_SHARP_BASELINE_WIDTH) <= ADJ_SHARP_BASELINE_TOL if fin(w10) else None))
    n16, n10, ncam = (_num(ci.get("sharp_16", {}).get("flat_noise")), _num(ci.get("identity", {}).get("flat_noise")),
                      _num(camera.get("flat_noise")))
    nratio = n16 / n10 if fin(n16, n10) and n10 > 0 else float("nan")
    checks.append(_check("sharpness_ci_16_flat_noise_vs_identity", nratio, f"≤{ADJ_SHARP16_NOISE_MAX}",
                         nratio <= ADJ_SHARP16_NOISE_MAX if fin(nratio) else None,
                         flat_noise_vs_camera=n16 / ncam if fin(n16, ncam) and ncam > 0 else float("nan")))
    # noise_reduction: off > light > full（空があれば sky_noise、無ければ flat_noise）、full の星と塗りつぶし
    key = "sky_noise" if fin(*(_num(ci.get(v, {}).get("sky_noise")) for v in ("nr_off", "identity", "nr_full"))) else "flat_noise"
    off, light, full = (_num(ci.get(v, {}).get(key)) for v in ("nr_off", "identity", "nr_full"))
    checks.append(_check("nr_ci_monotonic", f"{off:.3f} > {light:.3f} > {full:.3f}", f"off > light > full（{key}）",
                         (off > light > full) if fin(off, light, full) else None))
    recall = _num(ci.get("nr_full", {}).get("star_recall"))
    light_recall = _num(ci.get("identity", {}).get("star_recall"))
    recall_n = _num(ci.get("nr_full", {}).get("star_recall_n"))
    has_stars = recall_n > 0
    drop_max = max(ADJ_NR_FULL_RECALL_DROP_MAX, ADJ_NR_FULL_RECALL_DROP_POINTS / recall_n) if has_stars else ADJ_NR_FULL_RECALL_DROP_MAX
    recall_ok = recall >= ADJ_NR_FULL_RECALL_MIN or (fin(light_recall) and recall >= light_recall - drop_max)
    checks.append(_check("nr_ci_full_star_recall", recall,
                         f"≥{ADJ_NR_FULL_RECALL_MIN} か light − max({ADJ_NR_FULL_RECALL_DROP_MAX}, {ADJ_NR_FULL_RECALL_DROP_POINTS}/n) 以上",
                         recall_ok if fin(recall) and has_stars else None, light_star_recall=light_recall, recall_drop_max=drop_max))
    hf, hf_cam = _num(ci.get("nr_full", {}).get("sky_hf_ratio")), _num(camera.get("sky_hf_ratio"))
    hf_light = _num(ci.get("identity", {}).get("sky_hf_ratio"))
    hf_ok = hf >= ADJ_NR_FULL_HF_MIN * hf_cam or (fin(hf_light) and hf >= ADJ_NR_FULL_HF_VS_LIGHT_MIN * hf_light)
    checks.append(_check("nr_ci_full_sky_hf_vs_camera", hf / hf_cam if fin(hf, hf_cam) and hf_cam > 0 else float("nan"),
                         f"≥{ADJ_NR_FULL_HF_MIN} か light 比 ≥{ADJ_NR_FULL_HF_VS_LIGHT_MIN}", hf_ok if fin(hf, hf_cam) else None,
                         hf_vs_light=hf / hf_light if fin(hf, hf_light) and hf_light > 0 else float("nan")))
    # 白点: impulse_vs_full ≤ 10、ガードは nr_off_sh0 だけで発動
    for variant in ADJ_WHITE_DOT_VARIANTS:
        imp = _num(ci.get(variant, {}).get("impulse_vs_full"))
        checks.append(_check(f"white_dot_{variant}_impulse_vs_full", imp, f"≤{ADJ_IMPULSE_MAX}", imp <= ADJ_IMPULSE_MAX if fin(imp) else None))
        guards = ci.get(variant, {}).get("ci_adj_guards")
        if isinstance(guards, str):
            fired = ADJ_WHITE_DOT_GUARD in guards.split(";")
            expect = variant == "nr_off_sh0"
            checks.append(_check(f"white_dot_{variant}_guard", guards or "(none)",
                                 ADJ_WHITE_DOT_GUARD if expect else "発動しない", fired == expect))
    # highlight_recovery は CLI 0.6.0（#81）から写像で効く → ignored は空（0.5.0 までは ["highlight_recovery"]）
    ignored = ci.get("identity", {}).get("ci_adj_ignored")
    if isinstance(ignored, str):
        checks.append(_check("adjustments_ignored_empty", ignored or "(none)", "(none)", ignored == ""))
    return checks


def adjustments_plan(ctx: "RunContext", capture_ids: list[str]) -> list[dict[str, Any]]:
    """--adjustments で投げるリクエスト / ワーカー呼び出しの一覧（純粋。CLI は呼ばない）。--dry-run で書き出す。"""
    preset = ci_preset_adjustments(frozen_presets.DEFAULT_PRESET)
    plan: list[dict[str, Any]] = []
    for cid in capture_ids:
        arw = source_file(ctx.source_root, cid, "ARW")

        def ci(variant: str, mode: str, **kw: Any) -> None:
            out = coreimage_output_path(ctx, cid, mode, True, variant=variant, output_format=kw.get("output_format", "jpeg"))
            request = coreimage_request(arw, out, mode, True, decoder_version="9", profile="apple_default",
                                        max_edge=ctx.max_edge, **kw)
            plan.append({"capture_id": cid, "engine": "coreimage", "variant": variant, "mode": mode,
                         "request": request, "problems": validate_adjustments_request(request)})

        ci("adj_default", "preview")  # 既定リクエスト（adjustments 無し）の回帰
        for variant, *_ in ADJ_SWEEP_VARIANTS:
            ci(f"adj_{variant}", "preview", adjustments=adjustments_for(variant))
            plan.append({"capture_id": cid, "engine": "rawpy", "variant": variant, "mode": "preview",
                         "worker_adjustments": adjustments_for(variant), "problems": []})
        ci("adj_nr_off_sh0", "preview", adjustments=adjustments_for("nr_off_sh0"), overrides=ADJ_WHITE_DOT_VARIANTS["nr_off_sh0"])
        for variant, overrides in ADJ_WHITE_DOT_VARIANTS.items():
            ci(f"adj_{variant}", "full", adjustments=adjustments_for(variant), overrides=overrides or None)
        ci("or_base", "preview", output_format="tiff16", output_color_space="srgb")
        for variant, post, _kind in ADJ_ORACLE_VARIANTS:
            ci(variant, "preview", output_format="tiff16", output_color_space="srgb",
               adjustments=oracle_adjustments(post, preset))
    return plan


def develop_adjustment_variants(ctx: RunContext, capture_id: str, workers: int = 4) -> dict[str, dict[str, Any]]:
    """1 カット分の現像（rawpy 掃引・CI 掃引・白点・既定・オラクル）。キーは "rawpy:<v>" / "ci:<v>" / "ci_full:<v>" / "or:<v>"。"""
    from concurrent.futures import ThreadPoolExecutor

    # オラクル or_preset は CI 値（presets.json）の既定プリセット（#81 設計書 §6）。
    preset = ci_preset_adjustments(frozen_presets.DEFAULT_PRESET)
    ci_kw = {"decoder_version": "9", "profile": "apple_default"}
    jobs: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs["ci:default"] = pool.submit(run_coreimage, ctx, capture_id, "preview", True, variant="adj_default", **ci_kw)
        for variant, *_ in ADJ_SWEEP_VARIANTS:
            adj = adjustments_for(variant)
            jobs[f"rawpy:{variant}"] = pool.submit(run_rawpy, ctx, capture_id, "preview", adjustments=adj, variant=variant)
            jobs[f"ci:{variant}"] = pool.submit(run_coreimage, ctx, capture_id, "preview", True,
                                                variant=f"adj_{variant}", adjustments=adj, **ci_kw)
        jobs["ci:nr_off_sh0"] = pool.submit(run_coreimage, ctx, capture_id, "preview", True, variant="adj_nr_off_sh0",
                                            adjustments=adjustments_for("nr_off_sh0"),
                                            overrides=ADJ_WHITE_DOT_VARIANTS["nr_off_sh0"], **ci_kw)
        for variant, overrides in ADJ_WHITE_DOT_VARIANTS.items():
            jobs[f"ci_full:{variant}"] = pool.submit(run_coreimage, ctx, capture_id, "full", True, variant=f"adj_{variant}",
                                                     adjustments=adjustments_for(variant), overrides=overrides or None, **ci_kw)
        jobs["or:base"] = pool.submit(run_coreimage, ctx, capture_id, "preview", True, variant="or_base",
                                      output_format="tiff16", output_color_space="srgb", **ci_kw)
        for variant, post, _kind in ADJ_ORACLE_VARIANTS:
            jobs[f"or:{variant}"] = pool.submit(run_coreimage, ctx, capture_id, "preview", True, variant=variant,
                                                output_format="tiff16", output_color_space="srgb",
                                                adjustments=oracle_adjustments(post, preset), **ci_kw)
        return {name: future.result() for name, future in jobs.items()}


def read_provenance(row: dict[str, Any]) -> dict[str, Any]:
    """run_coreimage が出力の隣に残した CLI 応答 JSON（無ければ空）。"""
    if row.get("error") or not row.get("output"):
        return {}
    path = Path(row["output"]).with_suffix(".json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_adj_baseline() -> dict[str, dict[str, Any]]:
    """既定リクエスト回帰の baseline（capture_id -> applied）。無ければ空。"""
    try:
        data = json.loads(ADJ_BASELINE_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {c["capture_id"]: c.get("applied") or {} for c in data.get("captures", []) if c.get("capture_id")}


def adjustments_capture(
    ctx: RunContext,
    capture_id: str,
    features: dict[str, Any],
    category: str,
    baseline: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """1 カットの掃引・オラクル・白点・回帰。戻り値は (指標行, 差分行, オラクル行, チェック行)。"""
    runs = develop_adjustment_variants(ctx, capture_id)
    camera = read_rgb_upright(source_file(ctx.source_root, capture_id, "JPG"))
    ch, cw = camera.shape[:2]
    target = fit_size(cw, ch, ctx.max_edge)
    cam_small = resize_to(camera, target)
    preview_rows = {k: v for k, v in runs.items() if k.startswith(("rawpy:", "ci:"))}
    images, rows_by_key = load_variant_images(preview_rows, target)
    images["camera"] = cam_small
    iso = features.get("iso")
    base = {"capture_id": capture_id, "mode": "preview", "category": category, "iso": iso,
            "exposure_time": features.get("exposure_time"), "lens_model": features.get("lens_model"),
            "width": target[0], "height": target[1]}

    # --- 基本指標（全バリアント同じ画素: 近中立タイル・暗部はカメラ JPEG で決める）
    neutral = neutral_tile_mask(cam_small)
    dark = dark_pixel_mask(cam_small)
    measured: dict[str, dict[str, Any]] = {k: adjustment_image_metrics(img, neutral, dark) for k, img in images.items()}
    # exposure: 各エンジン内で identity との露出差（--bracket と同じ exposure_ratio_stops）
    for engine_name in ("rawpy", "ci"):
        base_key = f"{engine_name}:identity"
        if base_key not in images:
            continue
        measured[base_key]["exposure_stops"] = 0.0
        for variant in ("exp_m1", "exp_p1"):
            if f"{engine_name}:{variant}" in images:
                measured[f"{engine_name}:{variant}"]["exposure_stops"] = exposure_ratio_stops(
                    images[base_key], images[f"{engine_name}:{variant}"])["stops"]
    # --- シャープ: エッジ幅（カメラ比）
    cam_gray = cv2.cvtColor(cam_small, cv2.COLOR_RGB2GRAY).astype(np.float32)
    ref = edge_reference(cam_gray)
    for key in ("camera", "rawpy:identity", "rawpy:sharp_08", "rawpy:sharp_16", "ci:identity", "ci:sharp_08", "ci:sharp_16"):
        if key in images:
            measured[key].update(edge_width_vs_camera(ref, cam_gray, cv2.cvtColor(images[key], cv2.COLOR_RGB2GRAY).astype(np.float32)))
    # --- NR / 白点: 夜景指標（空・星・impulse）。impulse_vs_full は同じリクエストの原寸レンダを縮めたものが基準
    full_imgs = {f"ci:{k.split(':', 1)[1]}": read_rgb_upright(Path(r["output"]))
                 for k, r in runs.items() if k.startswith("ci_full:") and not r.get("error")}
    full_refs = {k: resize_to(img, target) for k, img in full_imgs.items()}
    night_keys = ["camera", *[f"{e}:{v}" for e in ("rawpy", "ci") for v in ("identity", "nr_off", "nr_full")], "ci:nr_off_sh0"]
    exif = {k: features.get(k) for k in ("iso", "exposure_time", "f_number")}
    night, _ = night_measure(images, exif, night_keys, full_refs)
    for key, values in night.items():
        measured[key].update({k: values.get(k) for k in (
            "sky_noise", "sky_noise_ratio", "sky_chroma_noise", "sky_hf_ratio", "star_recall", "star_recall_n",
            "impulse_count", "impulse_vs_full", "point_count")})
    cam_noise = measured["camera"].get("flat_noise")

    metric_rows: list[dict[str, Any]] = []
    by_engine: dict[str, dict[str, dict[str, Any]]] = {"rawpy": {}, "ci": {}}
    for key in sorted(set(rows_by_key) | {"camera"}):
        engine_name, _, variant = key.partition(":") if key != "camera" else ("camera", "", "camera")
        row: dict[str, Any] = {**base, "engine": engine_name, "variant": variant or key, **rows_by_key.get(key, {})}
        if key in measured:
            row.update(measured[key])
            if isinstance(cam_noise, float) and cam_noise > 0 and isinstance(row.get("flat_noise"), float):
                row["flat_noise_ratio"] = row["flat_noise"] / cam_noise
        else:
            row.setdefault("error", "no image")
        if engine_name in by_engine:
            by_engine[engine_name][variant] = row
        metric_rows.append(row)
    deltas = [{**base, **d} for d in sweep_deltas(by_engine)]

    # --- オラクル（tiff16 sRGB 同士、half float のみ。JPEG を挟まない）
    oracle_rows: list[dict[str, Any]] = []
    or_base = runs.get("or:base", {})
    base01 = read_tiff16_rgb(Path(or_base["output"])) if not or_base.get("error") else None
    worst: tuple[float, str, np.ndarray, np.ndarray] | None = None
    preset = ci_preset_adjustments(frozen_presets.DEFAULT_PRESET)
    for variant, post, kind in ADJ_ORACLE_VARIANTS:
        run = runs.get(f"or:{variant}", {})
        orow: dict[str, Any] = {**base, "variant": variant, "kind": kind, "gates": kind == "tone",
                                "wall_s": run.get("wall_s"), "engine_elapsed_s": run.get("engine_elapsed_s")}
        adj = oracle_adjustments(post, preset)
        orow.update({f"adj_{k}": adj[k] for k in ("shadow_lift", "brightness", "contrast", "saturation", "sharpness")})
        if base01 is None or run.get("error"):
            orow["error"] = run.get("error") or or_base.get("error") or "base unavailable"
            oracle_rows.append(orow)
            continue
        ci01 = read_tiff16_rgb(Path(run["output"]))
        if ci01.shape != base01.shape:
            orow["error"] = f"size mismatch {ci01.shape} vs base {base01.shape}"
            oracle_rows.append(orow)
            continue
        usm = ((read_provenance(run).get("adjustments_applied") or {}).get("post") or {}).get("usm")
        oracle01 = post_chain_oracle(base01, adj, usm if kind == "tone+usm" else None)
        orow.update({k: run.get(k) for k in ("ci_usm_sigma", "ci_usm_gain", "ci_usm_coring")})
        orow.update(oracle_compare(ci01, oracle01))
        oracle_rows.append(orow)
        if worst is None or orow["de76_p99"] > worst[0]:
            worst = (orow["de76_p99"], variant, ci01, oracle01)

    # --- 既定リクエストの回帰と、identity の applied がそれと一致するか
    checks = [{**base, **c} for c in adjustment_checks(by_engine["ci"], by_engine["rawpy"], measured.get("camera", {}), deltas)]
    default_prov = read_provenance(runs.get("ci:default", {}))
    identity_prov = read_provenance(runs.get("ci:identity", {}))
    if default_prov:
        checks.append({**base, **_check("default_request_no_adjustments_applied", "adjustments_applied" in default_prov,
                                         "キー自体が無い", "adjustments_applied" not in default_prov)})
        if capture_id in baseline:
            diffs = applied_diff(baseline[capture_id], default_prov.get("applied") or {})
            checks.append({**base, **_check("default_request_vs_baseline", len(diffs), "差分 0 件", not diffs, diffs="; ".join(diffs))})
        if identity_prov:
            diffs = applied_diff(default_prov.get("applied") or {}, identity_prov.get("applied") or {}, rel_tol=1e-4)
            checks.append({**base, **_check("identity_applied_equals_default", len(diffs), "差分 0 件（デコード段が恒等）",
                                             not diffs, diffs="; ".join(diffs))})
    # --- 処理時間: adjustments 込みの preview（JPEG）が既定リクエストの 1.5 倍以内
    t_default = _num(runs.get("ci:default", {}).get("engine_elapsed_s"))
    t_adj = median_or_nan(runs[f"ci:{v}"].get("engine_elapsed_s") for v, *_ in ADJ_SWEEP_VARIANTS if f"ci:{v}" in runs)
    ratio = t_adj / t_default if math.isfinite(t_default) and t_default > 0 and math.isfinite(t_adj) else float("nan")
    checks.append({**base, **_check("elapsed_ratio_vs_default", ratio, f"≤{ADJ_ELAPSED_RATIO_MAX}",
                                     ratio <= ADJ_ELAPSED_RATIO_MAX if math.isfinite(ratio) else None)})

    adjustments_sheets(ctx, capture_id, images, measured, worst, f"{capture_id}  preview {target[0]}x{target[1]}  ISO {iso}")
    return metric_rows, deltas, oracle_rows, checks


def adjustments_sheets(
    ctx: RunContext,
    capture_id: str,
    images: dict[str, np.ndarray],
    measured: dict[str, dict[str, Any]],
    worst: tuple[float, str, np.ndarray, np.ndarray] | None,
    caption: str,
) -> None:
    """パラメータごとの全体シート（上段 rawpy、下段 CI）、シャープ / NR の等倍クロップ、オラクルの最大差分（EXIF 無し）。"""
    sample = images.get("camera")
    if sample is None:
        return
    small = fit_size(sample.shape[1], sample.shape[0], 360)
    groups: dict[str, list[str]] = {}
    for variant, param, _value, _label in ADJ_SWEEP_VARIANTS:
        if param is not None:
            groups.setdefault(param, ["identity"]).append(variant)
    for param, variants in groups.items():
        stacked = []
        for engine_name in ("rawpy", "ci"):
            panels = [(f"{engine_name} {ADJ_SWEEP_LABELS[v]}", resize_to(images[f"{engine_name}:{v}"], small))
                      for v in variants if f"{engine_name}:{v}" in images]
            if panels:
                stacked.append(labeled_row(panels))
        if stacked:
            width = max(r.shape[1] for r in stacked)
            sheet = np.vstack([np.pad(r, ((0, 0), (0, width - r.shape[1]), (0, 0)), constant_values=24) for r in stacked])
            save_plain_jpeg(labeled_row([("", sheet)], f"{caption}  {param}"), ctx.out / "sheets" / f"{capture_id}_adj_{param}.jpg")
    # 等倍クロップ（シャープ: ディテール最大の箱、NR / 白点: 空の箱を 2 倍）
    gray = cv2.cvtColor(sample, cv2.COLOR_RGB2GRAY)
    box = detail_box(gray, 200)
    for name, keys, factor, crop_box in (
        ("sharpness", ("camera", "rawpy:sharp_08", "rawpy:identity", "rawpy:sharp_16", "ci:sharp_08", "ci:identity", "ci:sharp_16"), 1, box),
        ("nr", ("camera", "rawpy:nr_off", "rawpy:identity", "rawpy:nr_full", "ci:nr_off", "ci:identity", "ci:nr_full", "ci:nr_off_sh0"), 2,
         best_box_by_mask(flat_tile_mask_pixels(gray), 120)),
    ):
        x0, y0, x1, y1 = crop_box
        panels = []
        for key in keys:
            if key not in images:
                continue
            m = measured.get(key, {})
            label = f"{key}\nedge {_num(m.get('edge_width_ratio')):.2f} fn {_num(m.get('flat_noise')):.2f}" if name == "sharpness" else \
                f"{key}\nsky {_num(m.get('sky_noise')):.2f} imp {m.get('impulse_vs_full', '-')}"
            panels.append((label, magnify(images[key][y0:y1, x0:x1], factor)))
        if panels:
            save_plain_jpeg(labeled_row(panels[:4], f"{caption}  {name} x{factor}"), ctx.out / "sheets" / f"{capture_id}_adj_{name}_a.jpg", quality=95)
            if panels[4:]:
                save_plain_jpeg(labeled_row(panels[4:], f"{caption}  {name} x{factor}"), ctx.out / "sheets" / f"{capture_id}_adj_{name}_b.jpg", quality=95)
    if worst is not None:
        _p99, variant, ci01, oracle01 = worst
        h, w = ci01.shape[:2]
        diff = np.abs(ci01 - oracle01).max(axis=2)
        cy, cx = np.unravel_index(int(np.argmax(cv2.blur(diff, (31, 31)))), diff.shape)
        s = min(200, h, w)
        x0, y0 = int(min(max(0, cx - s // 2), w - s)), int(min(max(0, cy - s // 2), h - s))
        crop = (slice(y0, y0 + s), slice(x0, x0 + s))
        heat = to8(np.repeat(np.clip(diff[crop] * 32.0, 0, 1)[..., None], 3, axis=2))
        panels = [("CI", magnify(to8(ci01[crop]), 2)), ("oracle (Python post)", magnify(to8(oracle01[crop]), 2)),
                  ("|diff| x32", magnify(heat, 2))]
        save_plain_jpeg(labeled_row(panels, f"{caption}  oracle worst {variant}"), ctx.out / "sheets" / f"{capture_id}_adj_oracle_worst.jpg", quality=95)


def flat_tile_mask_pixels(gray8: np.ndarray) -> np.ndarray:
    """空（平坦タイル）の画素マスク。NR シートの箱選びに使う。"""
    return tile_mask_to_pixels(flat_tile_mask(gray8), gray8.shape)


def check_summary(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """チェック名ごとの合格 / 不合格 / 測れず の件数（純粋）。"""
    out: dict[str, dict[str, Any]] = {}
    for c in checks:
        item = out.setdefault(c["check"], {"check": c["check"], "threshold": c.get("threshold"), "pass": 0, "fail": 0, "n_a": 0, "failed": []})
        if c.get("pass") is True:
            item["pass"] += 1
        elif c.get("pass") is False:
            item["fail"] += 1
            item["failed"].append(c.get("capture_id"))
        else:
            item["n_a"] += 1
    return [out[k] for k in sorted(out)]


def run_adjustments(ctx: RunContext, selection: list[dict[str, Any]], features_by_id: dict[str, dict[str, Any]]) -> None:
    metric_rows: list[dict[str, Any]] = []
    delta_rows: list[dict[str, Any]] = []
    oracle_rows: list[dict[str, Any]] = []
    check_rows: list[dict[str, Any]] = []
    started = time.monotonic()
    baseline = load_adj_baseline()
    for index, row in enumerate(selection, 1):
        cid = row["capture_id"]
        ctx.say(f"[adjustments] ({index}/{len(selection)}) {cid}")
        try:
            m, d, o, c = adjustments_capture(ctx, cid, features_by_id[cid], row.get("category", "other"), baseline)
            metric_rows += m
            delta_rows += d
            oracle_rows += o
            check_rows += c
            for r in m + o:
                if r.get("error"):
                    ctx.say(f"  ERROR {r.get('engine', 'oracle')}:{r['variant']}: {str(r['error'])[:300]}")
            failed = [x["check"] for x in c if x.get("pass") is False]
            ctx.say(f"  checks: 不合格 {len(failed)} 件" + (f"（{', '.join(failed)}）" if failed else ""))
        except Exception as error:  # 1 カットの失敗で全体を止めない
            metric_rows.append({"capture_id": cid, "mode": "preview", "engine": "harness", "variant": "harness",
                                "error": f"{type(error).__name__}: {error}"})
            ctx.say(f"  ERROR {type(error).__name__}: {error}")
        write_rows_csv(ctx.out / "adjustments_metrics.csv", metric_rows, ("capture_id", "engine", "variant"))
        if delta_rows:
            write_rows_csv(ctx.out / "adjustments_deltas.csv", delta_rows, ("capture_id", "variant", "metric"))
        if oracle_rows:
            write_rows_csv(ctx.out / "adjustments_oracle.csv", oracle_rows, ("capture_id", "variant", "kind"))
        if check_rows:
            write_rows_csv(ctx.out / "adjustments_checks.csv", check_rows, ("capture_id", "check", "pass", "value", "threshold"))

    by_variant = summarize_sharpness(metric_rows, ADJ_KEYS, ("engine", "variant"))
    delta_keys = ("delta_rawpy", "delta_ci", "ratio")
    by_delta = summarize_sharpness(delta_rows, delta_keys, ("param", "variant", "metric"))
    oracle_keys = ("de76_median", "de76_p99", "de76_max", "chan_abs_p99", "chan_abs_max")
    by_oracle = summarize_sharpness(oracle_rows, oracle_keys, ("kind", "variant"))
    checks = check_summary(check_rows)
    errors = [r for r in metric_rows + oracle_rows if r.get("error")]
    summary = {
        "out": str(ctx.out),
        "identity": ADJ_IDENTITY,
        "sweep_variants": [{"variant": v, "param": p, "value": val, "adjustments": adjustments_for(v)} for v, p, val, _ in ADJ_SWEEP_VARIANTS],
        "white_dot_variants": ADJ_WHITE_DOT_VARIANTS,
        "oracle_variants": [{"variant": v, "kind": k, "adjustments": oracle_adjustments(p, ci_preset_adjustments(frozen_presets.DEFAULT_PRESET))}
                            for v, p, k in ADJ_ORACLE_VARIANTS],
        "ci_settings": {"decoder_version": "9", "profile": "apple_default", "lens_correction": True, "max_edge": ctx.max_edge},
        "thresholds": {
            "oracle_de76_median_max": ORACLE_DE76_MEDIAN_MAX, "oracle_channel_p99_max": ORACLE_CHANNEL_P99_MAX,
            "exposure_stop_tol": ADJ_EXPOSURE_STOP_TOL, "wb_ratio_range": ADJ_WB_RATIO_RANGE,
            "sharp_baseline_width": ADJ_SHARP_BASELINE_WIDTH, "sharp_baseline_tol": ADJ_SHARP_BASELINE_TOL,
            "sharp16_noise_max": ADJ_SHARP16_NOISE_MAX, "nr_full_recall_min": ADJ_NR_FULL_RECALL_MIN,
            "nr_full_hf_min": ADJ_NR_FULL_HF_MIN, "impulse_max": ADJ_IMPULSE_MAX, "elapsed_ratio_max": ADJ_ELAPSED_RATIO_MAX,
            "exposure_ratio_range": ADJ_EXPOSURE_RATIO_RANGE, "nr_full_recall_drop_max": ADJ_NR_FULL_RECALL_DROP_MAX,
            "nr_full_recall_drop_points": ADJ_NR_FULL_RECALL_DROP_POINTS,
            "nr_full_hf_vs_light_min": ADJ_NR_FULL_HF_VS_LIGHT_MIN, "impulse_reference_ratio": IMPULSE_REFERENCE_RATIO,
            "rb_pixel_range": RB_PIXEL_RANGE,
        },
        "baseline": str(ADJ_BASELINE_JSON.relative_to(PROJECT_ROOT)),
        "regression_script": "scripts/coreimage-regression-applied.sh <ARW のフォルダ>",
        "selection_count": len(selection),
        "elapsed_total_s": time.monotonic() - started,
        "errors": errors,
        "checks": checks,
        "by_variant": by_variant,
        "by_delta": by_delta,
        "by_oracle": by_oracle,
    }
    (ctx.out / "adjustments_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    md = "## checks（設計書 §3.7 の暫定の目安）\n\n| check | threshold | pass | fail | n/a | failed |\n|---|---|---|---|---|---|\n"
    md += "".join(f"| {c['check']} | {c['threshold']} | {c['pass']} | {c['fail']} | {c['n_a']} | {', '.join(map(str, c['failed']))} |\n" for c in checks)
    md += "\n## oracle（tiff16 sRGB、CI 後段 vs Python 後段）\n\n" + table_md(by_oracle, ("kind", "variant"), oracle_keys, digits=4)
    md += "\n## sweep deltas（identity との差の中央値、ratio = ci / rawpy）\n\n" + table_md(by_delta, ("param", "variant", "metric"), delta_keys, digits=4)
    md += "\n## by engine x variant\n\n" + table_md(by_variant, ("engine", "variant"), (
        "exposure_stops", "log2_mean_linear", "rb_ln_neutral", "dark_mean_luma", "mean_luma", "std_luma", "ab_std",
        "edge_width_ratio", "flat_noise_ratio", "sky_noise", "star_recall", "impulse_vs_full", "engine_elapsed_s"))
    (ctx.out / "adjustments_tables.md").write_text(md, encoding="utf-8")
    (ctx.out / "run.log").write_text("\n".join(ctx.log) + "\n", encoding="utf-8")
    n_fail = sum(c["fail"] for c in checks)
    ctx.say(f"[adjustments] 完了: {ctx.out}（エラー {len(errors)} 件、不合格チェック {n_fail} 件）")


def cli_probe(cli: Path, *args: str) -> dict[str, Any]:
    """CLI を ``--version`` / ``--capabilities`` で呼んで最後の JSON 行を返す（失敗時は {"error": ...}）。"""
    if not cli.is_file():
        return {"error": f"CLI がありません: {cli}"}
    try:
        proc = subprocess.run([str(cli), *args], capture_output=True, text=True, timeout=60)
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as error:  # noqa: BLE001
        return {"error": f"{type(error).__name__}: {error}"}



def adjustments_dry_run(ctx: RunContext, capture_ids: list[str]) -> None:
    """--adjustments --dry-run: CLI を呼ばずにリクエストを組み、§4.1 の契約検査と CLI 版の確認だけ行う。"""
    plan = adjustments_plan(ctx, capture_ids)
    problems = [(p["capture_id"], p["variant"], p["mode"], msg) for p in plan for msg in p["problems"]]
    version = cli_probe(ctx.cli, "--version")
    capabilities = cli_probe(ctx.cli, "--capabilities") if not version.get("error") else {}
    cli_problem = adjustments_cli_problem(version, capabilities)
    payload = {
        "capture_ids": capture_ids,
        "cli": str(ctx.cli),
        "cli_version": version,
        "cli_ready": cli_problem is None,
        "cli_problem": cli_problem,
        "counts": {
            "coreimage": sum(1 for p in plan if p["engine"] == "coreimage"),
            "rawpy": sum(1 for p in plan if p["engine"] == "rawpy"),
        },
        "contract_problems": [{"capture_id": c, "variant": v, "mode": m, "problem": msg} for c, v, m, msg in problems],
        "plan": plan,
    }
    (ctx.out / "adjustments_plan.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    ctx.say(f"[adjustments] dry-run: CI {payload['counts']['coreimage']} リクエスト / rawpy {payload['counts']['rawpy']} 回"
            f" → {ctx.out / 'adjustments_plan.json'}")
    if cli_problem:
        ctx.say(f"[adjustments] 注意: 実行時はここで止まります — {cli_problem}")
    if problems:
        for c, v, m, msg in problems:
            ctx.say(f"  契約違反 {c} {v} {m}: {msg}")
        raise SystemExit(f"--adjustments のリクエストが設計書 §4.1 の契約に反します（{len(problems)} 件）")


def environment_info(ctx: RunContext, probe: Path | None) -> dict[str, Any]:
    def cli_json(*args: str) -> Any:
        try:
            proc = subprocess.run([str(ctx.cli), *args], capture_output=True, text=True, timeout=60)
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception as error:  # noqa: BLE001
            return {"error": str(error)}

    cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    mem = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip()
    import PIL

    info: dict[str, Any] = {
        "machine": {"cpu": cpu, "memory_bytes": int(mem) if mem.isdigit() else mem, "platform": platform.platform()},
        "macos": platform.mac_ver()[0],
        "python": platform.python_version(),
        "pillow": PIL.__version__,
        "opencv": cv2.__version__,
        "numpy": np.__version__,
        "legacy_rawpy": ctx.legacy_rawpy,
        "coreimage_cli": str(ctx.cli),
        "coreimage_version": cli_json("--version"),
        "coreimage_capabilities": cli_json("--capabilities", *( [str(probe)] if probe else [] )),
        "coreimage_decoder_version": CI_DECODER_VERSION,
        "max_edge": ctx.max_edge,
    }
    if ctx.legacy_rawpy:
        # 旧経路（アーカイブ）を opt-in したときだけ rawpy / 旧エンジンの来歴を残す。
        rawpy = legacy.import_module("rawpy", "環境情報（rawpy）")
        engine = legacy.import_module("local_raw.engine", "環境情報（旧エンジン）")
        info.update({"rawpy": rawpy.__version__, "libraw": getattr(rawpy, "libraw_version", None),
                     "engine_version": engine.ENGINE_VERSION, "rawpy_preset": engine.DEFAULT_PRESET})
    return info


# =============================================================================
# 診断モード --diagnosis（Issue #92、設計書 coreimage-highlight-diagnosis.md §5〜§10）
# =============================================================================
#
# センサー CFA（S0）→ 線形段（S1）→ トーン（S2）→ 色域マッピング（S3）→ 16bit（S4）→ 8bit JPEG（S5）の
# どこで白飛びが生まれ、どの段のレバーで戻せるかを ROI ごとに帰属させる。CLI 0.4.0 の ``diagnostics``
# （設計書 §4）で float ダンプを取り、ROI 統計は Python 側で計算する（§13 判断 10）。
# rawpy は CFA 飽和の計測（§5）と比較用の現像にだけ使う（アプリの現像経路には入れない）。

DIAGNOSIS_MIN_CLI = (0, 4, 0)
#: 16bit で白になる線形値（設計書 §4.4 の frac_ge_1 と同じ）。
WHITE16_LINEAR = 1.0 - 1.0 / 65535.0
#: 8bit sRGB で 255 に丸まる線形値の下限（254.5/255 を sRGB 復号）。帰属規則 3 の「8bit 量子化」の判定に使う。
WHITE8_LINEAR = float(((254.5 / 255.0 + 0.055) / 1.055) ** 2.4)
#: 2×2 セル飽和マップのビット（色別）。
CFA_BIT = {"r": 1, "g": 2, "b": 4}
#: §8.3 / §13 判断 5: 「同じ明るさ」の許容幅と、タスク C 合否の階調比。
MID_P50_TOL = 0.03
GRAD_TEXTURE_RATIO_MIN = 0.7
#: §10.2 のシーン判定「階調が実際に戻る」と §9 のタスク C 実施条件（S1/S2 に階調が残る）の閾値。
GRAD_RECOVER_MIN = 0.5
FG_P01_DROP_MAX = -0.02
MAGENTA_RATE_MAX = 0.01
#: §10.1: K が ROI のこの割合未満なら「クリップなし」。最大の帰属がこの割合以上で 1 位、2 位がこの割合以上で「混合」。
ATTR_MIN_CLIP_FRACTION = 0.01
ATTR_MAJOR_FRACTION = 0.5
ATTR_SECOND_FRACTION = 0.3
#: §10.1 の帰属カテゴリ（規則の順）。レポート §8-3（オーナー判断）で名称と内訳を改めた。旧名との対応:
#: - quant8（8bit 量子化。白の手前 1/255）= 旧 gamut_output（色域・SDR 出力）。S2 が [0,1] で S3 が 8bit 255 に丸まる値。
#:   中身は色域の損失ではなく白の手前の丸め（レポート §3.3）なので改名した。規則の判定式は変えていない。
#: - jpeg_encode（JPEG 符号化）+ unverifiable（検証不能）= 旧 unverifiable。S3 が 8bit 255 未満なのに K に入った画素のうち、
#:   S3 ≥ JPEG_NEAR_WHITE_LINEAR（白の手前で止まっている）を jpeg_encode、それ未満と照合不能を unverifiable に分けた。
ATTR_CATEGORIES = ("sensor", "decoder_tone", "quant8", "jpeg_encode", "unverifiable")
#: 規則 4 の分割: S3（線形、いずれかのチャンネル）がこの値以上なら「JPEG 符号化」（sRGB 8bit で 253 付近から上）。
JPEG_NEAR_WHITE_LINEAR = 0.98
#: §10.2 の「S0 も飽和していない」: cloud_grad の CFA セル飽和率がこれ未満。
SCENE_SENSOR_FREE_MAX = 0.01
#: §8.4: 標本化したトーン曲線を S1 に掛けた値と S2 の ΔL（線形輝度）p99 の許容。
CURVE_CHECK_TOL = 0.01
#: 定数置換（diagnostics.linear_space_filter constant）で標本化した boost 1（gamut OFF）の応答。
#: 2026-10-21（T3）に DSC04705 / DSC04473 / DSC04432 / DSC04532（ISO 100 / 200 / 2000 / 100）で 19 点を標本化し、
#: 4 カットとも 1e-5 以内で同値だった（画像に依存しない大域的な曲線）。0.0025〜0.015 は影側の追加標本（DSC04705）。
#: 設計書 §2.4 の 9 点（1 カット）から置き換えた理由: 9 点の PCHIP では 0.3〜0.7 の補間誤差が ΔL 0.015 になり、
#: §8.4 の許容 0.01 を超えて「局所適応」と誤判定した（実測の曲線は大域的）。2.7 超は最後の傾き（≈0.14）で外挿する。
TONE_CURVE_BOOST1 = ((0.0, 0.0), (0.0025, 0.00191), (0.005, 0.00405), (0.01, 0.00897), (0.015, 0.01472),
                     (0.02, 0.02126), (0.05, 0.07436), (0.1, 0.195), (0.18, 0.38745), (0.3, 0.59795),
                     (0.4, 0.71799), (0.5, 0.805), (0.6, 0.87147), (0.7, 0.92126), (0.8, 0.9573),
                     (0.9, 0.98257), (0.95, 0.99208), (1.0, 1.0), (1.1, 1.0145), (1.2, 1.029),
                     (1.5, 1.0725), (2.0, 1.145), (2.5, 1.2175), (2.7, 1.2465))
#: cloud_grad の帯に足す低彩度条件（HSV の S の上限、レポート §8-4）。値の根拠は diagnosis_rois のコメント。
CLOUD_GRAD_MAX_SAT = 0.2
DIAG_ROI_NAMES = ("sky_bright", "cloud_grad", "clip_edge", "fg_dark", "midtone")
#: §7.2: sky_bright の平行移動を流用する ROI（この順）。
SKY_SHIFT_SOURCES = ("midtone", "clip_edge", "cloud_grad")
#: grad_* / sky_* を数える最小画素数（これ未満は欠測）。
DIAG_MIN_PIXELS = 50
#: §4.5 post_filters の許可リスト: name -> {param: ("scalar", lo, hi) | ("vec2", (xlo, xhi), (ylo, yhi)) | ("vec4", lo, hi)}
POST_FILTER_ALLOWLIST: dict[str, dict[str, tuple]] = {
    "CIHighlightShadowAdjust": {
        "inputHighlightAmount": ("scalar", 0.0, 1.0),
        "inputShadowAmount": ("scalar", -1.0, 1.0),
        "inputRadius": ("scalar", 0.0, 10.0),
    },
    "CIToneCurve": {f"inputPoint{i}": ("vec2", (0.0, 1.0), (0.0, 2.0)) for i in range(5)},
    "CIExposureAdjust": {"inputEV": ("scalar", -5.0, 5.0)},
    "CIColorClamp": {"inputMinComponents": ("vec4", None, None), "inputMaxComponents": ("vec4", None, None)},
}
#: post_filters の最大個数（T1 CLI 0.4.0 の実装上の上限）。
POST_FILTERS_MAX = 16
DIAG_REQUEST_TOP_KEYS = frozenset({
    "input", "output", "mode", "decoder_version", "settings", "format", "output_color_space", "max_edge",
    "adjustments", "diagnostics",
})
DIAG_KEYS = frozenset({"stats", "stats_color_space", "dump_float", "linear_space_filter", "post_filters", "skip_write"})
DIAG_COLOR_SPACES = ("linear_srgb", "linear_p3")

#: タスク C の候補（§9）。C1 は adjustments（exposure + 後段の lift）、C2 は settings.exposure_ev +
#: CIHighlightShadowAdjust、C3 は線形読み出し（boost 0 + gamut OFF）+ 自前トーン圧縮。
#: C1 の brightness は 1.0 に固定する。後段の brightness は sRGB ガンマ空間の乗算（geoframe_tone）なので 1.1 でも
#: ガンマ値 0.91（線形 0.80）以上が白に戻り、計測 1 回目（2026-10-21）では exposure −0.7 で 0 だった sky_bright の
#: clip8 が 0.78〜0.98 に戻った。lift（ピーク x≈1/3、x=1 で 0）だけで中間調を戻す。lift 40 / 55 は 10 カットの掃引
#: （lift 25〜55）で |mid_p50_diff| ≤ 0.03 を満たすカットが最多だった値。
C1_SPECS = {
    "c1_ev-07_lift": {"exposure": -0.7, "shadow_lift": 40.0, "brightness": 1.0},
    "c1_ev-10_lift": {"exposure": -1.0, "shadow_lift": 55.0, "brightness": 1.0},
}
C2_HSA = {"name": "CIHighlightShadowAdjust", "inputHighlightAmount": 0.3, "inputShadowAmount": 0.5, "inputRadius": 0.0}
#: C3: 2.7（S1 の最大）→ 1.0 に収める露出。2^-1.43 ≈ 0.371。
C3_EXPOSURE_EV = -1.43
#: C3 のトーン曲線。**CIToneCurve の制御点は sRGB ガンマ符号化した座標で、補間は 5 点を通る 3 次スプライン**
#: （定数置換で確認、2026-10-21: 制御点 x は線形入力を sRGB 符号化した値に対応し、y も sRGB 符号化した出力。
#: 平坦な区間を置くとスプラインが振動する）。計測 1 回目は線形座標で点を置いたため中間調がカメラ比 +0.33〜+0.38 明るくなった。
#: 設計: CIExposureAdjust −1.43 後の線形 x = v·0.371 について、v = 0.18 / 0.5 を boost 1 の応答（0.3875 / 0.805）に
#: 一致させ、v = 1.0（元の表示白）→ 0.90、v = 2.7 → 1.0 とする（1.0 超のヘッドルームを 8bit 243〜255 に収める）。
#: 応答の実測（DSC04705、定数置換）: v 0.18 → 0.387、0.5 → 0.805、0.8 → 0.892、1.0 → 0.900、1.5 → 0.924、2.0 → 0.955、2.7 → 1.0（単調）。
C3_TONE_CURVE = {
    "name": "CIToneCurve",
    "inputPoint0": [0.0, 0.0], "inputPoint1": [0.2867, 0.6557], "inputPoint2": [0.4679, 0.9088],
    "inputPoint3": [0.6431, 0.9547], "inputPoint4": [1.0, 1.0],
}
C3B_EXTRA_EV = 0.3
C_CANDIDATES = ("c1_ev-07_lift", "c1_ev-10_lift", "c2_ev-07_hsa", "c3_lin_curve", "c3_lin_curve_b")
#: 露出掃引（§8.3 の内挿に使う）: variant -> exposure_ev。d_base は 0。
DIAG_EV_SWEEP = (("d_base", 0.0), ("d_ev-03", -0.3), ("d_ev-07", -0.7), ("d_ev-10", -1.0), ("d_ev-15", -1.5))
DIAG_STAGE_VARIANTS = ("d_s1_linear", "d_s2_tone", "d_s3_default")
#: full で回すバリアント（§6.3 の 12 本 + 画素照合に要る d_lens_off）。c_best はプレビューの結果から決める。
DIAG_FULL_VARIANTS = (
    "d_base", "d_s1_linear", "d_s2_tone", "d_s3_default", "d_ev-07", "d_ev-10", "d_boost00", "d_gamut_off",
    "d_tiff_srgb", "d_lens_off", "c1_ev-07_lift", "c3_lin_curve", "c_best",
)
#: ROI 別に出すクリップ指標の ROI。
DIAG_CLIP_ROIS = ("sky_bright", "cloud_grad", "clip_edge")
DIAG_TABLE_KEYS = (
    "clip8_any", "sky_bright_clip8_any", "cloud_grad_clip8_any", "blue_only_clip", "mid_p50", "mid_p50_diff",
    "grad_levels_hi", "grad_texture_ratio", "grad_slope", "fg_p01_diff", "fg_noise", "magenta_rate",
)


def _spec(lens: bool = True, profile: str = "apple_default", overrides: dict[str, float] | None = None,
          extra_settings: dict[str, Any] | None = None, output_format: str = "jpeg", output_color_space: str | None = None,
          adjustments: dict[str, Any] | None = None, diagnostics: dict[str, Any] | None = None, dump: bool = False,
          label: str = "") -> dict[str, Any]:
    return {"lens": lens, "profile": profile, "overrides": dict(overrides or {}), "extra_settings": extra_settings,
            "output_format": output_format, "output_color_space": output_color_space, "adjustments": adjustments,
            "diagnostics": diagnostics, "dump": dump, "label": label or ""}


def diagnosis_specs() -> dict[str, dict[str, Any]]:
    """§6.2 のマトリクス（preview の全バリアント。c_best は含まない）。順序はシート・CSV の並び。"""
    stage_lens = False  # 段階読み出しは CFA マップと同じ幾何（lens OFF）で取る（§6.2 / §7.3）
    specs: dict[str, dict[str, Any]] = {
        "d_base": _spec(label="B apple v9 lens ON"),
        "d_s1_linear": _spec(lens=stage_lens, overrides={"boost_amount": 0.0}, extra_settings={"gamut_mapping": False},
                             dump=True, label="S1 linear (boost 0, gamut OFF, lens OFF)"),
        "d_s2_tone": _spec(lens=stage_lens, extra_settings={"gamut_mapping": False}, dump=True,
                           label="S2 tone (gamut OFF, lens OFF)"),
        "d_s3_default": _spec(lens=stage_lens, dump=True, label="S3 default (lens OFF)"),
    }
    for name, value in (("d_boost00", 0.0), ("d_boost025", 0.25), ("d_boost050", 0.5), ("d_boost075", 0.75)):
        specs[name] = _spec(overrides={"boost_amount": value}, label=f"boost {value}")
    for name, ev in DIAG_EV_SWEEP[1:]:
        specs[name] = _spec(overrides={"exposure_ev": ev}, label=f"exposure {ev:+.1f}EV")
    specs["d_gamut_off"] = _spec(extra_settings={"gamut_mapping": False}, label="gamut mapping OFF")
    specs["d_tiff_srgb"] = _spec(output_format="tiff16", output_color_space="srgb", label="tiff16 sRGB")
    specs["d_tiff_p3"] = _spec(output_format="tiff16", output_color_space="display_p3", label="tiff16 Display P3")
    for name, value in (("d_edr05", 0.5), ("d_edr10", 1.0), ("d_edr20", 2.0)):
        specs[name] = _spec(overrides={"extended_dynamic_range": value}, label=f"EDR {value}")
    specs["d_neutral"] = _spec(profile="neutral", label="neutral v9 lens ON")
    specs["d_lens_off"] = _spec(lens=False, label="B lens OFF")
    for name, post in C1_SPECS.items():
        specs[name] = _spec(adjustments={**ADJ_IDENTITY, **post},
                            label=f"C1 exp {post['exposure']} lift {post['shadow_lift']:g} bright {post['brightness']}")
    specs["c2_ev-07_hsa"] = _spec(overrides={"exposure_ev": -0.7}, diagnostics={"post_filters": [dict(C2_HSA)]},
                                  label="C2 -0.7EV + HighlightShadow")
    c3_filters = [{"name": "CIExposureAdjust", "inputEV": C3_EXPOSURE_EV}, dict(C3_TONE_CURVE)]
    specs["c3_lin_curve"] = _spec(overrides={"boost_amount": 0.0}, extra_settings={"gamut_mapping": False},
                                  diagnostics={"post_filters": c3_filters}, label="C3 linear + tone curve")
    c3b_filters = [c3_filters[0], {"name": "CIExposureAdjust", "inputEV": C3B_EXTRA_EV}, dict(C3_TONE_CURVE)]
    specs["c3_lin_curve_b"] = _spec(overrides={"boost_amount": 0.0}, extra_settings={"gamut_mapping": False},
                                    diagnostics={"post_filters": c3b_filters}, label="C3b linear + 0.3EV + tone curve")
    return specs


def diagnosis_mode_specs(mode: str, best: str | None = None) -> dict[str, dict[str, Any]]:
    """mode ごとのバリアント。full は §6.3 の部分集合 + d_lens_off、c_best は preview で選んだ候補の複製。"""
    specs = diagnosis_specs()
    if mode == "preview":
        return specs
    out = {name: specs[name] for name in DIAG_FULL_VARIANTS if name in specs}
    if best and best in specs:
        out["c_best"] = {**specs[best], "label": f"c_best = {best}"}
    return out


def diagnosis_request_kwargs(spec: dict[str, Any], dump_path: Path | None) -> dict[str, Any]:
    """spec を run_coreimage / coreimage_request のキーワード引数にする（純粋）。dump は skip_write で取る。"""
    diagnostics = json.loads(json.dumps(spec["diagnostics"])) if spec.get("diagnostics") is not None else None
    if spec.get("dump"):
        if dump_path is None:
            raise ValueError("dump バリアントには dump_path が必要です")
        diagnostics = dict(diagnostics or {})
        diagnostics["dump_float"] = {"path": str(dump_path), "color_space": "linear_srgb"}
        diagnostics["skip_write"] = True
    return {
        "profile": spec["profile"],
        "overrides": dict(spec["overrides"]) or None,
        "extra_settings": dict(spec["extra_settings"]) if spec.get("extra_settings") else None,
        "output_format": spec["output_format"],
        "output_color_space": spec["output_color_space"],
        "adjustments": dict(spec["adjustments"]) if spec.get("adjustments") is not None else None,
        "diagnostics": diagnostics,
    }


def diagnosis_dump_path(out: Path, capture_id: str, mode: str, variant: str) -> Path:
    return out / "dumps" / mode / f"{capture_id}_{mode}_{variant}.f32"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def validate_post_filter(item: Any, index: int) -> list[str]:
    """§4.5 の許可リストに沿うか（純粋）。"""
    where = f"diagnostics.post_filters[{index}]"
    if not isinstance(item, dict):
        return [f"{where} がオブジェクトではありません"]
    name = item.get("name")
    allowed = POST_FILTER_ALLOWLIST.get(name)
    if allowed is None:
        return [f"{where}.name={name!r} は許可リストにありません"]
    problems = []
    for key, value in item.items():
        if key == "name":
            continue
        rule = allowed.get(key)
        if rule is None:
            problems.append(f"{where}: {name} は {key} を受けません")
            continue
        kind = rule[0]
        if kind == "scalar":
            if not _is_number(value):
                problems.append(f"{where}.{key} が数値ではありません: {value!r}")
            elif not (rule[1] <= float(value) <= rule[2]):
                problems.append(f"{where}.{key}={value} が値域 {rule[1]}..{rule[2]} の外です")
        else:
            size = 2 if kind == "vec2" else 4
            if not isinstance(value, list) or len(value) != size or not all(_is_number(v) for v in value):
                problems.append(f"{where}.{key} は数値 {size} 要素の配列です: {value!r}")
            elif kind == "vec2":
                (xlo, xhi), (ylo, yhi) = rule[1], rule[2]
                if not (xlo <= value[0] <= xhi and ylo <= value[1] <= yhi):
                    problems.append(f"{where}.{key}={value} が値域 x {xlo}..{xhi} / y {ylo}..{yhi} の外です")
    if name == "CIToneCurve":
        xs = [item[f"inputPoint{i}"][0] for i in range(5) if isinstance(item.get(f"inputPoint{i}"), list) and item[f"inputPoint{i}"]]
        if any(b <= a for a, b in zip(xs, xs[1:])):
            problems.append(f"{where}: CIToneCurve の x は単調増加でなければなりません")
    return problems


def validate_diagnostics_request(request: dict[str, Any]) -> list[str]:
    """ハーネスが組んだ CLI 0.4.0 リクエストが設計書 §4.2〜4.5 の契約に沿うかを確かめる（純粋。問題の一覧）。"""
    problems: list[str] = []
    unknown = sorted(set(request) - DIAG_REQUEST_TOP_KEYS)
    if unknown:
        problems.append(f"未知のトップレベルキー: {', '.join(unknown)}")
    settings = request.get("settings") or {}
    if "gamut_mapping" in settings and settings["gamut_mapping"] is not None and not isinstance(settings["gamut_mapping"], bool):
        problems.append(f"settings.gamut_mapping は boolean です: {settings['gamut_mapping']!r}")
    if settings.get("baseline_exposure") is not None:
        value = settings["baseline_exposure"]
        if not _is_number(value) or not (-5.0 <= float(value) <= 5.0):
            problems.append(f"settings.baseline_exposure は −5..5 の数値です: {value!r}")
    problems += validate_adjustments_request(request)
    diag = request.get("diagnostics")
    if diag is None:
        return problems
    if not isinstance(diag, dict):
        return problems + ["diagnostics がオブジェクトではありません"]
    extra = sorted(set(diag) - DIAG_KEYS)
    if extra:
        problems.append(f"diagnostics に未知のキー: {', '.join(extra)}")
    if "stats" in diag and not isinstance(diag["stats"], bool):
        problems.append("diagnostics.stats は boolean です")
    if "skip_write" in diag and not isinstance(diag["skip_write"], bool):
        problems.append("diagnostics.skip_write は boolean です")
    if "stats_color_space" in diag and diag["stats_color_space"] not in DIAG_COLOR_SPACES:
        problems.append(f"diagnostics.stats_color_space が不正です: {diag['stats_color_space']!r}")
    dump = diag.get("dump_float")
    if dump is not None:
        if not isinstance(dump, dict):
            problems.append("diagnostics.dump_float がオブジェクトではありません")
        else:
            if set(dump) - {"path", "color_space"}:
                problems.append(f"diagnostics.dump_float に未知のキー: {', '.join(sorted(set(dump) - {'path', 'color_space'}))}")
            path = dump.get("path")
            if not isinstance(path, str) or not Path(path).is_absolute() or not path.endswith(".f32"):
                problems.append(f"diagnostics.dump_float.path は絶対パス・拡張子 .f32 です: {path!r}")
            elif path in (request.get("input"), request.get("output")):
                problems.append("diagnostics.dump_float.path は input / output と別のパスにしてください")
            if dump.get("color_space", "linear_srgb") not in DIAG_COLOR_SPACES:
                problems.append(f"diagnostics.dump_float.color_space が不正です: {dump.get('color_space')!r}")
    lsf = diag.get("linear_space_filter")
    if lsf is not None:
        kind = lsf.get("kind") if isinstance(lsf, dict) else None
        if kind not in ("identity", "scale", "constant"):
            problems.append(f"diagnostics.linear_space_filter.kind が不正です: {kind!r}")
        elif kind == "identity":
            if "value" in lsf:
                problems.append("linear_space_filter identity に value は付けません")
        else:
            lo, hi = (0.01, 8.0) if kind == "scale" else (0.0, 8.0)
            value = lsf.get("value")
            if not _is_number(value) or not (lo <= float(value) <= hi):
                problems.append(f"linear_space_filter {kind} の value は {lo}..{hi} です: {value!r}")
    posts = diag.get("post_filters")
    if posts is not None:
        if not isinstance(posts, list):
            problems.append("diagnostics.post_filters は配列です")
        else:
            if len(posts) > POST_FILTERS_MAX:
                problems.append(f"diagnostics.post_filters は最大 {POST_FILTERS_MAX} 個です（{len(posts)} 個）")
            for index, item in enumerate(posts):
                problems += validate_post_filter(item, index)
    return problems


def diagnosis_cli_problem(version: Any, capabilities: Any) -> str | None:
    """--diagnosis を回せない理由（CLI 0.4.0 未満 / --capabilities に diagnostics.supported が無い）。回せれば None。"""
    if not isinstance(version, dict) or version.get("error") or not version.get("cli_version"):
        detail = version.get("error") if isinstance(version, dict) else version
        return f"Core Image CLI のバージョンを取得できません（--version: {detail}）"
    parsed = parse_cli_version(version.get("cli_version"))
    need = ".".join(str(v) for v in DIAGNOSIS_MIN_CLI)
    if parsed is None or parsed < DIAGNOSIS_MIN_CLI:
        return f"Core Image CLI {version.get('cli_version')} は diagnostics に未対応です（{need} 以上が必要。設計書 §4）"
    diag = capabilities.get("diagnostics") if isinstance(capabilities, dict) else None
    if not isinstance(diag, dict) or diag.get("supported") is not True:
        return f"Core Image CLI {version.get('cli_version')} の --capabilities に diagnostics.supported: true がありません（設計書 §4.6）"
    return None


def diagnostics_response_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """CLI 0.4.0 応答の ``applied.gamut_mapping_enabled`` と ``diagnostics`` ブロック（§4.4）を CSV の平らな列にする（純粋）。"""
    applied = payload.get("applied") or {}
    out: dict[str, Any] = {"ci_gamut": applied.get("gamut_mapping_enabled")}
    diag = payload.get("diagnostics")
    if not isinstance(diag, dict):
        out["ci_diag_present"] = False
        return out
    stats = diag.get("stats") or {}
    readback = diag.get("readback") or {}
    dump = diag.get("dump_float") or {}
    out.update({
        "ci_diag_present": True,
        "ci_diag_version": diag.get("cli_diagnostics_version"),
        "ci_diag_written": diag.get("written"),
        "ci_stats_color_space": diag.get("stats_color_space"),
        "ci_stats_source": diag.get("stats_source"),
        "ci_stats_any_gt_1": stats.get("any_gt_1"),
        "ci_stats_all_gt_1": stats.get("all_gt_1"),
        "ci_readback_gamut": readback.get("gamut_mapping_enabled"),
        "ci_readback_baseline": readback.get("baseline_exposure"),
        "ci_readback_extent": json.dumps(readback.get("extent")) if readback.get("extent") is not None else None,
        "ci_dump_path": dump.get("path"),
    })
    for ch in ("r", "g", "b"):
        st = stats.get(ch) or {}
        for key in ("p50", "p999", "max", "frac_gt_1", "frac_ge_1"):
            out[f"ci_stats_{ch}_{key}"] = st.get(key)
    return out


# --- CFA 飽和（§5） --------------------------------------------------------------


def cfa_quantization_step(values: np.ndarray, white_level: int) -> int:
    """white_level/2 より大きい値の np.unique の最小差分（α6400 の圧縮 ARW は 32。非圧縮では 1 になりうる）。"""
    top = np.unique(values[values > white_level // 2])
    if top.size < 2:
        return 1
    return max(1, int(np.diff(top).min()))


def cfa_color_letters(color_desc: bytes | str, n_colors: int = 4) -> list[str]:
    """raw_colors_visible の 0..3 → 'r' / 'g' / 'b'（color_desc 'RGBG' なら [r, g, b, g]）。"""
    desc = color_desc.decode() if isinstance(color_desc, (bytes, bytearray)) else str(color_desc)
    return [desc[i].lower() if i < len(desc) else "g" for i in range(n_colors)]


def cfa_crop(array: np.ndarray, sizes: Any) -> np.ndarray:
    """raw_image_visible からマージン（crop_*）を落として有効域（6000×4000）にする。偶数寸法に揃える。

    2×2 セルはどの位置から切っても R / G / B / G を 1 つずつ含むので、マージンの偶奇はセルの構成を変えない。
    """
    top = int(getattr(sizes, "crop_top_margin", 0) or 0)
    left = int(getattr(sizes, "crop_left_margin", 0) or 0)
    height = int(getattr(sizes, "crop_height", 0) or 0) or array.shape[0] - top
    width = int(getattr(sizes, "crop_width", 0) or 0) or array.shape[1] - left
    out = array[top:top + height, left:left + width]
    return out[: out.shape[0] // 2 * 2, : out.shape[1] // 2 * 2]


def _cells(array: np.ndarray) -> np.ndarray:
    h, w = array.shape[:2]
    return array[: h // 2 * 2, : w // 2 * 2].reshape(h // 2, 2, w // 2, 2)


def cfa_analyze_arrays(
    vis: np.ndarray,
    colors: np.ndarray,
    letters: list[str],
    black: Iterable[float],
    white_level: int,
    camera_white: Iterable[float] | None = None,
    step: int | None = None,
) -> dict[str, Any]:
    """CFA の色別飽和率・2×2 セル飽和マップ・ヘッドルームを求める（純粋）。

    閾値は ``T_libraw = white_level − step``（主）と ``T_camera = camera_white − step``（副、色別）。
    値 ≥ T を飽和とみなす（黒補正は閾値側で扱う）。マップは 1 セル = 1 画素で、ビットは ``CFA_BIT``。
    """
    black = [float(b) for b in black]
    step = int(step or cfa_quantization_step(vis, white_level))
    t_libraw = float(white_level - step)
    cam = [float(c) for c in camera_white] if camera_white is not None else [float(white_level)] * len(black)
    t_camera_by_index = np.array([c - step if c > 0 else t_libraw for c in cam], dtype=np.float64)
    pixel_t_camera = t_camera_by_index[colors]
    letter_of = np.array([CFA_BIT[c] for c in letters], dtype=np.uint8)[colors]
    out: dict[str, Any] = {
        "cfa_white_level": int(white_level), "cfa_step": step, "cfa_t_libraw": t_libraw,
        "cfa_t_camera": float(t_camera_by_index.min()), "cfa_black": float(np.mean(black)),
        "cfa_width": int(vis.shape[1]), "cfa_height": int(vis.shape[0]),
    }
    maps: dict[str, np.ndarray] = {}
    for tag, prefix, sat in (("libraw", "cfa_sat", vis >= t_libraw), ("camera", "cfa_satc", vis >= pixel_t_camera)):
        for letter in ("r", "g", "b"):
            sel = letter_of == CFA_BIT[letter]
            out[f"{prefix}_{letter}"] = float(sat[sel].mean()) if sel.any() else float("nan")
        cells = _cells(sat)
        out[f"{prefix}_cell_any"] = float(cells.any(axis=(1, 3)).mean())
        out[f"{prefix}_cell_all"] = float(cells.all(axis=(1, 3)).mean())
        bits = np.where(sat, letter_of, 0).astype(np.uint8)
        maps[tag] = np.bitwise_or.reduce(np.bitwise_or.reduce(_cells(bits), axis=3), axis=1)
    for letter in ("r", "g", "b"):
        idx = [i for i, c in enumerate(letters) if c == letter]
        sel = letter_of == CFA_BIT[letter]
        if not idx or not sel.any():
            out[f"cfa_headroom_{letter}"] = float("nan")
            continue
        blk = float(np.mean([black[i] for i in idx]))
        out[f"cfa_headroom_{letter}"] = float((np.percentile(vis[sel], 99.9) - blk) / max(1e-6, t_libraw - blk))
    # 位置合わせ用の輝度サムネイル（セル平均 − 黒、0..1）
    blk_px = np.array(black, dtype=np.float32)[colors]
    norm = (vis.astype(np.float32) - blk_px) / max(1.0, float(white_level) - float(np.mean(black)))
    thumb = _cells(norm).mean(axis=(1, 3)).astype(np.float32)
    return {"metrics": out, "map_libraw": maps["libraw"], "map_camera": maps["camera"], "thumb": thumb}


def cfa_analyze(raw: Any) -> dict[str, Any]:
    """rawpy の RawPy から有効域を切り出して ``cfa_analyze_arrays`` を呼ぶ（読むだけ）。"""
    vis = cfa_crop(raw.raw_image_visible, raw.sizes)
    colors = cfa_crop(raw.raw_colors_visible, raw.sizes)
    letters = cfa_color_letters(raw.color_desc, 4)
    camera_white = getattr(raw, "camera_white_level_per_channel", None)
    return cfa_analyze_arrays(vis, colors, letters, raw.black_level_per_channel, int(raw.white_level), camera_white)


def cfa_saturation(raw: Any) -> dict[str, Any]:
    """CFA 色別の飽和率（§5 の指標。``T_libraw`` と ``T_camera`` の両方）。"""
    return cfa_analyze(raw)["metrics"]


def cfa_saturation_map(raw: Any, threshold: str = "libraw") -> np.ndarray:
    """2×2 セル飽和マップ（有効域の 1/2 寸法、uint8 のビット ``CFA_BIT``。向きは未適用）。"""
    result = cfa_analyze(raw)
    return result["map_camera" if threshold == "camera" else "map_libraw"]


def orient_array(array: np.ndarray, orientation: Any) -> np.ndarray:
    """EXIF Orientation を適用して正立にする（PIL の exif_transpose と同じ規則）。"""
    o = int(orientation) if isinstance(orientation, (int, float)) and not isinstance(orientation, bool) else 1
    ops = {
        1: lambda a: a,
        2: lambda a: a[:, ::-1],
        3: lambda a: a[::-1, ::-1],
        4: lambda a: a[::-1, :],
        5: lambda a: np.swapaxes(a, 0, 1),
        6: lambda a: np.rot90(a, -1),
        7: lambda a: np.rot90(a, 2).swapaxes(0, 1),
        8: lambda a: np.rot90(a, 1),
    }
    return np.ascontiguousarray(ops.get(o, ops[1])(array))


def resize_cell_map(bits: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """セル飽和マップ（uint8 ビット）を (w, h) へ。縮小は「足跡が少しでも重なるセルの OR」（INTER_AREA > 0）、
    拡大は最近傍（補間しない）。ビットごとに処理して合成する。"""
    tw, th = size
    h, w = bits.shape
    if (w, h) == (tw, th):
        return bits.copy()
    out = np.zeros((th, tw), dtype=np.uint8)
    shrink = tw <= w and th <= h
    for bit in CFA_BIT.values():
        mask = ((bits & bit) > 0).astype(np.float32)
        if shrink:
            moved = cv2.resize(mask, (tw, th), interpolation=cv2.INTER_AREA) > 0
        else:
            moved = cv2.resize(mask, (tw, th), interpolation=cv2.INTER_NEAREST) > 0.5
        out |= np.where(moved, bit, 0).astype(np.uint8)
    return out


def cfa_from_file(path: Path, orientation: Any = 1) -> dict[str, Any]:
    """ARW を rawpy で読み（読むだけ）、正立のセルマップ・サムネイルと指標を返す。旧経路（``--legacy-rawpy``）のみ。"""
    rawpy = legacy.import_module("rawpy", "--diagnosis の CFA 飽和計測")

    started = time.monotonic()
    with rawpy.imread(str(path)) as raw:
        result = cfa_analyze(raw)
    result["map_libraw"] = orient_array(result["map_libraw"], orientation)
    result["map_camera"] = orient_array(result["map_camera"], orientation)
    result["thumb"] = orient_array(result["thumb"], orientation)
    result["metrics"]["cfa_elapsed_s"] = time.monotonic() - started
    return result


# --- dump / 色空間 ---------------------------------------------------------------


def load_dump(meta: dict[str, Any]) -> np.ndarray:
    """CLI 応答の ``diagnostics.dump_float``（§4.4）を検査してから .f32 を (h, w, 3) float32 で読む。"""
    problems = []
    if meta.get("dtype") != "float32":
        problems.append(f"dtype={meta.get('dtype')!r}")
    if meta.get("byte_order") != "little":
        problems.append(f"byte_order={meta.get('byte_order')!r}")
    if meta.get("channels") != 3:
        problems.append(f"channels={meta.get('channels')!r}")
    if meta.get("layout") != "interleaved_rgb_row_major":
        problems.append(f"layout={meta.get('layout')!r}")
    width, height = meta.get("width"), meta.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        problems.append(f"width/height={width!r}/{height!r}")
    if problems:
        raise ValueError("dump のメタデータが契約（§4.4）と違います: " + ", ".join(problems))
    expected = width * height * 3 * 4
    path = Path(str(meta.get("path")))
    if meta.get("bytes") not in (None, expected):
        raise ValueError(f"dump の bytes {meta.get('bytes')} が width×height×12 = {expected} と違います")
    actual = path.stat().st_size
    if actual != expected:
        raise ValueError(f"dump のファイルサイズ {actual} が期待値 {expected} と違います: {path}")
    return np.fromfile(path, dtype="<f4").reshape(height, width, 3)


def srgb_encode(linear: np.ndarray) -> np.ndarray:
    """リニア（0..1 に clip 済みを想定）→ sRGB ガンマ（IEC 61966-2-1）。"""
    v = np.clip(np.asarray(linear, dtype=np.float32), 0.0, None)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * np.power(v, 1.0 / 2.4) - 0.055).astype(np.float32)


def _rgb_to_xyz(primaries: tuple[tuple[float, float], ...], white: tuple[float, float]) -> np.ndarray:
    xyz = np.array([[x / y, 1.0, (1.0 - x - y) / y] for x, y in primaries]).T
    wx, wy = white
    w = np.array([wx / wy, 1.0, (1.0 - wx - wy) / wy])
    return xyz * np.linalg.solve(xyz, w)


#: リニア Display P3 → リニア sRGB（D65 同士なので白色点変換なし）。
P3_TO_SRGB = np.linalg.solve(
    _rgb_to_xyz(((0.64, 0.33), (0.30, 0.60), (0.15, 0.06)), (0.3127, 0.3290)),
    _rgb_to_xyz(((0.680, 0.320), (0.265, 0.690), (0.150, 0.060)), (0.3127, 0.3290)),
).astype(np.float32)


def p3_to_srgb(p3_encoded01: np.ndarray) -> np.ndarray:
    """Display P3（sRGB 伝達関数、0..1）→ sRGB ガンマ値（clip しない。1 超 = sRGB で白飛び）。"""
    linear = srgb_to_linear(np.clip(p3_encoded01, 0.0, 1.0)) @ P3_TO_SRGB.T
    out = np.where(linear <= 0.0031308, linear * 12.92, 1.055 * np.power(np.clip(linear, 0, None), 1.0 / 2.4) - 0.055)
    return out.astype(np.float32)


# --- ROI（§7） -------------------------------------------------------------------


def _grid_sums(integral: np.ndarray, s: int, step: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """積分画像から、s×s 箱（step 刻み）の総和を返す: (xs, ys, sums) の 1 次元配列。"""
    h, w = integral.shape[0] - 1, integral.shape[1] - 1
    xs = np.arange(0, w - s + 1, step)
    ys = np.arange(0, h - s + 1, step)
    gx, gy = np.meshgrid(xs, ys)
    gx, gy = gx.ravel(), gy.ravel()
    sums = integral[gy + s, gx + s] - integral[gy, gx + s] - integral[gy + s, gx] + integral[gy, gx]
    return gx, gy, sums.astype(np.float64)


def _overlaps(box: tuple[int, int, int, int], taken: Iterable[tuple[int, int, int, int]]) -> bool:
    x0, y0, x1, y1 = box
    return any(not (x1 <= bx0 or bx1 <= x0 or y1 <= by0 or by1 <= y0) for bx0, by0, bx1, by1 in taken)


def _pick(xs: np.ndarray, ys: np.ndarray, scores: np.ndarray, s: int, taken: list, allowed: np.ndarray | None = None,
          prefer: str = "max") -> tuple[int, int, int, int] | None:
    order = np.argsort(-scores if prefer == "max" else scores, kind="stable")
    for i in order:
        if allowed is not None and not allowed[i]:
            continue
        if not math.isfinite(scores[i]):
            continue
        box = (int(xs[i]), int(ys[i]), int(xs[i]) + s, int(ys[i]) + s)
        if not _overlaps(box, taken):
            return box
    return None


def diagnosis_rois(camera_rgb8: np.ndarray, size: int) -> tuple[dict[str, tuple[int, int, int, int] | None], dict[str, Any]]:
    """カメラ JPEG から §7.1 の 5 ROI を重ならないように順に確保する（純粋）。見つからない ROI は None。

    fg_dark の「構造がある」は、箱の Laplacian 平均（σ1 ぼかし後、8bit スケール）が全箱の中央値以上とした
    （絶対値の閾値はシーン依存が大きいので相対にした）。返す info に閾値を残す。
    cloud_grad の「ローカル標準偏差」は帯（0.6 < L < 0.98）内の画素だけで取る（箱全体だと地平線を拾う）。
    """
    h, w = camera_rgb8.shape[:2]
    s = max(8, min(size, h, w))
    step = max(1, s // 2)
    rgb01 = camera_rgb8.astype(np.float32) / 255.0
    L = luma_of(rgb01)
    S = rgb01.max(axis=2) - rgb01.min(axis=2)
    # HSV の S（(max − min) / max）。上の S はクロマ（max − min）で midtone 用。明るい画素では両者が最大 ~1.6 倍違う。
    vmax = rgb01.max(axis=2)
    hsv_s = np.where(vmax > 0, S / np.maximum(vmax, 1e-6), 0.0)
    area = float(s * s)
    taken: list[tuple[int, int, int, int]] = []
    rois: dict[str, tuple[int, int, int, int] | None] = {}
    info: dict[str, Any] = {"size": s, "step": step}

    def counts(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return _grid_sums(cv2.integral(mask.astype(np.uint8)), s, step)

    xs, ys, n = counts(L > 0.85)
    rois["sky_bright"] = _pick(xs, ys, n, s, taken, allowed=n > 0)
    if rois["sky_bright"]:
        taken.append(rois["sky_bright"])

    blurred_L = cv2.GaussianBlur(L, (0, 0), 2.0).astype(np.float64)

    def pick_cloud(band: np.ndarray) -> tuple[int, int, int, int] | None:
        xs, ys, n = counts(band)
        # 起伏は帯（0.6 < L < 0.98）内の画素だけで測る。箱全体の標準偏差だと地平線（空と暗い前景の境界）が最大になる。
        blurred = blurred_L * band
        _, _, s1 = _grid_sums(cv2.integral(blurred), s, step)
        _, _, s2 = _grid_sums(cv2.integral(blurred * blurred), s, step)
        nb = np.maximum(n, 1.0)
        local_std = np.sqrt(np.clip(s2 / nb - (s1 / nb) ** 2, 0.0, None))
        return _pick(xs, ys, local_std, s, taken, allowed=(n / area) >= 0.7)

    # 帯に低彩度（HSV の S < CLOUD_GRAD_MAX_SAT）の条件を足す（レポート §8-4。DSC04754 で雲ではなく手前の明るい
    # 被写体を拾った誤抽出の対策）。条件を満たす箱が無いときは従来の帯（彩度条件なし）で選び直す: cloud_grad が
    # 欠けるとシーン判定（no_clip 扱い）・S1/S2 の階調比・タスク C の実施条件まで連鎖して欠測になるため。
    # どちらで選んだかは info["cloud_grad_low_sat"] に残す。
    # 閾値 0.2 の根拠（2026-10-21、10 カット preview + 3 full の カメラ JPEG で確認）: 夜明けの桃色の雲 DSC04532 の箱は
    # 帯内の S が p50 / p90 / p99 = 0.08 / 0.10 / 0.12 で 0.2 でも同じ箱のまま。0.2 で動くのは 4 カット（DSC04754 は車体 →
    # 空、DSC04607 / 04579 / 04746 は帯内に S 0.19〜0.25 の青みの稜線・空を含む箱から隣の低彩度の空へ 1 箱）。0.25 なら
    # DSC04754 だけが動き、0.3 以上では何も動かない（DSC04754 の誤抽出が残る）。オーナー判断（レポート §8-4）の 0.2 を採った。
    band = (L > 0.6) & (L < 0.98)
    rois["cloud_grad"] = pick_cloud(band & (hsv_s < CLOUD_GRAD_MAX_SAT))
    info["cloud_grad_max_sat"] = CLOUD_GRAD_MAX_SAT
    info["cloud_grad_low_sat"] = rois["cloud_grad"] is not None
    if rois["cloud_grad"] is None:
        rois["cloud_grad"] = pick_cloud(band)
    if rois["cloud_grad"]:
        taken.append(rois["cloud_grad"])

    clipped = (camera_rgb8 >= 255).any(axis=2)
    ring = dilate_mask(clipped, CLIP_BAND_PX) & ~clipped
    xs, ys, n = counts(ring)
    rois["clip_edge"] = _pick(xs, ys, n, s, taken, allowed=n > 0)
    if rois["clip_edge"]:
        taken.append(rois["clip_edge"])

    lap = np.abs(cv2.Laplacian(cv2.GaussianBlur(L * 255.0, (0, 0), 1.0), cv2.CV_32F)).astype(np.float64)
    xs, ys, lap_sum = _grid_sums(cv2.integral(lap), s, step)
    lap_mean = lap_sum / area
    threshold = float(np.median(lap_mean)) if lap_mean.size else float("nan")
    info["fg_dark_laplacian_threshold"] = threshold
    p50 = np.full(lap_mean.shape, np.inf)
    for i in np.flatnonzero(lap_mean >= threshold):
        box = (int(xs[i]), int(ys[i]), int(xs[i]) + s, int(ys[i]) + s)
        if not _overlaps(box, taken):
            p50[i] = float(np.median(L[box[1]:box[3], box[0]:box[2]]))
    rois["fg_dark"] = _pick(xs, ys, p50, s, taken, prefer="min")
    if rois["fg_dark"]:
        taken.append(rois["fg_dark"])

    xs, ys, n = counts((L > 0.3) & (L < 0.6) & (S < 0.25))
    rois["midtone"] = _pick(xs, ys, n, s, taken, allowed=n > 0)
    return rois, info


def align_rois(
    ref_gray: np.ndarray,
    tgt_gray: np.ndarray,
    rois: dict[str, tuple[int, int, int, int] | None],
    size: int,
    min_response: float = ROI_MIN_RESPONSE,
) -> dict[str, dict[str, Any]]:
    """カメラ（ref）→ バリアント（tgt）の ROI ごとの平行移動（§7.2）。失敗した ROI は box None（欠測）。

    ``sky_bright`` は白一色で相関が落ちやすいので、平行移動は ``SKY_SHIFT_SOURCES`` の最初に成功した ROI の値を
    流用し、自身の相関（response）は記録だけする。ref の内容が tgt の (x+dx, y+dy) にある向き。
    """
    h, w = tgt_gray.shape[:2]
    max_shift = size / 4.0
    out: dict[str, dict[str, Any]] = {}
    for name in DIAG_ROI_NAMES:
        box = rois.get(name)
        if box is None:
            out[name] = {"box": None, "ok": False, "reason": "no_roi"}
            continue
        dx, dy, response = roi_shift(ref_gray, tgt_gray, box, margin=int(max_shift))
        ok = response >= min_response and math.hypot(dx, dy) <= max_shift
        out[name] = {"dx": dx, "dy": dy, "response": response, "ok": ok, "reason": None if ok else "align_failed",
                     "box": shift_box(box, dx, dy, w, h) if ok else None, "shift_source": name}
    sky = out.get("sky_bright")
    if sky and rois.get("sky_bright") is not None:
        source = next((n for n in SKY_SHIFT_SOURCES if out.get(n, {}).get("ok")), None)
        if source is not None:
            dx, dy = out[source]["dx"], out[source]["dy"]
            sky.update({"dx": dx, "dy": dy, "ok": True, "reason": None, "shift_source": source,
                        "box": shift_box(rois["sky_bright"], dx, dy, w, h)})
    return out


def identity_alignment(rois: dict[str, tuple[int, int, int, int] | None]) -> dict[str, dict[str, Any]]:
    return {n: {"box": b, "ok": b is not None, "dx": 0.0, "dy": 0.0, "response": 1.0,
                "reason": None if b is not None else "no_roi", "shift_source": n} for n, b in rois.items()}


def _crop(array: np.ndarray, box: tuple[int, int, int, int] | None) -> np.ndarray | None:
    if box is None:
        return None
    x0, y0, x1, y1 = box
    return array[y0:y1, x0:x1]


# --- 指標（§8） ------------------------------------------------------------------


def stage_rates(rgb: np.ndarray, prefix: str, threshold: float = 1.0, strict: bool = True) -> dict[str, float]:
    """float 画像のチャンネル別・いずれか・全部が threshold 超（strict）/ 以上の割合。"""
    over = rgb > threshold if strict else rgb >= threshold
    return {
        f"{prefix}_r": float(over[..., 0].mean()), f"{prefix}_g": float(over[..., 1].mean()),
        f"{prefix}_b": float(over[..., 2].mean()),
        f"{prefix}_any": float(over.any(axis=2).mean()), f"{prefix}_all": float(over.all(axis=2).mean()),
    }


def stage_loss(prev_white: np.ndarray, cur_white: np.ndarray) -> float:
    """前段で白でなかった（[0,1)）画素が当段で白になった割合（§8.1。分母は全画素。画素はいずれかのチャンネル）。"""
    if prev_white.shape != cur_white.shape:
        raise ValueError("stage_loss は同じ幾何の 2 段で数えます")
    return float((~prev_white & cur_white).mean()) if prev_white.size else float("nan")


def blue_only_clip(white_ch: np.ndarray) -> float:
    """B だけ白（R, G は白でない）の画素率。"""
    return float((white_ch[..., 2] & ~white_ch[..., 0] & ~white_ch[..., 1]).mean()) if white_ch.size else float("nan")


def grad_texture(L: np.ndarray, mask: np.ndarray, sigma: float = 1.0, min_pixels: int = DIAG_MIN_PIXELS) -> float:
    """σ1 ハイパス（L − ぼかし）の標準偏差を mask の画素で（§8.2 grad_texture）。画素が少なければ NaN。"""
    if mask is None or int(mask.sum()) < min_pixels:
        return float("nan")
    hp = L.astype(np.float32) - cv2.GaussianBlur(L.astype(np.float32), (0, 0), sigma)
    return float(hp[mask].std())


def distinct_levels(L: np.ndarray, mask: np.ndarray | None, levels: int) -> int:
    """L（0..1）を levels 段に量子化したときの distinct 数。"""
    values = L if mask is None else L[mask]
    if values.size == 0:
        return 0
    return int(np.unique(np.rint(np.clip(values, 0.0, 1.0) * levels)).size)


def dominant_gradient_angle(L: np.ndarray) -> float:
    """ぼかし後の構造テンソルから、最大勾配の方向（ラジアン）。"""
    b = cv2.GaussianBlur(L.astype(np.float32), (0, 0), 2.0)
    gx = cv2.Sobel(b, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(b, cv2.CV_32F, 0, 1, ksize=3)
    return 0.5 * math.atan2(2.0 * float((gx * gy).sum()), float((gx * gx).sum() - (gy * gy).sum()))


def projected_profile(L: np.ndarray, angle: float) -> np.ndarray:
    """L を angle 方向へ射影した 1px ビンの平均プロファイル（ESF と同じ採り方）。"""
    h, w = L.shape
    yy, xx = np.mgrid[0:h, 0:w]
    t = (xx - (w - 1) / 2.0) * math.cos(angle) + (yy - (h - 1) / 2.0) * math.sin(angle)
    bins = np.rint(t - t.min()).astype(np.int64).ravel()
    sums = np.bincount(bins, weights=L.astype(np.float64).ravel())
    n = np.bincount(bins)
    keep = n >= max(3, int(0.25 * min(h, w)))
    return (sums[keep] / n[keep]).astype(np.float64)


def rise_width_10_90(profile: np.ndarray, min_range: float = 0.02) -> float:
    """プロファイルの min→max 区間で 10% と 90% を横切る位置の差（px）。振れ幅が小さければ NaN。"""
    if profile.size < 3:
        return float("nan")
    i_min, i_max = int(np.argmin(profile)), int(np.argmax(profile))
    lo, hi = float(profile[i_min]), float(profile[i_max])
    if hi - lo < min_range:
        return float("nan")
    # min → max の向きに並べた区間（昇り）
    seg = profile[i_min:i_max + 1] if i_min < i_max else profile[i_max:i_min + 1][::-1]

    def crossing(level: float) -> float:
        idx = np.flatnonzero(seg >= level)
        if idx.size == 0:
            return float("nan")
        i = int(idx[0])
        if i == 0:
            return 0.0
        d = float(seg[i] - seg[i - 1])
        return (i - 1) + ((level - float(seg[i - 1])) / d if d > 0 else 1.0)

    return abs(crossing(lo + 0.9 * (hi - lo)) - crossing(lo + 0.1 * (hi - lo)))


def sky_color(rgb01: np.ndarray, white_ch: np.ndarray, min_pixels: int = DIAG_MIN_PIXELS) -> dict[str, float]:
    """非クリップ画素の Lab 色相角の中央値（度）とクロマの中央値（§8.2 sky_hue / sky_chroma）。"""
    keep = ~white_ch.any(axis=2)
    if int(keep.sum()) < min_pixels:
        return {"sky_hue": float("nan"), "sky_chroma": float("nan")}
    lab = cv2.cvtColor(np.clip(rgb01, 0, 1).astype(np.float32), cv2.COLOR_RGB2Lab)[keep]
    hue = np.degrees(np.arctan2(lab[:, 2], lab[:, 1])) % 360.0
    return {"sky_hue": float(np.median(hue)), "sky_chroma": float(np.median(np.hypot(lab[:, 1], lab[:, 2])))}


def pchip_curve(points: Iterable[tuple[float, float]]) -> Any:
    """単調 3 次エルミート（Fritsch–Carlson）で点列を補間する関数を返す。最後の点より先は最後の区間の傾きで外挿。"""
    pts = sorted((float(x), float(y)) for x, y in points)
    xs = np.array([p[0] for p in pts])
    ys = np.array([p[1] for p in pts])
    h = np.diff(xs)
    delta = np.diff(ys) / h
    m = np.zeros_like(ys)
    m[0], m[-1] = delta[0], delta[-1]
    for i in range(1, len(xs) - 1):
        if delta[i - 1] * delta[i] <= 0:
            m[i] = 0.0
        else:
            w1, w2 = 2 * h[i] + h[i - 1], h[i] + 2 * h[i - 1]
            m[i] = (w1 + w2) / (w1 / delta[i - 1] + w2 / delta[i])

    def f(x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        out = np.empty_like(x)
        below, above = x <= xs[0], x >= xs[-1]
        out[below] = ys[0] + m[0] * (x[below] - xs[0])
        out[above] = ys[-1] + delta[-1] * (x[above] - xs[-1])
        mid = ~(below | above)
        k = np.clip(np.searchsorted(xs, x[mid], side="right") - 1, 0, len(xs) - 2)
        t = (x[mid] - xs[k]) / h[k]
        h00, h10, h01, h11 = 2 * t**3 - 3 * t**2 + 1, t**3 - 2 * t**2 + t, -2 * t**3 + 3 * t**2, t**3 - t**2
        out[mid] = h00 * ys[k] + h10 * h[k] * m[k] + h01 * ys[k + 1] + h11 * h[k] * m[k + 1]
        return out

    return f


def interp_at(points: Iterable[tuple[float, float]], target: float) -> float:
    """(x, y) の折れ線を x = target で内挿する（外挿しない。範囲外・点不足は NaN）。x 昇順に並べ替える。"""
    pts = sorted((float(x), float(y)) for x, y in points
                 if _is_number(x) and _is_number(y))
    if len(pts) < 2 or not _is_number(target):
        return float("nan")
    xs = [p[0] for p in pts]
    if not (xs[0] <= target <= xs[-1]):
        return float("nan")
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= target <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (target - x0) / (x1 - x0)
    return float("nan")


# --- 帰属・判定（§10） ------------------------------------------------------------


def attribute_pixels(
    clipped: np.ndarray,
    sensor: np.ndarray | None,
    s2_over: np.ndarray | None,
    s3_white8: np.ndarray | None,
    valid: bool = True,
    s3_near_white: np.ndarray | None = None,
) -> dict[str, int]:
    """§10.1 の帰属規則を画素集合 K（clipped）に順に適用して件数を返す（純粋）。

    1 センサー飽和（CFA セルのいずれかが T 以上）→ 2 デコーダ/トーン（S2 のいずれかのチャンネルが 1.0 超）→
    3 8bit 量子化 quant8（S2 が [0,1] で、S3 が 8bit sRGB で 255 に丸まる値。旧「色域・SDR 出力」）→
    4 それ以外（S3 で 8bit 白でないのに K に入った画素）を S3 で 2 つに分ける:
    s3_near_white（S3 ≥ JPEG_NEAR_WHITE_LINEAR）なら JPEG 符号化 jpeg_encode、それ以外は検証不能 unverifiable
    （位置合わせ失敗・リサイズ等）。s3_near_white が None なら分けずに全部 unverifiable（旧規則 4 と同じ）。
    dump が無い・照合できない（valid False / マスク None）なら S3 の値が分からないので、K 全体を unverifiable にする
    （JPEG 符号化とは言えない）。

    K を S3 dump（≥ WHITE8_LINEAR）から取る経路（diagnosis_measure、レポート §6.4）では、規則 3 が残りを全部
    受け取るので jpeg_encode / unverifiable は構造上 0 になる（JPEG だけで白になった画素はそもそも K に入らない）。
    """
    k = clipped.astype(bool)
    n_k = int(k.sum())
    out = {"k": n_k, **{c: 0 for c in ATTR_CATEGORIES}}
    if not valid or sensor is None or s2_over is None or s3_white8 is None:
        out["unverifiable"] = n_k
        return out
    rest = k.copy()
    is_sensor = rest & sensor
    out["sensor"] = int(is_sensor.sum())
    rest &= ~sensor
    is_tone = rest & s2_over
    out["decoder_tone"] = int(is_tone.sum())
    rest &= ~s2_over
    is_quant = rest & s3_white8
    out["quant8"] = int(is_quant.sum())
    rest &= ~s3_white8
    if s3_near_white is not None:
        out["jpeg_encode"] = int((rest & s3_near_white).sum())
        rest &= ~s3_near_white
    out["unverifiable"] = int(rest.sum())
    return out


def classify_attribution(counts: dict[str, int] | None, roi_pixels: int) -> str:
    """ROI の分類: K が ROI の 1% 未満 → no_clip、最大の帰属 ≥ 50% かつ 2 位 < 30% → その帰属、それ以外 → mixed。

    設計書 §10.1 は「最大（≥ 50%）、2 位 ≥ 30% なら混合」。最大が 50% 未満の場合は書かれていないので mixed とした。
    """
    if counts is None or roi_pixels <= 0:
        return "unverifiable"
    k = counts.get("k", 0)
    if k < ATTR_MIN_CLIP_FRACTION * roi_pixels:
        return "no_clip"
    shares = sorted(((counts.get(c, 0) / k, c) for c in ATTR_CATEGORIES), reverse=True)
    (top, name), (second, _) = shares[0], shares[1]
    if top >= ATTR_MAJOR_FRACTION and second < ATTR_SECOND_FRACTION:
        return name
    return "mixed"


def classify_scene_diagnosis(
    roi_class: dict[str, str],
    counts: dict[str, dict[str, int] | None],
    grad_ratio_ev: Iterable[float],
    s1_grad_ratio: float,
    cloud_cfa_sat: float,
) -> str:
    """§10.2 のシーン判定（純粋）。上から順に評価する。

    - no_clip: sky_bright / cloud_grad がどちらも「クリップなし」（または ROI 無し）
    - sensor: 両方が sensor
    - decoder_tone: cloud_grad が decoder_tone で、exposure −0.7 / −1.0 のどちらかで grad_texture_ratio ≥ 0.5
    - quant8（旧 gamut_output）: 2 ROI の K を合算した帰属で quant8 が最大
    - unverifiable: 合算で「jpeg_encode + unverifiable」（= 旧 unverifiable）が最大、または S1 に階調が無く
      （s1 比 < 0.5）S0 も飽和していない。jpeg_encode を独立のシーン判定にしないのは、§10.2 の判定を名称変更
      （§8-3）で動かさないため（旧規則 4 の中身を 2 つに分けただけ）
    - mixed: それ以外
    """
    sky, cloud = roi_class.get("sky_bright"), roi_class.get("cloud_grad")
    if {sky, cloud} <= {"no_clip", None}:
        return "no_clip"
    if sky == "sensor" and cloud == "sensor":
        return "sensor"
    ratios = [r for r in grad_ratio_ev if _is_number(r)]
    if cloud == "decoder_tone" and any(r >= GRAD_RECOVER_MIN for r in ratios):
        return "decoder_tone"
    total = {c: sum((counts.get(n) or {}).get(c, 0) for n in ("sky_bright", "cloud_grad")) for c in ATTR_CATEGORIES}
    total["unverifiable"] += total.pop("jpeg_encode")
    if sum(total.values()) > 0:
        top = max(total, key=lambda c: total[c])
        if top == "quant8":
            return "quant8"
        if top == "unverifiable":
            return "unverifiable"
    if _is_number(s1_grad_ratio) and s1_grad_ratio < GRAD_RECOVER_MIN and _is_number(cloud_cfa_sat) and cloud_cfa_sat < SCENE_SENSOR_FREE_MAX:
        return "unverifiable"
    return "mixed"


def candidate_checks(row: dict[str, Any], camera: dict[str, Any]) -> dict[str, Any]:
    """§9 の合否 (1)〜(5)（自動判定分）。値が無い項目は None。(6) 目視は None のまま残す（オーナー）。"""

    def check(value: Any, fn: Any) -> bool | None:
        return bool(fn(float(value))) if _is_number(value) else None

    cam_clip = camera.get("sky_bright_clip8_any")
    out = {
        "c_pass_p50": check(row.get("mid_p50_diff"), lambda v: abs(v) <= MID_P50_TOL),
        "c_pass_clip": (bool(row["sky_bright_clip8_any"] <= cam_clip)
                        if _is_number(row.get("sky_bright_clip8_any")) and _is_number(cam_clip) else None),
        "c_pass_texture": check(row.get("grad_texture_ratio"), lambda v: v >= GRAD_TEXTURE_RATIO_MIN),
        "c_pass_fg": check(row.get("fg_p01_diff"), lambda v: v >= FG_P01_DROP_MAX),
        # 境界が無い（magenta_rate NaN）なら偽色も無いので合格
        "c_pass_magenta": check(row.get("magenta_rate"), lambda v: v <= MAGENTA_RATE_MAX) if row.get("band_pixels") else True,
        "c_pass_visual": None,
    }
    parts = [out[k] for k in ("c_pass_p50", "c_pass_clip", "c_pass_texture", "c_pass_fg", "c_pass_magenta")]
    out["c_pass_auto"] = False if any(p is False for p in parts) else (None if any(p is None for p in parts) else True)
    return out


def choose_c_best(rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """§9 c_best: |mid_p50_diff| ≤ 0.03 の候補から sky_bright の clip8_any 最小、同点なら grad_texture_ratio 最大。

    許容内の候補が無ければ |mid_p50_diff| 最小の候補を選び、within_tol False を残す（目視シートは作る）。
    """
    def num(v: Any, default: float) -> float:
        return float(v) if _is_number(v) else default

    candidates = [(name, rows[name]) for name in C_CANDIDATES if name in rows and not rows[name].get("error")]
    if not candidates:
        return {"best": None, "within_tol": False}
    within = [(n, r) for n, r in candidates if _is_number(r.get("mid_p50_diff")) and abs(r["mid_p50_diff"]) <= MID_P50_TOL]
    if within:
        name, _ = min(within, key=lambda nr: (num(nr[1].get("sky_bright_clip8_any"), num(nr[1].get("clip8_any"), 1.0)),
                                              -num(nr[1].get("grad_texture_ratio"), -1.0)))
        return {"best": name, "within_tol": True}
    name, _ = min(candidates, key=lambda nr: abs(num(nr[1].get("mid_p50_diff"), 9.0)))
    return {"best": name, "within_tol": False}


# --- 1 カット分の計測 ---------------------------------------------------------------


def _gray8(rgb01: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(to8(rgb01), cv2.COLOR_RGB2GRAY)


def display_luma(linear: np.ndarray, scale: float | None = None) -> np.ndarray:
    """線形 RGB（1 超を含む）を scale で割って sRGB ガンマの輝度 L にする（階調の有無を見るための表示化）。"""
    lum = luma_of(np.asarray(linear, dtype=np.float32))
    if scale is None:
        scale = max(1.0, float(np.percentile(lum, 99.9))) if lum.size else 1.0
    return luma_of(srgb_encode(np.clip(np.asarray(linear, dtype=np.float32) / scale, 0.0, 1.0)))


def image_variant_metrics(
    rgb01: np.ndarray,
    white_ch: np.ndarray,
    levels: int,
    align: dict[str, dict[str, Any]],
    camera_ref: dict[str, Any] | None,
) -> dict[str, Any]:
    """8bit / 16bit 出力 1 枚の全画面・ROI 指標（§8.1〜8.3）。camera_ref はカメラ JPEG の同指標と cloud マスク。"""
    row: dict[str, Any] = {}
    white_any = white_ch.any(axis=2)
    for i, ch in enumerate("rgb"):
        row[f"clip{'8' if levels == 255 else '16'}_{ch}"] = float(white_ch[..., i].mean())
    tag = "8" if levels == 255 else "16"
    row[f"clip{tag}_any"] = float(white_any.mean())
    row[f"clip{tag}_all"] = float(white_ch.all(axis=2).mean())
    row["blue_only_clip"] = blue_only_clip(white_ch)
    L = luma_of(rgb01)
    row["mean_luma"] = float(L.mean())
    row["p50_luma"] = float(np.percentile(L, 50))
    rgb8 = to8(rgb01)
    row.update(clip_boundary_magenta_rate(rgb8 if levels == 255 else np.where(white_ch, 255, np.minimum(rgb8, 254)).astype(np.uint8)))
    excluded = []
    for name in DIAG_ROI_NAMES:
        a = align.get(name) or {}
        row[f"{name}_shift_response"] = a.get("response")
        if a.get("box") is None and a.get("reason") == "align_failed":
            excluded.append(name)
    row["roi_excluded"] = ";".join(excluded)
    for name in DIAG_CLIP_ROIS:
        box = (align.get(name) or {}).get("box")
        wc = _crop(white_ch, box)
        if wc is None:
            continue
        for i, ch in enumerate("rgb"):
            row[f"{name}_clip{tag}_{ch}"] = float(wc[..., i].mean())
        row[f"{name}_clip{tag}_any"] = float(wc.any(axis=2).mean())
        row[f"{name}_blue_only_clip"] = blue_only_clip(wc)
    box = (align.get("clip_edge") or {}).get("box")
    if box is not None:
        row["clip_edge_magenta_rate"] = clip_boundary_magenta_rate(_crop(rgb8, box))["magenta_rate"]
    box = (align.get("midtone") or {}).get("box")
    if box is not None:
        row["mid_p50"] = float(np.percentile(_crop(L, box), 50))
        if camera_ref and _is_number(camera_ref.get("mid_p50")):
            row["mid_p50_diff"] = row["mid_p50"] - camera_ref["mid_p50"]
    box = (align.get("cloud_grad") or {}).get("box")
    if box is not None:
        Lc = _crop(L, box)
        mask = camera_ref.get("cloud_mask") if camera_ref else None
        if mask is None or mask.shape != Lc.shape:
            mask = Lc > 0.8
        row["grad_levels"] = distinct_levels(Lc, None, levels)
        row["grad_levels_hi"] = distinct_levels(Lc, mask, levels)
        row["grad_texture"] = grad_texture(Lc, mask)
        if camera_ref and _is_number(camera_ref.get("grad_texture")) and camera_ref["grad_texture"] > 0 and _is_number(row["grad_texture"]):
            row["grad_texture_ratio"] = row["grad_texture"] / camera_ref["grad_texture"]
        angle = camera_ref.get("cloud_angle") if camera_ref and camera_ref.get("cloud_angle") is not None else dominant_gradient_angle(Lc)
        row["grad_slope"] = rise_width_10_90(projected_profile(Lc, angle))
    box = (align.get("sky_bright") or {}).get("box")
    if box is not None:
        row.update(sky_color(_crop(rgb01, box), _crop(white_ch, box)))
    box = (align.get("fg_dark") or {}).get("box")
    if box is not None:
        Lf = _crop(L, box)
        row["fg_noise"] = flat_noise(np.clip(Lf * 255.0, 0, 255).astype(np.uint8))
        row["fg_p01"] = float(np.percentile(Lf, 1))
        if camera_ref and _is_number(camera_ref.get("fg_p01")):
            row["fg_p01_diff"] = row["fg_p01"] - camera_ref["fg_p01"]
    return row


def camera_reference(camera_rgb8: np.ndarray, rois: dict[str, tuple[int, int, int, int] | None]) -> dict[str, Any]:
    """カメラ JPEG の基準値（mid_p50・cloud_grad の L>0.8 マスク・grad_texture・勾配方向・fg_p01）。"""
    L = luma_of(camera_rgb8.astype(np.float32) / 255.0)
    ref: dict[str, Any] = {}
    if rois.get("midtone"):
        ref["mid_p50"] = float(np.percentile(_crop(L, rois["midtone"]), 50))
    if rois.get("cloud_grad"):
        Lc = _crop(L, rois["cloud_grad"])
        ref["cloud_mask"] = Lc > 0.8
        ref["grad_texture"] = grad_texture(Lc, ref["cloud_mask"])
        ref["cloud_angle"] = dominant_gradient_angle(Lc)
    if rois.get("fg_dark"):
        ref["fg_p01"] = float(np.percentile(_crop(L, rois["fg_dark"]), 1))
    return ref


def _roi_attr(counts: dict[str, int] | None, prefix: str, roi_pixels: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if counts is None:
        return out
    k = counts.get("k", 0)
    out[f"{prefix}_k_frac"] = k / roi_pixels if roi_pixels else float("nan")
    for c in ATTR_CATEGORIES:
        out[f"{prefix}_{c}"] = counts.get(c, 0) / k if k else float("nan")
    return out


def diagnosis_measure(
    images: dict[str, np.ndarray],
    white: dict[str, np.ndarray],
    levels: dict[str, int],
    dumps: dict[str, np.ndarray],
    cfa: dict[str, Any] | None,
    rois: dict[str, tuple[int, int, int, int] | None],
    roi_size: int,
    *,
    p3_native_white: np.ndarray | None = None,
    cli_any_gt_1: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """1 カット分の指標・帰属・シーン判定（ファイル I/O 無し。同寸に揃えた配列を受ける）。

    - images: variant → RGB 0..1 float（camera / rawpy / d_* / c*。tiff は 16bit 値、jpeg は 8bit 値）
    - white: variant → チャンネル別の白マスク（jpeg は 255、tiff は 65535。P3 tiff は sRGB へ変換後）
    - dumps: d_s1_linear / d_s2_tone / d_s3_default → 線形 float（lens OFF 幾何）
    - cfa: {"metrics", "map_libraw", "map_camera", "thumb"}（マップ・サムネイルは同寸・正立、位置合わせ前）
    返す: (variant → 指標行, summary)。行には "stages"（段階・帰属・シーン判定）と "cfa" を含む。
    """
    camera = images["camera"]
    cam_gray = _gray8(camera)
    cam_ref = camera_reference(to8(camera), rois)
    rows: dict[str, dict[str, Any]] = {}
    aligns: dict[str, dict[str, dict[str, Any]]] = {}
    for variant, rgb in images.items():
        align = identity_alignment(rois) if variant == "camera" else align_rois(cam_gray, _gray8(rgb), rois, roi_size)
        aligns[variant] = align
        rows[variant] = image_variant_metrics(rgb, white[variant], levels.get(variant, 255), align, cam_ref)
    if p3_native_white is not None and "d_tiff_p3" in rows:
        for i, ch in enumerate("rgb"):
            rows["d_tiff_p3"][f"clip16p3_{ch}"] = float(p3_native_white[..., i].mean())
        rows["d_tiff_p3"]["clip16p3_any"] = float(p3_native_white.any(axis=2).mean())
    stages: dict[str, Any] = {}
    summary: dict[str, Any] = {"alignment": {v: {n: {k: a.get(k) for k in ("box", "dx", "dy", "response", "ok", "shift_source")}
                                                  for n, a in al.items()} for v, al in aligns.items()}}
    # --- 段階（全画面） ---
    s1, s2, s3 = (dumps.get(v) for v in DIAG_STAGE_VARIANTS)
    white_f = {name: (d >= WHITE16_LINEAR).any(axis=2) for name, d in (("s1", s1), ("s2", s2), ("s3", s3)) if d is not None}
    if s1 is not None:
        stages.update(stage_rates(s1, "s1_gt1"))
    if s2 is not None:
        stages.update(stage_rates(s2, "s2_gt1"))
    if s3 is not None:
        stages.update(stage_rates(s3, "s3_ge1", WHITE16_LINEAR, strict=False))
    for name, dump in zip(("s1", "s2", "s3"), (s1, s2, s3)):
        variant = DIAG_STAGE_VARIANTS[("s1", "s2", "s3").index(name)]
        cli = (cli_any_gt_1 or {}).get(variant)
        if dump is not None and _is_number(cli):
            stages[f"{name}_cli_any_gt_1_diff"] = float((dump > 1.0).any(axis=2).mean()) - float(cli)
    if "s1" in white_f and "s2" in white_f:
        stages["stage_loss_s1s2"] = stage_loss(white_f["s1"], white_f["s2"])
    if "s2" in white_f and "s3" in white_f:
        stages["stage_loss_s2s3"] = stage_loss(white_f["s2"], white_f["s3"])
    if "s3" in white_f and "d_tiff_srgb" in white:
        # S3 は lens OFF、S4 は lens ON で幾何が違うので画素単位にできない。白の率の差で代える（符号付き）。
        stages["stage_loss_s3s4"] = float(white["d_tiff_srgb"].any(axis=2).mean()) - float(white_f["s3"].mean())
    if "d_tiff_srgb" in white and "d_base" in white:
        stages["stage_loss_s4s5"] = stage_loss(white["d_tiff_srgb"].any(axis=2), white["d_base"].any(axis=2))
    for key in ("clip16_any", "clip16_b"):
        if key in rows.get("d_tiff_srgb", {}):
            stages[key] = rows["d_tiff_srgb"][key]
    for key in ("clip8_any", "clip8_b", "blue_only_clip"):
        if key in rows.get("d_base", {}):
            stages[key] = rows["d_base"][key]
    # S2 vs S3: S2 が [0,1] の画素の差（§8.4）と ΔE76（§8.2 delta_e76_inrange）
    if s2 is not None and s3 is not None and s2.shape == s3.shape:
        inrange = ((s2 >= 0.0) & (s2 <= 1.0)).all(axis=2)
        stages["s2_inrange_frac"] = float(inrange.mean())
        if inrange.any():
            diff = np.abs(s2 - s3).max(axis=2)[inrange]
            stages["s2s3_inrange_absdiff_mean"] = float(diff.mean())
            stages["s2s3_inrange_absdiff_max"] = float(diff.max())
            lab2 = cv2.cvtColor(srgb_encode(np.clip(s2, 0, 1)), cv2.COLOR_RGB2Lab)[inrange]
            lab3 = cv2.cvtColor(srgb_encode(np.clip(s3, 0, 1)), cv2.COLOR_RGB2Lab)[inrange]
            stages["delta_e76_inrange"] = float(np.sqrt(((lab2 - lab3) ** 2).sum(axis=1)).mean())
    # lens ON/OFF のクリップ差（§7.3 の前提）
    if "d_base" in rows and "d_lens_off" in rows:
        stages["lens_clip_diff"] = rows["d_base"].get("clip8_any", float("nan")) - rows["d_lens_off"].get("clip8_any", float("nan"))
        for name in DIAG_CLIP_ROIS:
            a, b = rows["d_base"].get(f"{name}_clip8_any"), rows["d_lens_off"].get(f"{name}_clip8_any")
            if _is_number(a) and _is_number(b):
                stages[f"{name}_lens_clip_diff"] = a - b
    # --- lens OFF 幾何での画素照合（§7.3 / §10.1） ---
    off_align = aligns.get("d_lens_off")
    off_boxes = {n: (off_align or {}).get(n, {}).get("box") for n in DIAG_ROI_NAMES}
    cfa_ok = False
    sensor_maps: dict[str, np.ndarray | None] = {"libraw": None, "camera": None}
    if cfa is not None and "d_lens_off" in images:
        thumb = cfa.get("thumb")
        if thumb is not None and thumb.shape == cam_gray.shape:
            thumb8 = to8(srgb_encode(np.clip(thumb / max(1e-6, float(np.percentile(thumb, 99.5))), 0, 1)))
            dx, dy, response = global_shift(_gray8(images["d_lens_off"]), thumb8)
            cfa_ok = response >= ROI_MIN_RESPONSE and math.hypot(dx, dy) <= roi_size / 4.0
            stages.update({"cfa_shift_dx": dx, "cfa_shift_dy": dy, "cfa_shift_response": response, "cfa_align_ok": cfa_ok})
            if cfa_ok:
                for tag in ("libraw", "camera"):
                    sensor_maps[tag] = shift_mask(cfa[f"map_{tag}"] > 0, -dx, -dy)
    stage_ok = all(d is not None and d.shape[:2] == cam_gray.shape for d in (s2, s3))
    s2_over = (s2 > 1.0).any(axis=2) if stage_ok else None
    s3_white8 = (s3 >= WHITE8_LINEAR).any(axis=2) if stage_ok else None
    s3_near_white = (s3 >= JPEG_NEAR_WHITE_LINEAR).any(axis=2) if stage_ok else None
    # K（白の画素集合）は S3 dump の「8bit sRGB で 255 に丸まる値」（≥ WHITE8_LINEAR、lens OFF 幾何）から取る
    # （レポート §6.4 / §8-5）。8bit JPEG の d_lens_off から取ると、原寸の lens OFF で白の手前の画素が JPEG の量子化で
    # 255 → 254 に落ちて白を数え損なう。lens OFF の tiff16 は現像していない（d_tiff_srgb は lens ON で幾何が違う）ので
    # 使わない。dump が無い・寸法が合わない（stage_ok False）ときは従来どおり d_lens_off の 8bit JPEG の白に戻す
    # （この経路は attribute_pixels が K 全体を unverifiable にするので、効くのは k_frac だけ）。
    # 比較用に JPEG の K の割合も attr_{roi}_k_jpeg8_frac に残す。
    clipped_jpeg8 = white["d_lens_off"].any(axis=2) if "d_lens_off" in white else None
    if stage_ok and s3_white8 is not None and "d_lens_off" in white:
        clipped_off, k_source = s3_white8, "s3_dump"
    else:
        clipped_off, k_source = clipped_jpeg8, "jpeg8"
    stages["k_source"] = k_source if clipped_off is not None else None
    attribution: dict[str, Any] = {}
    roi_class: dict[str, str] = {}
    roi_counts: dict[str, dict[str, int] | None] = {}
    for name in ("sky_bright", "cloud_grad", "full"):
        box = (0, 0, cam_gray.shape[1], cam_gray.shape[0]) if name == "full" else off_boxes.get(name)
        if clipped_off is None or (box is None and rois.get(name) is not None):
            roi_class[name] = "unverifiable" if rois.get(name) is not None or name == "full" else "no_roi"
            roi_counts[name] = None
            continue
        if box is None:
            roi_class[name], roi_counts[name] = "no_roi", None
            continue
        roi_pixels = (box[2] - box[0]) * (box[3] - box[1])
        classes = {}
        for tag in ("libraw", "camera"):
            counts = attribute_pixels(
                _crop(clipped_off, box), _crop(sensor_maps[tag], box) if sensor_maps[tag] is not None else None,
                _crop(s2_over, box) if s2_over is not None else None, _crop(s3_white8, box) if s3_white8 is not None else None,
                valid=cfa_ok and stage_ok,
                s3_near_white=_crop(s3_near_white, box) if s3_near_white is not None else None,
            )
            classes[tag] = classify_attribution(counts, roi_pixels)
            if tag == "libraw":
                roi_counts[name] = counts
                attribution.update(_roi_attr(counts, f"attr_{name}", roi_pixels))
        roi_class[name] = classes["libraw"]
        if clipped_jpeg8 is not None:
            attribution[f"attr_{name}_k_jpeg8_frac"] = float(_crop(clipped_jpeg8, box).mean())
            if k_source == "s3_dump":
                # 参考: 旧来の K（8bit JPEG の白）に同じ規則を当てた内訳（attr_{roi}_jpeg8_*）。K を S3 から取ると
                # jpeg_encode / unverifiable は構造上 0 になるので、JPEG だけで白になった画素の量はこちらで見る（レポート §3.3）。
                jcounts = attribute_pixels(
                    _crop(clipped_jpeg8, box), _crop(sensor_maps["libraw"], box) if sensor_maps["libraw"] is not None else None,
                    _crop(s2_over, box), _crop(s3_white8, box), valid=cfa_ok and stage_ok,
                    s3_near_white=_crop(s3_near_white, box),
                )
                jk = jcounts["k"]
                attribution.update({f"attr_{name}_jpeg8_{c}": jcounts[c] / jk if jk else float("nan") for c in ATTR_CATEGORIES})
        attribution[f"attr_{name}_class"] = classes["libraw"]
        attribution[f"attr_{name}_class_camera_threshold"] = classes["camera"]
        attribution[f"attr_{name}_sensor_threshold_sensitive"] = classes["libraw"] != classes["camera"]
        if name != "full":
            for prefix, d, strict, thr in (("s1_gt1", s1, True, 1.0), ("s2_gt1", s2, True, 1.0), ("s3_ge1", s3, False, WHITE16_LINEAR)):
                if d is not None and d.shape[:2] == cam_gray.shape:
                    attribution.update({f"{name}_{k}": v for k, v in stage_rates(_crop(d, box), prefix, thr, strict).items()
                                        if k.endswith(("_any", "_b"))})
            if sensor_maps["libraw"] is not None:
                attribution[f"{name}_cfa_sat_cell_any"] = float(_crop(sensor_maps["libraw"], box).mean())
    stages.update(attribution)
    # S1 / S2 に cloud_grad の階調が残るか（§9 の実施条件・§10.2）。dump を表示化して camera のマスクで測る。
    cloud_box = off_boxes.get("cloud_grad")
    mask = cam_ref.get("cloud_mask")
    for name, dump in (("s1", s1), ("s2", s2)):
        if dump is not None and cloud_box is not None and mask is not None and _is_number(cam_ref.get("grad_texture")) and cam_ref["grad_texture"] > 0:
            Lc = display_luma(_crop(dump, cloud_box))
            if Lc.shape == mask.shape:
                stages[f"{name}_grad_texture_ratio"] = grad_texture(Lc, mask) / cam_ref["grad_texture"]
    # §8.4 トーン曲線の大域性: S1 に §2.4 の曲線を掛けて S2 と比べる（cloud_grad / midtone の ΔL p99）
    if s1 is not None and s2 is not None and s1.shape == s2.shape:
        curve = pchip_curve(TONE_CURVE_BOOST1)
        verdicts = []
        for name in ("cloud_grad", "midtone"):
            box = off_boxes.get(name)
            if box is None:
                continue
            pred = curve(_crop(s1, box).astype(np.float64)).astype(np.float32)
            dl = np.abs(luma_of(pred) - luma_of(_crop(s2, box)))
            stages[f"curve_check_dl_p99_{name}"] = float(np.percentile(dl, 99))
            verdicts.append(stages[f"curve_check_dl_p99_{name}"] <= CURVE_CHECK_TOL)
        stages["curve_global"] = all(verdicts) if verdicts else None
    # --- 露出掃引の内挿（§8.3） ---
    sweep = [(rows[v].get("mid_p50"), v) for v, _ in DIAG_EV_SWEEP if v in rows]
    interp_rows = {}
    for target_name, target_p50 in (("camera", rows.get("camera", {}).get("mid_p50")), ("rawpy", rows.get("rawpy", {}).get("mid_p50"))):
        if not _is_number(target_p50) or len(sweep) < 2:
            continue
        keys = sorted({k for _, v in sweep for k, val in rows[v].items() if _is_number(val) and not isinstance(val, bool)})
        irow: dict[str, Any] = {"interpolated": True, "interp_target": target_name, "interp_target_p50": target_p50}
        for key in keys:
            irow[key] = interp_at([(rows[v].get("mid_p50"), rows[v].get(key)) for _, v in sweep], target_p50)
        irow["interp_ok"] = _is_number(irow.get("mid_p50"))
        irow["interp_ev"] = interp_at([(rows[v].get("mid_p50"), ev) for v, ev in DIAG_EV_SWEEP if v in rows], target_p50)
        interp_rows[f"d_interp_{target_name}_p50"] = irow
    rows.update(interp_rows)
    # --- タスク C の合否と c_best（§9） ---
    cam_row = rows.get("camera", {})
    for name in (*C_CANDIDATES, "c_best"):
        if name in rows:
            rows[name].update(candidate_checks(rows[name], cam_row))
    best = choose_c_best(rows)
    eligible_ratios = [stages.get("s1_grad_texture_ratio"), stages.get("s2_grad_texture_ratio")]
    eligible = any(_is_number(r) and r >= GRAD_RECOVER_MIN for r in eligible_ratios)
    stages["taskc_eligible"] = eligible
    stages["c_best"] = best["best"]
    stages["c_best_within_tol"] = best["within_tol"]
    # --- シーン判定（§10.2） ---
    scene = classify_scene_diagnosis(
        roi_class, roi_counts,
        [rows.get(v, {}).get("grad_texture_ratio") for v in ("d_ev-07", "d_ev-10")],
        stages.get("s1_grad_texture_ratio", float("nan")), stages.get("cloud_grad_cfa_sat_cell_any", float("nan")),
    )
    stages["scene_class"] = scene
    rows["stages"] = stages
    if cfa is not None:
        rows["cfa"] = dict(cfa.get("metrics") or {})
    summary.update({"roi_class": roi_class, "scene_class": scene, "c_best": best, "taskc_eligible": eligible,
                    "cfa_align_ok": cfa_ok, "stages_ok": stage_ok})
    return rows, summary


# --- 実行（CLI 呼び出し・I/O） -------------------------------------------------------


def develop_diagnosis_variants(
    ctx: RunContext,
    capture_id: str,
    mode: str,
    specs: dict[str, dict[str, Any]],
    *,
    with_rawpy: bool = True,
    workers: int = 4,
) -> dict[str, dict[str, Any]]:
    """CI バリアント（と rawpy）を現像する。full の dump（288 MB）は同時 1 本にする（§6.3 / §12）。"""
    from concurrent.futures import ThreadPoolExecutor

    def one(variant: str) -> dict[str, Any]:
        spec = specs[variant]
        dump_path = diagnosis_dump_path(ctx.out, capture_id, mode, variant) if spec.get("dump") else None
        if dump_path is not None:
            dump_path.parent.mkdir(parents=True, exist_ok=True)
        kwargs = diagnosis_request_kwargs(spec, dump_path)
        row = run_coreimage(ctx, capture_id, mode, spec["lens"], variant=variant, **kwargs)
        row["spec_label"] = spec.get("label")
        return row

    results: dict[str, dict[str, Any]] = {}
    serial = [v for v in specs if specs[v].get("dump")] if mode == "full" else []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {v: pool.submit(one, v) for v in specs if v not in serial}
        if with_rawpy and ctx.legacy_rawpy:
            jobs["rawpy"] = pool.submit(run_rawpy, ctx, capture_id, mode)
        results.update({v: f.result() for v, f in jobs.items()})
    for variant in serial:
        results[variant] = one(variant)
    return results


def _read_response(row: dict[str, Any]) -> dict[str, Any]:
    output = row.get("output")
    if not output:
        return {}
    path = Path(output).with_suffix(".json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_diagnosis_images(
    engine_rows: dict[str, dict[str, Any]],
    target: tuple[int, int],
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict[str, int], dict[str, np.ndarray], dict[str, Any], np.ndarray | None]:
    """現像結果を読み、target 寸法に揃える。dump は応答の meta を検査して読む（§4.4）。"""
    images: dict[str, np.ndarray] = {}
    white: dict[str, np.ndarray] = {}
    levels: dict[str, int] = {}
    dumps: dict[str, np.ndarray] = {}
    cli_any: dict[str, Any] = {}
    p3_native = None
    for variant, row in engine_rows.items():
        if row.get("error"):
            continue
        try:
            payload = _read_response(row) if variant != "rawpy" else {}
            diag = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else None
            if diag and isinstance(diag.get("dump_float"), dict):
                dump = load_dump(diag["dump_float"])
                dumps[variant], row["fit"] = fit_to_target(dump, target)
                cli_any[variant] = (diag.get("stats") or {}).get("any_gt_1")
                continue
            if diag is not None and diag.get("written") is False:
                continue
            path = Path(row["output"])
            if path.suffix.lower() in (".tif", ".tiff"):
                rgb = read_tiff16_rgb(path)
                rgb, row["fit"] = fit_to_target(rgb, target)
                if variant == "d_tiff_p3":
                    p3_native = rgb >= (1.0 - 0.5 / 65535.0)
                    rgb_srgb = p3_to_srgb(rgb)
                    white[variant] = rgb_srgb >= (1.0 - 0.5 / 65535.0)
                    rgb = np.clip(rgb_srgb, 0.0, 1.0)
                else:
                    white[variant] = rgb >= (1.0 - 0.5 / 65535.0)
                images[variant], levels[variant] = rgb.astype(np.float32), 65535
            else:
                src = read_rgb_upright(path)
                row["native_width"], row["native_height"] = src.shape[1], src.shape[0]
                rgb8, row["fit"] = fit_to_target(src, target)
                images[variant], white[variant], levels[variant] = rgb8.astype(np.float32) / 255.0, rgb8 >= 255, 255
        except Exception as error:  # noqa: BLE001 — 1 バリアントの読み込み失敗は欠測にする
            row["error"] = f"load failed: {type(error).__name__}: {error}"
    return images, white, levels, dumps, cli_any, p3_native


def diagnosis_capture(
    ctx: RunContext,
    capture_id: str,
    mode: str,
    features: dict[str, Any],
    category: str,
    *,
    cfa: dict[str, Any] | None,
    best: str | None = None,
    with_rawpy: bool = True,
    keep_dumps: bool = False,
    workers: int = 4,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """1 カット・1 mode の診断: 現像 → 読み込み → ROI → 指標・帰属 → シート → dump 削除。"""
    specs = diagnosis_mode_specs(mode, best)
    engine_rows = develop_diagnosis_variants(ctx, capture_id, mode, specs, with_rawpy=with_rawpy, workers=workers)
    try:
        camera = read_rgb_upright(source_file(ctx.source_root, capture_id, "JPG"))
        ch, cw = camera.shape[:2]
        target = fit_size(cw, ch, ctx.max_edge) if mode == "preview" else (cw, ch)
        images, white, levels, dumps, cli_any, p3_native = load_diagnosis_images(engine_rows, target)
        cam8 = resize_to(camera, target)
        images["camera"], white["camera"], levels["camera"] = cam8.astype(np.float32) / 255.0, cam8 >= 255, 255
        roi_size = max(8, int(round(ROI_FRACTION * max(target))))
        rois, roi_info = diagnosis_rois(cam8, roi_size)
        cfa_t = None
        if cfa is not None:
            cfa_t = {
                "metrics": cfa["metrics"],
                "map_libraw": resize_cell_map(cfa["map_libraw"], target),
                "map_camera": resize_cell_map(cfa["map_camera"], target),
                "thumb": cv2.resize(cfa["thumb"], target, interpolation=cv2.INTER_AREA if cfa["thumb"].shape[1] >= target[0] else cv2.INTER_LINEAR),
            }
        metrics, summary = diagnosis_measure(images, white, levels, dumps, cfa_t, rois, roi_size,
                                             p3_native_white=p3_native, cli_any_gt_1=cli_any)
        diagnosis_sheets(ctx, capture_id, mode, images, rois, metrics, best or summary["c_best"].get("best"))
    finally:
        if not keep_dumps:
            for variant, spec in specs.items():
                if spec.get("dump"):
                    with contextlib.suppress(FileNotFoundError):
                        diagnosis_dump_path(ctx.out, capture_id, mode, variant).unlink()
    base = {"capture_id": capture_id, "mode": mode, "category": category, "iso": features.get("iso"),
            "lens_model": features.get("lens_model"), "focal_length": features.get("focal_length"),
            "width": target[0], "height": target[1], "roi_size": roi_size}
    rows = []
    order = ["camera", "rawpy", *specs, *(["c_best"] if mode == "preview" else []), "d_interp_camera_p50", "d_interp_rawpy_p50", "cfa", "stages"]
    for variant in order:
        if variant == "rawpy" and not with_rawpy:
            continue
        if variant == "c_best" and mode == "preview":
            name = summary["c_best"].get("best")
            if not name:
                continue
            row = {**base, "variant": "c_best", "c_best_of": name, **engine_rows.get(name, {}), **metrics.get(name, {})}
            rows.append(row)
            continue
        if variant not in metrics and variant not in engine_rows:
            continue
        row = {**base, "variant": variant, **engine_rows.get(variant, {}), **metrics.get(variant, {})}
        if variant == "c_best":
            row["c_best_of"] = best
        if variant in engine_rows and variant not in metrics and not row.get("error") and variant not in DIAG_STAGE_VARIANTS:
            row["error"] = "no image"
        rows.append(row)
    summary.update({"rois": {k: list(v) if v else None for k, v in rois.items()}, "roi_info": roi_info,
                    "target": list(target), "dumps_kept": keep_dumps})
    return rows, summary


def diagnosis_sheets(
    ctx: RunContext,
    capture_id: str,
    mode: str,
    images: dict[str, np.ndarray],
    rois: dict[str, tuple[int, int, int, int] | None],
    metrics: dict[str, dict[str, Any]],
    best: str | None,
) -> None:
    """目視シート（§9 (6)）: ROI 枠付きのカメラ全景と、camera / rawpy / d_base / 候補 の 4 列
    （全景・cloud_grad 等倍・fg_dark 等倍）。EXIF は付けない。"""
    camera = to8(images["camera"])
    th, tw = camera.shape[:2]
    small = fit_size(tw, th, 600)
    sx = small[0] / tw
    canvas = Image.fromarray(resize_to(camera, small))
    draw = ImageDraw.Draw(canvas)
    colors = {"sky_bright": (80, 160, 255), "cloud_grad": (255, 255, 255), "clip_edge": (255, 80, 200),
              "fg_dark": (255, 200, 60), "midtone": (120, 255, 120)}
    for name, box in rois.items():
        if box is None:
            continue
        x0, y0, x1, y1 = (int(round(v * sx)) for v in box)
        draw.rectangle((x0, y0, x1, y1), outline=colors.get(name, (255, 0, 0)), width=2)
        draw.text((x0 + 3, y0 + 2), name, fill=colors.get(name, (255, 0, 0)), font=_font(14))
    save_plain_jpeg(np.asarray(canvas), ctx.out / "sheets" / f"{capture_id}_{mode}_rois.jpg", quality=92)
    candidates = [c for c in (*C_CANDIDATES, "c_best") if c in images]
    if mode == "preview" and best in images:
        candidates = [best] + [c for c in candidates if c != best]
    overview = fit_size(tw, th, 400)
    for cand in candidates:
        columns = [v for v in ("camera", "rawpy", "d_base", cand) if v in images]
        sheet_rows = []
        panels = []
        for v in columns:
            m = metrics.get(v, {})
            label = f"{v}\nclip {m.get('clip8_any', float('nan')) * 100:.2f}%  p50d {m.get('mid_p50_diff', float('nan')):+.3f}"
            panels.append((label, resize_to(to8(images[v]), overview)))
        sheet_rows.append(labeled_row(panels, f"{capture_id} {mode} overview  ({cand})"))
        for roi in ("cloud_grad", "fg_dark"):
            box = rois.get(roi)
            if box is None:
                continue
            crops = []
            for v in columns:
                a = align_rois(_gray8(images["camera"]), _gray8(images[v]), {roi: box}, box[2] - box[0]).get(roi, {}) if v != "camera" else {"box": box}
                vbox = a.get("box") or box
                m = metrics.get(v, {})
                key = "grad_texture_ratio" if roi == "cloud_grad" else "fg_p01_diff"
                crops.append((f"{v} {roi}\n{key} {m.get(key, float('nan')):.3f}", to8(_crop(images[v], vbox))))
            sheet_rows.append(labeled_row(crops, f"{roi} 1:1"))
        width = max(r.shape[1] for r in sheet_rows)
        padded = [np.pad(r, ((0, 8), (0, width - r.shape[1]), (0, 0)), constant_values=24) for r in sheet_rows]
        save_plain_jpeg(np.concatenate(padded, axis=0), ctx.out / "sheets" / f"{capture_id}_{mode}_{cand}.jpg", quality=92)


def diagnosis_tables_md(rows: list[dict[str, Any]], summaries: list[dict[str, Any]]) -> str:
    """レポート転記用の Markdown（シーン判定・帰属・段階・候補合否・バリアント別の中央値）。"""
    def fmt(v: Any, digits: int = 4) -> str:
        if v is None:
            return "—"
        if isinstance(v, bool):
            return "yes" if v else "no"
        if isinstance(v, float):
            return "NaN" if not math.isfinite(v) else f"{v:.{digits}f}"
        return str(v)

    stage_rows = [r for r in rows if r.get("variant") == "stages"]
    cfa_by = {(r["capture_id"], r["mode"]): r for r in rows if r.get("variant") == "cfa"}
    lines = ["## シーン判定と帰属（§10）", "",
             "| capture | mode | scene | sky_bright | cloud_grad | sens.(cam thr) | cfa G | cfa B | cfa cell_any | s1>1 any | s2>1 any | s3≥1 any | clip8 any | c_best | eligible |",
             "|" + "---|" * 15]
    for r in stage_rows:
        c = cfa_by.get((r["capture_id"], r["mode"]), {})
        sens = r.get("attr_sky_bright_sensor_threshold_sensitive") or r.get("attr_cloud_grad_sensor_threshold_sensitive")
        lines.append("| " + " | ".join([
            r["capture_id"], r["mode"], fmt(r.get("scene_class")), fmt(r.get("attr_sky_bright_class")),
            fmt(r.get("attr_cloud_grad_class")), fmt(sens), fmt(c.get("cfa_sat_g")), fmt(c.get("cfa_sat_b")),
            fmt(c.get("cfa_sat_cell_any")), fmt(r.get("s1_gt1_any")), fmt(r.get("s2_gt1_any")), fmt(r.get("s3_ge1_any")),
            fmt(r.get("clip8_any")), fmt(r.get("c_best")), fmt(r.get("taskc_eligible"))]) + " |")
    lines += ["", "## 帰属の内訳（K に占める割合、lens OFF 幾何。K の出所は k_source）", "",
              "quant8 = 8bit 量子化（白の手前 1/255、旧「色域・SDR 出力」）、jpeg_encode = JPEG 符号化（S3 ≥ 0.98、旧「検証不能」の一部）。"
              "K/ROI(jpeg8) は d_lens_off の 8bit JPEG で数えた K の割合（比較用）。", "",
              "| capture | mode | ROI | k_source | K/ROI | K/ROI(jpeg8) | sensor | decoder_tone | quant8 | jpeg_encode | unverifiable |",
              "|" + "---|" * 11]
    for r in stage_rows:
        for roi in ("sky_bright", "cloud_grad", "full"):
            if f"attr_{roi}_k_frac" not in r:
                continue
            lines.append("| " + " | ".join([r["capture_id"], r["mode"], roi, fmt(r.get("k_source")),
                                            fmt(r.get(f"attr_{roi}_k_frac")), fmt(r.get(f"attr_{roi}_k_jpeg8_frac"))]
                                           + [fmt(r.get(f"attr_{roi}_{c}")) for c in ATTR_CATEGORIES]) + " |")
    lines += ["", "## 参考: K を 8bit JPEG（d_lens_off の 255）で取った場合の内訳（K/ROI(jpeg8) に占める割合）", "",
              "| capture | mode | ROI | K/ROI(jpeg8) | sensor | decoder_tone | quant8 | jpeg_encode | unverifiable |",
              "|" + "---|" * 9]
    for r in stage_rows:
        for roi in ("sky_bright", "cloud_grad", "full"):
            if f"attr_{roi}_jpeg8_sensor" not in r:
                continue
            lines.append("| " + " | ".join([r["capture_id"], r["mode"], roi, fmt(r.get(f"attr_{roi}_k_jpeg8_frac"))]
                                           + [fmt(r.get(f"attr_{roi}_jpeg8_{c}")) for c in ATTR_CATEGORIES]) + " |")
    lines += ["", "## 段階と検査（§8.1 / §8.4）", "",
              "| capture | mode | loss s1s2 | loss s2s3 | loss s3s4 (rate diff) | loss s4s5 | ΔE in-range | S2/S3 absdiff mean | curve ΔL p99 cloud | curve ΔL p99 mid | lens clip diff |",
              "|" + "---|" * 11]
    for r in stage_rows:
        lines.append("| " + " | ".join([r["capture_id"], r["mode"]] + [fmt(r.get(k)) for k in (
            "stage_loss_s1s2", "stage_loss_s2s3", "stage_loss_s3s4", "stage_loss_s4s5", "delta_e76_inrange",
            "s2s3_inrange_absdiff_mean", "curve_check_dl_p99_cloud_grad", "curve_check_dl_p99_midtone", "lens_clip_diff")]) + " |")
    cand_rows = [r for r in rows if r.get("variant") in C_CANDIDATES]
    lines += ["", "## タスク C 候補の合否（§9。(6) 目視はオーナー）", "",
              "| capture | mode | candidate | mid_p50_diff | sky clip8 | grad_texture_ratio | fg_p01_diff | magenta | p50 | clip | texture | fg | magenta | auto |",
              "|" + "---|" * 14]
    for r in cand_rows:
        lines.append("| " + " | ".join([r["capture_id"], r["mode"], r["variant"]] + [fmt(r.get(k)) for k in (
            "mid_p50_diff", "sky_bright_clip8_any", "grad_texture_ratio", "fg_p01_diff", "magenta_rate",
            "c_pass_p50", "c_pass_clip", "c_pass_texture", "c_pass_fg", "c_pass_magenta", "c_pass_auto")]) + " |")
    variant_rows = [r for r in rows if r.get("variant") not in ("stages", "cfa") and not r.get("error")]
    by_variant = summarize_sharpness(variant_rows, DIAG_TABLE_KEYS, ("mode", "variant"))
    lines += ["", "## バリアント別の中央値", "", table_md(by_variant, ("mode", "variant"), DIAG_TABLE_KEYS, digits=4)]
    return "\n".join(lines) + "\n"


def diagnosis_plan(ctx: RunContext, capture_ids: list[str], full_ids: list[str]) -> list[dict[str, Any]]:
    """--diagnosis --dry-run: 組んだリクエストと契約検査の結果（CLI は呼ばない）。full の c_best は未定なので除く。"""
    plan = []
    for cid, mode in [(c, "preview") for c in capture_ids] + [(c, "full") for c in full_ids]:
        for variant, spec in diagnosis_mode_specs(mode).items():
            dump_path = diagnosis_dump_path(ctx.out, cid, mode, variant) if spec.get("dump") else None
            kwargs = diagnosis_request_kwargs(spec, dump_path)
            output = coreimage_output_path(ctx, cid, mode, spec["lens"], variant=variant, output_format=kwargs["output_format"])
            request = coreimage_request(source_file(ctx.source_root, cid, "ARW"), output, mode, spec["lens"],
                                        max_edge=ctx.max_edge, **kwargs)
            plan.append({"capture_id": cid, "mode": mode, "variant": variant, "request": request,
                         "problems": validate_diagnostics_request(request)})
    return plan


def diagnosis_dry_run(ctx: RunContext, capture_ids: list[str], full_ids: list[str]) -> None:
    plan = diagnosis_plan(ctx, capture_ids, full_ids)
    version = cli_probe(ctx.cli, "--version")
    capabilities = cli_probe(ctx.cli, "--capabilities") if not version.get("error") else {}
    problem = diagnosis_cli_problem(version, capabilities)
    problems = [p for p in plan if p["problems"]]
    payload = {"capture_ids": capture_ids, "full_ids": full_ids, "cli": str(ctx.cli), "cli_version": version,
               "cli_ready": problem is None, "cli_problem": problem, "count": len(plan),
               "contract_problems": [{k: p[k] for k in ("capture_id", "mode", "variant", "problems")} for p in problems],
               "plan": plan}
    (ctx.out / "diagnosis_plan.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ctx.say(f"[diagnosis] dry-run: CI {len(plan)} リクエスト → {ctx.out / 'diagnosis_plan.json'}")
    if problem:
        ctx.say(f"[diagnosis] 注意: 実行時はここで止まります — {problem}")
    if problems:
        raise SystemExit(f"--diagnosis のリクエストが設計書 §4 の契約に反します（{len(problems)} 件）")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.floating, float)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, np.ndarray):
        return None
    return value


def run_diagnosis(
    ctx: RunContext,
    selection: list[dict[str, Any]],
    features_by_id: dict[str, dict[str, Any]],
    full_ids: list[str],
    *,
    keep_dumps: bool = False,
    with_rawpy: bool = True,
    cfa_loader: Any = None,
    environment: dict[str, Any] | None = None,
) -> None:
    """--diagnosis 本体: preview を全カット回してから full（c_best は preview の結果で決める）。"""
    import shutil

    if cfa_loader is not None:
        loader = cfa_loader
    elif ctx.legacy_rawpy:
        loader = lambda cid, orientation: cfa_from_file(source_file(ctx.source_root, cid, "ARW"), orientation)  # noqa: E731
    else:
        # CFA 飽和（センサー段の帰属）は rawpy でしか読めない旧経路。既定では計測せず unverifiable に寄せる。
        loader = lambda cid, orientation: None  # noqa: E731
    started = time.monotonic()
    free = shutil.disk_usage(ctx.out).free
    if free < 5 * 1024**3:
        ctx.say(f"[diagnosis] 注意: 出力先の空きが {free / 1024**3:.1f} GB です（§12 の目安は 5 GB 以上）")
    rows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    rois_out: dict[str, dict[str, Any]] = {}
    cfa_cache: dict[str, dict[str, Any] | None] = {}
    best_by_id: dict[str, str | None] = {}
    errors: list[dict[str, Any]] = []
    cat_by_id = {r["capture_id"]: r["category"] for r in selection}
    jobs = [(r["capture_id"], "preview") for r in selection] + [(cid, "full") for cid in full_ids]
    for index, (cid, mode) in enumerate(jobs, 1):
        ctx.say(f"[diagnosis] ({index}/{len(jobs)}) {cid} {mode}")
        features = features_by_id[cid]
        try:
            if cid not in cfa_cache:
                try:
                    cfa_cache[cid] = loader(cid, features.get("orientation"))
                except Exception as error:  # noqa: BLE001 — CFA が読めなくても CI 側の指標は出す
                    cfa_cache[cid] = None
                    errors.append({"capture_id": cid, "mode": mode, "variant": "cfa", "error": f"{type(error).__name__}: {error}"})
                    ctx.say(f"  ERROR cfa: {type(error).__name__}: {error}")
            new_rows, summary = diagnosis_capture(
                ctx, cid, mode, features, cat_by_id.get(cid, "other"), cfa=cfa_cache[cid],
                best=best_by_id.get(cid) if mode == "full" else None, with_rawpy=with_rawpy, keep_dumps=keep_dumps,
            )
            if mode == "preview":
                best_by_id[cid] = summary["c_best"].get("best")
            rows.extend(new_rows)
            summaries.append({"capture_id": cid, "mode": mode, **summary})
            rois_out.setdefault(cid, {})[mode] = summary["rois"]
            for r in new_rows:
                if r.get("error"):
                    ctx.say(f"  ERROR {r['variant']}: {str(r['error'])[:300]}")
        except Exception as error:  # noqa: BLE001 — 1 カットの失敗で全体を止めない
            rows.append({"capture_id": cid, "mode": mode, "variant": "harness", "error": f"{type(error).__name__}: {error}"})
            ctx.say(f"  ERROR {type(error).__name__}: {error}")
        write_rows_csv(ctx.out / "diagnosis_metrics.csv", rows, ("capture_id", "mode", "variant"))
    errors += [{k: r.get(k) for k in ("capture_id", "mode", "variant", "error")} for r in rows if r.get("error")]
    specs = diagnosis_specs()
    summary_payload = {
        "out": str(ctx.out),
        "environment": environment or {},
        "captures": [{"capture_id": cid, **{k: features_by_id[cid].get(k) for k in (
            "iso", "exposure_time", "f_number", "focal_length", "lens_model", "orientation")}} for cid in features_by_id],
        "selection_count": len(selection), "full_ids": full_ids,
        "variants": {v: {k: s[k] for k in ("label", "lens", "profile", "overrides", "extra_settings", "output_format",
                                           "output_color_space", "adjustments", "diagnostics", "dump")} for v, s in specs.items()},
        "full_variants": list(DIAG_FULL_VARIANTS),
        "constants": {
            "mid_p50_tol": MID_P50_TOL, "grad_texture_ratio_min": GRAD_TEXTURE_RATIO_MIN, "grad_recover_min": GRAD_RECOVER_MIN,
            "fg_p01_drop_max": FG_P01_DROP_MAX, "magenta_rate_max": MAGENTA_RATE_MAX,
            "attr_min_clip_fraction": ATTR_MIN_CLIP_FRACTION, "attr_major": ATTR_MAJOR_FRACTION, "attr_second": ATTR_SECOND_FRACTION,
            "white16_linear": WHITE16_LINEAR, "white8_linear": WHITE8_LINEAR, "curve_check_tol": CURVE_CHECK_TOL,
            "jpeg_near_white_linear": JPEG_NEAR_WHITE_LINEAR, "cloud_grad_max_sat": CLOUD_GRAD_MAX_SAT,
            "roi_fraction": ROI_FRACTION, "roi_min_response": ROI_MIN_RESPONSE,
        },
        "notes": {
            "tone_curve": "§8.4 の曲線は定数置換で標本化した boost 1 の応答 24 点（4 カット・ISO 100〜2000 で同値、2026-10-21）を PCHIP で補間したもの",
            "stage_loss_s3s4": "S3（lens OFF）と S4（lens ON）は幾何が違うため、白の率の差（S4 − S3、符号付き）で代えた",
            "attribution_k": "K は S3 dump の ≥ WHITE8_LINEAR（8bit sRGB で 255 に丸まる値、lens OFF 幾何）。dump が無ければ d_lens_off の 8bit JPEG の白（k_source = jpeg8）",
            "attribution_rule3": "S3 の線形値が 8bit sRGB で 255 に丸まる（≥ WHITE8_LINEAR）なら 8bit 量子化 quant8（旧 gamut_output「色域・SDR 出力」）",
            "attribution_rule4": "それ以外のうち S3 ≥ JPEG_NEAR_WHITE_LINEAR なら JPEG 符号化 jpeg_encode、残りは検証不能 unverifiable（旧 unverifiable を 2 つに分けた）。K を S3 から取る経路では両方 0",
            "scene_class": "シーン判定は jpeg_encode + unverifiable を合算して旧 unverifiable として比べる（判定を名称変更で動かさない）",
            "cloud_grad": "帯 0.6 < L < 0.98 に HSV の S < CLOUD_GRAD_MAX_SAT を足す。満たす箱が無ければ従来の帯で選ぶ（diagnosis_summary.json の by_capture.*.roi_info.cloud_grad_low_sat）",
            "classification": "最大の帰属が 50% 未満のときは mixed",
            "fg_dark": "構造の閾値は箱の Laplacian 平均の中央値（相対）",
        },
        "elapsed_total_s": time.monotonic() - started,
        "errors": errors,
        "by_capture": summaries,
    }
    (ctx.out / "diagnosis_summary.json").write_text(
        json.dumps(_jsonable(summary_payload), ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    (ctx.out / "rois.json").write_text(json.dumps(rois_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ctx.out / "diagnosis_tables.md").write_text(diagnosis_tables_md(rows, summaries), encoding="utf-8")
    (ctx.out / "run.log").write_text("\n".join(ctx.log) + "\n", encoding="utf-8")
    ctx.say(f"[diagnosis] 完了: {ctx.out}（エラー {len(errors)} 件）")


# =============================================================================
# プリセット再調整の評価モード --presets（Issue #81、設計書 docs/design/coreimage-preset-retune.md §7・§8）
# =============================================================================

#: highlight_recovery を写像で効かせる CLI の最小版（設計書 §2.3。0.6.0 で ignored が空になる）。
PRESETS_MIN_CLI = (0, 6, 0)
#: Core Image 向けのプリセット表（正は Rust `src-tauri/src/presets.rs`。設計書 §6）。
CI_PRESETS_JSON = PROJECT_ROOT / "src-tauri" / "tests" / "fixtures" / "presets.json"
#: hr → (ΔEV, Δlift) の節点（CLI `HighlightRecoveryMapping.nodes` の写し。応答の実効値との突き合わせ用）。
HR_NODES: tuple[tuple[float, float, float], ...] = ((0.0, 0.0, 0.0), (60.0, -0.7, 40.0), (90.0, -1.0, 55.0), (100.0, -1.1, 60.0))
#: #92 の 10 カット（空が主役。--diagnosis と同じ ID）。§7.4 の 1〜5 はこの集合で数える。
PRESET_SKY_IDS = ("DSC04473", "DSC04607", "DSC04532", "DSC04705", "DSC04579",
                  "DSC04604", "DSC04754", "DSC04432", "DSC04746", "DSC04462")
#: シーン別の代表カット（§7.3）。夜景 3 カットは別フォルダ（--source-root を分けて 2 回回す）。
PRESET_SCENE_IDS = ("DSC04670", "DSC04651", "DSC04872", "DSC04430", "DSC04453", "DSC07415", "DSC07407", "DSC07402")
#: 原寸で回すカット（救済系 3 プリセット × 3 カット。§7.3）。
PRESET_FULL_IDS = ("DSC04473", "DSC04607", "DSC04705")
#: 救済系（hr > 0 を持つ）プリセット。§7.4 の 1〜5 の対象。
RESCUE_PRESETS = ("highlight_rescue", "backlit_rescue", "sunset_glow")
#: シートに並べる追加の候補（オーナー判断 2026-10-22・設計書 §8）: variant -> (元のプリセット, 上書き, ラベル)。
#: シートのラベルは ASCII だけにする（Pillow の既定フォントは日本語を描けない）。
PRESET_EXTRA_CANDIDATES: dict[str, tuple[str, dict[str, Any], str]] = {
    "neutral_recovery_hr30": ("neutral_recovery", {"highlight_recovery": 30.0}, "plan C: hr 30"),
    "landscape_clear_hr30": ("landscape_clear", {"highlight_recovery": 30.0}, "plan C: hr 30"),
    "landscape_clear_weak": ("landscape_clear", {"contrast": 1.06, "saturation": 1.05, "sharpness": 1.08}, "weak c1.06 s1.05 sh1.08"),
    "sunset_glow_hr70": ("sunset_glow", {"highlight_recovery": 70.0}, "hr 70"),
    "highlight_rescue_hr100": ("highlight_rescue", {"highlight_recovery": 100.0}, "hr 100 (-1.1EV, +60)"),
}
#: §7.3 の「宣言する差」（d_base 比）: プリセット -> [(指標, 符号)]。符号 0 は「≈ 0（絶対値が小さい）」。
PRESET_DIRECTIONS: dict[str, tuple[tuple[str, int], ...]] = {
    "sunset_glow": (("rb_ln_neutral", 1), ("mean_saturation", 1), ("clip8_any", -1)),
    "indoor_warm": (("mean_luma", 1), ("rb_ln_neutral", 1)),
    "vivid_travel": (("mean_saturation", 1), ("std_luma", 1)),
    "landscape_clear": (("mean_saturation", 1), ("std_luma", 1)),
    "monochrome_street": (("mean_saturation", 0),),
    "portrait_soft": (("std_luma", -1), ("edge_width_px", 1)),
    "nightscape": (("dark_mean_luma", 1),),
    "shadow_rescue": (("dark_mean_luma", 1),),
    "highlight_rescue": (("clip8_any", -1),),
    "backlit_rescue": (("clip8_any", -1),),
}
#: monochrome_street の「彩度 ≈ 0」の許容（mean_saturation、0..1）。
PRESET_MONO_SAT_MAX = 0.01
#: §7.4 の 1: P_hr0 の白飛びがこれ未満のカットは「hr の寄与」を n/a にする。
PRESET_CLIP_MIN = 0.01
# 条件 5 で中間調の幅（±MID_P50_TOL）を問わないプリセット。被写体を持ち上げる設計（設計書 §7.4・§11 #3）。
PRESET_MID_EXEMPT = frozenset({"backlit_rescue"})
#: §7.4 の 2・3・5 の割合（9/10・9/10・8/10）。
PRESET_MID_FRACTION = 0.9
PRESET_FG_FRACTION = 0.9
PRESET_DBASE_FRACTION = 0.8
#: §7.4 の 7: 実効 lift ≥ 40 のプリセット × ISO ≥ 1600 のカットで flat_noise(P) / flat_noise(d_base) ≤ 1.5（【推測】
#: ADJ_SHARP16_NOISE_MAX の流用）。
PRESET_NOISE_LIFT_MIN = 40.0
PRESET_NOISE_ISO_MIN = 1600
PRESET_NOISE_RATIO_MAX = ADJ_SHARP16_NOISE_MAX
#: §2.2 の等価性: (hr, exposure, shadow_lift)。{0, 0, hr} と {exposure, shadow_lift, 0} の tiff16 が一致すること。
PRESET_EQUIVALENCE = ((60.0, -0.7, 40.0), (90.0, -1.0, 55.0))
#: §8 のオーナーに見せる組: (組, [(ID, プリセット)], 見るポイント)。
PRESET_OWNER_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...], str], ...] = (
    ("救済系", tuple((cid, p) for cid in ("DSC04473", "DSC04607", "DSC04705") for p in ("highlight_rescue", "backlit_rescue")),
     "空の階調が戻るか、前景が沈まないか、境界の偽色（原寸シートも）"),
    ("夕景・薄明", (("DSC04532", "sunset_glow"),), "桃色の雲の階調と暖色の方向・強さ（hr 60 と 70）"),
    ("既定", tuple((cid, "neutral_recovery") for cid in ("DSC04705", "DSC04473", "DSC04670")),
     "案 B / C の選択（hr 0 と 30）。CI 既定とどれだけ違うか"),
    ("風景", tuple((cid, "landscape_clear") for cid in ("DSC04670", "DSC04872")),
     "コントラスト・彩度・シャープが強すぎないか（白い粒・硬さ）。弱め候補と比べる"),
    ("室内・高 ISO", tuple((cid, p) for cid in ("DSC04430", "DSC04432") for p in ("indoor_warm", "shadow_rescue", "nightscape")),
     "lift のノイズ、窓の白"),
    ("夜景", tuple((cid, p) for cid in ("DSC07415", "DSC07407") for p in ("nightscape", "shadow_rescue")),
     "星の残り、空のノイズ、NR full"),
)
PRESET_TABLE_KEYS = (
    "clip8_any", "sky_bright_clip8_any", "cloud_grad_clip8_any", "mid_p50", "fg_p01", "p01_luma", "p50_luma",
    "p99_luma", "mean_luma", "std_luma", "mean_saturation", "rb_ln_neutral", "dark_mean_luma", "flat_noise",
    "magenta_rate", "edge_width_px", "de76_vs_base", "ci_hr_effective_exposure", "ci_hr_effective_shadow_lift",
)


def load_ci_presets(path: Path = CI_PRESETS_JSON) -> list[dict[str, Any]]:
    """Core Image 向けのプリセット表（``presets.json`` の ``presets``。並びはそのまま）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    presets = data.get("presets")
    if not isinstance(presets, list) or not presets:
        raise ValueError(f"{path} に presets がありません")
    return presets


def ci_preset_adjustments(name: str, presets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """CI のプリセット 1 件の Adjustments（9 キー全量のコピー）。"""
    for preset in presets if presets is not None else load_ci_presets():
        if preset.get("name") == name:
            return dict(preset["adjustments"])
    raise KeyError(f"presets.json に無いプリセットです: {name}")


def hr_delta(hr: float) -> tuple[float, float]:
    """hr → (ΔEV, Δlift)（CLI と同じ区分線形。節点では表の値をそのまま返す）。"""
    if not hr > HR_NODES[0][0]:
        return HR_NODES[0][1], HR_NODES[0][2]
    for (h0, e0, l0), (h1, e1, l1) in zip(HR_NODES, HR_NODES[1:]):
        if hr == h1:
            return e1, l1
        if hr < h1:
            t = (hr - h0) / (h1 - h0)
            return e0 + (e1 - e0) * t, l0 + (l1 - l0) * t
    return HR_NODES[-1][1], HR_NODES[-1][2]


def effective_exposure_lift(adjustments: dict[str, Any]) -> tuple[float, float]:
    """プリセット値 + hr の寄与（加算、lift は [0, 100] にクランプ）。設計書 §3。"""
    d_ev, d_lift = hr_delta(float(adjustments.get("highlight_recovery", 0.0)))
    lift = min(100.0, max(0.0, float(adjustments.get("shadow_lift", 0.0)) + d_lift))
    return float(adjustments.get("exposure", 0.0)) + d_ev, lift


def preset_variant_specs(presets: list[dict[str, Any]], mode: str = "preview") -> dict[str, dict[str, Any]]:
    """--presets のバリアント（純粋）: variant -> {"preset", "adjustments"(None = d_base), "label", "kind"}。

    preview: d_base、全プリセット、hr > 0 のプリセットの ``<preset>_hr0``、``PRESET_EXTRA_CANDIDATES``。
    full: d_base と救済系 3 プリセット・その hr0 だけ（§7.3）。
    """
    specs: dict[str, dict[str, Any]] = {"d_base": {"preset": None, "adjustments": None, "label": "d_base (CI default)", "kind": "base"}}
    for preset in presets:
        name = preset["name"]
        if mode == "full" and name not in RESCUE_PRESETS:
            continue
        adj = dict(preset["adjustments"])
        ev, lift = effective_exposure_lift(adj)
        specs[name] = {"preset": name, "adjustments": adj, "label": f"{name} (eff {ev:+.2f}EV, lift {lift:g})", "kind": "preset"}
        if float(adj.get("highlight_recovery", 0.0)) > 0:
            specs[f"{name}_hr0"] = {"preset": name, "adjustments": {**adj, "highlight_recovery": 0.0},
                                    "label": f"{name} hr 0", "kind": "hr0"}
    if mode == "preview":
        names = {p["name"] for p in presets}
        for variant, (base, override, label) in PRESET_EXTRA_CANDIDATES.items():
            if base not in names:
                continue
            adj = {**ci_preset_adjustments(base, presets), **override}
            specs[variant] = {"preset": base, "adjustments": adj, "label": f"{base} {label}", "kind": "candidate"}
    return specs


def equivalence_requests() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """§2.2 の等価性の組: (名前, hr 側の Adjustments, 直接指定側の Adjustments)。"""
    out = []
    for hr, ev, lift in PRESET_EQUIVALENCE:
        out.append((f"hr{int(hr)}", {**ADJ_IDENTITY, "highlight_recovery": hr},
                    {**ADJ_IDENTITY, "exposure": ev, "shadow_lift": lift}))
    return out


def presets_cli_problem(version: Any, capabilities: Any) -> str | None:
    """--presets を回せない理由（回せれば None）。CLI 0.6.0 以上で adjustments.ignored が空であること。"""
    problem = adjustments_cli_problem(version, capabilities)
    if problem:
        return problem
    parsed = parse_cli_version(version.get("cli_version"))
    need = ".".join(str(v) for v in PRESETS_MIN_CLI)
    if parsed is None or parsed < PRESETS_MIN_CLI:
        return f"Core Image CLI {version.get('cli_version')} は highlight_recovery の写像に未対応です（{need} 以上が必要）"
    ignored = (capabilities.get("adjustments") or {}).get("ignored")
    if ignored:
        return f"--capabilities の adjustments.ignored が空ではありません: {ignored}"
    return None


def highlight_recovery_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """CLI 0.6.0 応答の ``adjustments_applied.highlight_recovery`` を平らな列にする（純粋）。"""
    adj = payload.get("adjustments_applied")
    hr = adj.get("highlight_recovery") if isinstance(adj, dict) else None
    if not isinstance(hr, dict):
        return {}
    return {
        "ci_hr_value": hr.get("value"),
        "ci_hr_exposure_delta": hr.get("exposure_delta"),
        "ci_hr_shadow_lift_delta": hr.get("shadow_lift_delta"),
        "ci_hr_effective_exposure": hr.get("effective_exposure"),
        "ci_hr_effective_shadow_lift": hr.get("effective_shadow_lift"),
        "ci_hr_shadow_lift_clamped": hr.get("shadow_lift_clamped"),
    }


def _roi_value(row: dict[str, Any], roi_key: str, fallback_key: str) -> tuple[float, str]:
    """ROI の値が取れなければ全画面の値（§7.4 の 2・3）。どちらを使ったかも返す。"""
    value = _num(row.get(roi_key))
    if math.isfinite(value):
        return value, "roi"
    return _num(row.get(fallback_key)), "full_frame"


def preset_capture_checks(
    capture_id: str,
    mode: str,
    metrics: dict[str, dict[str, Any]],
    iso: Any,
    presets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """1 カット分の §7.4 の自動判定（1〜7）。集計（9/10 など）は ``preset_check_summary``（純粋）。"""
    checks: list[dict[str, Any]] = []
    base = metrics.get("d_base", {})
    sky = capture_id in PRESET_SKY_IDS

    def add(cond: int, name: str, preset: str, value: Any, threshold: str, passed: bool | None, **extra: Any) -> None:
        checks.append({"capture_id": capture_id, "mode": mode, "condition": cond, "check": name, "preset": preset,
                       "sky_cut": sky, "value": value, "threshold": threshold, "pass": passed, **extra})

    for name in RESCUE_PRESETS:
        p, p0 = metrics.get(name), metrics.get(f"{name}_hr0")
        if not p or not p0:
            continue
        c, c0 = _num(p.get("clip8_any")), _num(p0.get("clip8_any"))
        applicable = math.isfinite(c0) and c0 >= PRESET_CLIP_MIN
        add(1, "hr_clip8_lt_hr0", name, c - c0 if math.isfinite(c) and math.isfinite(c0) else float("nan"),
            f"clip8(P) < clip8(P_hr0)（P_hr0 ≥ {PRESET_CLIP_MIN} のとき）", (c < c0) if applicable and math.isfinite(c) else None,
            clip8=c, clip8_hr0=c0)
        m, src = _roi_value(p, "mid_p50", "p50_luma")
        m0, _ = _roi_value(p0, "mid_p50", "p50_luma")
        diff = m - m0
        add(2, "hr_mid_p50_vs_hr0", name, diff, f"|Δ| ≤ {MID_P50_TOL}", abs(diff) <= MID_P50_TOL if math.isfinite(diff) else None,
            source=src)
        f, fsrc = _roi_value(p, "fg_p01", "p01_luma")
        f0, _ = _roi_value(p0, "fg_p01", "p01_luma")
        fd = f - f0
        add(3, "hr_fg_p01_vs_hr0", name, fd, f"≥ {FG_P01_DROP_MAX}", fd >= FG_P01_DROP_MAX if math.isfinite(fd) else None,
            source=fsrc)
        mg = _num(p.get("magenta_rate"))
        add(4, "magenta_rate", name, mg, f"≤ {MAGENTA_RATE_MAX}（preview。原寸は記録のみ）",
            (mg <= MAGENTA_RATE_MAX) if (mode == "preview" and math.isfinite(mg)) else None)
        cb = _num(base.get("clip8_any"))
        mb, _ = _roi_value(base, "mid_p50", "p50_luma")
        md = m - mb
        # backlit_rescue は被写体向けの lift 30 で中間調を上げる設計（オーナー判断 #3）なので中間調の幅は問わない。
        # d_base の clip8 が PRESET_CLIP_MIN 未満のカットは救う白飛びが無いので n/a（条件 1 と同じ）。
        mid_free = name in PRESET_MID_EXEMPT
        if not all(math.isfinite(v) for v in (c, cb, md)) or cb < PRESET_CLIP_MIN:
            ok = None
        else:
            ok = c < cb and (mid_free or abs(md) <= MID_P50_TOL)
        rule = "clip8 < d_base（中間調は問わない）" if mid_free else f"clip8 < d_base かつ |mid_p50 − d_base| ≤ {MID_P50_TOL}"
        add(5, "dbase_clip8_and_mid", name, md, f"{rule}（d_base ≥ {PRESET_CLIP_MIN} のとき）", ok,
            clip8=c, clip8_base=cb)
    # 6: 個性の方向（d_base 比）
    for name, decl in PRESET_DIRECTIONS.items():
        p = metrics.get(name)
        if not p:
            continue
        for key, sign in decl:
            v, vb = _num(p.get(key)), _num(base.get(key))
            if sign == 0:
                add(6, f"direction_{key}_zero", name, v, f"≤ {PRESET_MONO_SAT_MAX}", v <= PRESET_MONO_SAT_MAX if math.isfinite(v) else None)
                continue
            delta = v - vb
            if key == "clip8_any" and not (math.isfinite(vb) and vb >= PRESET_CLIP_MIN):
                # d_base がほぼ白飛びしていないカットは減らす余地が無いので n/a（条件 1・5 と同じ）。
                add(6, f"direction_{key}", name, delta, f"−（d_base ≥ {PRESET_CLIP_MIN} のとき）", None)
                continue
            add(6, f"direction_{key}", name, delta, "+" if sign > 0 else "−",
                direction_match(float(sign), delta) if math.isfinite(delta) else None)
    # 7: ノイズ（実効 lift ≥ 40 × ISO ≥ 1600）
    iso_v = _num(iso)
    for preset in presets:
        name = preset["name"]
        p = metrics.get(name)
        if not p:
            continue
        _, lift = effective_exposure_lift(preset["adjustments"])
        if lift < PRESET_NOISE_LIFT_MIN:
            continue
        ratio = _num(p.get("flat_noise")) / _num(base.get("flat_noise")) if _num(base.get("flat_noise")) > 0 else float("nan")
        applicable = math.isfinite(iso_v) and iso_v >= PRESET_NOISE_ISO_MIN
        add(7, "noise_vs_dbase", name, ratio, f"≤ {PRESET_NOISE_RATIO_MAX}（ISO ≥ {PRESET_NOISE_ISO_MIN}）",
            (ratio <= PRESET_NOISE_RATIO_MAX) if applicable and math.isfinite(ratio) else None, effective_lift=lift, iso=iso)
    return checks


def preset_check_summary(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """§7.4 の合否（純粋）。1〜5 は空の 10 カット（preview）で数え、2・3 は 9/10、5 は 8/10、1・4 は該当すべて。
    6・7 は全カットの該当すべて。サンプルが 0 件の条件は pass=None（未実施）。"""
    rules = {1: ("all", True), 2: (PRESET_MID_FRACTION, True), 3: (PRESET_FG_FRACTION, True), 4: ("all", True),
             5: (PRESET_DBASE_FRACTION, True), 6: ("all", False), 7: ("all", False)}
    out: list[dict[str, Any]] = []
    keys = sorted({(c["condition"], c["preset"]) for c in checks})
    for cond, preset in keys:
        need, sky_only = rules[cond]
        rows = [c for c in checks if c["condition"] == cond and c["preset"] == preset and c["mode"] == "preview"
                and (c["sky_cut"] or not sky_only)]
        decided = [c for c in rows if c["pass"] is not None]
        passed = sum(1 for c in decided if c["pass"])
        if not decided:
            verdict = None
        elif need == "all":
            verdict = passed == len(decided)
        else:
            verdict = passed >= math.ceil(float(need) * len(decided) - 1e-9)
        out.append({"condition": cond, "preset": preset, "rule": "all" if need == "all" else f"≥ {need:.0%}",
                    "scope": "sky 10" if sky_only else "all", "pass_n": passed, "decided_n": len(decided),
                    "n_a": len(rows) - len(decided), "pass": verdict,
                    "failed": sorted({f"{c['capture_id']}:{c['check']}" for c in decided if not c["pass"]})})
    return out


def equivalence_compare(a01: np.ndarray, b01: np.ndarray, applied_a: dict[str, Any], applied_b: dict[str, Any]) -> dict[str, Any]:
    """§2.2: 2 枚の tiff16 と applied の一致（純粋）。"""
    same_shape = a01.shape == b01.shape
    diff = float(np.abs(a01 - b01).max()) if same_shape else float("nan")
    applied_equal = json.dumps(applied_a, sort_keys=True) == json.dumps(applied_b, sort_keys=True)
    return {"max_abs_diff": diff, "applied_equal": applied_equal, "pass": bool(same_shape and diff == 0.0 and applied_equal)}


def preset_variant_metrics(
    rgb8: np.ndarray,
    camera: dict[str, Any],
    align: dict[str, dict[str, Any]],
    base8: np.ndarray | None,
) -> dict[str, Any]:
    """1 バリアントの指標（§7.3）。camera はカメラ JPEG 由来の基準（マスク・ROI・エッジ）。"""
    rgb01 = rgb8.astype(np.float32) / 255.0
    row: dict[str, Any] = {}
    full = image_metrics(rgb8)
    row.update({k: full[k] for k in ("p01_luma", "p50_luma", "p99_luma", "clip_high_any", "flat_noise", "mean_saturation")})
    row.update(image_variant_metrics(rgb01, rgb8 >= 255, 255, align, camera["ref"]))
    row.update(adjustment_image_metrics(rgb8, camera["neutral"], camera["dark"]))
    row["flat_noise"] = full["flat_noise"]
    gray = cv2.cvtColor(rgb8, cv2.COLOR_RGB2GRAY)
    row.update(edge_width_vs_camera(camera["edge"], camera["gray"], gray))
    if base8 is not None and base8.shape == rgb8.shape:
        row["de76_vs_base"] = delta_e76(rgb8, base8)
    return row


def presets_sheet_columns(preset: str, available: Iterable[str]) -> list[str]:
    """シートの列（§8）: d_base | 新プリセット | 同 hr 0 | 追加候補 | 旧 rawpy（純粋）。"""
    have = set(available)
    cols = ["d_base", preset, f"{preset}_hr0"]
    cols += [v for v, (base, _o, _l) in PRESET_EXTRA_CANDIDATES.items() if base == preset]
    cols.append(f"rawpy_{preset}")
    return [c for c in cols if c in have]


def presets_sheets(
    ctx: RunContext,
    capture_id: str,
    mode: str,
    images: dict[str, np.ndarray],
    rois: dict[str, tuple[int, int, int, int] | None],
    align: dict[str, dict[str, Any]],
    metrics: dict[str, dict[str, Any]],
    labels: dict[str, str],
) -> list[str]:
    """プリセットごとのシート ``sheets/<ID>_<preset>[_full].jpg``。EXIF・位置情報は付けない。書いたパスを返す。"""
    written: list[str] = []
    base = images.get("d_base")
    if base is None:
        return written
    th, tw = base.shape[:2]
    overview = fit_size(tw, th, 400)
    gray = cv2.cvtColor(base, cv2.COLOR_RGB2GRAY)
    detail = detail_box(gray, max(64, int(round(ROI_FRACTION * max(tw, th)))))
    crop_box = (align.get("cloud_grad") or {}).get("box") or (align.get("midtone") or {}).get("box") or detail
    crop_name = "cloud_grad" if (align.get("cloud_grad") or {}).get("box") else ("midtone" if (align.get("midtone") or {}).get("box") else "detail")
    fg_box = (align.get("fg_dark") or {}).get("box")
    presets = [v for v in metrics if v not in ("d_base",) and not v.startswith("rawpy_")
               and not v.endswith("_hr0") and v not in PRESET_EXTRA_CANDIDATES]
    suffix = "" if mode == "preview" else "_full"
    for preset in presets:
        columns = presets_sheet_columns(preset, images)
        if preset not in columns:
            continue
        rows = []
        panels = []
        for v in columns:
            m = metrics.get(v, {})
            note = " *other base" if v.startswith("rawpy_") else ""
            text = (f"{labels.get(v, v)}{note}\nclip {_num(m.get('clip8_any')) * 100:.2f}%  mid {_num(m.get('mid_p50')):.3f}  "
                    f"fg_p1 {_num(m.get('fg_p01')):.3f}")
            panels.append((text, resize_to(images[v], overview)))
        rows.append(labeled_row(panels, f"{capture_id} {mode} {preset}"))
        for roi_name, box in ((crop_name, crop_box), ("fg_dark", fg_box)):
            if box is None:
                continue
            crops = []
            for v in columns:
                img = images[v]
                vbox = box
                if v.startswith("rawpy_"):
                    # rawpy は幾何が違う（レンズ補正の実装差）ので、d_base → rawpy の平行移動で箱をずらす。
                    key = roi_name if roi_name in DIAG_ROI_NAMES else "midtone"
                    a = align_rois(cv2.cvtColor(base, cv2.COLOR_RGB2GRAY), cv2.cvtColor(img, cv2.COLOR_RGB2GRAY),
                                   {key: box}, box[2] - box[0])
                    vbox = (a.get(key) or {}).get("box") or box
                crops.append((v, _crop(img, vbox)))
            rows.append(labeled_row(crops, f"{roi_name} 1:1"))
        width = max(r.shape[1] for r in rows)
        padded = [np.pad(r, ((0, 8), (0, width - r.shape[1]), (0, 0)), constant_values=24) for r in rows]
        path = ctx.out / "sheets" / f"{capture_id}_{preset}{suffix}.jpg"
        save_plain_jpeg(np.concatenate(padded, axis=0), path, quality=92)
        written.append(str(path))
    return written


def presets_capture(
    ctx: RunContext,
    capture_id: str,
    mode: str,
    features: dict[str, Any],
    presets: list[dict[str, Any]],
    *,
    with_rawpy: bool = True,
    workers: int = 4,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """1 カット・1 mode: 現像 → 指標 → §7.4 の判定 → シート。(metric_rows, check_rows, sheet_paths)。"""
    from concurrent.futures import ThreadPoolExecutor

    specs = preset_variant_specs(presets, mode)
    ci_kw = {"decoder_version": "9", "profile": "apple_default"}
    engine_rows: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {v: pool.submit(run_coreimage, ctx, capture_id, mode, True, variant=f"ps_{v}", adjustments=s["adjustments"], **ci_kw)
                for v, s in specs.items()}
        if with_rawpy and ctx.legacy_rawpy and mode == "preview":
            for preset in presets:
                jobs[f"rawpy_{preset['name']}"] = pool.submit(run_rawpy, ctx, capture_id, mode, preset=preset["name"])
        engine_rows = {v: f.result() for v, f in jobs.items()}
    labels = {v: s["label"] for v, s in specs.items()}
    labels.update({f"rawpy_{p['name']}": f"rawpy {p['name']} legacy" for p in presets})
    camera_full = read_rgb_upright(source_file(ctx.source_root, capture_id, "JPG"))
    ch, cw = camera_full.shape[:2]
    target = fit_size(cw, ch, ctx.max_edge) if mode == "preview" else (cw, ch)
    cam8 = resize_to(camera_full, target)
    images: dict[str, np.ndarray] = {}
    for v, row in engine_rows.items():
        if row.get("error"):
            continue
        try:
            rgb8, row["fit"] = fit_to_target(read_rgb_upright(Path(row["output"])), target)
            images[v] = rgb8
        except Exception as error:  # noqa: BLE001 — 1 バリアントの読み込み失敗は欠測にする
            row["error"] = f"load failed: {type(error).__name__}: {error}"
    roi_size = max(8, int(round(ROI_FRACTION * max(target))))
    rois, _info = diagnosis_rois(cam8, roi_size)
    cam_gray = cv2.cvtColor(cam8, cv2.COLOR_RGB2GRAY)
    camera = {"ref": camera_reference(cam8, rois), "neutral": neutral_tile_mask(cam8), "dark": dark_pixel_mask(cam8),
              "edge": edge_reference(cam_gray), "gray": cam_gray}
    base8 = images.get("d_base")
    # CI のバリアントは d_base と同じ幾何（lens ON・同じ scale）なので位置合わせは 1 回だけ取る。rawpy は別に取る。
    ci_align = align_rois(cam_gray, cv2.cvtColor(base8, cv2.COLOR_RGB2GRAY), rois, roi_size) if base8 is not None else identity_alignment(rois)
    metrics: dict[str, dict[str, Any]] = {}
    for v, rgb8 in images.items():
        align = ci_align if not v.startswith("rawpy_") else align_rois(cam_gray, cv2.cvtColor(rgb8, cv2.COLOR_RGB2GRAY), rois, roi_size)
        metrics[v] = preset_variant_metrics(rgb8, camera, align, base8 if not v.startswith("rawpy_") else None)
    checks = preset_capture_checks(capture_id, mode, metrics, features.get("iso"), presets)
    sheets = presets_sheets(ctx, capture_id, mode, images, rois, ci_align, metrics, labels)
    base_info = {"capture_id": capture_id, "mode": mode, "iso": features.get("iso"), "sky_cut": capture_id in PRESET_SKY_IDS,
                 "width": target[0], "height": target[1]}
    rows = []
    for v in [*specs, *(k for k in engine_rows if k.startswith("rawpy_"))]:
        if v not in engine_rows:
            continue
        spec = specs.get(v, {})
        adj = spec.get("adjustments") or {}
        row = {**base_info, "variant": v, "preset": spec.get("preset") or (v[len("rawpy_"):] if v.startswith("rawpy_") else None),
               "kind": spec.get("kind") or "rawpy_legacy", "engine": "rawpy" if v.startswith("rawpy_") else "coreimage",
               **{f"adj_{k}": adj.get(k) for k in ("exposure", "shadow_lift", "highlight_recovery") if adj},
               **engine_rows[v], **metrics.get(v, {})}
        if v in engine_rows and v not in metrics and not row.get("error"):
            row["error"] = "no image"
        rows.append(row)
    return rows, checks, sheets


def presets_equivalence(ctx: RunContext, capture_id: str) -> list[dict[str, Any]]:
    """§7.4 の 9（§2.2）: 1 カットで {0, 0, hr} と {ev, lift, 0} の tiff16 sRGB と applied が一致するか。"""
    ci_kw = {"decoder_version": "9", "profile": "apple_default", "output_format": "tiff16", "output_color_space": "srgb"}
    rows = []
    for name, via_hr, direct in equivalence_requests():
        a = run_coreimage(ctx, capture_id, "preview", True, variant=f"eq_{name}_hr", adjustments=via_hr, **ci_kw)
        b = run_coreimage(ctx, capture_id, "preview", True, variant=f"eq_{name}_direct", adjustments=direct, **ci_kw)
        row: dict[str, Any] = {"capture_id": capture_id, "pair": name, "via_hr": via_hr, "direct": direct}
        if a.get("error") or b.get("error"):
            row.update({"error": a.get("error") or b.get("error"), "pass": False})
        else:
            pa, pb = _read_response(a), _read_response(b)
            row.update(equivalence_compare(read_tiff16_rgb(Path(a["output"])), read_tiff16_rgb(Path(b["output"])),
                                           pa.get("applied") or {}, pb.get("applied") or {}))
            row["post_shadow_lift"] = [(pa.get("adjustments_applied") or {}).get("post", {}).get("shadow_lift"),
                                       (pb.get("adjustments_applied") or {}).get("post", {}).get("shadow_lift")]
        rows.append(row)
    return rows


def presets_index_md(out: Path, sheets: list[str], check_summary_rows: list[dict[str, Any]], equivalence: list[dict[str, Any]]) -> str:
    """index.md: オーナーに見せる組（§8）とシートのパス、自動判定の要約。"""
    have = {Path(p).name: p for p in sheets}
    lines = ["# --presets（#81）見るシート", "", f"出力: `{out}`", "",
             "列: d_base（CI 既定）| 新プリセット（CI）| 同 hr 0（hr > 0 のとき）| 追加候補 | 旧 rawpy プリセット（参考）。"
             "`*other base` は rawpy の明るさの基準が違う（auto_bright、カメラ比 −0.13）ことを示す。"
             "行: 全景（長辺 400px）/ cloud_grad・midtone・detail のいずれかの等倍 / fg_dark の等倍。", "",
             "| 組 | シート | 見るポイント |", "|---|---|---|"]
    for group, pairs, point in PRESET_OWNER_GROUPS:
        paths = []
        for cid, preset in pairs:
            for name in (f"{cid}_{preset}.jpg", f"{cid}_{preset}_full.jpg"):
                if name in have:
                    paths.append(f"`sheets/{name}`")
        lines.append(f"| {group} | {'<br>'.join(paths) if paths else '（該当カットなし）'} | {point} |")
    lines += ["", "## §7.4 の自動判定", "", "| # | preset | 範囲 | 規則 | 合格 / 判定 | n/a | 結果 | 不合格 |", "|---|---|---|---|---|---|---|---|"]
    for c in check_summary_rows:
        verdict = "—（未判定）" if c["pass"] is None else ("合格" if c["pass"] else "不合格")
        lines.append(f"| {c['condition']} | {c['preset']} | {c['scope']} | {c['rule']} | {c['pass_n']} / {c['decided_n']} | {c['n_a']} | {verdict} | {', '.join(c['failed'])} |")
    lines += ["", "## §2.2 の等価性（条件 9）", "", "| カット | 組 | max abs diff | applied 一致 | 結果 |", "|---|---|---|---|---|"]
    for e in equivalence:
        lines.append(f"| {e['capture_id']} | {e['pair']} | {e.get('max_abs_diff')} | {e.get('applied_equal')} | {'合格' if e.get('pass') else '不合格'} |")
    return "\n".join(lines) + "\n"


def presets_plan(ctx: RunContext, capture_ids: list[str], full_ids: list[str]) -> list[dict[str, Any]]:
    """--presets で投げる CI リクエストの一覧（純粋。CLI は呼ばない）。--dry-run で書き出して契約を検査する。"""
    presets = load_ci_presets()
    plan: list[dict[str, Any]] = []
    jobs = [(cid, "preview") for cid in capture_ids] + [(cid, "full") for cid in full_ids]
    for cid, mode in jobs:
        arw = source_file(ctx.source_root, cid, "ARW")
        for variant, spec in preset_variant_specs(presets, mode).items():
            out = coreimage_output_path(ctx, cid, mode, True, variant=f"ps_{variant}")
            request = coreimage_request(arw, out, mode, True, decoder_version="9", profile="apple_default",
                                        max_edge=ctx.max_edge, adjustments=spec["adjustments"])
            plan.append({"capture_id": cid, "mode": mode, "variant": variant, "request": request,
                         "problems": validate_adjustments_request(request) if spec["adjustments"] is not None else []})
    return plan


def presets_dry_run(ctx: RunContext, capture_ids: list[str], full_ids: list[str]) -> None:
    plan = presets_plan(ctx, capture_ids, full_ids)
    (ctx.out / "presets_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    problems = [p for p in plan if p["problems"]]
    ctx.say(f"[presets] dry-run: {len(plan)} リクエスト、契約違反 {len(problems)} 件 → {ctx.out / 'presets_plan.json'}")


def run_presets(
    ctx: RunContext,
    selection: list[dict[str, Any]],
    features_by_id: dict[str, dict[str, Any]],
    full_ids: list[str],
    *,
    with_rawpy: bool = True,
    environment: dict[str, Any] | None = None,
) -> None:
    """--presets 本体（設計書 §7.3〜§8）: preview を全カット、full を指定カットで回し、判定とシートを出す。"""
    presets = load_ci_presets()
    started = time.monotonic()
    rows: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    sheets: list[str] = []
    jobs = [(r["capture_id"], "preview") for r in selection] + [(cid, "full") for cid in full_ids]
    for index, (cid, mode) in enumerate(jobs, 1):
        ctx.say(f"[presets] ({index}/{len(jobs)}) {cid} {mode}")
        try:
            r, c, s = presets_capture(ctx, cid, mode, features_by_id[cid], presets, with_rawpy=with_rawpy)
            rows += r
            checks += c
            sheets += s
            for row in r:
                if row.get("error"):
                    ctx.say(f"  ERROR {row['variant']}: {str(row['error'])[:300]}")
        except Exception as error:  # noqa: BLE001 — 1 カットの失敗で全体を止めない
            rows.append({"capture_id": cid, "mode": mode, "variant": "harness", "error": f"{type(error).__name__}: {error}"})
            ctx.say(f"  ERROR {type(error).__name__}: {error}")
        write_rows_csv(ctx.out / "metrics.csv", rows, ("capture_id", "mode", "variant"))
        if checks:
            write_rows_csv(ctx.out / "checks.csv", checks, ("capture_id", "mode", "condition", "preset", "check", "pass"))
    equivalence = presets_equivalence(ctx, selection[0]["capture_id"]) if selection else []
    summary_checks = preset_check_summary(checks)
    errors = [{k: r.get(k) for k in ("capture_id", "mode", "variant", "error")} for r in rows if r.get("error")]
    errors += [{"capture_id": e["capture_id"], "variant": f"eq_{e['pair']}", "error": e["error"]} for e in equivalence if e.get("error")]
    by_variant = summarize_sharpness([r for r in rows if r.get("mode") == "preview"], PRESET_TABLE_KEYS, ("variant",))
    by_variant_sky = summarize_sharpness([r for r in rows if r.get("mode") == "preview" and r.get("sky_cut")], PRESET_TABLE_KEYS, ("variant",))
    payload = {
        "out": str(ctx.out),
        "environment": environment or {},
        "presets_json": str(CI_PRESETS_JSON.relative_to(PROJECT_ROOT)),
        "presets": presets,
        "hr_nodes": [list(n) for n in HR_NODES],
        "variants": {v: {k: s[k] for k in ("preset", "adjustments", "label", "kind")} for v, s in preset_variant_specs(presets).items()},
        "captures": [{"capture_id": cid, "sky_cut": cid in PRESET_SKY_IDS, **{k: features_by_id[cid].get(k) for k in (
            "iso", "exposure_time", "f_number", "focal_length", "lens_model", "orientation")}} for cid in features_by_id],
        "full_ids": full_ids,
        "thresholds": {
            "mid_p50_tol": MID_P50_TOL, "fg_p01_drop_max": FG_P01_DROP_MAX, "magenta_rate_max": MAGENTA_RATE_MAX,
            "clip_min": PRESET_CLIP_MIN, "mid_fraction": PRESET_MID_FRACTION, "fg_fraction": PRESET_FG_FRACTION,
            "dbase_fraction": PRESET_DBASE_FRACTION, "noise_lift_min": PRESET_NOISE_LIFT_MIN,
            "noise_iso_min": PRESET_NOISE_ISO_MIN, "noise_ratio_max": PRESET_NOISE_RATIO_MAX, "mono_sat_max": PRESET_MONO_SAT_MAX,
        },
        "directions": {k: [list(x) for x in v] for k, v in PRESET_DIRECTIONS.items()},
        "elapsed_total_s": time.monotonic() - started,
        "errors": errors,
        "check_summary": summary_checks,
        "equivalence": equivalence,
        "sheets": sheets,
    }
    (ctx.out / "summary.json").write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    md = "## preview（全カット、バリアント別の中央値）\n\n" + table_md(by_variant, ("variant",), PRESET_TABLE_KEYS, digits=4)
    md += "\n## preview（空の 10 カット）\n\n" + table_md(by_variant_sky, ("variant",), PRESET_TABLE_KEYS, digits=4)
    (ctx.out / "tables.md").write_text(md, encoding="utf-8")
    (ctx.out / "index.md").write_text(presets_index_md(ctx.out, sheets, summary_checks, equivalence), encoding="utf-8")
    (ctx.out / "run.log").write_text("\n".join(ctx.log) + "\n", encoding="utf-8")
    n_fail = sum(1 for c in summary_checks if c["pass"] is False)
    ctx.say(f"[presets] 完了: {ctx.out}（エラー {len(errors)} 件、不合格の条件 {n_fail} 件、シート {len(sheets)} 枚）")



# =============================================================================
# 凍結基準との比較 --frozen-manifest（#57 / #90 / #117。OS 更新時の回帰確認）
# =============================================================================
#
# 凍結画像（#107 の rawpy 基準 4 枚や、前回の Core Image 出力を ``--frozen-capture`` で凍結したもの）を
# ハッシュ・寸法で照合してから、同じ撮影 ID を現在の Core Image CLI で描画し、凍結画像と同寸で比べる。
# 照合に失敗した凍結物（欠落・改変・寸法違い）は成功扱いにしない（終了コード 1）。差の合否閾値は持たない
# （旧 rawpy の値を CI の受け入れ基準にしない。判断材料として数値とシートを残す）。

#: 凍結マニフェストに request が無い行（#107 の rawpy 基準）で使う CI のリクエスト。
FROZEN_DEFAULT_REQUEST: dict[str, Any] = {"profile": "apple_default", "lens": True, "decoder_version": CI_DECODER_VERSION}


def reference_diff(rgb: np.ndarray, reference: np.ndarray, prefix: str) -> dict[str, float]:
    """同寸 8bit RGB 2 枚の差（ΔE76 平均・RGB MAE・平均輝度差・完全一致画素率）。"""
    if reference.shape != rgb.shape:
        raise ValueError("reference は同寸である必要があります")
    a01, b01 = rgb.astype(np.float32) / 255.0, reference.astype(np.float32) / 255.0
    return {
        f"{prefix}_delta_e76": delta_e76(rgb, reference),
        f"{prefix}_rgb_mae": float(np.abs(a01 - b01).mean()),
        f"{prefix}_luma_diff": float(luma_of(a01).mean() - luma_of(b01).mean()),
        f"{prefix}_identical_fraction": float(np.all(rgb == reference, axis=2).mean()),
    }


def frozen_request(entry: dict[str, Any], preset: str) -> dict[str, Any]:
    """凍結行から CI リクエストの要素を決める（行の ``request`` を優先、無ければ既定 + CI プリセット値）。"""
    request = dict(FROZEN_DEFAULT_REQUEST)
    recorded = entry.get("request")
    if isinstance(recorded, dict):
        request.update({k: recorded[k] for k in ("profile", "lens", "decoder_version", "adjustments") if k in recorded})
    if "adjustments" not in request:
        request["adjustments"] = ci_preset_adjustments(str(entry.get("preset") or preset))
    return request


def frozen_path_conflicts(manifest: Path, root: Path, out: Path, capture: Path | None) -> list[str]:
    """凍結入力（凍結ルート・マニフェスト）へ書き込みうる出力先の組み合わせを列挙する（パスだけで判定）。

    照合より前に呼ぶ（照合レポート ``frozen_verification.json`` の書き込みも ``--out`` へ行うため）。
    マニフェストが壊れていても判定できるよう、マニフェストの中身は読まない。
    """
    manifest, root, out = manifest.resolve(), root.resolve(), out.resolve()
    problems = []
    if out == root:
        problems.append(f"--out が --frozen-root と同じです: {out}")
    elif out.is_relative_to(root):
        problems.append(f"--out が --frozen-root の中にあります: {out}")
    elif root.is_relative_to(out):
        problems.append(f"--out が --frozen-root を含みます: {out}")
    if manifest.is_relative_to(out):
        problems.append(f"--out が --frozen-manifest を含みます: {manifest}")
    if capture is not None:
        capture = capture.resolve()
        if capture == manifest:
            problems.append(f"--frozen-capture が --frozen-manifest と同じファイルです: {capture}")
        if capture.is_relative_to(root):
            problems.append(f"--frozen-capture が --frozen-root の中にあります: {capture}")
    return problems


def verify_frozen(manifest: Path, root: Path, out: Path) -> list[frozen.FrozenImage]:
    """凍結物を照合し ``frozen_verification.json`` を書く。失敗なら書いてから SystemExit(1)。

    呼ぶ前に ``frozen_path_conflicts`` で ``out`` が凍結入力と重ならないことを確かめておくこと。
    """
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"manifest": str(manifest), "root": str(root)}
    try:
        images = frozen.verify_manifest(manifest, root)
    except frozen.FrozenArtifactError as error:
        report.update({"ok": False, "error": str(error)})
        (out / "frozen_verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    report.update({"ok": True, "verified": [
        {"capture_id": i.capture_id, "mode": i.mode, "path": i.entry["path"], "dimensions": list(i.dimensions)}
        for i in images]})
    (out / "frozen_verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return images


def run_frozen(ctx: RunContext, images: list[frozen.FrozenImage], *, preset: str, capture: Path | None) -> int:
    """照合済みの凍結画像ごとに現在の CI で描画し、凍結画像と同寸で比べる。描画失敗が 1 件でもあれば 1 を返す。

    出力のバリアント名・シート名はマニフェストの行番号で一意にする（同じ撮影 ID・mode の行が request 違いで
    複数あっても、出力が上書きされて凍結マニフェストのハッシュが食い違わないように）。
    """
    rows: list[dict[str, Any]] = []
    captured: list[dict[str, Any]] = []
    for index, image in enumerate(images, 1):
        cid, mode = validate_capture_id(image.capture_id), image.mode
        if mode not in ("preview", "full"):
            raise SystemExit(f"凍結行の mode は preview / full のどちらかです: {cid} {mode}")
        ctx.say(f"[frozen] ({index}/{len(images)}) {cid} {mode} {image.dimensions[0]}x{image.dimensions[1]}")
        request = frozen_request(image.entry, preset)
        variant = f"frozen{index:02d}"
        base = {"capture_id": cid, "mode": mode, "frozen_index": index, "frozen_variant": variant,
                "frozen_path": image.entry["path"],
                "frozen_width": image.dimensions[0], "frozen_height": image.dimensions[1],
                "ci_profile": request["profile"], "ci_lens": request["lens"], "ci_decoder_version": request["decoder_version"]}
        if not source_file(ctx.source_root, cid, "ARW").is_file():
            rows.append({**base, "error": "source ARW がありません"})
            continue
        row = run_coreimage(ctx, cid, mode, bool(request["lens"]), decoder_version=str(request["decoder_version"]),
                            profile=str(request["profile"]), variant=variant, adjustments=request["adjustments"],
                            max_edge=max(image.dimensions) if mode == "preview" else None)
        if row.get("error"):
            rows.append({**base, **row})
            continue
        current = read_rgb_upright(Path(row["output"]))
        with Image.open(image.path) as handle:
            reference = np.asarray(ImageOps.exif_transpose(handle).convert("RGB"), dtype=np.uint8)
        fitted, fit = fit_to_target(current, (reference.shape[1], reference.shape[0]))
        out_row = {**base, **row, "ci_width": current.shape[1], "ci_height": current.shape[0], "fit": fit,
                   **reference_diff(fitted, reference, "frozen"), **{f"ci_{k}": v for k, v in image_metrics(fitted).items()},
                   **{f"frozen_{k}": v for k, v in image_metrics(reference).items()}}
        rows.append(out_row)
        sheet_size = fit_size(reference.shape[1], reference.shape[0], 900)
        save_plain_jpeg(labeled_row([("frozen", resize_to(reference, sheet_size)), ("current Core Image", resize_to(fitted, sheet_size))],
                                    f"{cid} {mode}  ΔE76 {out_row['frozen_delta_e76']:.2f}  fit {fit}"),
                        ctx.out / "sheets" / f"{cid}_{mode}_{index:02d}_frozen.jpg")
        if capture is not None:
            output = Path(row["output"])
            with Image.open(output) as handle:
                rgb8 = np.asarray(handle.convert("RGB"))
                size = list(handle.size)
            captured.append({"capture_id": cid, "mode": mode, "path": output.relative_to(ctx.out).as_posix(),
                             "sha256": frozen.file_sha256(output),
                             "rgb8_sha256": hashlib.sha256(rgb8.tobytes()).hexdigest(), "dimensions": size,
                             "request": {k: request[k] for k in ("profile", "lens", "decoder_version", "adjustments")},
                             "coreimage_applied": {k: row.get(k) for k in ("ci_baseline_exposure", "ci_sharpness", "ci_lum_nr")}})
    write_rows_csv(ctx.out / "frozen_comparison.csv", rows, ("capture_id", "mode"))
    errors = [r for r in rows if r.get("error")]
    payload = {"out": str(ctx.out), "rows": rows, "errors": errors,
               "note": "差の合否閾値は持たない。凍結物の照合失敗と描画失敗だけを失敗として扱う。"}
    (ctx.out / "frozen_comparison.json").write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if capture is not None and (errors or len(captured) != len(images)):
        # 部分結果で完全な凍結基準を置き換えない（既存の capture ファイルには触れない）。
        ctx.say(f"[frozen] 描画エラーがあるため凍結マニフェストを書きません: {capture}")
    elif capture is not None:
        capture.parent.mkdir(parents=True, exist_ok=True)
        capture.write_text(json.dumps(captured, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        ctx.say(f"[frozen] 現在の CI 出力を凍結マニフェストに書きました: {capture}（凍結ルートは {ctx.out}）")
    (ctx.out / "run.log").write_text("\n".join(ctx.log) + "\n", encoding="utf-8")
    ctx.say(f"[frozen] 完了: {ctx.out}（描画エラー {len(errors)} 件）")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Core Image CLI の出力をカメラ JPEG・凍結基準（旧 rawpy は明示 opt-in）と比較します。")
    parser.add_argument("--source-root", type=Path, help="ARW / JPG の現像元フォルダ（--frozen-verify-only 以外では必須）")
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
    parser.add_argument(
        "--adjustments", action="store_true",
        help="Adjustments の評価モード（#72 / #73）: 掃引・後段オラクル・白点・既定リクエスト回帰。CLI 0.3.0 以上が必要",
    )
    parser.add_argument(
        "--diagnosis", action="store_true",
        help="Core Image の白飛びの段階別診断（#92）: CFA 飽和・段階読み出し・ROI 帰属・タスク C 候補。CLI 0.4.0 以上が必要",
    )
    parser.add_argument(
        "--presets", action="store_true",
        help="プリセット再調整の評価（#81）: CI 値（src-tauri/tests/fixtures/presets.json）の 11 プリセット・hr 0 比・"
             "d_base 比・§2.2 の等価性・シート。CLI 0.6.0 以上が必要",
    )
    parser.add_argument("--presets-no-rawpy", action="store_true", help="--presets で rawpy の legacy プリセット（参考列）を省く")
    parser.add_argument("--keep-dumps", action="store_true", help="--diagnosis の float ダンプ（.f32）を指標の計算後も残す")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="選定だけ行って終了する（--adjustments では組んだリクエストを adjustments_plan.json に書いて契約を検査する）",
    )
    parser.add_argument(
        "--features-cache", type=Path,
        help="選定用特徴量の JSON キャッシュ（あれば読み、無ければ書く。work/ 配下を推奨）",
    )
    parser.add_argument("--cli", type=Path, default=DEFAULT_CLI)
    parser.add_argument(
        "--legacy-rawpy", action="store_true",
        help="旧 rawpy / local_raw 経路（アーカイブ）を明示的に使う: rawpy 列・--bracket・--adjustments の実行・CFA 計測。"
             "local_raw/ が残る checkout と requirements-raw.txt の環境が前提（docs/evaluation-paths.md）",
    )
    parser.add_argument("--frozen-manifest", type=Path,
                        help="凍結基準のマニフェスト（例 tests/fixtures/baseline/rawpy_images.json）。照合してから現在の CI と比べる")
    parser.add_argument("--frozen-root", type=Path, help="凍結マニフェストの path の基準ディレクトリ（例 wt-107-baseline の checkout）")
    parser.add_argument("--frozen-verify-only", action="store_true", help="凍結物のハッシュ・寸法の照合だけ行う（CLI・現像元は不要）")
    parser.add_argument("--frozen-preset", default=frozen_presets.DEFAULT_PRESET,
                        help="request の無い凍結行で使う CI プリセット（presets.json。既定 neutral_recovery）")
    parser.add_argument("--frozen-capture", type=Path,
                        help="今回の CI 出力を凍結マニフェストとして書く（次の OS 更新で --frozen-manifest に渡す。--frozen-root は --out）")
    # 内部用（別プロセスのワーカー）
    parser.add_argument("--worker-rawpy", help=argparse.SUPPRESS)
    parser.add_argument("--worker-noop", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-mode", choices=["preview", "full"], default="preview", help=argparse.SUPPRESS)
    parser.add_argument("--worker-out", help=argparse.SUPPRESS)
    parser.add_argument("--worker-nosharp", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-adjustments", help=argparse.SUPPRESS)  # 既定プリセットへの上書き（JSON オブジェクト）
    parser.add_argument("--worker-preset", help=argparse.SUPPRESS)  # rawpy のプリセット名（--presets の legacy 参考）
    args = parser.parse_args(argv)

    if args.worker_noop:
        worker_noop(args)
        return
    if args.worker_rawpy:
        worker_rawpy(args)
        return

    if args.frozen_manifest:
        if not args.frozen_root:
            raise SystemExit("--frozen-manifest には --frozen-root が必要です")
        out = (args.out or DEFAULT_OUT_ROOT / time.strftime("frozen-%Y%m%d-%H%M%S")).resolve()
        manifest = args.frozen_manifest.resolve()
        root = args.frozen_root.expanduser().resolve()
        capture = args.frozen_capture.resolve() if args.frozen_capture else None
        conflicts = frozen_path_conflicts(manifest, root, out, None if args.frozen_verify_only else capture)
        if conflicts:
            # 照合レポートを含め、凍結入力のある場所へは何も書かない（凍結ルートとは別の --out を求める）。
            raise SystemExit("凍結入力を上書きしうる出力先です（別の --out / --frozen-capture を指定してください）:\n- "
                             + "\n- ".join(conflicts))
        images = verify_frozen(manifest, root, out)
        print(f"凍結物の照合: PASS（{len(images)} 件）", flush=True)
        if args.frozen_verify_only:
            return
        inside = [str(i.path) for i in images
                  if i.path.resolve().is_relative_to(out) or (capture is not None and i.path.resolve() == capture)]
        if inside:
            # 現在の CI 出力で凍結画像を上書きしないよう、凍結ルートとは別の --out を求める。
            raise SystemExit("--out / --frozen-capture が凍結画像と重なります（上書きを避けるため別の出力先を指定してください）: "
                             + ", ".join(inside))
        if not args.source_root or not args.source_root.expanduser().is_dir():
            raise SystemExit("--frozen-manifest の比較には --source-root（ARW のあるフォルダ）が必要です")
        if not args.cli.is_file():
            raise SystemExit(f"Core Image CLI がありません（npm run build:coreimage）: {args.cli}")
        ctx = RunContext(source_root=args.source_root.expanduser().resolve(), out=out, cli=args.cli.resolve(),
                         max_edge=args.max_edge, legacy_rawpy=False)
        raise SystemExit(run_frozen(ctx, images, preset=args.frozen_preset, capture=capture))
    if args.frozen_verify_only:
        raise SystemExit("--frozen-verify-only には --frozen-manifest が必要です")
    if args.source_root is None:
        raise SystemExit("--source-root を指定してください")
    if args.bracket and not args.legacy_rawpy:
        raise SystemExit(f"--bracket は {legacy.PREREQUISITE}")
    if args.adjustments and not args.dry_run and not args.legacy_rawpy:
        raise SystemExit(f"--adjustments の実行（rawpy 掃引・後段オラクル）は {legacy.PREREQUISITE}。--dry-run は不要")

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
    ctx = RunContext(source_root=source_root, out=out, cli=args.cli.resolve(), max_edge=args.max_edge,
                     legacy_rawpy=args.legacy_rawpy)

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
    if args.adjustments and args.dry_run:
        adjustments_dry_run(ctx, [r["capture_id"] for r in selection])
        return
    diagnosis_full_ids: list[str] = []
    if args.diagnosis:
        diagnosis_full_ids = [validate_capture_id(c) for c in (args.full_ids or [])] if args.full else []
        if args.full and not args.full_ids:
            diagnosis_full_ids = full_subset(selection, args.full_limit if args.full_limit > 0 else len(selection))
        unknown = sorted(set(diagnosis_full_ids) - set(features_by_id))
        if unknown:
            raise SystemExit(f"--full-ids は選定済みのカットから指定してください: {', '.join(unknown)}")
    if args.diagnosis and args.dry_run:
        diagnosis_dry_run(ctx, [r["capture_id"] for r in selection], diagnosis_full_ids)
        return
    presets_full_ids: list[str] = []
    if args.presets:
        presets_full_ids = [validate_capture_id(c) for c in (args.full_ids or [])] if args.full else []
        if args.full and not args.full_ids:
            presets_full_ids = [cid for cid in PRESET_FULL_IDS if cid in features_by_id]
        unknown = sorted(set(presets_full_ids) - set(features_by_id))
        if unknown:
            raise SystemExit(f"--full-ids は選定済みのカットから指定してください: {', '.join(unknown)}")
    if args.presets and args.dry_run:
        presets_dry_run(ctx, [r["capture_id"] for r in selection], presets_full_ids)
        return
    if args.dry_run:
        return

    if not ctx.cli.is_file():
        raise SystemExit(f"Core Image CLI がありません（npm run build:coreimage）: {ctx.cli}")
    if args.adjustments:
        problem = adjustments_cli_problem(cli_probe(ctx.cli, "--version"), cli_probe(ctx.cli, "--capabilities"))
        if problem:
            raise SystemExit(f"--adjustments を実行できません: {problem}")
        run_adjustments(ctx, selection, features_by_id)
        if not (args.sharpness or args.night or args.highlight or args.bracket):
            return
    if args.presets:
        version, capabilities = cli_probe(ctx.cli, "--version"), cli_probe(ctx.cli, "--capabilities")
        problem = presets_cli_problem(version, capabilities)
        if problem:
            raise SystemExit(f"--presets を実行できません: {problem}")
        env = environment_info(ctx, None)
        run_presets(ctx, selection, features_by_id, presets_full_ids, with_rawpy=not args.presets_no_rawpy, environment=env)
        if not (args.diagnosis or args.sharpness or args.night or args.highlight or args.bracket):
            return
    if args.diagnosis:
        version, capabilities = cli_probe(ctx.cli, "--version"), cli_probe(ctx.cli, "--capabilities")
        problem = diagnosis_cli_problem(version, capabilities)
        if problem:
            raise SystemExit(f"--diagnosis を実行できません: {problem}")
        env = environment_info(ctx, None)
        run_diagnosis(ctx, selection, features_by_id, diagnosis_full_ids, keep_dumps=args.keep_dumps, environment=env)
        if not (args.sharpness or args.night or args.highlight or args.bracket):
            return

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
    # 基準値: rawpy ワーカーの常駐コスト（旧経路のみ）、Core Image の初回起動（計測から除外するウォームアップ）
    if ctx.legacy_rawpy:
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
    try:
        main()
    except legacy.LegacyUnavailable as error:
        raise SystemExit(str(error)) from None
