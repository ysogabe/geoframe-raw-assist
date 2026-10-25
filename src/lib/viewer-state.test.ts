import { describe, expect, it } from "vitest";
import {
  centeredView,
  createViewerState,
  faceOf,
  shownFace,
  viewerReducer,
  viewerStatus,
  type ViewerAction,
  type ViewerState,
} from "./viewer-state";

const BASE = { width: 6000, height: 4000 };
const STAGE = { width: 1200, height: 800 };

function run(state: ViewerState, ...actions: ViewerAction[]): ViewerState {
  return actions.reduce(viewerReducer, state);
}

/** 現像後を最初に見せる写真が、現像前の寸法まで取れて、現像後の <img> も読めた状態。 */
function readyAfter(): ViewerState {
  const state = createViewerState(0, "after");
  return run(
    state,
    { type: "base/loaded", generation: state.generation, size: BASE },
    { type: "stage/fit", stage: STAGE, initial: true },
    { type: "face/loaded", generation: state.generation, face: "after" },
  );
}

describe("faceOf", () => {
  it("現像していなければ initial が after でも before", () => {
    expect(faceOf({ initial: "after" })).toBe("before");
    expect(faceOf({ afterUrl: "x", initial: "after" })).toBe("after");
    expect(faceOf({ afterUrl: "x", initial: "before" })).toBe("before");
  });
});

describe("面ごとの読み込み状態", () => {
  it("読み込み中は寸法が無い間も、面が未読でもスピナー", () => {
    const state = createViewerState(0, "before");
    expect(viewerStatus(state)).toEqual({ kind: "loading" });
    const withBase = run(state, { type: "base/loaded", generation: 0, size: BASE });
    expect(viewerStatus(withBase)).toEqual({ kind: "loading" });
    expect(viewerStatus(run(withBase, { type: "face/loaded", generation: 0, face: "before" }))).toEqual({
      kind: "ready",
    });
  });

  it("見ていない面の onError は、見ている正常な面にエラーを出さない（旧: 全体 failure）", () => {
    const state = readyAfter();
    // hover warm で before が載り、非表示のまま読み込みに失敗する。
    const next = run(
      state,
      { type: "face/warm", face: "before" },
      { type: "face/failed", generation: 0, face: "before", message: "現像前の画像を読み込めませんでした。" },
    );
    expect(shownFace(next)).toBe("after");
    expect(viewerStatus(next)).toEqual({ kind: "ready" });
  });

  it("壊れた面へ切り替えるとエラー、正常な面へ戻ればエラーは消える", () => {
    const failed = run(
      readyAfter(),
      { type: "face/warm", face: "before" },
      { type: "face/failed", generation: 0, face: "before", message: "NG" },
    );
    const onBroken = run(failed, { type: "face/select", face: "before" });
    expect(viewerStatus(onBroken)).toEqual({ kind: "error", message: "NG" });
    const back = run(onBroken, { type: "face/select", face: "after" });
    expect(viewerStatus(back)).toEqual({ kind: "ready" });
  });

  it("壊れていた面が後から読めたら復帰する", () => {
    const failed = run(readyAfter(), { type: "face/failed", generation: 0, face: "after", message: "NG" });
    expect(viewerStatus(failed).kind).toBe("error");
    expect(viewerStatus(run(failed, { type: "face/loaded", generation: 0, face: "after" }))).toEqual({
      kind: "ready",
    });
  });

  it("未読の面へ切り替えている間は、その面のスピナー（正常面の ready を引きずらない）", () => {
    const next = run(readyAfter(), { type: "face/select", face: "before" });
    expect(next.mounted.before).toBe(true);
    expect(viewerStatus(next)).toEqual({ kind: "loading" });
  });

  it("長押し（peek）は表示面だけを現像前にして、その面の状態で判定する", () => {
    const state = run(readyAfter(), { type: "peek/start" });
    expect(state.peek).toBe(true);
    expect(shownFace(state)).toBe("before");
    expect(viewerStatus(state)).toEqual({ kind: "loading" });
    const failed = run(state, { type: "face/failed", generation: 0, face: "before", message: "NG" });
    expect(viewerStatus(failed)).toEqual({ kind: "error", message: "NG" });
    expect(viewerStatus(run(failed, { type: "peek/end" }))).toEqual({ kind: "ready" });
  });

  it("現像前を見せている間の長押しは何もしない", () => {
    const state = createViewerState(0, "before");
    expect(run(state, { type: "peek/start" })).toBe(state);
  });
});

