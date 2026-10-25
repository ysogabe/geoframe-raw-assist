import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { DevelopmentCandidates } from "./DevelopmentCandidates";
import type { NativeRoiDiagnostic } from "../lib/types";

function renderDiagnostic(status: NativeRoiDiagnostic["status"], reason: string | null) {
  return renderToStaticMarkup(createElement(DevelopmentCandidates, {
    candidates: [], rejected: [], starScene: null, busy: false, error: "", disabled: false,
    diagnostic: { status, reason, rois: [], sets: [], renders: 0, unique_settings: 0, elapsed_ms: { total: 0 } },
    onGenerate: () => {}, onApply: () => {},
  }));
}

it("hides not_run diagnostics and keeps opt-in unchecked by default", () => {
  const html = renderDiagnostic("not_run", "opt_in_disabled");
  expect(html).not.toContain("<details");
  expect(html).not.toContain("opt_in_disabled");
  expect(html).not.toContain("原寸現像0回");
  expect(html).not.toContain("checked");
});

it("keeps missing and failed diagnostics visible with Japanese reasons", () => {
  const missing = renderDiagnostic("missing", "native_roi_incomplete");
  expect(missing).toContain("<details");
  expect(missing).toContain("原寸ROI診断: 欠測");
  expect(missing).toContain("一部の原寸ROIを診断できませんでした。");
  const failed = renderDiagnostic("failed", "SIDECAR_TIMEOUT");
  expect(failed).toContain("原寸ROI診断: 失敗");
  expect(failed).toContain("原寸現像が制限時間を超えました。");
  expect(failed).not.toContain("SIDECAR_TIMEOUT");
});

it("does not describe unknown or absent failure reasons as successful measurements", () => {
  for (const reason of ["NEW_UNKNOWN_REASON", "toString", null]) {
    const html = renderDiagnostic("failed", reason);
    expect(html).toContain("診断を完了できませんでした。");
    expect(html).not.toContain("同一ROI比較を記録しました");
  }
  expect(renderDiagnostic("complete", null)).toContain("完了（画質合格の判定なし）");
});
