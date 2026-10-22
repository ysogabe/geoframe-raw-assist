import type { RefObject } from "react";
import {
  ADJUSTMENT_LABELS,
  formatAdjustmentValue,
  formatHighlightRecoveryEffect,
  isMergedResult,
  lensFlagLabels,
  lensReasonLabel,
} from "../lib/format";
import { sourceJpgUrl } from "../lib/raw-api";
import type { Adjustments, CaptureId, DevelopedResult, MergedResult } from "../lib/types";

/**
 * 比較エリアのタブ。表示状態そのものは State の `viewing` が持っており、
 * ここはその写像を受け取るだけの見た目用の型（state の形は知らない）。
 */
export type ComparisonTab = "develop" | "merged";

type BeforeAfterProps = {
  captureId: CaptureId;
  /** 単写の現像結果、またはブラケット合成の結果。 */
  result: DevelopedResult | MergedResult | undefined;
  comparisonRef: RefObject<HTMLDivElement | null>;
  /**
   * 現像結果と合成結果の両方があるときだけ渡す表示切替。
   * 片方しか無いときは省略し、押しても何も変わらないタブを出さない。
   */
  view?: ComparisonTab;
  onChangeView?: (view: ComparisonTab) => void;
  /**
   * 現像後を原寸ビューアで開く。押されたことを伝えるだけにして、どの 2 面を渡すかは
   * OperationPanel が決める（この画面が state の形を知らずに済む）。
   */
  onOpenFullSize: () => void;
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

export function BeforeAfter({
  captureId,
  result,
  comparisonRef,
  view,
  onChangeView,
  onOpenFullSize,
}: BeforeAfterProps) {
  if (!result) return null;

  const lens = result.lens_correction;
  const flags = lensFlagLabels(lens.flags);
  const merged = isMergedResult(result) ? result : null;
  // 合成結果は 1 枚の capture_id を持たない。現像前として並べるのは選択中のカット。
  // ここは `merged` ではなくガードで書く（変数経由だと result の型が絞られない）。
  const beforeCaptureId = isMergedResult(result) ? captureId : result.capture_id;
  const canToggle = Boolean(view && onChangeView);
  // プレビュー・本現像とも Core Image。合成結果には engine がないので型を絞る。
  const coreImage = !isMergedResult(result) && result.engine === "coreimage";
  const highlightNote = coreImage && !isMergedResult(result) ? formatHighlightRecoveryEffect(result.highlight_recovery_effect) : null;

  return (
    <div ref={comparisonRef} className="developed-comparison operation-comparison">
      {canToggle && (
        <div className="comparison-switch" role="tablist" aria-label="表示する結果">
          <button
            role="tab"
            aria-selected={view === "develop"}
            className={view === "develop" ? "active" : ""}
            onClick={() => onChangeView?.("develop")}
          >
            単写の現像
          </button>
          <button
            role="tab"
            aria-selected={view === "merged"}
            className={view === "merged" ? "active" : ""}
            onClick={() => onChangeView?.("merged")}
          >
            ブラケット合成
          </button>
        </div>
      )}

      <div className="developed-head">
        <div>
          <span className="comparison-check">✓</span>
          <strong>{merged ? "合成前後を比較" : "現像前後を比較"}</strong>
          {merged && <span className="merged-tag">合成 {merged.frames}枚</span>}
          {merged && (
            <span className={`merged-tag${merged.aligned ? " ok" : ""}`}>
              {merged.aligned ? "位置合わせ済み" : "位置合わせなし"}
            </span>
          )}
          {result.preview && <span className="preview-tag">プレビュー品質</span>}
          {coreImage && <span className="preview-tag">Core Image {result.preview ? "プレビュー" : "本現像"}</span>}
        </div>
        <span>{result.preset_label}</span>
      </div>

      <div className="before-after-grid">
        <figure>
          <div className="comparison-image">
            <img src={sourceJpgUrl(beforeCaptureId)} alt={`${beforeCaptureId} 現像前のJPG`} decoding="async" />
          </div>
          <figcaption><strong>現像前</strong><span>カメラ内 JPG（{beforeCaptureId}）</span></figcaption>
        </figure>
        <figure>
          <button type="button" className="comparison-image" onClick={onOpenFullSize}>
            <img
              src={result.output_url}
              alt={merged ? `${merged.frames}枚のブラケット合成後` : `${captureId} RAW現像後`}
              decoding="async"
            />
            <span className="developed-label">原寸で見る</span>
          </button>
          <figcaption>
            <strong>{merged ? "合成後" : "現像後"}</strong>
            <span>{merged ? `ARW ${merged.frames}枚を合成 / ${result.preset_label}` : `ARW / ${result.preset_label}`}</span>
          </figcaption>
        </figure>
      </div>

      {merged && <p className="merged-frames">使用フレーム: {merged.capture_ids.join(" / ")}</p>}

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

      {coreImage && (
        <>
          {/* highlight_recovery は CLI 0.6.0（#81）で露出と暗部の持ち上げに写像して加算される。
              スライダーの値と実際に描いた値がずれるので、hr > 0 のときだけ実効値を出す。 */}
          {highlightNote && <p className="engine-note">{highlightNote}</p>}
        </>
      )}

      <div className="developed-meta">
        <span>{result.width} × {result.height}px</span>
        <span>{result.filename}</span>
        <span>{result.cached ? "保存済み結果" : `${result.elapsed_seconds.toFixed(1)}秒`}</span>
      </div>
    </div>
  );
}
