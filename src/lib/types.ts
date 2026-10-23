/**
 * GeoFrame RAW Assist — API 契約型（凍結版）
 *
 * 凍結済み。変更には全ウェーブ停止が必要。
 *
 * `docs/API_CONTRACT.md` の TypeScript 側の写しであり、Python 側との唯一の正である。
 * Wave 1 以降のエージェントは参照のみ行うこと。変更が必要になった場合は実装を止め、
 * orchestrator に差し戻して全ウェーブ停止のうえ再凍結する。
 *
 * 凍結日: 2026-08-10
 * 出典: .claude/plans/vectorized-snacking-falcon-agent-ad862d8a7d0ff47d3.md §d / §f
 *
 * 利用方針:
 * - Wave 2-C2 で契約型を本ファイルへ一本化した（Wave 1-C の暫定型 Shot / Group / プリセット union は廃止）。
 * - 表示用のフォーマッタ等の実行時ヘルパーは `app/lib/format.ts` に置く。
 *
 * 本ファイルは型定義のみで構成する（実行時の値を持たない）。
 * tsconfig の isolatedModules が有効なため、`const enum` は使用しない。
 */

/** ベースURL。RAW_ASSIST_PORT で変更可能。 */
export type RawApiBaseUrl = string;

// ---------------------------------------------------------------------------
// 共通
// ---------------------------------------------------------------------------

/** 撮影ID。`DSC` + 5桁数字（例: "DSC02943"）。 */
export type CaptureId = string;

/**
 * プリセット名。
 * union 型は意図的に廃止した（Python 側との二重定義を避けるため）。
 * 実際の一覧は動的に取得する（Rust の `get_presets`。raw-api.ts の `getPresets`。Issue #80）。
 */
export type PresetName = string;

/** 全エラーレスポンスの形。HTTP 400 / 403 / 404 / 500 共通。 */
export type ErrorResponse = {
  error: string;
};

// ---------------------------------------------------------------------------
// Adjustments — 現像パラメータ
// ---------------------------------------------------------------------------

export type NoiseReduction = "off" | "light" | "full";

/**
 * 現像調整パラメータ。範囲外・未知キーはサーバ側で reject（HTTP 400）される。
 * リクエストでは Partial<Adjustments> を送り、未指定キーはプリセット既定値で埋められる。
 * DevelopedResult.adjustments は解決後の全量が返る。
 */
export type Adjustments = {
  /** EV -2.0..+2.0。Core Image の露出補正。 */
  exposure: number;
  /** -100..100。camera_whitebalance の R/B ゲインをスケールし user_wb へ */
  wb_temp: number;
  /** 0..100。256エントリLUT（下半分中心の持ち上げ） */
  shadow_lift: number;
  /**
   * 0..100。
   * Core Image（CLI 0.6.0、#81）: 区分線形の写像 0 → (0EV, 0)、60 → (−0.7EV, +40)、90 → (−1.0EV, +55)、
   * 100 → (−1.1EV, +60) を exposure / shadow_lift に加算する（露出を下げて暗部を戻す）。
   */
  highlight_recovery: number;
  /** 0.7..1.4 (ImageEnhance.Brightness) */
  brightness: number;
  /** 0.8..1.4 (ImageEnhance.Contrast) */
  contrast: number;
  /** 0.0..1.5 (ImageEnhance.Color)。0 = モノクロ */
  saturation: number;
  /** 0.8..1.6 (ImageEnhance.Sharpness) */
  sharpness: number;
  noise_reduction: NoiseReduction;
};

/** UIスライダーの可動域（DevelopControls 用の参考値。契約の一部）。 */
export type AdjustmentRange = {
  min: number;
  max: number;
};

// ---------------------------------------------------------------------------
// プリセット（Tauri `get_presets`）
// ---------------------------------------------------------------------------

/** プリセット定義。adjustments はUIスライダーの初期値になる。 */
export type PresetInfo = {
  name: PresetName;
  label: string;
  description: string;
  adjustments: Adjustments;
};

// ---------------------------------------------------------------------------
// 分析 — get_analysis, start_analysis
// ---------------------------------------------------------------------------

