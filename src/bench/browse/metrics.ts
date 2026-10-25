/**
 * 閲覧ベンチの集計（純粋関数）。ブラウザ API に触らないので node で決定論的にテストできる。
 */

export const SAMPLE_WARN_P95 = 20;
export const SAMPLE_WARN_P99 = 100;
/** 60Hz の 1 フレーム。120Hz の画面では実測側の gap 分布で読み替える。 */
export const FRAME_BUDGET_MS = 1000 / 60;

export type SampleSummary = {
  n: number;
  min: number | null;
  mean: number | null;
  p50: number | null;
  p95: number | null;
  p99: number | null;
  max: number | null;
  /** n >= 20。false の p95 は「ほぼ最大値」で、分布の裾の推定にならない。 */
  p95_reliable: boolean;
  /** n >= 100。false の p99 は同上。 */
  p99_reliable: boolean;
};

/** nearest-rank 法のパーセンタイル。入力は昇順。 */
export function percentile(sorted: readonly number[], p: number): number | null {
  if (sorted.length === 0) return null;
  const rank = Math.max(1, Math.ceil((p / 100) * sorted.length));
  return sorted[Math.min(sorted.length, rank) - 1] ?? null;
}

export function summarizeSamples(values: readonly number[]): SampleSummary {
  const finite = values.filter((value) => Number.isFinite(value));
  const sorted = [...finite].sort((a, b) => a - b);
  const n = sorted.length;
  return {
    n,
    min: sorted[0] ?? null,
    mean: n ? sorted.reduce((sum, value) => sum + value, 0) / n : null,
    p50: percentile(sorted, 50),
    p95: percentile(sorted, 95),
    p99: percentile(sorted, 99),
    max: sorted[n - 1] ?? null,
    p95_reliable: n >= SAMPLE_WARN_P95,
    p99_reliable: n >= SAMPLE_WARN_P99,
  };
}

export type FrameGapSummary = SampleSummary & {
  /** 隣り合う rAF コールバックの時刻差（ms）。 */
  budget_ms: number;
  /** gap が 1.5 フレーム超（= 少なくとも 1 フレーム落とした）の回数。 */
  over_1_5_frames: number;
  /** gap が 33.4ms 超（30fps 相当を割った）の回数。 */
  over_33ms: number;
  /** gap が 100ms 超の回数（目に見える引っかかり）。 */
  over_100ms: number;
};

export function summarizeFrameGaps(gaps: readonly number[], budgetMs = FRAME_BUDGET_MS): FrameGapSummary {
  return {
    ...summarizeSamples(gaps),
    budget_ms: budgetMs,
    over_1_5_frames: gaps.filter((gap) => gap > budgetMs * 1.5).length,
    over_33ms: gaps.filter((gap) => gap > 33.4).length,
    over_100ms: gaps.filter((gap) => gap > 100).length,
  };
}

/** rAF のタイムスタンプ列 → 隣との差。 */
export function gapsOf(timestamps: readonly number[]): number[] {
  const gaps: number[] = [];
  for (let index = 1; index < timestamps.length; index += 1) {
    gaps.push((timestamps[index] ?? 0) - (timestamps[index - 1] ?? 0));
  }
  return gaps;
}

// ---------------------------------------------------------------------------
// React Profiler
// ---------------------------------------------------------------------------

export type ProfilerRecord = {
  id: string;
  phase: "mount" | "update" | "nested-update";
  actual_ms: number;
  base_ms: number;
  /** performance.now() 基準のコミット時刻。 */
  commit_at: number;
};

export type ProfilerSummary = {
  commits: number;
  mounts: number;
  updates: number;
  actual_ms: SampleSummary;
  /** メモ化なしで全部描き直したときの推定コスト。actual との差が小さいほどメモ化が効いていない。 */
  base_ms: SampleSummary;
  actual_total_ms: number;
};

export function summarizeProfiler(records: readonly ProfilerRecord[]): ProfilerSummary {
  return {
    commits: records.length,
    mounts: records.filter((record) => record.phase === "mount").length,
    updates: records.filter((record) => record.phase !== "mount").length,
    actual_ms: summarizeSamples(records.map((record) => record.actual_ms)),
    base_ms: summarizeSamples(records.map((record) => record.base_ms)),
    actual_total_ms: records.reduce((sum, record) => sum + record.actual_ms, 0),
  };
}

export function summarizeProfilerById(records: readonly ProfilerRecord[]): Record<string, ProfilerSummary> {
  const ids = [...new Set(records.map((record) => record.id))].sort();
  return Object.fromEntries(ids.map((id) => [id, summarizeProfiler(records.filter((record) => record.id === id))]));
}

// ---------------------------------------------------------------------------
// 画像
// ---------------------------------------------------------------------------

export type ImageEvent = { at: number; ok: boolean };
export type ResourceSample = { duration_ms: number; transfer_size: number; decoded_body_size: number };

