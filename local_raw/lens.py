"""lensfun によるレンズ補正（ビネット・歪曲）。

契約: docs/API_CONTRACT.md §7「lensfun DB の扱い」 / §9 / docs/spikes-wave0.md スパイク1

設計方針: **どの失敗経路でも現像は必ず続行する**。lensfunpy が import できない、
カメラが見つからない、レンズが見つからない、補正中に例外 —— いずれも
`lens_correction = {"applied": False, "reason": ...}` を返すだけで、例外は投げない。

追加較正データは `local_raw/lensfun_db/*.xml`（取得元と改変内容は同ディレクトリの
README.md を参照）。ディレクトリが無い・空・壊れたXMLがある、のいずれでも動く。
"""

from __future__ import annotations

import glob
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

LOGGER = logging.getLogger(__name__)

LENSFUN_DB_DIR = Path(__file__).resolve().parent / "lensfun_db"

#: Modifier.initialize に渡す被写体距離[m]。風景主体なので実質無限遠。
FOCUS_DISTANCE_METERS = 1000.0

try:
    import lensfunpy

    _LENSFUNPY_IMPORT_ERROR: str | None = None
except Exception as error:  # pragma: no cover - 導入済み環境では通らない
    lensfunpy = None  # type: ignore[assignment]
    _LENSFUNPY_IMPORT_ERROR = str(error)

try:
    import cv2

    _CV2_IMPORT_ERROR: str | None = None
except Exception as error:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]
    _CV2_IMPORT_ERROR = str(error)

__all__ = [
    "LensResolution",
    "apply_lens_correction",
    "lensfun_info",
    "probe_lens",
]


@dataclass
class LensResolution:
    """デコード前に確定できるレンズ解決結果。

    `params_hash` はレンズ補正の適用有無を含むため、RAWデコード（10〜20秒）に
    入る前にこの解決を済ませておく必要がある。ピクセルには一切触らない。
    """

    applicable: bool = False
    reason: str | None = None
    lens_model: str | None = None
    lens_name: str | None = None
    focal_length: float | None = None
    aperture: float | None = None
    camera: Any = field(default=None, repr=False)
    lens: Any = field(default=None, repr=False)

    def unapplied(self, reason: str | None = None) -> dict[str, Any]:
        """`lens_correction`（不適用時）を組み立てる。"""
        payload: dict[str, Any] = {
            "applied": False,
            "reason": reason or self.reason or "unknown",
        }
        if self.lens_model:
            payload["lens_model"] = self.lens_model
        return payload


def _database_paths() -> list[str]:
    if not LENSFUN_DB_DIR.is_dir():
        return []
    return sorted(glob.glob(str(LENSFUN_DB_DIR / "*.xml")))


@lru_cache(maxsize=1)
def _database() -> Any:
    """lensfun DB を1回だけロードする（失敗時は None）。

    `Database(paths=[...])` は **1ファイルでも壊れていると全体の構築が失敗する**
    （実測: DB version 2 のXMLを渡すと XMLFormatError）。そのため
    一括ロード → 個別ロードで生存XMLのみ採用 → 同梱DBのみ、の順にフォールバックする。
    """
    if lensfunpy is None:
        return None

    paths = _database_paths()
    if not paths:
        return lensfunpy.Database()

    try:
        return lensfunpy.Database(paths=paths)
    except Exception as error:
        LOGGER.warning("lensfun 追加DBの一括ロードに失敗しました (%s)。個別に再試行します。", error)

    usable: list[str] = []
    for path in paths:
        try:
            lensfunpy.Database(paths=[path])
        except Exception as error:
            LOGGER.warning("lensfun 追加DBを読み飛ばします: %s (%s)", path, error)
        else:
            usable.append(path)

    try:
        return lensfunpy.Database(paths=usable) if usable else lensfunpy.Database()
    except Exception as error:  # pragma: no cover - 同梱DBが壊れている異常系
        LOGGER.warning("lensfun DB のロードに失敗しました: %s", error)
        return None


def lensfun_info() -> dict[str, Any]:
    """`/api/health` の `lensfun` キーに入る dict（契約 §9）。"""
    if lensfunpy is None:
        return {"available": False}

    info: dict[str, Any] = {"available": _database() is not None}
    version = getattr(lensfunpy, "lensfun_version", None)
    if version:
        info["db_version"] = ".".join(str(part) for part in version)
    return info


