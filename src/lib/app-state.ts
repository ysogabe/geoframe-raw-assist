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

/**
 * 1 枚の写真を書き出すときの形式。合成は「写真の別形式」ではなく別種の採用なので、
 * この union には入れない（AcceptedMerged 側が合成であること自体を表す）。
 */
export type PhotoExportVariant = "camera_jpg" | "developed";

/**
 * 採用した 1 枚の写真。
 *
 * 採用の同一性は `captureId` にある。表示カーソル（`viewing`）とは独立に固定するので、
 * 「採用したあとに代表を変えると書き出される写真が無言で変わる」経路が消える。
 * 一方で **成果物（filename）はスナップショットしない**。本現像をやり直したら新しい
 * 結果を書き出したいし、`outputs/` には params_hash 違いが全部残るため、古い filename を
 * 握ったままでも書き出しは成功してしまう（失敗しないぶん、UI 表示との食い違いに気づけない）。
 */
export type AcceptedPhoto = {
  kind: "photo";
  /** 表示・絞り込み用。再解析でIDが振り直されたら reconcile が書き換える。 */
  groupId: number;
  captureId: CaptureId;
  variant: PhotoExportVariant;
};

/** 採用した 1 件のブラケット合成。 */
export type AcceptedMerged = {
  kind: "merged";
  groupId: number;
  /**
   * 合成の先頭フレーム（= MergedResult.capture_ids[0] = ファイル名の先頭ID）。
   * POST /api/export の capture_id はこれでなければサーバが 400 を返す。
   * 採用した瞬間に確定させ、表示カーソルの変化から完全に切り離す。
   */
  leadCaptureId: CaptureId;
};

export type AcceptedItem = AcceptedPhoto | AcceptedMerged;

/**
 * 採用アイテムの一意キー（React の key と、行単位の操作の宛先に使う）。
 *
 * 合成のキーに **先頭フレームまで混ぜている** のは、グループIDだけだと衝突するから。
 * 再解析の再結び付け（reconcileAccepted）は仕様どおり重複除去をしないので、別々の
 * グループで採用した 2 件の合成が再解析で 1 グループへ再結合すると `merged:12` が
 * 2 つ並び、React の duplicate key になる。先頭フレームは合成ごとに必ず違うため、
 * `merged:12:DSC02943` まで含めれば衝突しない。
 *
 * `accept/merged` のトグルは groupId を直接見ていてこのキー文字列を使わないので、
 * ここを変えても「グループにつき合成は 1 件」という採用の意味は変わらない。
 */
export function acceptedKey(item: AcceptedItem): string {
  switch (item.kind) {
    case "photo":
      return `photo:${item.captureId}`;
    case "merged":
      return `merged:${item.groupId}:${item.leadCaptureId}`;
  }
}

/**
 * グループ内で「今どの面を見ているか」。
 *
 * 以前は写真カーソル（グループ単位）と比較タブ（**グローバル**）に割れていたため、
 * グループを跨ぐたびに比較タブだけがリセットされていた。1 本にまとめてグループ単位で
 * 持てば、戻ってきたときに見ていた面がそのまま残る。
 *
 * 合成を見ている間は「今の写真」も合成の先頭フレームになる（viewingCaptureOf）。
 * カーソルが 1 本である以上、左ペインだけ別の写真を指し続けることはない。
 */
export type ViewingItem = { kind: "photo"; captureId: CaptureId } | { kind: "merged" };

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
  /** グループごとの表示カーソル。未設定なら recommended_capture_id を見ている。 */
  viewing: Record<number, ViewingItem>;
  /**
   * 採用したもの。写真と合成を **1 本の配列** に採用順で混ぜて持つ。
   * 別配列に分けると書き出し順を毎回決め直すことになるため。
   */
  accepted: AcceptedItem[];
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
  batchName: string;
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
  viewing: {},
  accepted: [],
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
  batchName: "",
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
  /** 表示カーソルを 1 枚の写真へ（フィルムストリップ / 比較タブの「単写の現像」）。 */
  | { type: "view/capture"; groupId: number; captureId: CaptureId }
  /** 表示カーソルを合成へ（比較タブの「ブラケット合成」）。 */
  | { type: "view/merged"; groupId: number }
  /** 写真の採用トグル。採用時点の variant は呼び出し側が決める（成果物の有無で既定が変わる）。 */
  | { type: "accept/photo"; groupId: number; captureId: CaptureId; variant: PhotoExportVariant }
  /** 合成の採用トグル。本合成の結果があるときだけ呼ばれる。 */
  | { type: "accept/merged"; groupId: number; leadCaptureId: CaptureId }
  /** 書き出し形式の変更。宛先は acceptedKey（capture でもグループでもない）。 */
  | { type: "accept/variant"; key: string; variant: PhotoExportVariant }
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
  | { type: "export/batchName"; value: string }
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

