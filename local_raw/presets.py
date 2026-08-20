"""プリセット定義と Adjustments の解決。

契約: docs/API_CONTRACT.md §2（Adjustments の型と範囲）・§5（プリセット11種）・§9（module 契約）
"""

from __future__ import annotations

from typing import Any


class RawDevelopmentError(RuntimeError):
    """現像リクエストが不正、または現像に失敗したことを表す。

    `local_raw.engine` から再エクスポートされる（サーバ側は engine 経由で import する）。
    最下層モジュールである本モジュールで定義することで engine との循環 import を避けている。
    """


DEFAULT_PRESET = "neutral_recovery"

NOISE_REDUCTION_CHOICES: tuple[str, ...] = ("off", "light", "full")

#: 数値 Adjustments の許容範囲（契約 §2）。範囲外の値はこの範囲へクランプする。
ADJUSTMENT_RANGES: dict[str, tuple[float, float]] = {
    "exposure": (-2.0, 2.0),
    "wb_temp": (-100.0, 100.0),
    "shadow_lift": (0.0, 100.0),
    "highlight_recovery": (0.0, 100.0),
    "brightness": (0.7, 1.4),
    "contrast": (0.8, 1.4),
    "saturation": (0.0, 1.5),
    "sharpness": (0.8, 1.6),
}

#: Adjustments の全キー（数値8種 + noise_reduction）。
ADJUSTMENT_KEYS: tuple[str, ...] = (*ADJUSTMENT_RANGES.keys(), "noise_reduction")


def _adjustments(
    exposure: float,
    wb_temp: float,
    shadow_lift: float,
    highlight_recovery: float,
    brightness: float,
    contrast: float,
    saturation: float,
    sharpness: float,
    noise_reduction: str,
) -> dict[str, Any]:
    return {
        "exposure": float(exposure),
        "wb_temp": float(wb_temp),
        "shadow_lift": float(shadow_lift),
        "highlight_recovery": float(highlight_recovery),
        "brightness": float(brightness),
        "contrast": float(contrast),
        "saturation": float(saturation),
        "sharpness": float(sharpness),
        "noise_reduction": noise_reduction,
    }


#: 契約 §5 の表と1対1対応。数値は Wave 3 で微調整可、name とキー構成は凍結。
PRESETS: dict[str, dict[str, Any]] = {
    "neutral_recovery": {
        "label": "階調回復",
        "description": "自然な明暗差を保つ標準仕上げ",
        "adjustments": _adjustments(0.0, 0, 15, 60, 1.0, 1.03, 1.02, 1.04, "light"),
    },
    "landscape_clear": {
        "label": "風景クリア",
        "description": "遠景の輪郭と色をくっきりさせる",
        "adjustments": _adjustments(0.0, 0, 10, 55, 1.0, 1.10, 1.08, 1.08, "light"),
    },
    "nightscape": {
        "label": "夜景ノイズ低減",
        "description": "高ISO夜景のノイズを抑え暗部を持ち上げる",
        "adjustments": _adjustments(0.2, -5, 25, 50, 1.05, 1.07, 1.04, 1.03, "full"),
    },
    "sunset_glow": {
        "label": "夕景・マジックアワー",
        "description": "夕焼けの暖色を強め空の階調を守る",
        "adjustments": _adjustments(-0.1, 25, 15, 80, 1.0, 1.08, 1.12, 1.05, "light"),
    },
    "indoor_warm": {
        "label": "室内・料理",
        "description": "室内照明の暖かさを残し料理を明るく見せる",
        "adjustments": _adjustments(0.3, 10, 30, 45, 1.05, 1.02, 1.05, 1.04, "light"),
    },
    "portrait_soft": {
        "label": "人物・柔らか",
        "description": "肌を滑らかに見せる柔らかいコントラスト",
        "adjustments": _adjustments(0.1, 5, 20, 55, 1.02, 0.96, 1.03, 0.95, "light"),
    },
    "monochrome_street": {
        "label": "モノクロ",
        "description": "彩度を落としコントラストで見せる白黒仕上げ",
        "adjustments": _adjustments(0.0, 0, 10, 60, 1.0, 1.15, 0.0, 1.10, "light"),
    },
    "highlight_rescue": {
        "label": "白飛び救済",
        "description": "飛びかけた空や光源の階調を最大限引き戻す",
        "adjustments": _adjustments(-0.5, 0, 40, 100, 1.05, 1.0, 1.0, 1.02, "light"),
    },
    "shadow_rescue": {
        "label": "黒つぶれ救済",
        "description": "つぶれた暗部を大きく持ち上げ、増えるノイズを抑える",
        "adjustments": _adjustments(0.3, 0, 80, 65, 1.05, 0.98, 1.06, 1.02, "full"),
    },
    "backlit_rescue": {
        "label": "逆光",
        "description": "明るい背景の階調を残しつつ、影になった被写体を持ち上げる",
        "adjustments": _adjustments(0.2, 5, 60, 90, 1.04, 0.96, 1.08, 1.05, "light"),
    },
    "vivid_travel": {
        "label": "ビビッド",
        "description": "旅の記録向けに色を鮮やかに強調する",
        "adjustments": _adjustments(0.0, 5, 10, 55, 1.0, 1.12, 1.18, 1.10, "light"),
    },
}


def preset_infos() -> list[dict[str, Any]]:
    """`/api/health` の `presets` に入る `PresetInfo[]`（契約 §1）。"""
    return [
        {
            "name": name,
            "label": preset["label"],
            "description": preset["description"],
            "adjustments": dict(preset["adjustments"]),
        }
        for name, preset in PRESETS.items()
    ]


def _coerce_number(key: str, value: Any) -> float:
    # bool は int のサブクラスなので明示的に弾く（True が 1.0 に化けるのを防ぐ）。
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RawDevelopmentError(f"{key} には数値を指定してください。")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise RawDevelopmentError(f"{key} には有限の数値を指定してください。")
    minimum, maximum = ADJUSTMENT_RANGES[key]
    return min(maximum, max(minimum, number))


def resolve_adjustments(
    preset_name: str,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """プリセット既定値に overrides をマージし、Adjustments 全キーを返す（契約 §9）。

    - 未知のプリセット名 / 未知キー / 不正な型 → `RawDevelopmentError`
    - 数値の範囲外 → 契約 §2 の範囲へクランプ
    - 数値は必ず `float` に正規化する（params_hash を int/float 差で揺らさないため）
    """
    if preset_name not in PRESETS:
        raise RawDevelopmentError(f"未対応のプリセットです: {preset_name}")

    resolved = dict(PRESETS[preset_name]["adjustments"])

    if overrides is None:
        return resolved
    if not isinstance(overrides, dict):
        raise RawDevelopmentError("adjustments はオブジェクトで指定してください。")

    unknown = sorted(set(overrides) - set(ADJUSTMENT_KEYS))
    if unknown:
        raise RawDevelopmentError(f"未対応の調整項目です: {', '.join(unknown)}")

    for key, value in overrides.items():
        if key == "noise_reduction":
            if value not in NOISE_REDUCTION_CHOICES:
                raise RawDevelopmentError(
                    "noise_reduction は off / light / full のいずれかで指定してください。"
                )
            resolved[key] = value
        else:
            resolved[key] = _coerce_number(key, value)

    return resolved
