"""JPG proxy based development candidate generation.

The candidate generator deliberately lives outside :mod:`engine`: it must not
decode the ARW once per particle.  A small, orientation-corrected JPEG proxy is
loaded once and all particles are evaluated against that array.  The output is
only a list of adjustments; selecting a candidate in the UI never develops a
RAW file by itself.

This is a lightweight multi-objective PSO.  Every style keeps the
non-dominated evaluations (rather than choosing from a single weighted sum),
and a final Pareto check is also performed before a candidate is selected.  The
proxy metrics are intentionally conservative estimates, not a claim of final
RAW quality.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import numpy as np
from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

from . import config
from .exif import read_capture_exif
from .presets import (
    ADJUSTMENT_RANGES,
    DEFAULT_PRESET,
    PRESETS,
    RawDevelopmentError,
    resolve_adjustments,
)

try:  # OpenCV is part of the RAW environment, but keep proxy tests portable.
    import cv2
except Exception:  # pragma: no cover - exercised only in minimal environments
    cv2 = None  # type: ignore[assignment]


CAPTURE_ID_PATTERN = re.compile(r"^DSC\d{5}$")

DEFAULT_BUDGET = 48
MIN_BUDGET = 32
MAX_BUDGET = 512
MAX_PROXY_EDGE = 640
MAX_PROXY_PIXELS = 50_000_000
MAX_STAR_BRIGHT_RATIO = 0.005

NUMERIC_KEYS: tuple[str, ...] = tuple(ADJUSTMENT_RANGES)
STYLE_IDS: tuple[str, ...] = ("natural", "stars", "foreground", "low_noise", "social")

STYLE_LABELS: dict[str, str] = {
    "natural": "自然",
    "stars": "星空",
    "foreground": "前景を明るく",
    "low_noise": "低ノイズ",
    "social": "SNS向け",
}

STYLE_DESCRIPTIONS: dict[str, str] = {
    "natural": "元JPEGの印象を保ち、白飛び・黒つぶれを穏やかに整えた基準候補",
    "stars": "星を残しながら暗い背景の階調と星色を守る候補",
    "foreground": "暗い前景を持ち上げ、空や明部を過度に壊さない候補",
    "low_noise": "平坦な暗部のノイズを抑え、細部を残す候補",
    "social": "小さな画面でも見やすいコントラストと色に寄せた候補",
}


class DevelopmentCandidateNotFound(FileNotFoundError):
    """The requested capture has no source JPG in the selected folder."""


class DevelopmentCandidateError(RawDevelopmentError):
    """Invalid candidate request or unreadable proxy image."""


def _normalise_rgb(image: Image.Image | np.ndarray) -> np.ndarray:
    if isinstance(image, Image.Image):
        image = image.convert("RGB")
        array = np.asarray(image, dtype=np.float32) / 255.0
    else:
        array = np.asarray(image, dtype=np.float32)
        if array.ndim == 2:
            array = np.repeat(array[..., None], 3, axis=2)
        if array.ndim != 3 or array.shape[2] < 3:
            raise DevelopmentCandidateError("代理画像はRGB画像で指定してください。")
        array = array[..., :3]
        if np.nanmax(array, initial=0.0) > 1.0:
            array = array / 255.0
    if array.ndim != 3 or array.shape[2] != 3:
        raise DevelopmentCandidateError("代理画像はRGB画像で指定してください。")
    if not np.isfinite(array).all():
        raise DevelopmentCandidateError("代理画像に不正な画素値があります。")
    return np.clip(array, 0.0, 1.0)


def load_proxy_image(source_path: Path, max_edge: int = MAX_PROXY_EDGE) -> np.ndarray:
    """Load and safely shrink a source JPG into an RGB float proxy.

    EXIF orientation is applied before shrinking so metric regions correspond
    to what the user sees.  The source is opened read-only and no derived file
    is written.  Dimension and file-size checks avoid accidentally expanding a
    malformed image during optimization.
    """

    if not isinstance(source_path, Path):
        source_path = Path(source_path)
    if max_edge < 64 or max_edge > 1600:
        raise DevelopmentCandidateError("代理画像の長辺は64〜1600pxで指定してください。")
    try:
        with Image.open(source_path) as opened:
            width, height = opened.size
            if width <= 0 or height <= 0 or width * height > MAX_PROXY_PIXELS:
                raise DevelopmentCandidateError("元JPGの画像サイズが大きすぎます。")
            oriented = ImageOps.exif_transpose(opened).convert("RGB")
            image = oriented
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            proxy = _normalise_rgb(image)
            return proxy
    except DevelopmentCandidateError:
        raise
    except (OSError, ValueError, UnidentifiedImageError) as error:
        raise DevelopmentCandidateError(f"元JPGを読み込めません: {error}") from error


def _connected_bright_components(mask: np.ndarray) -> list[tuple[int, int, int, int, int]]:
    """Return x, y, width, height, area for bright components."""

    if not np.any(mask):
        return []
    if cv2 is not None:
        count, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
        return [tuple(int(v) for v in stats[index]) for index in range(1, count)]

    # A small fallback for environments without cv2.  It is only used by tests
    # and keeps star gating independent from an optional acceleration import.
    height, width = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    components: list[tuple[int, int, int, int, int]] = []
    for y, x in zip(*np.nonzero(mask)):
        if visited[y, x]:
            continue
        stack = [(int(y), int(x))]
        visited[y, x] = True
        points: list[tuple[int, int]] = []
        while stack:
            cy, cx = stack.pop()
            points.append((cy, cx))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if not (dx or dy):
                        continue
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        stack.append((ny, nx))
        ys = [point[0] for point in points]
        xs = [point[1] for point in points]
        components.append((min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1, len(points)))
    return components


def _star_measurements(array: np.ndarray, exif: dict[str, Any] | None = None) -> dict[str, Any]:
    rgb = _normalise_rgb(array)
    gray = np.dot(rgb, np.array([0.2126, 0.7152, 0.0722], dtype=np.float32))
    dark_fraction = float(np.mean(gray < 0.20))
    # A high percentile avoids making a dim city skyline a collection of stars.
    threshold = max(0.58, float(np.percentile(gray, 99.35)))
    bright = gray >= threshold
    bright_ratio = float(np.mean(bright))
    components = _connected_bright_components(bright)
    height, width = gray.shape
    max_point_area = max(12, int(height * width * 0.00045))
    points = [component for component in components if 1 <= component[4] <= max_point_area]
    point_count = len(points)

    exif = exif or {}
    iso = exif.get("iso")
    exposure_time = exif.get("exposure_time")
    exif_night = False
    try:
        exif_night |= float(iso) >= 800
    except (TypeError, ValueError):
        pass
    try:
        exif_night |= float(exposure_time) >= 0.5
    except (TypeError, ValueError):
        pass
    captured_at = str(exif.get("captured_at") or "")
    if "T" in captured_at:
        try:
            hour = int(captured_at.split("T", 1)[1][:2])
            exif_night |= hour >= 20 or hour < 5
        except (TypeError, ValueError):
            pass

    # Stars need a dark background and multiple small isolated points.  EXIF is
    # a supporting signal, never a reason to force star optimization on a
    # regular daytime photo.  Very dark synthetic images are allowed without
    # EXIF, which makes proxy tests and exported JPEGs useful too.
    # A sparse bright-point pattern is useful for stars, while a larger bright
    # area usually describes lamps, buildings, or a city night scene.  Cap the
    # total bright area so those light sources cannot enable star optimization.
    visual_gate = dark_fraction >= 0.56 and point_count >= 3 and bright_ratio <= MAX_STAR_BRIGHT_RATIO
    is_star_scene = visual_gate and (exif_night or dark_fraction >= 0.72)

    halo = 0.0
    color_clip = float(np.mean(np.max(rgb, axis=2) >= 0.995))
    # A daytime/noisy image can contain thousands of tiny bright components.
    # They are useful for rejecting the star gate, but iterating over each one
    # for halo statistics would make a proxy evaluation needlessly expensive.
    if visual_gate and points:
        ring_values: list[float] = []
        for x, y, component_width, component_height, _ in points:
            pad = max(2, int(max(component_width, component_height) * 2))
            left, top = max(0, x - pad), max(0, y - pad)
            right = min(width, x + component_width + pad)
            bottom = min(height, y + component_height + pad)
            region = gray[top:bottom, left:right]
            if region.size == 0:
                continue
            core = gray[y : min(height, y + component_height), x : min(width, x + component_width)]
            ring = np.ones(region.shape, dtype=bool)
            ring[y - top : min(bottom, y + component_height) - top, x - left : min(right, x + component_width) - left] = False
            if np.any(ring):
                ring_values.append(float(np.mean(region[ring])))
            if core.size:
                color_clip += float(np.mean(np.max(rgb[y : y + component_height, x : x + component_width], axis=2) >= 0.995))
        if ring_values:
            halo = float(np.clip(np.mean(ring_values) / max(threshold, 0.01), 0.0, 1.0))

    return {
        "is_star_scene": bool(is_star_scene),
        "star_count": int(point_count),
        "dark_background_ratio": round(dark_fraction, 6),
        "bright_point_ratio": round(bright_ratio, 6),
        "exif_night": bool(exif_night),
        "star_halo": round(float(np.clip(halo, 0.0, 1.0)), 6),
        "star_color_saturation": round(float(np.clip(color_clip, 0.0, 1.0)), 6),
    }


def detect_star_scene(
    image: Image.Image | np.ndarray,
    exif: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return deterministic star-scene features from a proxy and EXIF hints."""

    return _star_measurements(_normalise_rgb(image), exif)