/** capture_id → その写真が属するグループID。再解析後の結び直しに使う索引。 */
function groupIdByCapture(analysis: Analysis): Map<CaptureId, number> {
  const index = new Map<CaptureId, number>();
  for (const group of analysis.groups) {
    for (const captureId of group.capture_ids) index.set(captureId, group.id);
  }
  return index;
}

/**
 * 再解析でグループIDが振り直されても採用を保つ（再結び付け）。
 *
 * 採用の同一性は写真なら capture_id、合成なら先頭フレームにある。同じフォルダの
 * 再解析ならこれらは必ず生き残るので、そこから新しいグループを引き直すだけでよい。
 *
 * 気をつけるところが 3 つある（いずれも型検査では捕まらない）:
 * 1. 採用した写真が新しい解析から消えている → そのアイテムを落とす（引けないので）
 * 2. 採用した合成の先頭フレームが別のグループへ移った → groupId を書き換えて残す
 * 3. 別々のグループから採用した 2 枚が同じグループに再結合する → **重複除去しない**
 *    （同一グループから複数枚採用するのは正当な状態であり、片方を消す理由がない）
 *
 * なお合成の成果物（MergedResult）は groupId ではなく先頭フレームで引き直すので
 * （mergedResultFor）、ここでグループ単位の合成結果を詰め替える必要はない。
 */
