"""候補評価の純関数（旧 ``local_raw/development_candidates.py`` の評価用の写し。#57 / #90）。

候補の星判定・指標・安全策・スコア・理由だけを、値もロジックも変えずに写した。RAW デコード・
探索・アプリ設定には触れない。写しの一致は ``tests/test_evaluation_support.py`` が凍結 JSON
（``tests/fixtures/baseline/metrics.json`` / ``tests/fixtures/pso-selection.json``）で検証する。
旧 Python の閾値を Core Image の合否基準として使うものではない（docs/development/evaluation-paths.md）。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np
from PIL import Image, ImageFilter

from .sources import ev100

try:  # OpenCV は評価環境の依存。無い環境でも同じ結果の遅いフォールバックを使う。
    import cv2
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]


#: 評価に使う代理画像の長辺（engine の shadow_lift → resize 後）。
MAX_PROXY_EDGE = 640
#: 星空判定（#46）。主信号は EXIF（長秒かつ低EV100）、画像側は「点光源が多数ある」ことの確認。
STAR_MIN_EXPOSURE_SEC = 2.0
#: これ以下が「星空の暗さ」。星空は ≤ −6.4、最も暗い非星空（長秒ブラケット）は +0.3。
#: 薄明・月明かりの星空も拾えるよう余裕を持たせつつ、ブラケットとは 2EV 以上離す。
STAR_MAX_EV100 = -2.0
#: 代理画像（長辺 ≤ 640px）上の局所ピーク（星候補）の最小密度（点数/画素数）。
#: 星空10枚は 0.0026〜0.013、長秒ブラケットの風景は最大 0.0003。中間の対数中点付近。
STAR_MIN_POINT_DENSITY = 0.0008
#: EXIF が無い（露光時間かEV100が分からない）とき用の厳しい条件。暗い背景の割合と
#: 星密度の両方を求める（旧来は暗背景 0.72 と点 3 個で通っていたので誤判定しやすかった）。
STAR_NO_EXIF_MIN_DARK_FRACTION = 0.72
STAR_NO_EXIF_MIN_POINT_DENSITY = 0.002
MAX_STAR_BRIGHT_RATIO = 0.005
#: 大面積の光源（月・街灯・ライトアップ・空の白飛び）の上限。輝度 0.85 以上の画素の割合。
#: 星空10枚は最大 0.0064（天の川の明部）。天の川は広がっても 0.85 には届かない。
STAR_MAX_BIG_LIGHT_RATIO = 0.02
STAR_BIG_LIGHT_LEVEL = 0.85
#: 局所コントラスト検出: gray − ガウシアンぼかし(σ)。差が STAR_PEAK_LOW 以上の連結領域のうち、
#: 面積が上限以下で最大差が STAR_PEAK_HIGH 以上のものを「点」として数える。
STAR_LOCAL_SIGMA = 3.0
STAR_PEAK_LOW = 0.04
STAR_PEAK_HIGH = 0.08
STAR_POINT_MAX_AREA_RATIO = 0.00011
STAR_POINT_MIN_AREA_CAP = 4

#: highlight_recovery のカテゴリ（engine `_postprocess_params` の3段階と一致させる）。
HIGHLIGHT_CATEGORIES: tuple[str, ...] = ("clip", "blend", "reconstruct")
HIGHLIGHT_REPRESENTATIVE: dict[str, float] = {"clip": 0.0, "blend": 50.0, "reconstruct": 85.0}

#: 安全策のしきい値（基準 natural との差、絶対値）。
CLIP_WORSEN_LIMIT = 0.10
#: foreground: 基準の暗部（輝度下位25%）の平均輝度がこれ以上明るくなること。
SHADOW_GAIN_MIN = 0.005
SHADOW_QUANTILE = 0.25
STYLE_IDS: tuple[str, ...] = ("natural", "stars", "foreground", "low_noise", "social")


def _normalise_rgb(image: Image.Image | np.ndarray) -> np.ndarray:
    if isinstance(image, Image.Image):
        image = image.convert("RGB")
        array = np.asarray(image, dtype=np.float32) / 255.0
    else:
        array = np.asarray(image, dtype=np.float32)
        if array.ndim == 2:
            array = np.repeat(array[..., None], 3, axis=2)
        if array.ndim != 3 or array.shape[2] < 3:
            raise ValueError("代理画像はRGB画像で指定してください。")
        array = array[..., :3]
        if np.nanmax(array, initial=0.0) > 1.0:
            array = array / 255.0
    if array.ndim != 3 or array.shape[2] != 3:
        raise ValueError("代理画像はRGB画像で指定してください。")
    if not np.isfinite(array).all():
        raise ValueError("代理画像に不正な画素値があります。")
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


def _point_light_count(gray: np.ndarray) -> int:
    """局所コントラストで「小さな点光源」の数を数える（星の候補）。

    gray − ガウシアンぼかしが STAR_PEAK_LOW 以上の連結領域のうち、面積が上限
    （画素数 × STAR_POINT_MAX_AREA_RATIO）以下で領域内の最大差が STAR_PEAK_HIGH 以上のもの。
    絶対輝度に依らないので、背景が中間調の天の川でも星を数えられる。
    """

    height, width = gray.shape
    if cv2 is not None:
        blurred = cv2.GaussianBlur(gray, (0, 0), STAR_LOCAL_SIGMA)
    else:
        blurred = _blur(np.repeat(gray[:, :, None], 3, axis=2), STAR_LOCAL_SIGMA)[:, :, 0]
    diff = gray - blurred
    max_area = max(STAR_POINT_MIN_AREA_CAP, int(round(height * width * STAR_POINT_MAX_AREA_RATIO)))
    count = 0
    for x, y, component_width, component_height, area in _connected_bright_components(diff >= STAR_PEAK_LOW):
        if area > max_area:
            continue
        if float(diff[y : y + component_height, x : x + component_width].max()) >= STAR_PEAK_HIGH:
            count += 1
    return count


def _exif_night_state(exif: dict[str, Any]) -> str:
    """EXIF から "night"（長秒かつ暗い）/ "daylight"（そうではないと確定）/ "unknown" を返す。"""

    try:
        exposure_time = float(exif.get("exposure_time"))
    except (TypeError, ValueError):
        return "unknown"
    if not math.isfinite(exposure_time) or exposure_time <= 0:
        return "unknown"
    if exposure_time < STAR_MIN_EXPOSURE_SEC:
        return "daylight"
    scene_ev = ev100(exif)
    if scene_ev is None:
        return "unknown"
    return "night" if scene_ev <= STAR_MAX_EV100 else "daylight"


def _star_measurements(
    array: np.ndarray,
    exif: dict[str, Any] | None = None,
    *,
    evaluate_gate: bool = True,
) -> dict[str, Any]:
    """星空判定と星の指標（数・ハロ・星色の白飛び）を返す。

    判定（``is_star_scene``）は EXIF を主信号にする。

    * EXIF で「露光 >= STAR_MIN_EXPOSURE_SEC かつ EV100 <= STAR_MAX_EV100」（night）:
      点光源の密度が STAR_MIN_POINT_DENSITY 以上で、大面積の光源
      （輝度 STAR_BIG_LIGHT_LEVEL 以上の画素が STAR_MAX_BIG_LIGHT_RATIO 超）が無ければ星空。
      背景の暗さは問わない（天の川が画面全体に広がる星空も判定できる）。
    * EXIF で短秒・明るいと分かる（daylight）: 画像に依らず星空ではない（夜景・ライトアップ）。
    * EXIF が無い・露光時間かEV100が不明（unknown）: 安全側に厳しい画像条件だけで判定する。
      暗い背景 >= STAR_NO_EXIF_MIN_DARK_FRACTION、点密度 >= STAR_NO_EXIF_MIN_POINT_DENSITY、
      明部の合計面積 <= MAX_STAR_BRIGHT_RATIO をすべて満たすとき。

    ``evaluate_gate=False`` は候補評価用で、判定を行わず星の指標だけを測る。
    """

    rgb = _normalise_rgb(array)
    gray = np.dot(rgb, np.array([0.2126, 0.7152, 0.0722], dtype=np.float32))
    dark_fraction = float(np.mean(gray < 0.20))
    # 星の指標用の点（明るい小連結成分）。判定とは独立で、既存の最適化目標の基準になる。
    # A high percentile avoids making a dim city skyline a collection of stars.
    threshold = max(0.58, float(np.percentile(gray, 99.35)))
    bright = gray >= threshold
    bright_ratio = float(np.mean(bright))
    components = _connected_bright_components(bright)
    height, width = gray.shape
    max_point_area = max(12, int(height * width * 0.00045))
    points = [component for component in components if 1 <= component[4] <= max_point_area]
    point_count = len(points)

    exif_state = _exif_night_state(exif or {})
    # 点光源密度は実際に測った経路でだけ値を持つ（測っていなければ None）。
    point_density: float | None = None
    is_star_scene = False
    if evaluate_gate and exif_state != "daylight":
        big_light_ratio = float(np.mean(gray >= STAR_BIG_LIGHT_LEVEL))
        if exif_state == "night":
            if big_light_ratio <= STAR_MAX_BIG_LIGHT_RATIO:
                point_density = _point_light_count(gray) / float(height * width)
                is_star_scene = point_density >= STAR_MIN_POINT_DENSITY
        elif dark_fraction >= STAR_NO_EXIF_MIN_DARK_FRACTION and bright_ratio <= MAX_STAR_BRIGHT_RATIO:
            point_density = _point_light_count(gray) / float(height * width)
            is_star_scene = point_density >= STAR_NO_EXIF_MIN_POINT_DENSITY

    halo = 0.0
    # 星の色の指標なので、検出した星（点）の領域だけで測る（画像全体は含めない）。
    color_clip = 0.0
    # A daytime/noisy image can contain thousands of tiny bright components, so
    # the ring statistics are only measured for star scenes (or when the caller
    # already decided to optimise for stars).
    if (is_star_scene or not evaluate_gate) and points:
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
        "star_point_density": None if point_density is None else round(point_density, 6),
        "dark_background_ratio": round(dark_fraction, 6),
        "bright_point_ratio": round(bright_ratio, 6),
        "exif_night": exif_state == "night",
        "exif_state": exif_state,
        "star_halo": round(float(np.clip(halo, 0.0, 1.0)), 6),
        "star_color_saturation": round(float(np.clip(color_clip, 0.0, 1.0)), 6),
    }


def detect_star_scene(
    image: Image.Image | np.ndarray,
    exif: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return deterministic star-scene features from a proxy and EXIF hints."""

    return _star_measurements(_normalise_rgb(image), exif)


def _blur(array: np.ndarray, radius: float = 1.0) -> np.ndarray:
    if cv2 is not None:
        sigma = max(0.1, float(radius))
        return cv2.GaussianBlur(array, (0, 0), sigmaX=sigma, sigmaY=sigma)
    # PIL fallback is slower but deterministic and only affects tiny proxies.
    image = Image.fromarray(np.uint8(np.clip(array, 0.0, 1.0) * 255.0), mode="RGB")
    image = image.filter(ImageFilter.GaussianBlur(radius=max(0.1, float(radius))))
    return np.asarray(image, dtype=np.float32) / 255.0


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
    star = _star_measurements(transformed, exif, evaluate_gate=False) if star_gate else {
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
