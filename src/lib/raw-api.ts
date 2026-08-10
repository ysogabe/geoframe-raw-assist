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
  PresetName,
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

export async function exportSelection(batchName: string, items: ExportItem[]): Promise<ExportResponse> {
  return (await postJson(
    "/api/export",
    { batch_name: batchName, items },
    "書き出しに失敗しました。",
  )) as ExportResponse;
}

export async function importToPhotos(batchName: string, album?: string): Promise<ImportToPhotosResponse> {
  const body: Record<string, unknown> = { batch_name: batchName };
  if (album) body.album = album;
  return (await postJson("/api/import-to-photos", body, "Appleフォトへの登録に失敗しました。")) as ImportToPhotosResponse;
}
