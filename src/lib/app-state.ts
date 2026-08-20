import { SOURCE_FOLDER_PERMISSION_HELP } from "./format";
import type {
  Adjustments,
  Analysis,
  AnalysisProgress,
  AnalysisResponse,
  CaptureId,
  DevelopedResult,
  ExportResponse,
  GroupInfo,
  HealthResponse,
  ImportToPhotosResponse,
  MergedResult,
  PhotoInfo,
  PresetInfo,
  PresetName,
  SourceFolderInfo,
} from "./types";

export type Screen = "suggestions" | "operation" | "export";
export type EngineStatus = "checking" | "ready" | "offline";
export type AnalysisState = "unknown" | "absent" | "running" | "ready" | "error";
export type ListView = "suggestions" | "queue" | "accepted";
export type DevelopMode = "preview" | "full";
export type ExportVariantChoice = "camera_jpg" | "developed" | "merged";
/** 比較エリアに出す結果の種類。単写の現像結果とブラケット合成結果を切り替える。 */
export type ComparisonView = "develop" | "merged";

/**
 * アプリ内の原寸ビューアに渡す 2 面。
 *
 * デスクトップシェル（Tauri/WKWebView）には別タブで開くリンク（target=_blank）を処理する
 * 経路が無く、「原寸で見る」が無反応になっていた。外部ブラウザへ逃がすのではなく、
 * 同じ拡大率・同じ位置のまま現像前後を見比べられるビューアを内製する。
 */
export type ViewerSource = {
  /** 現像前（カメラJPG）の URL。ビューアの基準グリッドはこの画像の実解像度に固定する。 */
  beforeUrl: string;
  /** 現像後 / 合成後の URL。無い場合（まだ現像していない）は undefined。 */
  afterUrl?: string;
  beforeLabel: string;
  afterLabel: string;
  /** 開いた直後にどちらの面を見せるか。 */
  initial: "before" | "after";
  /** 現像結果がプレビュー品質か。ヘッダの .preview-tag の出し分けに使う。 */
  preview: boolean;
  /** ヘッダに出す見出し（撮影ID など）。 */
  title: string;
};

const DEFAULT_PRESET: PresetName = "neutral_recovery";

/**
 * ブラケット合成に渡せるフレーム数（契約: POST /api/merge-bracket）。
 * 範囲外はサーバが HTTP 400 を返すため、UI 側も同じ範囲で実行を止める。
 */
export const MERGE_MIN_FRAMES = 2;
export const MERGE_MAX_FRAMES = 9;

export type State = {
  screen: Screen;
  engineStatus: EngineStatus;
  health: HealthResponse | null;
  analysisState: AnalysisState;
  analysisProgress: AnalysisProgress | null;
  analysis: Analysis | null;
  analysisError: string;
  analyzeRequested: boolean;
  /** 現像対象フォルダ。GET /api/source-folder が唯一の正で、analysis.source より優先する。 */
  sourceFolder: SourceFolderInfo | null;
  /** エンドポイントが 404 / 接続不能だったとき。フォルダ変更UIを出さずに従来表示へ退避する。 */
  sourceFolderUnavailable: boolean;
  /** POST /api/source-folder の実行中。 */
  sourceFolderBusy: boolean;
  selectedGroupId: number | null;
  /** グループごとの採用候補。未設定なら recommended_capture_id を使う。 */
  selectedCaptures: Record<number, CaptureId>;
  acceptedGroupIds: number[];
  listView: ListView;
  presetName: PresetName;
  /** プリセット既定値からのユーザー変更分だけを保持し、そのまま送信する。 */
  overrides: Partial<Adjustments>;
  developMode: DevelopMode | null;
  developingCapture: CaptureId | null;
  developStartedAt: number;
  previewResults: Record<CaptureId, DevelopedResult>;
  fullResults: Record<CaptureId, DevelopedResult>;
  /**
   * ブラケット合成の状態は **グループ単位** で持つ（capture_id 単位ではない）。
   * 合成は複数フレームの産物であり、どの 1 枚にも属さないため。
   */
  mergeSelections: Record<number, CaptureId[]>;
  mergePreviewResults: Record<number, MergedResult>;
  /** 書き出し（variant="merged"）に使えるのは本合成の結果だけ。プレビューと混ぜてはならない。 */
  mergeFullResults: Record<number, MergedResult>;
  mergeMode: DevelopMode | null;
  mergingGroupId: number | null;
  mergeStartedAt: number;
  comparisonView: ComparisonView;
  batchName: string;
  exportVariants: Record<CaptureId, ExportVariantChoice>;
  exporting: boolean;
  exportResult: ExportResponse | null;
  importing: boolean;
  importResult: ImportToPhotosResponse | null;
  /** 原寸ビューアで開いている 2 面。null なら閉じている。 */
  viewer: ViewerSource | null;
  notice: string;
  noticeTone: "info" | "error";
};

