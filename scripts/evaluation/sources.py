"""撮影 ID の ARW / JPG 解決と EXIF 要約（``local_raw.source_files`` / ``local_raw.exif`` の評価用の写し）。

- 対象は ``DSC\\d{5}`` + 拡張子（ARW / JPG、拡張子だけ大文字小文字を問わない）。同じ ID・種別が
  大小違いで複数あれば正規表記（``.ARW`` / ``.JPG``）を優先し、無ければ実名の辞書順で最初のもの。
- EXIF は露出・レンズ・向き・撮影日時の要約だけを読む。GPS IFD は読まない。
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any

from PIL import Image

CAPTURE_ID_PATTERN = re.compile(r"^DSC\d{5}$")
KINDS = ("ARW", "JPG")
_ENTRY_PATTERN = re.compile(r"^(DSC\d{5})\.((?i:ARW|JPG))$")

TAG_ORIENTATION = 0x0112
TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_EXIF_IFD = 0x8769
TAG_EXPOSURE_TIME = 0x829A
TAG_F_NUMBER = 0x829D
TAG_ISO = 0x8827
TAG_DATETIME_ORIGINAL = 0x9003
TAG_OFFSET_TIME_ORIGINAL = 0x9011
TAG_FOCAL_LENGTH = 0x920A
TAG_LENS_MODEL = 0xA434


def _entries(root: Path) -> list[tuple[str, str, str]]:
    """``(実エントリ名, capture_id, 種別)`` を実名の辞書順で返す。"""
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    result = []
    for name in names:
        match = _ENTRY_PATTERN.match(name)
        if match:
            result.append((name, match.group(1), match.group(2).upper()))
    return result


def _pick(candidates: list[str], kind: str, capture_id: str) -> str:
    canonical = f"{capture_id}.{kind}"
    return canonical if canonical in candidates else sorted(candidates)[0]


def list_capture_files(root: Path, kind: str) -> dict[str, Path]:
    """``capture_id -> パス``（種別 ``kind`` のみ、capture_id 昇順）。"""
    kind = kind.upper()
    grouped: dict[str, list[str]] = {}
    for name, capture_id, entry_kind in _entries(root):
        if entry_kind == kind:
            grouped.setdefault(capture_id, []).append(name)
    return {capture_id: root / _pick(names, kind, capture_id) for capture_id, names in sorted(grouped.items())}


def find_capture_file(root: Path, capture_id: str, kind: str) -> Path | None:
    """``capture_id`` の ``kind``（"ARW" / "JPG"）を実エントリ名で解決する。無ければ None。"""
    kind = kind.upper()
    if kind not in KINDS or not CAPTURE_ID_PATTERN.fullmatch(capture_id):
        return None
    candidates = [name for name, entry_id, entry_kind in _entries(root) if entry_id == capture_id and entry_kind == kind]
    if not candidates:
        return None
    return root / _pick(candidates, kind, capture_id)


def paired_ids(root: Path) -> list[str]:
    """ARW と JPG が揃う撮影 ID（昇順）。"""
    jpg = set(list_capture_files(root, "JPG"))
    arw = set(list_capture_files(root, "ARW"))
    return sorted(jpg & arw)


def _rational_to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def read_capture_exif(source_jpg: Path) -> dict[str, Any]:
    """露出・レンズ・向き・撮影日時の EXIF 要約（ピクセルと GPS は読まない）。読めなければ空 dict。

    壊れた EXIF で Pillow が投げる ``OSError`` / ``ValueError`` / ``AttributeError`` は「読めない」として
    空 dict にする（旧 ``local_raw.exif.read_capture_exif`` と同じ。e838ecc）。それ以外の例外は握り潰さない。
    """
    try:
        with Image.open(source_jpg) as image:
            return _summarize_exif(image)
    except (OSError, ValueError, AttributeError):
        return {}


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


def ev100(exif: dict[str, Any] | None) -> float | None:
    """EV100 = log2(N²/t) − log2(ISO/100)。3 値のどれかが無い・不正なら None。"""
    if not exif:
        return None
    try:
        f_number = float(exif.get("f_number"))
        exposure_time = float(exif.get("exposure_time"))
        iso = float(exif.get("iso"))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) and value > 0 for value in (f_number, exposure_time, iso)):
        return None
    try:
        return math.log2(f_number**2 / exposure_time) - math.log2(iso / 100.0)
    except OverflowError:
        return None
