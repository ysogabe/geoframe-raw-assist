/**
 * 手動プレビュー（「プレビュー現像」ボタン → 画像表示）の任意計測フック（Issue #141 Stage1）。
 *
 * - **既定 OFF**。ON は devtools で `localStorage.setItem("geoframe.previewTiming", "1")`（次のクリックから有効、
 *   `removeItem` で OFF）。OFF のとき `begin()` は null を返し、以降の呼び出しは何もしない。
 *   通常の UX・キャッシュ・画質・候補には影響しない（計測は invoke の前後と、表示中の `<img>` の load イベントを
 *   観測するだけで、画像を先読みしたり再描画したりしない）。
 * - 起点: 「プレビュー現像」ボタンのハンドラ（`runDevelop("preview")`）の入口で取った `handlerEntryMs` を
 *   `developPreviewCoreImage` の第 3 引数で渡す。渡されなければ起点は API 呼び出しになり、行の `origin` が
 *   `api_call` になる（ボタン→画像とは呼ばない）。
 * - 計測する区間（すべて `performance.now()` 起点の ms。同じ request_id の 1 行に集約する）:
 *   `pre_invoke_ms`（ハンドラ入口 → invoke 開始。dispatch など）、
 *   `invoke_wall_ms`（Tauri invoke の開始 → 戻り。Rust の枠待ち・キャッシュ・CLI・IPC を含む）、
 *   `invoke_to_load_ms`（戻り → `<img>` の load）、`load_to_decode_ms`（`img.decode()`）、
 *   `decode_to_raf_ms`（decode 後の 2 回目の requestAnimationFrame まで）。
 * - **onload / decode / rAF を「描画完了」と断定しない**。これは paint の代理観測（decode 後に 2 フレーム進んだ）で、
 *   実際の合成・表示は確認していない（`paint_proxy` に明記）。
 * - 記録しない: ファイルパス・表示 URL（`output_url` は絶対 APPDATA パスを含むので内部の突合にだけ使い、行・export・
 *   console に出さない）・GPS・画像の値・調整値。行にあるのは request_id・撮影 ID・時間・寸法・キャッシュ有無・
 *   CLI の elapsed_ms・エラーコードだけ。ローカルの `window.__geoframePreviewTimings`（最大 200 行）と console に出すのみで、
 *   送信はしない。
 * - **測らないもの**: クリックイベント → ハンドラ入口までの入力遅延（`event.timeStamp` は取っていない）、スライダー入力 → 数値表示の応答
 *   （画像 E2E とは別の指標なので、この行と混ぜない）、実際の paint。
 */
export const PREVIEW_TIMING_STORAGE_KEY = "geoframe.previewTiming";
const RING_SIZE = 200;
/** invoke が戻ってから load が来なければ記録を閉じる（同じ src が表示済みで load が再発火しない場合など）。 */
const IMAGE_WAIT_MS = 4000;
export const PAINT_PROXY_NOTE = "decode() resolved + 2 requestAnimationFrame; proxy for paint, not a confirmed paint";

export type PreviewTimingStatus = "ok" | "invoke_error" | "image_not_observed" | "decode_failed";

export type PreviewTimingRow = {
  request_id: string;
  /** 撮影 ID（カードの識別子。パスではない）。 */
  capture_id: string;
  /** `button_handler`: ボタンのハンドラ入口が起点。`api_call`: API 呼び出しが起点（ハンドラ前段は未計測）。 */
  origin: "button_handler" | "api_call";
  kind: "manual_preview_button_to_image" | "api_call_to_image";
  status: PreviewTimingStatus;
  /** ハンドラ入口 → invoke 開始。origin が api_call のとき null。 */
  pre_invoke_ms: number | null;
  invoke_wall_ms: number;
  invoke_to_load_ms: number | null;
  load_to_decode_ms: number | null;
  decode_to_raf_ms: number | null;
  /** invoke 開始 → 2 回目の rAF（image_not_observed / invoke_error では null）。 */
  invoke_start_to_raf_ms: number | null;
  /** 起点（ハンドラ入口、無ければ API 呼び出し）→ 2 回目の rAF。`origin` で何からの値かを読む。 */
  origin_to_raf_ms: number | null;
  cache_hit: boolean | null;
  /** CLI 応答の elapsed_ms（キャッシュヒット時は最初に現像したときの値）。 */
  cli_elapsed_ms: { decode?: number; render_encode?: number; total?: number } | null;
  /** invoke_wall − CLI total。キャッシュヒット・不明のとき null。Rust の枠待ち/IPC/起動/書き込みの上限。 */
  invoke_minus_cli_ms: number | null;
  width: number | null;
  height: number | null;
  error_code: string | null;
  paint_proxy: string;
};