export const initialState: State = {
  screen: "suggestions",
  engineStatus: "checking",
  health: null,
  analysisState: "unknown",
  analysisProgress: null,
  analysis: null,
  analysisError: "",
  analyzeRequested: false,
  sourceFolder: null,
  sourceFolderUnavailable: false,
  sourceFolderBusy: false,
  selectedGroupId: null,
  selectedCaptures: {},
  acceptedGroupIds: [],
  listView: "suggestions",
  presetName: DEFAULT_PRESET,
  overrides: {},
  developMode: null,
  developingCapture: null,
  developStartedAt: 0,
  previewResults: {},
  fullResults: {},
  mergeSelections: {},
  mergePreviewResults: {},
  mergeFullResults: {},
  mergeMode: null,
  mergingGroupId: null,
  mergeStartedAt: 0,
  comparisonView: "develop",
  batchName: "",
  exportVariants: {},
  exporting: false,
  exportResult: null,
  importing: false,
  importResult: null,
  viewer: null,
  notice: "",
  noticeTone: "info",
};

export type Action =
  | { type: "health/ok"; health: HealthResponse }
  | { type: "health/fail" }
  | { type: "analysis/status"; response: AnalysisResponse }
  | { type: "analysis/requested" }
  | { type: "analysis/error"; message: string }
  | { type: "source/loaded"; info: SourceFolderInfo }
  | { type: "source/unavailable" }
  | { type: "source/applying" }
  | { type: "source/changed"; info: SourceFolderInfo; message: string; tone?: "info" | "error" }
  | { type: "source/failed"; message: string }
  | { type: "screen/suggestions" }
  | { type: "screen/export" }
  | { type: "screen/operation"; groupId: number }
  | { type: "list/view"; view: ListView }
  | { type: "group/chooseCapture"; groupId: number; captureId: CaptureId }
  | { type: "group/accept"; groupId: number }
  | { type: "preset/change"; presetName: PresetName }
  | { type: "adjust/change"; key: keyof Adjustments; value: Adjustments[keyof Adjustments] }
  | { type: "adjust/reset" }
  | { type: "develop/start"; mode: DevelopMode; captureId: CaptureId }
  | { type: "develop/ok"; mode: DevelopMode; result: DevelopedResult }
  | { type: "develop/fail"; message: string }
  | { type: "merge/toggleFrame"; groupId: number; captureId: CaptureId }
  | { type: "merge/resetFrames"; groupId: number }
  | { type: "merge/start"; mode: DevelopMode; groupId: number }
  | { type: "merge/ok"; mode: DevelopMode; groupId: number; result: MergedResult }
  | { type: "merge/fail"; message: string }
  | { type: "comparison/view"; view: ComparisonView }
  | { type: "export/batchName"; value: string }
  | { type: "export/variant"; captureId: CaptureId; variant: ExportVariantChoice }
  | { type: "export/start" }
  | { type: "export/ok"; result: ExportResponse }
  | { type: "export/fail"; message: string }
  | { type: "import/start" }
  | { type: "import/ok"; result: ImportToPhotosResponse }
  | { type: "import/fail"; message: string }
  | { type: "viewer/open"; source: ViewerSource }
  | { type: "viewer/close" }
  | { type: "notice/set"; message: string; tone?: "info" | "error" }
  | { type: "notice/clear" };

