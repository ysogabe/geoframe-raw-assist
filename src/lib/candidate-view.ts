import type { DevelopmentCandidateStyle, StarSceneInfo } from "./types";

/** サーバーの候補 label と同じスタイル名。 */
export const CANDIDATE_STYLE_LABELS: Record<DevelopmentCandidateStyle, string> = {
  natural: "自然",
  stars: "星空",
  foreground: "前景を明るく",
  low_noise: "低ノイズ",
  social: "SNS向け",
};

export function candidateStyleLabel(id: string): string {
  return Object.hasOwn(CANDIDATE_STYLE_LABELS, id)
    ? (CANDIDATE_STYLE_LABELS as Record<string, string>)[id]
    : id;
}

export type StarSceneSummary = {
  /** 「星空として評価」/「星空ではないと判定」 */
  verdict: string;
  isStarScene: boolean;
  /** 根拠の主な数値（表示順）。 */
  facts: { label: string; value: string }[];
};

export function summarizeStarScene(scene: StarSceneInfo): StarSceneSummary {
  const dark = Number.isFinite(scene.dark_background_ratio)
    ? `${Math.round(Math.min(1, Math.max(0, scene.dark_background_ratio)) * 100)}%`
    : "—";
  // サーバー判定は 0.5% 以下を条件とするため、0.5% 前後が読める精度（小数2桁）で出す。
  const bright = Number.isFinite(scene.bright_point_ratio)
    ? `${(Math.min(1, Math.max(0, scene.bright_point_ratio)) * 100).toFixed(2)}%`
    : "—";
  const count = Number.isFinite(scene.star_count) ? `${Math.round(scene.star_count)}個` : "—";
  return {
    isStarScene: scene.is_star_scene,
    verdict: scene.is_star_scene ? "星空として評価" : "星空ではないと判定",
    facts: [
      { label: "暗い背景の割合", value: dark },
      { label: "明るい部分の割合", value: bright },
      { label: "星の数", value: count },
      { label: "EXIFの夜判定", value: scene.exif_night ? "夜" : "夜ではない" },
    ],
  };
}
