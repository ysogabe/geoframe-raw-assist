"""現像元フォルダ内のファイル解決（ARW / JPG）を1か所に集約する。

カメラ・コピー方法によって拡張子は `.ARW` / `.arw` / `.JPG` / `.jpg` と揺れる。
macOS の `pathlib.glob` は既定で大文字小文字を区別する一方、APFS（既定）は区別しない
ボリュームなので、経路ごとに解決方法が違うと「解析では見つからないが現像では開ける」
という食い違いが起きる。全経路がこのモジュール経由で **フォルダの実エントリ名** を
返すことで、大文字小文字を区別するボリュームでも一貫して動く。

- 対象は `DSC\\d{5}` + 拡張子（ARW / JPG）。ステム（`DSC12345`）は大文字固定、拡張子だけ
  大文字小文字を問わない。
- 同じ ID・種別で大小違いのファイルが複数ある場合（大文字小文字を区別するボリュームでのみ
  起こりうる）は決定的に選ぶ: 正規表記（`.ARW` / `.JPG`）を最優先し、なければ実名の
  辞書順で最初のもの。

返すパスは `root / 実エントリ名` で、resolve はしない。呼び出し側は従来どおり
`resolve()` 後に親が `root`（resolve 済み）と一致するか検査してシンボリックリンクによる
フォルダ外参照を拒否すること。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

KINDS = ("ARW", "JPG")
_ENTRY_PATTERN = re.compile(r"^(DSC\d{5})\.((?i:ARW|JPG))$")


def _entries(root: Path) -> list[tuple[str, str, str]]:
    """`(実エントリ名, capture_id, 種別)` を実名の辞書順で返す。"""
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
    """`capture_id -> パス`（種別 `kind` のみ、capture_id 昇順）。"""
    kind = kind.upper()
    grouped: dict[str, list[str]] = {}
    for name, capture_id, entry_kind in _entries(root):
        if entry_kind == kind:
            grouped.setdefault(capture_id, []).append(name)
    return {
        capture_id: root / _pick(names, kind, capture_id)
        for capture_id, names in sorted(grouped.items())
    }


def find_capture_file(root: Path, capture_id: str, kind: str) -> Path | None:
    """`capture_id` の `kind`（"ARW" / "JPG"）ファイルを実エントリ名で解決する。無ければ None。"""
    kind = kind.upper()
    if kind not in KINDS or not re.fullmatch(r"DSC\d{5}", capture_id):
        return None
    candidates = [
        name for name, entry_id, entry_kind in _entries(root)
        if entry_id == capture_id and entry_kind == kind
    ]
    if not candidates:
        return None
    return root / _pick(candidates, kind, capture_id)


def source_entry_names(root: Path) -> list[str]:
    """キャッシュ署名用: 対象ファイルの実エントリ名一覧（辞書順）。"""
    return [name for name, _, _ in _entries(root)]
