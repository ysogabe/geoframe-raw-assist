import { describe, expect, it, vi } from "vitest";

// node 環境には window.__TAURI_INTERNALS__ が無いので、Tauri API は差し替える。
vi.mock("@tauri-apps/api/core", () => ({
  convertFileSrc: (path: string) => `asset://localhost/${path}`,
  invoke: vi.fn(() => Promise.reject(new Error("invoke はブラウザモードで呼ばれてはならない"))),
  isTauri: () => false,
}));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn() }));

import { invoke } from "@tauri-apps/api/core";
import {
  coreImageErrorMessage,
  previewEngine,
  resolveAdjustments,
  shouldFallbackToRawpy,
  toDevelopedResult,
  type CoreImagePreviewResult,
} from "./coreimage-api";
import type { Adjustments, PresetInfo } from "./types";

const ADJ: Adjustments = {
  exposure: 0,
  wb_temp: 0,
  shadow_lift: 15,
  highlight_recovery: 60,
  brightness: 1,
  contrast: 1.03,
  saturation: 1.02,
  sharpness: 1.04,
  noise_reduction: "light",
};

const PRESET: PresetInfo = { name: "neutral_recovery", label: "ニュートラル", description: "", adjustments: ADJ };

function ciResult(overrides: Partial<CoreImagePreviewResult> = {}): CoreImagePreviewResult {
  return {
    capture_id: "DSC04430",
    output_path: "/Users/u/Library/Application Support/com.geoframe.rawassist/coreimage/preview/DSC04430_0123456789abcdef.jpg",
    provenance_path:
      "/Users/u/Library/Application Support/com.geoframe.rawassist/coreimage/preview/DSC04430_0123456789abcdef.json",
    width: 2400,
    height: 1600,
    elapsed_ms: { decode: 900, render_encode: 400, total: 1316 },
    cache_hit: false,
    params_hash: "0123456789abcdef",
    decoder_version: "9",
    cli_version: "0.3.0",
    macos_version: "27.0.1",
    adjustments: { ...ADJ, exposure: 0.5 },
    applied: { lens_correction_enabled: true },
    source: { lens_model: "E 16-50mm F3.5-5.6 OSS", iso: 3200, camera_model: "ILCE-6400" },
    ...overrides,
  };
}

describe("resolveAdjustments", () => {
  it("上書きが無ければプリセット既定値の全量", () => {
    expect(resolveAdjustments(PRESET, {})).toEqual(ADJ);
  });

  it("上書きしたキーだけ差し替え、9 キー全量を返す", () => {
    const resolved = resolveAdjustments(PRESET, { exposure: 1.2, noise_reduction: "full" });
    expect(resolved).toEqual({ ...ADJ, exposure: 1.2, noise_reduction: "full" });
    expect(Object.keys(resolved)).toHaveLength(9);
  });

  it("プリセットの値を書き換えない", () => {
    resolveAdjustments(PRESET, { exposure: 1.2 });
    expect(PRESET.adjustments.exposure).toBe(0);
  });
});

describe("toDevelopedResult", () => {
  it("§2.7 の対応どおりに変換する", () => {
    const result = toDevelopedResult(ciResult(), PRESET);
    expect(result).toEqual({
      capture_id: "DSC04430",
      preset: "neutral_recovery",
      preset_label: "ニュートラル",
      filename: "DSC04430_0123456789abcdef.jpg",
      recipe_filename: "DSC04430_0123456789abcdef.json",
      params_hash: "0123456789",
      adjustments: { ...ADJ, exposure: 0.5 },
      width: 2400,
      height: 1600,
      elapsed_seconds: 1.32,
      cached: false,
      preview: true,
      lens_correction: { applied: true, lens: "E 16-50mm F3.5-5.6 OSS", flags: [] },
      exif_inherited: false,
      output_url: `asset://localhost/${ciResult().output_path}`,
      engine: "coreimage",
    });
  });

  it("レンズ補正が効いていなければ reason を coreimage_unsupported にする", () => {
    const result = toDevelopedResult(ciResult({ applied: { lens_correction_enabled: false } }), PRESET);
    expect(result.lens_correction).toEqual({
      applied: false,
      lens: "E 16-50mm F3.5-5.6 OSS",
      flags: [],
      reason: "coreimage_unsupported",
    });
  });

  it("lens_model が null なら lens は undefined", () => {
    const result = toDevelopedResult(ciResult({ source: { lens_model: null } }), PRESET);
    expect(result.lens_correction.lens).toBeUndefined();
  });

  it("キャッシュヒットは cached、計測値が無ければ 0 秒", () => {
    const result = toDevelopedResult(ciResult({ cache_hit: true, elapsed_ms: null }), PRESET);
    expect(result.cached).toBe(true);
    expect(result.elapsed_seconds).toBe(0);
  });
});

describe("shouldFallbackToRawpy（§2.6）", () => {
  const fallback = [
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
  ];
  const display = [
    "BUSY",
    "INVALID_REQUEST",
    "ADJUSTMENTS_INVALID",
    "INVALID_CAPTURE_ID",
    "SOURCE_ROOT_NOT_CONFIGURED",
    "CONFIG_INVALID",
    "CONFIG_UNREADABLE",
    "SOURCE_ROOT_NOT_FOUND",
    "ARW_NOT_FOUND",
    "ARW_OUTSIDE_SOURCE_ROOT",
    "SOMETHING_NEW",
  ];

  it.each(fallback)("インフラ系 %s は rawpy へフォールバックする", (code) => {
    expect(shouldFallbackToRawpy({ code, message: "m" })).toBe(true);
  });

  it.each(display)("%s はフォールバックせず表示する", (code) => {
    expect(shouldFallbackToRawpy({ code, message: "m" })).toBe(false);
  });

  it("コードを持たない失敗（invoke 自体の失敗など）はフォールバックしない", () => {
    expect(shouldFallbackToRawpy(new Error("command develop_preview_coreimage not found"))).toBe(false);
    expect(shouldFallbackToRawpy("command not found")).toBe(false);
    expect(shouldFallbackToRawpy(null)).toBe(false);
    expect(shouldFallbackToRawpy({ code: 42 })).toBe(false);
  });
});

describe("coreImageErrorMessage", () => {
  it("BUSY は「別の現像を実行中です」", () => {
    expect(coreImageErrorMessage({ code: "BUSY", message: "busy" })).toBe("別の現像を実行中です（BUSY）");
  });

  it("コード付きの失敗はメッセージにコードを添える", () => {
    expect(coreImageErrorMessage({ code: "INVALID_REQUEST", message: "bad" })).toBe("bad（INVALID_REQUEST）");
  });

  it("Error と文字列はそのまま", () => {
    expect(coreImageErrorMessage(new Error("e"))).toBe("e");
    expect(coreImageErrorMessage("s")).toBe("s");
  });
});

describe("previewEngine", () => {
  it("ブラウザモードは invoke せずに rawpy", async () => {
    await expect(previewEngine()).resolves.toBe("rawpy");
    expect(invoke).not.toHaveBeenCalled();
  });
});
