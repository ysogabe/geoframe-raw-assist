#!/usr/bin/env python3
"""現像結果を同じRAW群で機械評価する小さなベンチマーク。

評価は「良い写真」を自動判定するものではなく、プリセット変更で悪化しやすい
クリップ・暗部持ち上げ・色の暴れ・細部/ノイズの変化を同じ入力で比較するための
ガードレールである。出力は ``work/raw-evaluation/`` 配下（gitignore 対象）に置く。

既定経路（アプリ本体・rawpy・lensfunpy に依存しない）:

- ``--developed LABEL=DIR``（繰り返し可）… 現像済み画像（Core Image の出力等。ファイル名の先頭が
  撮影 ID ``DSC#####`` ちょうど（直後が数字でない）のもの）を同じ撮影 ID のカメラ JPEG と比べる。
  LABEL ごとに集計する。**1 つの DIR には 1 撮影 ID につき 1 枚だけ**置く（同じ撮影 ID の画像が
  複数あれば平均が黙って偏るので失敗にする）。compare_engines の ``coreimage/preview`` 等は同じ撮影の
  ``_preview`` / ``_preview_lens`` / ``_frozen*`` / ``_e<edge>`` を並べて持つので、そのままは渡せない。
  評価したい 1 バリアントだけを別フォルダへ集めてから LABEL を付ける。
- ``--frozen-manifest`` + ``--frozen-root`` … 凍結画像をハッシュ・寸法で照合し（欠落・改変は失敗）、
  カメラ JPEG と比べる。ラベルは ``frozen:<mode>``。

旧経路（アーカイブ）: ``--legacy-rawpy`` で旧 rawpy エンジンを各プリセットで現像して評価する
（``local_raw/`` が残る checkout と requirements-raw.txt の環境が前提。docs/development/evaluation-paths.md）。

例:
    # 1 バリアント（例: レンズ補正ありのプレビュー）だけを別フォルダへ集めてから評価する
    mkdir -p work/raw-evaluation/inputs/ci_lens
    cp work/engine-comparison/<run>/coreimage/preview/DSC*_preview_lens.jpg work/raw-evaluation/inputs/ci_lens/
    python scripts/evaluate_raw.py --source-root work/raw \
      --developed ci_lens=work/raw-evaluation/inputs/ci_lens \
      --frozen-manifest tests/fixtures/baseline/rawpy_images.json --frozen-root ../wt-107-baseline

    .venv/bin/python scripts/evaluate_raw.py --legacy-rawpy \
      --source-root /Volumes/KIOXIA/image_work --limit 12 --preview
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
import time
from pathlib import Path
from unittest import mock

import cv2
import numpy as np
from PIL import Image, ImageOps

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 評価用の独立サポート（scripts/evaluation/）。アプリ本体・rawpy・lensfunpy に依存しない。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from evaluation import frozen, legacy, sources  # noqa: E402
from evaluation.presets import PRESET_NAMES as PRESETS  # noqa: E402

DEVELOPED_SUFFIXES = (".jpg", ".jpeg", ".tif", ".tiff", ".png")
#: 先頭が撮影 ID ちょうど（``DSC074020.jpg`` を ``DSC07402`` と読まないよう、直後の数字を許さない）。
DEVELOPED_NAME = re.compile(r"^(DSC\d{5})(?!\d)")

# Rec.709 輝度係数（ITU-R BT.709）。出力画像と参照JPEGの両方の luma 導出で共有する。
REC709_LUMA = (0.2126, 0.7152, 0.0722)


def _paired_ids(source_root: Path) -> list[str]:
    """ARW と JPG が揃う撮影 ID（拡張子の大小を問わない。既定経路と同じ ``sources`` の解決）。"""
    return sources.paired_ids(source_root)


def _select_ids(available: list[str], requested: list[str] | None, limit: int) -> list[str]:
    if requested:
        missing = sorted(set(requested) - set(available))
        if missing:
            raise SystemExit(f"ペアが見つからない撮影IDです: {', '.join(missing)}")
        return requested
    if limit <= 0 or limit >= len(available):
        return available
    # 先頭・中間・末尾を均等に拾い、連番の一部だけへ過学習しないようにする。
    # dtype=int は切り捨て(floor)で前方へ偏るため、理想位置を四捨五入して使う。
    positions = np.rint(np.linspace(0, len(available) - 1, num=limit)).astype(int)
    return list(dict.fromkeys(available[int(position)] for position in positions))


def _read_rgb(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.uint8)


def _metrics(output: np.ndarray, camera_jpg: np.ndarray) -> dict[str, float]:
    rgb = output.astype(np.float32) / 255.0
    kr, kg, kb = REC709_LUMA
    luma = kr * rgb[:, :, 0] + kg * rgb[:, :, 1] + kb * rgb[:, :, 2]
    gray = cv2.cvtColor(output, cv2.COLOR_RGB2GRAY).astype(np.float32)
    blurred = cv2.GaussianBlur(gray, (0, 0), 1.0)
    highpass = gray - blurred

    reference = cv2.resize(
        camera_jpg,
        (output.shape[1], output.shape[0]),
        interpolation=cv2.INTER_AREA,
    ).astype(np.float32) / 255.0
    reference_luma = (
        kr * reference[:, :, 0]
        + kg * reference[:, :, 1]
        + kb * reference[:, :, 2]
    )

    # レンズ補正の失敗で数十pxの黒帯が出るケースを、内容依存の全体平均とは別に
    # 捕捉する。左右の端帯と、その直隣の帯の平均輝度差を測る（通常の周辺減光は
    # 緩やかなので、この値が急に大きくならない）。これは品質スコアではなく、
    # 境界アーティファクトの回帰ガードである。
    edge_band = max(8, output.shape[1] // 80)
    left_edge = luma[:, :edge_band].mean()
    left_inner = luma[:, edge_band : edge_band * 2].mean()
    right_edge = luma[:, -edge_band:].mean()
    right_inner = luma[:, -edge_band * 2 : -edge_band].mean()

    return {
        # Any-channel clipping is the important guardrail for color casts.
        "clip_low_any": float(np.any(rgb <= 0.0, axis=2).mean()),
        "clip_high_any": float(np.any(rgb >= 1.0, axis=2).mean()),
        "clip_high_all": float(np.all(rgb >= 1.0, axis=2).mean()),
        "dark_lt_5pct": float((luma < 0.05).mean()),
        "bright_gt_95pct": float((luma > 0.95).mean()),
        "mean_luma": float(luma.mean()),
        "p01_luma": float(np.percentile(luma, 1)),
        "p50_luma": float(np.percentile(luma, 50)),
        "p99_luma": float(np.percentile(luma, 99)),
        "mean_channel_spread": float((rgb.max(axis=2) - rgb.min(axis=2)).mean()),
        "highpass_std": float(highpass.std()),
        "laplacian_variance": float(cv2.Laplacian(gray, cv2.CV_32F).var()),
        "max_side_luma_drop": float(
            max(0.0, float(left_inner - left_edge), float(right_inner - right_edge))
        ),
        # This is not a quality score; it measures distance from the camera JPEG
        # so an extreme preset can be spotted without claiming the JPEG is truth.
        "camera_jpg_mae": float(np.abs(rgb - reference).mean()),
        "camera_jpg_luma_mae": float(np.abs(luma - reference_luma).mean()),
    }


def _summarize(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    by_preset: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        if row.get("error"):
            continue
        by_preset.setdefault(str(row["preset"]), []).append(row)

    # 集計対象はエラーでない全行のキーの和集合から決める。先頭行だけを見ると、
    # 先頭行がエラー行の場合に他の正常行の数値列が summary から消えてしまう。
    numeric_keys = sorted({
        key
        for row in rows
        if not row.get("error")
        for key, value in row.items()
        if isinstance(value, (int, float)) and key not in {"elapsed_seconds"}
    })
    summary: list[dict[str, object]] = []
    for preset, preset_rows in by_preset.items():
        item: dict[str, object] = {"preset": preset, "images": len(preset_rows)}
        for key in numeric_keys:
            values = [float(row[key]) for row in preset_rows if isinstance(row.get(key), (int, float))]
            if values:
                item[f"mean_{key}"] = statistics.fmean(values)
                item[f"median_{key}"] = statistics.median(values)
        summary.append(item)
    return summary


def _read_camera_rgb(path: Path) -> np.ndarray:
    """カメラ JPEG を EXIF の向きを反映して読む（現像出力は正立なので向きを揃える）。"""
    with Image.open(path) as image:
        return np.asarray(ImageOps.exif_transpose(image).convert("RGB"), dtype=np.uint8)


def parse_developed(specs: list[str]) -> list[tuple[str, Path]]:
    """``LABEL=DIR`` の並び。LABEL は英数字・``_``・``-``・``:``・``.``。同じ LABEL の重複は失敗。

    LABEL ごとに集計するため、同じ LABEL を 2 回指定すると別フォルダの同じ撮影 ID が 1 つの集計に混ざる。
    """
    result = []
    seen: dict[str, str] = {}
    for spec in specs:
        label, sep, directory = spec.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z0-9_.:-]+", label) or not directory:
            raise SystemExit(f"--developed は LABEL=DIR で指定してください: {spec!r}")
        if label in seen:
            raise SystemExit(
                f"--developed の LABEL {label!r} が重複しています（{seen[label]!r} と {spec!r}）。"
                "同じ LABEL は 1 つの集計にまとまり、同じ撮影 ID が重複して平均が偏ります。"
                "フォルダごとに別の LABEL（例 ci_a=DIR_A ci_b=DIR_B）を付けるか、1 フォルダにまとめてください。"
            )
        seen[label] = spec
        path = Path(directory).expanduser().resolve()
        if not path.is_dir():
            raise SystemExit(f"--developed のフォルダがありません: {path}")
        result.append((label, path))
    return result


def developed_images(directory: Path) -> list[tuple[str, Path]]:
    """フォルダ直下の現像済み画像（先頭が撮影 ID ``DSC#####`` ちょうどの名前）を (capture_id, path) で返す。

    同じ撮影 ID の画像が 2 枚以上あれば ``SystemExit``（どれか 1 枚を黙って選ぶことも、全部を同じ LABEL に
    混ぜて平均を偏らせることもしない）。評価したい 1 バリアントだけを別フォルダに置いて指定し直してもらう。
    """
    by_capture: dict[str, list[Path]] = {}
    for path in sorted(directory.iterdir()):
        match = DEVELOPED_NAME.match(path.name)
        if match and path.is_file() and path.suffix.lower() in DEVELOPED_SUFFIXES:
            by_capture.setdefault(match.group(1), []).append(path)
    duplicated = {cid: paths for cid, paths in by_capture.items() if len(paths) > 1}
    if duplicated:
        detail = "\n".join(f"- {cid}: {', '.join(p.name for p in paths)}" for cid, paths in sorted(duplicated.items()))
        raise SystemExit(
            f"--developed のフォルダに同じ撮影 ID の画像が複数あります（1 撮影 ID につき 1 枚にしてください）: {directory}\n"
            f"{detail}\n"
            "同じ LABEL に混ぜると集計が重複分だけ偏ります。compare_engines の coreimage/preview 等は "
            "_preview / _preview_lens / _frozen* / _e<edge> を並べて持つので、評価したい 1 バリアントだけを"
            "別フォルダへ集め、バリアントごとに --developed LABEL=DIR を分けて指定してください。"
        )
    return [(cid, paths[0]) for cid, paths in sorted(by_capture.items())]


def evaluate_image(label: str, capture_id: str, path: Path, source_root: Path, extra: dict[str, object] | None = None) -> dict[str, object]:
    """1 枚の現像済み画像をカメラ JPEG と比べた行（失敗は error 列）。"""
    row: dict[str, object] = {"capture_id": capture_id, "preset": label, "output": str(path), **(extra or {})}
    camera = sources.find_capture_file(source_root, capture_id, "JPG")
    try:
        if camera is None:
            raise FileNotFoundError(f"カメラ JPEG がありません: {capture_id}")
        row.update(_metrics(_read_rgb(path), _read_camera_rgb(camera)))
    except Exception as error:  # 1 枚の失敗で他を隠さない
        row["error"] = f"{type(error).__name__}: {error}"
    return row


def run_legacy(args: argparse.Namespace, source_root: Path, run_root: Path) -> tuple[list[dict[str, object]], list[str], dict[str, object]]:
    """旧経路（アーカイブ）: 旧 rawpy エンジンで各プリセットを現像して評価する。"""
    feature = "evaluate_raw の旧 rawpy 現像"
    config = legacy.import_module("local_raw.config", feature)
    engine = legacy.import_module("local_raw.engine", feature)
    available = _paired_ids(source_root)
    capture_ids = _select_ids(available, args.ids, args.limit)
    if not capture_ids:
        raise SystemExit("評価できるJPG/ARWペアがありません。")
    developed_root = run_root / "developed"
    preview_root = run_root / "preview"
    developed_root.mkdir(parents=True, exist_ok=True)
    preview_root.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, object]] = []
    # The production engine resolves source_root at request time. Patch only this
    # isolated benchmark process so evaluating KIOXIA does not mutate app settings.
    with mock.patch.object(config, "source_root", return_value=source_root), \
         mock.patch.object(engine, "OUTPUT_ROOT", developed_root), \
         mock.patch.object(engine, "PREVIEW_ROOT", preview_root):
        for preset_name in args.presets:
            for capture_id in capture_ids:
                row: dict[str, object] = {"capture_id": capture_id, "preset": preset_name}
                try:
                    result = engine.develop_raw(
                        capture_id,
                        preset_name,
                        preview=args.preview,
                        force=True,
                    )
                    output_root = preview_root if args.preview else developed_root
                    output_path = output_root / str(result["filename"])
                    camera = sources.find_capture_file(source_root, capture_id, "JPG")
                    if camera is None:
                        raise FileNotFoundError(f"カメラ JPEG がありません: {capture_id}")
                    # 既定経路と同じく、カメラ JPEG は EXIF の向きを反映して読む（出力は正立）。
                    metrics = _metrics(_read_rgb(output_path), _read_camera_rgb(camera))
                    row.update(metrics)
                    row["elapsed_seconds"] = float(result["elapsed_seconds"])
                    row["output"] = str(output_path)
                except Exception as error:  # keep one bad RAW from hiding the rest
                    row["error"] = f"{type(error).__name__}: {error}"
                rows.append(row)
    return rows, capture_ids, {"presets": args.presets, "preview": bool(args.preview), "engine_version": engine.ENGINE_VERSION}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="現像結果（Core Image の出力・凍結画像、旧経路は opt-in）をカメラ JPEG と機械評価します。")
    parser.add_argument("--source-root", type=Path, required=True, help="カメラ JPEG（と旧経路では ARW）のあるフォルダ")
    parser.add_argument("--output-root", type=Path, default=Path("work/raw-evaluation"))
    parser.add_argument("--developed", action="append", default=[], metavar="LABEL=DIR",
                        help="現像済み画像のフォルダ（先頭が撮影 ID DSC##### の名前、1 撮影 ID につき 1 枚）。LABEL ごとに集計（繰り返し可）")
    parser.add_argument("--frozen-manifest", type=Path, help="凍結画像のマニフェスト（例 tests/fixtures/baseline/rawpy_images.json）")
    parser.add_argument("--frozen-root", type=Path, help="凍結マニフェストの path の基準ディレクトリ")
    parser.add_argument("--legacy-rawpy", action="store_true",
                        help="旧 rawpy エンジンで各プリセットを現像して評価する（アーカイブ。local_raw/ と requirements-raw.txt が前提）")
    parser.add_argument("--ids", nargs="*", help="（旧経路）評価する撮影ID。省略時は --limit で均等抽出")
    parser.add_argument("--limit", type=int, default=8, help="（旧経路）均等抽出する枚数")
    parser.add_argument("--presets", nargs="*", choices=sorted(PRESETS), default=sorted(PRESETS), help="（旧経路）プリセット")
    parser.add_argument("--preview", action="store_true", help="（旧経路）長辺1600pxのプレビューで評価")
    args = parser.parse_args(argv)

    source_root = args.source_root.expanduser().resolve()
    if not source_root.is_dir():
        raise SystemExit(f"現像元フォルダがありません: {source_root}")
    if not (args.developed or args.frozen_manifest or args.legacy_rawpy):
        raise SystemExit("--developed LABEL=DIR か --frozen-manifest を指定してください（旧 rawpy 現像は --legacy-rawpy: "
                         + legacy.PREREQUISITE + "）")
    if args.frozen_manifest and not args.frozen_root:
        raise SystemExit("--frozen-manifest には --frozen-root が必要です")
    developed = parse_developed(args.developed)

    frozen_images: list[frozen.FrozenImage] = []
    if args.frozen_manifest:
        try:
            frozen_images = frozen.verify_manifest(args.frozen_manifest.resolve(), args.frozen_root.expanduser().resolve())
        except frozen.FrozenArtifactError as error:
            print(str(error), file=sys.stderr)
            raise SystemExit(1) from None

    run_name = time.strftime("%Y%m%d-%H%M%S")
    run_root = args.output_root / run_name
    run_root.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, object]] = []
    meta: dict[str, object] = {}
    if args.legacy_rawpy:
        legacy_rows, capture_ids, meta = run_legacy(args, source_root, run_root)
        rows.extend(legacy_rows)
    for image in frozen_images:
        rows.append(evaluate_image(f"frozen:{image.mode}", image.capture_id, image.path, source_root,
                                   {"frozen_path": image.entry["path"]}))
    for label, directory in developed:
        images = developed_images(directory)
        if not images:
            raise SystemExit(f"--developed {label} に先頭が撮影 ID（DSC#####）の画像がありません: {directory}")
        rows.extend(evaluate_image(label, cid, path, source_root) for cid, path in images)

    errors = sum(bool(row.get("error")) for row in rows)
    payload = {
        "source_root": str(source_root),
        "capture_ids": sorted({str(row["capture_id"]) for row in rows}),
        "labels": sorted({str(row["preset"]) for row in rows}),
        "developed": {label: str(path) for label, path in developed},
        "frozen_manifest": str(args.frozen_manifest) if args.frozen_manifest else None,
        "legacy_rawpy": bool(args.legacy_rawpy),
        **meta,
        "rows": rows,
        "summary": _summarize(rows),
    }
    (run_root / "evaluation.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    fields = sorted({key for row in rows for key in row})
    with (run_root / "evaluation.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    print(json.dumps({
        "run_root": str(run_root),
        "images": len(payload["capture_ids"]),
        "labels": len(payload["labels"]),
        "rows": len(rows),
        "errors": errors,
        "summary": payload["summary"],
    }, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)

if __name__ == "__main__":
    try:
        main()
    except legacy.LegacyUnavailable as error:
        raise SystemExit(str(error)) from None
