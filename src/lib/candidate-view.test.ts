import { describe, expect, it } from "vitest";
import { candidateStyleLabel, summarizeStarScene } from "./candidate-view";
import type { StarSceneInfo } from "./types";

const scene: StarSceneInfo = {
  is_star_scene: true,
  star_count: 42.4,
  dark_background_ratio: 0.734,
  bright_point_ratio: 0.001,
  exif_night: true,
  star_halo: 0.1,
  star_color_saturation: 0.2,
};

describe("candidateStyleLabel", () => {
  it("id を日本語名にする", () => {
    expect(candidateStyleLabel("natural")).toBe("自然");
    expect(candidateStyleLabel("stars")).toBe("星空");
    expect(candidateStyleLabel("foreground")).toBe("前景を明るく");
    expect(candidateStyleLabel("low_noise")).toBe("低ノイズ");
    expect(candidateStyleLabel("social")).toBe("SNS向け");
  });
  it("未知の id はそのまま返す", () => {
    expect(candidateStyleLabel("future")).toBe("future");
  });
  it("プロトタイプ由来のキーでも id 文字列を返す", () => {
    for (const id of ["__proto__", "constructor", "toString", "hasOwnProperty"]) {
      expect(candidateStyleLabel(id)).toBe(id);
    }
  });
});

describe("summarizeStarScene", () => {
  it("星空と判定した場合", () => {
    const s = summarizeStarScene(scene);
    expect(s.verdict).toBe("星空として評価");
    expect(s.facts).toEqual([
      { label: "暗い背景の割合", value: "73%" },
      { label: "明るい部分の割合", value: "0.10%" },
      { label: "星の数", value: "42個" },
      { label: "EXIFの夜判定", value: "夜" },
    ]);
  });
  it("星空でない場合と非有限値", () => {
    const s = summarizeStarScene({ ...scene, is_star_scene: false, exif_night: false, dark_background_ratio: NaN });
    expect(s.verdict).toBe("星空ではないと判定");
    expect(s.facts[0].value).toBe("—");
    expect(s.facts[3].value).toBe("夜ではない");
  });
  it("明るい部分の割合は 0.5% 前後が読める精度で表示する", () => {
    const f = (v: number) => summarizeStarScene({ ...scene, bright_point_ratio: v }).facts[1];
    expect(f(0.0065)).toEqual({ label: "明るい部分の割合", value: "0.65%" });
    expect(f(0.004).value).toBe("0.40%");
    expect(f(0.05).value).toBe("5.00%");
    expect(f(NaN).value).toBe("—");
    expect(f(Infinity).value).toBe("—");
  });
});
