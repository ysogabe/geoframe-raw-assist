import { describe, expect, it } from "vitest";
import fixture from "../../src-tauri/tests/fixtures/presets.json";
import { SLIDER_SPECS } from "./format";

/**
 * スライダーの可動域は契約 §2 の値域と一致させる（Issue #80）。値域の正は Rust
 * （src-tauri/src/adjustments.rs の ADJUSTMENT_RANGES）で、Python・Rust・フロントの 3 か所を
 * 同じフィクスチャ src-tauri/tests/fixtures/presets.json で突き合わせる。
 * 再生成: `.venv/bin/python scripts/export_presets_fixture.py --write`
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
