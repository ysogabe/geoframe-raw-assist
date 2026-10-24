/**
 * 閲覧ベンチの独立エントリ(`browse-bench.html`)。プロダクトの main.tsx / App.tsx には配線しない。
 *
 * 入力(URL クエリ):
 *   ?manifest=<dir>/manifest.json         bench-fixtures/ 配下の manifest(実メタデータでも合成でも可)
 *   ?synthetic=<グループ数>&pool=<サムネ枚数>&thumbs=<dir>/thumbs[&seed=1]
 *                                          合成 manifest をページ内で決定論的に生成する
 * どちらも `dataset.kind` の宣言を検証してから描く。
 */
import { createRoot } from "react-dom/client";
import "../../globals.css";
import { createApi, type BrowseBenchApi } from "./api";
import { probeThumbCachePolicy, type CachePolicyProbe } from "./cache-policy";
import { createBrowserDriver, createProfilerSink, longTaskSupported } from "./driver";
import { BenchApp, type BenchControls } from "./harness";
import {
  generateSyntheticManifest,
  isRelativeThumbUrl,
  manifestStats,
  manifestToAnalysis,
  parseSyntheticQuery,
  resolveThumbUrl,
  validateManifest,
  type BenchManifest,
} from "./manifest";
import type { BenchEnvironment, BootTiming } from "./metrics";
import { DEFAULT_OPTIONS, SCENARIO_IDS } from "./scenarios";

/** エントリ(このモジュール本体)の評価開始。import の評価時間は含まない。boot.from_entry_* の起点。 */
const bootStartedAt = performance.now();

declare const __BENCH_PROFILING__: boolean | undefined;

declare global {
  interface Window {
    __browseBench?: BrowseBenchApi;
  }
}

const FIXTURE_PREFIX = "/bench-fixtures/";

function fixtureUrl(relative: string): string | null {
  if (!isRelativeThumbUrl(relative)) return null;
  return new URL(`${FIXTURE_PREFIX}${relative}`, window.location.origin).href;
}

function environment(cache: CachePolicyProbe): BenchEnvironment {
  const profiling = typeof __BENCH_PROFILING__ !== "undefined" && __BENCH_PROFILING__;
  return {
    user_agent: navigator.userAgent,
    device_pixel_ratio: window.devicePixelRatio,
    viewport: { width: window.innerWidth, height: window.innerHeight },
    hardware_concurrency: navigator.hardwareConcurrency ?? null,
    react_build: import.meta.env.DEV ? "development" : profiling ? "production-profiling" : "production",
    is_tauri: "__TAURI_INTERNALS__" in window,
    longtask_supported: longTaskSupported(),
    prefers_reduced_motion: window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    thumb_cache_policy: cache.policy,
    thumb_cache_policy_source: cache.source,
    thumb_cache_policy_note: cache.note,
  };
}

function showStatus(message: string, isError = false) {
  const root = document.getElementById("root");
  if (root) {
    root.textContent = message;
    root.style.cssText = `padding:24px;font:14px/1.6 system-ui;color:${isError ? "#a11" : "#222"};white-space:pre-wrap`;
  }
}

async function loadManifest(): Promise<{ manifest: BenchManifest; manifestUrl: string }> {
  const params = new URLSearchParams(window.location.search);
  if (params.has("synthetic")) {
    // 件数・seed・thumbs を生成の前に厳密検査し(上限つき)、生成後も実データと同じ検証を通す。
    const manifest = generateSyntheticManifest(parseSyntheticQuery(params));
    return { manifest, manifestUrl: new URL(FIXTURE_PREFIX, window.location.origin).href };
  }
  const path = params.get("manifest");
  const manifestUrl = path === null ? null : fixtureUrl(path);
  if (manifestUrl === null) throw new Error("?manifest=<bench-fixtures 配下の相対パス> か ?synthetic=N&thumbs=<dir> が必要");
  const response = await fetch(manifestUrl, { cache: "no-store" });
  if (!response.ok) throw new Error(`manifest を取得できない: HTTP ${response.status}`);
  const checked = validateManifest(await response.json());
  if (!checked.ok) throw new Error(`manifest の検証に失敗:\n${checked.errors.join("\n")}`);
  return { manifest: checked.manifest, manifestUrl };
}

