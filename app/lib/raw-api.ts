import type { DevelopedResult, RawPreset } from "./types";

export const LOCAL_RAW_API = "http://127.0.0.1:8788";

export async function fetchHealth(): Promise<{ rawpy_version: string }> {
  const response = await fetch(`${LOCAL_RAW_API}/api/health`);
  if (!response.ok) throw new Error("offline");
  return response.json();
}

export async function developRaw(captureId: string, preset: RawPreset, maxEdge: number): Promise<DevelopedResult> {
  const response = await fetch(`${LOCAL_RAW_API}/api/raw-develop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ capture_id: captureId, preset, max_edge: maxEdge }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "RAW現像に失敗しました");
  return result;
}
