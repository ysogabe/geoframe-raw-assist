/**
 * 原寸ビューア（FullSizeViewer）の状態遷移。
 *
 * コンポーネントは DOM / Image の副作用だけを持ち、写真ごとの状態（面の読み込み・
 * 倍率とパン・ドラッグ・長押し）はここへ寄せてある。DOM が無い node 環境でも
 * 「写真送りの途中で遅い応答が返る」「片面だけ壊れている」といった順序を再現できるようにするため。
 *
 * 読み込み状態は面ごとに持つ。以前は単一の failure 文字列で、表示していない面の
 * onError でも全体がエラー表示になり、正常な面へ切り替えても消えなかった。
 */

export type Face = "before" | "after";
export type Size = { width: number; height: number };
/** ステージ座標系での画像の置き方。translate してから scale する（transform-origin は 0 0）。 */
export type ViewState = { scale: number; tx: number; ty: number };
export type FaceLoad = "idle" | "loading" | "ready" | "error";
export type DragState = { id: number; x: number; y: number; tx: number; ty: number };

/** 拡大の上限。6000px 級の画像で 8 倍まで寄れれば粒状まで確認できる。 */
export const MAX_SCALE = 8;
/** ホイールの deltaY にかける係数。exp を取るので、どの方向へも等比で動く。 */
export const WHEEL_ZOOM_SPEED = 0.0018;
/** キーボード / ボタン 1 回あたりの倍率。 */
export const ZOOM_STEP = 1.25;

export function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

/** 画像全体がステージに収まる倍率で中央に置く。 */
export function centeredView(scale: number, base: Size, stage: Size): ViewState {
  return {
    scale,
    tx: (stage.width - base.width * scale) / 2,
    ty: (stage.height - base.height * scale) / 2,
  };
}

/** その 2 面で最初に見せる面。現像していなければ現像後は選べない。 */
export function faceOf(item: { afterUrl?: string; initial: "before" | "after" }): Face {
  return item.afterUrl && item.initial === "after" ? "after" : "before";
}

export type ViewerState = {
  /** 写真送りのたびに増える。古い写真に向けて飛んだ非同期の応答を捨てる印。 */
  generation: number;
  index: number;
  face: Face;
  peek: boolean;
  /** 片面しか見ない使い方でメモリを払わないよう、2 枚目は必要になってから DOM に載せる。 */
  mounted: Record<Face, boolean>;
  faces: Record<Face, FaceLoad>;
  faceErrors: Record<Face, string>;
  /**
   * 基準グリッド。常に現像前（カメラJPG）の実解像度。scale = 1 がそのまま原寸になり、
   * 倍率表示が嘘をつかない。
   */
  base: Size | null;
  /** 基準グリッドそのものを作れなかったときの理由。この写真は面を問わず表示できない。 */
  baseError: string;
  view: ViewState;
  /** 全体表示の倍率（ステージ寸法が変わるたびに測り直す）。 */
  fit: number;
  drag: DragState | null;
};

const IDLE_FACES: Record<Face, FaceLoad> = { before: "idle", after: "idle" };
const NO_ERRORS: Record<Face, string> = { before: "", after: "" };
const HOME_VIEW: ViewState = { scale: 1, tx: 0, ty: 0 };

function photoState(index: number, face: Face, generation: number): Omit<ViewerState, "fit"> {
  return {
    generation,
    index,
    face,
    peek: false,
    mounted: { before: face === "before", after: face === "after" },
    faces: { ...IDLE_FACES, [face]: "loading" },
    faceErrors: { ...NO_ERRORS },
    base: null,
    baseError: "",
    view: { ...HOME_VIEW },
    drag: null,
  };
}

export function createViewerState(index: number, initialFace: Face): ViewerState {
  return { ...photoState(index, initialFace, 0), fit: 1 };
}

export type ViewerAction =
  | { type: "photo/go"; index: number; face: Face }
  | { type: "face/select"; face: Face }
  | { type: "face/warm"; face: Face }
  | { type: "peek/start" }
  | { type: "peek/end" }
  | { type: "face/loaded"; generation: number; face: Face }
  | { type: "face/failed"; generation: number; face: Face; message: string }
  /** 最初に見せる現像後の事前デコードが失敗した。現像前が読めるならそちらへ倒す。 */
  | { type: "initial/failed"; generation: number; face: Face; message: string }
  | { type: "base/loaded"; generation: number; size: Size }
  | { type: "base/failed"; generation: number; message: string }
  | { type: "stage/fit"; stage: Size; initial: boolean }
  | { type: "view/fit"; stage: Size }
  | { type: "zoom/at"; point: { x: number; y: number }; factor: number }
  | { type: "zoom/to"; point: { x: number; y: number }; target: number }
  | { type: "drag/start"; id: number; x: number; y: number }
  | { type: "drag/move"; id: number; x: number; y: number }
  | { type: "drag/end"; id: number };

/** まだ DOM に載せていない面を載せる。読み込みは載せた時点から始まる。 */
function withMounted(state: ViewerState, target: Face): ViewerState {
  if (state.mounted[target]) return state;
  return {
    ...state,
    mounted: { ...state.mounted, [target]: true },
    faces: state.faces[target] === "idle" ? { ...state.faces, [target]: "loading" } : state.faces,
  };
}

