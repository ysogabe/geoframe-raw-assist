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
"""

from __future__ import annotations

import argparse
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
# I/O
# =============================================================================


def read_features(source_root: Path, capture_id: str) -> dict[str, Any]:
    """選定用の特徴量（EXIF 要約 + カメラ JPEG 縮小版の明暗比率）。GPS は読まない。"""
    from local_raw.exif import read_capture_exif

    jpg = source_root / f"{capture_id}.JPG"
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
    jpg = {p.stem for p in source_root.glob("DSC*.JPG") if p.is_file() and CAPTURE_ID_PATTERN.fullmatch(p.stem)}
    arw = {p.stem for p in source_root.glob("DSC*.ARW") if p.is_file() and CAPTURE_ID_PATTERN.fullmatch(p.stem)}
    return sorted(jpg & arw)


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
    label_h = 34
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
    with mock.patch.object(config, "source_root", return_value=source_root), \
         mock.patch.object(engine, "OUTPUT_ROOT", out_dir), \
         mock.patch.object(engine, "PREVIEW_ROOT", out_dir):
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


def run_rawpy(ctx: RunContext, capture_id: str, mode: str) -> dict[str, Any]:
    out_dir = ctx.out / "rawpy" / mode
    cmd = [
        sys.executable, str(Path(__file__).resolve()),
        "--source-root", str(ctx.source_root),
        "--worker-rawpy", capture_id,
        "--worker-mode", mode,
        "--worker-out", str(out_dir),
        "--max-edge", str(ctx.max_edge),
    ]
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


def run_coreimage(ctx: RunContext, capture_id: str, mode: str, lens: bool) -> dict[str, Any]:
    out_dir = ctx.out / "coreimage" / mode
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_lens" if lens else ""
    output = out_dir / f"{capture_id}_{mode}{suffix}.jpg"
    request: dict[str, Any] = {
        "input": str(ctx.source_root / f"{capture_id}.ARW"),
        "output": str(output),
        "mode": mode,
        "decoder_version": CI_DECODER_VERSION,
        "settings": {"profile": "neutral", "lens_correction": lens},
    }
    if mode == "preview":
        request["max_edge"] = ctx.max_edge
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

    camera = read_rgb_upright(ctx.source_root / f"{capture_id}.JPG")
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

    env = environment_info(ctx, source_root / f"{selection[0]['capture_id']}.ARW")
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
