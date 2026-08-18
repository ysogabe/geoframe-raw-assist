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
 * 実際の一覧は GET /api/health の `presets` から動的に取得する。
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
  /** EV -2.0..+2.0 → rawpy exp_shift = clamp(2**ev, 0.25, 8) */
  exposure: number;
  /** -100..100。camera_whitebalance の R/B ゲインをスケールし user_wb へ */
  wb_temp: number;
  /** 0..100。256エントリLUT（下半分中心の持ち上げ） */
  shadow_lift: number;
  /** 0..100 → exp_preserve_highlights 0.4..1.0、>=75 で HighlightMode.Reconstruct */
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
// GET /api/health
// ---------------------------------------------------------------------------

/** プリセット定義。adjustments はUIスライダーの初期値になる。 */
export type PresetInfo = {
  name: PresetName;
  label: string;
  description: string;
  adjustments: Adjustments;
};

export type LensfunInfo = {
  available: boolean;
  /**
   * lensfunpy.lensfun_version を "0.3.4.0" 形式に整形した文字列。
   * lensfun にDBスナップショット固有の版番号APIは無いため optional。
   */
  db_version?: string;
};

export type HealthResponse = {
  status: "ready";
  engine: "rawpy/LibRaw";
  rawpy_version: string;
  lensfun: LensfunInfo;
  presets: PresetInfo[];
};

// ---------------------------------------------------------------------------
// 分析 — GET /api/analysis, POST /api/analyze
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
  analysis_version: 2;
  generated_at: string;
  source: AnalysisSource;
  summary: AnalysisSummary;
  photos: Record<CaptureId, PhotoInfo>;
  groups: GroupInfo[];
};

export type AnalysisAbsentResponse = { status: "absent" };
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
// POST /api/raw-develop
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
  /** {capture_id}-{preset}-{hash10}.jpg */
  filename: string;
  recipe_filename: string;
  /** sha1(canonical JSON)[:10]。キャッシュ判定はレシピJSON内のこの値の完全一致で行う。 */
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
   * 配信URL。キャッシュバスター `?v=<st_mtime_ns>` が付いた完全URLである。
   * そのまま <img src> に渡すこと。パスとして再構成してはならない。
   */
  output_url: string;
};

// ---------------------------------------------------------------------------
// POST /api/export
// ---------------------------------------------------------------------------

export type ExportVariant = "camera_jpg" | "developed";

export type ExportItem = {
  capture_id: CaptureId;
  variant: ExportVariant;
  /** variant="developed" のとき必須。raw-develop が返した filename。 */
  developed_filename?: string;
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
// POST /api/import-to-photos
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
// 現像対象フォルダ — GET /api/source-folder, POST /api/source-folder
//
// 本セクションは凍結（2026-08-10）後の追加分である。既存の契約型には一切手を
// 触れていない。Python 側の実装と対になっており、変更する場合は両方を同時に
// 直すこと。
// ---------------------------------------------------------------------------

/**
 * 現在の現像対象フォルダの情報。GET / POST いずれも成功時はこの形を返す。
 * `pairs` などの枚数は保存時点でのスキャン結果であり、`GET /api/analysis` の
 * `AnalysisSource` とは取得タイミングが違うため一致しないことがある。
 * フォルダそのものの正は常にこちら側。
 */
export type SourceFolderInfo = {
  /** 解決済みの絶対パス */
  path: string;
  /** 表示用。プロジェクト相対 / "~/..." / 絶対のいずれか */
  display_path: string;
  /** 既定の sample/100MSDCF のままかどうか */
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
 * POST /api/source-folder が HTTP 400 で返す失敗理由。
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

/** POST /api/source-folder の失敗レスポンス（HTTP 400）。 */
export type SourceFolderErrorResponse = ErrorResponse & {
  reason: SourceFolderErrorReason;
};
