import { useState } from "react";
import {
  coreImageErrorMessage,
  coreImageUrl,
  developCoreImage,
  type CoreImageResult,
} from "../lib/coreimage-api";
import { sourceJpgUrl } from "../lib/raw-api";
import type { CaptureId } from "../lib/types";

type CoreImageExperimentProps = {
  captureId: CaptureId;
  /** 既存エンジン（rawpy）の現像結果 URL。まだ現像していなければ undefined。 */
  rawpyUrl: string | undefined;
};

function formatMs(ms: number | undefined): string {
  return ms === undefined ? "—" : `${(ms / 1000).toFixed(2)}秒`;
}

/**
 * Core Image 試験現像（Issue #65）。フラグ有効時だけ OperationPanel が描画する。
 *
 * 状態はこのコンポーネントのローカル state だけで完結させ、既存の reducer には載せない。
 * 呼び出し側が `key={captureId}` で描画するので、カットを切り替えると state ごと作り直され、
 * 前のカットの結果やエラーが残らない（実行中に切り替えた場合の応答は捨てられた
 * インスタンスに届くだけで画面には出ない）。
 */
export function CoreImageExperiment({ captureId, rawpyUrl }: CoreImageExperimentProps) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<CoreImageResult | null>(null);
  const [error, setError] = useState("");

  async function run() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      setResult(await developCoreImage(captureId, "preview"));
    } catch (caught) {
      setResult(null);
      setError(coreImageErrorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="coreimage-experiment" aria-label="Core Image 試験現像">
      <div className="coreimage-head">
        <div>
          <span className="preview-tag">試験機能</span>
          <strong>Core Image 試験現像</strong>
          <small>macOS 標準の RAW 現像（CIRAWFilter）で {captureId} をプレビュー現像して比較します</small>
        </div>
        <button type="button" className="secondary-action" disabled={busy} onClick={() => void run()}>
          {busy ? "Core Image で現像中…" : "Core Image 試験現像"}
        </button>
      </div>

      {error && (
        <p className="coreimage-error" role="alert">
          {error}
        </p>
      )}

      {result && (
        <>
          <div className="coreimage-grid">
            <figure>
              <div className="comparison-image">
                <img src={sourceJpgUrl(captureId)} alt={`${captureId} カメラ内JPG`} decoding="async" />
              </div>
              <figcaption><strong>カメラ JPEG</strong></figcaption>
            </figure>
            <figure>
              <div className="comparison-image">
                {rawpyUrl ? (
                  <img src={rawpyUrl} alt={`${captureId} rawpy 現像`} decoding="async" />
                ) : (
                  <span className="coreimage-empty">未現像（RAW現像を実行すると並びます）</span>
                )}
              </div>
              <figcaption><strong>rawpy（現行）</strong></figcaption>
            </figure>
            <figure>
              <div className="comparison-image">
                <img src={coreImageUrl(result)} alt={`${captureId} Core Image 現像`} decoding="async" />
              </div>
              <figcaption><strong>Core Image</strong></figcaption>
            </figure>
          </div>
          <div className="developed-meta">
            <span>{result.width} × {result.height}px</span>
            <span>decoder_version {result.decoder_version} / CLI {result.cli_version}</span>
            <span>
              {result.cache_hit ? "保存済み結果" : "今回現像"}・合計 {formatMs(result.elapsed_ms?.total)}
              （デコード {formatMs(result.elapsed_ms?.decode)} / 描画・保存 {formatMs(result.elapsed_ms?.render_encode)}）
            </span>
          </div>
        </>
      )}
    </section>
  );
}