/** point（ステージ座標）の下にある画像上の点を動かさずに倍率を変える。 */
function zoomedView(prev: ViewState, point: { x: number; y: number }, scale: number): ViewState {
  if (scale === prev.scale) return prev;
  // t2 = p - (p - t) * (s2 / s) を x/y 独立に解く。
  const ratio = scale / prev.scale;
  return {
    scale,
    tx: point.x - (point.x - prev.tx) * ratio,
    ty: point.y - (point.y - prev.ty) * ratio,
  };
}

function withView(state: ViewerState, view: ViewState): ViewerState {
  return view === state.view ? state : { ...state, view };
}

export function viewerReducer(state: ViewerState, action: ViewerAction): ViewerState {
  switch (action.type) {
    case "photo/go": {
      if (action.index === state.index) return state;
      // 写真に紐づく状態は **すべて** 初期化する。ドラッグも含む：掴んだまま送ると
      // 前の写真の起点座標が次の写真のパンに持ち越されてしまう。fit だけはステージの性質なので残す。
      return { ...photoState(action.index, action.face, state.generation + 1), fit: state.fit };
    }

    case "face/select":
      return { ...withMounted(state, action.face), peek: false, face: action.face };

    case "face/warm":
      return withMounted(state, action.face);

    case "peek/start":
      if (state.face === "before") return state;
      return { ...withMounted(state, "before"), peek: true };

    case "peek/end":
      return state.peek ? { ...state, peek: false } : state;

    case "face/loaded": {
      if (action.generation !== state.generation) return state;
      if (state.faces[action.face] === "ready") return state;
      // 後から読めたなら、以前の失敗は取り消す（一時的な失敗からの復帰）。
      return {
        ...state,
        faces: { ...state.faces, [action.face]: "ready" },
        faceErrors: { ...state.faceErrors, [action.face]: "" },
      };
    }

    case "face/failed": {
      if (action.generation !== state.generation) return state;
      return {
        ...state,
        faces: { ...state.faces, [action.face]: "error" },
        faceErrors: { ...state.faceErrors, [action.face]: action.message },
      };
    }

    case "initial/failed": {
      if (action.generation !== state.generation) return state;
      const failed: ViewerState = {
        ...state,
        faces: { ...state.faces, [action.face]: "error" },
        faceErrors: { ...state.faceErrors, [action.face]: action.message },
      };
      // 見せるはずだった面が壊れていても、反対側が読めるなら正常な面から始める。
      // 壊れた面は選べば（再読み込みも含めて）そのエラーが出る。
      if (action.face === "before" || state.face !== action.face) return failed;
      return { ...withMounted(failed, "before"), face: "before" };
    }

    case "base/loaded":
      if (action.generation !== state.generation) return state;
      return { ...state, base: action.size, baseError: "" };

    case "base/failed":
      if (action.generation !== state.generation) return state;
      return { ...state, base: null, baseError: action.message };

    case "stage/fit": {
      const base = state.base;
      if (!base) return state;
      const fit = Math.min(action.stage.width / base.width, action.stage.height / base.height);
      // 拡大して見ている最中のリサイズでは倍率と位置を保つ。全体表示より縮んでいる
      // （＝リサイズで下限を割った）ときだけ、全体表示へ戻して中央に置き直す。
      const view =
        !action.initial && state.view.scale > fit ? state.view : centeredView(fit, base, action.stage);
      return { ...state, fit, view };
    }

    case "view/fit":
      if (!state.base) return state;
      return withView(state, centeredView(state.fit, state.base, action.stage));

    case "zoom/at":
      return withView(
        state,
        zoomedView(state.view, action.point, clamp(state.view.scale * action.factor, state.fit, MAX_SCALE)),
      );

    case "zoom/to":
      return withView(state, zoomedView(state.view, action.point, clamp(action.target, state.fit, MAX_SCALE)));

    case "drag/start":
      if (!state.base) return state;
      return {
        ...state,
        drag: { id: action.id, x: action.x, y: action.y, tx: state.view.tx, ty: state.view.ty },
      };

    case "drag/move": {
      const drag = state.drag;
      if (!drag || drag.id !== action.id) return state;
      return {
        ...state,
        view: { ...state.view, tx: drag.tx + (action.x - drag.x), ty: drag.ty + (action.y - drag.y) },
      };
    }

    case "drag/end":
      if (!state.drag || state.drag.id !== action.id) return state;
      return { ...state, drag: null };

    default:
      return state;
  }
}

/** いま見せている面。長押し中は現像前。 */
export function shownFace(state: Pick<ViewerState, "face" | "peek">): Face {
  return state.peek ? "before" : state.face;
}

export type ViewerStatus =
  | { kind: "loading" }
  | { kind: "ready" }
  | { kind: "error"; message: string };

/**
 * 表示面に応じたスピナー / エラー。見ていない面の失敗はここには出ない
 * （その面へ切り替えたとき、はじめて出る）。
 */
export function viewerStatus(state: ViewerState): ViewerStatus {
  if (state.baseError) return { kind: "error", message: state.baseError };
  if (!state.base) return { kind: "loading" };
  const shown = shownFace(state);
  switch (state.faces[shown]) {
    case "error":
      return { kind: "error", message: state.faceErrors[shown] };
    case "ready":
      return { kind: "ready" };
    default:
      return { kind: "loading" };
  }
}
