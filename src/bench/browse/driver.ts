/**
 * ブラウザ上の `BenchDriver` 実装。実 DOM のクリック・スクロール・rAF・PerformanceObserver を使う。
 * ここは node では動かない(契約テストは fake driver 側で行う)。
 */
import type { ProfilerOnRenderCallback } from "react";
import type { BenchControls } from "./harness";
import type { DomCounts, ImageEvent, ProfilerRecord, ResourceSample } from "./metrics";
import type { BenchDriver, Observed } from "./scenarios";
import { isPendingImage, waitUntilSettled } from "./settle";

export type ProfilerSink = { onRender: ProfilerOnRenderCallback; records: ProfilerRecord[]; enabled: boolean };

export function createProfilerSink(): ProfilerSink {
  const sink: ProfilerSink = {
    records: [],
    enabled: false,
    onRender: (id, phase, actualDuration, baseDuration, _startTime, commitTime) => {
      if (!sink.enabled) return;
      sink.records.push({ id, phase, actual_ms: actualDuration, base_ms: baseDuration, commit_at: commitTime });
    },
  };
  return sink;
}

export function longTaskSupported(): boolean {
  return typeof PerformanceObserver !== "undefined" && (PerformanceObserver.supportedEntryTypes ?? []).includes("longtask");
}

export function createBrowserDriver(options: {
  controls: BenchControls;
  sink: ProfilerSink;
  groupIds: number[];
  recommendedOf: (groupId: number) => string;
}): BenchDriver {
  const { controls, sink } = options;
  let observing = false;
  let startedAt = 0;
  let images: ImageEvent[] = [];
  let resources: ResourceSample[] = [];
  let longTasks: { start: number; duration: number }[] = [];
  let lastImageEventAt = 0;

  const root = () => controls.root();

  // <img> の load/error は bubble しないので capture で拾う。観測の有無にかかわらず最終時刻は覚える(settle 判定用)。
  const onImage = (event: Event, ok: boolean) => {
    const target = event.target;
    if (!(target instanceof HTMLImageElement) || !root()?.contains(target)) return;
    lastImageEventAt = performance.now();
    if (observing) images.push({ at: lastImageEventAt, ok });
  };
  document.addEventListener("load", (event) => onImage(event, true), true);
  document.addEventListener("error", (event) => onImage(event, false), true);

  if (typeof PerformanceObserver !== "undefined") {
    try {
      new PerformanceObserver((list) => {
        if (!observing) return;
        for (const entry of list.getEntries() as PerformanceResourceTiming[]) {
          if (entry.initiatorType !== "img") continue;
          resources.push({
            duration_ms: entry.duration,
            transfer_size: entry.transferSize,
            decoded_body_size: entry.decodedBodySize,
          });
        }
      }).observe({ type: "resource" });
    } catch {
      // 非対応環境では Resource Timing の欄が空になるだけ。
    }
    if (longTaskSupported()) {
      new PerformanceObserver((list) => {
        if (!observing) return;
        for (const entry of list.getEntries()) longTasks.push({ start: entry.startTime, duration: entry.duration });
      }).observe({ type: "longtask" });
    }
  }

  const nextFrame = () => new Promise<number>((resolve) => requestAnimationFrame((stamp) => resolve(stamp)));
  const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
  const clickables = (selector: string) => Array.from(root()?.querySelectorAll<HTMLElement>(selector) ?? []);

  const driver: BenchDriver = {
    now: () => performance.now(),
    nextFrame,
    groupIds: () => options.groupIds,
    recommendedCaptureOf: options.recommendedOf,

    async reset() {
      controls.setListMounted(true);
      controls.dispatch({ type: "__bench/reset" });
      window.scrollTo({ top: 0, left: 0, behavior: "instant" });
      await nextFrame();
      await nextFrame();
      // 前のシナリオの読み込みが次の観測に混ざらないよう、要求済みの画像が全部終わって静かになるまで待つ。
      return driver.waitImagesSettled(2000, 150);
    },

    openGroup(groupId) {
      const index = options.groupIds.indexOf(groupId);
      const stripCard = clickables(`.group-strip-track li[data-group-id="${groupId}"] button`)[0];
      const listCard = clickables(".group-card")[index];
      // 実ハンドラ(onClick → onOpenGroup / onSelectGroup)を通す。対象が無いのに state だけ進めると
      // 「クリックの費用」を測っていないのに測ったように見えるので、黙ってフォールバックせず失敗にする。
      const target = stripCard ?? listCard;
      if (!target) throw new Error(`グループ ${groupId} のクリック対象(一覧カードか帯のカード)が DOM に無い`);
      target.click();
    },

    backToList() {
      controls.dispatch({ type: "screen/suggestions" });
    },

    toggleAccept(groupId) {
      controls.dispatch({
        type: "accept/photo",
        groupId,
        captureId: options.recommendedOf(groupId),
        variant: "camera_jpg",
      });
    },

    unmountList: () => controls.setListMounted(false),
    mountList: () => controls.setListMounted(true),

    windowScrollMax: () => Math.max(0, document.documentElement.scrollHeight - window.innerHeight),
    scrollWindowTo: (y) => window.scrollTo({ top: y, left: 0, behavior: "instant" }),
    stripScrollMax() {
      const scroller = root()?.querySelector<HTMLElement>(".group-strip-scroller");
      return scroller ? Math.max(0, scroller.scrollWidth - scroller.clientWidth) : 0;
    },
    scrollStripTo(x) {
      root()?.querySelector<HTMLElement>(".group-strip-scroller")?.scrollTo({ left: x, behavior: "instant" });
    },

    dom(): DomCounts {
      const element = root();
      const imgs = Array.from(element?.querySelectorAll("img") ?? []);
      return {
        elements: element ? element.querySelectorAll("*").length + 1 : 0,
        imgs: imgs.length,
        imgs_complete: imgs.filter((img) => img.complete).length,
        group_cards: element?.querySelectorAll(".group-card").length ?? 0,
        strip_cards: element?.querySelectorAll(".group-strip-card").length ?? 0,
      };
    },

    beginObserve() {
      sink.records = [];
      sink.enabled = true;
      images = [];
      resources = [];
      longTasks = [];
      startedAt = performance.now();
      observing = true;
    },
    observed(): Observed {
      return {
        started_at: startedAt,
        profiler: sink.records,
        images,
        resources,
        long_tasks: longTaskSupported() ? longTasks : null,
      };
    },
    endObserve(): Observed {
      const result = driver.observed();
      observing = false;
      sink.enabled = false;
      return result;
    },

    waitImagesSettled(timeoutMs, quietMs) {
      return waitUntilSettled(
        {
          now: () => performance.now(),
          sleep,
          lastImageEventAt: () => lastImageEventAt,
          pendingCount: () => {
            const viewport = { width: window.innerWidth, height: window.innerHeight };
            return Array.from(root()?.querySelectorAll("img") ?? []).filter((img) =>
              isPendingImage(
                { complete: img.complete, currentSrc: img.currentSrc, lazy: img.loading === "lazy", rect: img.getBoundingClientRect() },
                viewport,
              ),
            ).length;
          },
        },
        timeoutMs,
        quietMs,
      );
    },
  };
  return driver;
}
