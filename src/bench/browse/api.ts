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
  type ScenarioResult,
} from "./metrics";
import { DEFAULT_OPTIONS, SCENARIO_IDS, runAllScenarios, runScenario, type BenchDriver, type ScenarioOptions } from "./scenarios";

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
  const clock = options.now ?? (() => new Date());

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
      const result = await runScenario(options.driver, id, scenarioOptions);
      results.push(result);
      return result;
    },
    async runAll(scenarioOptions) {
      results = [...results, ...(await runAllScenarios(options.driver, scenarioOptions))];
      return report();
    },
    report,
    clear() {
      results = [];
    },
    exportJson: (reporterNotes) => JSON.stringify(report(reporterNotes), null, 2),
    download(reporterNotes) {
      const blob = new Blob([JSON.stringify(report(reporterNotes), null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `browse-bench-${options.stats.id}-${clock().toISOString().replace(/[:.]/g, "-")}.json`;
      anchor.click();
      URL.revokeObjectURL(url);
    },
  };
}
