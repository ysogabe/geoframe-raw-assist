import { convertFileSrc, invoke, isTauri } from "@tauri-apps/api/core";
import type { Adjustments, CaptureId, DevelopedResult, DevelopEngine, HighlightRecoveryEffect, PresetInfo } from "./types";

/**
 * Core Image プレビュー現像（Issue #72 / #73）。Tauri(Rust) が
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
  /** CLI の adjustments_applied そのまま（0.6.0 から highlight_recovery の実効値を含む）。古い CLI では null。 */
  adjustments_applied?: { highlight_recovery?: unknown; [key: string]: unknown } | null;
};

/** Rust 側 `CoreImageError`。CLI の error.code / message もそのまま入る。 */
export type CoreImageError = { code: string; message: string };

/** Rust 側 `EngineInfo`（engine_info コマンド, Issue #109）とフィールド名を一致させること。 */
export type EngineInfo = { cli_version: string; macos_version: string; sidecar_resolved: boolean };

/**
 * Core Image エンジンの稼働確認。「エンジン準備完了」（`engineStatus`）はこれだけで決める。
 * sidecar が見つからない / `--version` が失敗したときは CoreImageError を throw する。
 */
export async function getEngineInfo(): Promise<EngineInfo> {
  return await invoke<EngineInfo>("engine_info");
}

/** トップバーのエンジン表示（「Core Image <cli version> / macOS <version>」）。 */
export function engineInfoLabel(info: Pick<EngineInfo, "cli_version" | "macos_version">): string {
  return `Core Image ${info.cli_version} / macOS ${info.macos_version}`;
}

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

/**
 * プリセット既定値 + ユーザー変更分の 9 キー全量（Rust は全量を要求する）。
 * 実装は app-state.ts の 1 か所（`effectiveAdjustments` と共用）。ここからも import できるよう再エクスポートする。
 */
export { resolveAdjustments } from "./app-state";

/** `adjustments_applied.highlight_recovery` を読む。形が崩れていれば undefined（表示を出さないだけ）。 */
export function readHighlightRecoveryEffect(value: unknown): HighlightRecoveryEffect | undefined {
  if (typeof value !== "object" || value === null) return undefined;
  const v = value as Record<string, unknown>;
  const keys = ["value", "exposure_delta", "shadow_lift_delta", "effective_exposure", "effective_shadow_lift"] as const;
  if (!keys.every((key) => typeof v[key] === "number" && Number.isFinite(v[key]))) return undefined;
  return {
    value: v.value as number,
    exposure_delta: v.exposure_delta as number,
    shadow_lift_delta: v.shadow_lift_delta as number,
    effective_exposure: v.effective_exposure as number,
    effective_shadow_lift: v.effective_shadow_lift as number,
    shadow_lift_clamped: v.shadow_lift_clamped === true,
  };
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
  const highlightRecovery = readHighlightRecoveryEffect(result.adjustments_applied?.highlight_recovery);
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
    ...(highlightRecovery ? { highlight_recovery_effect: highlightRecovery } : {}),
  };
}

/**
 * Python（rawpy）プレビューへフォールバックしてよいエラーコード（§2.6）。
 * CLI・書き出し・アプリ領域などインフラ側の失敗だけを並べる（許可リスト）。
 * BUSY / INVALID_REQUEST / ADJUSTMENTS_INVALID / INVALID_CAPTURE_ID / ARW_NOT_FOUND /
 * ARW_OUTSIDE_SOURCE_ROOT、および未知のコードはフォールバックせずエラーとして表示する
 * （不具合を隠さないため）。
 *
 * 現像元フォルダの解決（Rust `read_source_root` / `resolve_arw`）の失敗はフォールバックする。
 * Python の `local_raw/config.py: source_root()` は設定が無い・壊れている・保存したフォルダが
 * 無いときに既定フォルダへ逃がすので、一覧（= Python が見ているフォルダ）のカットは rawpy なら
 * 現像できる場合がある。CLI の入力・出力先チェック（INPUT_NOT_FOUND など）も、Rust が直前に
 * 確かめた後の競合や CLI プロセス側の権限差で起きるもので、rawpy で救える場合がある。
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
  // 現像元フォルダの解決（Rust coreimage.rs: read_source_root / resolve_arw）
  "SOURCE_ROOT_NOT_CONFIGURED",
  "SOURCE_ROOT_NOT_FOUND",
  "CONFIG_UNREADABLE",
  "CONFIG_INVALID",
  // CLI の入力・出力先チェック（Swift Request.swift。Rust の cli_error がコードをそのまま渡す）
  "INPUT_NOT_FOUND",
  "INPUT_NOT_READABLE",
  "OUTPUT_DIR_NOT_FOUND",
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
