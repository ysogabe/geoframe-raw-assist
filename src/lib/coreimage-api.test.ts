import { afterEach, describe, expect, it, vi } from "vitest";

// node 環境には window.__TAURI_INTERNALS__ が無いので、Tauri API は差し替える。
vi.mock("@tauri-apps/api/core", () => ({
  convertFileSrc: vi.fn((path: string) => `asset://localhost/${path}`),
  invoke: vi.fn(() => Promise.reject(new Error("invoke はブラウザモードで呼ばれてはならない"))),
  isTauri: () => false,
}));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn() }));

import { convertFileSrc, invoke } from "@tauri-apps/api/core";
import { previewTiming } from "./preview-timing";
import {
  coreImageErrorMessage,
  engineInfoLabel,
  getEngineInfo,
  mergeBracketCoreImage,
  developFullCoreImage,
  developPreviewCoreImage,
  resolveAdjustments,
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
    preview: true,
    exif_inherited: false,
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

  it("CLI 0.6.0 の highlight_recovery の実効値を highlight_recovery_effect に写す（#81）", () => {
    const effect = {
      value: 90,
      exposure_delta: -1.0,
      shadow_lift_delta: 55,
      effective_exposure: -1.0,
      effective_shadow_lift: 55,
      shadow_lift_clamped: false,
    };
    const result = toDevelopedResult(ciResult({ adjustments_applied: { highlight_recovery: effect, ignored: [] } }), PRESET);
    expect(result.highlight_recovery_effect).toEqual(effect);
  });

  it("highlight_recovery が無い・壊れていれば highlight_recovery_effect は付けない", () => {
    expect(toDevelopedResult(ciResult(), PRESET)).not.toHaveProperty("highlight_recovery_effect");
    expect(toDevelopedResult(ciResult({ adjustments_applied: null }), PRESET)).not.toHaveProperty("highlight_recovery_effect");
    const broken = ciResult({ adjustments_applied: { highlight_recovery: { value: "90" } } });
    expect(toDevelopedResult(broken, PRESET)).not.toHaveProperty("highlight_recovery_effect");
  });

  it("キャッシュヒットは cached、計測値が無ければ 0 秒", () => {
    const result = toDevelopedResult(ciResult({ cache_hit: true, elapsed_ms: null }), PRESET);
    expect(result.cached).toBe(true);
    expect(result.elapsed_seconds).toBe(0);
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

describe("Core Image invoke", () => {
  it("merge_bracket に固定契約の引数を渡し、MergedResultを変換せず返す", async () => {
    const merged = { capture_ids: ["A1", "A2"], frames: 2, preview: true };
    vi.mocked(invoke).mockResolvedValueOnce(merged);
    await expect(mergeBracketCoreImage(["A1", "A2"], ADJ, PRESET, "preview")).resolves.toBe(merged);
    expect(invoke).toHaveBeenLastCalledWith("merge_bracket", {
      captureIds: ["A1", "A2"], preset: PRESET.name, adjustments: ADJ, preview: true, force: false,
    });
    vi.mocked(invoke).mockResolvedValueOnce(merged);
    await mergeBracketCoreImage(["A1", "A2"], ADJ, PRESET, "full");
    expect(invoke).toHaveBeenLastCalledWith("merge_bracket", {
      captureIds: ["A1", "A2"], preset: PRESET.name, adjustments: ADJ, preview: false, force: false,
    });
  });

  it("merge_bracket の失敗をそのまま返す", async () => {
    const error = { code: "BUSY", message: "busy" };
    vi.mocked(invoke).mockRejectedValueOnce(error);
    await expect(mergeBracketCoreImage(["A1", "A2"], ADJ, PRESET, "full")).rejects.toBe(error);
  });

  it("full / preview を別コマンドに送る。full は実寸と継承結果を表示に渡す", async () => {
    const full = toDevelopedResult(ciResult({ preview: false, exif_inherited: true, width: 6000, height: 4000 }), PRESET);
    vi.mocked(invoke).mockResolvedValueOnce(full);
    const result = await developFullCoreImage("DSC04430", ADJ, PRESET);
    expect(invoke).toHaveBeenLastCalledWith("develop_full_coreimage", { captureId: "DSC04430", adjustments: ADJ, preset: PRESET.name });
    expect(result).toMatchObject({ preview: false, exif_inherited: true, width: 6000, height: 4000 });
    vi.mocked(invoke).mockResolvedValueOnce(ciResult());
    await developPreviewCoreImage("DSC04430", ADJ);
    expect(invoke).toHaveBeenLastCalledWith("develop_preview_coreimage", { captureId: "DSC04430", adjustments: ADJ });
  });
  it("失敗はそのまま表示側に返す", async () => {
    const error = { code: "SIDECAR_NOT_FOUND", message: "not found" };
    vi.mocked(invoke).mockRejectedValueOnce(error);
    await expect(developFullCoreImage("DSC04430", ADJ, PRESET)).rejects.toBe(error);
  });
});

describe("getEngineInfo", () => {
  it("engine_info を引数なしで invoke して結果を返す", async () => {
    const info = { cli_version: "0.6.0", macos_version: "15.1", sidecar_resolved: true };
    vi.mocked(invoke).mockResolvedValueOnce(info);
    await expect(getEngineInfo()).resolves.toEqual(info);
    expect(invoke).toHaveBeenCalledWith("engine_info");
  });

  it("CoreImageError はそのまま reject される", async () => {
    const error = { code: "SIDECAR_NOT_FOUND", message: "見つかりません" };
    vi.mocked(invoke).mockRejectedValueOnce(error);
    await expect(getEngineInfo()).rejects.toBe(error);
  });

  it("表示ラベルは Core Image <cli> / macOS <ver>", () => {
    expect(engineInfoLabel({ cli_version: "0.6.0", macos_version: "15.1" })).toBe("Core Image 0.6.0 / macOS 15.1");
  });
});

describe("developPreviewCoreImage の任意計測は製品の結果を変えない（best effort）", () => {
  const boom = () => {
    throw new Error("timing exploded");
  };
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("begin が throw しても、成功した invoke の結果をそのまま返す", async () => {
    vi.spyOn(previewTiming, "begin").mockImplementation(boom);
    const result = ciResult();
    vi.mocked(invoke).mockResolvedValueOnce(result);
    await expect(developPreviewCoreImage("DSC04430", ADJ)).resolves.toBe(result);
  });

  it("timer.invokeReturned が throw しても、成功した invoke の結果を返す", async () => {
    vi.spyOn(previewTiming, "begin").mockReturnValue({ invokeReturned: boom, invokeFailed: boom });
    const result = ciResult();
    vi.mocked(invoke).mockResolvedValueOnce(result);
    await expect(developPreviewCoreImage("DSC04430", ADJ)).resolves.toBe(result);
  });

  it("convertFileSrc が throw しても、成功した invoke の結果を返す", async () => {
    const invokeReturned = vi.fn();
    vi.spyOn(previewTiming, "begin").mockReturnValue({ invokeReturned, invokeFailed: vi.fn() });
    vi.mocked(convertFileSrc).mockImplementationOnce(boom);
    const result = ciResult();
    vi.mocked(invoke).mockResolvedValueOnce(result);
    await expect(developPreviewCoreImage("DSC04430", ADJ)).resolves.toBe(result);
    expect(invokeReturned).not.toHaveBeenCalled();
  });

  it("invoke が失敗し、計測の記録（invokeFailed）も throw しても、元のエラーを返す", async () => {
    vi.spyOn(previewTiming, "begin").mockReturnValue({ invokeReturned: boom, invokeFailed: boom });
    const error = { code: "BUSY", message: "busy" };
    vi.mocked(invoke).mockRejectedValueOnce(error);
    await expect(developPreviewCoreImage("DSC04430", ADJ)).rejects.toBe(error);
  });

  it("invoke が失敗し、begin も throw しても、元のエラーを返す", async () => {
    vi.spyOn(previewTiming, "begin").mockImplementation(boom);
    const error = { code: "SIDECAR_TIMEOUT", message: "timeout" };
    vi.mocked(invoke).mockRejectedValueOnce(error);
    await expect(developPreviewCoreImage("DSC04430", ADJ)).rejects.toBe(error);
  });

  it("計測が有効でも、失敗は invokeFailed にコードだけ渡して元のエラーを返す", async () => {
    const invokeFailed = vi.fn();
    vi.spyOn(previewTiming, "begin").mockReturnValue({ invokeReturned: vi.fn(), invokeFailed });
    const error = { code: "BUSY", message: "busy" };
    vi.mocked(invoke).mockRejectedValueOnce(error);
    await expect(developPreviewCoreImage("DSC04430", ADJ)).rejects.toBe(error);
    expect(invokeFailed).toHaveBeenCalledWith("BUSY");
  });
});
