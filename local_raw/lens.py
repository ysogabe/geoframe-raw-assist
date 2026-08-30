"""lensfun によるレンズ補正（ビネット・歪曲・TCA）。

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

#: ビネット補正の候補画像で、いずれかのチャンネルが1.0を超える割合がこれを
#: 上回ったら補正を捨てる。広角レンズの強いゲインを空や雲へ一括適用すると、
#: その後のトーン処理で大面積の白飛びになるため。歪曲/TCAは継続する。
VIGNETTING_HIGHLIGHT_FRACTION_LIMIT = 0.10

#: このプロファイルの16〜18mmは、現行の Sony ARW/rawpy 経路では lensfun の
#: 周辺減光ゲインが過大になりやすい。焦点距離が短い範囲だけ周辺減光を止め、
#: 歪曲/TCAは残す。モデル名は loose match ではなく解決済みDBの厳密値で比較する。
SONY_E_PZ_16_50_MODEL = "E PZ 16-50mm f/3.5-5.6 OSS"
SONY_E_PZ_16_50_VIGNETTING_MIN_FOCAL = 18.0

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


def _all_finite(array: np.ndarray) -> bool:
    """配列に NaN / inf が含まれないか。

    `np.isfinite(array).all()` と等価だが、全画素ぶんの bool 配列を作らない
    （6000x4000x3 で 70MB 超になる）。np.min/np.max は NaN を伝播するので、
    NaN なら min が NaN に、±inf なら min/max が inf になり、いずれも弾ける。
    """
    return bool(np.isfinite(array.min()) and np.isfinite(array.max()))


def _remap_interpolation(height: int, width: int) -> int:
    """画像サイズに応じたレンズ補正の補間方式を返す。"""
    # 24MP前後の本現像・品質プレビューではLINEARの軟化が目立つ。一方、半解像度の
    # 高速プレビューや小さなテスト画像では、CUBICのオーバーシュートよりLINEARを優先する。
    return cv2.INTER_CUBIC if max(height, width) >= 4000 else cv2.INTER_LINEAR


def _vignetting_profile_guard(resolution: LensResolution) -> str | None:
    """特定プロファイルでビネット補正を止める理由を返す。"""
    lens_obj = resolution.lens
    maker = str(getattr(lens_obj, "maker", "")).strip().casefold()
    model = str(getattr(lens_obj, "model", "")).strip().casefold()
    if (
        maker == "sony"
        and model == SONY_E_PZ_16_50_MODEL.casefold()
        and resolution.focal_length is not None
        and resolution.focal_length <= SONY_E_PZ_16_50_VIGNETTING_MIN_FOCAL
    ):
        return "profile_focal_guard"
    return None


class _GeometryCorrectionGuardError(Exception):
    """歪曲/TCAの出力を破棄して部分適用を続けるための内部例外。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def apply_lens_correction(
    rgb: np.ndarray,
    resolution: LensResolution,
    force_skip_vignetting: bool = False,
) -> tuple[np.ndarray, dict[str, Any]]:
    """rawpy 出力 (H, W, 3) にビネット・歪曲・TCA補正を適用する。

    大きな出力では INTER_CUBIC で細線の解像感を保ち、半解像度以下の高速経路では
    INTER_LINEAR で安全性を優先する。TCA較正があるレンズは歪曲と統合したRGB別マップで
    一度だけ適用する。

    返り値は `(補正後配列, lens_correction dict)`。失敗時は入力配列をそのまま返す。
    寸法は EXIF ではなく **実際の配列** から取る（preview の half_size で半分になるため）。

    `force_skip_vignetting=True` ではビネット補正だけを強制的にスキップする。
    ブラケットのように、呼び出し元がグループ全体でビネット補正の適用可否を
    揃えたい場合に使う。既定値では従来どおりフレーム単位の安全弁を適用する。
    """
    if not resolution.applicable:
        return rgb, resolution.unapplied()

    try:
        height, width = rgb.shape[0], rgb.shape[1]
        applied_flags: list[str] = []
        skipped_flags: list[str] = []
        source_dtype = rgb.dtype
        maximum = float(np.iinfo(source_dtype).max) if np.issubdtype(source_dtype, np.integer) else 1.0

        # ビネット補正は float32 0..1 のバッファ上で in-place に行う。
        # このバッファは initialize() より **先に** 作る（pixel_format をここから導くため）。
        working = np.ascontiguousarray(rgb.astype(np.float32) / maximum)

        modifier = lensfunpy.Modifier(
            resolution.lens, resolution.camera.crop_factor, width, height
        )
        # pixel_format は apply_color_modification に渡すバッファの dtype と必ず一致させる。
        # 既定値は np.uint8 で、float32 のバッファを渡すと lensfun がそれを uint8 の
        # バイト列として走査し、補正が各行の左1/4だけに桁外れのゲインとして乗る
        # （実測: 最大 3.4e38・負値・NaN、gain>2 の画素がちょうど 24.5%）。
        # source_dtype ではなく working から導くこと。両者は別物（uint8/uint16 対 float32）で、
        # source_dtype を渡すと同じ種類の食い違いが再発する。
        #
        # SCALE は autoscale（scale-to-fill）。`initialize` の `scale` 引数の既定値 0.0 は
        # 「autoscale せよ」の意味だが、このフラグが無いと実行されない。糸巻き型歪曲
        # （望遠側）の補正では remap が入力フレーム外を参照し、cv2.remap の既定
        # BORDER_CONSTANT の黒が縁に残る（実測: Sony E 55-210mm @80mm で最大 65.8px の
        # はみ出し、画素の約 2% が黒縁・リンギング）。autoscale は補正込みの座標マップに
        # 画面いっぱいへの拡大を織り込むので画像外参照が 0 になり、出力寸法は W×H のまま
        # 不変（自前のクロップ・リサイズは不要）。
        has_distortion = bool(getattr(resolution.lens, "calib_distortion", None))
        has_tca = bool(getattr(resolution.lens, "calib_tca", None))
        vignetting_guard_reason = (
            "forced_skip"
            if force_skip_vignetting
            else _vignetting_profile_guard(resolution)
        )

        # TCAがあるレンズだけTCAフラグを立てる。テスト用の軽量な偽lensfun実装など、
        # 古いModifyFlagsにTCAが無い環境でも既存の歪曲補正は継続できるようにする。
        modify_flags = 0
        if vignetting_guard_reason is None:
            modify_flags |= lensfunpy.ModifyFlags.VIGNETTING
        if has_tca:
            modify_flags |= getattr(lensfunpy.ModifyFlags, "TCA", 0)
        if has_distortion:
            modify_flags |= (
                lensfunpy.ModifyFlags.DISTORTION | lensfunpy.ModifyFlags.SCALE
            )

        modifier.initialize(
            resolution.focal_length,
            resolution.aperture,
            FOCUS_DISTANCE_METERS,
            pixel_format=working.dtype.type,
            flags=modify_flags,
        )

        # --- ビネット補正（float32 0..1 で in-place） ---
        if vignetting_guard_reason is not None:
            corrected = rgb
            skipped_flags.append(f"vignetting_{vignetting_guard_reason}")
        elif modifier.apply_color_modification(working):
            if _all_finite(working):
                # 正しい pixel_format でのゲインは発散しないが、明るい空に強い
                # ビネット補正を掛けると広い範囲が1.0を超えることがある。これは
                # クリップして通すと、周辺減光の修正ではなく大面積の白飛びになる。
                highlight_fraction = float(
                    np.any(working > 1.0, axis=2).mean()
                )
                if highlight_fraction > VIGNETTING_HIGHLIGHT_FRACTION_LIMIT:
                    LOGGER.warning(
                        "ビネット補正でハイライトの余裕を失うため破棄します: %s (%.1f%%)",
                        resolution.lens_name,
                        highlight_fraction * 100.0,
                    )
                    # working は in-place で変更済みだが rgb は元の配列のまま。
                    # 元画像へ戻して、後段の歪曲/TCAだけは適用する。
                    corrected = rgb
                    skipped_flags.append("vignetting_highlight_headroom")
                else:
                    # float32経路では周辺の1.0超えを歪曲再標本化まで保持する。
                    # 内部上限4.0は異常なDBゲインの安全弁で、最終表示用の0..1
                    # クリップはengine側で行う。整数経路は従来どおり飽和させる。
                    if np.issubdtype(source_dtype, np.integer):
                        np.clip(working, 0.0, 1.0, out=working)
                        corrected = (working * maximum).astype(source_dtype)
                    else:
                        np.clip(working, 0.0, 4.0, out=working)
                        corrected = working
                    applied_flags.append("vignetting")
            else:
                # 安全網: np.clip は NaN を素通しする（np.clip(nan, 0, 1) は nan）ため、
                # クリップでは壊れた出力を止められない。非有限値が出たらビネット補正の
                # 結果ごと捨て、元の配列で続行する（"vignetting" も積まない）。
                LOGGER.warning(
                    "ビネット補正が非有限値を生成したため破棄します: %s", resolution.lens_name
                )
                corrected = rgb
        else:
            corrected = rgb

        # --- 歪曲 / TCA補正 ---
        # 歪曲較正の無いレンズでは幾何マップを要求しない。SCALE（autoscale）は幾何補正が
        # ひとつも無くても恒等マップ（float32 丸め誤差 ~0.001px 付き）を返すため、そのまま
        # 呼ぶと "distortion" フラグが偽って付いて無意味な恒等 remap が走り、較正なしの
        # レンズが "lens_not_calibrated" フォールバックに到達しなくなる。
        # なお同梱DB・追加DBとも model="none" の歪曲較正は 0 件なので、真偽値で足りる。
        # getattr なのは、属性が無い/None の異常系でここが AttributeError になると
        # 外側の catch-all が成功済みのビネット補正まで道連れに破棄するため
        # （その場合は歪曲だけを黙って諦め、部分適用のまま返すのが正しい）。
        # 歪曲/TCAは、ビネット補正が成功した配列を退避してから適用する。マップの
        # 形状不一致やremap後の非有限値は幾何補正だけを諦め、ビネットの部分成功を守る。
        corrected_before_geometry = corrected
        try:
            tca_coordinates = False
            coordinates = None
            if has_tca:
                coordinates = (
                    modifier.apply_subpixel_geometry_distortion()
                    if has_distortion
                    else modifier.apply_subpixel_distortion()
                )
                tca_coordinates = coordinates is not None
            if coordinates is None and has_distortion:
                coordinates = modifier.apply_geometry_distortion()
            if coordinates is not None:
                # autoscale 済みでもマップ端は実質ソース境界上に乗るため、画像外を黒で埋める
                # BORDER_CONSTANTではなくBORDER_REPLICATEを使う。大きな本現像はCubic、
                # 半解像度以下はLinearで再標本化し、TCAはRGB別の統合マップを一度だけ適用する。
                coordinates = np.asarray(coordinates, dtype=np.float32)
                interpolation = _remap_interpolation(height, width)
                if tca_coordinates:
                    if coordinates.shape != (height, width, 3, 2):
                        raise _GeometryCorrectionGuardError("tca_map_unexpected_shape")
                    remapped = np.empty_like(corrected)
                    for channel in range(3):
                        remapped[..., channel] = cv2.remap(
                            corrected[..., channel],
                            coordinates[..., channel, :],
                            None,
                            interpolation,
                            borderMode=cv2.BORDER_REPLICATE,
                        )
                    if not _all_finite(remapped):
                        raise _GeometryCorrectionGuardError("geometry_non_finite")
                    corrected = remapped
                    if has_distortion:
                        applied_flags.append("distortion")
                    applied_flags.append("tca")
                else:
                    corrected = cv2.remap(
                        corrected,
                        coordinates,
                        None,
                        interpolation,
                        borderMode=cv2.BORDER_REPLICATE,
                    )
                    if not _all_finite(corrected):
                        raise _GeometryCorrectionGuardError("geometry_non_finite")
                    applied_flags.append("distortion")

            if not _all_finite(corrected):
                raise _GeometryCorrectionGuardError("geometry_non_finite")
        except _GeometryCorrectionGuardError as error:
            corrected = corrected_before_geometry
            skipped_flags.append(error.reason)
        except Exception as error:
            # lensfun/OpenCVの想定外エラーでも、成功済みのビネット補正は保持して
            # 歪曲/TCAだけをスキップする。理由は安定したフラグに正規化し、詳細はログへ残す。
            corrected = corrected_before_geometry
            skipped_flags.append("geometry_correction_failed")
            LOGGER.warning(
                "歪曲/TCA補正をスキップします (%s): %s",
                type(error).__name__,
                error,
            )

        if not applied_flags:
            reason = skipped_flags[0] if skipped_flags else "lens_not_calibrated"
            return rgb, resolution.unapplied(reason)

        correction = {
            "applied": True,
            "lens": resolution.lens_name,
            "flags": applied_flags,
            "focal_length": resolution.focal_length,
            "aperture": resolution.aperture,
        }
        if skipped_flags:
            correction["skipped"] = skipped_flags
        return corrected, correction
    except Exception as error:
        LOGGER.warning("レンズ補正に失敗しました。補正なしで続行します: %s", error)
        return rgb, resolution.unapplied(f"error:{error}")