def probe_lens(exif_summary: dict[str, Any]) -> LensResolution:
    """EXIF 要約だけでレンズ補正が適用可能かを判定する（デコード前に呼ぶ）。"""
    lens_model = exif_summary.get("lens_model")
    lens_model = lens_model.strip() if isinstance(lens_model, str) else None
    resolution = LensResolution(lens_model=lens_model)

    if lensfunpy is None:
        resolution.reason = "lensfunpy_unavailable"
        return resolution
    if cv2 is None:
        resolution.reason = "error:opencv_unavailable"
        return resolution

    database = _database()
    if database is None:
        resolution.reason = "lensfunpy_unavailable"
        return resolution

    try:
        # カメラは厳密一致で引く。loose_search=True はメーカー名だけで別機種に
        # マッチしうる（実測: Nikon/存在しない型番 → "Nikon Coolpix A" crop 1.523）。
        # 誤ったクロップファクタでの補正は無補正より有害なので、外したら諦める。
        cameras = database.find_cameras(
            exif_summary.get("make"), exif_summary.get("model")
        )
        if not cameras:
            resolution.reason = "camera_not_found"
            return resolution
        camera = cameras[0]

        if not lens_model:
            resolution.reason = "lens_not_in_db"
            return resolution

        # EXIF にレンズメーカー名は入らないので maker=None で引く（spikes §スパイク1）。
        lenses = database.find_lenses(camera, None, lens_model)
        if not lenses:
            lenses = database.find_lenses(camera, None, lens_model, loose_search=True)
        if not lenses:
            resolution.reason = "lens_not_in_db"
            return resolution

        focal_length = exif_summary.get("focal_length")
        aperture = exif_summary.get("f_number")
        if not focal_length or not aperture:
            resolution.reason = "error:focal_or_aperture_missing"
            return resolution

        resolution.camera = camera
        resolution.lens = lenses[0]
        resolution.lens_name = f"{lenses[0].maker} / {lenses[0].model}"
        resolution.focal_length = float(focal_length)
        resolution.aperture = float(aperture)
        resolution.applicable = True
        return resolution
    except Exception as error:
        resolution.reason = f"error:{error}"
        return resolution


def apply_lens_correction(
    rgb: np.ndarray,
    resolution: LensResolution,
) -> tuple[np.ndarray, dict[str, Any]]:
    """rawpy 出力 (H, W, 3) にビネット・歪曲補正を適用する（リサイズ前に呼ぶ）。

    返り値は `(補正後配列, lens_correction dict)`。失敗時は入力配列をそのまま返す。
    寸法は EXIF ではなく **実際の配列** から取る（preview の half_size で半分になるため）。
    """
    if not resolution.applicable:
        return rgb, resolution.unapplied()

    try:
        height, width = rgb.shape[0], rgb.shape[1]
        modifier = lensfunpy.Modifier(
            resolution.lens, resolution.camera.crop_factor, width, height
        )
        modifier.initialize(
            resolution.focal_length,
            resolution.aperture,
            FOCUS_DISTANCE_METERS,
            flags=lensfunpy.ModifyFlags.VIGNETTING | lensfunpy.ModifyFlags.DISTORTION,
        )

        applied_flags: list[str] = []
        source_dtype = rgb.dtype
        maximum = float(np.iinfo(source_dtype).max) if np.issubdtype(source_dtype, np.integer) else 1.0

        # --- ビネット補正（float32 0..1 で in-place） ---
        working = np.ascontiguousarray(rgb.astype(np.float32) / maximum)
        if modifier.apply_color_modification(working):
            # ビネット補正は多項式による除算なので容易に範囲外へ発散する
            # （実測で 5.7e12 まで到達）。クリップは必須。
            np.clip(working, 0.0, 1.0, out=working)
            corrected = (working * maximum).astype(source_dtype)
            applied_flags.append("vignetting")
        else:
            corrected = rgb

        # --- 歪曲補正 ---
        coordinates = modifier.apply_geometry_distortion()
        if coordinates is not None:
            corrected = cv2.remap(corrected, coordinates, None, cv2.INTER_LANCZOS4)
            applied_flags.append("distortion")

        if not applied_flags:
            return rgb, resolution.unapplied("lens_not_calibrated")

        return corrected, {
            "applied": True,
            "lens": resolution.lens_name,
            "flags": applied_flags,
            "focal_length": resolution.focal_length,
            "aperture": resolution.aperture,
        }
    except Exception as error:
        LOGGER.warning("レンズ補正に失敗しました。補正なしで続行します: %s", error)
        return rgb, resolution.unapplied(f"error:{error}")
