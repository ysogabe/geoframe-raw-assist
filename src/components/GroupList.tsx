import type { ListView } from "../lib/app-state";
import { formatConfidence, groupKindLabel, rawPillClass } from "../lib/format";
import { assetUrl } from "../lib/raw-api";
import type { Analysis, CaptureId, GroupInfo } from "../lib/types";

type GroupListProps = {
  analysis: Analysis;
  visibleGroups: GroupInfo[];
  /** カードの代表として出すカット。表示カーソルであって採用そのものではない。 */
  viewingCaptureOf: (group: GroupInfo) => CaptureId;
  /** そのグループから採用している件数。1 グループから複数枚採用できる。 */
  acceptedCountOf: (group: GroupInfo) => number;
  /** 採用アイテムを持つグループ数（「採用済み」タブに出る件数と一致させる）。 */
  acceptedGroupCount: number;
  developedGroupIds: number[];
  activeView: ListView;
  onChangeView: (view: ListView) => void;
  onOpenGroup: (groupId: number) => void;
};

export function GroupList({
  analysis,
  visibleGroups,
  viewingCaptureOf,
  acceptedCountOf,
  acceptedGroupCount,
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
          <button className={activeView === "accepted" ? "active" : ""} onClick={() => onChangeView("accepted")}>採用済み <span>{acceptedGroupCount}</span></button>
        </div>
      </div>

      <div className="group-stack">
        {visibleGroups.length ? visibleGroups.map((group) => {
          const captureId = viewingCaptureOf(group);
          const hero = analysis.photos[captureId];
          const reason = group.recommendation_reasons[0]?.text ?? "";
          // 1 グループから複数枚採用できるようになったので、二値のバッジでは足りない。
          const acceptedCount = acceptedCountOf(group);
          return (
            <article key={group.id} className="group-card" onClick={() => onOpenGroup(group.id)}>
              <div className="group-hero">
                {hero ? (
                  <img src={assetUrl(hero.thumb)} alt={`${group.title} ${captureId}`} loading="lazy" decoding="async" />
                ) : null}
                <span className="photo-count">▧ {group.capture_ids.length}枚</span>
                {acceptedCount > 0 && (
                  <span className="accepted-badge">✓ {acceptedCount > 1 ? `採用 ${acceptedCount}件` : "採用済み"}</span>
                )}
              </div>
              <div className="group-copy">
                <div className="group-meta"><span>{groupKindLabel(group.kind)}</span><small>信頼度 {formatConfidence(group.confidence)}%</small></div>
                <h3>{group.title}</h3>
                <div className="recommendation"><span className="spark">✦</span><p><strong>{captureId}</strong><small>{reason}</small></p></div>
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
