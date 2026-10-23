"""凍結画像・JSON の照合（#107 の ``rawpy_images.json`` 形式）。

凍結画像は Git に入れず（実写・GPS を持ちうる）、``work/baseline/`` 等の外部に置く。照合は
``path``（凍結ルートからの相対）・``sha256``（ファイル全体）・``rgb8_sha256``（RGB 8bit へデコードした
行優先バイト列）・``dimensions``（width, height）の 4 点。欠落・改変・寸法違いは例外にし、
成功扱いにしない（docs/design/evaluation-before-python-removal.md §5）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np
from PIL import Image

REQUIRED_KEYS = ("capture_id", "mode", "path", "sha256", "rgb8_sha256", "dimensions")


class FrozenArtifactError(RuntimeError):
    """凍結物の欠落・改変・寸法違い・マニフェスト不正。"""


@dataclass(frozen=True)
class FrozenImage:
    capture_id: str
    mode: str
    path: Path
    dimensions: tuple[int, int]
    entry: dict[str, Any]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(path: Path) -> list[dict[str, Any]]:
    """凍結マニフェスト（配列）を読み、各行の必須キーと値の型を検査する。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise FrozenArtifactError(f"凍結マニフェストを読めません: {path}: {error}") from error
    if not isinstance(payload, list) or not payload:
        raise FrozenArtifactError(f"凍結マニフェストは空でない配列が必要です: {path}")
    for index, entry in enumerate(payload):
        missing = [k for k in REQUIRED_KEYS if not isinstance(entry, dict) or k not in entry]
        if missing:
            raise FrozenArtifactError(f"{path}[{index}] に {', '.join(missing)} がありません")
        dims = entry["dimensions"]
        if not (isinstance(dims, list) and len(dims) == 2 and all(isinstance(v, int) and v > 0 for v in dims)):
            raise FrozenArtifactError(f"{path}[{index}] の dimensions が不正です: {dims!r}")
        rel = PurePosixPath(str(entry["path"]))
        if rel.is_absolute() or ".." in rel.parts:
            raise FrozenArtifactError(f"{path}[{index}] の path は凍結ルートからの相対パスにしてください: {entry['path']}")
    return payload


def verify_image(entry: dict[str, Any], root: Path) -> FrozenImage:
    """1 行を照合する。欠落・ハッシュ不一致・寸法不一致は ``FrozenArtifactError``。"""
    path = Path(root) / str(entry["path"])
    label = f"{entry['capture_id']} {entry['mode']} ({entry['path']})"
    if not path.is_file():
        raise FrozenArtifactError(f"凍結画像がありません: {label} → {path}")
    actual = file_sha256(path)
    if actual != entry["sha256"]:
        raise FrozenArtifactError(f"凍結画像のファイル sha256 が一致しません: {label}（{actual}）")
    try:
        with Image.open(path) as image:
            size = image.size
            rgb = np.asarray(image.convert("RGB"))
    except OSError as error:
        raise FrozenArtifactError(f"凍結画像を読めません: {label}: {error}") from error
    if list(size) != list(entry["dimensions"]):
        raise FrozenArtifactError(f"凍結画像の寸法が一致しません: {label}（{size[0]}x{size[1]}）")
    pixels = hashlib.sha256(rgb.tobytes()).hexdigest()
    if pixels != entry["rgb8_sha256"]:
        raise FrozenArtifactError(f"凍結画像の RGB 画素 sha256 が一致しません: {label}（{pixels}）")
    return FrozenImage(entry["capture_id"], entry["mode"], path, (int(size[0]), int(size[1])), dict(entry))


def verify_manifest(manifest: Path, root: Path) -> list[FrozenImage]:
    """マニフェストの全行を照合する。1 件でも欠落・改変があれば全件の問題を列挙して失敗する。"""
    images, problems = [], []
    for entry in load_manifest(manifest):
        try:
            images.append(verify_image(entry, root))
        except FrozenArtifactError as error:
            problems.append(str(error))
    if problems:
        raise FrozenArtifactError("凍結物の照合に失敗しました:\n- " + "\n- ".join(problems))
    return images
