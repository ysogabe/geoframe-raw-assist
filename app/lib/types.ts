export type Shot = {
  id: string;
  image: string;
  score: number;
  sharpness: number;
  highlight: number;
  label?: string;
};

export type Group = {
  id: number;
  title: string;
  kind: string;
  time: string;
  recommendation: string;
  reason: string;
  raw: "none" | "maybe" | "recommended" | "strong";
  rawLabel: string;
  shots: Shot[];
};

export type RawPreset = "neutral_recovery" | "landscape_clear" | "nightscape";

export type DevelopedResult = {
  capture_id: string;
  preset: RawPreset;
  preset_label: string;
  output_url: string;
  width: number;
  height: number;
  elapsed_seconds: number;
  cached: boolean;
};

export const rawClass = (level: Group["raw"]) => `raw-pill raw-${level}`;
