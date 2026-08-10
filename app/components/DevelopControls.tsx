import type { RawPreset } from "../lib/types";

type DevelopControlsProps = {
  engineStatus: "checking" | "ready" | "offline";
  engineVersion: string;
  rawPreset: RawPreset;
  onChangePreset: (preset: RawPreset) => void;
  isAccepted: boolean;
  onAccept: () => void;
  onQueueRaw: () => void;
  queueDisabled: boolean;
  isProcessing: boolean;
  isDeveloped: boolean;
};

export function DevelopControls({
  engineStatus,
  engineVersion,
  rawPreset,
  onChangePreset,
  isAccepted,
  onAccept,
  onQueueRaw,
  queueDisabled,
  isProcessing,
  isDeveloped,
}: DevelopControlsProps) {
  return (
    <>
      <div className="raw-controls">
        <div className="engine-row"><label htmlFor="raw-preset">現像プリセット</label><span className={`engine-badge ${engineStatus}`}><i />{engineStatus === "ready" ? `${engineVersion} 接続済み` : engineStatus === "checking" ? "確認中" : "ローカルエンジン停止中"}</span></div>
        <select id="raw-preset" value={rawPreset} onChange={(event) => onChangePreset(event.target.value as RawPreset)}><option value="neutral_recovery">階調回復 — 自然な明暗差</option><option value="landscape_clear">風景クリア — 彩度と輪郭</option><option value="nightscape">夜景ノイズ低減 — 高ISO向け</option></select>
      </div>
      <div className="inspector-actions operation-actions"><button className="primary-action" onClick={onAccept}>{isAccepted ? "✓ 採用済み" : "この写真を採用"}</button><button className="secondary-action" onClick={onQueueRaw} disabled={queueDisabled}>{isProcessing ? "現像中…" : isDeveloped ? "✓ 現像済み" : "RAW現像を実行"}</button></div>
    </>
  );
}
