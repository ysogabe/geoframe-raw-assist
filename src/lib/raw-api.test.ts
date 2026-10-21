import { beforeEach, describe, expect, it, vi } from "vitest";

// node 環境には window.__TAURI_INTERNALS__ が無いので、Tauri API は差し替える。
const tauri = vi.hoisted(() => ({ isTauri: false }));
vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(),
  isTauri: () => tauri.isTauri,
}));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn() }));

import { invoke } from "@tauri-apps/api/core";
import fixture from "../../src-tauri/tests/fixtures/presets.json";
import { getPresets } from "./raw-api";
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