function presetFor(health: HealthResponse | null, name: PresetName): PresetInfo | null {
  return health?.presets.find((preset) => preset.name === name) ?? null;
}

/**
 * 推薦理由に対応するプリセットを選ぶ（契約 §3 の `reason_codes` / §5）。
 *
 * 以前は RAW 推薦の**強さ**だけを見ていたため、解析が「暗部の黒つぶれが 35.7%」と
 * 言っている写真にも夜景プリセットが初期選択される、というちぐはぐが起きていた。
 * 逆光は専用の検出コードを持たないので、**白飛びと黒つぶれが同時に立つこと**を
 * そのシグナルとして扱う（明暗差が極端＝逆光の典型）。
 */
function presetForReasons(group: GroupInfo | undefined): PresetName {
  const codes = group?.raw.reason_codes ?? [];
  const blownHighlights = codes.includes("highlight_clipping");
  const crushedShadows = codes.includes("deep_shadows");

  if (blownHighlights && crushedShadows) return "backlit_rescue";
  if (crushedShadows) return "shadow_rescue";
  if (blownHighlights) return "highlight_rescue";
  if (codes.includes("night_scene") || codes.includes("high_iso")) return "nightscape";
  if (group?.raw.level === "none") return "landscape_clear";
  return DEFAULT_PRESET;
}

/** 初期プリセットを決める。存在しない名前なら既定にフォールバックする。 */
function suggestPreset(health: HealthResponse | null, group: GroupInfo | undefined): PresetName {
  const wanted = presetForReasons(group);
  if (!health) return wanted;
  if (presetFor(health, wanted)) return wanted;
  return presetFor(health, DEFAULT_PRESET) ? DEFAULT_PRESET : (health.presets[0]?.name ?? DEFAULT_PRESET);
}

