import { convertFileSrc, invoke } from "@tauri-apps/api/core";
import { previewTiming } from "./preview-timing";
import type { Adjustments, CaptureId, DevelopedResult, HighlightRecoveryEffect, MergedResult, PresetInfo } from "./types";

/** Core Image 現像。Tauri(Rust) → Swift CLI。EXIF は Rust が APP1 をコピーする。 */

/** CLI の計測値（ミリ秒）。キャッシュヒット時は最初に現像したときの値。 */
export type CoreImageElapsedMs = { decode?: number; render_encode?: number; total?: number } | null;

/** Rust 側 `CoreImagePreviewResult`（§4.3）とフィールド名を一致させること。 */
export type CoreImagePreviewResult = {
  capture_id: CaptureId;
  /** 出力 JPEG の絶対パス（`<app_data_dir>/coreimage/{preview,full}/` 配下）。 */
  output_path: string;
  /** 来歴 JSON（CLI 応答全文）の絶対パス。 */
  provenance_path: string;
  width: number;
  height: number;
  elapsed_ms: CoreImageElapsedMs;
  cache_hit: boolean;
  preview: boolean;
  exif_inherited: boolean;
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

/** 計測コードの例外を握りつぶす（計測は製品の結果に影響させない）。 */
function bestEffort<T>(fn: () => T): T | undefined {
  try {
    return fn();
  } catch {
    return undefined;
  }
}

/** 選択中カットを Core Image でプレビュー現像する。失敗時は CoreImageError を throw する。 */
export async function developPreviewCoreImage(
  captureId: CaptureId,
  adjustments: Adjustments,
  /** 任意計測（既定 OFF）の起点。ボタンのハンドラ入口の `previewTiming.now()`。省略時は API 呼び出しが起点。 */
  timing?: { handlerEntryMs?: number },
): Promise<CoreImagePreviewResult> {
  // 任意計測（既定 OFF。preview-timing.ts）。OFF のとき timer は null で、以下は invoke だけになる。
  // 計測は best effort: 計測側の例外で、成功した invoke の結果や本来の invoke エラーを変えない。
  const timer = bestEffort(() => previewTiming.begin({ captureId, handlerEntryMs: timing?.handlerEntryMs }));
  let result: CoreImagePreviewResult;
  try {
    result = await invoke<CoreImagePreviewResult>("develop_preview_coreimage", { captureId, adjustments });
  } catch (error) {
    bestEffort(() => timer?.invokeFailed(coreImageErrorOf(error)?.code ?? "UNKNOWN"));
    throw error;
  }
  bestEffort(() =>
    timer?.invokeReturned({
      displayUrl: convertFileSrc(result.output_path),
      cacheHit: result.cache_hit,
      elapsed: result.elapsed_ms,
      width: result.width,
      height: result.height,
    }),
  );
  return result;
}

/** 選択中カットを原寸現像する。max_edge は付けず CLI の full を使う。 */
export async function developFullCoreImage(
  captureId: CaptureId,
  adjustments: Adjustments,
  preset: PresetInfo,
): Promise<DevelopedResult> {
  return await invoke<DevelopedResult>("develop_full_coreimage", { captureId, adjustments, preset: preset.name });
}

/** ブラケットを Core Image で合成する。Rust が既存 MergedResult 形で返す。 */
export async function mergeBracketCoreImage(
  captureIds: CaptureId[],
  adjustments: Adjustments,
  preset: PresetInfo,
  mode: "preview" | "full",
): Promise<MergedResult> {
  return await invoke<MergedResult>("merge_bracket", {
    captureIds,
    preset: preset.name,
    adjustments,
    preview: mode === "preview",
    force: false,
  });
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
    preview: result.preview,
    // CI は歪曲 / 色収差 / 周辺減光を分けて報告できないので flags は空にする（嘘の内訳を出さない）。
    lens_correction: lensApplied
      ? { applied: true, lens, flags: [] }
      : { applied: false, lens, flags: [], reason: "coreimage_unsupported" },
    exif_inherited: result.exif_inherited,
    // asset プロトコル。ハッシュがファイル名に入るのでキャッシュバスターは不要。
    output_url: convertFileSrc(result.output_path),
    engine: "coreimage",
    ...(highlightRecovery ? { highlight_recovery_effect: highlightRecovery } : {}),
  };
}

/** invoke の reject 値が `CoreImageError` の形（code が文字列）なら取り出す。 */
export function coreImageErrorOf(error: unknown): CoreImageError | null {
  if (!error || typeof error !== "object" || !("code" in error)) return null;
  const { code, message } = error as { code: unknown; message?: unknown };
  if (typeof code !== "string") return null;
  return { code, message: typeof message === "string" ? message : "" };
}

/** invoke の reject 値（CoreImageError か任意の値）を表示用の文言にする。 */
export function coreImageErrorMessage(error: unknown): string {
  const parsed = coreImageErrorOf(error);
  if (parsed) {
    if (parsed.code === "BUSY") return `別の現像を実行中です（${parsed.code}）`;
    return parsed.message ? `${parsed.message}（${parsed.code}）` : `Core Image での現像に失敗しました（${parsed.code}）`;
  }
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "string" && error) return error;
  return "Core Image での現像に失敗しました";
}