describe("最初の面と基準寸法の失敗", () => {
  it("初期 after のデコード失敗でも現像前が測れれば、現像前から始まる", () => {
    const start = createViewerState(0, "after");
    const state = run(
      start,
      { type: "initial/failed", generation: 0, face: "after", message: "現像後NG" },
      { type: "base/loaded", generation: 0, size: BASE },
    );
    expect(state.face).toBe("before");
    expect(state.mounted.before).toBe(true);
    expect(viewerStatus(state)).toEqual({ kind: "loading" });
    const ready = run(state, { type: "face/loaded", generation: 0, face: "before" });
    expect(viewerStatus(ready)).toEqual({ kind: "ready" });
    // 壊れた現像後は、選んだときにはじめてエラーが出て、現像前へ戻れる。
    const picked = run(ready, { type: "face/select", face: "after" });
    expect(viewerStatus(picked)).toEqual({ kind: "error", message: "現像後NG" });
    expect(viewerStatus(run(picked, { type: "face/select", face: "before" }))).toEqual({ kind: "ready" });
  });

  it("初期 before の失敗は face を動かさない", () => {
    const start = createViewerState(0, "before");
    const state = run(start, { type: "initial/failed", generation: 0, face: "before", message: "NG" });
    expect(state.face).toBe("before");
    expect(viewerStatus(state).kind).toBe("loading");
  });

  it("現像前の寸法が取れないときは、現像後が読めていても面を問わず致命エラー", () => {
    const state = run(readyAfter(), { type: "base/failed", generation: 0, message: "寸法NG" });
    expect(viewerStatus(state)).toEqual({ kind: "error", message: "寸法NG" });
    // 面を切り替えても消えない（基準グリッドが無い）。
    expect(viewerStatus(run(state, { type: "face/select", face: "before" }))).toEqual({
      kind: "error",
      message: "寸法NG",
    });
  });
});

describe("写真送りで遅い応答が現画像を汚さない", () => {
  const gen0 = createViewerState(0, "after");

  it("送った後に届いた前の写真の load / error / 寸法 / 初期失敗は無視される", () => {
    const moved = run(gen0, { type: "photo/go", index: 1, face: "before" });
    expect(moved.generation).toBe(1);
    const stale: ViewerAction[] = [
      { type: "base/loaded", generation: 0, size: BASE },
      { type: "base/failed", generation: 0, message: "前の写真のNG" },
      { type: "face/loaded", generation: 0, face: "before" },
      { type: "face/failed", generation: 0, face: "before", message: "前の写真のNG" },
      { type: "initial/failed", generation: 0, face: "after", message: "前の写真のNG" },
    ];
    for (const action of stale) expect(viewerReducer(moved, action)).toBe(moved);
    expect(viewerStatus(moved)).toEqual({ kind: "loading" });
  });

  it("前の写真の失敗が先に出ていても、送れば新しい写真はエラーを引き継がない", () => {
    const failed = run(readyAfter(), { type: "face/failed", generation: 0, face: "after", message: "NG" });
    const moved = run(failed, { type: "photo/go", index: 1, face: "after" });
    expect(moved.faceErrors).toEqual({ before: "", after: "" });
    expect(moved.baseError).toBe("");
    expect(viewerStatus(moved)).toEqual({ kind: "loading" });
  });

  it("A→B→A と戻っても、最初の A の応答は世代が違うので届かない", () => {
    const back = run(gen0, { type: "photo/go", index: 1, face: "before" }, { type: "photo/go", index: 0, face: "after" });
    expect(back.generation).toBe(2);
    expect(viewerReducer(back, { type: "base/loaded", generation: 0, size: BASE })).toBe(back);
  });

  it("同じ添字への送りは何もしない（世代も進めない）", () => {
    expect(viewerReducer(gen0, { type: "photo/go", index: 0, face: "after" })).toBe(gen0);
  });

  it("写真ごとの状態（面・長押し・mounted・倍率・パン）がすべて初期化される", () => {
    const zoomed = run(
      readyAfter(),
      { type: "peek/start" },
      { type: "zoom/to", point: { x: 600, y: 400 }, target: 2 },
      { type: "drag/start", id: 1, x: 10, y: 10 },
      { type: "drag/move", id: 1, x: 60, y: 30 },
    );
    expect(zoomed.view.scale).toBe(2);
    const moved = run(zoomed, { type: "photo/go", index: 1, face: "before" });
    expect(moved).toMatchObject({
      index: 1,
      face: "before",
      peek: false,
      base: null,
      mounted: { before: true, after: false },
      faces: { before: "loading", after: "idle" },
      view: { scale: 1, tx: 0, ty: 0 },
      drag: null,
    });
  });
});

