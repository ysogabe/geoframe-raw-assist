import type { RefObject } from "react";
import { LOCAL_RAW_API } from "../lib/raw-api";
import type { DevelopedResult } from "../lib/types";

type BeforeAfterProps = {
  shotId: string;
  result: DevelopedResult | undefined;
  comparisonRef: RefObject<HTMLDivElement | null>;
};

export function BeforeAfter({ shotId, result, comparisonRef }: BeforeAfterProps) {
  if (!result) return null;

  return (
    <div ref={comparisonRef} className="developed-comparison operation-comparison">
      <div className="developed-head"><div><span className="comparison-check">✓</span><strong>現像前後を比較</strong></div><span>{result.preset_label}</span></div>
      <div className="before-after-grid">
        <figure><div className="comparison-image"><img src={`${LOCAL_RAW_API}/source-jpg/${shotId}.JPG`} alt={`${shotId} 現像前のJPG`} /></div><figcaption><strong>現像前</strong><span>カメラ内 JPG</span></figcaption></figure>
        <figure><a className="comparison-image" href={result.output_url} target="_blank" rel="noreferrer"><img src={result.output_url} alt={`${shotId} RAW現像後`} /><span className="developed-label">原寸で見る ↗</span></a><figcaption><strong>現像後</strong><span>ARW / {result.preset_label}</span></figcaption></figure>
      </div>
      <div className="developed-meta"><span>{result.width} × {result.height}px</span><span>{result.cached ? "保存済み結果" : `${result.elapsed_seconds.toFixed(1)}秒`}</span></div>
    </div>
  );
}
