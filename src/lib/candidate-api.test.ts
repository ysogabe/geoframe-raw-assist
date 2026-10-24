import { beforeEach, expect, it, vi } from "vitest";
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(), convertFileSrc: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn() }));
import { invoke } from "@tauri-apps/api/core";
import { listen, type Event } from "@tauri-apps/api/event";
import { generateDevelopmentCandidates } from "./raw-api";
import type { DevelopmentCandidatesProgress } from "./types";

beforeEach(() => { vi.mocked(invoke).mockReset(); vi.mocked(listen).mockReset(); });
it("keeps native diagnostics off by default", async () => {
  vi.mocked(invoke).mockResolvedValue({ native_roi_diagnostics: { status: "not_run" } });
  await generateDevelopmentCandidates({ capture_id: "DSC07402", preset: "nightscape" });
  expect(invoke).toHaveBeenCalledWith("generate_development_candidates", expect.objectContaining({ nativeRoiDiagnostics: false }));
  expect(listen).not.toHaveBeenCalled();
});
it("waits for full diagnostics and releases progress listener after success", async () => {
  const unlisten = vi.fn();
  let handler: ((event: Event<DevelopmentCandidatesProgress>) => void) | undefined;
  vi.mocked(listen).mockImplementation(async (_name, cb) => { handler = cb as typeof handler; return unlisten; });
  const progress = vi.fn();
  vi.mocked(invoke).mockImplementation(async () => {
    handler?.({ payload: { capture_id: "DSC07423", preset: "nightscape", stage: "native_roi", completed: 1, total: 3 } } as Event<DevelopmentCandidatesProgress>);
    handler?.({ payload: { capture_id: "DSC07402", preset: "nightscape", stage: "native_roi", completed: 2, total: 3 } } as Event<DevelopmentCandidatesProgress>);
    expect(unlisten).not.toHaveBeenCalled();
    return { native_roi_diagnostics: { status: "missing" } };
  });
  const result = await generateDevelopmentCandidates({ capture_id: "DSC07402", preset: "nightscape", native_roi_diagnostics: true }, progress);
  expect(invoke).toHaveBeenCalledWith("generate_development_candidates", expect.objectContaining({ nativeRoiDiagnostics: true }));
  expect(progress).toHaveBeenCalledTimes(1);
  expect(result.native_roi_diagnostics.status).toBe("missing");
  expect(unlisten).toHaveBeenCalledTimes(1);
});
it("preserves rejection and releases the listener", async () => {
  const unlisten = vi.fn(); vi.mocked(listen).mockResolvedValue(unlisten);
  const stale = { code: "CANDIDATES_STALE", message: "discard" };
  vi.mocked(invoke).mockRejectedValue(stale);
  await expect(generateDevelopmentCandidates({ capture_id: "DSC07402", preset: "nightscape", native_roi_diagnostics: true }, vi.fn())).rejects.toBe(stale);
  expect(unlisten).toHaveBeenCalledTimes(1);
});
