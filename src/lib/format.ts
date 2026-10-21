import type {
  Adjustments,
  AnalysisStage,
  DevelopedResult,
  GroupKind,
  HighlightRecoveryEffect,
  MergedResult,
  PhotoInfo,
  RawLevel,
  SourceFolderErrorReason,
  SourceFolderInfo,
} from "./types";

const DASH = "—";

export const rawPillClass = (level: RawLevel) => `raw-pill raw-${level}`;

export function groupKindLabel(kind: GroupKind): string {
  switch (kind) {
    case "burst":
      return "連写";
    case "bracket":
      return "ブラケット";
    case "similar":
      return "類似カット";
    default:
      return "単写";
  }
}

export function analysisStageLabel(stage: AnalysisStage | string): string {
  switch (stage) {
    case "scanning":
      return "ファイルを走査しています";
    case "scoring":
      return "画質を採点しています";
    case "grouping":
      return "似たカットをまとめています";
    default:
      return "解析しています";
  }
}

/** ISO8601（秒精度）から HH:MM を取り出す。パースできなければ空文字。 */
export function formatClock(isoLike: string): string {
  const match = /T(\d{2}):(\d{2})/.exec(isoLike);
  return match ? `${match[1]}:${match[2]}` : "";
}

/** `shutter` は秒の float。1未満は 1/N 表記、1以上は「N秒」。 */
export function formatShutter(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds <= 0) return DASH;
  if (seconds < 1) return `1/${Math.round(1 / seconds)}`;
  return `${Number(seconds.toFixed(1))}秒`;
}

export function formatIso(iso: number | null): string {
  return iso === null ? DASH : `ISO ${iso}`;
}

export function formatAperture(fNumber: number | null): string {
  return fNumber === null ? DASH : `F${Number(fNumber.toFixed(1))}`;
}

export function formatFocalLength(focal: number | null): string {
  return focal === null ? DASH : `${Number(focal.toFixed(1))}mm`;
}

export function formatExposureBias(bias: number | null): string {
  if (bias === null) return DASH;
  const value = Number(bias.toFixed(1));
  return `${value > 0 ? "+" : ""}${value}EV`;
}

/** EXIF の orientation が 6 / 8 なら縦位置。width / height は未回転値なので表示時に入れ替える。 */
export function isPortrait(orientation: number | null): boolean {
  return orientation === 6 || orientation === 8;
}

export function formatDimensions(photo: Pick<PhotoInfo, "width" | "height" | "orientation">): string {
  const [w, h] = isPortrait(photo.orientation) ? [photo.height, photo.width] : [photo.width, photo.height];
  return `${w} × ${h}px`;
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return DASH;
  const gb = bytes / 1024 ** 3;
  if (gb >= 1) return `${gb.toFixed(1)} GB`;
  return `${(bytes / 1024 ** 2).toFixed(0)} MB`;
}

export function formatPercent(ratio: number): number {
  return Math.round(Math.min(Math.max(ratio, 0), 1) * 100);
}

export function formatScore(score: number): number {
  return Math.round(Math.min(Math.max(score, 0), 100));
}

/**
 * 平均輝度は 8bit グレースケールの平均（0〜255、analysis.py の gray.mean()）。
 * ブラケットのどのコマが明るい / 暗いかを見るだけなので小数は落とす。
 */
export function formatMeanLuma(value: number): number {
  return Math.round(Math.min(Math.max(value, 0), 255));
}

/**
 * 現像結果か合成結果かの判別。契約型は型定義のみで実行時の値を持てないため、
 * 判別用のガードはここに置く。MergedResult だけが `capture_ids`（複数形）を持つ。
 */
export function isMergedResult(result: DevelopedResult | MergedResult): result is MergedResult {
  return "capture_ids" in result;
}

/** 0..1 の confidence を % 表示に。 */
export function formatConfidence(confidence: number): number {
  return formatPercent(confidence);
}

export const NOISE_REDUCTION_LABELS: Record<Adjustments["noise_reduction"], string> = {
  off: "なし",
  light: "弱め",
  full: "しっかり",
};

export type SliderSpec = {
  key: Exclude<keyof Adjustments, "noise_reduction">;
  label: string;
  min: number;
  max: number;
  /** 契約 §2 は範囲のみ規定。step は UI 側の設計判断。 */
  step: number;
  /** 表示用の小数桁 */
  digits: number;
  hint: string;
};

/** 契約 §2 の範囲に対応するスライダー8本。 */
export const SLIDER_SPECS: SliderSpec[] = [
  { key: "exposure", label: "露出", min: -2, max: 2, step: 0.1, digits: 1, hint: "EV" },
  { key: "wb_temp", label: "色温度", min: -100, max: 100, step: 1, digits: 0, hint: "青 ←→ 暖" },
  { key: "shadow_lift", label: "暗部の持ち上げ", min: 0, max: 100, step: 1, digits: 0, hint: "0〜100" },
  { key: "highlight_recovery", label: "ハイライト回復", min: 0, max: 100, step: 1, digits: 0, hint: "露出を下げて暗部を戻す" },
  { key: "brightness", label: "明るさ", min: 0.7, max: 1.4, step: 0.01, digits: 2, hint: "倍率" },
  { key: "contrast", label: "コントラスト", min: 0.8, max: 1.4, step: 0.01, digits: 2, hint: "倍率" },
  { key: "saturation", label: "彩度", min: 0, max: 1.5, step: 0.01, digits: 2, hint: "0 でモノクロ" },
  { key: "sharpness", label: "シャープネス", min: 0.8, max: 1.6, step: 0.01, digits: 2, hint: "倍率" },
];