export function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "health/ok": {
      const group = state.analysis?.groups.find((item) => item.id === state.selectedGroupId);
      return {
        ...state,
        health: action.health,
        engineStatus: "ready",
        presetName: presetFor(action.health, state.presetName)
          ? state.presetName
          : suggestPreset(action.health, group),
      };
    }

    case "health/fail":
      return { ...state, engineStatus: "offline" };

    case "analysis/status": {
      const response = action.response;
      if (response.status === "ready") {
        return {
          ...state,
          analysisState: "ready",
          analysis: response.analysis,
          analysisProgress: null,
          analysisError: "",
          analyzeRequested: false,
        };
      }
      if (response.status === "running") {
        return {
          ...state,
          analysisState: "running",
          analysisProgress: response.progress,
          analysisError: "",
        };
      }
      return { ...state, analysisState: "absent", analysisProgress: null, analysisError: "" };
    }

    case "analysis/requested":
      return { ...state, analyzeRequested: true, analysisState: "running", analysisError: "" };

    case "analysis/error":
      return { ...state, analysisState: "error", analysisError: action.message, analyzeRequested: false };

    case "source/loaded":
      return {
        ...state,
        sourceFolder: action.info,
        sourceFolderUnavailable: false,
        sourceFolderBusy: false,
        // 保存済みのフォルダが後から読めなくなる（外部ボリュームの取り外し、macOS の
        // 許可取り消し）ことがある。サイドバーの短い注記だけでは対処が分からないため、
        // 起動時に気づいた時点で対処方法もトーストで出す。
        notice: action.info.exists && !action.info.readable ? SOURCE_FOLDER_PERMISSION_HELP : state.notice,
        noticeTone: action.info.exists && !action.info.readable ? "error" : state.noticeTone,
      };

    case "source/unavailable":
      return { ...state, sourceFolderUnavailable: true, sourceFolderBusy: false };

    case "source/applying":
      return { ...state, sourceFolderBusy: true };

    case "source/changed": {
      // フォルダが変わると capture_id の指す実体が変わる（前のフォルダの DSC02943 は
      // 新しいフォルダでは別の写真か、存在しない）。選択・現像結果・書き出し選択を
      // まとめて捨て、エンジン接続情報と選択中プリセットだけを引き継ぐ。
      // 解析のやり直しは呼び出し側が beginAnalyze(true) で行う。
      return {
        ...initialState,
        health: state.health,
        engineStatus: state.engineStatus,
        presetName: state.presetName,
        sourceFolder: action.info,
        notice: action.message,
        noticeTone: action.tone ?? "info",
      };
    }

    case "source/failed":
      return { ...state, sourceFolderBusy: false, notice: action.message, noticeTone: "error" };

    case "screen/suggestions":
      return { ...state, screen: "suggestions" };

    case "screen/export":
      return { ...state, screen: "export", exportResult: null, importResult: null };

    case "screen/operation": {
      // グループ切替とプリセット/スライダーの初期化は 1 アクションで行う（effect 内での setState を避ける）。
      const group = state.analysis?.groups.find((item) => item.id === action.groupId);
      return {
        ...state,
        screen: "operation",
        selectedGroupId: action.groupId,
        presetName: suggestPreset(state.health, group),
        overrides: {},
        // 比較エリアはまず単写の現像側に戻す（前のグループの合成を見ていた状態を持ち越さない）。
        comparisonView: "develop",
      };
    }

    case "list/view":
      return { ...state, listView: action.view };

    case "group/chooseCapture":
      return {
        ...state,
        selectedCaptures: { ...state.selectedCaptures, [action.groupId]: action.captureId },
      };

    case "group/accept": {
      const already = state.acceptedGroupIds.includes(action.groupId);
      return {
        ...state,
        acceptedGroupIds: already
          ? state.acceptedGroupIds.filter((id) => id !== action.groupId)
          : [...state.acceptedGroupIds, action.groupId],
      };
    }

    case "preset/change":
      return { ...state, presetName: action.presetName, overrides: {} };

    case "adjust/change":
      return { ...state, overrides: { ...state.overrides, [action.key]: action.value } };

    case "adjust/reset":
      return { ...state, overrides: {} };

    case "develop/start":
      return {
        ...state,
        developMode: action.mode,
        developingCapture: action.captureId,
        developStartedAt: Date.now(),
        notice: `${action.captureId}.ARW を${action.mode === "preview" ? "プレビュー" : "本"}現像しています…`,
        noticeTone: "info",
      };

    case "develop/ok": {
      const { capture_id: captureId } = action.result;
      const elapsed = action.result.cached ? "保存済み結果" : `${action.result.elapsed_seconds.toFixed(1)}秒`;
      const modeLabel = action.mode === "preview" ? "プレビュー" : "本";
      // プレビュー（outputs/preview/）と本現像（outputs/developed/）は別枠で保持する。
      // 書き出しは本現像の filename しか受け付けないため、混ぜてはならない。
      return {
        ...state,
        previewResults:
          action.mode === "preview"
            ? { ...state.previewResults, [captureId]: action.result }
            : state.previewResults,
        fullResults:
          action.mode === "full" ? { ...state.fullResults, [captureId]: action.result } : state.fullResults,
        developMode: null,
        developingCapture: null,
        // 直前に合成を見ていても、現像を実行したらその結果へ切り替える。
        comparisonView: "develop",
        notice: `${captureId} の${modeLabel}現像が完了しました（${elapsed}）`,
        noticeTone: "info",
      };
    }

    case "develop/fail":
      return { ...state, developMode: null, developingCapture: null, notice: action.message, noticeTone: "error" };

    case "merge/toggleFrame": {
      const group = state.analysis?.groups.find((item) => item.id === action.groupId);
      if (!group) return state;
      const current = new Set(mergeFramesOf(state, group));
      if (current.has(action.captureId)) current.delete(action.captureId);
      else current.add(action.captureId);
      // 撮影順（group.capture_ids の並び）を保ったまま作り直す。チェックした順ではない。
      const next = group.capture_ids.filter((id) => current.has(id));
      return { ...state, mergeSelections: { ...state.mergeSelections, [action.groupId]: next } };
    }

    case "merge/resetFrames": {
      const group = state.analysis?.groups.find((item) => item.id === action.groupId);
      if (!group) return state;
      return {
        ...state,
        mergeSelections: { ...state.mergeSelections, [action.groupId]: defaultMergeFrames(group) },
      };
    }

    case "merge/start":
      return {
        ...state,
        mergeMode: action.mode,
        mergingGroupId: action.groupId,
        mergeStartedAt: Date.now(),
        notice: `ブラケットを${action.mode === "preview" ? "プレビュー" : "本"}合成しています…`,
        noticeTone: "info",
      };

    case "merge/ok": {
      const { result } = action;
      const elapsed = result.cached ? "保存済み結果" : `${result.elapsed_seconds.toFixed(1)}秒`;
      const modeLabel = action.mode === "preview" ? "プレビュー" : "本";
      // プレビューと本合成は別枠。書き出しは本合成の filename しか受け付けないため混ぜない。
      return {
        ...state,
        mergePreviewResults:
          action.mode === "preview"
            ? { ...state.mergePreviewResults, [action.groupId]: result }
            : state.mergePreviewResults,
        mergeFullResults:
          action.mode === "full"
            ? { ...state.mergeFullResults, [action.groupId]: result }
            : state.mergeFullResults,
        mergeMode: null,
        mergingGroupId: null,
        comparisonView: "merged",
        notice: `${result.frames}枚の${modeLabel}合成が完了しました（${elapsed}${result.aligned ? "・位置合わせあり" : ""}）`,
        noticeTone: "info",
      };
    }

    case "merge/fail":
      return { ...state, mergeMode: null, mergingGroupId: null, notice: action.message, noticeTone: "error" };

    case "comparison/view":
      return { ...state, comparisonView: action.view };

    case "export/batchName":
      return { ...state, batchName: action.value, exportResult: null, importResult: null };

    case "export/variant":
      return { ...state, exportVariants: { ...state.exportVariants, [action.captureId]: action.variant } };

    case "export/start":
      return { ...state, exporting: true, exportResult: null, importResult: null };

    case "export/ok":
      return {
        ...state,
        exporting: false,
        exportResult: action.result,
        notice: `${action.result.files.length}件を書き出しました`,
        noticeTone: "info",
      };

    case "export/fail":
      return { ...state, exporting: false, notice: action.message, noticeTone: "error" };

    case "import/start":
      return { ...state, importing: true, importResult: null };

    case "import/ok":
      return {
        ...state,
        importing: false,
        importResult: action.result,
        notice:
          action.result.status === "ok"
            ? `Appleフォトへ ${action.result.imported} 件を登録しました（${action.result.album}）`
            : action.result.error,
        noticeTone: action.result.status === "ok" ? "info" : "error",
      };

    case "import/fail":
      return { ...state, importing: false, notice: action.message, noticeTone: "error" };

    case "viewer/open":
      return { ...state, viewer: action.source };

    case "viewer/close":
      return { ...state, viewer: null };

    case "notice/set":
      return { ...state, notice: action.message, noticeTone: action.tone ?? "info" };

    case "notice/clear":
      return { ...state, notice: "" };

    default:
      return state;
  }
}

