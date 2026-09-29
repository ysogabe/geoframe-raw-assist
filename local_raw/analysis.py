"""Photo analysis pipeline for GeoFrame RAW Assist.

Scans the configured source folder (``config.source_root()``, default
``sample/100MSDCF``) for JPG/ARW pairs, scores every JPEG, generates
thumbnails, groups consecutive shots and produces RAW-development
recommendations. The result matches the ``Analysis`` type frozen in
``docs/API_CONTRACT.md`` §3 and is cached at ``outputs/analysis/analysis.json``.

This module deliberately does **not** import ``engine.py``: analysis only needs
Pillow / OpenCV / the standard library, and staying decoupled keeps the
analysis endpoints working independently of the RAW development stack.

Threshold constants are module level on purpose — Wave 3 tunes them against
real photographs without touching the logic.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import traceback
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageOps

from . import config, source_files

# OpenCV spawns its own worker pool. Combined with our ThreadPoolExecutor that
# oversubscribes the CPU badly, so keep cv2 single threaded and let the
# executor own the parallelism.
cv2.setNumThreads(1)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 現像元フォルダはユーザーが切り替えられるため定数にしない。参照は呼び出し時に
# ``config.source_root()`` で解決する（モジュールレベルで束縛すると import 時に固定される）。
THUMBS_ROOT = PROJECT_ROOT / "outputs" / "thumbnails"
ANALYSIS_ROOT = PROJECT_ROOT / "outputs" / "analysis"
ANALYSIS_PATH = ANALYSIS_ROOT / "analysis.json"

ANALYSIS_VERSION = 2

# --- pipeline tuning -------------------------------------------------------
MAX_WORKERS = 6
SCORE_DRAFT_DIVISOR = 8          # Image.draft target = full size // this
THUMB_MAX_EDGE = 640
THUMB_QUALITY = 80
HIGHLIGHT_LEVEL = 250            # a channel at/above this counts as clipped
SHADOW_LEVEL = 5                 # all channels at/below this count as crushed

# --- grouping --------------------------------------------------------------
# Tuned on the 281-pair sample library (Wave 3). 12s/1.3 left 85% of groups as
# single shots; 60s/1.5 folds the "linger at one viewpoint and take a few frames"
# pattern into one group (single 84.6% -> 57.6%) while the largest group stays at
# 6 photos. Note: the 10-18mm zoom spans exactly 1.8x, so a ratio above ~1.8 would
# make the focal rule inert on this library — see docs/GEOFRAME_RAW_ASSIST_HANDOFF.md.
GROUP_GAP_SECONDS = 60.0         # larger gap starts a new group
GROUP_FOCAL_RATIO = 1.5          # focal length ratio above this starts a new group
BURST_MEDIAN_GAP_SECONDS = 1.5   # median gap at/below this => "burst"

# Bracket detection. The sample set uses a *constant* +-0.3EV everyday exposure
# compensation on 96.4% of frames (docs/spikes-wave0.md), so "bias != 0" would
# fire almost everywhere. A real bracket needs several *distinct* bias values
# spanning at least BRACKET_MIN_SPAN_EV.
#
# Note on the comparison: EXIF stores the bias as the rational -3/10, and in
# IEEE754 `0.3 - (-0.3)` is exactly 0.6 (doubling is exact in binary floating
# point), so a group holding both -0.3 and +0.3 lands precisely on the
# threshold. "span >= 0.6EV" is inclusive by definition, and the epsilon below
# makes that inclusion robust rather than dependent on exact representation.
BRACKET_MIN_DISTINCT_BIAS = 2
BRACKET_MIN_SPAN_EV = 0.6
BRACKET_SPAN_EPSILON = 1e-6

# --- representative selection ---------------------------------------------
WEIGHT_SHARPNESS = 0.6
WEIGHT_HIGHLIGHT = 0.4

# confidence = CONFIDENCE_FLOOR + (1 - CONFIDENCE_FLOOR) * min(margin / CONFIDENCE_FULL_MARGIN, 1)
# where `margin` is the weighted-score gap between the top and runner-up photo.
# A dead heat yields the floor, a margin at/above CONFIDENCE_FULL_MARGIN yields
# 1.0. Single-photo groups are always 1.0.
#
# CONFIDENCE_FULL_MARGIN is 1.0 because the weighted rank score itself spans
# 0..1, so 1.0 is the largest margin that can actually occur. Normalising by a
# smaller value saturates the scale: at 0.5 the lowest confidence reachable by
# a group of three is exactly 0.600, which NEEDS_REVIEW_CONFIDENCE's strict "<"
# then excludes, leaving summary.needs_review permanently zero.
CONFIDENCE_FLOOR = 0.5
CONFIDENCE_FULL_MARGIN = 1.0
NEEDS_REVIEW_CONFIDENCE = 0.6    # groups below this land in summary.needs_review

# --- RAW recommendation ----------------------------------------------------
RAW_STRONG_ISO = 1600
RAW_STRONG_NIGHT_LUMA = 60.0
RAW_STRONG_HIGHLIGHT = 0.04
RAW_RECOMMENDED_HIGHLIGHT = 0.015
RAW_RECOMMENDED_SHADOW = 0.20
RAW_RECOMMENDED_ISO = 800
RAW_MAYBE_HIGHLIGHT = 0.005
RAW_MAYBE_SHADOW = 0.10
# Plan said "ExposureBias != 0" but 96% of the sample carries a constant -0.3,
# so only a pronounced negative compensation (deliberate underexposure, where
# RAW headroom actually helps) counts.
RAW_MAYBE_EXPOSURE_BIAS = -0.7

RAW_LEVEL_LABELS = {
    "strong": "RAW現像を強く推奨",
    "recommended": "RAW現像がおすすめ",
    "maybe": "RAW現像の余地あり",
    "none": "JPEGで十分",
}

KIND_LABELS = {
    "burst": "連写",
    "bracket": "ブラケット",
    "similar": "類似",
    "single": "単写",
}

__all__ = [
    "ANALYSIS_PATH",
    "ANALYSIS_ROOT",
    "ANALYSIS_VERSION",
    "PROJECT_ROOT",
    "THUMBS_ROOT",
    "build_analysis",
    "extract_exif",
    "get_analysis_state",
    "group_photos",
    "is_analysis_running",
    "make_thumbnail",
    "read_cached_analysis",
    "recommend",
    "scan_pairs",
    "score_array",
    "score_photo",
    "start_analysis",
]


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------


def scan_pairs(source_root: Path | None = None) -> tuple[dict[str, Any], list[Path]]:
    """Enumerate ``DSC*.JPG`` and check for the matching ``.ARW``.

    Returns ``(source_info, jpg_paths)`` where ``source_info`` matches the
    ``Analysis.source`` contract shape. ``total_bytes`` covers every JPG and
    ARW file found (that is what the user would copy off the card).
    """
    root = source_root if source_root is not None else config.source_root()
    # 拡張子の大文字小文字は問わない（`source_files` 参照）。
    jpg_paths = list(source_files.list_capture_files(root, "JPG").values()) if root.is_dir() else []
    arw_paths = list(source_files.list_capture_files(root, "ARW").values()) if root.is_dir() else []

    jpg_stems = {path.stem for path in jpg_paths}
    arw_stems = {path.stem for path in arw_paths}

    total_bytes = 0
    for path in (*jpg_paths, *arw_paths):
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass

    source_info = {
        # ``SourceFolderInfo.display_path`` と同じ整形（プロジェクト相対 / ``~/...`` / 絶対）。
        "root": config.display_path(root),
        "pairs": len(jpg_stems & arw_stems),
        "jpg_only": len(jpg_stems - arw_stems),
        "arw_only": len(arw_stems - jpg_stems),
        "total_bytes": total_bytes,
    }
    return source_info, jpg_paths


def _source_signature(source_root: Path | None = None) -> str:
    """sha1 over the resolved root path **and** the sorted JPG+ARW filename list.

    The root path is part of the signature because ``capture_id`` is a camera
    counter: two different cards can both hold ``DSC02943``. Without it, a
    similarly shaped folder would silently reuse another folder's analysis.
    """
    root = (source_root if source_root is not None else config.source_root()).resolve()
    names = source_files.source_entry_names(root) if root.is_dir() else []
    payload = "\n".join([str(root), *names])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# EXIF
# ---------------------------------------------------------------------------


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return result if result == result else None  # drop NaN


def _parse_datetime(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    for pattern in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def extract_exif(path: Path) -> dict[str, Any]:
    """Read the EXIF fields the analysis needs.

    ``SubsecTimeOriginal`` is intentionally absent: it does not exist on any of
    the sample frames (docs/spikes-wave0.md), so capture time resolution is one
    second and ordering is stabilised with the filename.
    """
    record: dict[str, Any] = {
        "capture_id": path.stem,
        "filename": path.name,
        "captured_at": None,
        "iso": None,
        "f_number": None,
        "shutter": None,
        "focal_length": None,
        "exposure_bias": None,
        "lens_model": None,
        "make": None,
        "model": None,
        "orientation": 1,
        "width": None,
        "height": None,
    }
    with Image.open(path) as image:
        record["width"], record["height"] = image.size
        exif = image.getexif()
        record["make"] = exif.get(0x010F)
        record["model"] = exif.get(0x0110)
        orientation = exif.get(0x0112)
        record["orientation"] = int(orientation) if orientation else 1
        ifd = exif.get_ifd(0x8769)

    captured = _parse_datetime(ifd.get(0x9003))
    record["captured_at"] = captured.isoformat() if captured else None
    iso = ifd.get(0x8827)
    if isinstance(iso, (tuple, list)):
        iso = iso[0] if iso else None
    record["iso"] = int(iso) if iso is not None else None
    record["f_number"] = _to_float(ifd.get(0x829D))
    record["shutter"] = _to_float(ifd.get(0x829A))       # seconds
    record["exposure_bias"] = _to_float(ifd.get(0x9204))  # EV
    record["focal_length"] = _to_float(ifd.get(0x920A))
    lens_model = ifd.get(0xA434)
    record["lens_model"] = str(lens_model) if lens_model else None
    return record


# ---------------------------------------------------------------------------
# scoring + thumbnails (one decode per photo)
# ---------------------------------------------------------------------------


def score_array(rgb: np.ndarray) -> dict[str, float]:
    """Compute the quality metrics from an already decoded RGB uint8 array."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    return {
        "sharpness_raw": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
        "highlight_clip_ratio": float((rgb >= HIGHLIGHT_LEVEL).any(axis=2).mean()),
        "shadow_clip_ratio": float((rgb <= SHADOW_LEVEL).all(axis=2).mean()),
        "mean_luma": float(gray.mean()),
    }


