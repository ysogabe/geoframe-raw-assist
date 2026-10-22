import { beforeEach, describe, expect, it, vi } from "vitest";

// node 環境には window.__TAURI_INTERNALS__ が無いので、Tauri API は差し替える。
const tauri = vi.hoisted(() => ({ isTauri: false }));
vi.mock("@tauri-apps/api/core", () => ({
  // 実物（tauri-2 scripts/core.js, macOS）と同じ: パス全体を encodeURIComponent する。
  convertFileSrc: (path: string, protocol = "asset") => `${protocol}://localhost/${encodeURIComponent(path)}`,
  invoke: vi.fn(),
  isTauri: () => tauri.isTauri,
}));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn() }));

import { invoke } from "@tauri-apps/api/core";
import fixture from "../../src-tauri/tests/fixtures/presets.json";
import {
  assetUrl,
  getAnalysis,
  isSourceRootNotConfigured,
  normalizeAnalysisResponse,
  sourceJpgUrl,
  startAnalyze,
  getPresets,
  getSourceFolder, isSourceFolderApiError,
  setSourceFolder,
  sourceFolderApiErrorFromInvoke,
} from "./raw-api";
import type { PresetInfo } from "./types";

const RUST_PRESETS = fixture.presets as PresetInfo[];
const HEALTH_PRESETS: PresetInfo[] = [{ ...RUST_PRESETS[0], label: "health 由来" }];

describe("getPresets（#80）", () => {
  beforeEach(() => {
    vi.mocked(invoke).mockReset();
  });

  it("ブラウザモードでは invoke を呼ばず health の presets を返す", async () => {
    tauri.isTauri = false;
    expect(await getPresets(HEALTH_PRESETS)).toBe(HEALTH_PRESETS);
    expect(invoke).not.toHaveBeenCalled();
  });

  it("Tauri では Rust の get_presets を優先する", async () => {
    tauri.isTauri = true;
    vi.mocked(invoke).mockResolvedValue(RUST_PRESETS);
    expect(await getPresets(HEALTH_PRESETS)).toEqual(RUST_PRESETS);
    expect(invoke).toHaveBeenCalledWith("get_presets");
  });

  it("Tauri 側の取得に失敗したら health の presets へ戻る", async () => {
    tauri.isTauri = true;
    vi.mocked(invoke).mockRejectedValue(new Error("command get_presets not found"));
    expect(await getPresets(HEALTH_PRESETS)).toBe(HEALTH_PRESETS);
  });

  it("Tauri 側が空・不正な形なら health の presets へ戻る", async () => {
    tauri.isTauri = true;
    vi.mocked(invoke).mockResolvedValue([]);
    expect(await getPresets(HEALTH_PRESETS)).toBe(HEALTH_PRESETS);
    vi.mocked(invoke).mockResolvedValue(null);
    expect(await getPresets(HEALTH_PRESETS)).toBe(HEALTH_PRESETS);
  });
});

describe("source folder Tauri commands", () => {
  beforeEach(() => vi.mocked(invoke).mockReset());
  const info = { path: "/photos", display_path: "~/photos", is_default: false, exists: true, readable: true, pairs: 0, jpg_only: 0, arw_only: 0, total_bytes: 0 };

  it("Tauri では get_source_folder invoke を使う", async () => {
    tauri.isTauri = true;
    vi.mocked(invoke).mockResolvedValue(info);
    expect(await getSourceFolder()).toEqual(info);
    expect(invoke).toHaveBeenCalledWith("get_source_folder");
  });

  it("Tauri では set_source_folder を呼び、拒否値を SourceFolderApiError にする", async () => {
    tauri.isTauri = true;
    const converted = sourceFolderApiErrorFromInvoke({ code: "not_found", reason: "not_found", message: "見つかりません" });
    expect(isSourceFolderApiError(converted)).toBe(true);
    expect(converted.name).toBe("SourceFolderApiError");
    expect(converted.message).toBe("見つかりません");
    expect(converted.reason).toBe("not_found");
    vi.mocked(invoke).mockResolvedValue(info);
    expect(await setSourceFolder("/photos")).toEqual(info);
    expect(invoke).toHaveBeenCalledWith("set_source_folder", { path: "/photos" });
  });
});