function reconcileAccepted(
  accepted: AcceptedItem[],
  analysis: Analysis,
): { accepted: AcceptedItem[]; dropped: number } {
  const index = groupIdByCapture(analysis);
  const next: AcceptedItem[] = [];

  for (const item of accepted) {
    switch (item.kind) {
      case "photo": {
        const groupId = index.get(item.captureId);
        // その写真自体が新しい解析に無い（フォルダから消えた等）。追跡できないので落とす。
        if (groupId === undefined) break;
        next.push(groupId === item.groupId ? item : { ...item, groupId });
        break;
      }
      case "merged": {
        const groupId = index.get(item.leadCaptureId);
        if (groupId === undefined) break;
        next.push(groupId === item.groupId ? item : { ...item, groupId });
        break;
      }
    }
  }

  return { accepted: next, dropped: accepted.length - next.length };
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
        const analysis = response.analysis;
        // 解析は 1 秒ポーリングで何度も同じ結果を返す。作り直しが起きたとき
        // （generated_at が変わったとき）だけ採用を結び直す。毎回やると、同じ
        // 内容の再取得でも配列の同一性が失われて useMemo が無駄に走る。
        const regenerated = state.analysis !== null && state.analysis.generated_at !== analysis.generated_at;
        const { accepted, dropped } = regenerated
          ? reconcileAccepted(state.accepted, analysis)
          : { accepted: state.accepted, dropped: 0 };
        return {
          ...state,
          analysisState: "ready",
          analysis,
          analysisProgress: null,
          analysisError: "",
          analyzeRequested: false,
          accepted,
          // 何件失われたかは伝える。黙って減っていると「書き出したら足りない」まで気づけない。
          notice: dropped > 0 ? `再解析で見つからなくなった${dropped}件の採用を解除しました。` : state.notice,
          noticeTone: dropped > 0 ? "error" : state.noticeTone,
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
        // 表示カーソルはグループ単位なのでここでは触らない。前に見ていた面のまま戻る。
      };
    }

    case "list/view":
      return { ...state, listView: action.view };

    case "view/capture":
      return {
        ...state,
        viewing: { ...state.viewing, [action.groupId]: { kind: "photo", captureId: action.captureId } },
      };

    case "view/merged":
      return { ...state, viewing: { ...state.viewing, [action.groupId]: { kind: "merged" } } };

    case "accept/photo": {
      const already = state.accepted.some(
        (item) => item.kind === "photo" && item.captureId === action.captureId,
      );
      return {
        ...state,
        accepted: already
          ? state.accepted.filter((item) => !(item.kind === "photo" && item.captureId === action.captureId))
          : [
              ...state.accepted,
              {
                kind: "photo",
                groupId: action.groupId,
                captureId: action.captureId,
                variant: action.variant,
              },
            ],
      };
    }

    case "accept/merged": {
      // 合成の採用はグループにつき 1 件。ここは acceptedKey を経由せず groupId を
      // 直接見る（キーは先頭フレームも含むが、採用の同一性はグループにある）。
      const already = state.accepted.some((item) => item.kind === "merged" && item.groupId === action.groupId);
      return {
        ...state,
        accepted: already
          ? state.accepted.filter((item) => !(item.kind === "merged" && item.groupId === action.groupId))
          : [
              ...state.accepted,
              { kind: "merged", groupId: action.groupId, leadCaptureId: action.leadCaptureId },
            ],
      };
    }

    case "accept/variant":
      return {
        ...state,
        accepted: state.accepted.map((item) =>
          item.kind === "photo" && acceptedKey(item) === action.key ? { ...item, variant: action.variant } : item,
        ),
      };

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
      // 直前に合成を見ていても、現像を実行したらその写真の面へ切り替える。
      // どのグループの話かは結果の capture_id から引く（アクションは持っていない）。
      const developedGroup = state.analysis?.groups.find((item) => item.capture_ids.includes(captureId));
      // プレビュー（outputs/preview/）と本現像（outputs/developed/）は別枠で保持する。
      // 書き出しは本現像の filename しか受け付けないため、混ぜてはならない。
      return {
        ...state,
        viewing: developedGroup
          ? { ...state.viewing, [developedGroup.id]: { kind: "photo", captureId } }
          : state.viewing,
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
        // 合成したらそのグループの表示は合成側へ。他グループのカーソルは触らない。
        viewing: { ...state.viewing, [action.groupId]: { kind: "merged" } },
        notice: `${result.frames}枚の${modeLabel}合成が完了しました（${elapsed}${result.aligned ? "・位置合わせあり" : ""}）`,
        noticeTone: "info",
      };
    }

    case "merge/fail":
      return { ...state, mergeMode: null, mergingGroupId: null, notice: action.message, noticeTone: "error" };

    case "export/batchName":
      return { ...state, batchName: action.value, exportResult: null, importResult: null };

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

/**
 * グループ内で今見ている面。未設定なら推薦カットの写真。
 *
 * 再解析でグループの中身が入れ替わると、保存済みのカーソルがそのグループに属さない
 * 写真を指すことがある。ここで弾いておけば、カーソル側を詰め替えなくても左ペインが
 * 別グループの写真を指し続けることはない。
 */
export function viewingOf(state: State, group: GroupInfo): ViewingItem {
  const current = state.viewing[group.id];
  if (!current) return { kind: "photo", captureId: group.recommended_capture_id };
  if (current.kind === "photo" && !group.capture_ids.includes(current.captureId)) {
    return { kind: "photo", captureId: group.recommended_capture_id };
  }
  return current;
}

/**
 * 今見ている写真。合成を見ている間は合成の先頭フレームを返す。
 * 表示カーソルは 1 本なので、合成を見ているときは左ペインもその先頭フレームになる。
 */
export function viewingCaptureOf(state: State, group: GroupInfo): CaptureId {
  const current = viewingOf(state, group);
  if (current.kind === "photo") return current.captureId;
  // 合成の状態は **グループID をキー** に持っており、再解析では詰め替えていない
  // （キーと構造を変えない約束のため）。IDが使い回されると、別のグループの合成が
  // そのまま残っていることがある。ここで返した撮影IDは「この写真を採用」の宛先に
  // なるので、グループに属さないIDを返すとそのグループの採用として別の写真が
  // 記録されてしまう。必ずグループ内に絞ってから返す。
  const merged = state.mergeFullResults[group.id] ?? state.mergePreviewResults[group.id];
  const lead = [merged?.capture_ids[0], mergeFramesOf(state, group)[0]].find(
    (captureId) => captureId !== undefined && group.capture_ids.includes(captureId),
  );
  return lead ?? group.recommended_capture_id;
}

