import { describe, expect, it } from "vitest";
import { createApi } from "./api";
import { generateSyntheticManifest, manifestStats } from "./manifest";
import type { BenchEnvironment } from "./metrics";
import type { BenchDriver, Observed } from "./scenarios";

const environment = (): BenchEnvironment => ({
  user_agent: "test",
  device_pixel_ratio: 1,
  viewport: { width: 1, height: 1 },
  hardware_concurrency: null,
  react_build: "production-profiling",
  is_tauri: false,
  longtask_supported: false,
  prefers_reduced_motion: false,
  thumb_cache_policy: null,
  thumb_cache_policy_source: "unobservable",
  thumb_cache_policy_note: "test",
});

/** 任意の await でブロックできる fake。nextFrame を手動で解放する。 */
function gatedDriver() {
  let release: (() => void) | null = null;
  let fail = false;
  let clock = 0;
  const observed: Observed = { started_at: 0, profiler: [], images: [], resources: [], long_tasks: null };
  const driver: BenchDriver = {
    now: () => clock,
    nextFrame: () =>
      new Promise<number>((resolve, reject) => {
        release = () => {
          clock += 16;
          if (fail) reject(new Error("driver 失敗"));
          else resolve(clock);
        };
      }),
    groupIds: () => [1, 2, 3],
    recommendedCaptureOf: (id) => `DSC${id}`,
    reset: async () => ({ waited_ms: 0, settled: true, pending: 0 }),
    openGroup: () => {},
    backToList: () => {},
    toggleAccept: () => {},
    unmountList: () => {},
    mountList: () => {},
    windowScrollMax: () => 0,
    scrollWindowTo: () => {},
    stripScrollMax: () => 0,
    scrollStripTo: () => {},
    dom: () => ({ elements: 1, imgs: 0, imgs_complete: 0, group_cards: 3, strip_cards: 0 }),
    beginObserve: () => {},
    observed: () => observed,
    endObserve: () => observed,
    waitImagesSettled: async () => ({ waited_ms: 0, settled: true, pending: 0 }),
  };
  return {
    driver,
    step: async () => {
      // 次の nextFrame が登録されるまで待つ。
      while (!release) await new Promise((resolve) => setTimeout(resolve, 0));
      const current = release;
      release = null;
      current();
    },
    failNext: () => {
      fail = true;
    },
    heal: () => {
      fail = false;
    },
  };
}

const makeApi = (driver: BenchDriver) =>
  createApi({
    driver,
    stats: manifestStats(generateSyntheticManifest({ id: "t", groupCount: 3, seed: 1, thumbPoolSize: 2, thumbPrefix: "t/thumbs" })),
    environment,
    boot: () => null,
    now: () => new Date("2026-10-24T00:00:00Z"),
  });

describe("createApi の同時実行ガード", () => {
  it("run / runAll の同時起動を拒否し、完了後は再び走らせられる", async () => {
    const gate = gatedDriver();
    const api = makeApi(gate.driver);
    const first = api.run("accept_toggle_list", { steps: 1 });
    await expect(api.run("accept_toggle_list", { steps: 1 })).rejects.toThrow("実行中");
    await expect(api.runAll()).rejects.toThrow("実行中");
    expect(() => api.clear()).toThrow("実行中");
    await gate.step();
    await gate.step();
    await first;
    const again = api.run("accept_toggle_list", { steps: 1 });
    await gate.step();
    await gate.step();
    await again;
    expect(api.report().scenarios).toHaveLength(2);
    expect(api.report().run_status).toBe("complete");
  });

  it("失敗してもガードを解放し、報告は incomplete で failures を残す(成功扱いにしない)", async () => {
    const gate = gatedDriver();
    const api = makeApi(gate.driver);
    gate.failNext();
    const failing = api.run("accept_toggle_list", { steps: 1 });
    const settled = failing.catch((error: Error) => error);
    await gate.step();
    expect(((await settled) as Error).message).toContain("driver 失敗");
    const report = api.report();
    expect(report.run_status).toBe("incomplete");
    expect(report.failures).toEqual([{ scenario: "accept_toggle_list", message: "driver 失敗" }]);
    expect(report.scenarios).toHaveLength(0);

    gate.heal();
    const retry = api.run("accept_toggle_list", { steps: 1 });
    await gate.step();
    await gate.step();
    await retry;
    // 一度失敗した報告は、成功した run が後から付いても incomplete のまま(clear するまで)。
    expect(api.report().run_status).toBe("incomplete");
    api.clear();
    expect(api.report().run_status).toBe("complete");
  });

  it("runAll は途中で失敗しても完了済みのシナリオを残し、incomplete にする", async () => {
    const gate = gatedDriver();
    const api = makeApi(gate.driver);
    const all = api.runAll({ steps: 1, mountRepeats: 1, maxScrollMs: 10 }).catch((error: Error) => error);
    // mount_list(1回)を通す: unmount 後 2 frame + mount 後 2 frame = 4 frame
    for (let i = 0; i < 4; i += 1) await gate.step();
    gate.failNext();
    await gate.step();
    expect(((await all) as Error).message).toContain("driver 失敗");
    const report = api.report();
    expect(report.run_status).toBe("incomplete");
    // mount_list は frame 4 つで完了、scroll_list は高さ 0 でフレームを使わず完了、select_group_strip の setup で失敗。
    expect(report.scenarios.map((result) => result.id)).toEqual(["mount_list", "scroll_list"]);
    expect(report.failures[0]?.scenario).toBe("runAll");
  });
});