describe("解析・配信 URL（#110）", () => {
  beforeEach(() => {
    vi.mocked(invoke).mockReset();
  });

  it("元JPG: Tauri では geoframe://localhost/source-jpg/DSC12345.JPG（Rust の source_jpg_name_pattern と一致）", () => {
    tauri.isTauri = true;
    const url = sourceJpgUrl("DSC12345");
    expect(url).toBe("geoframe://localhost/source-jpg/DSC12345.JPG");
    // Rust は request.uri().path() をデコードせずに正規表現へ渡す。
    expect(new URL(url).pathname).toMatch(/^\/source-jpg\/(DSC[0-9]{5})\.JPG$/);
  });

  it("元JPG: convertFileSrc 素通しだと / が %2F になり Rust のパターンに合わない（自前で組み立てる理由）", () => {
    const naive = `geoframe://localhost/${encodeURIComponent("source-jpg/DSC12345.JPG")}`;
    expect(new URL(naive).pathname).not.toMatch(/^\/source-jpg\/(DSC[0-9]{5})\.JPG$/);
  });

  it("元JPG: ブラウザでは従来の HTTP", () => {
    tauri.isTauri = false;
    expect(sourceJpgUrl("DSC12345")).toBe("http://127.0.0.1:8788/source-jpg/DSC12345.JPG");
  });

  it("サムネ: Tauri の絶対パスは asset protocol、Python 相対パスと http は従来どおり", () => {
    tauri.isTauri = true;
    expect(assetUrl("/Users/a b/Library/thumbnails/DSC00001.webp")).toBe(
      "asset://localhost/%2FUsers%2Fa%20b%2FLibrary%2Fthumbnails%2FDSC00001.webp",
    );
    expect(assetUrl("/thumbs/DSC00001.webp")).toBe("http://127.0.0.1:8788/thumbs/DSC00001.webp");
    expect(assetUrl("http://x/y.jpg")).toBe("http://x/y.jpg");
    tauri.isTauri = false;
    expect(assetUrl("/thumbs/DSC00001.webp")).toBe("http://127.0.0.1:8788/thumbs/DSC00001.webp");
  });

  it("getAnalysis: Tauri では get_analysis を invoke し、ready / running / absent を写す", async () => {
    tauri.isTauri = true;
    const analysis = { analysis_version: 3, scored: false, groups: [], photos: {} };
    vi.mocked(invoke).mockResolvedValueOnce({ status: "ready", analysis });
    expect(await getAnalysis()).toEqual({ status: "ready", analysis });
    expect(invoke).toHaveBeenCalledWith("get_analysis");

    vi.mocked(invoke).mockResolvedValueOnce({ status: "running", progress: { stage: "thumbnails", done: 3, total: 10 } });
    expect(await getAnalysis()).toEqual({ status: "running", progress: { stage: "thumbnails", done: 3, total: 10 } });

    vi.mocked(invoke).mockResolvedValueOnce({ status: "absent", error: "失敗" });
    expect(await getAnalysis()).toEqual({ status: "absent", error: "失敗" });
  });

  it("getAnalysis: SOURCE_ROOT_NOT_CONFIGURED の reject はそのまま投げ、判別できる", async () => {
    tauri.isTauri = true;
    const rejection = { code: "SOURCE_ROOT_NOT_CONFIGURED", message: "未設定" };
    vi.mocked(invoke).mockRejectedValueOnce(rejection);
    const error = await getAnalysis().catch((e) => e);
    expect(isSourceRootNotConfigured(error)).toBe(true);
    expect(isSourceRootNotConfigured(new Error("x"))).toBe(false);
    expect(isSourceRootNotConfigured(null)).toBe(false);
  });

  it("normalizeAnalysisResponse: 不正な形は absent に倒す", () => {
    expect(normalizeAnalysisResponse(null)).toEqual({ status: "absent" });
    expect(normalizeAnalysisResponse({ status: "ready" })).toEqual({ status: "absent" });
    expect(normalizeAnalysisResponse({ status: "running" })).toEqual({
      status: "running",
      progress: { stage: "", done: 0, total: 0 },
    });
  });

  it("startAnalyze: Tauri では start_analysis に force を渡し、文字列を { status } に包む", async () => {
    tauri.isTauri = true;
    vi.mocked(invoke).mockResolvedValue("started");
    expect(await startAnalyze(true)).toEqual({ status: "started" });
    expect(invoke).toHaveBeenCalledWith("start_analysis", { force: true });
    await startAnalyze();
    expect(invoke).toHaveBeenLastCalledWith("start_analysis", { force: false });
  });
});