export const ADJUSTMENT_LABELS: Record<keyof Adjustments, string> = {
  exposure: "露出",
  wb_temp: "色温度",
  shadow_lift: "暗部の持ち上げ",
  highlight_recovery: "ハイライト回復",
  brightness: "明るさ",
  contrast: "コントラスト",
  saturation: "彩度",
  sharpness: "シャープネス",
  noise_reduction: "ノイズ低減",
};

export function formatAdjustmentValue(key: keyof Adjustments, adjustments: Adjustments): string {
  if (key === "noise_reduction") return NOISE_REDUCTION_LABELS[adjustments.noise_reduction];
  const spec = SLIDER_SPECS.find((item) => item.key === key);
  const value = adjustments[key];
  return spec ? value.toFixed(spec.digits) : String(value);
}

/**
 * Core Image で highlight_recovery が実際にどう効いたか（#81）。hr 0 や情報が無いときは null（表示しない）。
 * 例: 「ハイライト回復 90（露出 −1.0 EV / 暗部 +55 として適用。実効 露出 −1.0 EV / 暗部 55）」
 */
export function formatHighlightRecoveryEffect(effect: HighlightRecoveryEffect | undefined): string | null {
  if (!effect || !(effect.value > 0)) return null;
  const signed = (value: number, digits: number) => `${value < 0 ? "−" : "+"}${Math.abs(value).toFixed(digits)}`;
  const ev = (value: number) => (Math.abs(value) < 0.005 ? "0.0" : signed(value, 1));
  const lift = `${Math.round(effect.effective_shadow_lift)}${effect.shadow_lift_clamped ? "（上限）" : ""}`;
  return (
    `ハイライト回復 ${Math.round(effect.value)}（露出 ${ev(effect.exposure_delta)} EV / 暗部 ${signed(effect.shadow_lift_delta, 0)} として適用。` +
    `実効 露出 ${ev(effect.effective_exposure)} EV / 暗部 ${lift}）`
  );
}

/** 現像結果のレンズ補正バッジ用のテキスト。 */
const LENS_FLAG_LABELS: Record<string, string> = {
  distortion: "歪曲",
  tca: "色収差",
  vignetting: "周辺減光",
};

export function lensFlagLabels(flags: string[] | undefined): string {
  if (!flags?.length) return "";
  return flags.map((flag) => LENS_FLAG_LABELS[flag] ?? flag).join("・");
}

const LENS_REASON_LABELS: Record<string, string> = {
  lens_not_in_db: "レンズがDBに未登録",
  camera_not_found: "カメラがDBに未登録",
  lensfunpy_unavailable: "lensfun 未導入",
  coreimage_unsupported: "Core Image が補正に未対応",
};

export function lensReasonLabel(reason: string | undefined): string {
  if (!reason) return "適用なし";
  return LENS_REASON_LABELS[reason] ?? reason;
}

// ---------------------------------------------------------------------------
// 現像対象フォルダ
// ---------------------------------------------------------------------------

/**
 * 読み取り許可が無いときの案内。macOS は ~/Pictures / ~/Desktop / 外部ボリュームへの
 * 初回アクセスで許可ダイアログを出し、拒否されると以後読めなくなる。
 * 「フォルダに写真が無い」と取り違えられないよう、専用の文言を必ず出すこと。
 */
export const SOURCE_FOLDER_PERMISSION_HELP =
  "このフォルダを読み取る許可がありません。システム設定 > プライバシーとセキュリティ > ファイルとフォルダ で許可してから、もう一度選び直してください。";

/** ペアが1組も無いフォルダを選んだとき。保存自体は成功しているので、エラーではなく注意として出す。 */
export const SOURCE_FOLDER_NO_PAIRS =
  "このフォルダには DSC#####.JPG と DSC#####.ARW のペアが見つかりません。別のフォルダを選び直してください。";

/** Python 側が未対応・停止しているときにフォルダ変更を試した場合。 */
export const SOURCE_FOLDER_UNAVAILABLE = "現像対象フォルダの変更を準備中です。しばらくしてからお試しください。";

/**
 * POST /api/source-folder の失敗をユーザー向けの文にする。
 * invalid / not_found / not_directory はサーバの文言がそのまま十分に具体的なので素通しする。
 */
export function sourceFolderErrorMessage(
  reason: SourceFolderErrorReason | null,
  serverMessage: string,
): string {
  switch (reason) {
    case "permission_denied":
      return SOURCE_FOLDER_PERMISSION_HELP;
    case "analysis_running":
      return "解析の実行中はフォルダを変更できません。解析が終わってからやり直してください。";
    default:
      return serverMessage || "現像対象フォルダを変更できませんでした。";
  }
}

/**
 * 選択中フォルダそのものの不調（保存後に外部ボリュームが外れた／許可を取り消された／
 * ペアが無い）を表す短い注記。サイドバーは幅が狭いので1行に収まる長さにし、
 * 長い案内が要る permission 系はトースト側（SOURCE_FOLDER_PERMISSION_HELP）で補う。
 * 問題が無ければ null。
 */
export function sourceFolderIssue(info: SourceFolderInfo): string | null {
  if (!info.exists) return "フォルダが見つかりません";
  if (!info.readable) return "読み取りできません";
  if (info.pairs === 0) return "JPG/ARWのペアなし";
  return null;
}
