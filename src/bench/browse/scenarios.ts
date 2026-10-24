/**
 * 閲覧ベンチのシナリオ。ブラウザ依存はすべて `BenchDriver` の向こうに置くので、
 * fake driver で操作の順序と集計を決定論的にテストできる(scenarios.test.ts)。
 */
import { type SettleResult } from "./settle";
import {
  gapsOf,
  summarizeFrameGaps,
  summarizeImages,
  summarizeProfilerById,
  summarizeSamples,
  type DomCounts,
  type ImageEvent,
  type ProfilerRecord,
  type ResourceSample,
  type ScenarioId,
  type ScenarioResult,
  SAMPLE_WARN_P95,
} from "./metrics";

export type Observed = {
  started_at: number;
  profiler: ProfilerRecord[];
  images: ImageEvent[];
  resources: ResourceSample[];
  /** longtask 非対応なら null。 */
  long_tasks: { start: number; duration: number }[] | null;
};

export interface BenchDriver {
  now(): number;
  /** 次の requestAnimationFrame のタイムスタンプ(performance.now 基準)。 */
  nextFrame(): Promise<number>;
  /** manifest の順のグループ ID。 */
  groupIds(): number[];
  recommendedCaptureOf(groupId: number): string;
  /** 状態を「解析済み・一覧表示・採用なし・スクロール先頭」へ戻し、1〜2 フレーム待つ。 */
  reset(): Promise<SettleResult>;
  /** 一覧のカード(または操作画面の帯)のクリックでグループを開く/選ぶ。実ハンドラを通す。 */
  openGroup(groupId: number): void;
  backToList(): void;
  toggleAccept(groupId: number): void;
  unmountList(): void;
  mountList(): void;
  windowScrollMax(): number;
  scrollWindowTo(y: number): void;
  stripScrollMax(): number;
  scrollStripTo(x: number): void;
  dom(): DomCounts;
  /** Profiler・画像イベント・Resource Timing・longtask の記録を空にして観測を始める。 */
  beginObserve(): void;
  /** 観測中の記録への参照(操作ごとの commit 時刻を引くのに使う)。 */
  observed(): Observed;
  endObserve(): Observed;
  /** load/error が `quietMs` 来ず、かつ要求済みで未完了の画像が 0 になるまで待つ。`timeoutMs` で打ち切ったら settled=false(未完了を settled にしない)。 */
  waitImagesSettled(timeoutMs: number, quietMs: number): Promise<SettleResult>;
}

export type ScenarioOptions = {
  /** 1 シナリオあたりの操作回数(選択・採用・戻る)。既定 40。 */
  steps?: number;
  /** 一覧の mount を繰り返す回数。既定 5。 */
  mountRepeats?: number;
  /** スクロール速度(px/s)。既定 2400。 */
  scrollSpeed?: number;
  /** スクロールの打ち切り(ms)。既定 15000。 */
  maxScrollMs?: number;
  /** 画像の読み込みが落ち着くのを待つ上限(ms)。既定 3000。 */
  settleMs?: number;
};

export const DEFAULT_OPTIONS: Required<ScenarioOptions> = {
  steps: 40,
  mountRepeats: 5,
  scrollSpeed: 2400,
  maxScrollMs: 15_000,
  settleMs: 3000,
};

const QUIET_MS = 200;

export const SCENARIO_IDS: readonly ScenarioId[] = [
  "mount_list",
  "scroll_list",
  "select_group_strip",
  "accept_toggle_list",
  "accept_toggle_strip",
  "return_to_list",
  "scroll_strip",
];

const TITLES: Record<ScenarioId, string> = {
  mount_list: "一覧の初期 mount(unmount → mount の繰り返し)",
  scroll_list: "一覧の縦スクロール(フレーム gap)",
  select_group_strip: "操作画面の帯でグループを順に選択",
  accept_toggle_list: "採用トグル(一覧表示中の再描画)",
  accept_toggle_strip: "採用トグル(操作画面の帯の再描画)",
  return_to_list: "操作画面 → 一覧へ戻って再表示",
  scroll_strip: "操作画面の帯の横スクロール(フレーム gap)",
};