// ---------------------------------------------------------------------------
// 派生値
// ---------------------------------------------------------------------------

export function selectedCaptureOf(state: State, group: GroupInfo): CaptureId {
  return state.selectedCaptures[group.id] ?? group.recommended_capture_id;
}

export function photoOf(analysis: Analysis | null, captureId: CaptureId | undefined): PhotoInfo | null {
  if (!analysis || !captureId) return null;
  return analysis.photos[captureId] ?? null;
}

export function activePreset(state: State): PresetInfo | null {
  return presetFor(state.health, state.presetName);
}

/** プリセット既定値 + ユーザー変更分。プリセット未取得なら null。 */
export function effectiveAdjustments(state: State): Adjustments | null {
  const preset = activePreset(state);
  if (!preset) return null;
  return { ...preset.adjustments, ...state.overrides };
}

export function changedAdjustmentKeys(state: State): (keyof Adjustments)[] {
  const preset = activePreset(state);
  if (!preset) return [];
  return (Object.keys(state.overrides) as (keyof Adjustments)[]).filter(
    (key) => state.overrides[key] !== preset.adjustments[key],
  );
}

/** 現像済み（本現像）または本合成の結果を持つグループID。 */
export function developedGroupIds(state: State): number[] {
  const groups = state.analysis?.groups ?? [];
  return groups
    .filter(
      (group) =>
        group.capture_ids.some((captureId) => state.fullResults[captureId]) ||
        Boolean(state.mergeFullResults[group.id]),
    )
    .map((group) => group.id);
}

