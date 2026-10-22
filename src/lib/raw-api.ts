import { convertFileSrc, invoke } from "@tauri-apps/api/core";
import { isTauri } from "./setup";
import type {
  Adjustments,
  Analysis,
  AnalysisProgress,
  AnalysisResponse,
  AnalyzeResponse,
  CaptureId,
  DevelopedResult,
  DevelopmentCandidatesResponse,
  DriveUploadStartResponse,
  DriveUploadState,
  ExportItem,
  ExportResponse,
  HealthResponse,
  ImportToPhotosResponse,
  MergedResult,
  PresetInfo,
  PresetName,
  SourceFolderErrorReason,
  SourceFolderInfo,
} from "./types";

/** ローカル RAW エンジンのベースURL（docs/API_CONTRACT.md §0）。 */
export const LOCAL_RAW_API = "http://127.0.0.1:8788";

/**
 * `/thumbs/...` `/source-jpg/...` のような相対パスを絶対URLにする。
 * `DevelopedResult.output_url` は既に完全URLなので、この関数を通してはならない。
 */
export function assetUrl(path: string): string {
  if (path.startsWith("http")) return path;
  // Tauri: Rust 解析の photos[*].thumb は $APPDATA/thumbnails/ 配下の絶対パス。asset protocol で配信する。
  // Python 由来の相対パス（"/thumbs/..."）は従来どおりエンジンの HTTP を指す。
  if (isTauri() && path.startsWith("/") && !path.startsWith("/thumbs/")) return convertFileSrc(path);
  return `${LOCAL_RAW_API}${path}`;
}

/** Rust 側 URI scheme（src-tauri/src/analysis.rs `source_jpg_protocol`）。 */
export const SOURCE_JPG_SCHEME = "geoframe";

/**
 * カメラ内JPG（現像前）のURL。
 *
 * Tauri では `geoframe://localhost/source-jpg/DSC12345.JPG`（macOS）。`convertFileSrc("source-jpg/x", "geoframe")` は
 * パス全体を encodeURIComponent するため `source-jpg%2Fx` になり、Rust 側の `request.uri().path()`（デコードされない）が
 * `^/source-jpg/(DSC[0-9]{5})\.JPG$` に一致しない。そのため `/` を残した形をここで組み立てる。
 */
export function sourceJpgUrl(captureId: CaptureId): string {
  if (isTauri()) return `${SOURCE_JPG_SCHEME}://localhost/source-jpg/${captureId}.JPG`;
  return `${LOCAL_RAW_API}/source-jpg/${captureId}.JPG`;
}

/** `get_analysis` が現像元フォルダ未設定で reject したときのコード（Rust `SOURCE_ROOT_NOT_CONFIGURED`）。 */
export const SOURCE_ROOT_NOT_CONFIGURED = "SOURCE_ROOT_NOT_CONFIGURED";

export function isSourceRootNotConfigured(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { code?: unknown }).code === SOURCE_ROOT_NOT_CONFIGURED
  );
}

class RawApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "RawApiError";
    this.status = status;
  }
}

/**
 * POST /api/source-folder の失敗（HTTP 400）。`reason` で分岐したい呼び出し側の
 * ために、他のエンドポイントと違って理由コードを保持したまま throw する。
 * RawApiError を継承しているので isEndpointUnavailable もそのまま効く。
 */
export class SourceFolderApiError extends RawApiError {
  /** サーバが `reason` を返さなかった場合（想定外の 5xx 等）は null。 */
  readonly reason: SourceFolderErrorReason | null;

  constructor(message: string, status: number, reason: SourceFolderErrorReason | null) {
    super(message, status);
    this.name = "SourceFolderApiError";
    this.reason = reason;
  }
}

export function isSourceFolderApiError(error: unknown): error is SourceFolderApiError {
  return error instanceof SourceFolderApiError;
}

/** Tauri の invoke rejection payload を既存 UI 向けエラーへ写す。 */
export function sourceFolderApiErrorFromInvoke(error: unknown): SourceFolderApiError {
  const body = (error ?? {}) as { code?: unknown; reason?: unknown; message?: unknown };
  const message = typeof body.message === "string" ? body.message : "現像対象フォルダを変更できませんでした。";
  const reason = typeof body.reason === "string" ? (body.reason as SourceFolderErrorReason) : null;
  return new SourceFolderApiError(message, typeof body.code === "string" ? 400 : 0, reason);
}

/** 未実装エンドポイント（404）・接続不能を呼び出し側が判別するための印。 */
export function isEndpointUnavailable(error: unknown): boolean {
  if (error instanceof RawApiError) return error.status === 404;
  // fetch 自体が失敗した場合（サーバ停止・CORS 遮断）は TypeError になる。
  return error instanceof TypeError;
}

