from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import rawpy
from PIL import Image, ImageEnhance

from .presets import PRESETS


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = PROJECT_ROOT / "sample" / "100MSDCF"
OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "developed"
CAPTURE_ID_PATTERN = re.compile(r"^DSC\d{5}$")

__all__ = [
    "CAPTURE_ID_PATTERN",
    "OUTPUT_ROOT",
    "PRESETS",
    "PROJECT_ROOT",
    "SOURCE_ROOT",
    "RawDevelopmentError",
    "develop_raw",
]


class RawDevelopmentError(RuntimeError):
    pass


def _validate_capture_id(capture_id: str) -> str:
    normalized = capture_id.upper().removesuffix(".ARW").removesuffix(".JPG")
    if not CAPTURE_ID_PATTERN.fullmatch(normalized):
        raise RawDevelopmentError("無効な撮影IDです。DSC00000形式で指定してください。")
    return normalized


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _save_recipe(recipe_path: Path, recipe: dict[str, Any]) -> None:
    temporary_path = recipe_path.with_suffix(".tmp.json")
    temporary_path.write_text(json.dumps(recipe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary_path, recipe_path)


def develop_raw(
    capture_id: str,
    preset_name: str = "neutral_recovery",
    *,
    force: bool = False,
    max_edge: int = 6000,
) -> dict[str, Any]:
    capture_id = _validate_capture_id(capture_id)
    if preset_name not in PRESETS:
        raise RawDevelopmentError(f"未対応のプリセットです: {preset_name}")
    if not 1200 <= max_edge <= 6048:
        raise RawDevelopmentError("最大辺は1200〜6048pxで指定してください。")

    source_path = (SOURCE_ROOT / f"{capture_id}.ARW").resolve()
    if source_path.parent != SOURCE_ROOT.resolve() or not source_path.is_file():
        raise RawDevelopmentError(f"ARWが見つかりません: {capture_id}.ARW")

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_ROOT / f"{capture_id}-{preset_name}.jpg"
    recipe_path = OUTPUT_ROOT / f"{capture_id}-{preset_name}.json"
    preset = PRESETS[preset_name]

    if output_path.is_file() and recipe_path.is_file() and not force:
        recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
        return {
            "capture_id": capture_id,
            "preset": preset_name,
            "preset_label": preset["label"],
            "filename": output_path.name,
            "recipe_filename": recipe_path.name,
            "width": recipe["output"]["width"],
            "height": recipe["output"]["height"],
            "elapsed_seconds": 0.0,
            "cached": True,
        }

    started_at = time.monotonic()
    fbdd_mode = (
        rawpy.FBDDNoiseReductionMode.Full
        if preset["noise_reduction"] == "full"
        else rawpy.FBDDNoiseReductionMode.Light
    )

    try:
        with rawpy.imread(str(source_path)) as raw:
            rgb = raw.postprocess(
                use_camera_wb=True,
                use_auto_wb=False,
                output_color=rawpy.ColorSpace.sRGB,
                output_bps=8,
                demosaic_algorithm=rawpy.DemosaicAlgorithm.AHD,
                highlight_mode=rawpy.HighlightMode.Blend,
                exp_shift=preset["exp_shift"],
                exp_preserve_highlights=0.65,
                no_auto_bright=False,
                auto_bright_thr=preset["auto_bright_thr"],
                fbdd_noise_reduction=fbdd_mode,
                median_filter_passes=1 if preset["noise_reduction"] == "full" else 0,
            )
    except (rawpy.LibRawError, OSError) as error:
        raise RawDevelopmentError(f"RAWデコードに失敗しました: {error}") from error

    image = Image.fromarray(np.asarray(rgb, dtype=np.uint8), mode="RGB")
    if max(image.size) > max_edge:
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    image = ImageEnhance.Contrast(image).enhance(preset["contrast"])
    image = ImageEnhance.Color(image).enhance(preset["color"])
    image = ImageEnhance.Sharpness(image).enhance(preset["sharpness"])

    temporary_output = output_path.with_suffix(".tmp.jpg")
    image.save(temporary_output, "JPEG", quality=92, subsampling=0, optimize=True, progressive=True)
    os.replace(temporary_output, output_path)
    elapsed_seconds = round(time.monotonic() - started_at, 2)

    recipe = {
        "recipe_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "source": {
            "path": str(source_path.relative_to(PROJECT_ROOT)),
            "sha256": _sha256(source_path),
            "size_bytes": source_path.stat().st_size,
        },
        "engine": {
            "name": "rawpy/LibRaw",
            "rawpy_version": rawpy.__version__,
            "pillow_version": Image.__version__,
        },
        "preset": {"name": preset_name, **preset},
        "output": {
            "path": str(output_path.relative_to(PROJECT_ROOT)),
            "width": image.width,
            "height": image.height,
            "format": "JPEG",
            "quality": 92,
            "metadata_copied": False,
        },
        "elapsed_seconds": elapsed_seconds,
    }
    _save_recipe(recipe_path, recipe)

    return {
        "capture_id": capture_id,
        "preset": preset_name,
        "preset_label": preset["label"],
        "filename": output_path.name,
        "recipe_filename": recipe_path.name,
        "width": image.width,
        "height": image.height,
        "elapsed_seconds": elapsed_seconds,
        "cached": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Sony ARWを非破壊でローカル現像します。")
    parser.add_argument("capture_id", help="例: DSC02943")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="neutral_recovery")
    parser.add_argument("--force", action="store_true", help="既存出力を再生成します。")
    parser.add_argument("--max-edge", type=int, default=6000)
    args = parser.parse_args()
    print(json.dumps(develop_raw(args.capture_id, args.preset, force=args.force, max_edge=args.max_edge), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