type Body = {
  operations: number;
  to_commit: number[];
  to_frame: number[];
  gaps: number[];
  extra?: Record<string, number[]>;
  warnings?: string[];
};

/** 画像が静まらなかったときの注記を集める。未完了のまま次へ進んだことを結果に残すため。 */
type SettleTracker = { notes: string[]; settle(label: string, timeoutMs: number, quietMs?: number): Promise<SettleResult> };

function createSettleTracker(driver: BenchDriver): SettleTracker {
  const notes: string[] = [];
  return {
    notes,
    async settle(label, timeoutMs, quietMs = QUIET_MS) {
      const result = await driver.waitImagesSettled(timeoutMs, quietMs);
      if (!result.settled) {
        notes.push(`${label}: 画像が ${timeoutMs}ms 以内に静まらなかった(要求済み未完了 ${result.pending} 枚)。次の操作・計測に読み込みが混入しうる`);
      }
      return result;
    },
  };
}

async function openFirstGroup(driver: BenchDriver, tracker: SettleTracker, settleMs: number): Promise<void> {
  const first = driver.groupIds()[0];
  if (first === undefined) return;
  driver.openGroup(first);
  await twoFrames(driver);
  await tracker.settle("setup(先頭グループを開く)", settleMs);
}

/** 同じグループを複数回トグルする(= 追加と取消が混ざる)ときの説明。 */
function toggleMixWarning(ids: readonly number[]): string[] {
  const distinct = new Set(ids).size;
  return distinct < ids.length
    ? [`グループ数 ${distinct} < 操作 ${ids.length} 回: 同じグループを繰り返しトグルするので、採用の追加と取消が混ざる(後半は取消の再描画を含む)`]
    : [];
}

async function twoFrames(driver: BenchDriver): Promise<number> {
  await driver.nextFrame();
  return driver.nextFrame();
}

/** 1 操作を計る。op の同期部分の後、2 回目の rAF までを to_frame、最初のコミットまでを to_commit にする。 */
async function measureOperation(driver: BenchDriver, op: () => void): Promise<{ commit: number | null; frame: number }> {
  const before = driver.observed().profiler.length;
  const t0 = driver.now();
  op();
  const t2 = await twoFrames(driver);
  const first = driver.observed().profiler.slice(before).find((record) => record.commit_at >= t0);
  return { commit: first ? first.commit_at - t0 : null, frame: t2 - t0 };
}

async function sweep(
  driver: BenchDriver,
  max: number,
  scrollTo: (position: number) => void,
  speed: number,
  maxMs: number,
): Promise<number[]> {
  const stamps: number[] = [];
  let position = 0;
  let previous = await driver.nextFrame();
  const startedAt = previous;
  stamps.push(previous);
  while (position < max && previous - startedAt < maxMs) {
    const stamp = await driver.nextFrame();
    position = Math.min(max, position + (speed * (stamp - previous)) / 1000);
    scrollTo(position);
    stamps.push(stamp);
    previous = stamp;
  }
  return gapsOf(stamps);
}

function pickGroups(driver: BenchDriver, count: number): { ids: number[]; warnings: string[] } {
  const all = driver.groupIds();
  const warnings: string[] = [];
  if (all.length === 0) warnings.push("グループが 0 件");
  return { ids: all.length ? Array.from({ length: count }, (_, index) => all[index % all.length] as number) : [], warnings };
}

