import { SOURCE_FOLDER_PERMISSION_HELP } from "./format";
import type {
  Adjustments,
  Analysis,
  AnalysisProgress,
  AnalysisResponse,
  CaptureId,
  DevelopmentCandidate,
  DevelopmentCandidatesResponse,
  DevelopedResult,
  ExportResponse,
  GroupInfo,
  ImportToPhotosResponse,
  MergedResult,
  PhotoInfo,
  PresetInfo,
  PresetName,
  SourceFolderInfo,
} from "./types";

export type Screen = "suggestions" | "operation" | "export";
export type EngineStatus = "checking" | "ready" | "offline";
/**
 * `unconfigured` = 現像元フォルダが未設定（Tauri の get_analysis が SOURCE_ROOT_NOT_CONFIGURED で reject。#110）。
 * 解析を始める前にフォルダ選択が要る状態で、`absent`（設定済みだが未解析）とは別に扱う。
 */
export type AnalysisState = "unknown" | "unconfigured" | "absent" | "running" | "ready" | "error";
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
   * export_selection の capture_id はこれでなければサーバが 400 を返す。
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
 * `accept/merged` の **採用を外す側** と isMergedAccepted も同じ 2 つ組（groupId +
 * 先頭フレーム）で同一性を見る。キーの粒度と採用の同一性が食い違っていると、再結合した
 * グループでチップを 1 回押しただけで 2 件とも消える。ここを変えるなら両方あわせて変えること。
 *
 * **採用する側だけは groupId 単位で置き換える**（1 グループにつき合成の採用は 1 件）。
 * 同じグループを合成し直すとリードが変わり、古い採用が成果物を引けないまま件数だけ
 * 残る（＝取り除けない幽霊になる）ため。詳細は `accept/merged` のコメント。
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
  /** この 2 面が属する写真。送り位置の特定に使う。 */
  captureId: CaptureId;
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
  /**
   * プレビュー結果の長辺（px）。ヘッダの「プレビュー品質（長辺◯px）」に使う。
   * プレビューと本現像で長辺が違うため、固定値ではなく結果の寸法から渡す。
   */
  previewEdge?: number;
  /** ヘッダに出す見出し（撮影ID など）。 */
  title: string;
};

/**
 * 原寸ビューアで前後に送れる写真の輪。
 *
 * 送り先をコールバックで引かせず、**開いた時点のスナップショットを配列で渡す**。
 * ビューアは自分の中の添字だけを動かせばよく、グループ選択（selectedGroupId）や
 * 表示カーソル（viewing）には一切触れない ＝ 閉じたときに元の操作パネルがそのまま残る。
 *
 * スナップショットである以上、ビューアを開いている間に現像が終わってもその結果は
 * 反映されない（開き直せば入る）。同期機構を足すと「見ている面が勝手に差し替わる」
 * 方が害が大きいので、これは仕様として許容する。
 */
export type ViewerGallery = {
  items: ViewerSource[];
  /** items の中で最初に見せる位置。 */
  index: number;
};

const DEFAULT_PRESET: PresetName = "neutral_recovery";

/**
 * ブラケット合成に渡せるフレーム数（M2 まで隠している）。
 * 範囲外はサーバが HTTP 400 を返すため、UI 側も同じ範囲で実行を止める。
 */
export const MERGE_MIN_FRAMES = 2;
export const MERGE_MAX_FRAMES = 9;

export type State = {
  screen: Screen;
  engineStatus: EngineStatus;
  /** engine_info（#109）で得たエンジン表示用の文字列。未取得なら null。 */
  engineLabel: string | null;
  /** プリセット一覧（#98）。Rust の get_presets から埋まる。 */
  presets: PresetInfo[];
  analysisState: AnalysisState;
  analysisProgress: AnalysisProgress | null;
  analysis: Analysis | null;
  analysisError: string;
  analyzeRequested: boolean;
  /** 現像対象フォルダ。get_source_folder が唯一の正で、analysis.source より優先する。 */
  sourceFolder: SourceFolderInfo | null;
  /** set_source_folder の実行中。 */
  sourceFolderBusy: boolean;
  /**
   * 現像対象フォルダの世代。フォルダを切り替えるたびに 1 進む。
   *
   * 現像・合成は非同期で数十秒かかる。その最中にフォルダを切り替えると、`develop/ok` /
   * `merge/ok` が届いたときサーバはもう別のフォルダを向いているのに、**前のフォルダの
   * RAW から作った結果**が新しいフォルダの state へ書き込まれてしまう。capture_id は
   * カメラの連番でしかなくフォルダをまたいで衝突しうるため、書き込み先まで一致して
   * しまい、UI 上は正常な現像結果に見える（別の写真が出る、とは気づけない）。
   *
   * UI ロック（Sidebar の sourceFolderLocked）だけでは「切替とほぼ同時にレスポンスが
   * 返る」窓が残るので、リクエスト発行時の世代を結果のアクションに載せ、reducer 側で
   * 現在の世代と違うものを捨てる。
   */
  sourceFolderGeneration: number;
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
  /** RAW縮小デコード代理評価による候補。対象capture_idと世代を併記して古い結果を表示しない。 */
  developmentCandidates: DevelopmentCandidatesResponse | null;
  developmentCandidatesCaptureId: CaptureId | null;
  developmentCandidatesBusy: boolean;
  developmentCandidatesError: string;
  developmentCandidatesGeneration: number;
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
  /** 原寸ビューアで開いている写真の輪。null なら閉じている。 */
  viewer: ViewerGallery | null;
  notice: string;
  noticeTone: "info" | "error";
};

