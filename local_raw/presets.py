from __future__ import annotations

from typing import Any


PRESETS: dict[str, dict[str, Any]] = {
    "neutral_recovery": {
        "label": "階調回復",
        "exp_shift": 1.0,
        "auto_bright_thr": 0.01,
        "noise_reduction": "light",
        "contrast": 1.03,
        "color": 1.02,
        "sharpness": 1.04,
    },
    "landscape_clear": {
        "label": "風景クリア",
        "exp_shift": 1.0,
        "auto_bright_thr": 0.008,
        "noise_reduction": "light",
        "contrast": 1.10,
        "color": 1.08,
        "sharpness": 1.08,
    },
    "nightscape": {
        "label": "夜景ノイズ低減",
        "exp_shift": 1.15,
        "auto_bright_thr": 0.015,
        "noise_reduction": "full",
        "contrast": 1.07,
        "color": 1.04,
        "sharpness": 1.03,
    },
}
