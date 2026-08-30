"""ブラケット撮影の露出合成（Exposure Fusion / Mertens 法）。

契約: docs/API_CONTRACT.md §1（`POST /api/merge-bracket`）・§4（命名・ディレクトリ）

解析が `exposure_bracket` と判定したグループ（露出を変えて撮った同一構図の連写）を
1枚に合成する。HDR 合成 + トーンマッピングは**採用しない**。放射輝度の推定も
トーンマッピングも要らず破綻が少ない **Exposure Fusion**（Mertens）で、LDR のまま
コントラスト・彩度・露出適正度で重み付けして合成する。

処理順序（**品質を決めるので変更しないこと**）:

    1. 各フレームを露出差を保ったままデコード（`no_auto_bright=True`）
    2. レンズ補正（フレームごと・リサイズ前）
    3. 位置合わせ（`cv2.createAlignMTB`）
    4. Mertens 合成 → クリップ（float32 0..1）
    5. リサイズ（`max_edge`）
    6. shadow_lift → Brightness → Contrast → Color → Sharpness → 8bit化
    7. save（EXIF は先頭フレームから継承 / sRGB ICC）
       本合成は `outputs/merged/`、プレビュー合成は `outputs/merged_preview/` へ

⚠️ **`no_auto_bright=True` は本モジュールの要**。`engine._postprocess_params()` は
自動明るさ補正を**有効**（`no_auto_bright=False`）にしており、フレームごとに露出が
ヒストグラム正規化される。あれを流用するとブラケットの露出差が消え、合成する意味が
無くなる。そのため合成用のデコードは `_merge_postprocess_params()` を別に持つ。
デモザイク・色空間・ビット深度（AHD / sRGB / 16bit）は単独現像と揃える。

`Adjustments` の扱い:

- `exposure` / `highlight_recovery` は **使わない**。どちらも rawpy 段
  （`exp_shift` / `bright` / `exp_preserve_highlights` / `highlight_mode`）の
  パラメータで、フレームごとの露出やハイライト処理を動かすと「露出差そのものが入力」
  という合成の前提を壊すため。`highlight_recovery` はブラケット合成では no-op とし、
  複数露出からハイライト情報を統合するMertens自体の処理に任せる。明るさを変えたい
  場合は合成後に効く `brightness` / `shadow_lift` を使う。
- `wb_temp` / `noise_reduction` も rawpy 段だが、**全フレームに同一に効く**ので
  フレーム間の露出差には影響しない。単独現像と色・ノイズの傾向を揃えるため反映する。
- トーン調整（`shadow_lift` / `brightness` / `contrast` / `saturation` / `sharpness`）は
  フレームごとではなく**合成結果に対して**1回だけ適用する。LUT とトーンの実装は
  `engine._apply_shadow_lift_array()` / `engine._apply_tone_array()` を再利用する（重複実装しない）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import rawpy
from PIL import Image

from . import config
from .engine import (
    DEFAULT_MAX_EDGE,
    ENGINE_VERSION,
    JPEG_QUALITY,
    MAX_MAX_EDGE,
    MIN_MAX_EDGE,
    PREVIEW_MAX_EDGE,
    PROJECT_ROOT,
    _apply_shadow_lift_array,
    _apply_tone_array,
    _array_to_image,
    _decode_16bit_to_float01,
    _resize_array,
    _relative_path,
    _resolve_save_kwargs,
    _save_developed_jpeg,
    _save_recipe,
    _sha256,
    _validate_capture_id,
)
from .exif import read_capture_exif
from .lens import LensResolution, _all_finite, apply_lens_correction, probe_lens
from .presets import DEFAULT_PRESET, PRESETS, RawDevelopmentError, resolve_adjustments

try:
    import cv2

    _CV2_IMPORT_ERROR: str | None = None
except Exception as error:  # pragma: no cover - 導入済み環境では通らない
    cv2 = None  # type: ignore[assignment]
    _CV2_IMPORT_ERROR = str(error)

LOGGER = logging.getLogger(__name__)

MERGED_ROOT = PROJECT_ROOT / "outputs" / "merged"

#: プレビュー品質の合成の出力先（契約 §4）。
#:
#: 本合成と**ディレクトリで分ける**のは、`params_hash` が違うだけの同居では
#: 「そのJPEGが原寸なのかプレビューなのか」をサーバ側で判定できないため。
#: 同居させると `POST /api/export` の `variant: "merged"` に長辺1600pxの合成を
#: マスターとして書き出せてしまう（フロントが両者を別々に持つことでしか防げない）。
#: 現像側が `outputs/developed/` と `outputs/preview/` に分けているのと同じ理由・同じ形。
MERGED_PREVIEW_ROOT = PROJECT_ROOT / "outputs" / "merged_preview"

#: 合成に使えるフレーム数の範囲。
#:
#: 上限 9 は「1グループ＝1シーン」を保証できないことへの防波堤。実データには
#: 1グループ 20 枚（別シーンの AEB 4セット分）が存在し、全部混ぜると破綻する。
#: **どのフレームを混ぜるかの選択はフロントの責務**で、エンジンは受け取ったものを合成する。
MIN_FRAMES = 2
MAX_FRAMES = 9

#: 合成パイプラインの版。`params_hash` に含むので、ここを上げると既存キャッシュは
#: 自動的に無効になる。
BRACKET_VERSION = 4
MERGE_RECIPE_VERSION = 1

#: 位置合わせの版。`params_hash` には**試行したか**（= この版）だけを含め、
#: 成否の**結果**は含めない。結果はデコードして合成を回すまで判らないのに対し、
#: `params_hash` はファイル名の決定に使うため作業前に確定している必要があるため
#: （`engine.develop_raw` が `probe_lens` の事前解決を使うのと同じ理由）。
#: `aligned` の実績はレシピJSONと `MergedResult` に残す。
ALIGN_VERSION = 1

#: 位置合わせで許すズレの上限（短辺に対する割合）。
#:
#: `cv2.createAlignMTB()` の既定 `max_bits=6` は **±64px** まで探す。誤ったシフトを拾うと
#: その分だけ画面の端が黒帯になるので、探索幅は画像サイズに比例させて被害を有界にする。
#:
#: 実測での効き方（ILCE-6400 / 6024x4024 の ARW・手持ち AEB 5枚）:
#:   - **本現像**: 位置合わせは 4024x6024 の段で走り `max_bits=6`（= OpenCV 既定）に
#:     なる。つまり**この上限は本現像では効かない**。既定のままでも実測ズレは (2, 1) px
#:     と小さく、実害は出ていない。
#:   - **プレビュー**: `half_size` の 2012x3012 で `max_bits=5`（±32px）に絞られる。
#:     同じブラケットの実測ズレはやはり (2, 1) px なので、正しいシフトは取りこぼさない。
#: 上限が要るのは特徴の乏しいシーン（空だけ・グラデーション）で誤検出したときの保険。
#: 合成テスト画像（128x96）では既定のままだと 1 枚がほぼ全面黒になり合成が破綻した。
ALIGN_MAX_SHIFT_RATIO = 0.02
ALIGN_MAX_BITS = 6  # OpenCV の既定値。これ以上は広げない
ALIGN_MIN_BITS = 1

__all__ = [
    "ALIGN_VERSION",
    "BRACKET_VERSION",
    "MAX_FRAMES",
    "MERGED_PREVIEW_ROOT",
    "MERGED_ROOT",
    "MIN_FRAMES",
    "RawDevelopmentError",
    "merge_bracket",
]


def _validate_capture_ids(capture_ids: Any) -> list[str]:
    """`capture_ids` を検証し、**昇順に正規化した**撮影IDの一覧を返す。

    並び順を固定するのは、`params_hash` が「ソート済み」の一覧から計算されるため。
    リクエストの順序をそのまま使うと、同じ組み合わせを別の順で投げたときに
    ハッシュ（＝キャッシュキー）は同じなのにファイル名の先頭IDだけが変わる。
    合成順・位置合わせの基準フレーム・EXIF 継承元も、この正規化後の先頭に揃える。
    """
    if not isinstance(capture_ids, (list, tuple)):
        raise RawDevelopmentError("capture_ids は撮影IDの配列で指定してください。")
    if not MIN_FRAMES <= len(capture_ids) <= MAX_FRAMES:
        raise RawDevelopmentError(
            f"合成できるのは{MIN_FRAMES}〜{MAX_FRAMES}枚です。"
            f"{len(capture_ids)}枚が指定されました。"
        )

    normalized = [_validate_capture_id(str(capture_id)) for capture_id in capture_ids]
    if len(set(normalized)) != len(normalized):
        raise RawDevelopmentError("同じ撮影IDが重複しています。")
    return sorted(normalized)


def _merge_postprocess_params(
    raw: Any,
    adjustments: dict[str, Any],
    preview: bool,
) -> dict[str, Any]:
    """合成用のデコードパラメータ（**露出差を保つ**）。

    `engine._postprocess_params()` との違いは意図的なもので、流用してはいけない:

    - `no_auto_bright=True` … 自動明るさ補正はフレームごとに露出を正規化するため、
      有効のままだとブラケットの露出差が消えて合成の意味が無くなる。
    - `exp_shift` / `bright` を渡さない（= 1.0 相当） … `exposure` は合成では使わない。
    - `exp_preserve_highlights` / `highlight_mode` を渡さない … `highlight_recovery` も
      合成では使わない。ハイライトの階調は「暗いフレーム」が持っている。

    デモザイク・色空間・ビット深度（AHD / sRGB / 16bit）と、`wb_temp` /
    `noise_reduction` の解釈は既存の現像と揃える。
    """
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
        "output_bps": 16,
        "demosaic_algorithm": rawpy.DemosaicAlgorithm.AHD,
        # プレビューも本現像と同じ full-size でデモザイクする（engine.PREVIEW_HALF_SIZE 参照）。
        "half_size": False,
        "no_auto_bright": True,
        "fbdd_noise_reduction": fbdd_mode,
        "median_filter_passes": median_passes,
        "use_auto_wb": False,
    }

    wb_temp = float(adjustments["wb_temp"])
    if wb_temp == 0:
        params["use_camera_wb"] = True
    else:
        # camera_whitebalance は [R, G1, B, G2]。R/B のみスケールする（契約 §2）。
        camera_wb = [float(gain) for gain in raw.camera_whitebalance]
        camera_wb[0] *= 1.0 + 0.003 * wb_temp
        camera_wb[2] *= 1.0 - 0.003 * wb_temp
        params["use_camera_wb"] = False
        params["user_wb"] = camera_wb

    return params


def _decode_frame(
    source_path: Path,
    adjustments: dict[str, Any],
    preview: bool,
) -> np.ndarray:
    """1フレームを露出差を保ったままデコードし `(H, W, 3)` float32 を返す。

    合成段だけを rawpy 無しで検証できるよう、**モジュール関数として切り出している**
    （`tests/test_bracket.py` はここを差し替える）。`merge_bracket` の中に
    `rawpy.imread` を直接書かないこと。
    """
    try:
        with rawpy.imread(str(source_path)) as raw:
            rgb = raw.postprocess(**_merge_postprocess_params(raw, adjustments, preview))
    except (rawpy.LibRawError, OSError) as error:
        # 例外の詳細（`[Errno 13] ... '/Users/.../DSC04510.ARW'` のように内部の絶対パスを
        # 含みうる）はレスポンスに載せず**ログにだけ**出す（契約 §0 / `server._fail()` と同じ流儀）。
        # 例外連鎖（`from error`）は残すので、サーバログ側では原因を追える。
        LOGGER.warning("RAWデコードに失敗しました（%s）: %s", source_path.name, error)
        raise RawDevelopmentError(
            f"RAWデコードに失敗しました: {source_path.name}"
        ) from error
    return _decode_16bit_to_float01(rgb)


def _require_uniform_shapes(frames: list[np.ndarray]) -> None:
    """全フレームの寸法が一致することを確かめる。

    縦横が混ざったグループ（実データには1グループ20枚の例がある）をそのまま
    OpenCV に渡すと、内容の分からない `cv2.error` が出て HTTP 500 になる。
    ここで日本語の 400 に変換しておく。
    """
    shapes = {frame.shape for frame in frames}
    if len(shapes) != 1:
        raise RawDevelopmentError(
            "画像サイズが異なるフレームは合成できません。"
            "同じ向き・同じ設定で撮影された写真を選んでください。"
        )


def _align_max_bits(shape: tuple[int, ...]) -> int:
    """画像サイズから `AlignMTB` の探索幅（`max_bits`）を決める。

    許すズレを短辺の `ALIGN_MAX_SHIFT_RATIO` に収め、`2**max_bits` px がその範囲に
    収まる最大の `max_bits` を返す（`ALIGN_MIN_BITS`〜`ALIGN_MAX_BITS` に丸める）。

    ⚠️ 本現像サイズ（短辺4024）では計算結果が OpenCV 既定の 6 と一致するため、
    **この絞り込みが実際に効くのはプレビュー以下のサイズだけ**。定数側のコメントに
    実測値がある。
    """
    min_edge = min(int(shape[0]), int(shape[1]))
    allowed = max(2.0, min_edge * ALIGN_MAX_SHIFT_RATIO)
    return int(min(ALIGN_MAX_BITS, max(ALIGN_MIN_BITS, math.floor(math.log2(allowed)))))


def _align_frames(frames: list[np.ndarray]) -> tuple[list[np.ndarray], bool]:
    """`AlignMTB` で手持ちのズレを吸収する（三脚撮影でも無害）。

    失敗しても**合成は続行**し、`aligned=False` を返す。位置合わせは品質改善であって
    合成の前提条件ではないため。基準フレームは `src[0]`（＝正規化後の先頭ID）。
    """
    try:
        # 出力は **src と同じリストを渡して in-place で受ける**（OpenCV の Python での
        # 標準イディオム）。ここを外すと静かに壊れる:
        #   - 空リストを渡す → OpenCV は何も書かず、長さ0のまま「成功」して戻る
        #   - `np.zeros_like` で事前確保したリストを渡す → **1枚が全面0になる**
        #     （実測 OpenCV 5.0.0 / 2〜5枚のいずれでも 1 枚が真っ黒。位置は枚数で変わる）
        # 入力を壊さないようコピーに対して行い、検証に落ちたら元のフレームへ戻す。
        # AlignMTB は 8bit 相当の明るさを前提に閾値ピラミッドを作るため、RAW の
        # float32 0..1 をそのまま渡さない。位置合わせの対象だけ 0..255 に拡大し、
        # 呼び出し元へ返すときに元の float32 契約へ戻す。1.0 超のヘッドルームも
        # 保つ必要があるので、uint8 へ丸めたりクリップしたりはしない。
        aligned_scaled = [
            np.ascontiguousarray(np.asarray(frame, dtype=np.float32) * 255.0).copy()
            for frame in frames
        ]
        cv2.createAlignMTB(_align_max_bits(frames[0].shape)).process(
            aligned_scaled, aligned_scaled
        )
        aligned = [
            np.asarray(frame, dtype=np.float32) / 255.0 for frame in aligned_scaled
        ]
    except Exception as error:
        LOGGER.warning("位置合わせに失敗しました。ズレ補正なしで続行します: %s", error)
        return frames, False

    if len(aligned) != len(frames) or any(
        result.shape != frame.shape for result, frame in zip(aligned, frames)
    ):
        LOGGER.warning("位置合わせの出力が不正だったため、ズレ補正なしで続行します。")
        return frames, False

    # 上記の「1枚だけ全面0」を取りこぼさないための最終確認。真っ黒なフレームを
    # 合成に混ぜると、その領域の重みが狂って合成結果が破綻する。
    if any(
        float(np.max(result)) <= 1e-6 and float(np.max(frame)) > 1e-6
        for result, frame in zip(aligned, frames)
    ):
        LOGGER.warning("位置合わせ結果に真っ黒なフレームがあるため、ズレ補正なしで続行します。")
        return frames, False
    return aligned, True


def _merge_frames(frames: list[np.ndarray]) -> np.ndarray:
    """Mertens の Exposure Fusion で合成し `(H, W, 3)` float32 を返す。

    `MergeMertens.process()` は float32 を返すが **0..1 の範囲外に出ることがある**
    （ラプラシアンピラミッドの再構成でオーバーシュートする）ので、uint8 化の前に
    必ずクリップする。`np.clip` は NaN を素通しする（`lens.py` に実測メモあり）ため、
    クリップだけでは壊れた出力を止められない。有限性の確認を先に行う。8bit化は
    リサイズ・トーン調整の後まで遅らせる。

    OpenCV の Mertens / AlignMTB はどちらも内部で `COLOR_RGB2GRAY` を使うので、
    rawpy 出力（RGB）をそのまま渡してよい（BGR への変換は不要かつ有害）。
    """
    mertens_frames = [
        np.ascontiguousarray(np.asarray(frame, dtype=np.float32) * 255.0)
        for frame in frames
    ]
    merged = np.asarray(
        cv2.createMergeMertens().process(mertens_frames), dtype=np.float32
    )
    if not _all_finite(merged):
        raise RawDevelopmentError("合成結果が不正な値になりました。フレームを見直してください。")

    # OpenCV 5.0.0 の MergeMertens は入力スケールに関わらず正規化済みの
    # float32 0..1 を返す（実測: 0..255 入力で out max 0.94、0..1 入力で
    # out max 0.0037）。「ビルドによっては入力スケールのまま返る」という
    # 追加の再スケール判定は、lens.py がビネット補正で許容する最大4.0まで
    # のヘッドルーム（VIGNETTING_HIGHLIGHT_FRACTION_LIMIT は画素の「割合」
    # しか制限せず、大きさは制限しない）を持つ入力と原理的に区別できず、
    # 正当な合成結果を255で割って真っ黒にする（このブランチが直そうとした
    # バグそのものを再導入する）ため、恒久的に削除する。
    np.clip(merged, 0.0, 1.0, out=merged)
    return merged


def _summarize_lens_corrections(
    per_frame: list[dict[str, Any]],
) -> dict[str, Any]:
    """フレームごとの `lens_correction` を `MergedResult` 用の1件に畳む。

    全フレームで同じ結果になるのが正常（`probe_lens` は EXIF から決まり、ブラケットは
    同一レンズ・同一焦点距離・同一絞りのため）。**1枚でも食い違ったら適用扱いにしない**:
    歪曲補正は放射状のワープなので、補正済みと未補正が混ざると位置合わせでは
    取り除けないゴーストになる。食い違いの中身はレシピJSONの `frames[]` に残す。
    """
    applied_flags = {bool(entry.get("applied")) for entry in per_frame}

    def shared_value(key: str) -> Any:
        """全フレームに同じ値があるフィールドだけを転記する。"""
        if not per_frame or any(key not in entry for entry in per_frame):
            return None
        values = [entry[key] for entry in per_frame]
        first = values[0]
        if all(value == first for value in values[1:]):
            return first
        return None

    def add_shared_fields(summary: dict[str, Any], keys: tuple[str, ...]) -> None:
        for key in keys:
            value = shared_value(key)
            if value is not None:
                summary[key] = value

    if applied_flags == {True}:
        first = per_frame[0]
        summary: dict[str, Any] = {"applied": True}
        if first.get("lens"):
            summary["lens"] = first["lens"]
        add_shared_fields(summary, ("flags", "focal_length", "aperture", "skipped"))
        return summary
    if applied_flags == {False}:
        first = per_frame[0]
        summary = {"applied": False, "reason": first.get("reason") or "unknown"}
        if first.get("lens_model"):
            summary["lens_model"] = first["lens_model"]
        add_shared_fields(summary, ("flags", "skipped", "focal_length", "aperture"))
        return summary
    return {"applied": False, "reason": "inconsistent_across_frames"}


# ブラケット内のフレーム間で実際に食い違いうる、ビネット非適用の理由だけを
# グループ判定のトリガーにする。他の非適用理由（レンズ/焦点距離のプロパティ、
# force_skip_vignetting自身が付けるマーカー、apply_color_modification失敗）は
# 全フレームで一致するか、こちらが制御しているため対象外。
_GROUP_SKIP_TRIGGER_REASONS = frozenset(
    {"vignetting_highlight_headroom", "vignetting_non_finite"}
)


def _apply_group_lens_corrections(
    frames: list[np.ndarray],
    lens_resolutions: list[LensResolution],
) -> tuple[list[np.ndarray], list[dict[str, Any]]]:
    """全フレームのレンズ補正を実行し、ビネット安全弁をグループで揃える。

    1パス目は通常のレンズ補正で各フレームのハイライト余地を判定する。1枚でも
    `_GROUP_SKIP_TRIGGER_REASONS` のいずれかが返った場合は、1パス目でビネットを
    適用できたフレームだけを、デコード済みの元配列から `force_skip_vignetting=True`
    で再実行する。これにより歪曲/TCAの処理は維持しつつ、ビネットだけが混在しない。

    トリガーに含めるのは画素内容に依存し、ブラケット内のフレーム間で実際に
    食い違いうる理由（ハイライト余地不足・非有限値破棄）だけ。
    `vignetting_profile_focal_guard`（レンズ/焦点距離のプロパティで全フレーム共通）と
    `vignetting_forced_skip`（この関数自身の2パス目が付与するマーカー）は
    フレーム間で揃っている、またはこちらが制御しているためトリガーにしない。
    """
    corrected_frames: list[np.ndarray] = []
    corrections: list[dict[str, Any]] = []
    for frame, resolution in zip(frames, lens_resolutions):
        corrected, correction = apply_lens_correction(frame, resolution)
        corrected_frames.append(np.ascontiguousarray(corrected))
        corrections.append(correction)

    def group_skip_trigger_reason(correction: dict[str, Any]) -> str | None:
        for reason in correction.get("skipped", []):
            if reason in _GROUP_SKIP_TRIGGER_REASONS:
                return reason
        reason = correction.get("reason")
        if reason in _GROUP_SKIP_TRIGGER_REASONS:
            return reason
        return None

    trigger_reasons = [group_skip_trigger_reason(correction) for correction in corrections]
    if not any(reason is not None for reason in trigger_reasons):
        return corrected_frames, corrections

    # グループ全体に記録する理由は、実際に発火した理由のうち最初のもの。
    group_skip_reason = next(reason for reason in trigger_reasons if reason is not None)

    for index, (frame, resolution, correction) in enumerate(
        zip(frames, lens_resolutions, corrections)
    ):
        if "vignetting" not in correction.get("flags", []):
            continue
        corrected, forced_correction = apply_lens_correction(
            frame, resolution, force_skip_vignetting=True
        )
        corrected_frames[index] = np.ascontiguousarray(corrected)
        corrections[index] = forced_correction

    # `force_skip_vignetting` は lens.py 内部の理由として
    # `vignetting_forced_skip` を返すが、レシピにはグループ判定の原因を共通して記録
    # する。フレーム単位の skipped が揃うため、全体 summary でも情報を落とさない。
    for index, correction in enumerate(corrections):
        normalized = dict(correction)
        skipped = [
            reason
            for reason in normalized.get("skipped", [])
            if reason != "vignetting_forced_skip"
        ]
        if group_skip_reason not in skipped:
            skipped.append(group_skip_reason)
        normalized["skipped"] = skipped
        if not normalized.get("applied"):
            normalized["reason"] = group_skip_reason
        corrections[index] = normalized

    return corrected_frames, corrections


def _merge_params_hash(
    capture_ids: list[str],
    adjustments: dict[str, Any],
    preset_name: str,
    max_edge: int,
    preview: bool,
    lens_applicable: bool,
    source_root: str,
) -> str:
    """合成のキャッシュキー（`engine._params_hash` と同じ思想）。

    `capture_ids`（ソート済み）と `source_root`（**解決済み**の現像元フォルダの
    絶対パス文字列）は必須。`capture_id` はカメラの連番でカードをまたぐと衝突するため、
    フォルダを含めないと**別フォルダの同じIDで他人の写真の合成結果が返る**。

    `align` は「位置合わせを試みたか」＝ `ALIGN_VERSION` だけを含め、成否の結果は
    含めない（結果は合成を回すまで判らず、ファイル名の決定に間に合わないため）。

    `engine`（= `ENGINE_VERSION`）も含める。合成はレンズ補正・トーン処理・JPEG保存を
    `engine` / `lens` と**共有**しているので、そちら側の出力が変わる修正で
    `ENGINE_VERSION` を上げたとき、合成側のキャッシュだけが無効化されずに古い結果を
    返し続ける（実例: ENGINE_VERSION 3 のビネット補正修正）。
    """
    payload = {
        "capture_ids": list(capture_ids),
        "adjustments": adjustments,
        "preset": preset_name,
        "max_edge": int(max_edge),
        "preview": bool(preview),
        "lens_applicable": bool(lens_applicable),
        "align_version": ALIGN_VERSION,
        "source_root": str(source_root),
        "versions": {
            "bracket": BRACKET_VERSION,
            "engine": ENGINE_VERSION,
            "rawpy": rawpy.__version__,
            "pillow": Image.__version__,
            "opencv": cv2.__version__ if cv2 is not None else None,
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(canonical.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]


def _cached_result(
    recipe: dict[str, Any],
    capture_ids: list[str],
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
        "capture_ids": capture_ids,
        "frames": len(capture_ids),
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
        "aligned": bool(recipe.get("aligned", False)),
        "lens_correction": recipe.get("lens_correction", {"applied": False, "reason": "unknown"}),
        "exif_inherited": bool(recipe.get("exif_inherited", False)),
    }


def merge_bracket(
    capture_ids: list[str],
    preset_name: str = DEFAULT_PRESET,
    *,
    adjustments: dict[str, Any] | None = None,
    preview: bool = False,
    force: bool = False,
    max_edge: int = DEFAULT_MAX_EDGE,
) -> dict[str, Any]:
    """ブラケット撮影を Exposure Fusion で1枚に合成する。

    戻り値は契約 `MergedResult` から `output_url` を除いた dict
    （`output_url` はサーバ側が `?v=<mtime_ns>` 付きで付与する）。

    フレーム数は2〜9枚（`MIN_FRAMES`〜`MAX_FRAMES`）。`capture_ids` は現像元フォルダ
    直下に限定し、重複は拒否する。`highlight_recovery` は no-op とする。Mertens が
    複数露出からハイライト情報を統合するため、合成後に追加の復元処理は行わない。
    その他の処理順序とパラメータの意図はモジュール docstring を参照。
    """
    if cv2 is None:
        # import 失敗の中身（dlopen が返すライブラリの絶対パス等）もレスポンスには載せない（契約 §0）。
        LOGGER.warning("OpenCV を読み込めません: %s", _CV2_IMPORT_ERROR)
        raise RawDevelopmentError("合成に必要な OpenCV を読み込めません。")

    normalized_ids = _validate_capture_ids(capture_ids)
    if preset_name not in PRESETS:
        raise RawDevelopmentError(f"未対応のプリセットです: {preset_name}")
    if not MIN_MAX_EDGE <= max_edge <= MAX_MAX_EDGE:
        raise RawDevelopmentError("最大辺は1200〜6048pxで指定してください。")

    effective_max_edge = (
        PREVIEW_MAX_EDGE if preview and max_edge == DEFAULT_MAX_EDGE else max_edge
    )

    resolved = resolve_adjustments(preset_name, adjustments)
    preset_label = PRESETS[preset_name]["label"]

    # 現像元フォルダは1リクエストにつき1回だけ解決し、パス組み立て・トラバーサル検査・
    # params_hash の全てで同じ値を使う（`engine.develop_raw` と同じ流儀）。
    source_root = config.source_root().resolve()
    source_paths: list[Path] = []
    for capture_id in normalized_ids:
        source_path = (source_root / f"{capture_id}.ARW").resolve()
        if source_path.parent != source_root or not source_path.is_file():
            raise RawDevelopmentError(f"ARWが見つかりません: {capture_id}.ARW")
        source_paths.append(source_path)

    source_jpgs = [source_root / f"{capture_id}.JPG" for capture_id in normalized_ids]
    capture_exifs = [
        read_capture_exif(path) if path.is_file() else {} for path in source_jpgs
    ]

    # レンズ解決は RAW デコード（1枚10〜20秒）の前に全フレームぶん済ませる。
    # params_hash がレンズ補正の適用可否を含むため、キャッシュ判定より前に必要。
    lens_resolutions = [probe_lens(exif) for exif in capture_exifs]
    lens_applicable = all(resolution.applicable for resolution in lens_resolutions)

    params_hash = _merge_params_hash(
        normalized_ids,
        resolved,
        preset_name,
        effective_max_edge,
        preview,
        lens_applicable,
        str(source_root),
    )

    # プレビュー合成と本合成は**ディレクトリで分ける**（`engine.develop_raw` と同じ形）。
    # ファイル名は `params_hash` が違うので衝突しないが、それだけでは
    # 「原寸かプレビューか」をサーバ側で判定できない（定数側のコメント参照）。
    destination_root = MERGED_PREVIEW_ROOT if preview else MERGED_ROOT
    destination_root.mkdir(parents=True, exist_ok=True)
    stem = f"{normalized_ids[0]}-merged{len(normalized_ids)}-{preset_name}-{params_hash}"
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
                normalized_ids,
                preset_name,
                preset_label,
                output_path,
                recipe_path,
                params_hash,
                resolved,
                preview,
            )

    started_at = time.monotonic()

    # --- 1. デコード（露出差を保つ） / 2. レンズ補正（リサイズ前） ---
    #
    # レンズ補正は「**全フレームに掛ける**」か「**1枚も掛けない**」かの二択にする。
    # 歪曲補正は放射状のワープなので、補正済みと未補正が混ざると位置合わせ
    # （`AlignMTB` は平行移動しか吸収できない）では取り除けないゴーストが周辺部に残る。
    # `lens_applicable` は「全フレームで解決できたか」の集約フラグで、`params_hash` にも
    # 入っている。適用判定にも同じ値を使うことで、キャッシュキーと実際の処理が
    # 食い違わないようにする（フレーム自身の `lens_resolution` で個別に判断すると、
    # ペアJPGが欠けている等で一部フレームだけ解決に失敗したときに割れる）。
    lens_inconsistent = not lens_applicable and any(
        resolution.applicable for resolution in lens_resolutions
    )
    decoded_frames: list[np.ndarray] = []
    frame_records: list[dict[str, Any]] = []
    for capture_id, source_path, lens_resolution in zip(
        normalized_ids, source_paths, lens_resolutions
    ):
        rgb = _decode_frame(source_path, resolved, preview)
        decoded_frames.append(np.ascontiguousarray(rgb))
        frame_records.append(
            {
                "capture_id": capture_id,
                "path": _relative_path(source_path),
                "sha256": _sha256(source_path),
                "size_bytes": source_path.stat().st_size,
            }
        )

    # デコード済みの元配列を保持したまま、レンズ補正をグループ単位で決める。全フレーム
    # 解決済みなら、ビネット安全弁が1枚でも発火したときだけ該当フレームを元配列から
    # 再実行する（デコードをやり直さない）。レンズ解決自体が不揃いな場合は従来どおり
    # 全フレームを無補正にして、補正済み/未補正の幾何を混ぜない。
    if lens_applicable:
        frames, lens_corrections = _apply_group_lens_corrections(
            decoded_frames, lens_resolutions
        )
    else:
        frames = decoded_frames
        lens_corrections = [
            resolution.unapplied("inconsistent_across_frames" if lens_inconsistent else None)
            for resolution in lens_resolutions
        ]
    for record, lens_correction in zip(frame_records, lens_corrections):
        record["lens_correction"] = lens_correction

    _require_uniform_shapes(frames)

    # --- 3. 位置合わせ / 4. 合成 ---
    frames, aligned = _align_frames(frames)
    merged = _merge_frames(frames)

    # --- 5. リサイズ → 6. shadow_lift → トーン調整（各1回だけ） ---
    merged = _resize_array(merged, effective_max_edge)
    merged = _apply_shadow_lift_array(merged, float(resolved["shadow_lift"]))
    merged = _apply_tone_array(merged, resolved)
    image = _array_to_image(merged)

    # --- 7. 保存（EXIF は先頭フレームから継承） ---
    save_kwargs, exif_inherited = _resolve_save_kwargs(source_jpgs[0], image.width, image.height)
    exif_inherited = _save_developed_jpeg(image, output_path, save_kwargs, exif_inherited)
    elapsed_seconds = round(time.monotonic() - started_at, 2)

    lens_correction = _summarize_lens_corrections(
        [record["lens_correction"] for record in frame_records]
    )

    recipe = {
        "recipe_version": MERGE_RECIPE_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "kind": "exposure_fusion",
        "capture_ids": normalized_ids,
        "params_hash": params_hash,
        "preset": {"name": preset_name, "label": preset_label},
        "adjustments": resolved,
        "aligned": aligned,
        "lens_correction": lens_correction,
        "exif": capture_exifs[0],
        "exif_inherited": exif_inherited,
        "makernote": "dropped",
        "frames": frame_records,
        "source": {"root": _relative_path(source_root)},
        "engine": {
            "name": "OpenCV MergeMertens",
            "bracket_version": BRACKET_VERSION,
            "align_version": ALIGN_VERSION,
            # `params_hash` に入れている版はすべてここにも残す（成果物だけを見て
            # 「どの版が作ったか」を answered にするためのブロック）。合成はレンズ補正・
            # トーン処理・JPEG保存を現像側と共有しているので `engine_version` も要る。
            "engine_version": ENGINE_VERSION,
            "rawpy_version": rawpy.__version__,
            "pillow_version": Image.__version__,
            "opencv_version": cv2.__version__,
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
        "capture_ids": normalized_ids,
        "frames": len(normalized_ids),
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
        "aligned": aligned,
        "lens_correction": lens_correction,
        "exif_inherited": exif_inherited,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ブラケット撮影を Exposure Fusion で合成します。"
    )
    parser.add_argument("capture_ids", nargs="+", help="例: DSC04510 DSC04513 DSC04514")
    parser.add_argument("--preset", choices=sorted(PRESETS), default=DEFAULT_PRESET)
    parser.add_argument("--force", action="store_true", help="既存出力を再生成します。")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="プレビュー合成（half_size・長辺1600px・outputs/merged_preview/ へ出力）。",
    )
    parser.add_argument(
        "--adjustments",
        help='調整のJSON。例: \'{"shadow_lift": 30, "saturation": 0}\'',
    )
    parser.add_argument("--max-edge", type=int, default=DEFAULT_MAX_EDGE)
    args = parser.parse_args()

    # 移行（不可逆なファイル移動）は `parse_args()` の**後**。`--help` や引数エラーは
    # ここへ来る前に `SystemExit` するので、意味のある実行だと分かってから副作用を起こす。
    # 通知は stderr へ。stdout は末尾の `json.dumps(...)` が機械可読な契約を持つため、
    # 初回だけ行が混ざると `| jq` のような使い方が壊れる。
    if config.ensure_migrated():
        print(
            f"[migrate] moved to app data dir: {config.CONFIG_PATH}",
            file=sys.stderr,
            flush=True,
        )

    overrides = None
    if args.adjustments:
        try:
            overrides = json.loads(args.adjustments)
        except json.JSONDecodeError as error:
            raise RawDevelopmentError(f"--adjustments のJSONが不正です: {error}") from error

    result = merge_bracket(
        args.capture_ids,
        args.preset,
        adjustments=overrides,
        preview=args.preview,
        force=args.force,
        max_edge=args.max_edge,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
