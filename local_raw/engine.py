"""Sony ARW の非破壊ローカル現像エンジン。

契約: docs/API_CONTRACT.md §2（Adjustments）・§4（命名）・§9（module 契約）

処理順序は契約 §2 で固定:
    rawpy postprocess → lensfun 補正 → shadow_lift LUT → resize
    → Brightness → Contrast → Color → Sharpness → save(exif, icc)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import rawpy
from PIL import Image, ImageEnhance

from . import config
from .exif import SRGB_ICC_BYTES, build_inherited_exif, read_capture_exif
from .lens import apply_lens_correction, probe_lens
from .presets import (
    DEFAULT_PRESET,
    PRESETS,
    RawDevelopmentError,
    preset_infos,
    resolve_adjustments,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 現像元フォルダはユーザーが切り替えられるため定数にしない（契約 §9）。
# 参照は `config.source_root()` で**呼び出し時に**解決する。
OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "developed"
PREVIEW_ROOT = PROJECT_ROOT / "outputs" / "preview"
CAPTURE_ID_PATTERN = re.compile(r"^DSC\d{5}$")

#: レシピ形式 / 現像パイプラインの版。params_hash に含めるので、
#: ここを上げると既存キャッシュは自動的に無効になる。
#:
#: ENGINE_VERSION 3: レンズ補正のビネット処理を修正したので出力が変わる
#: （lensfunpy の pixel_format を実バッファの dtype に合わせた。local_raw/lens.py 参照）。
#: 修正前は補正が効くレンズで画像左端およそ25%が白飛びの帯になっており、その壊れた
#: JPEG がキャッシュに残っている。版を上げて既存の現像キャッシュを無効化する。
#:
#: ENGINE_VERSION 4: レンズ歪曲補正に autoscale（ModifyFlags.SCALE）を追加し黒縁を除去。
#: 出力が変わるため版を上げ、黒縁付きの現像・ブラケット合成キャッシュを無効化する
#: （ENGINE_VERSION は両経路のキャッシュキーに入っている）。
ENGINE_VERSION = 4
RECIPE_VERSION = 2

DEFAULT_MAX_EDGE = 6000
PREVIEW_MAX_EDGE = 1600
MIN_MAX_EDGE = 1200
MAX_MAX_EDGE = 6048

JPEG_QUALITY = 92

#: LibRaw の自動明るさ補正しきい値。
#:
#: 自動明るさ補正は **有効のまま**にしている（露出が picture ごとに適応的に決まり、
#: カメラ内 JPEG に近い明るさになるため）。ただし副作用として `exp_shift` による
#: 露出変更はヒストグラム正規化でほぼ打ち消される。実測（DSC02943, half_size）:
#:   no_auto_bright=False → ±1EV での平均輝度差は 0.18（実質スライダーが死ぬ）
#:   no_auto_bright=True  → 同 15.65 だが平均輝度 12.7（カメラJPEG 52.4 の約1/4で暗すぎる）
#: そこで `exposure` は契約どおり `exp_shift` に渡しつつ、自動明るさ補正の**後段**に
#: 効く `bright` にも同じ倍率を渡している（実測スプレッド 42.7）。§2 の実装マッピングに
#: 対する追加であり、削除・調整は Wave 3 の課題。
AUTO_BRIGHT_THR = 0.01

#: shadow_lift LUT の強さ係数（契約 §2 の式の定数部）。Wave 3 で目視調整可。
SHADOW_LIFT_GAIN = 0.5 * 255.0 * 4.0

__all__ = [
    "CAPTURE_ID_PATTERN",
    "DEFAULT_PRESET",
    "ENGINE_VERSION",
    "OUTPUT_ROOT",
    "PREVIEW_ROOT",
    "PRESETS",
    "PROJECT_ROOT",
    "RawDevelopmentError",
    "develop_raw",
    "preset_infos",
    "resolve_adjustments",
]


def _validate_capture_id(capture_id: str) -> str:
    normalized = capture_id.upper().removesuffix(".ARW").removesuffix(".JPG")
    if not CAPTURE_ID_PATTERN.fullmatch(normalized):
        raise RawDevelopmentError("無効な撮影IDです。DSC00000形式で指定してください。")
    return normalized


def _relative_path(path: Path) -> str:
    """プロジェクト相対の文字列パス。相対化できない場合は絶対パスのまま返す。

    現像元フォルダはユーザーが任意の場所（`~/Pictures`・外部ボリューム等）を選べるので、
    `relative_to()` を素で使うと `ValueError` → HTTP 400 になり、プロジェクト外フォルダの
    現像が全滅する。`export._relative_path()` と同じ流儀。
    """
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _temp_path(path: Path, extension: str) -> Path:
    """`path` と同じ場所に、呼び出しごとに一意な一時ファイルパスを作る。

    同一 params_hash の同時リクエスト（ThreadingHTTPServer への二重クリック等）でも
    `pid` と `uuid4()` の組み合わせで一時ファイル名が衝突しないようにする。CPython は
    短命スレッドの終了後に `threading.get_ident()` の値を再利用し得るため、これだけに
    依存した命名は同時実行環境で衝突の余地がある。呼び出しごとに新しい UUID を振るので
    別スレッドから同時に呼ばれても、同一スレッド内で連続して呼ばれても全て別のパスになる。
    `os.replace` によるアトミック置換自体は変わらず、衝突が起きるのは書き込み中の一時
    ファイル名だけだったのを塞ぐ。元の stem と一時ファイル用拡張子は名前に残す
    （デバッグ時に何の一時ファイルか判別できるようにするため）。
    """
    return path.with_suffix(f".{os.getpid()}-{uuid.uuid4().hex}.{extension}")


def _save_recipe(recipe_path: Path, recipe: dict[str, Any]) -> None:
    """`recipe` を JSON として `recipe_path` にアトミック保存する。

    一時ファイルは成功・失敗いずれの経路でも残さない（`os.replace` 成功後は一時ファイルが
    存在しないため、`unlink(missing_ok=True)` は安全）。`_save_developed_jpeg` と同じ流儀。

    `_temp_path` が `threading.get_ident()` から `uuid4()` に変わったことで、名前が二度と
    衝突しなくなった代わりに、**失敗して残った一時ファイルが後続の書き込みで上書きされる
    こともなくなった**。掃除しないと `outputs/` に永久に溜まる。
    """
    temporary_path = _temp_path(recipe_path, "tmp.json")
    try:
        temporary_path.write_text(
            json.dumps(recipe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary_path, recipe_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _params_hash(
    adjustments: dict[str, Any],
    preset_name: str,
    max_edge: int,
    preview: bool,
    lens_applied: bool,
    source_root: str,
) -> str:
    """キャッシュキー（契約 §1）。

    出力に影響する全要素の canonical JSON から sha1 先頭10文字を取る純粋関数。
    数値は `resolve_adjustments` が float に正規化済みなので int/float 差では揺れない。

    `source_root`（**解決済み**の現像元フォルダの絶対パス文字列）を含めるのは、
    `capture_id` がカメラの連番でカードをまたぐと衝突するため。これが無いと、
    別フォルダの同名 ID を同じプリセットで現像したときに
    **他人の写真の現像結果が `cached: true` で返る**。
    """
    payload = {
        "adjustments": adjustments,
        "preset": preset_name,
        "max_edge": int(max_edge),
        "preview": bool(preview),
        "lens_applied": bool(lens_applied),
        "source_root": str(source_root),
        "versions": {
            "engine": ENGINE_VERSION,
            "rawpy": rawpy.__version__,
            "pillow": Image.__version__,
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(canonical.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]


def _shadow_lift_table(lift: float) -> list[int] | None:
    """暗部持ち上げ用の256エントリLUT（契約 §2 の式）。

    `y = x + (lift/100)·0.5·255·(x/255)·(1−x/255)²·4`
    ピーク位置は x/255 = 1/3 付近で、ハイライトにはほぼ触れない。
    """
    if lift <= 0:
        return None
    strength = lift / 100.0
    table: list[int] = []
    for value in range(256):
        normalized = value / 255.0
        lifted = value + strength * SHADOW_LIFT_GAIN * normalized * (1.0 - normalized) ** 2
        table.append(int(round(min(255.0, max(0.0, lifted)))))
    return table


def _postprocess_params(
    raw: Any,
    adjustments: dict[str, Any],
    preview: bool,
) -> dict[str, Any]:
    exposure = float(adjustments["exposure"])
    exposure_gain = 2.0**exposure
    highlight_recovery = float(adjustments["highlight_recovery"])
    noise_reduction = adjustments["noise_reduction"]

    if noise_reduction == "full":
        fbdd_mode = rawpy.FBDDNoiseReductionMode.Full
        median_passes = 1
    elif noise_reduction == "light":
        fbdd_mode = rawpy.FBDDNoiseReductionMode.Light
        median_passes = 0
    else:
        fbdd_mode = rawpy.FBDDNoiseReductionMode.Off
        median_passes = 0

    params: dict[str, Any] = {
        "output_color": rawpy.ColorSpace.sRGB,
        "output_bps": 8,
        "demosaic_algorithm": rawpy.DemosaicAlgorithm.AHD,
        "half_size": preview,
        "no_auto_bright": False,
        "auto_bright_thr": AUTO_BRIGHT_THR,
        # 契約 §2 の実装マッピング。auto bright に打ち消されるため bright も併用する
        # （AUTO_BRIGHT_THR のコメント参照）。
        "exp_shift": min(8.0, max(0.25, exposure_gain)),
        "bright": exposure_gain,
        "exp_preserve_highlights": 0.4 + 0.006 * highlight_recovery,
        "highlight_mode": (
            rawpy.HighlightMode.ReconstructDefault
            if highlight_recovery >= 75
            else rawpy.HighlightMode.Blend
        ),
        "fbdd_noise_reduction": fbdd_mode,
        "median_filter_passes": median_passes,
        "use_auto_wb": False,
    }

    wb_temp = float(adjustments["wb_temp"])
    if wb_temp == 0:
        params["use_camera_wb"] = True
    else:
        # camera_whitebalance は [R, G1, B, G2]。R/B のみスケールする。
        camera_wb = [float(gain) for gain in raw.camera_whitebalance]
        camera_wb[0] *= 1.0 + 0.003 * wb_temp
        camera_wb[2] *= 1.0 - 0.003 * wb_temp
        params["use_camera_wb"] = False
        params["user_wb"] = camera_wb

    return params


def _apply_tone(image: Image.Image, adjustments: dict[str, Any]) -> Image.Image:
    """resize 後のトーン調整（契約 §2 の順序: Brightness → Contrast → Color → Sharpness）。"""
    image = ImageEnhance.Brightness(image).enhance(float(adjustments["brightness"]))
    image = ImageEnhance.Contrast(image).enhance(float(adjustments["contrast"]))

    saturation = float(adjustments["saturation"])
    if saturation == 0:
        image = image.convert("L").convert("RGB")
    else:
        image = ImageEnhance.Color(image).enhance(saturation)

    return ImageEnhance.Sharpness(image).enhance(float(adjustments["sharpness"]))


def _resolve_save_kwargs(source_jpg: Path, width: int, height: int) -> tuple[dict[str, Any], bool]:
    """JPEG保存オプションを組み立てる。ペアJPGがあれば EXIF 継承を試みる。

    `build_inherited_exif` の失敗（`exif.tobytes()` が投げる `struct.error` を含む、
    あらゆる例外）は EXIF 無し保存へのフォールバックとして扱う。契約上のフォールバック方針
    （EXIF 無しでも保存は続行する）自体は変えない。
    """
    save_kwargs: dict[str, Any] = {
        "quality": JPEG_QUALITY,
        "subsampling": 0,
        "optimize": True,
        "progressive": True,
        "icc_profile": SRGB_ICC_BYTES,
    }
    exif_inherited = False
    if source_jpg.is_file():
        try:
            save_kwargs["exif"] = build_inherited_exif(source_jpg, width, height)
            exif_inherited = True
        except Exception:
            exif_inherited = False
    return save_kwargs, exif_inherited


def _save_developed_jpeg(
    image: Image.Image,
    output_path: Path,
    save_kwargs: dict[str, Any],
    exif_inherited: bool,
) -> bool:
    """`image` を JPEG として `output_path` にアトミック保存する。

    `exif` キー付きの保存が失敗した場合（`exif.tobytes()` の `struct.error` や、
    Pillow が `image.save()` 内で検出する EXIF サイズ超過の `ValueError` など）は、
    `exif` キーを外して1回だけ再保存する。それでも失敗したら `RawDevelopmentError` に
    変換して raise する。一時ファイルは成功・失敗いずれの経路でも残さない
    （`os.replace` 成功後は一時ファイルが存在しないため、`unlink(missing_ok=True)` は安全）。

    戻り値: 最終的に exif 付きで保存できたかどうか。
    """
    temporary_output = _temp_path(output_path, "tmp.jpg")
    try:
        try:
            image.save(temporary_output, "JPEG", **save_kwargs)
        except Exception as error:
            if not exif_inherited:
                raise RawDevelopmentError(f"JPEG保存に失敗しました: {error}") from error
            save_kwargs.pop("exif", None)
            exif_inherited = False
            try:
                image.save(temporary_output, "JPEG", **save_kwargs)
            except Exception as retry_error:
                raise RawDevelopmentError(f"JPEG保存に失敗しました: {retry_error}") from retry_error
        os.replace(temporary_output, output_path)
    finally:
        temporary_output.unlink(missing_ok=True)
    return exif_inherited


def _cached_result(
    recipe: dict[str, Any],
    capture_id: str,
    preset_name: str,
    preset_label: str,
    output_path: Path,
    recipe_path: Path,
    params_hash: str,
    adjustments: dict[str, Any],
    preview: bool,
) -> dict[str, Any]:
    output = recipe.get("output", {})
    return {
        "capture_id": capture_id,
        "preset": preset_name,
        "preset_label": preset_label,
        "filename": output_path.name,
        "recipe_filename": recipe_path.name,
        "params_hash": params_hash,
        "adjustments": recipe.get("adjustments", adjustments),
        "width": output.get("width", 0),
        "height": output.get("height", 0),
        "elapsed_seconds": 0.0,
        "cached": True,
        "preview": preview,
        "lens_correction": recipe.get("lens_correction", {"applied": False, "reason": "unknown"}),
        "exif_inherited": bool(recipe.get("exif_inherited", False)),
    }


def develop_raw(
    capture_id: str,
    preset_name: str = DEFAULT_PRESET,
    *,
    adjustments: dict[str, Any] | None = None,
    preview: bool = False,
    force: bool = False,
    max_edge: int = DEFAULT_MAX_EDGE,
) -> dict[str, Any]:
    """ARW を現像し、契約 `DevelopedResult` から `output_url` を除いた dict を返す。

    `output_url` はサーバ側（Wave 1-B）が `preview` の値に応じて付与する。
    """
    capture_id = _validate_capture_id(capture_id)
    if preset_name not in PRESETS:
        raise RawDevelopmentError(f"未対応のプリセットです: {preset_name}")
    if not MIN_MAX_EDGE <= max_edge <= MAX_MAX_EDGE:
        raise RawDevelopmentError("最大辺は1200〜6048pxで指定してください。")

    # §9 が既定値 6000 で凍結されているため、preview で max_edge 未指定のときだけ
    # 1600 に読み替える（明示指定された値は preview でも尊重する）。
    effective_max_edge = (
        PREVIEW_MAX_EDGE if preview and max_edge == DEFAULT_MAX_EDGE else max_edge
    )

    resolved = resolve_adjustments(preset_name, adjustments)
    preset_label = PRESETS[preset_name]["label"]

    # 現像元フォルダは1リクエストにつき1回だけ解決し、パス組み立て・トラバーサル検査・
    # params_hash の全てで同じ値を使う（途中で切り替わってもリクエスト内では一貫させる）。
    source_root = config.source_root().resolve()
    source_path = (source_root / f"{capture_id}.ARW").resolve()
    if source_path.parent != source_root or not source_path.is_file():
        raise RawDevelopmentError(f"ARWが見つかりません: {capture_id}.ARW")

    source_jpg = source_root / f"{capture_id}.JPG"
    capture_exif = read_capture_exif(source_jpg) if source_jpg.is_file() else {}

    # レンズ解決は RAW デコード（10〜20秒）の前に済ませる。
    # params_hash がレンズ補正の適用有無を含むため、キャッシュ判定より前に必要。
    lens_resolution = probe_lens(capture_exif)

    params_hash = _params_hash(
        resolved,
        preset_name,
        effective_max_edge,
        preview,
        lens_resolution.applicable,
        str(source_root),
    )

    destination_root = PREVIEW_ROOT if preview else OUTPUT_ROOT
    destination_root.mkdir(parents=True, exist_ok=True)
    stem = f"{capture_id}-{preset_name}-{params_hash}"
    output_path = destination_root / f"{stem}.jpg"
    recipe_path = destination_root / f"{stem}.json"

    if not force and output_path.is_file() and recipe_path.is_file():
        try:
            recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            recipe = {}
        # ヒット判定はレシピ内 params_hash の完全一致（契約 §1）。
        if recipe.get("params_hash") == params_hash:
            return _cached_result(
                recipe,
                capture_id,
                preset_name,
                preset_label,
                output_path,
                recipe_path,
                params_hash,
                resolved,
                preview,
            )

    started_at = time.monotonic()

    try:
        with rawpy.imread(str(source_path)) as raw:
            rgb = raw.postprocess(**_postprocess_params(raw, resolved, preview))
    except (rawpy.LibRawError, OSError) as error:
        raise RawDevelopmentError(f"RAWデコードに失敗しました: {error}") from error

    rgb = np.asarray(rgb, dtype=np.uint8)

    # --- レンズ補正（リサイズ前） ---
    rgb, lens_correction = apply_lens_correction(rgb, lens_resolution)

    image = Image.fromarray(rgb, mode="RGB")

    # --- shadow_lift LUT（リサイズ前） ---
    table = _shadow_lift_table(float(resolved["shadow_lift"]))
    if table is not None:
        image = image.point(table * 3)  # RGB は 256×3 エントリ必要

    if max(image.size) > effective_max_edge:
        image.thumbnail((effective_max_edge, effective_max_edge), Image.Resampling.LANCZOS)

    image = _apply_tone(image, resolved)

    save_kwargs, exif_inherited = _resolve_save_kwargs(source_jpg, image.width, image.height)
    exif_inherited = _save_developed_jpeg(image, output_path, save_kwargs, exif_inherited)
    elapsed_seconds = round(time.monotonic() - started_at, 2)

    recipe = {
        "recipe_version": RECIPE_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "capture_id": capture_id,
        "params_hash": params_hash,
        "preset": {"name": preset_name, "label": preset_label},
        "adjustments": resolved,
        "lens_correction": lens_correction,
        "exif": capture_exif,
        "exif_inherited": exif_inherited,
        "makernote": "dropped",
        "source": {
            "path": _relative_path(source_path),
            "sha256": _sha256(source_path),
            "size_bytes": source_path.stat().st_size,
        },
        "engine": {
            "name": "rawpy/LibRaw",
            "engine_version": ENGINE_VERSION,
            "rawpy_version": rawpy.__version__,
            "pillow_version": Image.__version__,
        },
        "output": {
            "path": _relative_path(output_path),
            "width": image.width,
            "height": image.height,
            "format": "JPEG",
            "quality": JPEG_QUALITY,
            "max_edge": effective_max_edge,
            "preview": preview,
        },
        "elapsed_seconds": elapsed_seconds,
    }
    _save_recipe(recipe_path, recipe)

    return {
        "capture_id": capture_id,
        "preset": preset_name,
        "preset_label": preset_label,
        "filename": output_path.name,
        "recipe_filename": recipe_path.name,
        "params_hash": params_hash,
        "adjustments": resolved,
        "width": image.width,
        "height": image.height,
        "elapsed_seconds": elapsed_seconds,
        "cached": False,
        "preview": preview,
        "lens_correction": lens_correction,
        "exif_inherited": exif_inherited,
    }


def main() -> None:
    if config.ensure_migrated():
        print(f"[migrate] moved to app data dir: {config.CONFIG_PATH}", flush=True)

    parser = argparse.ArgumentParser(description="Sony ARWを非破壊でローカル現像します。")
    parser.add_argument("capture_id", help="例: DSC02943")
    parser.add_argument("--preset", choices=sorted(PRESETS), default=DEFAULT_PRESET)
    parser.add_argument("--force", action="store_true", help="既存出力を再生成します。")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="プレビュー現像（half_size・長辺1600px・outputs/preview/ へ出力）。",
    )
    parser.add_argument(
        "--adjustments",
        help='調整のJSON。例: \'{"exposure": 0.5, "saturation": 0}\'',
    )
    parser.add_argument("--max-edge", type=int, default=DEFAULT_MAX_EDGE)
    args = parser.parse_args()

    overrides = None
    if args.adjustments:
        try:
            overrides = json.loads(args.adjustments)
        except json.JSONDecodeError as error:
            raise RawDevelopmentError(f"--adjustments のJSONが不正です: {error}") from error

    result = develop_raw(
        args.capture_id,
        args.preset,
        adjustments=overrides,
        preview=args.preview,
        force=args.force,
        max_edge=args.max_edge,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