export type PreviewInvokeOutcome = {
  /** 表示に使う URL。`<img src>` と一致させて突合に使うだけで、行には残さない。 */
  displayUrl: string;
  cacheHit: boolean;
  elapsed: { decode?: number; render_encode?: number; total?: number } | null;
  width: number;
  height: number;
};

export type PreviewTimingDeps = {
  enabled: () => boolean;
  now: () => number;
  requestFrame: (callback: () => void) => void;
  setTimer: (callback: () => void, ms: number) => unknown;
  clearTimer: (handle: unknown) => void;
  newId: () => string;
  sink: (row: PreviewTimingRow) => void;
};

/** `begin` に渡す任意の文脈。 */
export type PreviewTimingContext = {
  captureId: string;
  /** ボタンのハンドラ入口の `performance.now()`（`previewTiming.now()`）。 */
  handlerEntryMs?: number;
};

export type PreviewTimer = {
  invokeReturned: (outcome: PreviewInvokeOutcome) => void;
  invokeFailed: (code: string) => void;
};

const round = (value: number) => Math.round(value * 10) / 10;

type Pending = {
  context: PreviewTimingContext;
  preInvoke: number | null;
  invokeWall: number;
  tReturned: number;
  tStart: number;
  outcome: PreviewInvokeOutcome;
  timer: unknown;
};

