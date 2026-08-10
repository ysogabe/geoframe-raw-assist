export function SummaryStrip() {
  return (
    <section className="summary-strip" aria-label="分析サマリー">
      <div className="summary-lead"><span className="pulse">✓</span><div><strong>281組のペアを解析しました</strong><small>JPGとARWの欠落はありません</small></div></div>
      <div className="summary-metric"><strong>54</strong><span>撮影グループ</span></div>
      <div className="summary-metric"><strong>18</strong><span>RAW現像候補</span></div>
      <div className="summary-metric"><strong>6</strong><span>確認が必要</span></div>
      <div className="progress-ring" aria-label="分析完了 100%"><span>100<small>%</small></span></div>
    </section>
  );
}