/** 解析の進行ステージ。 */
export type AnalysisStage = "scanning" | "scoring" | "grouping";

export type AnalysisProgress = {
  stage: AnalysisStage | string;
  done: number;
  total: number;
};

export type PhotoInfo = {
  capture_id: CaptureId;
  /** ISO8601。ILCE-6400 はサブ秒を記録しないため秒精度。 */
  captured_at: string;
  iso: number | null;
  f_number: number | null;
  shutter: number | null;
  focal_length: number | null;
  exposure_bias: number | null;
  lens_model: string | null;
  orientation: number | null;
  width: number;
  height: number;
  /** グレースケール Laplacian 分散の生値 */
  sharpness_raw: number;
  /** グループ内で正規化した 0-100 */
  sharpness_score: number;
  /** いずれかのチャンネルが >=250 の画素比率 */
  highlight_clip_ratio: number;
  /** 全チャンネルが <=5 の画素比率 */
  shadow_clip_ratio: number;
  mean_luma: number;
  overall_score: number;
  /** 例: "/thumbs/DSC02703.webp" */
  thumb: string;
};

export type GroupKind = "burst" | "bracket" | "similar" | "single";

/** RAW現像推薦の強さ（4段階、ルールベース）。 */
export type RawLevel = "none" | "maybe" | "recommended" | "strong";

/**
 * RAW推薦の理由コード。日本語文は reasons[] に併記される。
 * 将来の追加に備え string との union にしてある。
 */
export type RawReasonCode =
  | "high_iso"
  | "night_scene"
  | "highlight_clipping"
  | "deep_shadows"
  | "exposure_bracket"
  | (string & {});

export type RawRecommendation = {
  level: RawLevel;
  label: string;
  reason_codes: RawReasonCode[];
  reasons: string[];
};

export type RecommendationReason = {
  code: string;
  text: string;
};

export type GroupInfo = {
  id: number;
  kind: GroupKind;
  /** 例: "10:42 連写 3枚" */
  title: string;
  start_at: string;
  end_at: string;
  capture_ids: CaptureId[];
  recommended_capture_id: CaptureId;
  confidence: number;
  recommendation_reasons: RecommendationReason[];
  raw: RawRecommendation;
};

export type AnalysisSource = {
  root: string;
  pairs: number;
  jpg_only: number;
  arw_only: number;
  total_bytes: number;
};

export type AnalysisSummary = {
  groups: number;
  raw_candidates: number;
  needs_review: number;
};

export type Analysis = {
  /** 2 = Python エンジン、3 = Rust（Tauri。#110）。 */
  analysis_version: 2 | 3;
  /**
   * false のとき採点値（シャープ・白飛び・黒つぶれ・総合点・信頼度）は未計算の 0。
   * v2（Python）には無いフィールドで、undefined は採点済みとして扱う。
   */
  scored?: boolean;
  generated_at: string;
  source: AnalysisSource;
  summary: AnalysisSummary;
  photos: Record<CaptureId, PhotoInfo>;
  groups: GroupInfo[];
};

export type AnalysisAbsentResponse = { status: "absent"; error?: string };
export type AnalysisRunningResponse = {
  status: "running";
  progress: AnalysisProgress;
};
export type AnalysisReadyResponse = { status: "ready"; analysis: Analysis };

export type AnalysisResponse =
  | AnalysisAbsentResponse
  | AnalysisRunningResponse
  | AnalysisReadyResponse;

export type AnalyzeRequest = {
  force?: boolean;
};

export type AnalyzeResponse = {
  status: "started" | "already_running" | "ready";
};

// ---------------------------------------------------------------------------
// 現像結果（DevelopedResult）
// ---------------------------------------------------------------------------

export type RawDevelopRequest = {
  capture_id: CaptureId;
  preset: PresetName;
  adjustments?: Partial<Adjustments>;
  /** true で half_size + max_edge 1600、出力先 outputs/preview/（2〜5秒） */
  preview?: boolean;
  force?: boolean;
  /** 1200..6048。範囲外は HTTP 400 */
  max_edge?: number;
};

export type LensCorrectionReason =
  | "lens_not_in_db"
  | "camera_not_found"
  | "lensfunpy_unavailable"
  | (string & {});

