import { useCallback, useEffect, useId, useRef, useState, type PointerEvent, type KeyboardEvent } from "react";
import type { ViewerSource } from "../lib/app-state";

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

type FullSizeViewerProps = {
  source: ViewerSource;
  onClose: () => void;
};

/**
 * アプリ内の原寸ビューア。
 *
 * デスクトップシェル（Tauri / WKWebView）には `target="_blank"` を処理する経路が無いため、
 * 外部ブラウザへ逃がさずここで原寸を見せる。ネイティブの `<dialog>` を `showModal()` で
 * 開くことで、Escape・フォーカストラップ・背景の inert・トップレイヤーをブラウザ実装に
 * 任せている（`open` 属性ではノンモーダルになり、`.toast` の z-index と競合する）。
 *
 * この機能の核は「拡大率と位置を保ったまま現像前後を切り替えられること」。そのため
 * 2 面は同じ transform ラッパの中に重ね、`visibility` だけで切り替える。アンマウントすると
 * 切り替えのたびに再デコードが走り、位置も飛ぶ。
 */
export function FullSizeViewer({ source, onClose }: FullSizeViewerProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const titleId = useId();

  const hasAfter = Boolean(source.afterUrl);
  // 現像していなければ切替コントロール自体を出さないので、初期面も before に倒す。
  const initialFace: Face = hasAfter && source.initial === "after" ? "after" : "before";

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
    if (dialog && !dialog.open) dialog.showModal();
    // アンマウント時に close() は呼ばない。close イベントはタスクとして非同期に飛ぶため、
    // StrictMode の二重実行で「開き直した直後に閉じる」経路ができてしまう。
    // 実際の閉じる操作（Escape / 背景クリック / 閉じるボタン）は必ず close() を通るので、
    // フォーカスの復帰はそちらで担保されている。
  }, []);

  const requestClose = useCallback(() => {
    // onClose を直接呼ばず close() を通す。フォーカスを起動ボタンへ戻すのはブラウザの仕事。
    dialogRef.current?.close();
  }, []);

  // ---- 画像の読み込み ------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    const faceUrl = initialFace === "after" && source.afterUrl ? source.afterUrl : source.beforeUrl;

    void (async () => {
      try {
        const decoded = await decodeImage(faceUrl);
        // 現像後を先に見せる場合でも、基準グリッドは現像前の実解像度に固定する。
        // 見ない面のデコードは払いたくないので、そのときは寸法だけを測る。
        const measured = faceUrl === source.beforeUrl ? decoded : await measureImage(source.beforeUrl);
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
  }, [initialFace, source.beforeUrl, source.afterUrl]);

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
  function handlePointerDown(event: PointerEvent<HTMLDivElement>) {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    const stage = stageRef.current;
    if (!stage || !base) return;
    stage.setPointerCapture(event.pointerId);
    dragRef.current = { id: event.pointerId, x: event.clientX, y: event.clientY, tx: view.tx, ty: view.ty };
    setDragging(true);
  }

  function handlePointerMove(event: PointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag || drag.id !== event.pointerId) return;
    setView((prev) => ({
      ...prev,
      tx: drag.tx + (event.clientX - drag.x),
      ty: drag.ty + (event.clientY - drag.y),
    }));
  }

  function handlePointerUp(event: PointerEvent<HTMLDivElement>) {
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

  function handlePeekKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key !== " " && event.key !== "Enter") return;
    // Space の既定動作（クリック発火とページスクロール）を止めて、押している間だけの表示にする。
    event.preventDefault();
    startPeek();
  }

  function handlePeekKeyUp(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key !== " " && event.key !== "Enter") return;
    event.preventDefault();
    setPeek(false);
  }

  // ---- キーボード ----------------------------------------------------------
  function handleKeyDown(event: KeyboardEvent<HTMLDialogElement>) {
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
      case "ArrowLeft":
        if (!hasAfter) break;
        event.preventDefault();
        selectFace("before");
        break;
      case "ArrowRight":
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

  return (
    <dialog
      ref={dialogRef}
      className="viewer-dialog"
      aria-labelledby={titleId}
      onClose={onClose}
      onKeyDown={handleKeyDown}
      onClick={(event) => {
        // ::backdrop へのクリックは <dialog> 自身が target になる。中身は別要素なので誤爆しない。
        if (event.target === dialogRef.current) requestClose();
      }}
    >
      <div className="viewer-body">
        <header className="viewer-head">
          <div className="viewer-heading">
            <p className="eyebrow">FULL SIZE</p>
            <h2 id={titleId}>{source.title}</h2>
          </div>

          {source.preview && (
            <p className="viewer-quality">
              <span className="preview-tag">プレビュー品質（長辺1600px）</span>
              <small>本現像すると原寸で比較できます</small>
            </p>
          )}

          <div className="viewer-controls">
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
                  {source.beforeLabel}
                </button>
                <button
                  type="button"
                  className={face === "after" ? "active" : ""}
                  aria-pressed={face === "after"}
                  onPointerEnter={() => warm("after")}
                  onFocus={() => warm("after")}
                  onClick={() => selectFace("after")}
                >
                  {source.afterLabel}
                </button>
              </div>
            )}

            {hasAfter && (
              <button
                type="button"
                className={`viewer-peek${peek ? " active" : ""}`}
                disabled={face === "before"}
                aria-pressed={peek}
                aria-label={`長押ししている間だけ${source.beforeLabel}を表示`}
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
                  src={source.beforeUrl}
                  alt={`${source.title} ${source.beforeLabel}`}
                  style={{ visibility: shown === "before" ? "visible" : "hidden" }}
                  draggable={false}
                  decoding="async"
                  onLoad={() => markReady("before")}
                  onError={() => setFailure(`${source.beforeLabel}の画像を読み込めませんでした。`)}
                />
              )}
              {mounted.after && source.afterUrl && (
                <img
                  className="viewer-face"
                  src={source.afterUrl}
                  alt={`${source.title} ${source.afterLabel}`}
                  style={{ visibility: shown === "after" ? "visible" : "hidden" }}
                  draggable={false}
                  decoding="async"
                  onLoad={() => markReady("after")}
                  onError={() => setFailure(`${source.afterLabel}の画像を読み込めませんでした。`)}
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
            {hasAfter && (
              <>
                {" / "}
                <kbd>←</kbd> <kbd>→</kbd> 面の切替
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
