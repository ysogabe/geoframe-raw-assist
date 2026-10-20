import { convertFileSrc, invoke } from "@tauri-apps/api/core";
import { useEffect, useState } from "react";
import { isTauri } from "./setup";
import type { CaptureId } from "./types";

/**
 * Core Image 試験現像（Issue #65）。Python エンジンを経由せず、Tauri(Rust) が Swift CLI
 * `geoframe-ci-develop` を直接呼ぶ。Rust 側は src-tauri/src/coreimage.rs。
 *
 * 環境変数 `GEOFRAME_COREIMAGE_EXPERIMENT=1` で起動したデスクトップアプリでだけ有効。
 * ブラウザモード（isTauri() === false）では常に無効で、invoke を一切呼ばない。
 */

export type CoreImageMode = "preview" | "full";

/** Rust 側 `CoreImageResult` とフィールド名を一致させること。 */
export type CoreImageResult = {
  capture_id: CaptureId;
  mode: CoreImageMode;
  /** 出力 JPEG の絶対パス（`<app_data_dir>/coreimage/` 配下）。表示には coreImageUrl() を通す。 */
  output_path: string;
  width: number;
  height: number;
  /** CLI の計測値（ミリ秒）。キャッシュヒット時は最初に現像したときの値。 */
  elapsed_ms: { decode?: number; render_encode?: number; total?: number } | null;
  cache_hit: boolean;
  decoder_version: string;
  cli_version: string;
};

/** Rust 側 `CoreImageError`。CLI の error.code / message もそのまま入る。 */
export type CoreImageError = { code: string; message: string };

let enabledPromise: Promise<boolean> | null = null;

/** フラグは起動中に変わらないので 1 回だけ問い合わせる。失敗したら無効扱い。 */
export function coreImageExperimentEnabled(): Promise<boolean> {
  if (!isTauri()) return Promise.resolve(false);
  enabledPromise ??= invoke<boolean>("coreimage_experiment_enabled").catch(() => false);
  return enabledPromise;
}

/** フラグを React から読む。問い合わせ中・無効・ブラウザモードはすべて false。 */
export function useCoreImageExperiment(): boolean {
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    let active = true;
    void coreImageExperimentEnabled().then((value) => {
      if (active) setEnabled(value);
    });
    return () => {
      active = false;
    };
  }, []);
  return enabled;
}

/** 選択中カットを Core Image で現像する。失敗時は CoreImageError を throw する。 */
export async function developCoreImage(captureId: CaptureId, mode: CoreImageMode): Promise<CoreImageResult> {
  return await invoke<CoreImageResult>("develop_coreimage", { captureId, mode });
}

/** 出力 JPEG の表示 URL（asset プロトコル。scope は tauri.conf.json の `$APPDATA/coreimage/**`）。 */
export function coreImageUrl(result: CoreImageResult): string {
  return convertFileSrc(result.output_path);
}

/** invoke の reject 値（CoreImageError か任意の値）を表示用の文言にする。 */
export function coreImageErrorMessage(error: unknown): string {
  if (error && typeof error === "object" && "code" in error && "message" in error) {
    const { code, message } = error as CoreImageError;
    return `${message}（${code}）`;
  }
  return error instanceof Error ? error.message : String(error);
}
