import type { Group } from "../lib/types";
import { rawClass } from "../lib/types";

type GroupListProps = {
  groups: Group[];
  visibleGroups: Group[];
  selectedShots: Record<number, string>;
  accepted: number[];
  queued: number[];
  activeView: "suggestions" | "queue" | "accepted";
  onChangeView: (view: "suggestions" | "queue" | "accepted") => void;
  onOpenGroup: (groupId: number) => void;
};

export function GroupList({ groups, visibleGroups, selectedShots, accepted, queued, activeView, onChangeView, onOpenGroup }: GroupListProps) {
  return (
    <section className="review-list screen-enter">
      <div className="section-heading">
        <div><p className="eyebrow">SMART REVIEW</p><h2>おすすめを確認</h2></div>
        <div className="view-tabs" role="tablist" aria-label="表示切替">
          <button className={activeView === "suggestions" ? "active" : ""} onClick={() => onChangeView("suggestions")}>すべて <span>{groups.length}</span></button>
          <button className={activeView === "queue" ? "active" : ""} onClick={() => onChangeView("queue")}>現像済み <span>{queued.length}</span></button>
          <button className={activeView === "accepted" ? "active" : ""} onClick={() => onChangeView("accepted")}>採用済み <span>{accepted.length}</span></button>
        </div>
      </div>

      <div className="group-stack">
        {visibleGroups.length ? visibleGroups.map((group) => {
          const hero = group.shots.find((shot) => shot.id === selectedShots[group.id]) ?? group.shots[0];
          return (
            <article key={group.id} className="group-card" onClick={() => onOpenGroup(group.id)}>
              <div className="group-hero">
                <img src={hero.image} alt={`${group.title} ${hero.id}`} />
                <span className="photo-count">▧ {group.shots.length}枚</span>
                {accepted.includes(group.id) && <span className="accepted-badge">✓ 採用済み</span>}
              </div>
              <div className="group-copy">
                <div className="group-meta"><span>{group.kind}</span><small>{group.time}</small></div>
                <h3>{group.title}</h3>
                <div className="recommendation"><span className="spark">✦</span><p><strong>{selectedShots[group.id]}</strong><small>{group.reason}</small></p></div>
                <div className="group-footer"><span className={rawClass(group.raw)}>{group.rawLabel}</span><button onClick={(event) => { event.stopPropagation(); onOpenGroup(group.id); }}>操作パネルを開く <b>→</b></button></div>
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