/**
 * そのグループの合成結果。**グループに属さない合成は無いものとして扱う。**
 *
 * 合成の状態はグループIDをキーに持っており再解析で詰め替えていないため、IDが
 * 使い回されると別グループの合成が残る。採用や表示の宛先にそのまま使うと、
 * 見ていたグループと無関係な写真が採用・書き出しされる（サーバはファイル名と
 * capture_id が一致するので 200 を返してしまい、気づけない）。
 *
 * 判定は「使ったフレームが **すべて** グループ内にあること」。先頭だけを見ると、
 * フレーム構成が変わった中途半端な古い結果を通してしまう。
 */
export function groupMergeResultOf(
  results: Record<number, MergedResult>,
  group: GroupInfo,
): MergedResult | undefined {
  const merged = results[group.id];
  if (!merged) return undefined;
  return merged.capture_ids.every((captureId) => group.capture_ids.includes(captureId)) ? merged : undefined;
}

/**
 * 採用した合成の成果物を引き直す。
 *
 * groupId ではなく **先頭フレーム** で引くのが要点。再解析でグループIDが振り直されても
 * 追従できるし、合成をやり直せば（同じ先頭フレームなら）新しい成果物を自動で拾う。
 * 逆に先頭フレームが変わる合成し直しは別物の合成なので、拾わないのが正しい。
 */
export function mergedResultFor(
  mergeFullResults: State["mergeFullResults"],
  leadCaptureId: CaptureId,
): MergedResult | undefined {
  return Object.values(mergeFullResults).find((result) => result.capture_ids[0] === leadCaptureId);
}

/** 採用アイテムを 1 件でも持つグループID。 */
export function acceptedGroupIdSet(state: State): Set<number> {
  return new Set(state.accepted.map((item) => item.groupId));
}

/**
 * そのグループの採用の内訳ラベル（例: `["DSC02943", "合成3枚"]`）。
 *
 * 件数も一覧カードの 1 行もこれ 1 本から作る。件数だけ別に数えると、バッジは「2件」
 * なのに内訳が 1 つしか出ない、という食い違いが生まれる余地が残るため。
 * 配列の長さの総和は state.accepted.length と一致する（= タブと Topbar の件数）。
 */
export function acceptedSummaryOf(state: State, groupId: number): string[] {
  return state.accepted
    .filter((item) => item.groupId === groupId)
    .map((item) => {
      if (item.kind === "photo") return item.captureId;
      // 合成の枚数は成果物から引く。採用時に控えても、合成し直したら古い枚数が残る。
      const merged = mergedResultFor(state.mergeFullResults, item.leadCaptureId);
      return merged ? `合成${merged.frames}枚` : "合成";
    });
}

/**
 * 一覧カードの代表として出す撮影ID。
 *
 * 採用があれば **最初に採用したアイテム**、無ければ表示カーソル。採用したものが
 * 一覧に出ていないと「採用したはずのカットが見当たらない」ように見えるため、
 * 採用は表示カーソルより優先する。
 */
export function heroCaptureOf(state: State, group: GroupInfo): CaptureId {
  const first = state.accepted.find((item) => item.groupId === group.id);
  if (first) return first.kind === "photo" ? first.captureId : first.leadCaptureId;
  return viewingCaptureOf(state, group);
}

export function isPhotoAccepted(state: State, captureId: CaptureId): boolean {
  return state.accepted.some((item) => item.kind === "photo" && item.captureId === captureId);
}

export function isMergedAccepted(state: State, groupId: number): boolean {
  return state.accepted.some((item) => item.kind === "merged" && item.groupId === groupId);
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
  if (state.listView === "accepted") {
    const accepted = acceptedGroupIdSet(state);
    return groups.filter((group) => accepted.has(group.id));
  }
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
