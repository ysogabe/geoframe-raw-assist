import { convertFileSrc, invoke } from "@tauri-apps/api/core";
import type {
  Analysis,
  AnalysisProgress,
  AnalysisResponse,
  AnalyzeResponse,
  CaptureId,
  ExportItem,
  ExportResponse,
  ImportToPhotosRequest,
  ImportToPhotosResponse,
  PresetInfo,
  SourceFolderErrorReason,
  SourceFolderInfo,
} from "./types";

/**
 * サムネイルなどのローカル絶対パス（`$APPDATA/thumbnails/...`）を asset プロトコルの URL にする。
 * すでに URL のもの（`DevelopedResult.output_url` など）はそのまま返すので、通してよい。
 */
export function assetUrl(path: string): string {
  return path.startsWith("/") ? convertFileSrc(path) : path;
}

/** Rust 側 URI scheme（src-tauri/src/analysis.rs `source_jpg_protocol`）。 */
export const SOURCE_JPG_SCHEME = "geoframe";

/**
 * カメラ内JPG（現像前）のURL。`geoframe://localhost/source-jpg/DSC12345.JPG`（macOS）。
 *
 * `convertFileSrc("source-jpg/x", "geoframe")` はパス全体を encodeURIComponent するため `source-jpg%2Fx` になり、
 * Rust 側の `request.uri().path()`（デコードされない）が `^/source-jpg/(DSC[0-9]{5})\.JPG$` に一致しない。
 * そのため `/` を残した形をここで組み立てる。
 */
export function sourceJpgUrl(captureId: CaptureId): string {
  return `${SOURCE_JPG_SCHEME}://localhost/source-jpg/${captureId}.JPG`;
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

/** 現像対象フォルダの変更失敗。`reason` で分岐したい呼び出し側のために理由コードを保持する。 */
export class SourceFolderApiError extends Error {
  /** Rust が `reason` を返さなかった場合は null。 */
  readonly reason: SourceFolderErrorReason | null;

  constructor(message: string, reason: SourceFolderErrorReason | null) {
    super(message);
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
  return new SourceFolderApiError(message, reason);
}

/**
 * プリセット一覧（`PresetInfo[]`。Issue #80）。定義の正は Rust（src-tauri/src/presets.rs）。
 * 取得に失敗した・空だったときは `[]`（呼び出し側は何も出さず、プリセット選択 UI が空のままになる）。
 */
export async function getPresets(): Promise<PresetInfo[]> {
  try {
    const presets = await invoke<PresetInfo[]>("get_presets");
    return Array.isArray(presets) ? presets : [];
  } catch {
    return [];
  }
}

export async function getAnalysis(): Promise<AnalysisResponse> {
  // 未設定（SOURCE_ROOT_NOT_CONFIGURED）の reject はそのまま投げる。呼び出し側が isSourceRootNotConfigured で分岐する。
  return normalizeAnalysisResponse(await invoke<unknown>("get_analysis"));
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
  const status = await invoke<AnalyzeResponse["status"]>("start_analysis", { force });
  return { status };
}

export async function exportSelection(batchName: string, items: ExportItem[]): Promise<ExportResponse> {
  try {
    return await invoke<ExportResponse>("export_selection", { batchName, items });
  } catch (error) {
    // Rust の String エラーも App の messageOf で理由を表示できるようにする。
    throw typeof error === "string" ? new Error(error) : error;
  }
}

/** 現在の現像対象フォルダ。 */
export async function getSourceFolder(): Promise<SourceFolderInfo> {
  return invoke<SourceFolderInfo>("get_source_folder");
}

/**
 * 現像対象フォルダを変更する。失敗時は SourceFolderApiError を throw するので、
 * 呼び出し側は `reason` で分岐すること（特に permission_denied は「写真が無い」と混同させてはならない）。
 */
export async function setSourceFolder(path: string): Promise<SourceFolderInfo> {
  try {
    return await invoke<SourceFolderInfo>("set_source_folder", { path });
  } catch (error) {
    throw sourceFolderApiErrorFromInvoke(error);
  }
}

/** 書き出し済みバッチを Apple「写真」へ取り込む（Issue #115）。 */
export async function importToPhotos(request: ImportToPhotosRequest): Promise<ImportToPhotosResponse> {
  return await invoke<ImportToPhotosResponse>("import_to_photos", { batchName: request.batch_name, album: request.album });
}

/** 書き出したフォルダを Finder で表示する。 */
export async function revealExport(path: string): Promise<void> {
  await invoke("reveal_export", { path });
}