def is_star_scene(image: Image.Image | np.ndarray, exif: dict[str, Any] | None = None) -> bool:
    """Boolean convenience wrapper used by callers that only need the gate."""

    return bool(detect_star_scene(image, exif).get("is_star_scene"))


def _blur(array: np.ndarray, radius: float = 1.0) -> np.ndarray:
    if cv2 is not None:
        sigma = max(0.1, float(radius))
        return cv2.GaussianBlur(array, (0, 0), sigmaX=sigma, sigmaY=sigma)
    # PIL fallback is slower but deterministic and only affects tiny proxies.
    image = Image.fromarray(np.uint8(np.clip(array, 0.0, 1.0) * 255.0), mode="RGB")
    image = image.filter(ImageFilter.GaussianBlur(radius=max(0.1, float(radius))))
    return np.asarray(image, dtype=np.float32) / 255.0


def _choose_noise_reduction(
    style: str,
    proxy: np.ndarray,
    exif: dict[str, Any],
) -> Literal["off", "light", "full"]:
    gray = np.dot(proxy, np.array([0.2126, 0.7152, 0.0722], dtype=np.float32))
    smooth = _blur(proxy, 1.0)
    high_frequency = float(np.std(gray - np.dot(smooth, np.array([0.2126, 0.7152, 0.0722], dtype=np.float32))))
    try:
        iso = float(exif.get("iso") or 0)
    except (TypeError, ValueError):
        iso = 0.0
    noisy = iso >= 1600 or high_frequency >= 0.045
    if style == "low_noise":
        return "full"
    if style == "stars":
        # Keep point sources crisp when the source is clean; only use a light
        # pass on a genuinely noisy high-ISO night image.
        return "light" if noisy else "off"
    if style in {"natural", "foreground"}:
        return "full" if noisy else "light"
    if style == "social":
        return "light" if noisy else "off"
    return "light"


