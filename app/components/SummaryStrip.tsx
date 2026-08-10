import type { Analysis } from "../lib/types";

type SummaryStripProps = {
  analysis: Analysis;
};

export function SummaryStrip({ analysis }: SummaryStripProps) {
  const { source, summary } = analysis;
  const missing = source.jpg_only + source.arw_only;

  return (
    <section className="summary-strip" aria-label="分析サマリー">
      <div className="summary-lead">
        <span className="pulse">✓</span>
        <div>
          <strong>{source.pairs}組のペアを解析しました</strong>
          <small>
            {missing === 0
              ? "JPGとARWの欠落はありません"
              : `JPGのみ ${source.jpg_only}件 / ARWのみ ${source.arw_only}件`}
          </small>
        </div>
      </div>
      <div className="summary-metric"><strong>{summary.groups}</strong><span>撮影グループ</span></div>
      <div className="summary-metric"><strong>{summary.raw_candidates}</strong><span>RAW現像候補</span></div>
      <div className="summary-metric"><strong>{summary.needs_review}</strong><span>確認が必要</span></div>
      <div className="progress-ring" aria-label="分析完了 100%"><span>100<small>%</small></span></div>
    </section>
  );
}