async function run(
  driver: BenchDriver,
  tracker: SettleTracker,
  id: ScenarioId,
  params: Record<string, number | string | boolean>,
  body: () => Promise<Body>,
  /** 観測を始める前の準備(操作画面を開いておく等)。ここでの mount は Profiler/DOM の before に混ぜない。 */
  setup?: () => Promise<void>,
): Promise<ScenarioResult> {
  const reset = await driver.reset();
  if (!reset.settled) {
    tracker.notes.push(`reset: 前のシナリオの画像が静まらなかった(要求済み未完了 ${reset.pending} 枚)。この結果に前の読み込みが混入しうる`);
  }
  if (setup) await setup();
  driver.beginObserve();
  let dom_before: DomCounts;
  let result: Body;
  try {
    dom_before = driver.dom();
    result = await body();
  } catch (error) {
    driver.endObserve();
    throw error;
  }
  const dom_after = driver.dom();
  const observed = driver.endObserve();
  const warnings = [...tracker.notes, ...(result.warnings ?? [])];
  if (result.operations < SAMPLE_WARN_P95) {
    warnings.push(`操作/フレーム数 n=${result.operations} が ${SAMPLE_WARN_P95} 未満: p95 は最大値に近い参考値`);
  }
  const skipped = result.operations - result.to_commit.length;
  if (result.to_frame.length > 0 && skipped > 0) {
    warnings.push(`${skipped} 回の操作でコミットを観測できなかった(状態が変わらない操作・Profiler 外の更新)`);
  }
  return {
    id,
    title: TITLES[id],
    params,
    operations: result.operations,
    to_commit_ms: result.to_commit.length ? summarizeSamples(result.to_commit) : null,
    to_frame_ms: result.to_frame.length ? summarizeSamples(result.to_frame) : null,
    frame_gaps: result.gaps.length ? summarizeFrameGaps(result.gaps) : null,
    profiler: summarizeProfilerById(observed.profiler),
    dom_before,
    dom_after,
    images: summarizeImages(observed.images, observed.resources, observed.started_at),
    extra: Object.fromEntries(Object.entries(result.extra ?? {}).map(([key, values]) => [key, summarizeSamples(values)])),
    long_tasks: observed.long_tasks
      ? {
          count: observed.long_tasks.length,
          total_ms: observed.long_tasks.reduce((sum, task) => sum + task.duration, 0),
          max_ms: observed.long_tasks.reduce((max, task) => Math.max(max, task.duration), 0),
        }
      : null,
    warnings,
    raw: { to_commit_ms: result.to_commit, to_frame_ms: result.to_frame, frame_gaps_ms: result.gaps },
  };
}

async function timedOperations(
  driver: BenchDriver,
  count: number,
  op: (index: number) => void,
  afterEach?: () => Promise<void>,
): Promise<Pick<Body, "operations" | "to_commit" | "to_frame">> {
  const to_commit: number[] = [];
  const to_frame: number[] = [];
  for (let index = 0; index < count; index += 1) {
    const measured = await measureOperation(driver, () => op(index));
    to_frame.push(measured.frame);
    if (measured.commit !== null) to_commit.push(measured.commit);
    if (afterEach) await afterEach();
  }
  return { operations: count, to_commit, to_frame };
}

