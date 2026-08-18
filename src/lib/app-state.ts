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
export type ExportVariantChoice = "camera_jpg" | "developed";

const DEFAULT_PRESET: PresetName = "neutral_recovery";

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
  batchName: string;
  exportVariants: Record<CaptureId, ExportVariantChoice>;
  exporting: boolean;
  exportResult: ExportResponse | null;
  importing: boolean;
  importResult: ImportToPhotosResponse | null;
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
  batchName: "",
  exportVariants: {},
  exporting: false,
  exportResult: null,
  importing: false,
  importResult: null,
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
  | { type: "export/batchName"; value: string }
  | { type: "export/variant"; captureId: CaptureId; variant: ExportVariantChoice }
  | { type: "export/start" }
  | { type: "export/ok"; result: ExportResponse }
  | { type: "export/fail"; message: string }
  | { type: "import/start" }
  | { type: "import/ok"; result: ImportToPhotosResponse }
  | { type: "import/fail"; message: string }
  | { type: "notice/set"; message: string; tone?: "info" | "error" }
  | { type: "notice/clear" };

function presetFor(health: HealthResponse | null, name: PresetName): PresetInfo | null {
  return health?.presets.find((preset) => preset.name === name) ?? null;
}

/** RAW推薦の強さから初期プリセットを選ぶ。存在しない名前なら既定にフォールバックする。 */
function suggestPreset(health: HealthResponse | null, group: GroupInfo | undefined): PresetName {
  const wanted =
    group?.raw.level === "strong"
      ? "nightscape"
      : group?.raw.level === "none"
        ? "landscape_clear"
        : DEFAULT_PRESET;
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
        notice: `${captureId} の${modeLabel}現像が完了しました（${elapsed}）`,
        noticeTone: "info",
      };
    }

    case "develop/fail":
      return { ...state, developMode: null, developingCapture: null, notice: action.message, noticeTone: "error" };

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

/** 現像済み（本現像）の結果を持つグループID。 */
export function developedGroupIds(state: State): number[] {
  const groups = state.analysis?.groups ?? [];
  return groups
    .filter((group) => group.capture_ids.some((captureId) => state.fullResults[captureId]))
    .map((group) => group.id);
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
