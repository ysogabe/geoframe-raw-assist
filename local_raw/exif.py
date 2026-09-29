"""ペアJPGからのEXIF継承（Pillow のみ / piexif は使わない）。

契約: docs/API_CONTRACT.md §7「EXIF継承」 / docs/spikes-wave0.md スパイク2
実機 Sony JPEG（MakerNote / GPS IFD / サムネイルIFD 付き）での round-trip 検証済み。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageCms

# --- EXIF タグID（Pillow は生の整数キーで扱う） ---
TAG_ORIENTATION = 0x0112
TAG_SOFTWARE = 0x0131
TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_EXIF_IFD = 0x8769

TAG_EXPOSURE_TIME = 0x829A
TAG_F_NUMBER = 0x829D
TAG_ISO = 0x8827
TAG_DATETIME_ORIGINAL = 0x9003
TAG_OFFSET_TIME_ORIGINAL = 0x9011
TAG_MAKERNOTE = 0x927C
TAG_FOCAL_LENGTH = 0x920A
TAG_PIXEL_X_DIMENSION = 0xA002
TAG_PIXEL_Y_DIMENSION = 0xA003
TAG_LENS_MODEL = 0xA434

try:  # rawpy はバージョン表記のためだけに使う（未導入でも本モジュールは動く）
    import rawpy as _rawpy

    _RAWPY_VERSION = _rawpy.__version__
except Exception:  # pragma: no cover - rawpy は通常インストール済み
    _RAWPY_VERSION = "unknown"

#: 出力JPEGの Software タグに入れる来歴文字列。
SOFTWARE_TAG = f"GeoFrame RAW Assist (rawpy {_RAWPY_VERSION})"


def _build_srgb_icc() -> bytes:
    """sRGB ICC プロファイルのバイト列をモジュールロード時に1回だけ生成する。"""
    profile = ImageCms.createProfile("sRGB")
    return ImageCms.ImageCmsProfile(profile).tobytes()


#: `image.save(..., icc_profile=SRGB_ICC_BYTES)` にそのまま渡す。
SRGB_ICC_BYTES: bytes = _build_srgb_icc()

__all__ = [
    "SOFTWARE_TAG",
    "SRGB_ICC_BYTES",
    "build_inherited_exif",
    "read_capture_exif",
    "read_capture_exif_from_bytes",
]


def _rational_to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _summarize_exif(image: Image.Image) -> dict[str, Any]:
    exif = image.getexif()
    ifd = exif.get_ifd(TAG_EXIF_IFD)
    lens_model = ifd.get(TAG_LENS_MODEL)
    return {
        "make": exif.get(TAG_MAKE),
        "model": exif.get(TAG_MODEL),
        "orientation": exif.get(TAG_ORIENTATION),
        "captured_at": ifd.get(TAG_DATETIME_ORIGINAL),
        "offset_time": ifd.get(TAG_OFFSET_TIME_ORIGINAL),
        "iso": ifd.get(TAG_ISO),
        "lens_model": lens_model.strip() if isinstance(lens_model, str) else lens_model,
        "focal_length": _rational_to_float(ifd.get(TAG_FOCAL_LENGTH)),
        "f_number": _rational_to_float(ifd.get(TAG_F_NUMBER)),
        "exposure_time": _rational_to_float(ifd.get(TAG_EXPOSURE_TIME)),
    }


def read_capture_exif(source_jpg: Path) -> dict[str, Any]:
    """レンズ補正の事前判定とレシピ記録に使う EXIF 要約を読む。

    デコード前に呼べるよう軽量（ピクセルは読まない）。読めない場合は空 dict を返す。
    """
    try:
        with Image.open(source_jpg) as image:
            return _summarize_exif(image)
    except (OSError, ValueError, AttributeError):
        return {}


def read_capture_exif_from_bytes(jpeg_bytes: bytes) -> dict[str, Any]:
    """JPEG バイト列（RAW 埋め込みサムネイル等）から `read_capture_exif` と同じ要約を読む。"""
    try:
        with Image.open(io.BytesIO(jpeg_bytes)) as image:
            return _summarize_exif(image)
    except (OSError, ValueError, AttributeError):
        return {}


def build_inherited_exif(source_jpg: Path, out_w: int, out_h: int) -> bytes:
    """ペアJPGのEXIFを継承した、出力JPEG用のEXIFバイト列を組み立てる。

    1. `Orientation` を **必ず 1 に正規化**（rawpy 出力は正立なので、元の 8 を継承すると二重回転する）
    2. ExifIFD の `PixelXDimension` / `PixelYDimension` を出力実寸へ更新
    3. `MakerNote` を破棄（Sony MakerNote は絶対オフセット依存で再シリアライズすると壊れる）
    4. `Software` に来歴を記録

    `exif.get_ifd()` は生きた参照を返すため、`ifd` への代入/pop はそのまま
    `exif.tobytes()` に反映される（`exif[0x8769] = ifd` の書き戻しは不要）。
    """
    with Image.open(source_jpg) as image:
        exif = image.getexif()
        exif[TAG_ORIENTATION] = 1
        exif[TAG_SOFTWARE] = SOFTWARE_TAG

        ifd = exif.get_ifd(TAG_EXIF_IFD)
        ifd[TAG_PIXEL_X_DIMENSION] = int(out_w)
        ifd[TAG_PIXEL_Y_DIMENSION] = int(out_h)
        ifd.pop(TAG_MAKERNOTE, None)

        return exif.tobytes()
