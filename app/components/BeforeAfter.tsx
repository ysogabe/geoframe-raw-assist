import type { RefObject } from "react";
import { ADJUSTMENT_LABELS, formatAdjustmentValue, lensFlagLabels, lensReasonLabel } from "../lib/format";
import { sourceJpgUrl } from "../lib/raw-api";
import type { Adjustments, CaptureId, DevelopedResult } from "../lib/types";

type BeforeAfterProps = {
  captureId: CaptureId;
  result: DevelopedResult | undefined;
  comparisonRef: RefObject<HTMLDivElement | null>;
};

const RECIPE_KEYS: (keyof Adjustments)[] = [
  "exposure",
  "wb_temp",
  "shadow_lift",
  "highlight_recovery",
  "brightness",
  "contrast",
  "saturation",
  "sharpness",
  "noise_reduction",
];

export function BeforeAfter({ captureId, result, comparisonRef }: BeforeAfterProps) {
  if (!result) return null;

  const lens = result.lens_correction;
  const flags = lensFlagLabels(lens.flags);

  return (
    <div ref={comparisonRef} className="developed-comparison operation-comparison">
      <div className="developed-head">
        <div>
          <span className="comparison-check">✓</span>
          <strong>現像前後を比較</strong>
          {result.preview && <span className="preview-tag">プレビュー品質</span>}
        </div>
        <span>{result.preset_label}</span>
      </div>

      <div className="before-after-grid">
        <figure>
          <div className="comparison-image">
            <img src={sourceJpgUrl(result.capture_id)} alt={`${captureId} 現像前のJPG`} decoding="async" />
          </div>
          <figcaption><strong>現像前</strong><span>カメラ内 JPG</span></figcaption>
        </figure>
        <figure>
          <a className="comparison-image" href={result.output_url} target="_blank" rel="noreferrer">
            <img src={result.output_url} alt={`${captureId} RAW現像後`} decoding="async" />
            <span className="developed-label">原寸で見る ↗</span>
          </a>
          <figcaption><strong>現像後</strong><span>ARW / {result.preset_label}</span></figcaption>
        </figure>
      </div>

      <div className="recipe-badges">
        <span className={`recipe-badge ${lens.applied ? "ok" : ""}`}>
          {lens.applied ? `レンズ補正済み${flags ? `（${flags}）` : ""}` : `レンズ補正なし（${lensReasonLabel(lens.reason)}）`}
        </span>
        <span className={`recipe-badge ${result.exif_inherited ? "ok" : ""}`}>
          {result.exif_inherited ? "EXIF継承済み" : "EXIF継承なし"}
        </span>
        <span className="recipe-badge">レシピ {result.params_hash}</span>
      </div>
      {lens.applied && lens.lens && <p className="recipe-lens">{lens.lens}</p>}

      <dl className="recipe-grid">
        {RECIPE_KEYS.map((key) => (
          <div key={key}>
            <dt>{ADJUSTMENT_LABELS[key]}</dt>
            <dd>{formatAdjustmentValue(key, result.adjustments)}</dd>
          </div>
        ))}
      </dl>

      <div className="developed-meta">
        <span>{result.width} × {result.height}px</span>
        <span>{result.filename}</span>
        <span>{result.cached ? "保存済み結果" : `${result.elapsed_seconds.toFixed(1)}秒`}</span>
      </div>
    </div>
  );
}
