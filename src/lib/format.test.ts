import { describe, expect, it } from "vitest";
import fixture from "../../src-tauri/tests/fixtures/presets.json";
import { formatHighlightRecoveryEffect, SLIDER_SPECS } from "./format";

/**
 * スライダーの可動域は契約 §2 の値域と一致させる（Issue #80）。値域の正は Rust
 * （src-tauri/src/adjustments.rs の ADJUSTMENT_RANGES）。フィクスチャ src-tauri/tests/fixtures/presets.json
 * （CI 値。#81 で Python の legacy フィクスチャと分けた）を Rust のテストと共に読む。
 * 再生成: `UPDATE_PRESETS_FIXTURE=1 cargo test write_presets_fixture -- --ignored`（src-tauri で）
 */
describe("SLIDER_SPECS と値域フィクスチャ", () => {
  const ranges = fixture.adjustment_ranges as Record<string, number[]>;

  it("数値キー 8 本がそろっている", () => {
    expect(SLIDER_SPECS.map((spec) => spec.key).sort()).toEqual(Object.keys(ranges).sort());
  });

  it.each(SLIDER_SPECS.map((spec) => [spec.key, spec] as const))("%s の min / max が一致する", (key, spec) => {
    expect([spec.min, spec.max]).toEqual(ranges[key]);
  });
});

describe("formatHighlightRecoveryEffect（#81）", () => {
  const base = {
    value: 90,
    exposure_delta: -1.0,
    shadow_lift_delta: 55,
    effective_exposure: -1.0,
    effective_shadow_lift: 55,
    shadow_lift_clamped: false,
  };

  it("写像の寄与と実効値を出す", () => {
    expect(formatHighlightRecoveryEffect(base)).toBe(
      "ハイライト回復 90（露出 −1.0 EV / 暗部 +55 として適用。実効 露出 −1.0 EV / 暗部 55）",
    );
  });

  it("スライダーの値に加算した実効値と上限を示す", () => {
    const text = formatHighlightRecoveryEffect({
      ...base,
      value: 60,
      exposure_delta: -0.7,
      shadow_lift_delta: 40,
      effective_exposure: -0.2,
      effective_shadow_lift: 100,
      shadow_lift_clamped: true,
    });
    expect(text).toBe("ハイライト回復 60（露出 −0.7 EV / 暗部 +40 として適用。実効 露出 −0.2 EV / 暗部 100（上限））");
  });

  it("hr 0 や情報が無いときは出さない", () => {
    expect(formatHighlightRecoveryEffect(undefined)).toBeNull();
    expect(formatHighlightRecoveryEffect({ ...base, value: 0, exposure_delta: 0, shadow_lift_delta: 0 })).toBeNull();
  });
});