export const initialState: State = {
  screen: "suggestions",
  engineStatus: "checking",
  engineLabel: null,
  presets: [],
  analysisState: "unknown",
  analysisProgress: null,
  analysis: null,
  analysisError: "",
  analyzeRequested: false,
  sourceFolder: null,
  sourceFolderBusy: false,
  sourceFolderGeneration: 0,
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
  developmentCandidates: null,
  developmentCandidatesCaptureId: null,
  developmentCandidatesBusy: false,
  developmentCandidatesError: "",
  developmentCandidatesGeneration: 0,
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
  /** engine_info が成功した（#109）。engineStatus を ready にする唯一の経路。 */
  | { type: "engine/ok"; label: string }
  /** engine_info が失敗した（sidecar が見つからない等）。 */
  | { type: "engine/fail" }
  /** プリセット一覧の到着（#98）。Tauri 起動時の get_presets。 */
  | { type: "presets/loaded"; presets: PresetInfo[] }
  /**
   * `get_analysis` の応答。`generation` は**リクエストを出した時点**の sourceFolderGeneration。
   * フォルダ変更を跨いで遅れて届いた前のフォルダの応答（成功・未設定・失敗とも）を捨てるため必須。
   */
  | { type: "analysis/status"; response: AnalysisResponse; generation: number }
  | { type: "analysis/requested" }
  /** 現像元フォルダが未設定（SOURCE_ROOT_NOT_CONFIGURED）。`generation` は analysis/status と同じ。 */
  | { type: "analysis/unconfigured"; generation: number }
  | { type: "analysis/error"; message: string; generation: number }
  | { type: "source/loaded"; info: SourceFolderInfo }
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
  /** `generation` はリクエストを出した時点の sourceFolderGeneration（古い結果を捨てるため）。 */
  | { type: "develop/ok"; mode: DevelopMode; result: DevelopedResult; generation: number }
  /** 失敗には generation を持たせない（後述: 通知だけは必ず出す）。 */
  | { type: "develop/fail"; message: string }
  | { type: "candidates/start"; captureId: CaptureId; generation: number }
  | { type: "candidates/ok"; captureId: CaptureId; response: DevelopmentCandidatesResponse; generation: number }
  | { type: "candidates/fail"; captureId: CaptureId; message: string; generation: number }
  /** `captureId` は候補を表示していた写真（現在の写真と応答の capture_id を照合する）。 */
  | { type: "candidates/apply"; candidate: DevelopmentCandidate; captureId: CaptureId }
  | { type: "merge/toggleFrame"; groupId: number; captureId: CaptureId }
  | { type: "merge/resetFrames"; groupId: number }
  | { type: "merge/start"; mode: DevelopMode; groupId: number }
  /** `generation` はリクエストを出した時点の sourceFolderGeneration（古い結果を捨てるため）。 */
  | { type: "merge/ok"; mode: DevelopMode; groupId: number; result: MergedResult; generation: number }
  | { type: "merge/fail"; groupId: number; generation: number; message: string }
  | { type: "export/batchName"; value: string }
  | { type: "export/start" }
  /**
   * `missingCount` は「採用したのに成果物が引けず、送らなかった行」の数。
   * サーバ側の `result.skipped`（送ったが書き出せなかったもの）とは別物なので、
   * 通知の文言でも混ぜない。
   */
  | { type: "export/ok"; result: ExportResponse; missingCount?: number }
  | { type: "export/fail"; message: string }
  | { type: "import/start" }
  | { type: "import/ok"; result: ImportToPhotosResponse }
  | { type: "import/fail"; message: string }
  | { type: "viewer/open"; gallery: ViewerGallery }
  | { type: "viewer/close" }
  | { type: "notice/set"; message: string; tone?: "info" | "error" }
  | { type: "notice/clear" };

function presetFor(presets: PresetInfo[], name: PresetName): PresetInfo | null {
  return presets.find((preset) => preset.name === name) ?? null;
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
function suggestPreset(presets: PresetInfo[], group: GroupInfo | undefined): PresetName {
  const wanted = presetForReasons(group);
  // 一覧が未着なら推薦どおりの名前を仮置きし、届いた時点で applyPresets が検証し直す。
  if (presets.length === 0) return wanted;
  if (presetFor(presets, wanted)) return wanted;
  return presetFor(presets, DEFAULT_PRESET) ? DEFAULT_PRESET : (presets[0]?.name ?? DEFAULT_PRESET);
}

/**
 * プリセット一覧を取り込む（#98）。
 *
 * - 空の一覧は無視する（既に持っている一覧を空で潰さない）。
 * - 選択中のプリセットが新しい一覧にもあれば、presetName も overrides も触らない。
 *   無いときだけ推薦し直し、別プリセット向けの候補を捨てる。
 */
function applyPresets(state: State, presets: PresetInfo[]): State {
  if (presets.length === 0) return state;
  const group = state.analysis?.groups.find((item) => item.id === state.selectedGroupId);
  const presetName = presetFor(presets, state.presetName) ? state.presetName : suggestPreset(presets, group);
  return {
    ...state,
    presets,
    presetName,
    // プリセットが変わったら別プリセット向けの候補は捨てる（生成中の busy/captureId は触らない）。
    ...(presetName === state.presetName ? {} : clearedCandidateResult()),
  };
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
 * なお合成の成果物（MergedResult）はグループIDをキーに持ったまま詰め替えていないが、
 * それでよい。引き当ては mergedResultFor を通し、そこで「そのグループの現在の合成
 * （groupMergeResultOf）であり、かつ先頭フレームが採用時と一致すること」まで確かめる。
 * IDの使い回しで別グループの合成が残っていても関門で落ちるし、フレーム構成の変わった
 * 合成し直しは別物として「成果物が失われた」扱いになる。詰め替えを増やして状態を二重に
 * 持つより、読み出し時に 1 箇所で検証する方が食い違いの余地が少ない。
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

/**
 * 解析結果の世代。Rust（#32）は完了した解析ごとに一意の `analysis_revision` を返す。
 * 世代を持たない解析（Python 版 v2 の応答）だけ、従来どおり `generated_at` で代用する。
 */
export function analysisRevisionOf(analysis: Analysis): string {
  return analysis.analysis_revision ?? `generated_at:${analysis.generated_at}`;
}

export function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "engine/ok":
      return { ...state, engineStatus: "ready", engineLabel: action.label };

    case "engine/fail":
      // プリセット一覧はエンジンと独立なので残す（#98）。
      return { ...state, engineStatus: "offline" };

    case "presets/loaded":
      return applyPresets(state, action.presets);

    case "analysis/status": {
      if (action.generation !== state.sourceFolderGeneration) return state;
      const response = action.response;
      if (response.status === "ready") {
        const analysis = response.analysis;
        // 解析は 1 秒ポーリングで何度も同じ結果を返す。作り直しが起きたとき
        // （analysis_revision が変わったとき）だけ採用を結び直す。毎回やると、同じ
        // 内容の再取得でも配列の同一性が失われて useMemo が無駄に走る。
        // generated_at は秒精度なので、同じ秒の再解析を見分けられない（#3 B1）。
        const regenerated =
          state.analysis !== null && analysisRevisionOf(state.analysis) !== analysisRevisionOf(analysis);
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
      return {
        ...state,
        analysisState: "absent",
        analysisProgress: null,
        // Rust 側が直近の解析失敗を添えてくることがある（#110）。ゲートに出せるよう保持する。
        analysisError: response.error ?? "",
        analyzeRequested: false,
      };
    }

    case "analysis/unconfigured":
      if (action.generation !== state.sourceFolderGeneration) return state;
      return {
        ...state,
        analysisState: "unconfigured",
        analysis: null,
        analysisProgress: null,
        analysisError: "",
        analyzeRequested: false,
      };

    case "analysis/requested":
      return { ...state, analyzeRequested: true, analysisState: "running", analysisError: "" };

    case "analysis/error":
      if (action.generation !== state.sourceFolderGeneration) return state;
      return { ...state, analysisState: "error", analysisError: action.message, analyzeRequested: false };

    case "source/loaded":
      return {
        ...state,
        sourceFolder: action.info,
        sourceFolderBusy: false,
        // 保存済みのフォルダが後から読めなくなる（外部ボリュームの取り外し、macOS の
        // 許可取り消し）ことがある。サイドバーの短い注記だけでは対処が分からないため、
        // 起動時に気づいた時点で対処方法もトーストで出す。
        notice: action.info.exists && !action.info.readable ? SOURCE_FOLDER_PERMISSION_HELP : state.notice,
        noticeTone: action.info.exists && !action.info.readable ? "error" : state.noticeTone,
      };

    case "source/applying":
      return { ...state, sourceFolderBusy: true };

    case "source/changed": {
      // フォルダが変わると capture_id の指す実体が変わる（前のフォルダの DSC02943 は
      // 新しいフォルダでは別の写真か、存在しない）。選択・現像結果・書き出し選択を
      // まとめて捨て、エンジン接続情報と選択中プリセットだけを引き継ぐ。
      // 解析のやり直しは呼び出し側が beginAnalyze(true) で行う。
      //
      // initialState を土台にするので developMode / mergeMode / developingCapture /
      // mergingGroupId も null に戻る（＝実行中表示は残らない）。飛んでいる fetch 自体は
      // 止められないため、遅れて届く結果は sourceFolderGeneration で捨てる。
      return {
        ...initialState,
        engineLabel: state.engineLabel,
        engineStatus: state.engineStatus,
        presets: state.presets,
        presetName: state.presetName,
        sourceFolder: action.info,
        // **initialState の 0 に戻さない**。ここで進めた値が「今のフォルダ」の目印であり、
        // 飛んでいる現像・合成のリクエストが持つ古い世代と必ず違う値になる必要がある。
        // スプレッドより後に置くこと（前に置くと 0 に上書きされ、ガードが無言で死ぬ）。
        sourceFolderGeneration: state.sourceFolderGeneration + 1,
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
        presetName: suggestPreset(state.presets, group),
        overrides: {},
        // 別グループ・別プリセットの候補を持ち越さない。生成中（busy）の応答は
        // captureId を残して受け取るが、表示・適用時に capture_id / preset を照合する。
        ...clearedCandidateResult(),
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
      // 採用を外すときの同一性は acceptedKey と同じ groupId + leadCaptureId で見る。
      // reconcileAccepted は仕様どおり重複除去をしないので、同じ groupId に合成の採用が
      // 複数同居しうる（別々のグループで採用した 2 件の合成が、再解析で 1 グループへ
      // 再結合した場合）。groupId だけで外すと、そのときチップを 1 回押しただけで
      // 2 件とも消える。
      const already = state.accepted.some(
        (item) =>
          item.kind === "merged" &&
          item.groupId === action.groupId &&
          item.leadCaptureId === action.leadCaptureId,
      );
      if (already) {
        // 完全一致する採用を外す（通常のトグルオフ）。同居している別の合成採用は残す。
        return {
          ...state,
          accepted: state.accepted.filter(
            (item) =>
              !(
                item.kind === "merged" &&
                item.groupId === action.groupId &&
                item.leadCaptureId === action.leadCaptureId
              ),
          ),
        };
      }
      const group = state.analysis?.groups.find((item) => item.id === action.groupId);
      // 新規採用は、選択中のグループの **今の合成設定に合う本合成** だけ許可する。
      // プレビュー結果・別グループ・古い capture ID に加えて、フレーム選択/プリセット/調整が
      // 変わって合わなくなった古い本合成も、action を直接渡されて書き出し対象にしない
      // （UI のボタンが無効でも reducer 単体で守る。取り消しは上の already 分岐が先に通す）。
      if (!group || state.selectedGroupId !== group.id) return state;
      const full = currentMergeResultsOf(state, group).full;
      if (!full || full.capture_ids[0] !== action.leadCaptureId) return state;
      // 新規採用は **同じグループの古い合成採用を置き換える**（増やさない）。
      //
      // 置き換えないと孤児が残る: グループGでリード=Xの合成を採用したあと、フレームを
      // 選び直して同じGを合成し直すとリードがYになる。mergedResultFor はグループの現在の
      // 合成しか見ないので {G,X} は成果物を引けなくなり、書き出しからは黙って落ちる。
      // なのに採用件数は 2 のまま、UI から {G,X} を外す手段も無い（リード=Xの構成で
      // 合成し直さない限りチップが {G,X} を指さない）。
      //
      // ユーザーの意図は「このグループの合成を採用」であって「このグループに合成の採用を
      // 積み増す」ではない。再結合で 2 件が同居するケース（acceptedKey のコメント参照）は
      // reconcileAccepted が作るものでこのアクションを経由しないため、ここで消しても
      // 影響しない。ただし再結合で同居した状態から **さらに** 同じグループを合成し直して
      // 採用すると、その 2 件は 1 件へまとまる。どちらも同じグループの合成であり、
      // 書き出せるのは現在の合成 1 件だけなので、これは正しい畳み込みになる。
      //
      // 置き換えたぶんは配列の末尾へ移る。accepted の並びは書き出し順なので、
      // 「合成し直して採用し直す」は採用し直した順に並ぶ（新規採用と同じ扱い）。
      return {
        ...state,
        accepted: [
          ...state.accepted.filter((item) => !(item.kind === "merged" && item.groupId === action.groupId)),
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
      return {
        ...state,
        presetName: action.presetName,
        overrides: {},
        developmentCandidates: null,
        developmentCandidatesCaptureId: null,
        developmentCandidatesError: "",
      };

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
      // フォルダが切り替わったあとに届いた、**前のフォルダの RAW から作られた結果**。
      // capture_id はカメラの連番なので新しいフォルダの写真と衝突しうる（同じIDの別写真が
      // 存在する）。書き込むと「現像したら別の写真が出る」になり、しかもファイル名も
      // capture_id も整合するので気づけない。結果は捨て、state を一切触らない。
      // 通知も出さない: フォルダ切替のトーストが出た直後に前のフォルダの完了トーストを
      // 重ねると、新しいフォルダで何かが起きたように読めるため。
      if (action.generation !== state.sourceFolderGeneration) return state;
      const { capture_id: captureId } = action.result;
      const elapsed = action.result.cached ? "保存済み結果" : `${action.result.elapsed_seconds.toFixed(1)}秒`;
      const modeLabel = action.mode === "preview" ? "プレビュー" : "本";
      // Core Image の結果は通知でもエンジンを明示する。
      const engineLabel = action.result.engine === "coreimage" ? "Core Image・" : "";
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
        notice: `${captureId} の${modeLabel}現像が完了しました（${engineLabel}${elapsed}）`,
        noticeTone: "info",
      };
    }

    case "develop/fail":
      return { ...state, developMode: null, developingCapture: null, notice: action.message, noticeTone: "error" };

    case "candidates/start":
      return {
        ...state,
        developmentCandidates: null,
        developmentCandidatesBusy: true,
        developmentCandidatesCaptureId: action.captureId,
        developmentCandidatesError: "",
        developmentCandidatesGeneration: action.generation,
        notice: `${action.captureId}.ARW を縮小デコードして評価しています…`,
        noticeTone: "info",
      };

    case "candidates/ok":
      // フォルダ切替後に届いた古い代理評価を新しい capture_id と混ぜない。
      // 同じ世代で将来リクエストを重複させても、後発写真の busy/error を先発写真の
      // 応答で上書きしないよう、start 時点の capture_id も照合する。
      if (
        action.generation !== state.sourceFolderGeneration ||
        action.captureId !== state.developmentCandidatesCaptureId
      ) return state;
      return {
        ...state,
        developmentCandidates: action.response,
        developmentCandidatesCaptureId: action.captureId,
        developmentCandidatesBusy: false,
        developmentCandidatesError: "",
        notice: `${action.captureId} の現像候補を作成しました。候補を選んでから現像してください。`,
        noticeTone: "info",
      };

    case "candidates/fail":
      if (
        action.generation !== state.sourceFolderGeneration ||
        action.captureId !== state.developmentCandidatesCaptureId
      ) return state;
      return {
        ...state,
        developmentCandidates: null,
        developmentCandidatesBusy: false,
        developmentCandidatesError: action.message,
        notice: action.message,
        noticeTone: "error",
      };

    case "candidates/apply": {
      // 表示中の候補と同じ照合を適用の直前にもかける（別の写真・別プリセットの調整値を
      // 今のスライダーへ入れない）。候補は表示中の応答から id で引き直す。
      const shown = currentDevelopmentCandidates(state, action.captureId).find(
        (item) => item.id === action.candidate.id,
      );
      if (!shown) {
        return {
          ...state,
          notice: "現在の写真・プリセットの候補ではないため、調整値は反映しませんでした。",
          noticeTone: "error",
        };
      }
      const preset = activePreset(state);
      const overrides: Partial<Adjustments> = {};
      for (const key of Object.keys(shown.adjustments) as (keyof Adjustments)[]) {
        const value = shown.adjustments[key];
        if (!preset || value !== preset.adjustments[key]) Object.assign(overrides, { [key]: value });
      }
      return {
        ...state,
        overrides,
        notice: `「${shown.label}」の調整値をスライダーへ反映しました。現像はまだ実行していません。`,
        noticeTone: "info",
      };
    }

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
      // develop/ok と同じ理由で、フォルダ切替後に届いた古いフォルダの合成結果は捨てる。
      // 合成は groupId をキーに書き込むため、こちらは「新しいフォルダの解析で同じIDに
      // なったグループ」に前のフォルダの合成が入る、という形で化ける。
      if (action.generation !== state.sourceFolderGeneration) return state;
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
      // 古いフォルダ/別グループから遅れて届いた失敗で、現在のbusyや通知を壊さない。
      if (
        action.generation !== state.sourceFolderGeneration ||
        action.groupId !== state.mergingGroupId
      ) return state;
      return { ...state, mergeMode: null, mergingGroupId: null, notice: action.message, noticeTone: "error" };

    case "export/batchName":
      // バッチ名が変わったら、前のバッチについての結果表示は意味を失う。
      return { ...state, batchName: action.value, exportResult: null, importResult: null };

    case "export/start":
      return { ...state, exporting: true, exportResult: null, importResult: null };

    case "export/ok": {
      // 採用したのに成果物が引けなかった行は送っていない。黙って減らすと「採用3件の
      // うち1件が書き出されなかった」ことに気づけないまま成功トーストが出るので、
      // 件数と理由を必ず添える。再解析で採用が落ちたとき（analysis/status）と同じ扱いで、
      // 1 件でも欠けたらトーストは error 側にする（見た目が成功のままだと読み飛ばされる）。
      const missing = action.missingCount ?? 0;
      return {
        ...state,
        exporting: false,
        exportResult: action.result,
        notice:
          missing > 0
            ? `${action.result.files.length}件を書き出しました（${missing}件は成果物が失われているため除外）`
            : `${action.result.files.length}件を書き出しました`,
        noticeTone: missing > 0 ? "error" : "info",
      };
    }

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
      return { ...state, viewer: action.gallery };

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
  //
  // 先頭フレームは **今の選択・プリセット・調整に合う合成結果** から取る。raw な本合成を
  // 優先すると、フレームを選び直して新しいプレビューを作ったあとも古い本合成の先頭を
  // 返し、画面に出ている合成と SELECTED PHOTO / EXIF / 写真の採用先が別の写真になる。
  // 合う結果が無いときは今の選択の先頭フレームを見る。frames は選択順、合成結果の
  // capture_ids はサーバが昇順に正規化したものなので、選択順が昇順でないと合成結果が
  // 合っている間と合っていない間で先頭が入れ替わりうる。これは「合っていない間は
  // 今の選択の先頭を写真として見せる」という明示的な表示上のフォールバックで、
  // effectiveViewingOf が写真面として扱う。採用の宛先（mergedAcceptTargetOf）は
  // 合成結果の先頭だけを使うので、このフォールバックが採用先を動かすことはない。
  const shown = currentMergeResultsOf(state, group).shown;
  const lead = [shown?.capture_ids[0], mergeFramesOf(state, group)[0]].find(
    (captureId) => captureId !== undefined && group.capture_ids.includes(captureId),
  );
  return lead ?? group.recommended_capture_id;
}

/**
 * 実際に画面へ出している面。
 *
 * 保存されたカーソルが merged でも、今の設定に合う合成結果が無ければ画面に出るのは
 * viewingCaptureOf が返す写真（今の選択の先頭フレーム）になる。カーソルのまま渡すと、
 * 表示は写真なのに合成タイルが「表示中」のままで、実際に出ている写真のタイルに印が付かない。
 * 表示・SELECTED PHOTO・EXIF・採用先は、この関数が返す面に揃える。
 * 保存されたカーソル自体は書き換えない（合成結果が合い直せば merged の表示へ戻る）。
 */
export function effectiveViewingOf(state: State, group: GroupInfo): ViewingItem {
  const viewing = viewingOf(state, group);
  if (viewing.kind === "merged" && !currentMergeResultsOf(state, group).shown) {
    return { kind: "photo", captureId: viewingCaptureOf(state, group) };
  }
  return viewing;
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
 * その合成結果が「今の合成設定で作ったもの」か。
 *
 * 照合するのはフレームの集合・プリセット・調整値の全量。いずれかが違えば、画面に出すと
 * 今の設定と別の画像を「合成結果」として見せることになる。
 *
 * プリセットと調整値（state.presetName / overrides）は **選択中のグループの操作内容** なので、
 * 選択中でないグループ（一覧カードの代表写真など）には当てはめられない。そちらはフレームの
 * 集合だけを照合する。調整値がまだ取れていない（プリセット未取得）選択中グループは、
 * 照合できないので合わないものとして扱う。
 */
export function isMergeResultCurrent(state: State, group: GroupInfo, result: MergedResult): boolean {
  const frames = mergeFramesOf(state, group);
  if (result.capture_ids.length !== frames.length || !frames.every((id) => result.capture_ids.includes(id))) {
    return false;
  }
  if (state.selectedGroupId !== group.id) return true;
  const adjustments = effectiveAdjustments(state);
  if (!adjustments || result.preset !== state.presetName) return false;
  // 古い recipe 由来の結果は adjustments が null / undefined / 配列などで来うる。
  // 照合できない結果を「合っている」とは推定せず、落とさずに合わないものとして扱う。
  const recipe: unknown = result.adjustments;
  if (!recipe || typeof recipe !== "object" || Array.isArray(recipe)) return false;
  const recorded = recipe as Partial<Adjustments>;
  return (Object.keys(adjustments) as (keyof Adjustments)[]).every((key) => recorded[key] === adjustments[key]);
}

/**
 * そのグループで「今の合成設定に合う」合成結果。
 *
 * - `full` / `preview`: groupMergeResultOf の関門（全フレームがグループ内）に加えて
 *   isMergeResultCurrent を通ったものだけ。
 * - `shown`: 画面に出す1枚。本合成が合っていればそれ、無ければ合っているプレビュー。
 *
 * 表示・SELECTED PHOTO/EXIF・写真の採用先・合成の採用ボタンの有効判定は、すべてこの値を
 * 見る。**採用済みの成果物の引き直し（mergedResultFor）はここを通さない**: 採用は
 * 採用した時点の成果物のスナップショットで、あとから現像パネルを触っただけで書き出し対象
 * から外れてはいけないため。
 */
export function currentMergeResultsOf(
  state: State,
  group: GroupInfo,
): { preview?: MergedResult; full?: MergedResult; shown?: MergedResult } {
  const pick = (results: Record<number, MergedResult>) => {
    const result = groupMergeResultOf(results, group);
    return result && isMergeResultCurrent(state, group, result) ? result : undefined;
  };
  const preview = pick(state.mergePreviewResults);
  const full = pick(state.mergeFullResults);
  return { preview, full, shown: full ?? preview };
}

/**
 * 合成タイルの採用ボタン（新規採用・取り消し）が操作する先頭フレーム。操作できないなら undefined。
 *
 * 宛先は **今の設定に合う本合成だけ**。合わなくなった本合成は、未採用なら新規採用させず、
 * 採用済みでもこのボタンの宛先にしない（現在の画像と別物の採用を、同じボタンで外すと
 * 何をトグルしているか分からなくなる）。採用済みの古い成果物の取り消しは
 * acceptedMergeOf の leadCaptureId を使う専用の導線が持つ。
 */
export function mergedAcceptTargetOf(state: State, group: GroupInfo): CaptureId | undefined {
  return currentMergeResultsOf(state, group).full?.capture_ids[0];
}

/** 採用済みの合成（採用時のスナップショット）と、今の合成設定との関係。 */
export type AcceptedMergeView = {
  /** accept/merged で取り消すときの先頭フレーム。 */
  leadCaptureId: CaptureId;
  /** 採用した成果物。再合成などで引けなくなっていれば undefined（取り消しだけできる）。 */
  result: MergedResult | undefined;
  /** 採用した成果物が、今の選択・プリセット・調整に合う本合成そのものか。 */
  isCurrent: boolean;
};

/**
 * そのグループで採用済みの合成。無ければ undefined。
 *
 * currentMergeResultsOf は **今の設定に合うものだけ** を返し、こちらは **採用時のスナップショット**
 * を返す。両者を黙って入れ替えない: 採用が今の画像と別物のとき（isCurrent が false）は、
 * ✓ を今表示している合成に付けず、「採用済み（以前の設定）」として別に見せる。
 * 引き直しは mergedResultFor と同じ（グループの現在の本合成の先頭フレームが一致するか）。
 * 再結合で同じグループに採用が複数同居するときは、成果物を引けるものを優先する。
 */
export function acceptedMergeOf(state: State, group: GroupInfo): AcceptedMergeView | undefined {
  const items = state.accepted.filter(
    (item): item is AcceptedMerged => item.kind === "merged" && item.groupId === group.id,
  );
  if (items.length === 0) return undefined;
  const currentFull = currentMergeResultsOf(state, group).full;
  const resolved = items
    .map((item) => ({ item, result: mergedResultFor(state.mergeFullResults, group, item.leadCaptureId) }))
    .find((entry) => entry.result !== undefined);
  if (!resolved) return { leadCaptureId: items[0].leadCaptureId, result: undefined, isCurrent: false };
  return {
    leadCaptureId: resolved.item.leadCaptureId,
    result: resolved.result,
    isCurrent: resolved.result === currentFull,
  };
}

/**
 * 採用した合成の成果物を引き直す。
 *
 * 以前は mergeFullResults を先頭フレームIDだけで **全件スキャン** していたが、再解析で
 * グループIDが使い回されると、同じ先頭フレームIDを持つ古い（無関係になった）合成結果が
 * 複数キーに残ることがあり、どちらが返るかは Object.values() の列挙順（整数キーは昇順）
 * で決まってしまっていた。挿入順ではないので、多くの場合は **古い方（数値の小さいキー）
 * が勝つ**。この結果は書き出し（exportRows）と一覧の枚数表示（acceptedSummaryOf）の
 * 両方に流れ、しかもファイル名と capture_id は整合するのでサーバは 200 を返す。採用を
 * 解除してやり直しても leadCaptureId は同じ値なので直らず、実質発見できなかった。
 *
 * 採用アイテムの groupId は reconcileAccepted が leadCaptureId から引き直した「現在
 * 属しているグループ」なので、その groupId の「現在の」合成結果（groupMergeResultOf）を
 * 見て、先頭フレームが一致するかだけ確認する。グローバルには探し直さない。
 *
 * undefined になる経路は 2 つあり、**どちらも「成果物が失われた」扱いで正しい**:
 * 1. 合成が別のフレーム構成でやり直された（先頭フレームが変わった）→ 別物の合成
 * 2. 再解析でグループIDがずれ、合成結果が **古いキーのまま取り残された**
 *    （mergeFullResults は詰め替えない）。合成をやり直していなくてもこうなる。
 *
 * 2 の方は「無関係な合成を無言で書き出す」より安全なだけでなく、そのグループを合成し直せば
 * 自然に直る: merge/ok は現在の groupId をキーに結果を入れるので、同じフレーム構成なら
 * 先頭フレームも同じ値に戻り、この関数も isMergedAccepted も再び一致する。
 *
 * ExportPanel 側は merged: undefined を「成果物が失われている」行として理由付きで表示する
 * （行は消さない）。
 */
export function mergedResultFor(
  mergeFullResults: State["mergeFullResults"],
  group: GroupInfo,
  leadCaptureId: CaptureId,
): MergedResult | undefined {
  const current = groupMergeResultOf(mergeFullResults, group);
  return current && current.capture_ids[0] === leadCaptureId ? current : undefined;
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
  // 成果物を引くにはグループが要る（mergedResultFor はグループの現在の合成だけを見る）。
  // 解析がまだ無い・グループが消えた場合は枚数を出さず "合成" へ落とす。
  const group = state.analysis?.groups.find((candidate) => candidate.id === groupId);
  return state.accepted
    .filter((item) => item.groupId === groupId)
    .map((item) => {
      if (item.kind === "photo") return item.captureId;
      // 合成の枚数は成果物から引く。採用時に控えても、合成し直したら古い枚数が残る。
      const merged = group ? mergedResultFor(state.mergeFullResults, group, item.leadCaptureId) : undefined;
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

/**
 * その合成（グループ + 先頭フレーム）が採用済みか。
 *
 * groupId だけでは足りない。再結合で同じ groupId に合成の採用が複数並びうるし、
 * `accept/merged` が採用を外すときも同じ 2 つ組で同一性を見るため、判定をここだけ
 * groupId にすると「採用済みに見えるチップを押すと別の採用が消える」食い違いになる。
 *
 * 逆に「同じグループの合成を採用し直したら古い方は消える」（accept/merged の新規採用が
 * groupId 単位で置き換えること）は、この判定と食い違わない。置き換えたあとは新しい
 * リードだけが残り、チップはそれを指すため。
 */
export function isMergedAccepted(state: State, groupId: number, leadCaptureId: CaptureId): boolean {
  return state.accepted.some(
    (item) => item.kind === "merged" && item.groupId === groupId && item.leadCaptureId === leadCaptureId,
  );
}

export function photoOf(analysis: Analysis | null, captureId: CaptureId | undefined): PhotoInfo | null {
  if (!analysis || !captureId) return null;
  return analysis.photos[captureId] ?? null;
}

/** 候補結果のクリア（busy / captureId / generation は触らない）。 */
function clearedCandidateResult(): Pick<State, "developmentCandidates" | "developmentCandidatesError"> {
  return { developmentCandidates: null, developmentCandidatesError: "" };
}

/**
 * `captureId` の写真について表示・適用してよい候補。応答の capture_id / preset が
 * 現在の撮影・プリセットと一致し、かつ現在のフォルダ世代の応答のときだけ返す。
 */
export function currentDevelopmentCandidatesResponse(
  state: State,
  captureId: CaptureId | null,
): DevelopmentCandidatesResponse | null {
  const response = state.developmentCandidates;
  if (!response || !captureId) return null;
  if (
    response.capture_id !== captureId ||
    response.preset !== state.presetName ||
    state.developmentCandidatesCaptureId !== captureId ||
    state.developmentCandidatesGeneration !== state.sourceFolderGeneration
  ) {
    return null;
  }
  return response;
}

export function currentDevelopmentCandidates(state: State, captureId: CaptureId | null): DevelopmentCandidate[] {
  return currentDevelopmentCandidatesResponse(state, captureId)?.candidates ?? [];
}

export function activePreset(state: State): PresetInfo | null {
  return presetFor(state.presets, state.presetName);
}

/**
 * プリセット既定値 + ユーザー変更分の 9 キー全量。調整値の解決はここ 1 か所だけで行う
 * （表示用の `effectiveAdjustments` と、Core Image へ渡す `coreimage-api.ts` の再エクスポートが共用）。
 * Rust（develop_preview_coreimage）は全量を要求するので、上書きの無いキーもプリセット値で埋める。
 */
export function resolveAdjustments(preset: PresetInfo, overrides: Partial<Adjustments>): Adjustments {
  return { ...preset.adjustments, ...overrides };
}

/** プリセット既定値 + ユーザー変更分。プリセット未取得なら null。 */
export function effectiveAdjustments(state: State): Adjustments | null {
  const preset = activePreset(state);
  if (!preset) return null;
  return resolveAdjustments(preset, state.overrides);
}

export function changedAdjustmentKeys(state: State): (keyof Adjustments)[] {
  const preset = activePreset(state);
  if (!preset) return [];
  return (Object.keys(state.overrides) as (keyof Adjustments)[]).filter(
    (key) => state.overrides[key] !== preset.adjustments[key],
  );
}

/**
 * 現像済み（本現像）または本合成の結果を持つグループID。
 *
 * 合成の有無も groupMergeResultOf の関門を通す。直接インデックスすると、再解析でIDが
 * 使い回されたときに別グループの合成が残っているだけで「現像済み」に数えてしまう
 * （タブの件数と、そのグループを開いたときの表示が食い違う）。
 */
export function developedGroupIds(state: State): number[] {
  const groups = state.analysis?.groups ?? [];
  return groups
    .filter(
      (group) =>
        group.capture_ids.some((captureId) => state.fullResults[captureId]) ||
        Boolean(groupMergeResultOf(state.mergeFullResults, group)),
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

/**
 * 現在の合成対象フレーム。ユーザーが触っていなければ既定値。
 *
 * 選択は **グループID をキー** に持っており、再解析では詰め替えていない
 * （キーと構造を変えない約束のため）。IDが使い回されると、別のグループで選んだ
 * 撮影IDがそのまま残る。ここで返した配列は合成の実行側がそのまま
 * 送るので、絞らないと無関係な写真同士を合成しにいく。必ずグループ内に絞る。
 */
export function mergeFramesOf(state: State, group: GroupInfo): CaptureId[] {
  const selected = state.mergeSelections[group.id];
  if (!selected) return defaultMergeFrames(group);
  const valid = selected.filter((id) => group.capture_ids.includes(id));
  // 全部がグループの外だった（= 丸ごと別グループの選択が残っていた）ときだけ既定値へ
  // 戻す。1枚でもグループ内に残っていればそれを尊重する。ユーザーが選び直した途中の
  // 状態を黙って全部リセットする方が分かりにくいため。
  return valid.length > 0 ? valid : defaultMergeFrames(group);
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