describe("ドラッグ", () => {
  it("掴んだまま写真を送ると、前の写真の起点座標は次の写真のパンへ持ち越されない", () => {
    const ready = readyAfter();
    const dragging = run(
      ready,
      { type: "zoom/to", point: { x: 0, y: 0 }, target: 2 },
      { type: "drag/start", id: 7, x: 100, y: 100 },
    );
    const origin = dragging.drag;
    expect(origin).not.toBeNull();

    // 掴んだまま送る → 新しい写真が読み込まれて全体表示になる → 指を動かす。
    const next = run(
      dragging,
      { type: "photo/go", index: 1, face: "before" },
      { type: "base/loaded", generation: 1, size: BASE },
      { type: "stage/fit", stage: STAGE, initial: true },
    );
    const fitted = next.view;
    const moved = viewerReducer(next, { type: "drag/move", id: 7, x: 160, y: 130 });
    expect(moved.view).toEqual(fitted);
    expect(moved.drag).toBeNull();
    // 指を離したときの end も何も起こさない。
    expect(viewerReducer(next, { type: "drag/end", id: 7 })).toBe(next);
  });

  it("通常のドラッグは起点からの差分だけ動き、離すと止まる", () => {
    const ready = run(readyAfter(), { type: "zoom/to", point: { x: 0, y: 0 }, target: 2 });
    const { tx, ty } = ready.view;
    const dragged = run(
      ready,
      { type: "drag/start", id: 1, x: 100, y: 100 },
      { type: "drag/move", id: 1, x: 130, y: 90 },
    );
    expect(dragged.view).toMatchObject({ tx: tx + 30, ty: ty - 10, scale: 2 });
    const ended = run(dragged, { type: "drag/end", id: 1 }, { type: "drag/move", id: 1, x: 500, y: 500 });
    expect(ended.view).toEqual(dragged.view);
  });

  it("別のポインタの move / end は無視する", () => {
    const state = run(readyAfter(), { type: "drag/start", id: 1, x: 0, y: 0 });
    expect(viewerReducer(state, { type: "drag/move", id: 2, x: 9, y: 9 })).toBe(state);
    expect(viewerReducer(state, { type: "drag/end", id: 2 })).toBe(state);
  });

  it("寸法が入る前（キャンバスが無い間）はドラッグを始めない", () => {
    const state = createViewerState(0, "before");
    expect(viewerReducer(state, { type: "drag/start", id: 1, x: 0, y: 0 })).toBe(state);
  });
});

