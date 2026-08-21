"""書き出し（GeoFrame 取り込み用バッチ）とマニフェスト生成。

契約: docs/API_CONTRACT.md §1（`POST /api/export`）・§4（ディレクトリ規約）
マニフェスト仕様: プラン §3

方針:
- コピー元は現像元フォルダ（`config.source_root()`）/ `OUTPUT_ROOT` 配下に `resolve()` 済みで限定する。
- コピー先は `outputs/export/<batch_name>/` 配下限定。元ファイルは一切変更しない。
- **入力の検証・セキュリティ違反は `ValueError`（→ HTTP 400）**、
  **ファイルが単に存在しないだけなら `skipped[]` に積んでバッチは続行**する。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config
from .engine import CAPTURE_ID_PATTERN, OUTPUT_ROOT, PROJECT_ROOT

EXPORT_ROOT = PROJECT_ROOT / "outputs" / "export"
# 合成（Exposure Fusion）の**本合成**の出力先。`bracket.MERGED_ROOT` と同じ場所を指すが、
# import すると cv2 まで引き込むのでここでローカルに導出する（`server.py` と同じ流儀）。
# プレビュー合成（`outputs/merged_preview/`）は**意図的にここへ含めない**。書き出しは
# 原寸のマスターだけを対象にするため、プレビューは構造的に対象外にする（契約 §1・§4）。
MERGED_ROOT = PROJECT_ROOT / "outputs" / "merged"

MANIFEST_VERSION = 1
APP_NAME = "geoframe-raw-assist"
MANIFEST_NAME = "manifest.json"
VARIANTS = ("camera_jpg", "developed", "merged")

MAX_BATCH_NAME_LENGTH = 64
MAX_ITEMS = 500

#: 合成JPEGのファイル名（`{先頭capture_id}-merged{枚数}-{preset}-{hash10}.jpg`）。
_MERGED_FILENAME_PATTERN = re.compile(r"^(?P<capture_id>DSC\d{5})-merged\d+-.+\.jpg$", re.IGNORECASE)

#: batch_name で許可しない文字（`_` に置換する）。
_DISALLOWED_IN_BATCH_NAME = re.compile(r"[^A-Za-z0-9_-]")

__all__ = [
    "EXPORT_ROOT",
    "MERGED_ROOT",
    "MANIFEST_NAME",
    "export_batch",
    "export_dir_for",
    "normalize_batch_name",
]


def normalize_batch_name(raw_name: Any) -> str:
    """`batch_name` を `[A-Za-z0-9_-]+` に正規化する。

    パス区切りや `..` を含む入力は**置換せず拒否**する（`../evil` を `___evil` として
    受理してしまうと、呼び出し側が「拒否された」と認識できないため）。
    それ以外の文字（空白・日本語など）は `_` に置換する。
    """
    if not isinstance(raw_name, str):
        raise ValueError("バッチ名は文字列で指定してください。")
    name = raw_name.strip()
    if not name:
        raise ValueError("バッチ名を指定してください。")
    if len(name) > MAX_BATCH_NAME_LENGTH:
        raise ValueError(f"バッチ名は{MAX_BATCH_NAME_LENGTH}文字以内で指定してください。")
    if "/" in name or "\\" in name or ".." in name or "\x00" in name:
        raise ValueError("バッチ名にパス区切りは使用できません。")
    normalized = _DISALLOWED_IN_BATCH_NAME.sub("_", name)
    if not normalized:
        raise ValueError("バッチ名に使用できる文字が含まれていません。")
    return normalized


def export_dir_for(batch_name: str) -> Path:
    """正規化済みバッチ名から書き出し先ディレクトリを解決する（`outputs/export/` 配下限定）。"""
    normalized = normalize_batch_name(batch_name)
    export_root = EXPORT_ROOT.resolve()
    destination = (EXPORT_ROOT / normalized).resolve()
    if destination.parent != export_root:
        raise ValueError("書き出し先が不正です。")
    return destination


def _validate_capture_id(raw_capture_id: Any) -> str:
    if not isinstance(raw_capture_id, str):
        raise ValueError("撮影IDは文字列で指定してください。")
    capture_id = raw_capture_id.strip().upper()
    if not CAPTURE_ID_PATTERN.fullmatch(capture_id):
        raise ValueError("無効な撮影IDです。DSC00000形式で指定してください。")
    return capture_id


def _validate_developed_filename(raw_filename: Any, capture_id: str) -> str:
    """現像済みJPEGのファイル名を検証する。

    他 capture の成果物やレシピJSON（`.json`）を持ち出せないよう、
    `{capture_id}-` 始まり・`.jpg` 終わりを必須とする。
    """
    if not isinstance(raw_filename, str) or not raw_filename.strip():
        raise ValueError("現像済みファイル名を指定してください。")
    filename = raw_filename.strip()
    if "/" in filename or "\\" in filename or ".." in filename or "\x00" in filename:
        raise ValueError("現像済みファイル名が不正です。")
    if not filename.startswith(f"{capture_id}-") or not filename.lower().endswith(".jpg"):
        raise ValueError("現像済みファイル名が撮影IDと一致しません。")
    if _MERGED_FILENAME_PATTERN.fullmatch(filename):
        # 合成済みファイル名は `outputs/merged/` にあるので、ここで探しても必ず
        # 「見つからない」で skip される。取り違えは沈黙させず変種の誤りとして返す。
        raise ValueError("合成済みファイルは variant: merged で指定してください。")
    return filename


def _validate_merged_filename(raw_filename: Any, capture_id: str) -> str:
    """合成済みJPEGのファイル名を検証する。

    `_validate_developed_filename` と同じ趣旨だが、`-merged{枚数}-` の中置を必須にする。
    前方一致だけだと現像済みファイル名が `merged` 経路を、合成ファイル名が `developed`
    経路を素通りしてしまい、`OUTPUT_ROOT` と `MERGED_ROOT` の取り違えが起きる。
    `capture_id` は**合成の先頭フレーム**（ファイル名の先頭ID）と一致させる。
    """
    if not isinstance(raw_filename, str) or not raw_filename.strip():
        raise ValueError("合成済みファイル名を指定してください。")
    filename = raw_filename.strip()
    if "/" in filename or "\\" in filename or ".." in filename or "\x00" in filename:
        raise ValueError("合成済みファイル名が不正です。")
    matched = _MERGED_FILENAME_PATTERN.fullmatch(filename)
    if matched is None or matched.group("capture_id").upper() != capture_id:
        raise ValueError("合成済みファイル名が撮影IDと一致しません。")
    return filename


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(path: Path) -> str:
    """プロジェクト相対の文字列パス。相対化できない場合は絶対パスのまま返す。"""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _load_analysis(source_root: Path) -> dict[str, Any]:
    """`source_root` の解析結果だけを読む。一致しない・無い・壊れている場合は空 dict。

    `capture_id` はカメラの連番なので、カードが違えば別の写真に同じ ID が付く。
    フォルダを検証せずに `analysis.json` を読むと、切り替え後に前のフォルダの
    メタデータが manifest に載ってしまうため、署名照合込みの
    `analysis.read_cached_analysis()` に委ねる（不一致は `None` なので空 dict に
    正規化し、`_exif_from_source` の実ファイル読みフォールバックへ落とす）。

    `analysis` は cv2 / numpy を引く重いモジュールなので、import は呼び出し時に遅延させる
    （`config.source_folder_info()` と同じ流儀）。
    """
    from . import analysis

    try:
        cached = analysis.read_cached_analysis(source_root)
    except (OSError, ValueError):
        # `read_cached_analysis()` は OSError / JSONDecodeError は自分で握るが、
        # 不正な UTF-8 の `UnicodeDecodeError`（= ValueError の subclass）は素通しする。
        # 壊れたキャッシュで書き出しを 500 にしないため、ここで空 dict へ落とす。
        return {}
    return cached if isinstance(cached, dict) else {}


def _group_info(analysis: dict[str, Any], capture_id: str) -> dict[str, Any] | None:
    groups = analysis.get("groups")
    if not isinstance(groups, list):
        return None
    for group in groups:
        if not isinstance(group, dict) or capture_id not in (group.get("capture_ids") or []):
            continue
        raw = group.get("raw") if isinstance(group.get("raw"), dict) else {}
        return {
            "id": group.get("id"),
            "kind": group.get("kind"),
            "raw_level": raw.get("level"),
            "was_recommended": group.get("recommended_capture_id") == capture_id,
            "user_selected": True,
        }
    return None


def _exif_from_source(source_jpg: Path | None) -> dict[str, Any] | None:
    """analysis.json が無い場合の簡易フォールバック（Pillow で元JPGから読む）。"""
    if source_jpg is None or not source_jpg.is_file():
        return None
    try:
        from .exif import read_capture_exif
    except Exception:
        return None
    exif = read_capture_exif(source_jpg)
    if not exif:
        return None
    return {
        "captured_at": exif.get("captured_at"),
        "iso": exif.get("iso"),
        "lens_model": exif.get("lens_model"),
        "focal_length": exif.get("focal_length"),
    }


def _exif_info(
    analysis: dict[str, Any], capture_id: str, source_jpg: Path | None
) -> dict[str, Any] | None:
    photos = analysis.get("photos")
    photo = photos.get(capture_id) if isinstance(photos, dict) else None
    if isinstance(photo, dict):
        return {
            "captured_at": photo.get("captured_at"),
            "iso": photo.get("iso"),
            "lens_model": photo.get("lens_model"),
            "focal_length": photo.get("focal_length"),
        }
    return _exif_from_source(source_jpg)


def _load_recipe(output_filename: str, root: Path) -> dict[str, Any] | None:
    """出力JPEGに対応するレシピJSON（同一ステム + `.json`）を `root` から読む。

    `root` は現像なら `OUTPUT_ROOT`、合成なら `MERGED_ROOT`。どちらも
    `resolve()` 後の親一致でルート外への脱出を弾く。
    """
    recipe_path = (root / f"{Path(output_filename).stem}.json").resolve()
    if recipe_path.parent != root.resolve() or not recipe_path.is_file():
        return None
    try:
        loaded = json.loads(recipe_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _recipe_source_root(recipe: dict[str, Any] | None, variant: str) -> Path | None:
    """レシピJSONに記録された**生成時点の現像元フォルダ**。判定できなければ None。

    出所の書かれる場所は変種で違う（どちらも `engine._relative_path()` を通るので、
    プロジェクト内なら相対・外なら絶対パスで入る）:

    - 現像（`engine.develop_raw`）: `source.path` … ARW の**ファイル**パス
    - 合成（`bracket.merge_bracket`）: `source.root` … 現像元**フォルダ**のパス

    比較を1本にするため、どちらもフォルダの絶対パスへ揃えて返す。相対パスは
    `PROJECT_ROOT` 基準で絶対化する（`_relative_path()` の相対化と対になる）。

    None は「**出所が記録されていない**」であって「一致した」ではない。レシピの無い
    `camera_jpg` や、`source` を持たない古い形式・壊れたレシピが該当する。判定材料の
    無いものまで弾くと、そうした成果物が一律で書き出せなくなるため通す側に倒している。
    """
    source = recipe.get("source") if isinstance(recipe, dict) else None
    if not isinstance(source, dict):
        return None
    raw_path = source.get("root") if variant == "merged" else source.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    try:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        resolved = candidate.resolve()
    except (OSError, ValueError):
        # NUL 文字入り・長すぎるパス等の壊れたレシピ。判定不能として扱う。
        return None
    return resolved if variant == "merged" else resolved.parent


def _recipe_is_preview(recipe: dict[str, Any] | None) -> bool:
    """レシピJSONがプレビュー品質の出力を指しているか。

    プレビュー合成は `outputs/merged_preview/` に分けたので、`MERGED_ROOT` だけを見る
    限りここへは来ないはず。**書き出しは GeoFrame へ渡す最終工程**なので、ディレクトリ
    分離をすり抜けた場合（分離前の古い出力が残っている等）に備えた二段目の防御として持つ。

    フラグが実際に書かれる位置は `output.preview`（`engine` / `bracket` のレシピで共通）。
    直下の `preview` も見るのは、レシピの形が変わったときに防御が黙って死なないため。
    """
    if not isinstance(recipe, dict):
        return False
    if bool(recipe.get("preview")):
        return True
    output = recipe.get("output")
    return isinstance(output, dict) and bool(output.get("preview"))


def _resolve_within(root: Path, filename: str) -> Path | None:
    """`root` 直下のファイルとして解決する。root を出るものは None。"""
    try:
        candidate = (root / filename).resolve()
    except (ValueError, OSError):
        return None
    if candidate.parent != root.resolve():
        return None
    return candidate


def _copy(source: Path, destination: Path) -> None:
    """`shutil.copy2` でメタデータごとコピーする（tmp + `os.replace` でアトミック）。"""
    temporary = destination.with_name(f"{destination.name}.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _write_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    temporary = manifest_path.with_name(f"{manifest_path.name}.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, manifest_path)


def export_batch(batch_name: Any, items: Any) -> dict[str, Any]:
    """選択された写真を `outputs/export/<batch_name>/` へ複製し、manifest.json を書く。

    契約 §1 の `POST /api/export` と 1:1。戻り値のパスはすべてプロジェクト相対文字列。
    """
    normalized_batch = normalize_batch_name(batch_name)
    if not isinstance(items, list) or not items:
        raise ValueError("書き出す写真を1枚以上指定してください。")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"一度に書き出せるのは{MAX_ITEMS}枚までです。")

    export_dir = export_dir_for(normalized_batch)
    # バッチ全体で同じフォルダを見る（途中で切り替えられても混ざらない）。
    # 解析結果も同じ `source_root` で検証する（別フォルダの解析は使わない）。
    source_root = config.source_root().resolve()
    analysis = _load_analysis(source_root)

    copies: list[tuple[Path, Path]] = []
    manifest_items: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    planned_names: set[str] = set()

    for item in items:
        if not isinstance(item, dict):
            raise ValueError("書き出し項目の形式が不正です。")
        capture_id = _validate_capture_id(item.get("capture_id"))
        variant = item.get("variant")
        if variant not in VARIANTS:
            raise ValueError("variant は camera_jpg / developed / merged を指定してください。")

        source_jpg = _resolve_within(source_root, f"{capture_id}.JPG")
        source_arw = _resolve_within(source_root, f"{capture_id}.ARW")
        recipe: dict[str, Any] | None = None

        if variant == "camera_jpg":
            source_path = source_jpg
            exported_name = f"{capture_id}.JPG"
            missing_reason = "元のJPGが見つかりません。"
        elif variant == "merged":
            merged_filename = _validate_merged_filename(item.get("merged_filename"), capture_id)
            source_path = _resolve_within(MERGED_ROOT, merged_filename)
            exported_name = merged_filename
            missing_reason = "合成済みJPEGが見つかりません。"
            recipe = _load_recipe(merged_filename, MERGED_ROOT)
        else:
            developed_filename = _validate_developed_filename(
                item.get("developed_filename"), capture_id
            )
            source_path = _resolve_within(OUTPUT_ROOT, developed_filename)
            exported_name = developed_filename
            missing_reason = "現像済みJPEGが見つかりません。"
            recipe = _load_recipe(developed_filename, OUTPUT_ROOT)

        if source_path is None or not source_path.is_file():
            skipped.append({"capture_id": capture_id, "reason": missing_reason})
            continue
        recipe_root = _recipe_source_root(recipe, variant)
        if recipe_root is not None and recipe_root != source_root:
            # 別のフォルダで作った成果物。`OUTPUT_ROOT` / `MERGED_ROOT` はフォルダに
            # 依らない固定ディレクトリなので、A で現像した JPEG は B へ切り替えた後も
            # 同じ名前でそこに残っている。`capture_id` はカメラの連番でカードをまたぐと
            # 衝突するため、これを素通しすると **実体は A・EXIF と group は B**（後者は
            # 現在のフォルダから引く）という自己矛盾した manifest が 200 で書き出される。
            # 解析キャッシュ側は `_load_analysis()` が署名照合で同じ取り違えを防いでいる。
            #
            # 現在のフォルダから見れば「その成果物は無い」ので、見つからなかったときと
            # 同じく `skipped[]` に積んでバッチは続行する（B の写真の書き出しまで
            # 巻き添えで 400 にしない）。理由も同じ文言にして、どのフォルダに何があるかを
            # レスポンスから推測させない。
            skipped.append({"capture_id": capture_id, "reason": missing_reason})
            continue
        if _recipe_is_preview(recipe):
            # プレビュー品質（長辺1600px）の成果物をマスターとして書き出させない。
            # `variant == "merged"` に限定しない: 現像レシピにも `output.preview` は
            # 書かれており（`engine.develop_raw`）、プレビュー現像が
            # `outputs/developed/` に紛れ込んだ場合に無警告で通ってしまう。
            # `camera_jpg` は `recipe` が None なので、`_recipe_is_preview()` は
            # `isinstance(recipe, dict)` で False を返し、ここは素通りする。
            # 「ファイルが単に無い」ではなく**入力の取り違え**なので skipped ではなく
            # `ValueError`（→400）にする（`_validate_developed_filename` が変種の取り違えを
            # 沈黙させず 400 にするのと同じ判断）。コピーはループを抜けた後にまとめて行うので、
            # ここで送出してもバッチは1件も書き出さずに終わる。
            raise ValueError("プレビュー品質の合成は書き出せません。本合成をやり直してください。")
        if exported_name in planned_names:
            skipped.append({"capture_id": capture_id, "reason": "同じファイルが重複しています。"})
            continue
        planned_names.add(exported_name)

        copies.append((source_path, export_dir / exported_name))
        manifest_items.append(
            {
                "capture_id": capture_id,
                "variant": variant,
                "exported_file": exported_name,
                "source": {
                    "jpg": _relative_path(source_jpg) if source_jpg and source_jpg.is_file() else None,
                    "arw": _relative_path(source_arw) if source_arw and source_arw.is_file() else None,
                    "jpg_sha256": (
                        _sha256(source_jpg) if source_jpg and source_jpg.is_file() else None
                    ),
                },
                "recipe": recipe,
                "group": _group_info(analysis, capture_id),
                "exif": _exif_info(analysis, capture_id, source_jpg),
            }
        )

    export_dir.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    for source_path, destination in copies:
        _copy(source_path, destination)
        files.append(_relative_path(destination))

    manifest_path = export_dir / MANIFEST_NAME
    _write_manifest(
        manifest_path,
        {
            "manifest_version": MANIFEST_VERSION,
            "app": APP_NAME,
            "batch_name": normalized_batch,
            "created_at": datetime.now(UTC).isoformat(),
            "source_root": _relative_path(source_root),
            "items": manifest_items,
        },
    )

    return {
        "export_dir": _relative_path(export_dir),
        "manifest_path": _relative_path(manifest_path),
        "files": files,
        "skipped": skipped,
    }
