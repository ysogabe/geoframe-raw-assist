import { describe, expect, it } from "vitest";
import type { DomCounts } from "./metrics";
import { SCENARIO_IDS, runAllScenarios, runScenario, type BenchDriver, type Observed } from "./scenarios";

const FRAME = 1000 / 60;

/** 時計を自前で進める fake。操作のたびに 2ms 後にコミットがあったことにする(決定論的)。 */
function createFake(groupCount: number, options: { windowMax?: number; stripMax?: number } = {}) {
  let clock = 0;
  const calls: string[] = [];
  let observed: Observed = { started_at: 0, profiler: [], images: [], resources: [], long_tasks: null };
  let observing = false;
  const ids = Array.from({ length: groupCount }, (_, index) => index + 1);
  const commit = () => {
    if (observing) observed.profiler.push({ id: "root", phase: "update", actual_ms: 1.5, base_ms: 3, commit_at: clock + 2 });
  };
  const dom = (): DomCounts => ({ elements: 100, imgs: 10, imgs_complete: 10, group_cards: groupCount, strip_cards: 0 });
  const driver: BenchDriver = {
    now: () => clock,
    nextFrame: async () => {
      clock += FRAME;
      return clock;
    },
    groupIds: () => ids,
    recommendedCaptureOf: (groupId) => `DSC${groupId}`,
    reset: async () => void calls.push("reset"),
    openGroup: (groupId) => {
      calls.push(`open:${groupId}`);
      commit();
    },
    backToList: () => {
      calls.push("back");
      commit();
    },
    toggleAccept: (groupId) => {
      calls.push(`accept:${groupId}`);
      commit();
    },
    unmountList: () => void calls.push("unmount"),
    mountList: () => {
      calls.push("mount");
      commit();
    },
    windowScrollMax: () => options.windowMax ?? 3000,
    scrollWindowTo: (y) => void calls.push(`scrollY:${Math.round(y)}`),
    stripScrollMax: () => options.stripMax ?? 3000,
    scrollStripTo: (x) => void calls.push(`scrollX:${Math.round(x)}`),
    dom,
    beginObserve: () => {
      observing = true;
      observed = { started_at: clock, profiler: [], images: [], resources: [], long_tasks: null };
    },
    observed: () => observed,
    endObserve: () => {
      observing = false;
      return observed;
    },
    waitImagesSettled: async () => {
      clock += 250;
      return { waited_ms: 250, settled: true };
    },
  };
  return { driver, calls, ids };
}

describe("操作シナリオ", () => {
  it("帯の選択は先頭を開いてから順に選び、操作回数ぶんの sample を集める", async () => {
    const { driver, calls } = createFake(10);
    const result = await runScenario(driver, "select_group_strip", { steps: 25 });
    expect(calls.filter((call) => call.startsWith("open:")).slice(0, 4)).toEqual(["open:1", "open:2", "open:3", "open:4"]);
    expect(result.operations).toBe(25);
    expect(result.to_frame_ms?.n).toBe(25);
    expect(result.to_commit_ms?.n).toBe(25);
    // 2 回目の rAF までは 2 フレーム。
    expect(result.to_frame_ms?.p50).toBeCloseTo(FRAME * 2, 5);
    expect(result.to_commit_ms?.p50).toBeCloseTo(2, 5);
    // グループ数 10 < steps 25 は巡回するので明示する。
    expect(result.warnings.some((line) => line.includes("巡回"))).toBe(true);
    expect(result.to_frame_ms?.p95_reliable).toBe(true);
  });

  it("操作回数が少ないときは p95 が参考値だと警告に出す", async () => {
    const { driver } = createFake(50);
    const result = await runScenario(driver, "accept_toggle_list", { steps: 5 });
    expect(result.operations).toBe(5);
    expect(result.to_frame_ms?.p95_reliable).toBe(false);
    expect(result.warnings.some((line) => line.includes("n=5") && line.includes("参考値"))).toBe(true);
  });

  it("採用トグルは先頭から順に別グループへ、帯版は先に操作画面を開く", async () => {
    const list = createFake(6);
    await runScenario(list.driver, "accept_toggle_list", { steps: 3 });
    expect(list.calls.filter((call) => call.startsWith("accept:"))).toEqual(["accept:1", "accept:2", "accept:3"]);
    expect(list.calls.some((call) => call.startsWith("open:"))).toBe(false);

    const strip = createFake(6);
    await runScenario(strip.driver, "accept_toggle_strip", { steps: 3 });
    expect(strip.calls.indexOf("open:1")).toBeLessThan(strip.calls.indexOf("accept:1"));
  });

  it("一覧の mount は unmount と交互に繰り返し、画像が落ち着くまでの時間を別系列で持つ", async () => {
    const { driver, calls } = createFake(4);
    const result = await runScenario(driver, "mount_list", { mountRepeats: 3 });
    expect(calls.filter((call) => call === "unmount")).toHaveLength(3);
    expect(calls.filter((call) => call === "mount")).toHaveLength(3);
    expect(result.extra.images_settle_ms?.n).toBe(3);
    expect(result.warnings.some((line) => line.includes("cold"))).toBe(true);
  });

  it("戻って再表示は開く→戻るを繰り返す", async () => {
    const { driver, calls } = createFake(3);
    const result = await runScenario(driver, "return_to_list", { steps: 4 });
    expect(calls.filter((call) => call !== "reset")).toEqual(["open:1", "back", "open:2", "back", "open:3", "back", "open:1", "back"]);
    expect(result.operations).toBe(4);
  });
});

describe("スクロールシナリオ", () => {
  it("フレーム gap を集め、最後まで届く", async () => {
    const { driver, calls } = createFake(10, { windowMax: 1000 });
    const result = await runScenario(driver, "scroll_list", { scrollSpeed: 2400, maxScrollMs: 5000 });
    expect(calls.filter((call) => call.startsWith("scrollY:")).at(-1)).toBe("scrollY:1000");
    expect(result.frame_gaps?.n).toBe(result.operations);
    expect(result.frame_gaps?.p50).toBeCloseTo(FRAME, 5);
    expect(result.frame_gaps?.over_1_5_frames).toBe(0);
    expect(result.to_frame_ms).toBeNull();
  });

  it("スクロールできる高さがなければ理由を警告に出す", async () => {
    const { driver } = createFake(10, { windowMax: 0, stripMax: 0 });
    expect((await runScenario(driver, "scroll_list")).warnings.some((line) => line.includes("スクロールできる高さ"))).toBe(true);
    expect((await runScenario(driver, "scroll_strip")).warnings.some((line) => line.includes("横スクロール"))).toBe(true);
  });

  it("maxScrollMs で打ち切る", async () => {
    const { driver } = createFake(10, { windowMax: 1_000_000 });
    const result = await runScenario(driver, "scroll_list", { scrollSpeed: 100, maxScrollMs: 500 });
    expect(result.operations).toBeLessThanOrEqual(Math.ceil(500 / FRAME) + 2);
  });
});

describe("全シナリオ", () => {
  it("固定の順序で直列に走り、各シナリオの前に状態を戻す", async () => {
    const { driver, calls } = createFake(8);
    const results = await runAllScenarios(driver, { steps: 5, mountRepeats: 2, maxScrollMs: 500 });
    expect(results.map((result) => result.id)).toEqual([...SCENARIO_IDS]);
    expect(calls.filter((call) => call === "reset")).toHaveLength(SCENARIO_IDS.length);
    for (const result of results) {
      expect(result.params).toBeDefined();
      expect(result.dom_before).not.toBeNull();
    }
  });
});
