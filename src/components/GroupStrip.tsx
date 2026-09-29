import { useEffect, useRef, useState } from "react";
import { formatClock, groupKindLabel } from "../lib/format";
import { adjacentGroupId } from "../lib/group-nav";
import { assetUrl } from "../lib/raw-api";
import type { Analysis, CaptureId, GroupInfo } from "../lib/types";

// 帯が固定されてから、さらにこれだけ（px）スクロールしたらコンパクト表示へ倒す。
// 戻すのは帯が固定を外れたとき（下の sentinel が見えたとき）。
// 縮むと文書の高さが「通常表示とコンパクト表示の高さの差」ぶん減り、ページ下端付近では
// スクロール位置がその分クランプされて戻される。入る閾値がその差より小さいと、クランプで
// 判定が戻って通常表示に復帰 → 高さが戻る → …と縮小/復帰を繰り返してチカチカする。
// なので入る閾値は差（約 85px）より十分大きく取る。
const COMPACT_ENTER_OFFSET = 140;

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
  /** カードのクリックでの移動。 */
  onSelectGroup: (groupId: number) => void;
  /** 左端の ◀▶ での移動（←→ キーと同じ操作）。 */
  onStepGroup: (groupId: number) => void;
};

export function GroupStrip({
  analysis,
  groups,
  selectedGroupId,
  heroCaptureOf,
  acceptedSummaryOf,
  onSelectGroup,
  onStepGroup,
}: GroupStripProps) {
  const stripRef = useRef<HTMLElement>(null);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);
  const [compact, setCompact] = useState(false);

  const index = groups.findIndex((group) => group.id === selectedGroupId);
  const prevId = adjacentGroupId(groups, selectedGroupId, -1);
  const nextId = adjacentGroupId(groups, selectedGroupId, 1);

  // 選択中のカードを常に見える位置へ寄せる。次々切り替えると選択が端へ流れていき、
  // 「今どこにいるのか」も「次がどれか」も見えなくなるため。
  // scrollIntoView は使わない。block: "nearest" でも window の縦スクロールに作用しうるので、
  // グループ切替時の scrollToTop()（window への smooth scroll）と競合する。
  // 横スクロールする要素（.group-strip-scroller）の scrollLeft だけを直接動かす。
  // コンパクト表示の切替でもカード幅が変わるので、寄せ直す。
  useEffect(() => {
    const scroller = scrollerRef.current;
    const target = scroller?.querySelector<HTMLElement>(`[data-group-id="${selectedGroupId}"]`);
    if (!scroller || !target) return;
    const left = target.offsetLeft - (scroller.clientWidth - target.offsetWidth) / 2;
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    scroller.scrollTo({ left, behavior: reduceMotion ? "auto" : "smooth" });
  }, [selectedGroupId, compact]);

  // 固定中（ページを縦に送っている間）はサムネを落としたチップ表示に縮める。
  // 判定は帯の **外側** に置いた sentinel（帯の上の余白を兼ねる）で行う。帯自身の位置や
  // 高さで判定すると、縮んだ結果で判定が変わって表示が振動する。sentinel は帯より上に
  // あるので、帯の高さが変わっても位置は動かない。
  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel) return;
    let frame = 0;
    const update = () => {
      frame = 0;
      // sentinel の下端 = 帯の元の上端。0 以下なら帯は固定されている。
      const bottom = sentinel.getBoundingClientRect().bottom;
      setCompact((current) => (current ? bottom <= 0 : bottom < -COMPACT_ENTER_OFFSET));
    };
    const onScroll = () => {
      if (frame === 0) frame = window.requestAnimationFrame(update);
    };
    update();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      window.removeEventListener("scroll", onScroll);
      if (frame !== 0) window.cancelAnimationFrame(frame);
    };
  }, []);

  // 帯の実寸を --strip-h として公開する。右カードの sticky 位置と、選択写真の
  // max-height がこれを引く（帯の下に潜らせないため）。帯と右カードは兄弟ではなく
  // 祖先も共有しないので、:root に置く。コンパクト/通常の切替もここで追従する。
  useEffect(() => {
    const strip = stripRef.current;
    if (!strip) return;
    const root = document.documentElement;
    const apply = () => root.style.setProperty("--strip-h", `${strip.offsetHeight}px`);
    apply();
    const observer = new ResizeObserver(apply);
    observer.observe(strip);
    return () => {
      observer.disconnect();
      root.style.removeProperty("--strip-h");
    };
  }, []);

  return (
    <>
      <div className="group-strip-sentinel" ref={sentinelRef} aria-hidden="true" />
      <nav
        className={`group-strip${compact ? " is-compact" : ""}`}
        aria-label="提案グループの切り替え"
        ref={stripRef}
      >
        <div className="group-strip-nav">
          <button
            type="button"
            aria-label="前のグループ"
            disabled={prevId === null}
            onClick={() => {
              if (prevId !== null) onStepGroup(prevId);
            }}
          >
            ◀
          </button>
          <span className="group-strip-position" aria-live="polite">
            {index + 1}/{groups.length}
          </span>
          <button
            type="button"
            aria-label="次のグループ"
            disabled={nextId === null}
            onClick={() => {
              if (nextId !== null) onStepGroup(nextId);
            }}
          >
            ▶
          </button>
          <small className="group-strip-hint">←→ で切替</small>
        </div>
        {/* position: relative は自動スクロール用。カードの offsetLeft がこの要素
            （＝横スクロールする要素）基準になり、scrollLeft とそのまま揃う。 */}
        <div className="group-strip-scroller" ref={scrollerRef}>
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
                      <small>
                        {groupKindLabel(group.kind)}
                        <span className="group-strip-count"> {group.capture_ids.length}枚</span>
                      </small>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      </nav>
    </>
  );
}
