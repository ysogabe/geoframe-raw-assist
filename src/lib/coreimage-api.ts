import { convertFileSrc, invoke } from "@tauri-apps/api/core";
import { isTauri } from "./setup";
import type { Adjustments, CaptureId, DevelopedResult, DevelopEngine, PresetInfo } from "./types";

/**
 * Core Image プレビュー現像（Issue #72 / #73）。Python エンジンを経由せず、Tauri(Rust) が
 * Swift CLI `geoframe-ci-develop` を直接呼ぶ。Rust 側は src-tauri/src/coreimage.rs。
 * 設計: docs/design/coreimage-preview-adjustments.md §2.6・§2.7・§4.3・§4.4。
 *
 * デスクトップアプリでは既定で有効。`GEOFRAME_PREVIEW_ENGINE=rawpy` で起動すると Python の
 * プレビューに戻る（切り戻しフラグ）。ブラウザモード（isTauri() === false）では常に
 * "rawpy" で、invoke を一切呼ばない。
 */

/** CLI の計測値（ミリ秒）。キャッシュヒット時は最初に現像したときの値。 */
export type CoreImageElapsedMs = { decode?: number; render_encode?: number; total?: number } | null;

/** Rust 側 `CoreImagePreviewResult`（§4.3）とフィールド名を一致させること。 */
export type CoreImagePreviewResult = {
  capture_id: CaptureId;
  /** 出力 JPEG の絶対パス（`<app_data_dir>/coreimage/preview/` 配下）。 */
  output_path: string;
  /** 来歴 JSON（CLI 応答全文）の絶対パス。 */
  provenance_path: string;
  width: number;
  height: number;
  elapsed_ms: CoreImageElapsedMs;
  cache_hit: boolean;
  /** キャッシュ鍵 hash16。 */
  params_hash: string;
  decoder_version: string;
  cli_version: string;
  macos_version: string;
  /** Rust が正規化（クランプ・float 化）した 9 キー全量。 */
  adjustments: Adjustments;
  /** CLI の applied そのまま。 */
  applied: { lens_correction_enabled?: boolean; [key: string]: unknown };
  /** CLI の source（lens_model / iso / camera_model）。 */
  source: { lens_model?: string | null; iso?: number | null; camera_model?: string | null; [key: string]: unknown };
};

/** Rust 側 `CoreImageError`。CLI の error.code / message もそのまま入る。 */
export type CoreImageError = { code: string; message: string };

let enginePromise: Promise<DevelopEngine> | null = null;

/**
 * プレビュー現像のエンジン。起動中に変わらないので 1 回だけ問い合わせて覚えておく。
 * ブラウザモードは問い合わせずに "rawpy"。問い合わせに失敗したときも "rawpy"（従来経路で動かす）。
 */
export function previewEngine(): Promise<DevelopEngine> {
  if (!isTauri()) return Promise.resolve("rawpy");
  enginePromise ??= invoke<string>("preview_engine")
    .then((value): DevelopEngine => (value === "rawpy" ? "rawpy" : "coreimage"))
    .catch((): DevelopEngine => "rawpy");
  return enginePromise;
}

/** 選択中カットを Core Image でプレビュー現像する。失敗時は CoreImageError を throw する。 */
export async function developPreviewCoreImage(
  captureId: CaptureId,
  adjustments: Adjustments,
): Promise<CoreImagePreviewResult> {
  return await invoke<CoreImagePreviewResult>("develop_preview_coreimage", { captureId, adjustments });
}

/** プリセット既定値 + ユーザー変更分の 9 キー全量（Rust は全量を要求する）。 */
export function resolveAdjustments(preset: PresetInfo, overrides: Partial<Adjustments>): Adjustments {
  return { ...preset.adjustments, ...overrides };
}

