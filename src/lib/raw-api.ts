import type {
  Adjustments,
  AnalysisResponse,
  AnalyzeResponse,
  CaptureId,
  DevelopedResult,
  ExportItem,
  ExportResponse,
  HealthResponse,
  ImportToPhotosResponse,
  MergedResult,
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
  return path.startsWith("http") ? path : `${LOCAL_RAW_API}${path}`;
}

/** カメラ内JPG（現像前）のURL。 */
export function sourceJpgUrl(captureId: CaptureId): string {
  return `${LOCAL_RAW_API}/source-jpg/${captureId}.JPG`;
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

export async function getAnalysis(): Promise<AnalysisResponse> {
  return (await getJson("/api/analysis", "解析状況を取得できませんでした。")) as AnalysisResponse;
}

export async function startAnalyze(force = false): Promise<AnalyzeResponse> {
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
  return (await postJson(
    "/api/export",
    { batch_name: batchName, items },
    "書き出しに失敗しました。",
  )) as ExportResponse;
}

/** 現在の現像対象フォルダ。エンジン未起動・未実装時は isEndpointUnavailable が true になる。 */
export async function getSourceFolder(): Promise<SourceFolderInfo> {
  return (await getJson("/api/source-folder", "現像対象フォルダを取得できませんでした。")) as SourceFolderInfo;
}

/**
 * 現像対象フォルダを変更する。成功時のみ 200 で新しい SourceFolderInfo が返る。
 * 失敗時は SourceFolderApiError を throw するので、呼び出し側は `reason` で
 * 分岐すること（特に permission_denied は「写真が無い」と混同させてはならない）。
 */
export async function setSourceFolder(path: string): Promise<SourceFolderInfo> {
  const fallback = "現像対象フォルダを変更できませんでした。";
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