export type LensCorrection = {
  applied: boolean;
  /** applied=false のときの理由。"error:<msg>" 形式も取りうる。 */
  reason?: LensCorrectionReason;
  /** applied=true のときの lensfun 表記のレンズ名 */
  lens?: string;
  flags?: string[];
  focal_length?: number;
  aperture?: number;
};

export type DevelopedResult = {
  capture_id: CaptureId;
  preset: PresetName;
  preset_label: string;
  /** Core Image: {capture_id}_{hash16}.jpg。 */
  filename: string;
  recipe_filename: string;
  /** キャッシュ鍵 hash16 の先頭10文字（表示用）。出力ファイル名には全16文字を含む。 */
  params_hash: string;
  /** 解決後の全量 */
  adjustments: Adjustments;
  width: number;
  height: number;
  elapsed_seconds: number;
  cached: boolean;
  preview: boolean;
  lens_correction: LensCorrection;
  exif_inherited: boolean;
  /**
   * 配信URL。キャッシュ鍵をファイル名に含む asset プロトコルの完全URL。
   * そのまま <img src> に渡すこと。パスとして再構成してはならない。
   */
  output_url: string;
  /**
   * 現像したエンジン。プレビュー・本現像とも "coreimage"。既存の保存結果では未設定の場合がある。
   */
  engine?: DevelopEngine;
  /**
   * Core Image で highlight_recovery を exposure / shadow_lift へ写像した結果（凍結後の任意フィールド追加、#81）。
   * CLI 応答 `adjustments_applied.highlight_recovery` の写し。CLI 0.5.0 以前では未設定。
   */
  highlight_recovery_effect?: HighlightRecoveryEffect;
};

/** highlight_recovery の写像と合成の結果（CLI 0.6.0、#81）。lift はスライダー単位（0..100）。 */
export type HighlightRecoveryEffect = {
  value: number;
  exposure_delta: number;
  shadow_lift_delta: number;
  effective_exposure: number;
  effective_shadow_lift: number;
  shadow_lift_clamped: boolean;
};

/** 現像エンジン（凍結後の追加、#72）。 */
export type DevelopEngine = "coreimage";

// ---------------------------------------------------------------------------
// 現像候補 — Core Image 縮小デコード代理による現像候補
// ---------------------------------------------------------------------------

export type DevelopmentCandidateStyle = "natural" | "stars" | "foreground" | "low_noise" | "social";

/** 候補選択の比較に使う代理評価値（0..1、star_count は検出数）。 */
export type DevelopmentCandidateMetrics = {
  highlight_clip: number;
  shadow_clip: number;
  noise_flat: number;
  source_noise_flat: number;
  detail: number;
  source_detail: number;
  halo: number;
  color_saturation_clip: number;
  tonal_range: number;
  /** 基準（natural＝プリセット値のまま）の代理画像との平均絶対差。 */
  source_deviation: number;
  /** 基準の暗部（輝度下位25%）に相当する画素の平均輝度。 */
  shadow_mean: number;
  star_count: number;
  star_halo: number;
  star_color_saturation: number;
  /** 互換・表示用の長い別名。サーバは同じ値を返す。 */
  highlight_clipping: number;
  shadow_crushing: number;
  flat_region_noise: number;
  detail_halo: number;
  color_clip: number;
  tone_range: number;
  baseline_deviation: number;
};

export type StarSceneInfo = {
  is_star_scene: boolean;
  star_count: number;
  dark_background_ratio: number;
  bright_point_ratio: number;
  /** 点光源の密度（点/画素）。測っていないとき（daylight・前提条件に落ちた場合など）は null。 */
  star_point_density: number | null;
  exif_night: boolean;
  exif_state: "night" | "daylight" | "unknown";
  star_halo: number;
  star_color_saturation: number;
};

export type DevelopmentCandidate = {
  id: DevelopmentCandidateStyle;
  label: string;
  description: string;
  adjustments: Adjustments;
  metrics: DevelopmentCandidateMetrics;
  score: number;
  reasons: string[];
};

