/**
 * 操作パネルのグループ送り（←→ キー / グループ帯の ◀▶）で使う純粋関数。
 */

/**
 * 並び順で隣にあるグループのID。**両端では循環しない**（null を返す）。
 * 158 グループを送っていて末尾から先頭へ無言で飛ぶと、「どこまで見たか」が分からなくなる。
 * 今のグループが並びに無い（再解析で消えた等）ときも、どこへ送るべきか決められないので null。
 */
export function adjacentGroupId(
  groups: readonly { id: number }[],
  currentId: number,
  direction: -1 | 1,
): number | null {
  const index = groups.findIndex((group) => group.id === currentId);
  if (index < 0) return null;
  return groups[index + direction]?.id ?? null;
}

/**
 * キー入力を横取りしてはいけない要素か。スライダー（DevelopControls の type="range"）は
 * 矢印キーで値を動かすし、テキスト入力ではカーソル移動に使う。ここで拾うと、調整中に
 * グループが勝手に切り替わる。
 */
export function isEditableTarget(target: EventTarget | null): boolean {
  if (!target || typeof (target as Element).closest !== "function") return false;
  const element = target as HTMLElement;
  if (element.isContentEditable) return true;
  return (
    element.closest(
      [
        "input",
        "select",
        "textarea",
        '[contenteditable]:not([contenteditable="false"])',
        '[role="slider"]',
        '[role="spinbutton"]',
        '[role="textbox"]',
        '[role="combobox"]',
        '[role="listbox"]',
        '[role="radiogroup"]',
        '[role="tablist"]',
        '[role="menu"]',
      ].join(", "),
    ) !== null
  );
}