function basename(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

/**
 * Rust の結果を表示側が読む `DevelopedResult` に変換する（§2.7）。
 * 表示側（BeforeAfter / FullSizeViewer / OperationPanel）は形を変えずにそのまま使える。
 */
export function toDevelopedResult(result: CoreImagePreviewResult, preset: PresetInfo): DevelopedResult {
  const lensApplied = result.applied?.lens_correction_enabled === true;
  const lens = result.source?.lens_model ?? undefined;
  const totalMs = result.elapsed_ms?.total ?? 0;
  return {
    capture_id: result.capture_id,
    preset: preset.name,
    preset_label: preset.label,
    filename: basename(result.output_path),
    recipe_filename: basename(result.provenance_path),
    params_hash: result.params_hash.slice(0, 10),
    adjustments: result.adjustments,
    width: result.width,
    height: result.height,
    elapsed_seconds: Math.round(totalMs / 10) / 100,
    cached: result.cache_hit,
    preview: true,
    // CI は歪曲 / 色収差 / 周辺減光を分けて報告できないので flags は空にする（嘘の内訳を出さない）。
    lens_correction: lensApplied
      ? { applied: true, lens, flags: [] }
      : { applied: false, lens, flags: [], reason: "coreimage_unsupported" },
    // CLI は EXIF を書かない契約。
    exif_inherited: false,
    // asset プロトコル。ハッシュがファイル名に入るのでキャッシュバスターは不要。
    output_url: convertFileSrc(result.output_path),
    engine: "coreimage",
  };
}

/**
 * Python（rawpy）プレビューへフォールバックしてよいエラーコード（§2.6）。
 * CLI・書き出し・アプリ領域などインフラ側の失敗だけを並べる（許可リスト）。
 * BUSY / INVALID_REQUEST / ADJUSTMENTS_INVALID / INVALID_CAPTURE_ID / 入力・設定エラー、
 * および未知のコードはフォールバックせずエラーとして表示する（不具合を隠さないため）。
 */
const FALLBACK_CODES: ReadonlySet<string> = new Set([
  "SIDECAR_NOT_FOUND",
  "SIDECAR_SPAWN_FAILED",
  "SIDECAR_TIMEOUT",
  "SIDECAR_IO_FAILED",
  "SIDECAR_BAD_RESPONSE",
  "UNSUPPORTED_RAW",
  "UNSUPPORTED_DECODER_VERSION",
  "RENDER_FAILED",
  "WRITE_FAILED",
  "OUTPUT_DIR_FAILED",
  "PROVENANCE_WRITE_FAILED",
  "APP_DATA_DIR_UNAVAILABLE",
  "INTERNAL_ERROR",
]);

/** invoke の reject 値が `CoreImageError` の形（code が文字列）なら取り出す。 */
export function coreImageErrorOf(error: unknown): CoreImageError | null {
  if (!error || typeof error !== "object" || !("code" in error)) return null;
  const { code, message } = error as { code: unknown; message?: unknown };
  if (typeof code !== "string") return null;
  return { code, message: typeof message === "string" ? message : "" };
}

/**
 * Core Image の失敗を Python プレビューで救ってよいか（§2.6 の表）。
 * コードを持たない失敗（invoke 自体の失敗など）はフォールバックしない（結合の不具合を表に出す）。
 */
export function shouldFallbackToRawpy(error: unknown): boolean {
  const parsed = coreImageErrorOf(error);
  return parsed !== null && FALLBACK_CODES.has(parsed.code);
}

/** invoke の reject 値（CoreImageError か任意の値）を表示用の文言にする。 */
export function coreImageErrorMessage(error: unknown): string {
  const parsed = coreImageErrorOf(error);
  if (parsed) {
    if (parsed.code === "BUSY") return `別の現像を実行中です（${parsed.code}）`;
    return parsed.message ? `${parsed.message}（${parsed.code}）` : `Core Image でのプレビュー現像に失敗しました（${parsed.code}）`;
  }
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "string" && error) return error;
  return "Core Image でのプレビュー現像に失敗しました";
}