export type DevelopmentCandidatesRequest = {
  capture_id: CaptureId;
  preset: PresetName;
  /** 省略時は capture_id:preset の安定SHA-256由来。 */
  seed?: number;
  /** 32〜512。省略時48。 */
  budget?: number;
};

export type DevelopmentCandidatesResponse = {
  capture_id: CaptureId;
  preset: PresetName;
  seed: number;
  budget: number;
  proxy: {
    max_edge: number;
    width: number;
    height: number;
    source: "coreimage_proxy";
    /** キャッシュに保持したデコード結果の長辺。 */
    decode_edge: number;
    /** 縮小デコードを実行した回数。 */
    decodes: number;
    renders: number;
    evaluations: number;
  };
  elapsed_ms: { total: number; decode: number; render: number };
  star_scene: StarSceneInfo;
  candidates: DevelopmentCandidate[];
  /** 基準より白飛び・黒つぶれ等が悪化したため返さなかった候補と、その理由。 */
  rejected: { id: DevelopmentCandidateStyle; reason: string }[];
};

// ---------------------------------------------------------------------------
// 書き出し — export_selection
// ---------------------------------------------------------------------------

/**
 * 書き出しの種別。"merged" はブラケット合成の結果（M2 まで隠している。
 * 本ファイル末尾の追加セクション）を指す。
 */
export type ExportVariant = "camera_jpg" | "developed" | "merged";

export type ExportItem = {
  capture_id: CaptureId;
  variant: ExportVariant;
  /** variant="developed" のとき必須。raw-develop が返した filename。 */
  developed_filename?: string;
  /** variant="merged" のとき必須。merge-bracket が返した filename。 */
  merged_filename?: string;
};

export type ExportRequest = {
  /** [A-Za-z0-9_-]+ に正規化される */
  batch_name: string;
  items: ExportItem[];
};

export type ExportSkipped = {
  capture_id: CaptureId;
  reason: string;
};

export type ExportResponse = {
  export_dir: string;
  manifest_path: string;
  files: string[];
  skipped: ExportSkipped[];
};

// ---------------------------------------------------------------------------
// 「写真」アプリへの取り込み（M1 の #115 まで隠している）
// ---------------------------------------------------------------------------

export type ImportToPhotosRequest = {
  batch_name: string;
  /** 既定 "GeoFrame RAW Assist <batch>" */
  album?: string;
};

export type ImportToPhotosOk = {
  status: "ok";
  imported: number;
  album: string;
};

export type ImportToPhotosError = {
  status: "error";
  error: string;
  stderr?: string;
};

export type ImportToPhotosResponse = ImportToPhotosOk | ImportToPhotosError;

// ---------------------------------------------------------------------------
// マニフェスト（outputs/export/<batch>/manifest.json、プラン §3）
// ---------------------------------------------------------------------------

export type ManifestItemSource = {
  jpg: string;
  arw: string | null;
  jpg_sha256: string;
};

export type ManifestItemGroup = {
  id: number;
  kind: GroupKind;
  raw_level: RawLevel;
  was_recommended: boolean;
  user_selected: boolean;
};

export type ManifestItemExif = {
  captured_at: string;
  iso: number | null;
  lens_model: string | null;
  focal_length: number | null;
};

export type ManifestItem = {
  capture_id: CaptureId;
  variant: ExportVariant;
  exported_file: string;
  source: ManifestItemSource;
  /** developed のレシピJSONを inline。camera_jpg なら null。 */
  recipe: Record<string, unknown> | null;
  group: ManifestItemGroup;
  exif: ManifestItemExif;
};

export type ExportManifest = {
  manifest_version: 1;
  app: "geoframe-raw-assist";
  batch_name: string;
  created_at: string;
  source_root: string;
  items: ManifestItem[];
};

// ---------------------------------------------------------------------------
// 現像対象フォルダ — get_source_folder, set_source_folder
//
// 本セクションは凍結（2026-08-10）後の追加分である。既存の契約型には一切手を
// 触れていない。Python 側の実装と対になっており、変更する場合は両方を同時に
// 直すこと。
// ---------------------------------------------------------------------------

/**
 * 現在の現像対象フォルダの情報。GET / POST いずれも成功時はこの形を返す。
 * `pairs` などの枚数は保存時点でのスキャン結果であり、`get_analysis` の
 * `AnalysisSource` とは取得タイミングが違うため一致しないことがある。
 * フォルダそのものの正は常にこちら側。
 */
