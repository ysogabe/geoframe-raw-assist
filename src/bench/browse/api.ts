/**
 * ブラウザ自動化(Playwright 等)が叩く `window.__browseBench` と、JSON 報告の組み立て。
 * シナリオ本体は scenarios.ts、ブラウザ操作は driver.ts。ここは結果をためて報告にまとめるだけ。
 */
import type { ManifestStats } from "./manifest";
import {
  LIMITATIONS,
  type BenchEnvironment,
  type BenchReport,
  type BootTiming,
  type ScenarioId,
  type RunFailure,
  type ScenarioResult,
} from "./metrics";
import { DEFAULT_OPTIONS, SCENARIO_IDS, runAllScenarios, runScenario, type BenchDriver, type ScenarioOptions } from "./scenarios";

/** download の Blob URL を失効させるまでの待ち(ms)。 */
export const REVOKE_DELAY_MS = 60_000;

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export const REPORT_SCHEMA = "geoframe.browse-bench.report/1";

/** 代表性に関わる LIMITATIONS だけを抜き出して報告欄へ固定で載せる。 */
const REPRESENTATIVENESS_CAVEATS = LIMITATIONS.filter((line) => /合成|Chromium|WKWebView|単純複製|サムネキャッシュ/.test(line));

export type BrowseBenchApi = {
  version: 1;
  /** マウントと manifest 検証が終わったら resolve。失敗したら reject(理由は `error`)。 */
  ready: Promise<void>;
  error: string | null;
  scenarios(): readonly ScenarioId[];
  defaults(): Required<ScenarioOptions>;
  manifest(): { stats: ManifestStats };
  run(id: ScenarioId, options?: ScenarioOptions): Promise<ScenarioResult>;
  runAll(options?: ScenarioOptions): Promise<BenchReport>;
  /** これまでに走らせた結果をまとめた報告(まだ何も走らせていなければ scenarios は空)。 */
  report(reporterNotes?: string): BenchReport;
  clear(): void;
  exportJson(reporterNotes?: string): string;
  /** 報告 JSON をファイルとして保存する(ブラウザのダウンロード)。 */
  download(reporterNotes?: string): void;
};

export type BrowseBenchCore = Omit<BrowseBenchApi, "ready" | "error">;

export function createApi(options: {
  driver: BenchDriver;
  stats: ManifestStats;
  environment: () => BenchEnvironment;
  boot: () => BootTiming | null;
  now?: () => Date;
}): BrowseBenchCore {
  let results: ScenarioResult[] = [];
  let failures: RunFailure[] = [];
  let running: string | null = null;
  const clock = options.now ?? (() => new Date());

  /** run / runAll の同時起動を拒否する。DOM と観測バッファは 1 つしか無く、混ぜると両方の数字が壊れる。 */
  async function exclusive<T>(label: string, task: () => Promise<T>): Promise<T> {
    if (running !== null) throw new Error(`別のシナリオが実行中(${running})。完了してから ${label} を呼ぶ`);
    running = label;
    try {
      return await task();
    } finally {
      running = null;
    }
  }

  function report(reporterNotes = ""): BenchReport {
    return {
      schema: REPORT_SCHEMA,
      generated_at: clock().toISOString(),
      environment: options.environment(),
      boot: options.boot(),
      dataset: {
        kind: options.stats.kind,
        id: options.stats.id,
        groups: options.stats.group_count,
        photos: options.stats.photo_count,
        source_groups: options.stats.source?.group_count ?? null,
      },
      representativeness: {
        dataset_kind: options.stats.kind,
        stats: options.stats,
        reporter_notes: reporterNotes,
        caveats: REPRESENTATIVENESS_CAVEATS,
      },
      // 失敗が 1 件でもあれば incomplete。部分結果(scenarios)は残すが、成功した報告としては扱わない。
      run_status: failures.length === 0 ? "complete" : "incomplete",
      failures,
      scenarios: results,
      limitations: LIMITATIONS,
    };
  }

  return {
    version: 1,
    scenarios: () => SCENARIO_IDS,
    defaults: () => DEFAULT_OPTIONS,
    manifest: () => ({ stats: options.stats }),
    async run(id, scenarioOptions) {
      return exclusive(id, async () => {
        try {
          const result = await runScenario(options.driver, id, scenarioOptions);
          results.push(result);
          return result;
        } catch (error) {
          failures.push({ scenario: id, message: messageOf(error) });
          throw error;
        }
      });
    },
    async runAll(scenarioOptions) {
      return exclusive("runAll", async () => {
        try {
          await runAllScenarios(options.driver, scenarioOptions, (result) => results.push(result));
        } catch (error) {
          failures.push({ scenario: "runAll", message: messageOf(error) });
          throw error;
        }
        return report();
      });
    },
    report,
    clear() {
      if (running !== null) throw new Error(`実行中(${running})は clear できない`);
      results = [];
      failures = [];
    },
    exportJson: (reporterNotes) => JSON.stringify(report(reporterNotes), null, 2),
    download(reporterNotes) {
      const blob = new Blob([JSON.stringify(report(reporterNotes), null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `browse-bench-${options.stats.id}-${clock().toISOString().replace(/[:.]/g, "-")}.json`;
      // Chrome はダウンロードの開始が非同期。click 直後に revoke すると取得に失敗しうるので遅らせる。
      document.body.append(anchor);
      try {
        anchor.click();
      } finally {
        anchor.remove();
        setTimeout(() => URL.revokeObjectURL(url), REVOKE_DELAY_MS);
      }
    },
  };
}