async function readJson(response: Response, fallback: string): Promise<unknown> {
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const message =
      payload && typeof payload === "object" && typeof (payload as { error?: unknown }).error === "string"
        ? (payload as { error: string }).error
        : fallback;
    throw new RawApiError(message, response.status);
  }
  return payload;
}

async function getJson(path: string, fallback: string): Promise<unknown> {
  const response = await fetch(`${LOCAL_RAW_API}${path}`);
  return readJson(response, fallback);
}

async function postJson(path: string, body: unknown, fallback: string): Promise<unknown> {
  const response = await fetch(`${LOCAL_RAW_API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // 空ボディはサーバ側で 400 になるため、必ず JSON を送る。
    body: JSON.stringify(body ?? {}),
  });
  return readJson(response, fallback);
}

export async function getHealth(): Promise<HealthResponse> {
  return (await getJson("/api/health", "ローカルRAWエンジンに接続できません。")) as HealthResponse;
}

/**
 * プリセット一覧（`PresetInfo[]`。Issue #80）。定義の正は Rust（src-tauri/src/presets.rs）。
 *
 * - Tauri 実行時は `invoke("get_presets")` を優先する（Python エンジンを経由しない）。
 * - ブラウザモード（isTauri() === false）では invoke を呼ばず、`/api/health` の `presets`（= fallback）を返す。
 * - Tauri 側の取得に失敗した・空だったときも fallback に戻る。#81 から数値は意図して違う（Rust = Core Image 向け、
 *   Python = rawpy 向けの旧値）。名前・並び・キー構成の一致は tests/test_presets_fixture.py が 2 本のフィクスチャで担保。
 */
export async function getPresets(fallback: PresetInfo[]): Promise<PresetInfo[]> {
  if (!isTauri()) return fallback;
  try {
    const presets = await invoke<PresetInfo[]>("get_presets");
    return Array.isArray(presets) && presets.length > 0 ? presets : fallback;
  } catch {
    return fallback;
  }
}

export async function getAnalysis(): Promise<AnalysisResponse> {
  // 未設定（SOURCE_ROOT_NOT_CONFIGURED）の reject はそのまま投げる。呼び出し側が isSourceRootNotConfigured で分岐する。
  if (isTauri()) return normalizeAnalysisResponse(await invoke<unknown>("get_analysis"));
  return (await getJson("/api/analysis", "解析状況を取得できませんでした。")) as AnalysisResponse;
}

/** Rust `get_analysis` の応答を既存の AnalysisResponse に写す（形は同じだが、不正な status は absent に倒す）。 */
export function normalizeAnalysisResponse(raw: unknown): AnalysisResponse {
  const body = (raw ?? {}) as { status?: unknown; progress?: unknown; analysis?: unknown; error?: unknown };
  if (body.status === "ready" && body.analysis) return { status: "ready", analysis: body.analysis as Analysis };
  if (body.status === "running") {
    const p = (body.progress ?? {}) as Partial<AnalysisProgress>;
    return {
      status: "running",
      progress: { stage: typeof p.stage === "string" ? p.stage : "", done: Number(p.done) || 0, total: Number(p.total) || 0 },
    };
  }
  return typeof body.error === "string" && body.error ? { status: "absent", error: body.error } : { status: "absent" };
}

export async function startAnalyze(force = false): Promise<AnalyzeResponse> {
  if (isTauri()) {
    const status = await invoke<AnalyzeResponse["status"]>("start_analysis", { force });
    return { status };
  }
  return (await postJson("/api/analyze", force ? { force: true } : {}, "解析を開始できませんでした。")) as AnalyzeResponse;
}

export type DevelopOptions = {
  adjustments?: Partial<Adjustments>;
  preview?: boolean;
  force?: boolean;
  maxEdge?: number;
};

export async function developRaw(
  captureId: CaptureId,
  preset: PresetName,
  options: DevelopOptions = {},
): Promise<DevelopedResult> {
  const body: Record<string, unknown> = { capture_id: captureId, preset };
  if (options.adjustments && Object.keys(options.adjustments).length > 0) body.adjustments = options.adjustments;
  if (options.preview) body.preview = true;
  if (options.force) body.force = true;
  if (typeof options.maxEdge === "number") body.max_edge = options.maxEdge;
  return (await postJson("/api/raw-develop", body, "RAW現像に失敗しました。")) as DevelopedResult;
}

export type DevelopmentCandidatesOptions = {
  seed?: number;
  budget?: number;
};

/**
 * RAWの縮小デコード（キャッシュ付き）を代理に、engineの後段関数で評価して現像候補を作る。所要時間は1枚あたり数秒〜15秒程度。
 * 返った調整値は選択時にスライダーへ反映するだけで、現像は別操作で実行する。
 */
export async function generateDevelopmentCandidates(
  captureId: CaptureId,
  preset: PresetName,
  options: DevelopmentCandidatesOptions = {},
): Promise<DevelopmentCandidatesResponse> {
  const body: Record<string, unknown> = { capture_id: captureId, preset };
  if (typeof options.seed === "number") body.seed = options.seed;
  if (typeof options.budget === "number") body.budget = options.budget;
  return (await postJson(
    "/api/development-candidates",
    body,
    "現像候補の生成に失敗しました。",
  )) as DevelopmentCandidatesResponse;
}

/**
 * ブラケット合成のオプション。DevelopOptions と同じ形にしてある（呼び出し側が
 * 現像と合成で同じ組み立てを使えるようにするため）。
 */
export type MergeBracketOptions = DevelopOptions;

/**
 * 露出ブラケットの複数フレームを 1 枚に合成する。
 *
 * `captureIds` は 2〜9 枚。範囲外はサーバが HTTP 400 を返すので、呼び出し側は
 * ボタンの活性制御でそこへ到達させないこと。フレーム数ぶんの RAW デコードが
 * 走るため、preview でも数秒、原寸なら数十秒かかる（実行中表示は必須）。
 */
export async function mergeBracket(
  captureIds: CaptureId[],
  preset: PresetName,
  options: MergeBracketOptions = {},
): Promise<MergedResult> {
  const body: Record<string, unknown> = { capture_ids: captureIds, preset };
  if (options.adjustments && Object.keys(options.adjustments).length > 0) body.adjustments = options.adjustments;
  if (options.preview) body.preview = true;
  if (options.force) body.force = true;
  if (typeof options.maxEdge === "number") body.max_edge = options.maxEdge;
  return (await postJson("/api/merge-bracket", body, "ブラケット合成に失敗しました。")) as MergedResult;
}

export async function exportSelection(batchName: string, items: ExportItem[]): Promise<ExportResponse> {
  if (isTauri()) {
    try {
      return await invoke<ExportResponse>("export_selection", { batchName, items });
    } catch (error) {
      // Rust の String エラーも App の messageOf で理由を表示できるようにする。
      throw typeof error === "string" ? new Error(error) : error;
    }
  }
  return (await postJson(
    "/api/export",
    { batch_name: batchName, items },
    "書き出しに失敗しました。",
  )) as ExportResponse;
}

/** 現在の現像対象フォルダ。エンジン未起動・未実装時は isEndpointUnavailable が true になる。 */
export async function getSourceFolder(): Promise<SourceFolderInfo> {
  if (isTauri()) return invoke<SourceFolderInfo>("get_source_folder");
  return (await getJson("/api/source-folder", "現像対象フォルダを取得できませんでした。")) as SourceFolderInfo;
}

/**
 * 現像対象フォルダを変更する。成功時のみ 200 で新しい SourceFolderInfo が返る。
 * 失敗時は SourceFolderApiError を throw するので、呼び出し側は `reason` で
 * 分岐すること（特に permission_denied は「写真が無い」と混同させてはならない）。
 */
export async function setSourceFolder(path: string): Promise<SourceFolderInfo> {
  const fallback = "現像対象フォルダを変更できませんでした。";
  if (isTauri()) {
    try {
      return await invoke<SourceFolderInfo>("set_source_folder", { path });
    } catch (error) {
      throw sourceFolderApiErrorFromInvoke(error);
    }
  }
  const response = await fetch(`${LOCAL_RAW_API}/api/source-folder`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });

  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    // readJson と違い、ここでは `reason` を落とさずに詰め替える。
    const body = (payload ?? {}) as { error?: unknown; reason?: unknown };
    const message = typeof body.error === "string" && body.error ? body.error : fallback;
    const reason = typeof body.reason === "string" ? (body.reason as SourceFolderErrorReason) : null;
    throw new SourceFolderApiError(message, response.status, reason);
  }

  return payload as SourceFolderInfo;
}

export async function importToPhotos(batchName: string, album?: string): Promise<ImportToPhotosResponse> {
  const body: Record<string, unknown> = { batch_name: batchName };
  if (album) body.album = album;
  return (await postJson("/api/import-to-photos", body, "Appleフォトへの登録に失敗しました。")) as ImportToPhotosResponse;
}

export async function startDriveUpload(batchName: string): Promise<DriveUploadStartResponse> {
  return (await postJson(
    "/api/drive-upload",
    { batch_name: batchName },
    "Google Drive アップロードを開始できませんでした。",
  )) as DriveUploadStartResponse;
}

export async function getDriveUploadState(): Promise<DriveUploadState> {
  return (await getJson("/api/drive-upload", "Google Drive アップロードの状況を取得できませんでした。")) as DriveUploadState;
}

/** 書き出したフォルダを Finder で表示する（Tauri 専用）。 */
export async function revealExport(path: string): Promise<void> {
  if (isTauri()) await invoke("reveal_export", { path });
}
