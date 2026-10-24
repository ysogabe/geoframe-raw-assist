import { describe, expect, it } from "vitest";
import {
  createPreviewTimingRecorder,
  PAINT_PROXY_NOTE,
  type PreviewInvokeOutcome,
  type PreviewTimingRow,
} from "./preview-timing";

/** 手動で進める時計・rAF・タイマー。 */
function harness(enabled = true) {
  let clock = 0;
  const rows: PreviewTimingRow[] = [];
  const frames: (() => void)[] = [];
  const timers = new Map<number, { at: number; fn: () => void }>();
  let timerId = 0;
  let idSeq = 0;
  const recorder = createPreviewTimingRecorder({
    enabled: () => enabled,
    now: () => clock,
    requestFrame: (cb) => frames.push(cb),
    setTimer: (fn, ms) => {
      timerId += 1;
      timers.set(timerId, { at: clock + ms, fn });
      return timerId;
    },
    clearTimer: (h) => void timers.delete(h as number),
    newId: () => `id-${(idSeq += 1)}`,
    sink: (row) => rows.push(row),
  });
  return {
    recorder,
    rows,
    advance: (ms: number) => {
      clock += ms;
      for (const [id, t] of [...timers]) {
        if (t.at <= clock) {
          timers.delete(id);
          t.fn();
        }
      }
    },
    flushFrame: () => frames.splice(0).forEach((f) => f()),
    pendingTimers: () => timers.size,
  };
}

const CTX = { captureId: "DSC01234" };

const OUTCOME: PreviewInvokeOutcome = {
  displayUrl: "asset://localhost/PRIVATE/path/DSC01234_preview.jpg",
  cacheHit: false,
  elapsed: { decode: 10, render_encode: 90, total: 100 },
  width: 2400,
  height: 1600,
};

