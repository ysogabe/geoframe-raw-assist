import { describe, expect, it } from "vitest";
import { LAZY_LOAD_MARGIN_PX, isPendingImage, waitUntilSettled, type ImgSnapshot, type SettleEnv } from "./settle";

const viewport = { width: 1200, height: 800 };
const rect = (top: number, left = 0) => ({ top, bottom: top + 150, left, right: left + 200 });
const img = (overrides: Partial<ImgSnapshot>): ImgSnapshot => ({
  complete: false,
  currentSrc: "http://x/a.jpg",
  lazy: true,
  rect: rect(100),
  ...overrides,
});

describe("isPendingImage", () => {
  it("完了済み・要求前(currentSrc 空)は未完了に数えない", () => {
    expect(isPendingImage(img({ complete: true }), viewport)).toBe(false);
    expect(isPendingImage(img({ currentSrc: "" }), viewport)).toBe(false);
  });

  it("画面内・近傍の lazy は未完了、遠方の lazy は要求されていないとして除外する", () => {
    expect(isPendingImage(img({}), viewport)).toBe(true);
    expect(isPendingImage(img({ rect: rect(viewport.height + LAZY_LOAD_MARGIN_PX - 10) }), viewport)).toBe(true);
    expect(isPendingImage(img({ rect: rect(viewport.height + LAZY_LOAD_MARGIN_PX + 500) }), viewport)).toBe(false);
    // 横スクロールの帯: 画面右の遠方カード。
    expect(isPendingImage(img({ rect: rect(100, viewport.width + LAZY_LOAD_MARGIN_PX + 500) }), viewport)).toBe(false);
  });

  it("eager は位置によらず未完了になる", () => {
    expect(isPendingImage(img({ lazy: false, rect: rect(99999) }), viewport)).toBe(true);
  });
});

/** 時計を sleep で進める fake。pending は時刻の関数で与える。 */
function envOf(pendingAt: (t: number) => number, events: number[] = []): SettleEnv & { time(): number } {
  let t = 0;
  return {
    time: () => t,
    now: () => t,
    sleep: async (ms) => {
      t += ms;
    },
    lastImageEventAt: () => Math.max(0, ...events.filter((at) => at <= t)),
    pendingCount: () => pendingAt(t),
  };
}

describe("waitUntilSettled", () => {
  it("要求も無く quiet が過ぎれば速やかに settled", async () => {
    const result = await waitUntilSettled(envOf(() => 0), 3000, 200);
    expect(result).toMatchObject({ settled: true, pending: 0 });
    expect(result.waited_ms).toBeLessThanOrEqual(250);
  });

  it("200ms を超える遅い要求の間は、load イベントが来なくても settled にしない", async () => {
    // 1000ms まで 1 枚が未完了(その間イベントなし)。load は 1000ms に 1 回。
    const env = envOf((t) => (t < 1000 ? 1 : 0), [1000]);
    const result = await waitUntilSettled(env, 5000, 200);
    expect(result.settled).toBe(true);
    // quiet(200ms) だけなら 250ms 付近で抜けてしまう。load 後の quiet まで待つ。
    expect(result.waited_ms).toBeGreaterThanOrEqual(1000 + 200);
  });

  it("未完了が残ったまま timeout したら settled=false で枚数を返す", async () => {
    const result = await waitUntilSettled(envOf(() => 5), 1000, 200);
    expect(result).toMatchObject({ settled: false, pending: 5 });
    expect(result.waited_ms).toBeGreaterThanOrEqual(1000);
  });

  it("load が続いている間(quiet を満たさない)は待ち続ける", async () => {
    const events = Array.from({ length: 20 }, (_, index) => 100 + index * 100);
    const result = await waitUntilSettled(envOf(() => 0, events), 5000, 200);
    expect(result.settled).toBe(true);
    expect(result.waited_ms).toBeGreaterThanOrEqual(2100);
  });
});