def _decode_small(path: Path) -> tuple[np.ndarray, Image.Image]:
    """Decode once at ~1/8 scale, returning the metric array and an upright image.

    Metrics are rotation invariant so they use the raw (untransposed) array;
    the thumbnail uses the EXIF-corrected image so the UI shows it upright.
    """
    with Image.open(path) as image:
        width, height = image.size
        image.draft("RGB", (max(1, width // SCORE_DRAFT_DIVISOR), max(1, height // SCORE_DRAFT_DIVISOR)))
        decoded = image.convert("RGB")
        upright = ImageOps.exif_transpose(decoded)
    return np.asarray(decoded), upright


def score_photo(path: Path) -> dict[str, float]:
    """Score a JPEG. Kept as a standalone entry point for tests/tooling."""
    rgb, upright = _decode_small(path)
    upright.close()
    return score_array(rgb)


def make_thumbnail(
    image: Image.Image,
    capture_id: str,
    thumbs_root: Path | None = None,
    root_key: str | None = None,
) -> str:
    """Write ``{root_key}-{capture_id}.webp`` (long edge 640, quality 80), return its URL.

    The ``root_key`` prefix keeps two source folders that share a ``capture_id``
    from overwriting each other's thumbnail. It stays a **flat** prefix on
    purpose: ``server._serve_from_root`` rejects anything whose resolved parent
    is not the thumbnails root, and nesting would force that guard to be loosened.
    """
    root = thumbs_root or THUMBS_ROOT
    key = root_key or config.source_root_key()
    root.mkdir(parents=True, exist_ok=True)
    thumb = image.copy()
    thumb.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE), Image.LANCZOS)
    name = f"{key}-{capture_id}"
    destination = root / f"{name}.webp"
    temporary = root / f"{name}.tmp.webp"
    thumb.save(temporary, "WEBP", quality=THUMB_QUALITY)
    os.replace(temporary, destination)
    thumb.close()
    return f"/thumbs/{name}.webp"


def _process_photo(
    path: Path, thumbs_root: Path | None = None, root_key: str | None = None
) -> dict[str, Any] | None:
    """EXIF + metrics + thumbnail from a single decode.

    Returns ``None`` (after logging to stdout) when the JPEG cannot be
    *decoded* -- e.g. a truncated file from an interrupted card copy, a
    Pillow decompression-bomb guard trip, or a malformed EXIF rational --
    instead of raising, so one bad photo does not abort the whole
    ``ThreadPoolExecutor.map()`` batch in ``build_analysis()``. The
    exception classes here intentionally mirror ``read_capture_exif``'s.

    Failures *after* a successful decode (thumbnail write, scoring) are
    **not** swallowed: a full disk or an unwritable thumbnails directory is
    a real failure, not a bad photo, and should not be reported as one
    photo silently missing from an otherwise "successful" batch.
    """
    try:
        record = extract_exif(path)
        rgb, upright = _decode_small(path)
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        # Single write (end="") so concurrent calls from other pool threads
        # can't interleave mid-line -- print()'s default two-write form
        # (message, then the newline) is not atomic across threads.
        print(
            f"[raw-analysis] JPEGを読み込めないためスキップします: {path.name} ({type(error).__name__})\n",
            end="",
            flush=True,
        )
        return None
    try:
        record.update(score_array(rgb))
        record["thumb"] = make_thumbnail(upright, record["capture_id"], thumbs_root, root_key)
        return record
    finally:
        upright.close()


# ---------------------------------------------------------------------------
# grouping (pure functions — synthetic-data testable)
# ---------------------------------------------------------------------------


def _sort_key(record: dict[str, Any]) -> tuple[bool, datetime, str]:
    captured = _parse_datetime(record.get("captured_at"))
    # Records without a timestamp sort last; the bool keeps datetime.min from
    # ever being compared against a real timestamp.
    return (captured is None, captured or datetime.min, str(record.get("capture_id") or ""))


def _starts_new_group(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_at = _parse_datetime(previous.get("captured_at"))
    current_at = _parse_datetime(current.get("captured_at"))
    if previous_at is None or current_at is None:
        return True
    if (current_at - previous_at).total_seconds() > GROUP_GAP_SECONDS:
        return True
    if (previous.get("lens_model") or None) != (current.get("lens_model") or None):
        return True
    previous_focal = previous.get("focal_length")
    current_focal = current.get("focal_length")
    if previous_focal and current_focal and previous_focal > 0 and current_focal > 0:
        ratio = max(previous_focal, current_focal) / min(previous_focal, current_focal)
        if ratio > GROUP_FOCAL_RATIO:
            return True
    return False


def _is_bracket(records: Sequence[dict[str, Any]]) -> bool:
    biases = [record.get("exposure_bias") for record in records]
    values = [value for value in biases if value is not None]
    if len({round(value, 3) for value in values}) < BRACKET_MIN_DISTINCT_BIAS:
        return False
    return (max(values) - min(values)) >= BRACKET_MIN_SPAN_EV - BRACKET_SPAN_EPSILON


def _classify_kind(records: Sequence[dict[str, Any]]) -> str:
    if len(records) < 2:
        return "single"
    # Bracket wins over burst when a group satisfies both.
    if _is_bracket(records):
        return "bracket"
    timestamps = [_parse_datetime(record.get("captured_at")) for record in records]
    gaps = [
        (later - earlier).total_seconds()
        for earlier, later in zip(timestamps, timestamps[1:])
        if earlier is not None and later is not None
    ]
    if gaps and median(gaps) <= BURST_MEDIAN_GAP_SECONDS:
        return "burst"
    return "similar"


def group_photos(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split EXIF records into shot groups. Pure function.

    Splits on a capture-time gap over ``GROUP_GAP_SECONDS``, a lens change, or a
    focal length ratio over ``GROUP_FOCAL_RATIO``.
    """
    ordered = sorted(records, key=_sort_key)
    groups: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    for record in ordered:
        if current and _starts_new_group(current[-1], record):
            groups.append(_finalize_group(current, len(groups)))
            current = []
        current.append(record)
    if current:
        groups.append(_finalize_group(current, len(groups)))
    return groups


def _finalize_group(records: Sequence[dict[str, Any]], index: int) -> dict[str, Any]:
    kind = _classify_kind(records)
    timestamps = [_parse_datetime(record.get("captured_at")) for record in records]
    known = [value for value in timestamps if value is not None]
    start_at = known[0].isoformat() if known else None
    end_at = known[-1].isoformat() if known else None
    clock = known[0].strftime("%H:%M") if known else "--:--"
    return {
        "id": index,
        "kind": kind,
        "title": f"{clock} {KIND_LABELS[kind]} {len(records)}枚",
        "start_at": start_at,
        "end_at": end_at,
        "capture_ids": [str(record.get("capture_id")) for record in records],
        "records": list(records),
    }


# ---------------------------------------------------------------------------
# recommendation
# ---------------------------------------------------------------------------


def _rank_scores(values: Sequence[float], higher_is_better: bool) -> list[float]:
    """Map values to 0..1 by rank (best = 1). Ties share the better rank."""
    count = len(values)
    if count < 2:
        return [1.0] * count
    order = sorted(range(count), key=lambda i: values[i], reverse=higher_is_better)
    scores = [0.0] * count
    rank = 0
    for position, index in enumerate(order):
        if position > 0 and values[index] != values[order[position - 1]]:
            rank = position
        scores[index] = (count - 1 - rank) / (count - 1)
    return scores


def recommend(group: dict[str, Any], scores: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Pick the representative photo and build its explanation.

    Representative = weighted blend of the in-group sharpness rank
    (``WEIGHT_SHARPNESS``) and highlight-retention rank (``WEIGHT_HIGHLIGHT``).
    ``overall_score`` is that same blend scaled to 0-100 so the representative
    always carries the highest score in its group.
    """
    if not scores:
        return {
            "recommended_capture_id": "",
            "confidence": 1.0,
            "recommendation_reasons": [],
            "overall_scores": {},
            "sharpness_scores": {},
        }

    sharpness = [float(item.get("sharpness_raw") or 0.0) for item in scores]
    highlight = [float(item.get("highlight_clip_ratio") or 0.0) for item in scores]
    sharpness_ranks = _rank_scores(sharpness, higher_is_better=True)
    highlight_ranks = _rank_scores(highlight, higher_is_better=False)
    weighted = [
        WEIGHT_SHARPNESS * sharpness_ranks[i] + WEIGHT_HIGHLIGHT * highlight_ranks[i]
        for i in range(len(scores))
    ]

    capture_ids = [str(item.get("capture_id")) for item in scores]
    # Highest weighted score wins; ties break on the earliest capture id so the
    # choice is deterministic across runs.
    best_index = min(range(len(weighted)), key=lambda i: (-weighted[i], capture_ids[i]))

    if len(weighted) < 2:
        confidence = 1.0
    else:
        runner_up = max(value for i, value in enumerate(weighted) if i != best_index)
        margin = weighted[best_index] - runner_up
        confidence = CONFIDENCE_FLOOR + (1.0 - CONFIDENCE_FLOOR) * min(margin / CONFIDENCE_FULL_MARGIN, 1.0)

    reasons: list[dict[str, str]] = []
    if len(scores) < 2:
        reasons.append({"code": "single_shot", "text": "このシーンで唯一の写真です。"})
    else:
        if sharpness_ranks[best_index] >= 1.0:
            reasons.append({"code": "sharpest", "text": "グループ内で最もシャープです。"})
        if highlight_ranks[best_index] >= 1.0:
            reasons.append({"code": "highlight_retained", "text": "グループ内で白飛びが最も少ない構図です。"})
        if not reasons:
            reasons.append({"code": "balanced", "text": "シャープさと白飛びの少なさのバランスが最良です。"})
        if confidence < NEEDS_REVIEW_CONFIDENCE:
            reasons.append({"code": "close_call", "text": "次点との差が小さいため、目視での確認をおすすめします。"})

    # sharpness_score: in-group min-max normalisation to 0-100. A single photo,
    # or a group where every photo scores identically, gets a flat 100.
    lowest, highest = min(sharpness), max(sharpness)
    span = highest - lowest
    sharpness_scores = {
        capture_ids[i]: (100.0 if span <= 0 else round((sharpness[i] - lowest) / span * 100.0, 1))
        for i in range(len(scores))
    }
    overall_scores = {capture_ids[i]: round(weighted[i] * 100.0, 1) for i in range(len(scores))}

    return {
        "recommended_capture_id": capture_ids[best_index],
        "confidence": round(confidence, 3),
        "recommendation_reasons": reasons,
        "overall_scores": overall_scores,
        "sharpness_scores": sharpness_scores,
    }


def raw_recommendation(group_kind: str, representative: dict[str, Any]) -> dict[str, Any]:
    """Four-level RAW recommendation for a group, judged on its representative."""
    iso = representative.get("iso") or 0
    mean_luma = float(representative.get("mean_luma") or 0.0)
    highlight = float(representative.get("highlight_clip_ratio") or 0.0)
    shadow = float(representative.get("shadow_clip_ratio") or 0.0)
    bias = representative.get("exposure_bias")

    codes: list[str] = []
    reasons: list[str] = []
    night = iso >= RAW_STRONG_ISO and mean_luma < RAW_STRONG_NIGHT_LUMA

    if night:
        codes.append("night_scene")
        reasons.append(f"高感度(ISO {iso})の暗いシーンです。RAWならノイズと階調を作り直せます。")
    if iso >= RAW_RECOMMENDED_ISO:
        codes.append("high_iso")
        reasons.append(f"ISO {iso} と高感度のため、ノイズ低減の効果が大きい写真です。")
    if highlight >= RAW_MAYBE_HIGHLIGHT:
        codes.append("highlight_clipping")
        reasons.append(f"明るい部分の白飛びが {highlight * 100:.1f}% あります。RAWなら階調を引き戻せます。")
    if shadow >= RAW_MAYBE_SHADOW:
        codes.append("deep_shadows")
        reasons.append(f"暗部の黒つぶれが {shadow * 100:.1f}% あります。RAWなら持ち上げられます。")
    if group_kind == "bracket":
        codes.append("exposure_bracket")
        reasons.append("露出を変えて撮影されています。RAWでの合成・選択に向いています。")

    if night or highlight >= RAW_STRONG_HIGHLIGHT:
        level = "strong"
    elif highlight >= RAW_RECOMMENDED_HIGHLIGHT or shadow >= RAW_RECOMMENDED_SHADOW or iso >= RAW_RECOMMENDED_ISO:
        level = "recommended"
    elif (
        highlight >= RAW_MAYBE_HIGHLIGHT
        or shadow >= RAW_MAYBE_SHADOW
        or (bias is not None and bias <= RAW_MAYBE_EXPOSURE_BIAS)
    ):
        level = "maybe"
    else:
        level = "none"

    if level == "maybe" and bias is not None and bias <= RAW_MAYBE_EXPOSURE_BIAS and not codes:
        reasons.append(f"露出を {bias:+.1f}EV 下げて撮影されています。RAWなら明るさを作り直せます。")

    if level == "none" and not reasons:
        reasons.append("白飛び・黒つぶれが少なく、JPEGのままで十分な写真です。")

    return {
        "level": level,
        "label": RAW_LEVEL_LABELS[level],
        "reason_codes": codes,
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# full build
# ---------------------------------------------------------------------------


def build_analysis(
    progress: "Progress | None" = None,
    source_root: Path | None = None,
    thumbs_root: Path | None = None,
) -> dict[str, Any]:
    """Run the whole pipeline and return the contract ``Analysis`` payload."""
    reporter = progress or Progress()
    reporter.set_stage("scanning")
    # Resolve the root exactly once: every thumbnail name in this run has to
    # carry the same root_key, even if the user switches folders mid-build.
    root = (source_root if source_root is not None else config.source_root()).resolve()
    root_key = config.root_key_for(root)
    source_info, jpg_paths = scan_pairs(root)
    reporter.set_total(len(jpg_paths))

    reporter.set_stage("scoring")
    records: list[dict[str, Any]] = []
    skipped = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for result in pool.map(lambda path: _process_photo(path, thumbs_root, root_key), jpg_paths):
            reporter.advance()
            if result is None:
                skipped += 1
                continue
            records.append(result)
    if skipped:
        print(f"[raw-analysis] {skipped}枚のJPEGをデコードできずスキップしました（全{len(jpg_paths)}枚中）", flush=True)

    reporter.set_stage("grouping")
    groups = group_photos(records)

    photos: dict[str, Any] = {}
    group_infos: list[dict[str, Any]] = []
    raw_candidates = 0
    needs_review = 0

    for group in groups:
        members = group.pop("records")
        outcome = recommend(group, members)
        by_id = {str(item["capture_id"]): item for item in members}
        representative = by_id.get(outcome["recommended_capture_id"], members[0] if members else {})
        raw = raw_recommendation(group["kind"], representative)

        for member in members:
            capture_id = str(member["capture_id"])
            photos[capture_id] = {
                "capture_id": capture_id,
                "captured_at": member.get("captured_at"),
                "iso": member.get("iso"),
                "f_number": member.get("f_number"),
                "shutter": member.get("shutter"),
                "focal_length": member.get("focal_length"),
                "exposure_bias": member.get("exposure_bias"),
                "lens_model": member.get("lens_model"),
                "orientation": member.get("orientation"),
                "width": member.get("width"),
                "height": member.get("height"),
                "sharpness_raw": round(float(member.get("sharpness_raw") or 0.0), 3),
                "sharpness_score": outcome["sharpness_scores"].get(capture_id, 100.0),
                "highlight_clip_ratio": round(float(member.get("highlight_clip_ratio") or 0.0), 5),
                "shadow_clip_ratio": round(float(member.get("shadow_clip_ratio") or 0.0), 5),
                "mean_luma": round(float(member.get("mean_luma") or 0.0), 2),
                "overall_score": outcome["overall_scores"].get(capture_id, 100.0),
                "thumb": member.get("thumb") or f"/thumbs/{root_key}-{capture_id}.webp",
            }

        if raw["level"] in ("recommended", "strong"):
            raw_candidates += 1
        if outcome["confidence"] < NEEDS_REVIEW_CONFIDENCE:
            needs_review += 1

        group_infos.append({
            "id": group["id"],
            "kind": group["kind"],
            "title": group["title"],
            "start_at": group["start_at"],
            "end_at": group["end_at"],
            "capture_ids": group["capture_ids"],
            "recommended_capture_id": outcome["recommended_capture_id"],
            "confidence": outcome["confidence"],
            "recommendation_reasons": outcome["recommendation_reasons"],
            "raw": raw,
        })

    return {
        "analysis_version": ANALYSIS_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": source_info,
        "summary": {
            "groups": len(group_infos),
            "raw_candidates": raw_candidates,
            "needs_review": needs_review,
        },
        "photos": photos,
        "groups": group_infos,
    }


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

# Cache-validation signature. Kept out of the contract Analysis payload: it is
# written to disk but stripped before the value is returned over HTTP.
SIGNATURE_KEY = "_source_signature"


def _write_analysis(payload: dict[str, Any]) -> None:
    ANALYSIS_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = ANALYSIS_PATH.with_suffix(".tmp.json")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, ANALYSIS_PATH)


def read_cached_analysis(source_root: Path | None = None) -> dict[str, Any] | None:
    """Return the cached Analysis if it is present and still valid, else None.

    ``source_root`` is threaded through so a caller that already resolved the
    folder validates against *that* folder rather than re-resolving the config.
    """
    if not ANALYSIS_PATH.is_file():
        return None
    try:
        payload = json.loads(ANALYSIS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("analysis_version") != ANALYSIS_VERSION:
        return None
    if payload.pop(SIGNATURE_KEY, None) != _source_signature(source_root):
        return None
    return payload


# ---------------------------------------------------------------------------
# job management
# ---------------------------------------------------------------------------


class Progress:
    """Thread-safe progress counter shared with the polling endpoint."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stage = "scanning"
        self._done = 0
        self._total = 0

    def set_stage(self, stage: str) -> None:
        with self._lock:
            self._stage = stage

    def set_total(self, total: int) -> None:
        with self._lock:
            self._total = int(total)

    def advance(self) -> None:
        with self._lock:
            self._done += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"stage": self._stage, "done": self._done, "total": self._total}


_STATE_LOCK = threading.Lock()
_RUNNING = False
_PROGRESS: Progress | None = None
_LAST_ERROR: str | None = None


def _run_analysis(progress: Progress, source_root: Path) -> None:
    global _RUNNING, _LAST_ERROR
    try:
        # The root is captured by start_analysis and passed in, so the payload
        # and the signature it is stored under always describe the same folder.
        # (POST /api/source-folder refuses to switch while a job runs; this is
        # the belt-and-braces half of that guarantee.)
        payload = build_analysis(progress, source_root)
        payload[SIGNATURE_KEY] = _source_signature(source_root)
        _write_analysis(payload)
        with _STATE_LOCK:
            _LAST_ERROR = None
    except Exception as error:  # keep the worker from wedging the state machine
        traceback.print_exc()
        with _STATE_LOCK:
            _LAST_ERROR = str(error)
    finally:
        with _STATE_LOCK:
            _RUNNING = False


def start_analysis(force: bool = False) -> str:
    """Start a background analysis. Returns started | already_running | ready."""
    global _RUNNING, _PROGRESS
    source_root = config.source_root().resolve()
    with _STATE_LOCK:
        if _RUNNING:
            return "already_running"
        if not force and read_cached_analysis(source_root) is not None:
            return "ready"
        progress = Progress()
        _PROGRESS = progress
        _RUNNING = True
    try:
        threading.Thread(
            target=_run_analysis, args=(progress, source_root), daemon=True, name="raw-analysis"
        ).start()
    except RuntimeError:
        # start() failed before the worker could run (e.g. the OS refused a new
        # thread) -- undo the _RUNNING flip above so /api/analysis does not get
        # wedged reporting "running" forever.
        with _STATE_LOCK:
            _RUNNING = False
        raise
    return "started"


def is_analysis_running() -> bool:
    """解析ジョブが実行中かどうか。

    現像元フォルダの切り替え可否判定（契約 §1 の `analysis_running`）に使う。
    `get_analysis_state()` と同じく `_STATE_LOCK` の下で読む。
    """
    with _STATE_LOCK:
        return _RUNNING


def get_analysis_state() -> dict[str, Any]:
    """Contract §1 payload for ``GET /api/analysis``."""
    with _STATE_LOCK:
        running = _RUNNING
        progress = _PROGRESS
    if running and progress is not None:
        return {"status": "running", "progress": progress.snapshot()}
    cached = read_cached_analysis()
    if cached is not None:
        return {"status": "ready", "analysis": cached}
    return {"status": "absent"}
