import type { ListView } from "../lib/app-state";
import { formatConfidence, groupKindLabel, rawPillClass } from "../lib/format";
import { assetUrl } from "../lib/raw-api";
import type { Analysis, CaptureId, GroupInfo } from "../lib/types";

type GroupListProps = {
  analysis: Analysis;
  visibleGroups: GroupInfo[];
  /** カードの代表として出すカット。採用があればその 1 件目、無ければ表示カーソル。 */
  heroCaptureOf: (group: GroupInfo) => CaptureId;
  /** 推薦カット。ヘロー（採用優先）とは別物なので分けて受け取る。 */
  recommendedCaptureOf: (group: GroupInfo) => CaptureId;
  /** そのグループの採用の内訳（例: ["DSC02943", "合成3枚"]）。件数もここから数える。 */
  acceptedSummaryOf: (group: GroupInfo) => string[];
  /**
   * 採用アイテムの総数。**グループ数ではない**。
   * 一覧バッジ・このタブ・Topbar の 3 か所で同じ数が出ていないと、
   * 「採用 1」と表示されたまま 2 枚書き出される、という食い違いが起きる。
   */
  acceptedItemCount: number;
  developedGroupIds: number[];
  activeView: ListView;
  onChangeView: (view: ListView) => void;
  onOpenGroup: (groupId: number) => void;
};

export function GroupList({
  analysis,
  visibleGroups,
  heroCaptureOf,
  recommendedCaptureOf,
  acceptedSummaryOf,
  acceptedItemCount,
  developedGroupIds,
  activeView,
  onChangeView,
  onOpenGroup,
}: GroupListProps) {
  return (
    <section className="review-list screen-enter">
      <div className="section-heading">
        <div><p className="eyebrow">SUGGESTIONS</p><h2>提案グループ一覧</h2></div>
        <div className="view-tabs" role="tablist" aria-label="表示切替">
          <button className={activeView === "suggestions" ? "active" : ""} onClick={() => onChangeView("suggestions")}>すべて <span>{analysis.groups.length}</span></button>
          <button className={activeView === "queue" ? "active" : ""} onClick={() => onChangeView("queue")}>現像済み <span>{developedGroupIds.length}</span></button>
          <button className={activeView === "accepted" ? "active" : ""} onClick={() => onChangeView("accepted")}>採用 <span>{acceptedItemCount}</span></button>
        </div>
      </div>

      <div className="group-stack">
        {visibleGroups.length ? visibleGroups.map((group) => {
          const heroCaptureId = heroCaptureOf(group);
          const hero = analysis.photos[heroCaptureId];
          // 推薦の行はあくまで「解析のおすすめ」。ヘローが採用側に寄っても動かさない。
          const recommendedCaptureId = recommendedCaptureOf(group);
          const reason = group.recommendation_reasons[0]?.text ?? "";
          // 1 グループから複数枚採用できるので、バッジは二値ではなく件数を出す。
          const acceptedSummary = acceptedSummaryOf(group);
          return (
            <article key={group.id} className="group-card" onClick={() => onOpenGroup(group.id)}>
              <div className="group-hero">
                {hero ? (
                  <img src={assetUrl(hero.thumb)} alt={`${group.title} ${heroCaptureId}`} loading="lazy" decoding="async" />
                ) : null}
                <span className="photo-count">▧ {group.capture_ids.length}枚</span>
                {acceptedSummary.length > 0 && (
                  <span className="accepted-badge">✓ 採用 {acceptedSummary.length}件</span>
                )}
              </div>
              <div className="group-copy">
                <div className="group-meta"><span>{groupKindLabel(group.kind)}</span><small>信頼度 {formatConfidence(group.confidence)}%</small></div>
                <h3>{group.title}</h3>
                <div className="recommendation"><span className="spark">✦</span><p><strong>{recommendedCaptureId}</strong><small>{reason}</small></p></div>
                {/* 何を採用したのかは件数だけでは分からない。カードを開かずに読めるようにする。 */}
                {acceptedSummary.length > 0 && (
                  <p className="accepted-summary"><span aria-hidden="true">✓</span>採用: {acceptedSummary.join(" / ")}</p>
                )}
                <div className="group-footer">
                  <span className={rawPillClass(group.raw.level)}>{group.raw.label}</span>
                  <button onClick={(event) => { event.stopPropagation(); onOpenGroup(group.id); }}>操作パネルを開く <b>→</b></button>
                </div>
              </div>
            </article>
          );
        }) : (
          <div className="empty-state"><span>○</span><h3>まだ写真がありません</h3><p>別の表示に切り替えるか、提案から写真を採用してください。</p></div>
        )}
      </div>
    </section>
  );
}