export type SourceFolderInfo = {
  /** 解決済みの絶対パス */
  path: string;
  /** 表示用。プロジェクト相対 / "~/..." / 絶対のいずれか */
  display_path: string;
  /** 単一アプリ化で意味が無くなった（常に false） */
  is_default: boolean;
  exists: boolean;
  /** 読み取り可能か。false は macOS のアクセス許可が無い場合を含む */
  readable: boolean;
  /** JPG/ARW 両方そろった組数 */
  pairs: number;
  jpg_only: number;
  arw_only: number;
  total_bytes: number;
};

export type SourceFolderRequest = {
  path: string;
};

/**
 * set_source_folder が返す失敗理由。
 * `permission_denied` は「写真が無い」ではなく macOS のアクセス許可の問題なので、
 * UI では必ず専用の案内（システム設定 > プライバシーとセキュリティ）を出すこと。
 * 将来の追加に備え string との union にしてある。
 */
export type SourceFolderErrorReason =
  | "invalid"
  | "not_found"
  | "not_directory"
  | "permission_denied"
  | "analysis_running"
  | (string & {});

/** set_source_folder の失敗レスポンス。 */
export type SourceFolderErrorResponse = ErrorResponse & {
  reason: SourceFolderErrorReason;
};

// ---------------------------------------------------------------------------
// ブラケット合成（M2 まで隠している）
//
// 本セクションは凍結（2026-08-10）後の追加分である。既存の契約型に手を入れたのは
// ExportVariant / ExportItem の 2 箇所だけで、いずれも合成結果を書き出せるように
// するための最小の拡張（"merged" と merged_filename の追加）である。
// Python 側の実装と対になっており、変更する場合は両方を同時に直すこと。
// ---------------------------------------------------------------------------

/**
 * ブラケット合成のリクエスト。
 * `capture_ids` は 2〜9 枚。範囲外はサーバが HTTP 400（ErrorResponse）を返すため、
 * UI 側でも同じ範囲で実行ボタンを制御すること（押せるのに必ず失敗する状態を作らない）。
 * フレーム数ぶんの RAW デコードが走るため、preview でも数秒、原寸なら数十秒かかる。
 */
export type MergeBracketRequest = {
  capture_ids: CaptureId[];
  preset: PresetName;
  adjustments?: Partial<Adjustments>;
  /** true でプレビュー品質（raw-develop と同じ扱い）。書き出しには使えない。 */
  preview?: boolean;
  force?: boolean;
  /** 1200..6048。範囲外は HTTP 400 */
  max_edge?: number;
};

/**
 * ブラケット合成の結果。DevelopedResult と多くのフィールドを共有するが、
 * 単写の `capture_id` ではなく合成に使った `capture_ids`（と `frames`）を持つ点、
 * および位置合わせの実施有無 `aligned` を持つ点が異なる。
 */
export type MergedResult = {
  /**
   * 合成に使ったフレーム。サーバ側で撮影IDの昇順に正規化される（リクエストで送った順ではない）。
   * export_selection の `variant: "merged"` では合成ファイル名の先頭IDと一致させるため、
   * `capture_id` に `capture_ids[0]` を渡さなければならない。
   */
  capture_ids: CaptureId[];
  /** capture_ids.length と同じ。2〜9。 */
  frames: number;
  preset: PresetName;
  preset_label: string;
  filename: string;
  recipe_filename: string;
  params_hash: string;
  /** 解決後の全量 */
  adjustments: Adjustments;
  width: number;
  height: number;
  elapsed_seconds: number;
  cached: boolean;
  preview: boolean;
  /** フレーム間の位置合わせを実施したか。手持ち撮影のブラケットで意味を持つ。 */
  aligned: boolean;
  lens_correction: LensCorrection;
  exif_inherited: boolean;
  /**
   * 配信URL。DevelopedResult.output_url と同じくキャッシュバスター付きの完全URLで、
   * そのまま <img src> に渡すこと。パスとして再構成してはならない。
   */
  output_url: string;
};