export type ImageSummary = {
  loaded: number;
  errored: number;
  /** シナリオ開始から最後の load までの ms。load が無ければ null。 */
  last_load_after_ms: number | null;
  /** Resource Timing が拾えた件数（バッファ溢れ・非対応なら 0 や loaded より少なくなる）。 */
  resource_entries: number;
  resource_duration_ms: SampleSummary;
  /** transferSize===0 かつ decodedBodySize>0 の件数。ブラウザキャッシュから返ったと推定されるもの（推定であって確証ではない）。 */
  cache_like: number;
};

export function summarizeImages(events: readonly ImageEvent[], resources: readonly ResourceSample[], startedAt: number): ImageSummary {
  const loadTimes = events.filter((event) => event.ok).map((event) => event.at);
  const last = loadTimes.length ? loadTimes.reduce((max, at) => Math.max(max, at), -Infinity) : null;
  return {
    loaded: loadTimes.length,
    errored: events.filter((event) => !event.ok).length,
    last_load_after_ms: last === null ? null : last - startedAt,
    resource_entries: resources.length,
    resource_duration_ms: summarizeSamples(resources.map((resource) => resource.duration_ms)),
    cache_like: resources.filter((resource) => resource.transfer_size === 0 && resource.decoded_body_size > 0).length,
  };
}

// ---------------------------------------------------------------------------
// DOM
// ---------------------------------------------------------------------------

export type DomCounts = {
  /** bench ルート配下の全要素数。 */
  elements: number;
  imgs: number;
  /** `complete` な <img>（読み込み済みまたは失敗）。lazy で未要求のものは false。 */
  imgs_complete: number;
  group_cards: number;
  strip_cards: number;
};

// ---------------------------------------------------------------------------
// 結果の型
// ---------------------------------------------------------------------------

export type ScenarioId =
  | "mount_list"
  | "scroll_list"
  | "select_group_strip"
  | "accept_toggle_list"
  | "accept_toggle_strip"
  | "return_to_list"
  | "scroll_strip";

export type ScenarioResult = {
  id: ScenarioId;
  title: string;
  /** 実行時に使ったパラメータ。再現のために全部出す。 */
  params: Record<string, number | string | boolean>;
  /** 操作 1 回ぶんの値の並び（ms）。順序を保つので、後半に悪化する様子も後から読める。 */
  operations: number;
  /** 操作開始 → 最初のコミット（Profiler の commitTime）。同期描画の目安。 */
  to_commit_ms: SampleSummary | null;
  /** 操作開始 → その後 2 回目の rAF。paint の代理観測で、実際の合成完了ではない。 */
  to_frame_ms: SampleSummary | null;
  frame_gaps: FrameGapSummary | null;
  profiler: Record<string, ProfilerSummary>;
  dom_before: DomCounts | null;
  dom_after: DomCounts | null;
  images: ImageSummary;
  /** シナリオ固有の追加系列(例: 画像が落ち着くまでの ms)。 */
  extra: Record<string, SampleSummary>;
  /** 長いタスク(50ms超)の件数。PerformanceObserver("longtask") 非対応環境(WebKit 等)では null。 */
  long_tasks: { count: number; total_ms: number; max_ms: number } | null;
  warnings: string[];
  /** 操作ごとの生値(ms)。debug / 再集計用。 */
  raw: { to_commit_ms: number[]; to_frame_ms: number[]; frame_gaps_ms: number[] };
};

export type BootTiming = {
  /** React の render 開始(`createRoot().render()` の直前)→ 最初の一覧コミット(ms)。manifest の fetch/検証/変換は含めない。 */
  to_first_commit_ms: number | null;
  /** 〃(render 開始)→ 2 回目の rAF。 */
  to_first_frame_ms: number | null;
  /** エントリ評価開始 → render 開始。manifest の fetch・検証・Analysis 変換・配信ヘッダ確認(HEAD)を含む。 */
  pre_render_ms: number;
  /** エントリ評価開始 → 最初のコミット。fetch/検証を含む全体値(= pre_render_ms + to_first_commit_ms)。 */
  from_entry_to_first_commit_ms: number | null;
  /** エントリ評価開始 → 2 回目の rAF。fetch/検証を含む全体値。 */
  from_entry_to_first_frame_ms: number;
  /** 最初のコミットの root Profiler actualDuration(mount)。React が dev ビルドだと過大、非 profiling の本番ビルドだと null。 */
  mount_actual_ms: number | null;
};