export function createPreviewTimingRecorder(deps: PreviewTimingDeps) {
  let pending: { id: string; entry: Pending } | null = null;

  type Rest = Omit<
    PreviewTimingRow,
    "request_id" | "capture_id" | "origin" | "kind" | "pre_invoke_ms" | "paint_proxy" | "origin_to_raf_ms"
  > & { origin_to_raf_ms?: number | null };

  function finish(id: string, context: PreviewTimingContext, preInvoke: number | null, row: Rest) {
    const fromHandler = preInvoke !== null;
    // sink（console / バッファ）の失敗も計測の外に出さない。
    try {
      deps.sink({
        request_id: id,
        capture_id: context.captureId,
        origin: fromHandler ? "button_handler" : "api_call",
        kind: fromHandler ? "manual_preview_button_to_image" : "api_call_to_image",
        pre_invoke_ms: preInvoke,
        paint_proxy: PAINT_PROXY_NOTE,
        origin_to_raf_ms: null,
        ...row,
      });
    } catch {
      // 計測は best effort
    }
  }

  function rowBase(outcome: PreviewInvokeOutcome | null, invokeWall: number) {
    const cli = outcome?.elapsed ?? null;
    const total = cli?.total;
    return {
      invoke_wall_ms: round(invokeWall),
      cache_hit: outcome ? outcome.cacheHit : null,
      cli_elapsed_ms: cli,
      invoke_minus_cli_ms:
        outcome && !outcome.cacheHit && typeof total === "number" ? round(invokeWall - total) : null,
      width: outcome ? outcome.width : null,
      height: outcome ? outcome.height : null,
    };
  }

  /** 計測 ON のとき計時を始める。OFF なら null（呼び出し側は `timer?.` で何もしない）。 */
  function begin(context: PreviewTimingContext): PreviewTimer | null {
    if (!deps.enabled()) return null;
    const id = deps.newId();
    const tStart = deps.now();
    const preInvoke = context.handlerEntryMs === undefined ? null : round(tStart - context.handlerEntryMs);
    return {
      invokeReturned(outcome) {
        const tReturned = deps.now();
        const invokeWall = tReturned - tStart;
        if (pending) {
          // 前の行がまだ閉じていなければ先に閉じる（UI は同時 1 本だが、取りこぼしを残さない）。
          deps.clearTimer(pending.entry.timer);
          const prev = pending;
          pending = null;
          finish(prev.id, prev.entry.context, prev.entry.preInvoke, {
            status: "image_not_observed",
            ...rowBase(prev.entry.outcome, prev.entry.invokeWall),
            invoke_to_load_ms: null, load_to_decode_ms: null, decode_to_raf_ms: null,
            invoke_start_to_raf_ms: null, error_code: null,
          });
        }
        const timer = deps.setTimer(() => {
          if (!pending || pending.id !== id) return;
          pending = null;
          finish(id, context, preInvoke, {
            status: "image_not_observed",
            ...rowBase(outcome, invokeWall),
            invoke_to_load_ms: null, load_to_decode_ms: null, decode_to_raf_ms: null,
            invoke_start_to_raf_ms: null, error_code: null,
          });
        }, IMAGE_WAIT_MS);
        pending = { id, entry: { context, preInvoke, invokeWall, tReturned, tStart, outcome, timer } };
      },
      invokeFailed(code) {
        finish(id, context, preInvoke, {
          status: "invoke_error",
          ...rowBase(null, deps.now() - tStart),
          invoke_to_load_ms: null, load_to_decode_ms: null, decode_to_raf_ms: null,
          invoke_start_to_raf_ms: null, error_code: code,
        });
      },
    };
  }

  /**
   * 表示中の `<img>` の load イベントから呼ぶ。`url` が待機中の要求の表示 URL と違えば何もしない
   * （別の画像の load・計測 OFF のときは pending が無いので即 return）。
   */
  function imageLoaded(url: string, decode: () => Promise<void>): void {
    const current = pending;
    if (!current || current.entry.outcome.displayUrl !== url) return;
    pending = null;
    deps.clearTimer(current.entry.timer);
    const { id, entry } = current;
    const tLoad = deps.now();
    let decoded: Promise<void>;
    try {
      decoded = decode();
    } catch (error) {
      decoded = Promise.reject(error);
    }
    void decoded.then(
      () => {
        const tDecode = deps.now();
        deps.requestFrame(() => {
          deps.requestFrame(() => {
            const tRaf = deps.now();
            finish(id, entry.context, entry.preInvoke, {
              status: "ok",
              ...rowBase(entry.outcome, entry.invokeWall),
              invoke_to_load_ms: round(tLoad - entry.tReturned),
              load_to_decode_ms: round(tDecode - tLoad),
              decode_to_raf_ms: round(tRaf - tDecode),
              invoke_start_to_raf_ms: round(tRaf - entry.tStart),
              origin_to_raf_ms: round(tRaf - entry.tStart + (entry.preInvoke ?? 0)),
              error_code: null,
            });
          });
        });
      },
      () => {
        finish(id, entry.context, entry.preInvoke, {
          status: "decode_failed",
          ...rowBase(entry.outcome, entry.invokeWall),
          invoke_to_load_ms: round(tLoad - entry.tReturned),
          load_to_decode_ms: null, decode_to_raf_ms: null, invoke_start_to_raf_ms: null,
          error_code: null,
        });
      },
    );
  }

  /** ハンドラ入口の時刻。取得に失敗しても製品の処理を止めない（undefined = 起点なし）。 */
  function now(): number | undefined {
    try {
      return deps.now();
    } catch {
      return undefined;
    }
  }

  return { begin, imageLoaded, now };
}

declare global {
  interface Window {
    __geoframePreviewTimings?: PreviewTimingRow[];
  }
}

function storageEnabled(): boolean {
  try {
    return globalThis.localStorage?.getItem(PREVIEW_TIMING_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function defaultSink(row: PreviewTimingRow): void {
  if (typeof window === "undefined") return;
  const rows = (window.__geoframePreviewTimings ??= []);
  rows.push(row);
  if (rows.length > RING_SIZE) rows.splice(0, rows.length - RING_SIZE);
  console.info("[geoframe-preview-timing]", JSON.stringify(row));
}

let sequence = 0;

/** アプリ用の既定インスタンス。 */
export const previewTiming = createPreviewTimingRecorder({
  enabled: storageEnabled,
  now: () => performance.now(),
  requestFrame: (callback) => requestAnimationFrame(() => callback()),
  setTimer: (callback, ms) => setTimeout(callback, ms),
  clearTimer: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
  newId: () => `pv-${Date.now().toString(36)}-${(sequence += 1)}`,
  sink: defaultSink,
});