def _apply_proxy_adjustments(proxy: np.ndarray, adjustments: dict[str, Any]) -> np.ndarray:
    image = _normalise_rgb(proxy).copy()
    image *= float(2.0 ** float(adjustments["exposure"]))

    wb_temp = float(adjustments["wb_temp"])
    image[..., 0] *= 1.0 + 0.003 * wb_temp
    image[..., 2] *= 1.0 - 0.003 * wb_temp

    shadow_lift = float(adjustments["shadow_lift"]) / 100.0
    if shadow_lift:
        image += shadow_lift * 0.24 * (image * (1.0 - image) ** 2)

    recovery = float(adjustments["highlight_recovery"]) / 100.0
    if recovery:
        high = np.maximum(image - 0.72, 0.0)
        image -= high * recovery * 0.62

    image *= float(adjustments["brightness"])
    contrast = float(adjustments["contrast"])
    image = (image - 0.5) * contrast + 0.5

    saturation = float(adjustments["saturation"])
    luma = np.sum(image * np.array([0.2126, 0.7152, 0.0722], dtype=np.float32), axis=2, keepdims=True)
    image = luma + (image - luma) * saturation

    noise_reduction = adjustments.get("noise_reduction", "light")
    if noise_reduction == "light":
        image = _blur(image, 0.48)
    elif noise_reduction == "full":
        image = _blur(image, 0.9)

    sharpness = float(adjustments["sharpness"])
    if sharpness > 1.0:
        detail = image - _blur(image, 1.1)
        image += detail * min(0.75, (sharpness - 1.0) * 2.2)
    elif sharpness < 1.0:
        image = image * sharpness + _blur(image, 1.1) * (1.0 - sharpness)
    return np.clip(image, 0.0, 1.0)


