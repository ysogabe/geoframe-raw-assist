import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import type { ViewerGallery } from "../lib/app-state";

type Face = "before" | "after";
type Size = { width: number; height: number };
/** ステージ座標系での画像の置き方。translate してから scale する（transform-origin は 0 0）。 */
type ViewState = { scale: number; tx: number; ty: number };

/** 拡大の上限。6000px 級の画像で 8 倍まで寄れれば粒状まで確認できる。 */
const MAX_SCALE = 8;
/** ホイールの deltaY にかける係数。exp を取るので、どの方向へも等比で動く。 */
const WHEEL_ZOOM_SPEED = 0.0018;
/** キーボード / ボタン 1 回あたりの倍率。 */
const ZOOM_STEP = 1.25;

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

/** 画像全体がステージに収まる倍率で中央に置く。 */
function centeredView(scale: number, base: Size, stage: Size): ViewState {
  return {
    scale,
    tx: (stage.width - base.width * scale) / 2,
    ty: (stage.height - base.height * scale) / 2,
  };
}

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

/** その 2 面で最初に見せる面。現像していなければ現像後は選べない。 */
function faceOf(item: { afterUrl?: string; initial: "before" | "after" }): Face {
  return item.afterUrl && item.initial === "after" ? "after" : "before";
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
  const prevRef = useRef<HTMLButtonElement>(null);
  const nextRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();

  const [index, setIndex] = useState(gallery.index);
  const current = gallery.items[index];
  const lastIndex = gallery.items.length - 1;
  const canStep = gallery.items.length > 1;

  const hasAfter = Boolean(current.afterUrl);
  // 現像していなければ切替コントロール自体を出さないので、初期面も before に倒す。
  const initialFace: Face = faceOf(current);

  const [face, setFace] = useState<Face>(initialFace);
  const [peek, setPeek] = useState(false);
  // 片面しか見ない使い方でメモリを払わないよう、2 枚目は必要になってから DOM に載せる。
  const [mounted, setMounted] = useState({
    before: initialFace === "before",
    after: initialFace === "after",
  });
  const [ready, setReady] = useState({ before: false, after: false });
  // 基準グリッドは常に現像前（カメラJPG）の実解像度。scale = 1 がそのまま原寸になり、
  // 倍率表示が嘘をつかない。プレビュー品質の現像後は引き伸ばされてぼやけるが、それが正直。
  const [base, setBase] = useState<Size | null>(null);
  const [failure, setFailure] = useState("");
  const [view, setView] = useState<ViewState>({ scale: 1, tx: 0, ty: 0 });
  const [fitScale, setFitScale] = useState(1);
  const [dragging, setDragging] = useState(false);

  // ホイール用のリスナーは一度だけ張るため、そこから読む値は ref に置く（古い値を掴まないように）。
  const fitRef = useRef(1);
  const dragRef = useRef<{ id: number; x: number; y: number; tx: number; ty: number } | null>(null);

  const shown: Face = peek ? "before" : face;
  const busy = !failure && (!base || !ready[shown]);
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
   * 新しい写真に前の写真の面・倍率・パン位置が乗った中間状態が見える。React は
   * ハンドラ内の setState を 1 コミットにまとめるので、ここで全部倒しておく。
   *
   * base を null に落とすとキャンバスごと外れ、読み込みスピナーに戻る。新しい寸法が
   * 入った時点で全体表示の計算（recompute(true)）が走るため、送るたびにズームは
   * 全体表示へ戻る（同一写真内の面切替では base を触らないので位置は保たれる）。
   */
  function goTo(nextIndex: number) {
    const clamped = clamp(nextIndex, 0, lastIndex);
    if (clamped === index) return;
    const nextFace = faceOf(gallery.items[clamped]);
    setIndex(clamped);
    setFace(nextFace);
    setPeek(false);
    setMounted({ before: nextFace === "before", after: nextFace === "after" });
    setReady({ before: false, after: false });
    setBase(null);
    setFailure("");
    // 全体表示の再計算は base が入ってからの effect（＝ペイント後）なので、ここで倒して
    // おかないと前の写真の倍率とパン位置で 1 フレーム描かれてしまう。
    setView({ scale: 1, tx: 0, ty: 0 });
  }

  function step(delta: 1 | -1) {
    const target = index + delta;
    if (target < 0 || target > lastIndex) return;
    goTo(target);
    // 端に着くと押していたボタンが disabled になる。フォーカスが <dialog> の外（body）へ
    // 落ちると onKeyDown が届かなくなり、以後キーボードで送れなくなるので先に移す。
    const edge = delta < 0 ? prevRef.current : nextRef.current;
    const reachedEdge = delta < 0 ? target === 0 : target === lastIndex;
    if (reachedEdge && document.activeElement === edge) {
      (delta < 0 ? nextRef : prevRef).current?.focus();
    }
  }

  // ---- 画像の読み込み ------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    const faceUrl = initialFace === "after" && current.afterUrl ? current.afterUrl : current.beforeUrl;

    void (async () => {
      try {
        const decoded = await decodeImage(faceUrl);
        // 現像後を先に見せる場合でも、基準グリッドは現像前の実解像度に固定する。
        // 見ない面のデコードは払いたくないので、そのときは寸法だけを測る。
        const measured = faceUrl === current.beforeUrl ? decoded : await measureImage(current.beforeUrl);
        if (cancelled) return;
        if (!measured.width || !measured.height) throw new Error("画像の寸法を取得できませんでした。");
        setBase(measured);
      } catch (error) {
        if (!cancelled) {
          setFailure(error instanceof Error ? error.message : "画像を読み込めませんでした。");
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [initialFace, current]);

  // ---- 全体表示の倍率（ステージ寸法が変わるたびに測り直す） ------------------
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage || !base) return;

    const recompute = (initial: boolean) => {
      const rect = stage.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const fit = Math.min(rect.width / base.width, rect.height / base.height);
      fitRef.current = fit;
      setFitScale(fit);
      setView((prev) => {
        // 拡大して見ている最中のリサイズでは倍率と位置を保つ。全体表示より縮んでいる
        // （＝リサイズで下限を割った）ときだけ、全体表示へ戻して中央に置き直す。
        if (!initial && prev.scale > fit) return prev;
        return centeredView(fit, base, { width: rect.width, height: rect.height });
      });
    };

    recompute(true);
    const observer = new ResizeObserver(() => recompute(false));
    observer.observe(stage);
    return () => observer.disconnect();
  }, [base]);

  // ---- 拡大縮小 ------------------------------------------------------------
  /** point（ステージ座標）の下にある画像上の点を動かさずに倍率を変える。 */
  const zoomAt = useCallback((point: { x: number; y: number }, factor: number) => {
    setView((prev) => {
      const scale = clamp(prev.scale * factor, fitRef.current, MAX_SCALE);
      if (scale === prev.scale) return prev;
      // t2 = p - (p - t) * (s2 / s) を x/y 独立に解く。
      const ratio = scale / prev.scale;
      return {
        scale,
        tx: point.x - (point.x - prev.tx) * ratio,
        ty: point.y - (point.y - prev.ty) * ratio,
      };
    });
  }, []);

  const stageCenter = useCallback(() => {
    const rect = stageRef.current?.getBoundingClientRect();
    return rect ? { x: rect.width / 2, y: rect.height / 2 } : null;
  }, []);

  const zoomFromCenter = useCallback(
    (factor: number) => {
      const point = stageCenter();
      if (point) zoomAt(point, factor);
    },
    [stageCenter, zoomAt],
  );

  /** 倍率を絶対値で指定する（原寸 / 200%）。固定点はステージ中心。 */
  const zoomTo = useCallback(
    (target: number) => {
      const point = stageCenter();
      if (!point) return;
      setView((prev) => {
        const scale = clamp(target, fitRef.current, MAX_SCALE);
        if (scale === prev.scale) return prev;
        const ratio = scale / prev.scale;
        return {
          scale,
          tx: point.x - (point.x - prev.tx) * ratio,
          ty: point.y - (point.y - prev.ty) * ratio,
        };
      });
    },
    [stageCenter],
  );

  const resetToFit = useCallback(() => {
    const stage = stageRef.current;
    if (!stage || !base) return;
    const rect = stage.getBoundingClientRect();
    setView(centeredView(fitRef.current, base, { width: rect.width, height: rect.height }));
  }, [base]);

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
      zoomAt({ x: event.clientX - rect.left, y: event.clientY - rect.top }, Math.exp(-delta * WHEEL_ZOOM_SPEED));
    };

    stage.addEventListener("wheel", handleWheel, { passive: false });
    return () => stage.removeEventListener("wheel", handleWheel);
  }, [zoomAt]);

  // ---- ドラッグ移動（setPointerCapture でステージ外へ出ても追従させる） -------
  function handlePointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    const stage = stageRef.current;
    if (!stage || !base) return;
    stage.setPointerCapture(event.pointerId);
    dragRef.current = { id: event.pointerId, x: event.clientX, y: event.clientY, tx: view.tx, ty: view.ty };
    setDragging(true);
  }

  function handlePointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag || drag.id !== event.pointerId) return;
    setView((prev) => ({
      ...prev,
      tx: drag.tx + (event.clientX - drag.x),
      ty: drag.ty + (event.clientY - drag.y),
    }));
  }

  function handlePointerUp(event: ReactPointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag || drag.id !== event.pointerId) return;
    dragRef.current = null;
    setDragging(false);
    const stage = stageRef.current;
    if (stage?.hasPointerCapture(event.pointerId)) stage.releasePointerCapture(event.pointerId);
  }

  // ---- 面の切替と長押し ----------------------------------------------------
  /** まだ DOM に載せていない面を先に載せておく。hover / focus の時点で温めて、初回の待ちを消す。 */
  const warm = useCallback(
    (target: Face) => {
      if (target === "after" && !hasAfter) return;
      setMounted((prev) => (prev[target] ? prev : { ...prev, [target]: true }));
    },
    [hasAfter],
  );

  function selectFace(next: Face) {
    if (next === "after" && !hasAfter) return;
    warm(next);
    setPeek(false);
    setFace(next);
  }

  function startPeek() {
    if (face === "before") return;
    warm("before");
    setPeek(true);
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
    setPeek(false);
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

  const markReady = (target: Face) =>
    setReady((prev) => (prev[target] ? prev : { ...prev, [target]: true }));

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
              <span className="preview-tag">プレビュー品質（長辺1600px）</span>
              <small>本現像すると原寸で比較できます</small>
            </p>
          )}

          <div className="viewer-controls">
            {/* 送りは輪に 2 枚以上あるときだけ。端では止まる（先頭↔末尾をつながない）。 */}
            {canStep && (
              <div className="viewer-nav" role="group" aria-label="前後の写真へ送る">
                <button
                  ref={prevRef}
                  type="button"
                  disabled={index === 0}
                  onClick={() => step(-1)}
                  aria-label="前の写真"
                >
                  ‹
                </button>
                <span className="viewer-count">
                  {index + 1} / {gallery.items.length}
                </span>
                <button
                  ref={nextRef}
                  type="button"
                  disabled={index === lastIndex}
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
                onPointerUp={() => setPeek(false)}
                onPointerLeave={() => setPeek(false)}
                onPointerCancel={() => setPeek(false)}
                onKeyDown={handlePeekKeyDown}
                onKeyUp={handlePeekKeyUp}
                onBlur={() => setPeek(false)}
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
          className={`viewer-stage${dragging ? " dragging" : ""}`}
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
                  onError={() => setFailure(`${current.beforeLabel}の画像を読み込めませんでした。`)}
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
                  onError={() => setFailure(`${current.afterLabel}の画像を読み込めませんでした。`)}
                />
              )}
            </div>
          )}

          {busy && (
            <div className="viewer-spinner">
              <span className="gate-mark running" aria-hidden="true" />
              <p>原寸を読み込んでいます…</p>
            </div>
          )}

          {failure && (
            <p className="viewer-error" role="alert">
              {failure}
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
