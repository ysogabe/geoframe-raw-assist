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
ANALYSIS_PATH = PROJECT_ROOT / "outputs" / "analysis" / "analysis.json"

MANIFEST_VERSION = 1
APP_NAME = "geoframe-raw-assist"
MANIFEST_NAME = "manifest.json"
VARIANTS = ("camera_jpg", "developed")

MAX_BATCH_NAME_LENGTH = 64
MAX_ITEMS = 500

#: batch_name で許可しない文字（`_` に置換する）。
_DISALLOWED_IN_BATCH_NAME = re.compile(r"[^A-Za-z0-9_-]")

__all__ = [
    "ANALYSIS_PATH",
    "EXPORT_ROOT",
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


def _load_analysis() -> dict[str, Any]:
    """`analysis.json` を読む。存在しない・壊れている場合は空 dict。"""
    try:
        if not ANALYSIS_PATH.is_file():
            return {}
        loaded = json.loads(ANALYSIS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


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


def _load_recipe(developed_filename: str) -> dict[str, Any] | None:
    """現像JPEGに対応するレシピJSON（同一ステム + `.json`）を読む。"""
    recipe_path = (OUTPUT_ROOT / f"{Path(developed_filename).stem}.json").resolve()
    if recipe_path.parent != OUTPUT_ROOT.resolve() or not recipe_path.is_file():
        return None
    try:
        loaded = json.loads(recipe_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


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
    analysis = _load_analysis()
    # バッチ全体で同じフォルダを見る（途中で切り替えられても混ざらない）。
    source_root = config.source_root().resolve()

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
            raise ValueError("variant は camera_jpg か developed を指定してください。")

        source_jpg = _resolve_within(source_root, f"{capture_id}.JPG")
        source_arw = _resolve_within(source_root, f"{capture_id}.ARW")
        recipe: dict[str, Any] | None = None

        if variant == "camera_jpg":
            source_path = source_jpg
            exported_name = f"{capture_id}.JPG"
            missing_reason = "元のJPGが見つかりません。"
        else:
            developed_filename = _validate_developed_filename(
                item.get("developed_filename"), capture_id
            )
            source_path = _resolve_within(OUTPUT_ROOT, developed_filename)
            exported_name = developed_filename
            missing_reason = "現像済みJPEGが見つかりません。"
            recipe = _load_recipe(developed_filename)

        if source_path is None or not source_path.is_file():
            skipped.append({"capture_id": capture_id, "reason": missing_reason})
            continue
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
