/**
 * 「画像の読み込みが落ち着いた」判定(純粋関数 + 注入可能な環境)。ブラウザ API に触らないので fake でテストできる。
 *
 * イベントが一定時間来ない(quiet)だけでは足りない。遅い要求(quiet より長くかかる読み込み)の最中は
 * load が来ない時間が続くので、quiet だけで settled にすると読み込み中のまま次の計測へ進み、画像が混入する。
 * そこで「要求済みで未完了の <img>」が 0 であることも settled の条件にする。
 */

export type ImgSnapshot = {
  complete: boolean;
  /** 解決済みのソース URL。要求前は空文字のことがある。 */
  currentSrc: string;
  lazy: boolean;
  rect: { top: number; bottom: number; left: number; right: number };
};

/** Chromium の lazy 読み込みはビューポート外 1250〜2500px 程度を先読みする。それより広めに取り、遠い未要求を除外する。 */
export const LAZY_LOAD_MARGIN_PX = 3000;

/**
 * 「要求済みで未完了」と見なす <img>。
 * - complete(読み込み済み・失敗済み)は除外。
 * - currentSrc が空(要求前)は除外。
 * - lazy は、ビューポート ± LAZY_LOAD_MARGIN_PX の外にあるなら、ブラウザが要求していないとして除外
 *   (currentSrc が先に埋まるエンジンでも、遠方の未読込を永久に待たない)。
 */
export function isPendingImage(img: ImgSnapshot, viewport: { width: number; height: number }): boolean {
  if (img.complete || img.currentSrc === "") return false;
  if (!img.lazy) return true;
  const { rect } = img;
  return (
    rect.bottom >= -LAZY_LOAD_MARGIN_PX &&
    rect.top <= viewport.height + LAZY_LOAD_MARGIN_PX &&
    rect.right >= -LAZY_LOAD_MARGIN_PX &&
    rect.left <= viewport.width + LAZY_LOAD_MARGIN_PX
  );
}

export type SettleEnv = {
  now(): number;
  sleep(ms: number): Promise<void>;
  /** 最後に <img> の load/error が来た時刻。 */
  lastImageEventAt(): number;
  /** 要求済みで未完了の <img> の数。 */
  pendingCount(): number;
};

export type SettleResult = {
  waited_ms: number;
  /** quiet を満たし、かつ pending が 0。timeout なら false(未完了を settled にしない)。 */
  settled: boolean;
  /** 打ち切り時(または終了時)に残っていた要求済み未完了の枚数。 */
  pending: number;
};

export async function waitUntilSettled(env: SettleEnv, timeoutMs: number, quietMs: number): Promise<SettleResult> {
  const begin = env.now();
  for (;;) {
    await env.sleep(Math.min(50, quietMs));
    const now = env.now();
    const pending = env.pendingCount();
    if (pending === 0 && now - Math.max(env.lastImageEventAt(), begin) >= quietMs) {
      return { waited_ms: now - begin, settled: true, pending: 0 };
    }
    if (now - begin >= timeoutMs) return { waited_ms: now - begin, settled: false, pending };
  }
}
