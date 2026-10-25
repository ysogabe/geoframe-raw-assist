import {
  useCallback,
  useEffect,
  useId,
  useReducer,
  useRef,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import type { ViewerGallery } from "../lib/app-state";
import {
  WHEEL_ZOOM_SPEED,
  ZOOM_STEP,
  clamp,
  createViewerState,
  faceOf,
  shownFace,
  viewerReducer,
  viewerStatus,
  type Face,
  type Size,
} from "../lib/viewer-state";

/**
 * 寸法だけを測る。
 * 表示しない面のデコード（6000×4000 の RGBA でおよそ 96MB）を払いたくないので、
 * decode() ではなく load を待つ。
 */
function measureImage(url: string): Promise<Size> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve({ width: image.naturalWidth, height: image.naturalHeight });
    image.onerror = () => reject(new Error("画像を読み込めませんでした。"));
    image.src = url;
  });
}

/**
 * 最初に見せる面。ペイントできる状態まで待ってからスピナーを消したいので decode() を待つ。
 * decode() は「まだ描けない」だけでも reject するため、寸法が取れていれば続行する。
 */
async function decodeImage(url: string): Promise<Size> {
  const image = new Image();
  image.src = url;
  try {
    await image.decode();
  } catch {
    if (!image.naturalWidth) throw new Error("画像を読み込めませんでした。");
  }
  return { width: image.naturalWidth, height: image.naturalHeight };
}

type FullSizeViewerProps = {
  gallery: ViewerGallery;
  onClose: () => void;
};

/**
 * アプリ内の原寸ビューア。
 *
 * デスクトップシェル（Tauri / WKWebView）には別タブで開くリンク（target=_blank）を
 * 処理する経路が無い。外部ブラウザへ逃がさずここで原寸を見せる。ネイティブの `<dialog>`
 * を `showModal()` で開くことで、Escape・フォーカストラップ・背景の inert・トップレイヤーを
 * ブラウザ実装に任せている（`open` 属性ではノンモーダルになり、`.toast` の z-index と競合する）。
 *
 * この機能の核は「拡大率と位置を保ったまま現像前後を切り替えられること」。そのため
 * 2 面は同じ transform ラッパの中に重ね、`visibility` だけで切り替える。アンマウントすると
 * 切り替えのたびに再デコードが走り、位置も飛ぶ。
 *
 * 前後の写真への送りは開いた時点のスナップショット（gallery）の添字を動かすだけで行う。
 * 送っても親の選択状態は動かないので、閉じれば開く前の画面がそのまま残る。
 */