describe("zoom / pan の既存挙動", () => {
  it("同じ写真内の面切替・peek では倍率と位置を保つ", () => {
    const zoomed = run(readyAfter(), { type: "zoom/to", point: { x: 300, y: 200 }, target: 3 });
    const view = zoomed.view;
    const switched = run(zoomed, { type: "face/select", face: "before" }, { type: "face/select", face: "after" }, {
      type: "peek/start",
    });
    expect(switched.view).toBe(view);
  });

  it("全体表示の倍率はステージに収まる最小比で、中央に置く", () => {
    const state = readyAfter();
    expect(state.fit).toBeCloseTo(0.2);
    expect(state.view).toEqual(centeredView(0.2, BASE, STAGE));
  });

  it("倍率は fit 〜 MAX_SCALE にクランプされ、ポインタ下の点は動かない", () => {
    const state = readyAfter();
    const point = { x: 400, y: 300 };
    const zoomed = viewerReducer(state, { type: "zoom/at", point, factor: 2 });
    const { scale, tx, ty } = zoomed.view;
    expect(scale).toBeCloseTo(0.4);
    // 画像座標 (p - t) / s が不変。
    expect((point.x - tx) / scale).toBeCloseTo((point.x - state.view.tx) / state.view.scale);
    expect((point.y - ty) / scale).toBeCloseTo((point.y - state.view.ty) / state.view.scale);
    expect(viewerReducer(zoomed, { type: "zoom/to", point, target: 100 }).view.scale).toBe(8);
    expect(viewerReducer(state, { type: "zoom/at", point, factor: 0.5 })).toBe(state);
  });

  it("拡大中のリサイズは位置を保ち、全体表示より縮んだときだけ中央へ戻す", () => {
    const zoomed = run(readyAfter(), { type: "zoom/to", point: { x: 0, y: 0 }, target: 2 });
    const kept = viewerReducer(zoomed, { type: "stage/fit", stage: { width: 1000, height: 700 }, initial: false });
    expect(kept.view).toBe(zoomed.view);
    expect(kept.fit).toBeCloseTo(1000 / 6000);
    // ステージが広がって全体表示の倍率（0.2 → 0.25）が現在の倍率を上回った。
    const atFit = readyAfter();
    const refit = viewerReducer(atFit, { type: "stage/fit", stage: { width: 1500, height: 1000 }, initial: false });
    expect(refit.view).toEqual(centeredView(0.25, BASE, { width: 1500, height: 1000 }));
  });

  it("全体表示のままステージが縮んだら、新しい fit へ追従する（旧倍率で保持しない）", () => {
    const atFit = readyAfter(); // scale === fit === 0.2
    const small = { width: 900, height: 600 }; // 新 fit 0.15
    const shrunk = viewerReducer(atFit, { type: "stage/fit", stage: small, initial: false });
    expect(shrunk.fit).toBeCloseTo(0.15);
    expect(shrunk.view).toEqual(centeredView(shrunk.fit, BASE, small));
  });

  it("全体表示のままステージが広がったときも新しい fit へ追従する", () => {
    const wide = { width: 1500, height: 1000 };
    const grown = viewerReducer(readyAfter(), { type: "stage/fit", stage: wide, initial: false });
    expect(grown.view).toEqual(centeredView(0.25, BASE, wide));
  });

  it("全体表示とみなす判定は浮動小数の誤差を吸収し、実際に拡大した倍率は拡大中として保持する", () => {
    const atFit = readyAfter();
    const noisy = { ...atFit, view: { ...atFit.view, scale: atFit.fit * (1 + 1e-9) } };
    const small = { width: 900, height: 600 };
    const followed = viewerReducer(noisy, { type: "stage/fit", stage: small, initial: false });
    expect(followed.view).toEqual(centeredView(followed.fit, BASE, small));
    // 0.1% でも拡大していれば、ユーザー操作の結果として保持する。
    const zoomedSlightly = { ...atFit, view: { ...atFit.view, scale: atFit.fit * 1.001 } };
    expect(viewerReducer(zoomedSlightly, { type: "stage/fit", stage: small, initial: false }).view).toBe(
      zoomedSlightly.view,
    );
  });

  it("全体 / 原寸ボタン相当", () => {
    const zoomed = run(readyAfter(), { type: "zoom/to", point: { x: 600, y: 400 }, target: 1 });
    expect(zoomed.view.scale).toBe(1);
    const fit = viewerReducer(zoomed, { type: "view/fit", stage: STAGE });
    expect(fit.view).toEqual(centeredView(0.2, BASE, STAGE));
  });
});
