#!/usr/bin/env python3
"""現像結果を同じRAW群で機械評価する小さなベンチマーク。

評価は「良い写真」を自動判定するものではなく、プリセット変更で悪化しやすい
クリップ・暗部持ち上げ・色の暴れ・細部/ノイズの変化を同じ入力で比較するための
ガードレールである。出力は ``work/raw-evaluation/`` 配下（gitignore 対象）に置く。

例:
    .venv/bin/python scripts/evaluate_raw.py \
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
from PIL import Image

# Running ``python scripts/evaluate_raw.py`` puts ``scripts/`` (not the
# repository root) first on sys.path. Keep the documented invocation working
# without requiring callers to set PYTHONPATH manually.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from local_raw import config, engine
from local_raw.presets import PRESETS

CAPTURE_ID_PATTERN = re.compile(r"^DSC\d{5}$")


def _paired_ids(source_root: Path) -> list[str]:
    jpg_ids = {
        path.stem
        for path in source_root.glob("DSC*.JPG")
        if path.is_file() and CAPTURE_ID_PATTERN.fullmatch(path.stem)
    }
    raw_ids = {
        path.stem
        for path in source_root.glob("DSC*.ARW")
        if path.is_file() and CAPTURE_ID_PATTERN.fullmatch(path.stem)
    }
    return sorted(jpg_ids & raw_ids)


def _select_ids(available: list[str], requested: list[str] | None, limit: int) -> list[str]:
    if requested:
        missing = sorted(set(requested) - set(available))
        if missing:
            raise SystemExit(f"ペアが見つからない撮影IDです: {', '.join(missing)}")
        return requested
    if limit <= 0 or limit >= len(available):
        return available
    # 先頭・中間・末尾を均等に拾い、連番の一部だけへ過学習しないようにする。
    positions = np.linspace(0, len(available) - 1, num=limit, dtype=int)
    return list(dict.fromkeys(available[int(position)] for position in positions))


def _read_rgb(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.uint8)


def _metrics(output: np.ndarray, camera_jpg: np.ndarray) -> dict[str, float]:
    rgb = output.astype(np.float32) / 255.0
    luma = 0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]
    gray = cv2.cvtColor(output, cv2.COLOR_RGB2GRAY).astype(np.float32)
    blurred = cv2.GaussianBlur(gray, (0, 0), 1.0)
    highpass = gray - blurred

    reference = cv2.resize(
        camera_jpg,
        (output.shape[1], output.shape[0]),
        interpolation=cv2.INTER_AREA,
    ).astype(np.float32) / 255.0
    reference_luma = (
        0.2126 * reference[:, :, 0]
        + 0.7152 * reference[:, :, 1]
        + 0.0722 * reference[:, :, 2]
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

    numeric_keys = [
        key
        for key, value in rows[0].items()
        if isinstance(value, (int, float)) and key not in {"elapsed_seconds"}
    ] if rows else []
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


def main() -> None:
    parser = argparse.ArgumentParser(description="同一RAW群でプリセットの機械評価を行います。")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("work/raw-evaluation"))
    parser.add_argument("--ids", nargs="*", help="評価する撮影ID。省略時は --limit で均等抽出")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--presets", nargs="*", choices=sorted(PRESETS), default=sorted(PRESETS))
    parser.add_argument("--preview", action="store_true", help="長辺1600pxのプレビューで評価")
    args = parser.parse_args()

    source_root = args.source_root.expanduser().resolve()
    if not source_root.is_dir():
        raise SystemExit(f"現像元フォルダがありません: {source_root}")
    available = _paired_ids(source_root)
    capture_ids = _select_ids(available, args.ids, args.limit)
    if not capture_ids:
        raise SystemExit("評価できるJPG/ARWペアがありません。")

    run_name = time.strftime("%Y%m%d-%H%M%S")
    run_root = args.output_root / run_name
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
                    metrics = _metrics(
                        _read_rgb(output_path),
                        _read_rgb(source_root / f"{capture_id}.JPG"),
                    )
                    row.update(metrics)
                    row["elapsed_seconds"] = float(result["elapsed_seconds"])
                    row["output"] = str(output_path)
                except Exception as error:  # keep one bad RAW from hiding the rest
                    row["error"] = f"{type(error).__name__}: {error}"
                rows.append(row)

    payload = {
        "source_root": str(source_root),
        "capture_ids": capture_ids,
        "presets": args.presets,
        "preview": bool(args.preview),
        "engine_version": engine.ENGINE_VERSION,
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
        "images": len(capture_ids),
        "presets": len(args.presets),
        "rows": len(rows),
        "errors": sum(bool(row.get("error")) for row in rows),
        "summary": payload["summary"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
