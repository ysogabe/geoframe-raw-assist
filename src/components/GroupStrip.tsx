import { useEffect, useRef } from "react";
import { formatClock, groupKindLabel } from "../lib/format";
import { assetUrl } from "../lib/raw-api";
import type { Analysis, CaptureId, GroupInfo } from "../lib/types";

type GroupStripProps = {
  analysis: Analysis;
  /**
   * 並べるグループ。**一覧のフィルタタブ（listView）は反映しない**。
   * 絞り込んだ結果を渡すと、絞り込みの外にあるグループへ移動できなくなる。
   */
  groups: GroupInfo[];
  selectedGroupId: number;
  /** ミニカードの代表として出すカット。一覧カードと同じ流儀（採用優先）。 */
  heroCaptureOf: (group: GroupInfo) => CaptureId;
  /** そのグループの採用の内訳。件数だけをバッジに出す。 */
  acceptedSummaryOf: (group: GroupInfo) => string[];
  onSelectGroup: (groupId: number) => void;
};

export function GroupStrip({
  analysis,
  groups,
  selectedGroupId,
  heroCaptureOf,
  acceptedSummaryOf,
  onSelectGroup,
}: GroupStripProps) {
  const stripRef = useRef<HTMLElement>(null);

  // 選択中のカードを常に見える位置へ寄せる。次々切り替えると選択が端へ流れていき、
  // 「今どこにいるのか」も「次がどれか」も見えなくなるため。
  // scrollIntoView は使わない。block: "nearest" でも window の縦スクロールに作用しうるので、
  // グループ切替時の scrollToTop()（window への smooth scroll）と競合する。
  // 横スクロールする要素（.group-strip）の scrollLeft だけを直接動かす。
  useEffect(() => {
    const strip = stripRef.current;
    const target = strip?.querySelector<HTMLElement>(`[data-group-id="${selectedGroupId}"]`);
    if (!strip || !target) return;
    const left = target.offsetLeft - (strip.clientWidth - target.offsetWidth) / 2;
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    strip.scrollTo({ left, behavior: reduceMotion ? "auto" : "smooth" });
  }, [selectedGroupId]);

  return (
    <nav className="group-strip" aria-label="提案グループの切り替え" ref={stripRef}>
      <ul className="group-strip-track">
        {groups.map((group) => {
          const heroCaptureId = heroCaptureOf(group);
          const hero = analysis.photos[heroCaptureId];
          const acceptedCount = acceptedSummaryOf(group).length;
          const selected = group.id === selectedGroupId;
          return (
            <li key={group.id} data-group-id={group.id}>
              <button
                type="button"
                className="group-strip-card"
                aria-current={selected ? "true" : undefined}
                title={group.title}
                onClick={() => {
                  // 再クリックで screen/operation を再ディスパッチすると presetName /
                  // overrides が「移動」の意味でリセットされ、手動調整が黙って消える。
                  if (!selected) onSelectGroup(group.id);
                }}
              >
                <span className="group-strip-thumb">
                  {hero ? (
                    <img src={assetUrl(hero.thumb)} alt="" loading="lazy" decoding="async" />
                  ) : null}
                  {acceptedCount > 0 && <span className="group-strip-accepted">✓{acceptedCount}</span>}
                </span>
                <span className="group-strip-label">
                  <strong>{formatClock(group.start_at)}</strong>
                  <small>{groupKindLabel(group.kind)} {group.capture_ids.length}枚</small>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