// ---------------------------------------------------------------------------
// ブラケット合成
// ---------------------------------------------------------------------------

/**
 * 合成の導線を出してよいグループか。
 * グループ種別が bracket か、RAW 推薦の理由に exposure_bracket が入っていれば対象。
 * （解析は「露出を変えて撮影されています」を理由コード側にも立てるため、両方を見る）
 */
export function isBracketGroup(group: GroupInfo): boolean {
  return group.kind === "bracket" || group.raw.reason_codes.includes("exposure_bracket");
}

/**
 * 既定の合成対象フレーム。9枚以下なら全部、10枚以上なら先頭9枚。
 *
 * 実データには 5枚AEB × 4セットが 1 グループ（20枚）にまとまった例があり、全部を
 * 混ぜると別シーンが合成されてしまう。既定を上限で切っておけば「押した瞬間に必ず
 * 400」にはならないが、**先頭9枚もシーンをまたぐ**ため、UI 側で選び直しを促すこと。
 */
export function defaultMergeFrames(group: GroupInfo): CaptureId[] {
  return group.capture_ids.slice(0, MERGE_MAX_FRAMES);
}

/** 現在の合成対象フレーム。ユーザーが触っていなければ既定値。 */
export function mergeFramesOf(state: State, group: GroupInfo): CaptureId[] {
  return state.mergeSelections[group.id] ?? defaultMergeFrames(group);
}

/**
 * 合成を実行できない理由。実行できるなら null。
 * サーバは 2〜9 枚の外を HTTP 400 で弾くので、UI 側でも同じ条件でボタンを止める。
 */
export function mergeBlockReason(frames: CaptureId[]): string | null {
  if (frames.length < MERGE_MIN_FRAMES) {
    return `合成には${MERGE_MIN_FRAMES}枚以上の選択が必要です（選択中 ${frames.length}枚）。`;
  }
  if (frames.length > MERGE_MAX_FRAMES) {
    return `一度に合成できるのは${MERGE_MAX_FRAMES}枚までです（選択中 ${frames.length}枚）。減らしてください。`;
  }
  return null;
}

export function visibleGroups(state: State): GroupInfo[] {
  const groups = state.analysis?.groups ?? [];
  if (state.listView === "accepted") return groups.filter((group) => state.acceptedGroupIds.includes(group.id));
  if (state.listView === "queue") {
    const ids = new Set(developedGroupIds(state));
    return groups.filter((group) => ids.has(group.id));
  }
  return groups;
}

export function selectedGroup(state: State): GroupInfo | null {
  if (state.selectedGroupId === null) return null;
  return state.analysis?.groups.find((group) => group.id === state.selectedGroupId) ?? null;
}

export const BATCH_NAME_PATTERN = /^[A-Za-z0-9_-]+$/;

export function isValidBatchName(value: string): boolean {
  return BATCH_NAME_PATTERN.test(value);
}
