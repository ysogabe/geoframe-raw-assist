import { describe, expect, it } from "vitest";
import { adjacentGroupId, isEditableTarget } from "./group-nav";

const groups = [{ id: 10 }, { id: 3 }, { id: 7 }];

describe("adjacentGroupId", () => {
  it("並び順（ID順ではない）で前後を返す", () => {
    expect(adjacentGroupId(groups, 3, 1)).toBe(7);
    expect(adjacentGroupId(groups, 3, -1)).toBe(10);
  });
  it("両端では循環せず null", () => {
    expect(adjacentGroupId(groups, 10, -1)).toBeNull();
    expect(adjacentGroupId(groups, 7, 1)).toBeNull();
  });
  it("今のグループが並びに無ければ null", () => {
    expect(adjacentGroupId(groups, 99, 1)).toBeNull();
    expect(adjacentGroupId([], 1, -1)).toBeNull();
  });
});

describe("isEditableTarget", () => {
  // テスト環境は node（DOM なし）。closest を持つ最小のスタブで判定の分岐だけを見る。
  const stub = (matches: boolean, isContentEditable = false) =>
    ({ isContentEditable, closest: () => (matches ? {} : null) }) as unknown as EventTarget;

  it("要素でないもの（null / window 相当）は false", () => {
    expect(isEditableTarget(null)).toBe(false);
    expect(isEditableTarget({} as EventTarget)).toBe(false);
  });
  it("編集系セレクタに当たれば true", () => {
    expect(isEditableTarget(stub(true))).toBe(true);
    expect(isEditableTarget(stub(false))).toBe(false);
  });
  it("contenteditable の中は true", () => {
    expect(isEditableTarget(stub(false, true))).toBe(true);
  });
});
