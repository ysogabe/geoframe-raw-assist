import { describe, expect, it } from "vitest";
import { gapsOf, percentile, summarizeFrameGaps, summarizeImages, summarizeProfilerById, summarizeSamples } from "./metrics";

describe("percentile / summarizeSamples", () => {
  it("nearest-rank で求める", () => {
    const values = Array.from({ length: 100 }, (_, index) => index + 1);
    expect(percentile(values, 50)).toBe(50);
    expect(percentile(values, 95)).toBe(95);
    expect(percentile(values, 99)).toBe(99);
    expect(percentile([], 95)).toBeNull();
  });

  it("n が少ないと p95/p99 を信頼できないと明示する", () => {
    expect(summarizeSamples([5, 1, 3])).toMatchObject({ n: 3, p50: 3, p95: 5, max: 5, p95_reliable: false, p99_reliable: false });
    const twenty = summarizeSamples(Array.from({ length: 20 }, (_, index) => index));
    expect(twenty.p95_reliable).toBe(true);
    expect(twenty.p99_reliable).toBe(false);
    expect(summarizeSamples(Array.from({ length: 100 }, () => 1)).p99_reliable).toBe(true);
  });

  it("空・非有限値を安全に扱う", () => {
    expect(summarizeSamples([])).toMatchObject({ n: 0, p50: null, mean: null, max: null });
    expect(summarizeSamples([1, Number.NaN, Number.POSITIVE_INFINITY]).n).toBe(1);
  });
});

describe("フレーム gap", () => {
  it("タイムスタンプ列から隣との差を取る", () => {
    expect(gapsOf([0, 16, 50, 52])).toEqual([16, 34, 2]);
    expect(gapsOf([5])).toEqual([]);
  });

  it("落ちたフレームの数を数える", () => {
    const summary = summarizeFrameGaps([16.7, 16.7, 40, 120, 16.7]);
    expect(summary).toMatchObject({ n: 5, over_1_5_frames: 2, over_33ms: 2, over_100ms: 1, max: 120 });
  });
});

describe("Profiler / 画像の集計", () => {
  it("id ごとに commit 数・mount/update を分ける", () => {
    const summary = summarizeProfilerById([
      { id: "list", phase: "mount", actual_ms: 10, base_ms: 10, commit_at: 1 },
      { id: "list", phase: "update", actual_ms: 4, base_ms: 9, commit_at: 2 },
      { id: "root", phase: "update", actual_ms: 5, base_ms: 9, commit_at: 2 },
    ]);
    expect(Object.keys(summary)).toEqual(["list", "root"]);
    expect(summary.list).toMatchObject({ commits: 2, mounts: 1, updates: 1, actual_total_ms: 14 });
  });

  it("キャッシュ由来らしい読み込みを推定として数える", () => {
    const summary = summarizeImages(
      [
        { at: 110, ok: true },
        { at: 150, ok: true },
        { at: 160, ok: false },
      ],
      [
        { duration_ms: 3, transfer_size: 0, decoded_body_size: 9000 },
        { duration_ms: 30, transfer_size: 4000, decoded_body_size: 9000 },
      ],
      100,
    );
    expect(summary).toMatchObject({ loaded: 2, errored: 1, last_load_after_ms: 50, resource_entries: 2, cache_like: 1 });
  });
});

describe("summarizeImages の大量イベント（Math.max(...) 引数上限の回帰）", () => {
  // Math.max(...loadTimes) は引数数が数万〜十数万で RangeError になる。reduce 版で落ちないことを見る。
  const BULK = 200_000;

  it("200,000 件超の成功/失敗の混在でも落ちず、件数と最後の load 時刻を返す", () => {
    // at=i の列（i%3===0 は失敗）の先頭に、最大時刻の成功を1件足す。最大時刻は末尾ではない。
    const events = [{ at: 999_999, ok: true }, ...Array.from({ length: BULK }, (_, index) => ({ at: index, ok: index % 3 !== 0 }))];
    const summary = summarizeImages(events, [], 100);
    expect(summary).toMatchObject({
      loaded: 133_334,
      errored: 66_667,
      last_load_after_ms: 999_899,
    });
  });

  it("成功が 0 件なら last_load_after_ms は null", () => {
    const events = Array.from({ length: BULK + 1 }, (_, index) => ({ at: index, ok: false }));
    expect(summarizeImages(events, [], 0)).toMatchObject({ loaded: 0, errored: BULK + 1, last_load_after_ms: null });
  });

  it("小さい配列では最大時刻が末尾でなくても従来の最大値と一致する", () => {
    const summary = summarizeImages(
      [
        { at: 300, ok: true },
        { at: 20, ok: true },
        { at: 10, ok: false },
        { at: 120, ok: true },
      ],
      [],
      10,
    );
    expect(summary).toMatchObject({ loaded: 3, errored: 1, last_load_after_ms: 290 });
  });
});
