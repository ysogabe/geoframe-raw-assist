import { useEffect, useState } from "react";

/** 表示が安定したとみなすまでの待ち時間。←→ の連打で通過するだけの写真では原寸を取りに行かない。 */
const FULL_DELAY_MS = 150;

type Props = {
  /** 即座に出すサムネ（最大辺 640px）。 */
  thumbSrc: string;
  /** 安定後に差し替える原寸。 */
  fullSrc: string;
  alt: string;
};

/**
 * サムネを即表示し、150ms 安定したら原寸を先読みして同じ <img> の src だけ差し替える。
 *
 * 原寸を使う理由: 640px のサムネは約 900 CSS px（Retina で約 1800px）のカードに引き伸ばされてボケるため。
 * デバウンスの理由: 連打で通過する写真ごとに 6000x4000 の JPG を取りに行くと帯域と CPU を無駄にするため。
 * <img> は remount せず src だけ切り替える（チラつき・レイアウト移動の回避）。
 * 読み込み失敗時はサムネのまま何も出さない。
 */
export function ProgressiveImage({ thumbSrc, fullSrc, alt }: Props) {
  // どの fullSrc が読み込み済みかを覚える。写真が変わると一致しなくなり、自動的にサムネへ戻る。
  const [loadedFull, setLoadedFull] = useState<string | null>(null);

  useEffect(() => {
    const preload = new Image();
    const timer = window.setTimeout(() => {
      preload.onload = () => setLoadedFull(fullSrc);
      preload.onerror = () => {};
      preload.src = fullSrc;
    }, FULL_DELAY_MS);
    // 写真が変わったら保留中のタイマーと読み込みを破棄し、古い onload が新しい写真へ反映されないようにする。
    return () => {
      window.clearTimeout(timer);
      preload.onload = null;
      preload.onerror = null;
      preload.src = "";
    };
  }, [fullSrc]);

  return <img src={loadedFull === fullSrc ? fullSrc : thumbSrc} alt={alt} decoding="async" />;
}