def _image_metrics(
    source: np.ndarray,
    transformed: np.ndarray,
    exif: dict[str, Any] | None,
    star_gate: bool,
) -> dict[str, Any]:
    luma_weights = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    gray = np.sum(transformed * luma_weights, axis=2)
    source_gray = np.sum(source * luma_weights, axis=2)
    high_clip = float(np.mean(np.max(transformed, axis=2) >= 0.985))
    shadow_clip = float(np.mean(np.min(transformed, axis=2) <= 0.018))

    transformed_blur = _blur(transformed, 1.1)
    residual = gray - np.sum(transformed_blur * luma_weights, axis=2)
    gradient = np.abs(np.diff(gray, axis=0, prepend=gray[:1])) + np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
    source_gradient = np.abs(np.diff(source_gray, axis=0, prepend=source_gray[:1])) + np.abs(np.diff(source_gray, axis=1, prepend=source_gray[:, :1]))
    # Means/stds keep each particle cheap; percentile scans are surprisingly
    # expensive on a few-hundred-pixel proxy and do not improve ranking enough
    # to justify running them for every PSO evaluation.
    flat = gradient < max(0.008, float(np.mean(gradient) * 1.35))
    flat_noise = float(np.std(residual[flat])) if np.any(flat) else float(np.std(residual))
    source_noise = float(np.std(source_gradient[flat])) if np.any(flat) else float(np.std(source_gradient))

    detail = float(np.clip(np.mean(gradient) * 7.0 + np.std(gradient) * 2.0, 0.0, 1.0))
    source_detail = float(np.clip(np.mean(source_gradient) * 5.0 + np.std(source_gradient), 0.0, 1.0))
    # Overshoot is estimated from high-frequency energy relative to the source.
    halo = float(np.clip(max(0.0, np.std(residual) - source_noise * 0.35) * 10.0, 0.0, 1.0))
    channel_range = np.max(transformed, axis=2) - np.min(transformed, axis=2)
    colour_clip = float(np.mean((np.max(transformed, axis=2) >= 0.985) & (channel_range > 0.55)))
    gray_mean = float(np.mean(gray))
    gray_std = float(np.std(gray))
    p05, p95 = max(0.0, gray_mean - 1.65 * gray_std), min(1.0, gray_mean + 1.65 * gray_std)
    tonal_range = float(np.clip((p95 - p05) / 0.78, 0.0, 1.0))
    source_deviation = float(np.mean(np.abs(transformed - source)))

    star = _star_measurements(transformed, exif) if star_gate else {
        "star_count": 0,
        "star_halo": 0.0,
        "star_color_saturation": 0.0,
    }
    metrics: dict[str, Any] = {
        "highlight_clip": round(float(np.clip(high_clip, 0.0, 1.0)), 6),
        "shadow_clip": round(float(np.clip(shadow_clip, 0.0, 1.0)), 6),
        "noise_flat": round(float(np.clip(flat_noise, 0.0, 1.0)), 6),
        "source_noise_flat": round(float(np.clip(source_noise, 0.0, 1.0)), 6),
        "detail": round(detail, 6),
        "source_detail": round(source_detail, 6),
        "halo": round(halo, 6),
        "color_saturation_clip": round(float(np.clip(colour_clip, 0.0, 1.0)), 6),
        "tonal_range": round(tonal_range, 6),
        "source_deviation": round(float(np.clip(source_deviation, 0.0, 1.0)), 6),
        "star_count": int(star.get("star_count", 0)),
        "star_halo": round(float(star.get("star_halo", 0.0)), 6),
        "star_color_saturation": round(float(star.get("star_color_saturation", 0.0)), 6),
    }
    # Clear aliases make the response self-describing for the UI and leave a
    # stable vocabulary for clients that prefer the longer metric names.
    metrics.update(
        {
            "highlight_clipping": metrics["highlight_clip"],
            "shadow_crushing": metrics["shadow_clip"],
            "flat_region_noise": metrics["noise_flat"],
            "detail_halo": metrics["halo"],
            "color_clip": metrics["color_saturation_clip"],
            "tone_range": metrics["tonal_range"],
            "jpeg_deviation": metrics["source_deviation"],
        }
    )
    return metrics


def evaluate_adjustments(
    proxy: Image.Image | np.ndarray,
    adjustments: dict[str, Any],
    *,
    exif: dict[str, Any] | None = None,
    star_scene: bool | None = None,
) -> dict[str, Any]:
    """Evaluate a numeric adjustment set on a proxy without touching RAW.

    This public helper is intentionally useful for synthetic tests and future
    UI diagnostics.  Missing values use ``neutral_recovery`` defaults and all
    numeric values are clamped using the same bounds as the RAW engine.
    """

    if not isinstance(adjustments, dict):
        raise DevelopmentCandidateError("adjustments はオブジェクトで指定してください。")
    resolved = resolve_adjustments(DEFAULT_PRESET, adjustments)
    source = _normalise_rgb(proxy)
    gate = is_star_scene(source, exif) if star_scene is None else bool(star_scene)
    return _image_metrics(source, _apply_proxy_adjustments(source, resolved), exif, gate)


def clamp_adjustments(adjustments: dict[str, Any], noise_reduction: str | None = None) -> dict[str, Any]:
    """Clamp a candidate to the existing adjustment contract."""

    resolved = resolve_adjustments(DEFAULT_PRESET, adjustments)
    if noise_reduction is not None:
        resolved = resolve_adjustments(DEFAULT_PRESET, {**resolved, "noise_reduction": noise_reduction})
    return resolved