export async function runScenario(
  driver: BenchDriver,
  id: ScenarioId,
  options: ScenarioOptions = {},
): Promise<ScenarioResult> {
  const o = { ...DEFAULT_OPTIONS, ...options };
  const groupIds = driver.groupIds();
  const tracker = createSettleTracker(driver);

  switch (id) {
    case "mount_list":
      return run(driver, tracker, id, { mountRepeats: o.mountRepeats, settleMs: o.settleMs }, async () => {
        const to_frame: number[] = [];
        const to_commit: number[] = [];
        const settle: number[] = [];
        for (let index = 0; index < o.mountRepeats; index += 1) {
          driver.unmountList();
          await twoFrames(driver);
          const measured = await measureOperation(driver, () => driver.mountList());
          to_frame.push(measured.frame);
          if (measured.commit !== null) to_commit.push(measured.commit);
          settle.push((await tracker.settle(`mount ${index + 1} 回目`, o.settleMs)).waited_ms);
        }
        return {
          operations: o.mountRepeats,
          to_commit,
          to_frame,
          gaps: [],
          extra: { images_settle_ms: settle },
          warnings: ["1 回目はサムネがブラウザキャッシュに無い可能性がある(cold)。2 回目以降は warm。raw の並び順で読む"],
        };
      });

    case "scroll_list":
      return run(driver, tracker, id, { scrollSpeed: o.scrollSpeed, maxScrollMs: o.maxScrollMs }, async () => {
        const max = driver.windowScrollMax();
        const warnings = max <= 0 ? ["スクロールできる高さが無い(グループが少なすぎる/ビューポートが大きすぎる)"] : [];
        const gaps = max > 0 ? await sweep(driver, max, (y) => driver.scrollWindowTo(y), o.scrollSpeed, o.maxScrollMs) : [];
        await tracker.settle("スクロール後", o.settleMs);
        return { operations: gaps.length, to_commit: [], to_frame: [], gaps, warnings };
      });

    case "select_group_strip":
      return run(driver, tracker, id, { steps: o.steps }, async () => {
        const warnings: string[] = [];
        if (groupIds.length < 2) return { operations: 0, to_commit: [], to_frame: [], gaps: [], warnings: ["グループが 2 件未満"] };
        if (groupIds.length - 1 < o.steps) warnings.push(`グループ数 ${groupIds.length} < steps ${o.steps}: 先頭から巡回して選択する`);
        const measured = await timedOperations(driver, o.steps, (index) => {
          // 1 個目は setup で選択済みなので、2 個目から順に選ぶ(←→ で送る操作に近い)。
          driver.openGroup(groupIds[(index + 1) % groupIds.length] as number);
        });
        return { ...measured, gaps: [], warnings };
      }, () => openFirstGroup(driver, tracker, o.settleMs));

    case "accept_toggle_list":
      return run(driver, tracker, id, { steps: o.steps }, async () => {
        const picked = pickGroups(driver, o.steps);
        const measured = await timedOperations(driver, picked.ids.length, (index) => driver.toggleAccept(picked.ids[index] as number));
        return { ...measured, gaps: [], warnings: [...picked.warnings, ...toggleMixWarning(picked.ids)] };
      });

    case "accept_toggle_strip":
      return run(driver, tracker, id, { steps: o.steps }, async () => {
        const picked = pickGroups(driver, o.steps);
        const measured = await timedOperations(driver, picked.ids.length, (index) => driver.toggleAccept(picked.ids[index] as number));
        return { ...measured, gaps: [], warnings: [...picked.warnings, ...toggleMixWarning(picked.ids)] };
      }, () => openFirstGroup(driver, tracker, o.settleMs));

    case "return_to_list":
      return run(driver, tracker, id, { steps: o.steps, settleMs: o.settleMs }, async () => {
        const picked = pickGroups(driver, o.steps);
        const settle: number[] = [];
        const to_commit: number[] = [];
        const to_frame: number[] = [];
        for (const groupId of picked.ids) {
          driver.openGroup(groupId);
          await twoFrames(driver);
          const measured = await measureOperation(driver, () => driver.backToList());
          to_frame.push(measured.frame);
          if (measured.commit !== null) to_commit.push(measured.commit);
          settle.push((await tracker.settle(`戻る ${settle.length + 1} 回目`, o.settleMs)).waited_ms);
        }
        return {
          operations: picked.ids.length,
          to_commit,
          to_frame,
          gaps: [],
          extra: { images_settle_ms: settle },
          warnings: picked.warnings,
        };
      });

    case "scroll_strip":
      return run(driver, tracker, id, { scrollSpeed: o.scrollSpeed, maxScrollMs: o.maxScrollMs }, async () => {
        if (groupIds.length === 0) return { operations: 0, to_commit: [], to_frame: [], gaps: [], warnings: ["グループが 0 件"] };
        const max = driver.stripScrollMax();
        const warnings = max <= 0 ? ["帯が横スクロールできない(グループが少ない)"] : [];
        const gaps = max > 0 ? await sweep(driver, max, (x) => driver.scrollStripTo(x), o.scrollSpeed, o.maxScrollMs) : [];
        await tracker.settle("スクロール後", o.settleMs);
        return { operations: gaps.length, to_commit: [], to_frame: [], gaps, warnings };
      }, () => openFirstGroup(driver, tracker, o.settleMs));
  }
}

/** 全シナリオを固定の順序で直列に走らせる。 */
export async function runAllScenarios(
  driver: BenchDriver,
  options: ScenarioOptions = {},
  /** 1 シナリオ終わるごとに呼ぶ。途中で失敗しても、それまでの結果を呼び出し側が保持できる。 */
  onResult?: (result: ScenarioResult) => void,
): Promise<ScenarioResult[]> {
  const results: ScenarioResult[] = [];
  for (const id of SCENARIO_IDS) {
    const result = await runScenario(driver, id, options);
    results.push(result);
    onResult?.(result);
  }
  return results;
}
