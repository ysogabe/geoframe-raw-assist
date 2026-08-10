import type { ListView } from "../lib/app-state";
import { formatConfidence, groupKindLabel, rawPillClass } from "../lib/format";
import { assetUrl } from "../lib/raw-api";
import type { Analysis, CaptureId, GroupInfo } from "../lib/types";

type GroupListProps = {
  analysis: Analysis;
  visibleGroups: GroupInfo[];
  selectedCaptureOf: (group: GroupInfo) => CaptureId;
  acceptedGroupIds: number[];
  developedGroupIds: number[];
  activeView: ListView;
  onChangeView: (view: ListView) => void;
  onOpenGroup: (groupId: number) => void;
};

export function GroupList({
  analysis,
  visibleGroups,
  selectedCaptureOf,
  acceptedGroupIds,
  developedGroupIds,
  activeView,
  onChangeView,
  onOpenGroup,
}: GroupListProps) {
  return (
    <section className="review-list screen-enter">
      <div className="section-heading">
        <div><p className="eyebrow">SMART REVIEW</p><h2>おすすめを確認</h2></div>
        <div className="view-tabs" role="tablist" aria-label="表示切替">
          <button className={activeView === "suggestions" ? "active" : ""} onClick={() => onChangeView("suggestions")}>すべて <span>{analysis.groups.length}</span></button>
          <button className={activeView === "queue" ? "active" : ""} onClick={() => onChangeView("queue")}>現像済み <span>{developedGroupIds.length}</span></button>
          <button className={activeView === "accepted" ? "active" : ""} onClick={() => onChangeView("accepted")}>採用済み <span>{acceptedGroupIds.length}</span></button>
        </div>
      </div>

      <div className="group-stack">
        {visibleGroups.length ? visibleGroups.map((group) => {
          const captureId = selectedCaptureOf(group);
          const hero = analysis.photos[captureId];
          const reason = group.recommendation_reasons[0]?.text ?? "";
          return (
            <article key={group.id} className="group-card" onClick={() => onOpenGroup(group.id)}>
              <div className="group-hero">
                {hero ? (
                  <img src={assetUrl(hero.thumb)} alt={`${group.title} ${captureId}`} loading="lazy" decoding="async" />
                ) : null}
                <span className="photo-count">▧ {group.capture_ids.length}枚</span>
                {acceptedGroupIds.includes(group.id) && <span className="accepted-badge">✓ 採用済み</span>}
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