def _vector_from_adjustments(adjustments: dict[str, Any]) -> np.ndarray:
    return np.asarray([float(adjustments[key]) for key in NUMERIC_KEYS], dtype=np.float64)


def _adjustments_from_vector(vector: Sequence[float], noise_reduction: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for index, key in enumerate(NUMERIC_KEYS):
        minimum, maximum = ADJUSTMENT_RANGES[key]
        value = float(np.clip(vector[index], minimum, maximum))
        digits = 3 if key == "exposure" else (1 if key in {"wb_temp", "shadow_lift", "highlight_recovery"} else 4)
        result[key] = round(value, digits)
    result["noise_reduction"] = noise_reduction
    return result


def _safe_vector(vector: Sequence[float]) -> bool:
    """Reject combinations that are technically in range but visibly extreme."""

    values = {key: float(vector[index]) for index, key in enumerate(NUMERIC_KEYS)}
    if abs(values["exposure"]) > 1.85:
        return False
    if values["saturation"] > 1.42 or values["sharpness"] > 1.54:
        return False
    if values["shadow_lift"] > 96 and values["brightness"] > 1.30:
        return False
    if values["highlight_recovery"] < 4 and values["exposure"] > 1.65:
        return False
    if values["contrast"] > 1.37 and values["brightness"] < 0.76:
        return False
    return True


def _normalised_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_vector = _vector_from_adjustments(left)
    right_vector = _vector_from_adjustments(right)
    scales = np.asarray([ADJUSTMENT_RANGES[key][1] - ADJUSTMENT_RANGES[key][0] for key in NUMERIC_KEYS])
    return float(np.linalg.norm((left_vector - right_vector) / scales) / math.sqrt(len(NUMERIC_KEYS)))


def _dominates(left: Sequence[float], right: Sequence[float]) -> bool:
    left_array = np.asarray(left, dtype=np.float64)
    right_array = np.asarray(right, dtype=np.float64)
    return bool(np.all(left_array <= right_array + 1e-12) and np.any(left_array < right_array - 1e-12))


def pareto_front(points: Sequence[Sequence[float]] | np.ndarray) -> list[int]:
    """Return indices of the minimisation Pareto front.

    Keeping this explicit public helper makes the non-dominated decision easy
    to test independently from the image proxy and prevents a regression to
    five unrelated weighted sums.
    """

    values = np.asarray(points, dtype=np.float64)
    if values.ndim != 2 or values.shape[0] == 0:
        return []
    front: list[int] = []
    for index in range(values.shape[0]):
        if not any(_dominates(values[other], values[index]) for other in range(values.shape[0]) if other != index):
            front.append(index)
    return front


def _style_target(base: dict[str, Any], style: str) -> np.ndarray:
    target = _vector_from_adjustments(base)
    offsets = {
        "natural": {},
        "stars": {"exposure": 0.08, "wb_temp": -4, "shadow_lift": 8, "highlight_recovery": 14, "brightness": 0.01, "contrast": -0.02, "saturation": -0.02, "sharpness": -0.02},
        "foreground": {"exposure": 0.16, "shadow_lift": 25, "highlight_recovery": 12, "brightness": 0.06, "contrast": -0.05, "saturation": 0.02, "sharpness": 0.01},
        "low_noise": {"exposure": 0.05, "shadow_lift": 15, "highlight_recovery": 8, "brightness": 0.03, "contrast": -0.04, "sharpness": -0.09},
        "social": {"exposure": 0.03, "shadow_lift": -3, "highlight_recovery": -4, "brightness": 0.01, "contrast": 0.12, "saturation": 0.12, "sharpness": 0.09},
    }[style]
    for index, key in enumerate(NUMERIC_KEYS):
        target[index] += offsets.get(key, 0.0)
        minimum, maximum = ADJUSTMENT_RANGES[key]
        target[index] = np.clip(target[index], minimum, maximum)
    return target


def _metric_objectives(
    metrics: dict[str, Any],
    style: str,
    vector: Sequence[float],
    target: Sequence[float],
    base_vector: Sequence[float],
    star_reference: int,
) -> tuple[float, ...]:
    scales = np.asarray([ADJUSTMENT_RANGES[key][1] - ADJUSTMENT_RANGES[key][0] for key in NUMERIC_KEYS])
    style_distance = float(np.linalg.norm((np.asarray(vector) - np.asarray(target)) / scales) / math.sqrt(len(NUMERIC_KEYS)))
    base_distance = float(np.linalg.norm((np.asarray(vector) - np.asarray(base_vector)) / scales) / math.sqrt(len(NUMERIC_KEYS)))
    star_loss = 0.0
    if style == "stars":
        star_loss = max(0.0, (star_reference - float(metrics["star_count"])) / max(1.0, star_reference))
    return (
        float(metrics["highlight_clip"]) * 1.9,
        float(metrics["shadow_clip"]) * 1.5,
        float(metrics["noise_flat"]) * (1.25 if style == "low_noise" else 0.75),
        max(0.0, 0.72 - float(metrics["detail"])) * 1.2,
        float(metrics["halo"]) * (1.35 if style != "social" else 1.0),
        float(metrics["color_saturation_clip"]) * (1.3 if style != "social" else 0.9),
        max(0.0, 0.68 - float(metrics["tonal_range"])) * 1.0,
        float(metrics["source_deviation"]) * (1.55 if style == "natural" else 0.55),
        style_distance * 0.65,
        base_distance * (0.45 if style == "natural" else 0.1),
        star_loss * 2.2,
    )


def _utility(objectives: Sequence[float], style: str) -> float:
    # This is only a deterministic tie-break inside one already computed
    # Pareto front.  The candidate cannot be dominated by a discarded point.
    weights = np.asarray([1.2, 1.0, 1.1, 0.75, 0.9, 0.9, 0.75, 1.1, 1.0, 0.3, 1.5 if style == "stars" else 0.0])
    return float(np.dot(np.asarray(objectives, dtype=np.float64), weights))


def _score(metrics: dict[str, Any], style: str, base_metrics: dict[str, Any]) -> float:
    penalties = (
        float(metrics["highlight_clip"]) * 42.0
        + float(metrics["shadow_clip"]) * 32.0
        + float(metrics["noise_flat"]) * (34.0 if style == "low_noise" else 20.0)
        + float(metrics["halo"]) * 24.0
        + float(metrics["color_saturation_clip"]) * 28.0
        + max(0.0, 0.62 - float(metrics["tonal_range"])) * 30.0
        + float(metrics["source_deviation"]) * (26.0 if style == "natural" else 10.0)
    )
    quality = float(metrics["detail"]) * 22.0
    if style == "stars":
        reference = max(1, int(base_metrics.get("star_count", 0)))
        quality += min(20.0, 20.0 * float(metrics["star_count"]) / reference)
        penalties += float(metrics["star_halo"]) * 24.0 + float(metrics["star_color_saturation"]) * 10.0
    if style == "social":
        quality += float(metrics["tonal_range"]) * 8.0
    return round(float(np.clip(48.0 + quality - penalties, 0.0, 100.0)), 1)


def _reasons(style: str, metrics: dict[str, Any], base: dict[str, Any], star_scene: bool) -> list[str]:
    reasons: list[str] = []
    if float(metrics["highlight_clip"]) <= float(base["highlight_clip"]) + 0.004:
        reasons.append("明部の白飛びを増やさない範囲で調整")
    if float(metrics["shadow_clip"]) < float(base["shadow_clip"]) - 0.004:
        reasons.append("暗部の黒つぶれを抑えて階調を残す")
    if style == "natural":
        reasons.append("元JPEGからの変化を小さくした基準候補")
    elif style == "stars" and star_scene:
        reasons.append(f"小さな高輝度点を優先（検出目安 {int(metrics['star_count'])}点）")
    elif style == "foreground":
        reasons.append("暗い前景を持ち上げ、ハイライト回復を併用")
    elif style == "low_noise":
        reasons.append("平坦な暗部のノイズを抑える設定")
    elif style == "social":
        reasons.append("小さな表示でも見やすいコントラストと彩度")
    if float(metrics["halo"]) <= 0.12:
        reasons.append("細部の強調によるハローを監視")
    if not reasons:
        reasons.append("代理画像の複数指標から選んだ非劣解")
    return reasons[:4]


def _candidate_style_ids(proxy: np.ndarray, exif: dict[str, Any], star: dict[str, Any]) -> list[str]:
    gray = np.dot(proxy, np.array([0.2126, 0.7152, 0.0722], dtype=np.float32))
    shadow_ratio = float(np.mean(gray <= 0.08))
    noise = float(np.std(gray - np.dot(_blur(proxy, 1.0), np.array([0.2126, 0.7152, 0.0722], dtype=np.float32))))
    try:
        iso = float(exif.get("iso") or 0)
    except (TypeError, ValueError):
        iso = 0.0
    styles = ["natural"]
    if bool(star.get("is_star_scene")):
        styles.append("stars")
    if shadow_ratio >= 0.16:
        styles.append("foreground")
    if iso >= 1600 or noise >= 0.045 or shadow_ratio >= 0.35:
        styles.append("low_noise")
    # The social candidate is useful for most images, but not as a forced star
    # optimization.  Its own target remains conservative on dark scenes.
    styles.append("social")
    return styles[: len(STYLE_IDS)]


def _seed_for_style(seed: int, style: str) -> int:
    digest = hashlib.sha256(f"{seed}:{style}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "little") % (2**32)


def default_seed(capture_id: str, preset: str) -> int:
    """Derive a stable per-photo seed when the caller does not provide one."""

    digest = hashlib.sha256(f"{capture_id}:{preset}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def _validate_request(capture_id: str, preset: str, seed: int | None, budget: int | None) -> tuple[str, str, int, int]:
    if not isinstance(capture_id, str) or not CAPTURE_ID_PATTERN.fullmatch(capture_id):
        raise DevelopmentCandidateError("無効な撮影IDです。DSC00000形式で指定してください。")
    if not isinstance(preset, str) or preset not in PRESETS:
        raise DevelopmentCandidateError(f"未対応のプリセットです: {preset}")
    if seed is None:
        seed = default_seed(capture_id, preset)
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0 or seed >= 2**32:
        raise DevelopmentCandidateError("seed は0以上の32bit整数で指定してください。")
    if budget is None:
        budget = DEFAULT_BUDGET
    if isinstance(budget, bool) or not isinstance(budget, int) or not MIN_BUDGET <= budget <= MAX_BUDGET:
        raise DevelopmentCandidateError(f"budget は{MIN_BUDGET}〜{MAX_BUDGET}の整数で指定してください。")
    return capture_id, preset, int(seed), int(budget)


def _optimise_style(
    proxy: np.ndarray,
    exif: dict[str, Any],
    base: dict[str, Any],
    style: str,
    star: dict[str, Any],
    budget: int,
    seed: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    numeric_base = _vector_from_adjustments(base)
    target = _style_target(base, style)
    noise_reduction = _choose_noise_reduction(style, proxy, exif)
    base_metrics = _image_metrics(proxy, _apply_proxy_adjustments(proxy, base), exif, style == "stars")
    star_reference = int(star.get("star_count", 0))
    per_style = max(4, budget)
    particles = min(8, max(4, per_style // 2))
    iterations = max(0, per_style // particles - 1)
    rng = np.random.default_rng(_seed_for_style(seed, style))
    scales = np.asarray([ADJUSTMENT_RANGES[key][1] - ADJUSTMENT_RANGES[key][0] for key in NUMERIC_KEYS])
    lower = np.asarray([ADJUSTMENT_RANGES[key][0] for key in NUMERIC_KEYS], dtype=np.float64)
    upper = np.asarray([ADJUSTMENT_RANGES[key][1] for key in NUMERIC_KEYS], dtype=np.float64)
    positions = target + rng.normal(0.0, 0.09, size=(particles, len(NUMERIC_KEYS))) * scales
    positions[0] = numeric_base
    if particles > 1:
        positions[1] = target
    positions = np.clip(positions, lower, upper)
    velocities = rng.normal(0.0, 0.025, size=positions.shape) * scales
    evaluations: list[tuple[np.ndarray, dict[str, Any], tuple[float, ...]]] = []
    personal_best: list[tuple[np.ndarray, dict[str, Any], tuple[float, ...]] | None] = [None] * particles

    def evaluate_population(current: np.ndarray) -> None:
        current_evaluations: list[tuple[np.ndarray, dict[str, Any], tuple[float, ...]]] = []
        for vector in current:
            adjustments = _adjustments_from_vector(vector, noise_reduction)
            metrics = _image_metrics(proxy, _apply_proxy_adjustments(proxy, adjustments), exif, style == "stars")
            objectives = _metric_objectives(metrics, style, vector, target, numeric_base, star_reference)
            if not _safe_vector(vector):
                objectives = tuple(value + 3.0 for value in objectives)
            item = (np.asarray(vector, dtype=np.float64).copy(), metrics, objectives)
            current_evaluations.append(item)
            evaluations.append(item)
        current_front = pareto_front([item[2] for item in current_evaluations])
        for index, item in enumerate(current_evaluations):
            previous = personal_best[index]
            if previous is None or _dominates(item[2], previous[2]) or (
                not _dominates(previous[2], item[2]) and _utility(item[2], style) < _utility(previous[2], style)
            ):
                personal_best[index] = item
        # Keep a Pareto-derived leader.  Weighted utility is used only to pick
        # one leader among this generation's non-dominated points.
        leader_index = min(current_front or range(len(current_evaluations)), key=lambda i: _utility(current_evaluations[i][2], style))
        leader = current_evaluations[leader_index][0]
        for index in range(particles):
            r1 = rng.random(len(NUMERIC_KEYS))
            r2 = rng.random(len(NUMERIC_KEYS))
            best = personal_best[index][0] if personal_best[index] is not None else current[index]
            velocities[index] = (
                0.56 * velocities[index]
                + 1.25 * r1 * (best - current[index])
                + 1.40 * r2 * (leader - current[index])
            )
        current[:] = np.clip(current + velocities, lower, upper)

    evaluate_population(positions)
    for _ in range(iterations):
        evaluate_population(positions)

    front = pareto_front([item[2] for item in evaluations])
    choices = [evaluations[index] for index in front] or evaluations
    safe_choices = [item for item in choices if _safe_vector(item[0])] or choices
    chosen_vector, chosen_metrics, _ = min(safe_choices, key=lambda item: _utility(item[2], style))
    # For natural, the preset itself is the stable anchor.  It is evaluated
    # explicitly and cannot be displaced by a noisy proxy's tiny improvement.
    if style == "natural":
        chosen_vector = numeric_base
        chosen_adjustments = dict(base)
        chosen_metrics = base_metrics
    else:
        chosen_adjustments = _adjustments_from_vector(chosen_vector, noise_reduction)
    return chosen_adjustments, chosen_metrics


def generate_development_candidates(
    capture_id: str,
    preset: str = DEFAULT_PRESET,
    *,
    seed: int | None = None,
    budget: int | None = None,
) -> dict[str, Any]:
    """Generate up to five deterministic development candidates.

    ``capture_id`` and ``preset`` are required contract values; ``seed`` and
    ``budget`` are optional.  The source JPG is the only image decoded here.
    """

    capture_id, preset, seed, budget = _validate_request(capture_id, preset, seed, budget)
    source_root = config.source_root().resolve()
    source_path = (source_root / f"{capture_id}.JPG").resolve()
    if source_path.parent != source_root or not source_path.is_file():
        raise DevelopmentCandidateNotFound(f"元JPGが見つかりません: {capture_id}.JPG")
    exif = read_capture_exif(source_path)
    proxy = load_proxy_image(source_path)
    star = detect_star_scene(proxy, exif)
    styles = _candidate_style_ids(proxy, exif, star)
    base = resolve_adjustments(preset)
    base_metrics = _image_metrics(proxy, _apply_proxy_adjustments(proxy, base), exif, bool(star["is_star_scene"]))

    # Every style receives a deterministic, bounded share of the exploration
    # budget.  There is no repeated global weighted-sum run here: each style
    # explicitly filters its evaluations through a Pareto front.
    per_style_budget = max(4, budget // len(styles))
    candidates: list[dict[str, Any]] = []
    used_adjustments: list[dict[str, Any]] = []
    for style in styles:
        adjustments, metrics = _optimise_style(
            proxy,
            exif,
            base,
            style,
            star,
            per_style_budget,
            seed,
        )
        # If two Pareto choices collapse to the same slider values, move the
        # optional style a deterministic step toward its style target.  Natural
        # remains untouched and is always present.
        if style != "natural" and any(_normalised_distance(adjustments, previous) < 0.035 for previous in used_adjustments):
            target = _style_target(base, style)
            vector = _vector_from_adjustments(adjustments)
            vector = vector * 0.55 + target * 0.45
            adjustments = _adjustments_from_vector(vector, adjustments["noise_reduction"])
            metrics = _image_metrics(proxy, _apply_proxy_adjustments(proxy, adjustments), exif, style == "stars")
        used_adjustments.append(adjustments)
        candidate = {
            "id": style,
            "label": STYLE_LABELS[style],
            "description": STYLE_DESCRIPTIONS[style],
            "adjustments": adjustments,
            "metrics": metrics,
            "score": _score(metrics, style, base_metrics),
            "reasons": _reasons(style, metrics, base_metrics, bool(star["is_star_scene"])),
        }
        candidates.append(candidate)

    # Stable order is part of the deterministic response contract.
    order = {style: index for index, style in enumerate(STYLE_IDS)}
    candidates.sort(key=lambda candidate: order[candidate["id"]])
    return {
        "capture_id": capture_id,
        "preset": preset,
        "seed": seed,
        "budget": budget,
        "proxy": {
            "max_edge": max(proxy.shape[:2]),
            "width": int(proxy.shape[1]),
            "height": int(proxy.shape[0]),
            "source": "jpeg",
        },
        "star_scene": star,
        "candidates": candidates[:5],
    }


# Short aliases make the module pleasant to use from scripts while keeping the
# endpoint-facing name explicit.
generate_candidates = generate_development_candidates


__all__ = [
    "CAPTURE_ID_PATTERN",
    "DEFAULT_BUDGET",
    "DevelopmentCandidateError",
    "DevelopmentCandidateNotFound",
    "MAX_BUDGET",
    "MIN_BUDGET",
    "MAX_STAR_BRIGHT_RATIO",
    "STYLE_IDS",
    "clamp_adjustments",
    "detect_star_scene",
    "default_seed",
    "evaluate_adjustments",
    "generate_candidates",
    "generate_development_candidates",
    "is_star_scene",
    "load_proxy_image",
    "pareto_front",
]