describe("preview-timing", () => {
  it("既定 OFF では begin が null で、何も記録しない", () => {
    const h = harness(false);
    expect(h.recorder.begin(CTX)).toBeNull();
    expect(h.recorder.now()).toBe(0);
    h.recorder.imageLoaded(OUTCOME.displayUrl, () => Promise.resolve());
    expect(h.rows).toEqual([]);
  });

  it("invoke 戻り → load → decode → 2 回の rAF を requestID の 1 行に集約する", async () => {
    const h = harness();
    const timer = h.recorder.begin(CTX)!;
    h.advance(150); // invoke 150ms
    timer.invokeReturned(OUTCOME);
    h.advance(20);
    h.recorder.imageLoaded(OUTCOME.displayUrl, () => new Promise((r) => setTimeout(r, 0)));
    h.advance(5);
    await new Promise((r) => setTimeout(r, 5));
    h.advance(7);
    h.flushFrame(); // 1 回目
    h.advance(16);
    h.flushFrame(); // 2 回目
    expect(h.rows).toHaveLength(1);
    const row = h.rows[0];
    expect(row.request_id).toBe("id-1");
    expect(row.status).toBe("ok");
    expect(row.invoke_wall_ms).toBe(150);
    expect(row.invoke_to_load_ms).toBe(20);
    expect(row.invoke_minus_cli_ms).toBe(50);
    expect(row.cache_hit).toBe(false);
    expect(row.invoke_start_to_raf_ms).toBe(row.invoke_wall_ms + row.invoke_to_load_ms! + row.load_to_decode_ms! + row.decode_to_raf_ms!);
    expect(row.paint_proxy).toBe(PAINT_PROXY_NOTE);
    expect(h.pendingTimers()).toBe(0);
  });

  it("ボタンのハンドラ入口を起点にすると pre_invoke と origin_to_raf を出す", async () => {
    const h = harness();
    h.advance(100); // ハンドラ入口の時刻
    const handlerEntryMs = h.recorder.now();
    h.advance(8); // dispatch など
    const timer = h.recorder.begin({ captureId: "DSC01234", handlerEntryMs })!;
    h.advance(50);
    timer.invokeReturned(OUTCOME);
    h.advance(10);
    h.recorder.imageLoaded(OUTCOME.displayUrl, () => Promise.resolve());
    await Promise.resolve();
    await Promise.resolve();
    h.advance(4);
    h.flushFrame();
    h.advance(16);
    h.flushFrame();
    const row = h.rows[0];
    expect(row).toMatchObject({ origin: "button_handler", kind: "manual_preview_button_to_image", pre_invoke_ms: 8 });
    expect(row.origin_to_raf_ms).toBe(row.pre_invoke_ms! + row.invoke_start_to_raf_ms!);
    expect(row.origin_to_raf_ms).toBe(88);
  });

  it("起点が渡されない（API 呼び出し起点）ときは origin=api_call で、ボタン→画像とは呼ばない", async () => {
    const h = harness();
    h.recorder.begin(CTX)!.invokeFailed("BUSY");
    expect(h.rows[0]).toMatchObject({ origin: "api_call", kind: "api_call_to_image", pre_invoke_ms: null });
  });

  it("キャッシュヒットは invoke_minus_cli を出さない（元の CLI 時間と混ぜない）", async () => {
    const h = harness();
    const timer = h.recorder.begin(CTX)!;
    h.advance(12);
    timer.invokeReturned({ ...OUTCOME, cacheHit: true });
    h.recorder.imageLoaded(OUTCOME.displayUrl, () => Promise.resolve());
    await Promise.resolve();
    await Promise.resolve();
    h.flushFrame();
    h.flushFrame();
    expect(h.rows[0].cache_hit).toBe(true);
    expect(h.rows[0].invoke_minus_cli_ms).toBeNull();
  });

  it("load が来ないときはタイムアウトで image_not_observed として閉じる", () => {
    const h = harness();
    h.recorder.begin(CTX)!.invokeReturned(OUTCOME);
    h.advance(3999);
    expect(h.rows).toEqual([]);
    h.advance(2);
    expect(h.rows).toHaveLength(1);
    expect(h.rows[0].status).toBe("image_not_observed");
    expect(h.rows[0].invoke_start_to_raf_ms).toBeNull();
  });

  it("別 URL の load は無視し、待機中の計測を壊さない", () => {
    const h = harness();
    h.recorder.begin(CTX)!.invokeReturned(OUTCOME);
    h.recorder.imageLoaded("asset://localhost/other.jpg", () => Promise.resolve());
    expect(h.pendingTimers()).toBe(1);
    expect(h.rows).toEqual([]);
  });

  it("invoke の失敗はエラーコードだけを残す", () => {
    const h = harness();
    h.recorder.begin(CTX)!.invokeFailed("BUSY");
    expect(h.rows[0]).toMatchObject({ status: "invoke_error", error_code: "BUSY", invoke_start_to_raf_ms: null });
  });

  it("decode() の失敗は decode_failed", async () => {
    const h = harness();
    h.recorder.begin(CTX)!.invokeReturned(OUTCOME);
    h.recorder.imageLoaded(OUTCOME.displayUrl, () => Promise.reject(new Error("x")));
    await Promise.resolve();
    await Promise.resolve();
    expect(h.rows[0].status).toBe("decode_failed");
  });

  it("前の行が閉じる前に次の invoke が戻っても取りこぼさない", () => {
    const h = harness();
    h.recorder.begin(CTX)!.invokeReturned(OUTCOME);
    h.recorder.begin(CTX)!.invokeReturned({ ...OUTCOME, displayUrl: "asset://localhost/b.jpg" });
    expect(h.rows).toHaveLength(1);
    expect(h.rows[0].request_id).toBe("id-1");
    expect(h.rows[0].status).toBe("image_not_observed");
  });

  it("行にパス・URL・撮影 ID を含めない", async () => {
    const h = harness();
    h.recorder.begin(CTX)!.invokeReturned(OUTCOME);
    h.recorder.imageLoaded(OUTCOME.displayUrl, () => Promise.resolve());
    await Promise.resolve();
    await Promise.resolve();
    h.flushFrame();
    h.flushFrame();
    const text = JSON.stringify(h.rows);
    expect(text).not.toMatch(/PRIVATE|asset:|\.jpg|output_url|displayUrl/);
    expect(h.rows[0].capture_id).toBe("DSC01234");
    expect(Object.keys(h.rows[0]).some((k) => /url|path/i.test(k))).toBe(false);
  });

  it("sink / decode / 時計が throw しても、計測の外に例外を出さない", async () => {
    let clock = 0;
    const frames: (() => void)[] = [];
    const recorder = createPreviewTimingRecorder({
      enabled: () => true,
      now: () => clock,
      requestFrame: (cb) => frames.push(cb),
      setTimer: () => 0,
      clearTimer: () => undefined,
      newId: () => "id",
      sink: () => {
        throw new Error("sink exploded");
      },
    });
    recorder.begin(CTX)!.invokeReturned(OUTCOME);
    expect(() => recorder.imageLoaded(OUTCOME.displayUrl, () => { throw new Error("decode exploded"); })).not.toThrow();
    await Promise.resolve();
    await Promise.resolve();
    clock += 1;
    expect(frames).toEqual([]); // decode が失敗したので rAF には進まない

    const brokenClock = createPreviewTimingRecorder({
      enabled: () => true,
      now: () => {
        throw new Error("no performance");
      },
      requestFrame: () => undefined,
      setTimer: () => 0,
      clearTimer: () => undefined,
      newId: () => "id",
      sink: () => undefined,
    });
    expect(brokenClock.now()).toBeUndefined();
  });
});
