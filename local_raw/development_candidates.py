"""RAW縮小デコード代理による現像候補生成。

候補生成は :mod:`engine` の現像を粒子ごとに丸ごと回さない。代わりに ARW を
``half_size=True`` で縮小デコードした配列を ``(highlight_mode, noise_reduction,
exposure)`` をキーにキャッシュし、その上で **engine の後段関数そのもの**
（``_apply_shadow_lift_array`` → ``_resize_array`` → ``_apply_tone_array``）を
適用して評価する。以前の「カメラJPG＋独自トーン式」代理は本番現像を予測できなかった
（highlight_recovery は rawpy のデコード時3段階、shadow_lift 係数・exposure の
auto_bright 圧縮・sharpness の輝度限定などが全て食い違った）ため、代理と本番の
ずれを構造的になくす設計へ作り直している。

代理が省略するもの:
- レンズ補正（幾何補正が中心で、トーン指標にほぼ影響しないため）
- full-size デモザイク（half_size で縮小デコード。ノイズ/細部の絶対値は本番と異なる）

探索の扱い:
- ``highlight_recovery`` は連続値ではなく3カテゴリ（Clip / Blend / Reconstruct）。
  各カテゴリの代表値（0 / 50 / 85、プリセットが同カテゴリならプリセット値）を返す。
- ``exposure`` は ``EXPOSURE_GRID`` 刻みにスナップしてデコード時の ``bright`` に渡す。
- ``wb_temp`` は探索しない（プリセット値のまま）。``noise_reduction`` はスタイルごとに
  固定で、その値でデコードしたキャッシュを使って評価する。
- ``natural`` はプリセット値そのもの（基準画像）で、PSO 予算は他スタイルに配分する。

多目的PSOであり、各スタイルは評価した点の非劣解（Pareto front）から選ぶ。最終候補は
基準（natural）より黒つぶれ・白飛びが大きく悪化していないか等の安全策を通し、
落とした候補は ``rejected`` として理由付きで返す。出力は調整値の提案だけで、候補を
選んでも RAW 現像は実行されない。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ContextManager, Literal

import numpy as np
from PIL import Image, ImageFilter

from . import config, engine
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
#: 評価に使う代理画像の長辺（engine の shadow_lift → resize 後）。
MAX_PROXY_EDGE = 640
#: デコード結果をキャッシュに保持するときの長辺。shadow_lift をこの解像度で当ててから
#: MAX_PROXY_EDGE へ縮小する（engine は原寸で lift → resize だが、点ごとの演算なので差は微小）。
DECODE_EDGE = 1280
#: キャッシュに保持するデコード結果の最大数（1280px の float32 RGB は約13MB）。
DECODE_CACHE_LIMIT = 24
#: exposure の探索格子（EV）。デコード時の bright に渡す値なので粗いほどデコード回数が減る。
EXPOSURE_GRID = 0.5
MAX_STAR_BRIGHT_RATIO = 0.005

#: highlight_recovery のカテゴリ（engine `_postprocess_params` の3段階と一致させる）。
HIGHLIGHT_CATEGORIES: tuple[str, ...] = ("clip", "blend", "reconstruct")
HIGHLIGHT_REPRESENTATIVE: dict[str, float] = {"clip": 0.0, "blend": 50.0, "reconstruct": 85.0}

#: 安全策のしきい値（基準 natural との差、絶対値）。
CLIP_WORSEN_LIMIT = 0.10
#: foreground: 基準の暗部（輝度下位25%）の平均輝度がこれ以上明るくなること。
SHADOW_GAIN_MIN = 0.005
SHADOW_QUANTILE = 0.25

#: PSO が探索する連続量。highlight_class は 0..3 の連続値をカテゴリへ丸める。
SEARCH_KEYS: tuple[str, ...] = (
    "exposure",
    "shadow_lift",
    "brightness",
    "contrast",
    "saturation",
    "sharpness",
    "highlight_class",
)
SEARCH_RANGES: dict[str, tuple[float, float]] = {
    **{key: ADJUSTMENT_RANGES[key] for key in SEARCH_KEYS if key != "highlight_class"},
    "highlight_class": (0.0, 3.0),
}
STYLE_IDS: tuple[str, ...] = ("natural", "stars", "foreground", "low_noise", "social")

STYLE_LABELS: dict[str, str] = {
    "natural": "自然",
    "stars": "星空",
    "foreground": "前景を明るく",
    "low_noise": "低ノイズ",
    "social": "SNS向け",
}

STYLE_DESCRIPTIONS: dict[str, str] = {
    "natural": "プリセット値のままの基準候補（他の候補との比較の基準）",
    "stars": "星を残しながら暗い背景の階調と星色を守る候補",
    "foreground": "暗い前景を持ち上げ、空や明部を過度に壊さない候補",
    "low_noise": "平坦な暗部のノイズを抑え、細部を残す候補",
    "social": "小さな画面でも見やすいコントラストと色に寄せた候補",
}

DecodeFn = Callable[[str, str, float], np.ndarray]
#: ``(arw_path, preset_adjustments) -> context manager yielding a DecodeFn``。
DecoderFactory = Callable[[Path, dict[str, Any]], ContextManager[DecodeFn]]


class DevelopmentCandidateNotFound(FileNotFoundError):
    """The requested capture has no source ARW in the selected folder."""


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


# ---------------------------------------------------------------------------
# highlight_recovery categories / exposure grid
# ---------------------------------------------------------------------------


def highlight_category(highlight_recovery: float) -> str:
    """engine `_postprocess_params` と同じ境界（<=0 Clip / <75 Blend / それ以上 Reconstruct）。"""

    value = float(highlight_recovery)
    if value <= 0.0:
        return "clip"
    if value < 75.0:
        return "blend"
    return "reconstruct"


def representative_highlight(category: str, preset_value: float) -> float:
    """カテゴリの代表値。プリセット値が同カテゴリならプリセット値を優先する。"""

    if highlight_category(preset_value) == category:
        return float(preset_value)
    return HIGHLIGHT_REPRESENTATIVE[category]


def snap_exposure(value: float) -> float:
    minimum, maximum = ADJUSTMENT_RANGES["exposure"]
    snapped = round(float(value) / EXPOSURE_GRID) * EXPOSURE_GRID
    return round(float(min(maximum, max(minimum, snapped))), 3)


def _class_index(value: float) -> int:
    return int(min(len(HIGHLIGHT_CATEGORIES) - 1, max(0, math.floor(value))))


# ---------------------------------------------------------------------------
# RAW half-size decode + cache
# ---------------------------------------------------------------------------


@contextmanager
def open_raw_half_decoder(source_path: Path, base: dict[str, Any]) -> Iterator[DecodeFn]:
    """ARW を1回だけ開き、``half_size`` 縮小デコードを返す関数を渡す。

    デコード時パラメータは engine の `_postprocess_params` をそのまま使い、
    ``half_size`` だけ True に差し替える（wb_temp・auto_bright・向き補正も engine と同じ）。
    """

    import rawpy

    try:
        raw = rawpy.imread(str(source_path))
    except (rawpy.LibRawError, OSError) as error:
        raise DevelopmentCandidateError(f"ARWを開けません: {error}") from error
    base_highlight = float(base["highlight_recovery"])

    def decode(category: str, noise_reduction: str, exposure: float) -> np.ndarray:
        adjustments = {
            **base,
            "exposure": float(exposure),
            "highlight_recovery": representative_highlight(category, base_highlight),
            "noise_reduction": noise_reduction,
        }
        params = engine._postprocess_params(raw, adjustments, False)
        params["half_size"] = True
        try:
            rgb = raw.postprocess(**params)
        except (rawpy.LibRawError, OSError) as error:
            raise DevelopmentCandidateError(f"ARWの縮小デコードに失敗しました: {error}") from error
        return engine._resize_array(engine._decode_16bit_to_float01(rgb), DECODE_EDGE)

    try:
        yield decode
    finally:
        raw.close()


class DecodeCache:
    """``(highlight category, noise_reduction, exposure)`` → デコード済み配列（LRU）。"""

    def __init__(self, decode: DecodeFn, limit: int = DECODE_CACHE_LIMIT) -> None:
        self._decode = decode
        self._limit = max(1, int(limit))
        self._store: OrderedDict[tuple[str, str, float], np.ndarray] = OrderedDict()
        self.decode_count = 0

    def get(self, category: str, noise_reduction: str, exposure: float) -> np.ndarray:
        key = (category, noise_reduction, round(float(exposure), 4))
        cached = self._store.get(key)
        if cached is not None:
            self._store.move_to_end(key)
            return cached
        array = np.asarray(self._decode(category, noise_reduction, key[2]), dtype=np.float32)
        if array.ndim != 3 or array.shape[2] != 3 or not np.isfinite(array).all():
            raise DevelopmentCandidateError("縮小デコード結果が不正です。")
        self.decode_count += 1
        self._store[key] = array
        while len(self._store) > self._limit:
            self._store.popitem(last=False)
        return array


def render_proxy(decoded: np.ndarray, adjustments: dict[str, Any]) -> np.ndarray:
    """デコード済み配列へ engine の後段（shadow_lift → resize → tone）を適用する。

    ``engine.develop_raw`` の後段と同じ関数（`engine.apply_post_decode_chain`）。
    レンズ補正は省略している。
    """

    return engine.apply_post_decode_chain(decoded, adjustments, MAX_PROXY_EDGE)


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
    # 星の色の指標なので、検出した星（点）の領域だけで測る（画像全体は含めない）。
    color_clip = 0.0
    # A daytime/noisy image can contain thousands of tiny bright components.
    # They are useful for rejecting the star gate, but iterating over each one
    # for halo statistics would make a proxy evaluation needlessly expensive.
    if visual_gate and points:
        ring_values: list[float] = []
        point_clip_ratios: list[float] = []
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
                point_clip_ratios.append(
                    float(np.mean(np.max(rgb[y : y + component_height, x : x + component_width], axis=2) >= 0.995))
                )
        # 星ごとの白飛び割合の平均（合計にすると星が多いだけで1.0に張り付く）。
        if point_clip_ratios:
            color_clip = float(np.mean(point_clip_ratios))
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



def _image_metrics(
    baseline: np.ndarray,
    transformed: np.ndarray,
    exif: dict[str, Any] | None,
    star_gate: bool,
    shadow_mask: np.ndarray | None = None,
) -> dict[str, Any]:
    luma_weights = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    gray = np.sum(transformed * luma_weights, axis=2)
    source_gray = np.sum(baseline * luma_weights, axis=2)
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
    # Overshoot is estimated from high-frequency energy relative to the baseline.
    halo = float(np.clip(max(0.0, np.std(residual) - source_noise * 0.35) * 10.0, 0.0, 1.0))
    channel_range = np.max(transformed, axis=2) - np.min(transformed, axis=2)
    colour_clip = float(np.mean((np.max(transformed, axis=2) >= 0.985) & (channel_range > 0.55)))
    gray_mean = float(np.mean(gray))
    gray_std = float(np.std(gray))
    p05, p95 = max(0.0, gray_mean - 1.65 * gray_std), min(1.0, gray_mean + 1.65 * gray_std)
    tonal_range = float(np.clip((p95 - p05) / 0.78, 0.0, 1.0))
    baseline_deviation = float(np.mean(np.abs(transformed - baseline)))
    shadow_mean = float(np.mean(gray[shadow_mask])) if shadow_mask is not None and np.any(shadow_mask) else gray_mean

    # The baseline is measured with exactly the same star function/gate, so the
    # "did stars increase" comparison is not a tautology against a zero count.
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
        "source_deviation": round(float(np.clip(baseline_deviation, 0.0, 1.0)), 6),
        "shadow_mean": round(float(np.clip(shadow_mean, 0.0, 1.0)), 6),
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
            "baseline_deviation": metrics["source_deviation"],
        }
    )
    return metrics


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


def _seed_for_style(seed: int, style: str) -> int:
    digest = hashlib.sha256(f"{seed}:{style}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "little") % (2**32)


def default_seed(capture_id: str, preset: str) -> int:
    """Derive a stable per-photo seed when the caller does not provide one."""

    digest = hashlib.sha256(f"{capture_id}:{preset}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")



def _search_scales() -> np.ndarray:
    return np.asarray([SEARCH_RANGES[key][1] - SEARCH_RANGES[key][0] for key in SEARCH_KEYS], dtype=np.float64)


def _vector_from_adjustments(adjustments: dict[str, Any]) -> np.ndarray:
    values: list[float] = []
    for key in SEARCH_KEYS:
        if key == "highlight_class":
            # カテゴリの中央値。0..3 の連続値を floor でカテゴリへ丸める。
            values.append(HIGHLIGHT_CATEGORIES.index(highlight_category(adjustments["highlight_recovery"])) + 0.5)
        else:
            values.append(float(adjustments[key]))
    return np.asarray(values, dtype=np.float64)


def _adjustments_from_vector(vector: Sequence[float], base: dict[str, Any], noise_reduction: str) -> dict[str, Any]:
    """探索ベクトルを Adjustments へ。exposure は格子へスナップ、highlight はカテゴリ代表値。"""

    result: dict[str, Any] = {"wb_temp": float(base["wb_temp"])}
    for index, key in enumerate(SEARCH_KEYS):
        if key == "highlight_class":
            continue
        minimum, maximum = SEARCH_RANGES[key]
        value = float(np.clip(vector[index], minimum, maximum))
        if key == "exposure":
            result[key] = snap_exposure(value)
        else:
            result[key] = round(value, 1 if key == "shadow_lift" else 4)
    category = HIGHLIGHT_CATEGORIES[_class_index(float(vector[SEARCH_KEYS.index("highlight_class")]))]
    result["highlight_recovery"] = representative_highlight(category, float(base["highlight_recovery"]))
    result["noise_reduction"] = noise_reduction
    return result


def _safe_vector(vector: Sequence[float]) -> bool:
    """Reject combinations that are technically in range but visibly extreme."""

    values = {key: float(vector[index]) for index, key in enumerate(SEARCH_KEYS)}
    if abs(values["exposure"]) > 1.85:
        return False
    if values["saturation"] > 1.42 or values["sharpness"] > 1.54:
        return False
    if values["shadow_lift"] > 96 and values["brightness"] > 1.30:
        return False
    if _class_index(values["highlight_class"]) == 0 and values["exposure"] > 1.65:
        return False
    if values["contrast"] > 1.37 and values["brightness"] < 0.76:
        return False
    return True


def _normalised_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    delta = (_vector_from_adjustments(left) - _vector_from_adjustments(right)) / _search_scales()
    return float(np.linalg.norm(delta) / math.sqrt(len(SEARCH_KEYS)))


def _style_target(base: dict[str, Any], style: str) -> np.ndarray:
    target = _vector_from_adjustments(base)
    offsets = {
        "natural": {},
        "stars": {"exposure": 0.08, "shadow_lift": 8, "highlight_recovery": 14, "brightness": 0.01, "contrast": -0.02, "saturation": -0.02, "sharpness": -0.02},
        "foreground": {"exposure": 0.16, "shadow_lift": 25, "highlight_recovery": 12, "brightness": 0.06, "contrast": -0.05, "saturation": 0.02, "sharpness": 0.01},
        "low_noise": {"exposure": 0.05, "shadow_lift": 15, "highlight_recovery": 8, "brightness": 0.03, "contrast": -0.04, "sharpness": -0.09},
        "social": {"exposure": 0.03, "shadow_lift": -3, "highlight_recovery": -4, "brightness": 0.01, "contrast": 0.12, "saturation": 0.12, "sharpness": 0.09},
    }[style]
    for index, key in enumerate(SEARCH_KEYS):
        if key == "highlight_class":
            hr_minimum, hr_maximum = ADJUSTMENT_RANGES["highlight_recovery"]
            wanted = float(np.clip(float(base["highlight_recovery"]) + offsets.get("highlight_recovery", 0.0), hr_minimum, hr_maximum))
            target[index] = HIGHLIGHT_CATEGORIES.index(highlight_category(wanted)) + 0.5
            continue
        minimum, maximum = SEARCH_RANGES[key]
        target[index] = np.clip(target[index] + offsets.get(key, 0.0), minimum, maximum)
    return target


# ---------------------------------------------------------------------------
# Safety guard against the natural baseline
# ---------------------------------------------------------------------------


def _guard_failure(style: str, metrics: dict[str, Any], base: dict[str, Any]) -> str | None:
    """基準（natural）と比べて壊れた候補なら理由文字列、問題なければ None。"""

    highlight_delta = float(metrics["highlight_clip"]) - float(base["highlight_clip"])
    if highlight_delta >= CLIP_WORSEN_LIMIT:
        return f"白飛びが基準より{highlight_delta * 100:.1f}pt増えるため除外"
    shadow_delta = float(metrics["shadow_clip"]) - float(base["shadow_clip"])
    if shadow_delta >= CLIP_WORSEN_LIMIT:
        return f"黒つぶれが基準より{shadow_delta * 100:.1f}pt増えるため除外"
    if style == "low_noise" and float(metrics["noise_flat"]) > float(base["noise_flat"]):
        return "平坦部ノイズが基準より悪化するため除外"
    if style == "foreground" and float(metrics["shadow_mean"]) < float(base["shadow_mean"]) + SHADOW_GAIN_MIN:
        return "暗部が基準より明るくならないため除外"
    return None


def _guard_penalty(style: str, metrics: dict[str, Any], base: dict[str, Any]) -> float:
    """PSO が安全策に違反する領域を避けるための連続ペナルティ（0 で問題なし）。"""

    half = CLIP_WORSEN_LIMIT / 2.0
    penalty = max(0.0, float(metrics["highlight_clip"]) - float(base["highlight_clip"]) - half)
    penalty += max(0.0, float(metrics["shadow_clip"]) - float(base["shadow_clip"]) - half)
    if style == "low_noise":
        penalty += max(0.0, float(metrics["noise_flat"]) - float(base["noise_flat"])) * 10.0
    if style == "foreground":
        penalty += max(0.0, float(base["shadow_mean"]) + SHADOW_GAIN_MIN - float(metrics["shadow_mean"])) * 2.0
    return penalty


def _metric_objectives(
    metrics: dict[str, Any],
    style: str,
    vector: Sequence[float],
    target: Sequence[float],
    base_vector: Sequence[float],
    star_reference: int,
    guard_penalty: float = 0.0,
) -> tuple[float, ...]:
    scales = _search_scales()
    root = math.sqrt(len(SEARCH_KEYS))
    style_distance = float(np.linalg.norm((np.asarray(vector) - np.asarray(target)) / scales) / root)
    base_distance = float(np.linalg.norm((np.asarray(vector) - np.asarray(base_vector)) / scales) / root)
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
        float(metrics["source_deviation"]) * 0.55,
        style_distance * 0.65,
        base_distance * 0.1,
        star_loss * 2.2,
        guard_penalty * 4.0,
    )


def _utility(objectives: Sequence[float], style: str) -> float:
    # This is only a deterministic tie-break inside one already computed
    # Pareto front.  The candidate cannot be dominated by a discarded point.
    weights = np.asarray([1.2, 1.0, 1.1, 0.75, 0.9, 0.9, 0.75, 1.1, 1.0, 0.3, 1.5 if style == "stars" else 0.0, 3.0])
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
    if style != "natural":
        if float(metrics["highlight_clip"]) <= float(base["highlight_clip"]) + 0.004:
            reasons.append("明部の白飛びを基準より増やさない範囲で調整")
        if float(metrics["shadow_clip"]) < float(base["shadow_clip"]) - 0.004:
            reasons.append("暗部の黒つぶれを基準より抑えて階調を残す")
    if style == "natural":
        reasons.append("プリセット値のままの基準候補（他候補はこの画像と比較）")
    elif style == "stars" and star_scene:
        reasons.append(
            f"小さな高輝度点を優先（検出目安 {int(metrics['star_count'])}点、基準 {int(base['star_count'])}点）"
        )
    elif style == "foreground":
        reasons.append("暗い前景を持ち上げ、ハイライト回復を併用")
    elif style == "low_noise":
        reasons.append("平坦な暗部のノイズを基準より抑える設定")
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


class _Evaluator:
    """デコードキャッシュ経由で調整値を評価する（同一調整値はメモ化）。"""

    def __init__(self, cache: DecodeCache, exif: dict[str, Any], base: dict[str, Any], star_gate: bool) -> None:
        self.cache = cache
        self.exif = exif
        self.base = base
        self.star_gate = star_gate
        self._memo: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.image = self.render(base)
        gray = np.sum(self.image * np.array([0.2126, 0.7152, 0.0722], dtype=np.float32), axis=2)
        self.shadow_mask = gray <= np.quantile(gray, SHADOW_QUANTILE)
        self.metrics = _image_metrics(self.image, self.image, exif, star_gate, self.shadow_mask)

    def render(self, adjustments: dict[str, Any]) -> np.ndarray:
        decoded = self.cache.get(
            highlight_category(adjustments["highlight_recovery"]),
            str(adjustments["noise_reduction"]),
            float(adjustments["exposure"]),
        )
        return render_proxy(decoded, adjustments)

    def evaluate(self, adjustments: dict[str, Any], star_gate: bool) -> dict[str, Any]:
        key = (star_gate, *(adjustments[name] for name in sorted(adjustments)))
        cached = self._memo.get(key)
        if cached is None:
            cached = _image_metrics(self.image, self.render(adjustments), self.exif, star_gate, self.shadow_mask)
            self._memo[key] = cached
        return cached


def _optimise_style(
    evaluator: _Evaluator,
    style: str,
    star_reference: int,
    budget: int,
    seed: int,
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """スタイルごとの多目的PSO。戻り値は (調整値, 指標, 安全策違反の理由 or None)。"""

    base = evaluator.base
    base_metrics = evaluator.metrics
    numeric_base = _vector_from_adjustments(base)
    target = _style_target(base, style)
    noise_reduction = _choose_noise_reduction(style, evaluator.image, evaluator.exif)
    star_gate = style == "stars"
    per_style = max(4, budget)
    particles = min(8, max(4, per_style // 2))
    iterations = max(0, per_style // particles - 1)
    rng = np.random.default_rng(_seed_for_style(seed, style))
    scales = _search_scales()
    lower = np.asarray([SEARCH_RANGES[key][0] for key in SEARCH_KEYS], dtype=np.float64)
    upper = np.asarray([SEARCH_RANGES[key][1] for key in SEARCH_KEYS], dtype=np.float64)
    class_index = SEARCH_KEYS.index("highlight_class")
    positions = target + rng.normal(0.0, 0.09, size=(particles, len(SEARCH_KEYS))) * scales
    positions[0] = numeric_base
    if particles > 1:
        positions[1] = target
    # 3カテゴリを決定的に巡回させ、highlight_mode の3段階を最初から探索に含める。
    for index in range(2, particles):
        positions[index, class_index] = ((index - 2) % len(HIGHLIGHT_CATEGORIES)) + 0.5
    positions = np.clip(positions, lower, upper - 1e-6)
    velocities = rng.normal(0.0, 0.025, size=positions.shape) * scales
    evaluations: list[tuple[np.ndarray, dict[str, Any], tuple[float, ...], dict[str, Any]]] = []
    personal_best: list[tuple[np.ndarray, dict[str, Any], tuple[float, ...], dict[str, Any]] | None] = [None] * particles

    def evaluate_population(current: np.ndarray) -> None:
        current_evaluations = []
        for vector in current:
            adjustments = _adjustments_from_vector(vector, base, noise_reduction)
            metrics = evaluator.evaluate(adjustments, star_gate)
            objectives = _metric_objectives(
                metrics, style, vector, target, numeric_base, star_reference, _guard_penalty(style, metrics, base_metrics)
            )
            if not _safe_vector(vector):
                objectives = tuple(value + 3.0 for value in objectives)
            item = (np.asarray(vector, dtype=np.float64).copy(), metrics, objectives, adjustments)
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
            r1 = rng.random(len(SEARCH_KEYS))
            r2 = rng.random(len(SEARCH_KEYS))
            best = personal_best[index][0] if personal_best[index] is not None else current[index]
            velocities[index] = (
                0.56 * velocities[index]
                + 1.25 * r1 * (best - current[index])
                + 1.40 * r2 * (leader - current[index])
            )
        current[:] = np.clip(current + velocities, lower, upper - 1e-6)

    evaluate_population(positions)
    for _ in range(iterations):
        evaluate_population(positions)

    front = pareto_front([item[2] for item in evaluations])
    pool = [evaluations[index] for index in front] or evaluations
    safe_pool = [item for item in pool if _safe_vector(item[0])] or pool
    # 安全策に通る点を優先して選ぶ。Pareto front に無ければ全評価点へ広げ、
    # それでも無い場合だけ最良点を「除外」として理由付きで返す。
    passing = [item for item in safe_pool if _guard_failure(style, item[1], base_metrics) is None]
    if not passing:
        passing = [
            item
            for item in evaluations
            if _safe_vector(item[0]) and _guard_failure(style, item[1], base_metrics) is None
        ]
    if passing:
        _, chosen_metrics, _, chosen_adjustments = min(passing, key=lambda item: _utility(item[2], style))
        return chosen_adjustments, chosen_metrics, None
    _, chosen_metrics, _, chosen_adjustments = min(safe_pool, key=lambda item: _utility(item[2], style))
    return chosen_adjustments, chosen_metrics, _guard_failure(style, chosen_metrics, base_metrics)


def _resolve_source_paths(capture_id: str) -> tuple[Path, Path | None]:
    source_root = config.source_root().resolve()
    arw_path = (source_root / f"{capture_id}.ARW").resolve()
    if arw_path.parent != source_root or not arw_path.is_file():
        raise DevelopmentCandidateNotFound(f"元ARWが見つかりません: {capture_id}.ARW")
    jpg_path = (source_root / f"{capture_id}.JPG").resolve()
    if jpg_path.parent != source_root or not jpg_path.is_file():
        jpg_path = None
    return arw_path, jpg_path


def generate_development_candidates(
    capture_id: str,
    preset: str = DEFAULT_PRESET,
    *,
    seed: int | None = None,
    budget: int | None = None,
    decoder_factory: DecoderFactory | None = None,
) -> dict[str, Any]:
    """Generate up to five deterministic development candidates.

    ``capture_id`` and ``preset`` are required contract values; ``seed`` and
    ``budget`` are optional.  The ARW is half-size decoded (cached by
    highlight category / noise reduction / exposure grid); EXIF is read from the
    paired JPG when present.  ``decoder_factory`` lets tests inject a synthetic
    decoder in place of :func:`open_raw_half_decoder`.
    """

    capture_id, preset, seed, budget = _validate_request(capture_id, preset, seed, budget)
    arw_path, jpg_path = _resolve_source_paths(capture_id)
    exif = read_capture_exif(jpg_path) if jpg_path is not None else {}
    base = resolve_adjustments(preset)
    factory = decoder_factory or open_raw_half_decoder

    with factory(arw_path, dict(base)) as decode:
        cache = DecodeCache(decode)
        # 基準（natural）= プリセット値のまま同じ代理パイプラインで作った画像。
        baseline_evaluator = _Evaluator(cache, exif, base, star_gate=False)
        star = detect_star_scene(baseline_evaluator.image, exif)
        # star 系指標は基準側も候補と同じ関数・同じ条件で計算する（トートロジーの解消）。
        evaluator = _Evaluator(cache, exif, base, star_gate=bool(star["is_star_scene"]))
        base_metrics = evaluator.metrics
        styles = _candidate_style_ids(evaluator.image, exif, star)
        search_styles = [style for style in styles if style != "natural"]

        # 基準 natural には探索予算を使わず、他スタイルへ均等配分する。
        per_style_budget = max(4, budget // max(1, len(search_styles)))
        candidates: list[dict[str, Any]] = [
            {
                "id": "natural",
                "label": STYLE_LABELS["natural"],
                "description": STYLE_DESCRIPTIONS["natural"],
                "adjustments": dict(base),
                "metrics": base_metrics,
                "score": _score(base_metrics, "natural", base_metrics),
                "reasons": _reasons("natural", base_metrics, base_metrics, bool(star["is_star_scene"])),
            }
        ]
        rejected: list[dict[str, str]] = []
        used_adjustments: list[dict[str, Any]] = [dict(base)]
        star_reference = int(base_metrics["star_count"])
        for style in search_styles:
            adjustments, metrics, failure = _optimise_style(evaluator, style, star_reference, per_style_budget, seed)
            if failure is None and any(_normalised_distance(adjustments, previous) < 0.035 for previous in used_adjustments):
                # If two Pareto choices collapse to the same slider values, move
                # the style a deterministic step toward its own target.
                target = _style_target(base, style)
                vector = _vector_from_adjustments(adjustments) * 0.55 + target * 0.45
                moved = _adjustments_from_vector(vector, base, adjustments["noise_reduction"])
                # 採用する値（スナップ後）が探索時と同じ安全チェックを満たす場合だけ移動を採る。
                if _safe_vector(_vector_from_adjustments(moved)):
                    moved_metrics = evaluator.evaluate(moved, style == "stars")
                    if _guard_failure(style, moved_metrics, base_metrics) is None:
                        adjustments, metrics = moved, moved_metrics
            if failure is not None:
                rejected.append({"id": style, "reason": failure})
                continue
            used_adjustments.append(adjustments)
            candidates.append(
                {
                    "id": style,
                    "label": STYLE_LABELS[style],
                    "description": STYLE_DESCRIPTIONS[style],
                    "adjustments": adjustments,
                    "metrics": metrics,
                    "score": _score(metrics, style, base_metrics),
                    "reasons": _reasons(style, metrics, base_metrics, bool(star["is_star_scene"])),
                }
            )
        decode_count = cache.decode_count
        height, width = evaluator.image.shape[:2]

    # Stable order is part of the deterministic response contract.
    order = {style: index for index, style in enumerate(STYLE_IDS)}
    candidates.sort(key=lambda candidate: order[candidate["id"]])
    return {
        "capture_id": capture_id,
        "preset": preset,
        "seed": seed,
        "budget": budget,
        "proxy": {
            "max_edge": max(height, width),
            "width": int(width),
            "height": int(height),
            "source": "raw_half",
            "decode_edge": DECODE_EDGE,
            "decodes": int(decode_count),
        },
        "star_scene": star,
        "candidates": candidates[:5],
        "rejected": rejected,
    }


# Short aliases make the module pleasant to use from scripts while keeping the
# endpoint-facing name explicit.
generate_candidates = generate_development_candidates


__all__ = [
    "CAPTURE_ID_PATTERN",
    "CLIP_WORSEN_LIMIT",
    "DEFAULT_BUDGET",
    "DecodeCache",
    "DevelopmentCandidateError",
    "DevelopmentCandidateNotFound",
    "EXPOSURE_GRID",
    "HIGHLIGHT_REPRESENTATIVE",
    "MAX_BUDGET",
    "MIN_BUDGET",
    "MAX_STAR_BRIGHT_RATIO",
    "STYLE_IDS",
    "default_seed",
    "detect_star_scene",
    "generate_candidates",
    "generate_development_candidates",
    "highlight_category",
    "is_star_scene",
    "open_raw_half_decoder",
    "pareto_front",
    "render_proxy",
    "representative_highlight",
    "snap_exposure",
]