export function FullSizeViewer({ gallery, onClose }: FullSizeViewerProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const titleId = useId();

  const [state, dispatch] = useReducer(viewerReducer, undefined, () =>
    createViewerState(gallery.index, faceOf(gallery.items[gallery.index])),
  );
  const { index, face, peek, mounted, base, view, fit: fitScale, generation } = state;
  const current = gallery.items[index];
  const lastIndex = gallery.items.length - 1;
  const canStep = gallery.items.length > 1;

  const hasAfter = Boolean(current.afterUrl);
  // 現像していなければ切替コントロール自体を出さないので、初期面も before に倒す。
  const initialFace: Face = faceOf(current);

  const shown: Face = shownFace(state);
  const status = viewerStatus(state);
  const devicePixels = typeof window === "undefined" ? 1 : window.devicePixelRatio || 1;

  // ---- ダイアログの開閉 ----------------------------------------------------
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (!dialog.open) dialog.showModal();

    // 背景（::backdrop）クリックで閉じる。React の onClick はルートへの委譲なので、
    // トップレイヤーに乗った <dialog> の背景クリックが届くかがエンジン依存になる。
    // ここだけは要素へ直接張る。
    // あわせて「押した場所も背景か」を見る。ステージ内で掴んで背景で離したドラッグは
    // 合成クリックの target が <dialog> になり、パンの途中で閉じてしまうため。
    let pressedBackdrop = false;
    const onPointerDown = (event: Event) => {
      pressedBackdrop = event.target === dialog;
    };
    const onClick = (event: Event) => {
      if (pressedBackdrop && event.target === dialog) dialog.close();
    };
    dialog.addEventListener("pointerdown", onPointerDown);
    dialog.addEventListener("click", onClick);

    return () => {
      dialog.removeEventListener("pointerdown", onPointerDown);
      dialog.removeEventListener("click", onClick);
      // close() は呼ばない。close イベントはタスクとして非同期に飛ぶため、StrictMode の
      // 二重実行で「開き直した直後に閉じる」経路ができてしまう。実際の閉じる操作
      // （Escape / 背景クリック / 閉じるボタン）は必ず close() を通るので、
      // フォーカスの復帰はそちらで担保されている。
    };
  }, []);

  const requestClose = useCallback(() => {
    // onClose を直接呼ばず close() を通す。フォーカスを起動ボタンへ戻すのはブラウザの仕事。
    dialogRef.current?.close();
  }, []);

  // ---- 前後の写真へ送る ----------------------------------------------------
  /**
   * 送り先へ移り、写真に紐づく state を **同じイベントハンドラの中で** すべて初期化する。
   *
   * useEffect([index]) でリセットすると「添字だけ進んだ 1 フレーム」がペイントされ、
   * 新しい写真に前の写真の面・倍率・パン位置が乗った中間状態が見える。1 回の dispatch
   * （reducer の photo/go）で面・読み込み状態・ドラッグ・倍率をまとめて倒す。
   *
   * base を null に落とすとキャンバスごと外れ、読み込みスピナーに戻る。新しい寸法が
   * 入った時点で全体表示の計算（stage/fit）が走るため、送るたびにズームは
   * 全体表示へ戻る（同一写真内の面切替では base を触らないので位置は保たれる）。
   */
  function goTo(nextIndex: number) {
    const clamped = clamp(nextIndex, 0, lastIndex);
    if (clamped === index) return;
    // ポインタは stage に捕捉されたままになる。掴んだまま送った場合に備えて手放しておく。
    releaseDragCapture();
    dispatch({ type: "photo/go", index: clamped, face: faceOf(gallery.items[clamped]) });
  }

  function step(delta: 1 | -1) {
    const target = index + delta;
    if (target < 0 || target > lastIndex) return;
    goTo(target);
  }

  // ---- 画像の読み込み ------------------------------------------------------
  useEffect(() => {
    // 写真送りで effect が張り替わったら cancelled で打ち切る。取りこぼした応答も
    // generation が合わなければ reducer が捨てる。
    let cancelled = false;
    const showAfterFirst = initialFace === "after" && Boolean(current.afterUrl);

    void (async () => {
      // 最初に見せる面を先にデコードする。現像後が壊れていても、現像前が読めるなら
      // 基準グリッドは作れるので、その面を落として現像前から始める。
      if (showAfterFirst && current.afterUrl) {
        try {
          await decodeImage(current.afterUrl);
        } catch {
          if (cancelled) return;
          dispatch({
            type: "initial/failed",
            generation,
            face: "after",
            message: `${current.afterLabel}の画像を読み込めませんでした。`,
          });
        }
        if (cancelled) return;
      }

      // 基準グリッドは常に現像前の実解像度に固定する。現像前を最初に見せるときは
      // デコードまで待ち、そうでなければ寸法だけを測る（見ない面のデコードは払わない）。
      try {
        const measured = showAfterFirst ? await measureImage(current.beforeUrl) : await decodeImage(current.beforeUrl);
        if (cancelled) return;
        if (!measured.width || !measured.height) {
          dispatch({
            type: "base/failed",
            generation,
            message: `${current.beforeLabel}の画像の寸法を取得できませんでした。`,
          });
          return;
        }
        dispatch({ type: "base/loaded", generation, size: measured });
      } catch {
        if (cancelled) return;
        dispatch({
          type: "base/failed",
          generation,
          message: `${current.beforeLabel}の画像を読み込めませんでした。原寸の基準にする寸法が取れないため表示できません。`,
        });
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [initialFace, current, generation]);

  // ---- 全体表示の倍率（ステージ寸法が変わるたびに測り直す） ------------------
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage || !base) return;

    const recompute = (initial: boolean) => {
      const rect = stage.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      dispatch({ type: "stage/fit", stage: { width: rect.width, height: rect.height }, initial });
    };

    recompute(true);
    const observer = new ResizeObserver(() => recompute(false));
    observer.observe(stage);
    return () => observer.disconnect();
  }, [base]);

  // ---- 拡大縮小 ------------------------------------------------------------
  const stageCenter = useCallback(() => {
    const rect = stageRef.current?.getBoundingClientRect();
    return rect ? { x: rect.width / 2, y: rect.height / 2 } : null;
  }, []);

  const zoomFromCenter = useCallback(
    (factor: number) => {
      const point = stageCenter();
      if (point) dispatch({ type: "zoom/at", point, factor });
    },
    [stageCenter],
  );

  /** 倍率を絶対値で指定する（原寸 / 200%）。固定点はステージ中心。 */
  const zoomTo = useCallback(
    (target: number) => {
      const point = stageCenter();
      if (point) dispatch({ type: "zoom/to", point, target });
    },
    [stageCenter],
  );

  const resetToFit = useCallback(() => {
    const rect = stageRef.current?.getBoundingClientRect();
    if (rect) dispatch({ type: "view/fit", stage: { width: rect.width, height: rect.height } });
  }, []);

  // React の onWheel はルート側に passive で張られるため preventDefault が効かない
  // （＝背景のページごとスクロールしてしまう）。ホイールだけは自前で non-passive に張る。
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;

    const handleWheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = stage.getBoundingClientRect();
      // deltaMode が行単位のブラウザでは deltaY が 1 桁小さいので揃えておく。
      const delta = event.deltaMode === 1 ? event.deltaY * 16 : event.deltaY;
      dispatch({
        type: "zoom/at",
        point: { x: event.clientX - rect.left, y: event.clientY - rect.top },
        factor: Math.exp(-delta * WHEEL_ZOOM_SPEED),
      });
    };

    stage.addEventListener("wheel", handleWheel, { passive: false });
    return () => stage.removeEventListener("wheel", handleWheel);
  }, []);

  // ---- ドラッグ移動（setPointerCapture でステージ外へ出ても追従させる） -------
  function releaseDragCapture() {
    const stage = stageRef.current;
    const dragId = state.drag?.id;
    if (stage && dragId !== undefined && stage.hasPointerCapture(dragId)) stage.releasePointerCapture(dragId);
  }

  function handlePointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    const stage = stageRef.current;
    if (!stage || !base) return;
    stage.setPointerCapture(event.pointerId);
    dispatch({ type: "drag/start", id: event.pointerId, x: event.clientX, y: event.clientY });
  }

  function handlePointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    // ドラッグしていないマウス移動で再描画を起こさない。
    if (state.drag?.id !== event.pointerId) return;
    dispatch({ type: "drag/move", id: event.pointerId, x: event.clientX, y: event.clientY });
  }

  function handlePointerUp(event: ReactPointerEvent<HTMLDivElement>) {
    if (state.drag?.id !== event.pointerId) return;
    dispatch({ type: "drag/end", id: event.pointerId });
    const stage = stageRef.current;
    if (stage?.hasPointerCapture(event.pointerId)) stage.releasePointerCapture(event.pointerId);
  }

  // ---- 面の切替と長押し ----------------------------------------------------
  /** まだ DOM に載せていない面を先に載せておく。hover / focus の時点で温めて、初回の待ちを消す。 */
  const warm = useCallback(
    (target: Face) => {
      if (target === "after" && !hasAfter) return;
      dispatch({ type: "face/warm", face: target });
    },
    [hasAfter],
  );

  function selectFace(next: Face) {
    if (next === "after" && !hasAfter) return;
    dispatch({ type: "face/select", face: next });
  }

  function startPeek() {
    dispatch({ type: "peek/start" });
  }

  function handlePeekKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>) {
    if (event.key !== " " && event.key !== "Enter") return;
    // Space の既定動作（クリック発火とページスクロール）を止めて、押している間だけの表示にする。
    event.preventDefault();
    startPeek();
  }

  function handlePeekKeyUp(event: ReactKeyboardEvent<HTMLButtonElement>) {
    if (event.key !== " " && event.key !== "Enter") return;
    event.preventDefault();
    dispatch({ type: "peek/end" });
  }

  // ---- キーボード ----------------------------------------------------------
  function handleKeyDown(event: ReactKeyboardEvent<HTMLDialogElement>) {
    // Escape は <dialog> が cancel → close と処理するのでここでは拾わない。
    switch (event.key) {
      case "+":
      case "=":
        event.preventDefault();
        zoomFromCenter(ZOOM_STEP);
        break;
      case "-":
      case "_":
        event.preventDefault();
        zoomFromCenter(1 / ZOOM_STEP);
        break;
      case "0":
        event.preventDefault();
        resetToFit();
        break;
      case "1":
        event.preventDefault();
        zoomTo(1);
        break;
      // 左右は写真送り、上下は面の切替。左右の方が「次の写真」の連想が強く、
      // 送りは 1 グループを見る間に何十回も使うのでこちらを主に割り当てる。
      case "ArrowLeft":
        if (!canStep) break;
        event.preventDefault();
        step(-1);
        break;
      case "ArrowRight":
        if (!canStep) break;
        event.preventDefault();
        step(1);
        break;
      case "ArrowUp":
        if (!hasAfter) break;
        event.preventDefault();
        selectFace("before");
        break;
      case "ArrowDown":
        if (!hasAfter) break;
        event.preventDefault();
        selectFace("after");
        break;
      default:
        break;
    }
  }

  const markReady = (target: Face) => dispatch({ type: "face/loaded", generation, face: target });
  const markFailed = (target: Face, label: string) =>
    dispatch({ type: "face/failed", generation, face: target, message: `${label}の画像を読み込めませんでした。` });

  /**
   * ブラウザキャッシュから即座に埋まった画像は、React が onLoad を張るより先に
   * 読み終わっていることがある。その場合 onLoad は二度と飛ばず、スピナーが残り続ける。
   * mount 時点で complete なら、そこで読み込み済みとして扱う。
   */
  const readyRefFor = (target: Face) => (node: HTMLImageElement | null) => {
    if (node?.complete && node.naturalWidth) markReady(target);
  };

  return (
    <dialog
      ref={dialogRef}
      className="viewer-dialog"
      aria-labelledby={titleId}
      onClose={onClose}
      onKeyDown={handleKeyDown}
    >
      <div className="viewer-body">
        <header className="viewer-head">
          <div className="viewer-heading">
            <p className="eyebrow">FULL SIZE</p>
            <h2 id={titleId}>{current.title}</h2>
          </div>

          {current.preview && (
            <p className="viewer-quality">
              <span className="preview-tag">
                {current.previewEdge ? `プレビュー品質（長辺${current.previewEdge}px）` : "プレビュー品質"}
              </span>
              <small>本現像すると原寸で比較できます</small>
            </p>
          )}

          <div className="viewer-controls">
            {/* 送りは輪に 2 枚以上あるときだけ。端では止まる（先頭↔末尾をつながない）。
                端で disabled にはしない。押していたボタンがフォーカスを失うと <dialog> の外
                （body）へ落ち、onKeyDown が届かなくなってキーボードで送れなくなるため。
                実際の無効化は step() 冒頭の範囲外 early return が担う。 */}
            {canStep && (
              <div className="viewer-nav" role="group" aria-label="前後の写真へ送る">
                <button
                  type="button"
                  aria-disabled={index === 0 ? "true" : undefined}
                  onClick={() => step(-1)}
                  aria-label="前の写真"
                >
                  ‹
                </button>
                <span className="viewer-count">
                  {index + 1} / {gallery.items.length}
                </span>
                <button
                  type="button"
                  aria-disabled={index === lastIndex ? "true" : undefined}
                  onClick={() => step(1)}
                  aria-label="次の写真"
                >
                  ›
                </button>
              </div>
            )}

            {hasAfter && (
              <div className="viewer-switch" role="group" aria-label="表示する面">
                <button
                  type="button"
                  className={face === "before" ? "active" : ""}
                  aria-pressed={face === "before"}
                  onPointerEnter={() => warm("before")}
                  onFocus={() => warm("before")}
                  onClick={() => selectFace("before")}
                >
                  {current.beforeLabel}
                </button>
                <button
                  type="button"
                  className={face === "after" ? "active" : ""}
                  aria-pressed={face === "after"}
                  onPointerEnter={() => warm("after")}
                  onFocus={() => warm("after")}
                  onClick={() => selectFace("after")}
                >
                  {current.afterLabel}
                </button>
              </div>
            )}

            {hasAfter && (
              <button
                type="button"
                className={`viewer-peek${peek ? " active" : ""}`}
                disabled={face === "before"}
                aria-pressed={peek}
                aria-label={`長押ししている間だけ${current.beforeLabel}を表示`}
                onPointerEnter={() => warm("before")}
                onFocus={() => warm("before")}
                onPointerDown={startPeek}
                onPointerUp={() => dispatch({ type: "peek/end" })}
                onPointerLeave={() => dispatch({ type: "peek/end" })}
                onPointerCancel={() => dispatch({ type: "peek/end" })}
                onKeyDown={handlePeekKeyDown}
                onKeyUp={handlePeekKeyUp}
                onBlur={() => dispatch({ type: "peek/end" })}
              >
                長押しで見比べ
              </button>
            )}

            <div className="viewer-zoom" role="group" aria-label="拡大率">
              <button type="button" onClick={() => zoomFromCenter(1 / ZOOM_STEP)} aria-label="縮小">
                −
              </button>
              <span className="viewer-scale">{Math.round(view.scale * 100)}%</span>
              <button type="button" onClick={() => zoomFromCenter(ZOOM_STEP)} aria-label="拡大">
                ＋
              </button>
              <button type="button" onClick={resetToFit} aria-label="全体表示">
                全体
              </button>
              <button type="button" onClick={() => zoomTo(1)} aria-label="原寸（100%）">
                原寸
              </button>
              <button type="button" onClick={() => zoomTo(2)} aria-label="200%">
                200%
              </button>
            </div>

            <button type="button" className="viewer-close" onClick={requestClose} aria-label="原寸ビューアを閉じる">
              ×
            </button>
          </div>
        </header>

        <div
          ref={stageRef}
          className={`viewer-stage${state.drag ? " dragging" : ""}`}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          onPointerCancel={handlePointerUp}
        >
          {base && (
            <div
              className="viewer-canvas"
              style={{
                width: `${base.width}px`,
                height: `${base.height}px`,
                transform: `translate3d(${view.tx}px, ${view.ty}px, 0) scale(${view.scale})`,
              }}
            >
              {mounted.before && (
                <img
                  className="viewer-face"
                  src={current.beforeUrl}
                  alt={`${current.title} ${current.beforeLabel}`}
                  style={{ visibility: shown === "before" ? "visible" : "hidden" }}
                  draggable={false}
                  decoding="async"
                  ref={readyRefFor("before")}
                  onLoad={() => markReady("before")}
                  onError={() => markFailed("before", current.beforeLabel)}
                />
              )}
              {mounted.after && current.afterUrl && (
                <img
                  className="viewer-face"
                  src={current.afterUrl}
                  alt={`${current.title} ${current.afterLabel}`}
                  style={{ visibility: shown === "after" ? "visible" : "hidden" }}
                  draggable={false}
                  decoding="async"
                  ref={readyRefFor("after")}
                  onLoad={() => markReady("after")}
                  onError={() => markFailed("after", current.afterLabel)}
                />
              )}
            </div>
          )}

          {status.kind === "loading" && (
            <div className="viewer-spinner">
              <span className="gate-mark running" aria-hidden="true" />
              <p>原寸を読み込んでいます…</p>
            </div>
          )}

          {status.kind === "error" && (
            <p className="viewer-error" role="alert">
              {status.message}
            </p>
          )}
        </div>

        <footer className="viewer-foot">
          <p>
            ドラッグで移動・ホイールでポインタ位置を中心に拡大縮小。
            <kbd>+</kbd> <kbd>-</kbd> 拡大縮小 / <kbd>0</kbd> 全体 / <kbd>1</kbd> 原寸
            {canStep && (
              <>
                {" / "}
                <kbd>←</kbd> <kbd>→</kbd> 前後の写真
              </>
            )}
            {hasAfter && (
              <>
                {" / "}
                <kbd>↑</kbd> <kbd>↓</kbd> 現像前後の切替
              </>
            )}
            {" / "}
            <kbd>Esc</kbd> 閉じる
          </p>
          <p className="viewer-note">
            原寸（100%）は「画像1px = CSS 1px」です。この画面は {devicePixels.toFixed(1)}倍表示なので、
            実ピクセル等倍は {Math.round(devicePixels * 100)}% にあたります。全体表示は{" "}
            {Math.round(fitScale * 100)}%。
          </p>
        </footer>
      </div>
    </dialog>
  );
}