export const LIMITATIONS: readonly string[] = [
  "計測しているのは WebView/ブラウザ内の React 描画・DOM・スクロールフレーム・サムネ読み込みだけ。Rust 側の解析、SMB の I/O、Tauri IPC、asset プロトコル配信は含まない(サムネは dev/preview サーバーが同一オリジンの HTTP で配る)。",
  "Chromium(Playwright 等)の結果を WKWebView(Tauri の実アプリ)の値として扱わない。JS エンジン・レイアウト・画像デコード・遅延読み込みのしきい値・GPU 合成が違う。実アプリ相当の数字が要るなら WebView 側で同じ API を呼び直す。",
  "操作回数・sample 数が少ない p95/p99 は最大値に近い参考値。`p95_reliable`(n>=20) / `p99_reliable`(n>=100) が false の値は分布の裾の推定として読まない。",
  "実メタデータと合成(1000/5000 グループ等)は別物。グループ数と写真数も別物で、報告には両方を併記する。合成は単純複製に近いメタデータ・使い回しのサムネ・同一サイズのグループ構成なので、サムネキャッシュのヒット率と画像デコード負荷が実運用より軽く見える。`manifest.stats` の unique_thumb_ratio / duplicate_group_signature_ratio を必ず併記する。",
  "to_frame_ms は『操作 → 2 回目の requestAnimationFrame』という paint の代理観測。実際の合成・表示完了は確認していない。",
  "コミット時間は React Profiler の actualDuration/commitTime。React が dev ビルドのときは本番より遅い(`environment.react_build` を確認)。",
  "操作はハーネス自身の DOM(GroupList/GroupStrip + 実 reducer)で再生しており、App.tsx の他の副作用(OperationPanel・キー入力・Tauri 呼び出し・確認ダイアログ)は含まない。",
  "画像デコード時間は直接測っていない。load イベントと Resource Timing の範囲(ネットワーク + ブラウザキャッシュ)まで。",
  "採用トグル(accept_toggle_*)と『戻る』(backToList)は DOM のクリックではなく reducer への直接 dispatch。グループ選択(select_group_strip)と一覧カードを開く操作だけが実 onClick を通る。操作回数がグループ数を超えると同じグループを繰り返しトグルするので、採用の追加と取消が混ざる(該当時は warnings に出る)。",
  "Profiler の合計(profiler.*.actual_total_ms・commits)には、観測窓の中で行う準備操作(mount_list の unmount、return_to_list の openGroup など)のコミットも入る。to_commit_ms / to_frame_ms は測定対象の操作 1 回ぶんだけ。両者を混ぜて『操作 1 回の費用』と読まない。strip 系の初回 mount は観測前の setup に移してある。",
  "GroupStrip は選択変更のたびに選択カードへ smooth scroll する(prefers-reduced-motion でなければ)。select_group_strip では前の操作の smooth scroll が次の操作に重なる。本番の ←→ 連打と同じ挙動込みの値で、reduced motion には固定していない。",
  "サムネ配信ヘッダ(キャッシュ方針)はサムネ 1 枚への HEAD で確認する。取れなかったときは environment.thumb_cache_policy_source が unobservable になり、キャッシュ方針は不明として読む。",
  "画像の『落ち着いた』は load/error が 200ms 来ず、かつ要求済みで未完了の <img> が 0。lazy の未要求は currentSrc とビューポート±3000px で除外する近似で、ブラウザの実際の先読み幅とは一致しない。timeout で打ち切った場合は settled=false として warnings に残す。",
];

export type BenchEnvironment = {
  user_agent: string;
  device_pixel_ratio: number;
  viewport: { width: number; height: number };
  hardware_concurrency: number | null;
  /** `import.meta.env.DEV` と profiling alias の有無から決める。 */
  react_build: "development" | "production-profiling" | "production";
  is_tauri: boolean;
  longtask_supported: boolean;
  prefers_reduced_motion: boolean;
  /** manifest 配信時の `x-bench-thumb-cache` ヘッダ(サーバー設定の記録)。合成をページ内生成したときは null。 */
  thumb_cache_policy: string | null;
  /** policy の出所。head_probe=サムネ 1 枚への HEAD 応答の `x-bench-thumb-cache`、unobservable=取れなかった(理由は _note)。 */
  thumb_cache_policy_source: "head_probe" | "unobservable";
  thumb_cache_policy_note: string | null;
};

export type RepresentativenessBlock = {
  dataset_kind: "real_metadata" | "synthetic";
  /** manifestStats() の値。 */
  stats: import("./manifest").ManifestStats;
  /** 報告者が埋める自由記述。ハーネスは中身を解釈しない。 */
  reporter_notes: string;
  /** 固定の注意書き(LIMITATIONS のうち代表性に関わるもの)。 */
  caveats: string[];
};

export type RunFailure = { scenario: string; message: string };

export type BenchReport = {
  schema: "geoframe.browse-bench.report/1";
  generated_at: string;
  environment: BenchEnvironment;
  boot: BootTiming | null;
  /** 一目で取り違えないための要約。groups と photos は別の数。 */
  dataset: { kind: "real_metadata" | "synthetic"; id: string; groups: number; photos: number; source_groups: number | null };
  representativeness: RepresentativenessBlock;
  /** failures があれば incomplete。scenarios には失敗前に完了した分だけが残る(成功した報告として読まない)。 */
  run_status: "complete" | "incomplete";
  failures: RunFailure[];
  scenarios: ScenarioResult[];
  limitations: readonly string[];
};