function mountToolbar(api: BrowseBenchApi, label: string) {
  // 計測対象の root の外に置く(DOM 数に入れない)。
  const bar = document.createElement("div");
  bar.style.cssText =
    "position:fixed;right:12px;bottom:12px;z-index:99999;max-width:min(560px,90vw);background:#111;color:#fff;font:12px system-ui;padding:8px 10px;border-radius:8px;display:flex;flex-direction:column;gap:6px";
  const row = document.createElement("div");
  row.style.cssText = "display:flex;gap:8px;align-items:center;flex-wrap:wrap";
  const text = document.createElement("span");
  text.textContent = label;
  const status = document.createElement("span");
  const run = document.createElement("button");
  run.textContent = "全シナリオ実行";
  const save = document.createElement("button");
  save.textContent = "JSON 保存";
  const show = document.createElement("button");
  show.textContent = "JSON 表示";
  const box = document.createElement("textarea");
  box.readOnly = true;
  box.hidden = true;
  box.style.cssText = "width:520px;max-width:100%;height:160px;font:11px ui-monospace,monospace";

  const setStatus = (message: string, isError = false) => {
    status.textContent = message;
    status.style.color = isError ? "#ff8a80" : "#9fe3b6";
  };
  const messageOf = (error: unknown) => (error instanceof Error ? error.message : String(error));

  run.onclick = () => {
    run.disabled = true;
    setStatus("実行中…");
    api
      .runAll()
      .then((report) => setStatus(report.run_status === "complete" ? `完了(${report.scenarios.length} シナリオ)` : "不完全", report.run_status !== "complete"))
      // 失敗を成功に見せない。完了したシナリオの分は report() に残るが run_status は incomplete。
      .catch((error: unknown) => setStatus(`失敗: ${messageOf(error)}(途中までの結果は JSON に incomplete として残る)`, true))
      .finally(() => {
        run.disabled = false;
      });
  };
  save.onclick = () => {
    try {
      api.download();
      setStatus("JSON 保存を開始した(ブラウザのダウンロードを確認。出ない場合は「JSON 表示」からコピー)");
    } catch (error) {
      setStatus(`JSON 保存に失敗: ${messageOf(error)}。「JSON 表示」からコピーできる`, true);
    }
  };
  // ダウンロードが使えない環境(自動化ブラウザ等)の回復手段。exportJson() と同じ内容。
  show.onclick = () => {
    box.hidden = !box.hidden;
    if (!box.hidden) {
      try {
        box.value = api.exportJson();
        box.select();
      } catch (error) {
        setStatus(`JSON 生成に失敗: ${messageOf(error)}`, true);
      }
    }
  };
  row.append(text, run, save, show, status);
  bar.append(row, box);
  document.body.append(bar);
}

async function main() {
  let failure: string | null = null;
  let core: ReturnType<typeof createApi> | null = null;
  let resolveReady!: () => void;
  let rejectReady!: (error: Error) => void;
  const ready = new Promise<void>((resolve, reject) => {
    resolveReady = resolve;
    rejectReady = reject;
  });
  // 未処理 rejection の警告を避ける(呼び出し側が await したときに初めて見える)。
  ready.catch(() => {});

  const needCore = () => {
    if (!core) throw new Error(failure ?? "ベンチがまだ準備できていない(await __browseBench.ready)");
    return core;
  };
  const api: BrowseBenchApi = {
    version: 1,
    ready,
    get error() {
      return failure;
    },
    scenarios: () => SCENARIO_IDS,
    defaults: () => DEFAULT_OPTIONS,
    manifest: () => needCore().manifest(),
    run: async (id, options) => {
      await ready;
      return needCore().run(id, options);
    },
    runAll: async (options) => {
      await ready;
      return needCore().runAll(options);
    },
    report: (notes) => needCore().report(notes),
    clear: () => needCore().clear(),
    exportJson: (notes) => needCore().exportJson(notes),
    download: (notes) => needCore().download(notes),
  };
  window.__browseBench = api;

  try {
    const { manifest, manifestUrl } = await loadManifest();
    const analysis = manifestToAnalysis(manifest, manifestUrl);
    const stats = manifestStats(manifest);
    // 計測に使うサムネ 1 枚への HEAD で配信ヘッダ(キャッシュ方針)を確認する。render より前に済ませる。
    const firstPhoto = manifest.photos[0];
    const cachePolicy = await probeThumbCachePolicy(firstPhoto ? resolveThumbUrl(firstPhoto.thumb, manifestUrl) : null);
    const sink = createProfilerSink();
    // 起動直後の最初の一覧コミットは Profiler(root/list) が mount として拾うので、ここだけ有効にして記録する。
    sink.enabled = true;
    let boot: BootTiming | null = null;
    let firstCommitAt: number | null = null;

    // boot.to_first_* の起点は『render 直前』。manifest の fetch/検証/変換/HEAD は含めない(pre_render_ms と
    // from_entry_* に別名で残す)。
    const renderStartedAt = performance.now();
    const controlsReady = new Promise<BenchControls>((resolve) => {
      createRoot(document.getElementById("root")!).render(
        <BenchApp
          analysis={analysis}
          onRender={sink.onRender}
          onControls={resolve}
          onFirstCommit={() => {
            firstCommitAt = performance.now();
          }}
        />,
      );
    });
    const controls = await controlsReady;
    const frameAt = await new Promise<number>((resolve) => requestAnimationFrame(() => requestAnimationFrame((stamp) => resolve(stamp))));
    const rootMount = sink.records.find((record) => record.id === "root" && record.phase === "mount");
    boot = {
      to_first_commit_ms: firstCommitAt === null ? null : firstCommitAt - renderStartedAt,
      to_first_frame_ms: frameAt - renderStartedAt,
      mount_actual_ms: rootMount ? rootMount.actual_ms : null,
      pre_render_ms: renderStartedAt - bootStartedAt,
      from_entry_to_first_commit_ms: firstCommitAt === null ? null : firstCommitAt - bootStartedAt,
      from_entry_to_first_frame_ms: frameAt - bootStartedAt,
    };
    sink.enabled = false;
    sink.records = [];

    const recommended = new Map(manifest.groups.map((group) => [group.id, group.recommended_capture_id]));
    const driver = createBrowserDriver({
      controls,
      sink,
      groupIds: manifest.groups.map((group) => group.id),
      recommendedOf: (groupId) => recommended.get(groupId) ?? "",
    });
    const base = createApi({
      driver,
      stats,
      boot: () => boot,
      environment: () => environment(cachePolicy),
    });
    core = base;
    mountToolbar(api, `${stats.kind} groups=${stats.group_count} photos=${stats.photo_count}`);
    resolveReady();
  } catch (error) {
    failure = error instanceof Error ? error.message : String(error);
    showStatus(`ベンチを開始できない:\n${failure}`, true);
    rejectReady(new Error(failure));
  }
}

void main();
