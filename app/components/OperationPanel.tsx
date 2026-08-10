import type { RefObject } from "react";
import type { DevelopedResult, Group, RawPreset, Shot } from "../lib/types";
import { rawClass } from "../lib/types";
import { DevelopControls } from "./DevelopControls";
import { BeforeAfter } from "./BeforeAfter";

type OperationPanelProps = {
  selectedGroup: Group;
  selectedShot: Shot;
  selectedShots: Record<number, string>;
  onChooseShot: (groupId: number, shotId: string) => void;
  engineStatus: "checking" | "ready" | "offline";
  engineVersion: string;
  rawPreset: RawPreset;
  onChangePreset: (preset: RawPreset) => void;
  accepted: number[];
  onAccept: () => void;
  onQueueRaw: () => void;
  processingCapture: string;
  developed: Record<string, DevelopedResult>;
  comparisonRef: RefObject<HTMLDivElement | null>;
};

export function OperationPanel({
  selectedGroup,
  selectedShot,
  selectedShots,
  onChooseShot,
  engineStatus,
  engineVersion,
  rawPreset,
  onChangePreset,
  accepted,
  onAccept,
  onQueueRaw,
  processingCapture,
  developed,
  comparisonRef,
}: OperationPanelProps) {
  return (
    <section className="operation-screen screen-enter" aria-label="写真の操作パネル">
      <div className="operation-progress"><span className="done">✓</span><div><strong>候補を選択</strong><small>{selectedGroup.shots.length}枚から比較</small></div><i /><span className="current">2</span><div><strong>仕上げを決定</strong><small>採用またはRAW現像</small></div></div>

      <div className="operation-layout">
        <div className="operation-media-card">
          <div className="media-heading"><div><p className="eyebrow">SELECTED PHOTO</p><h2>{selectedShot.id}</h2></div><span className={rawClass(selectedGroup.raw)}>{selectedGroup.rawLabel}</span></div>
          <div className="selected-preview operation-preview">
            <img src={selectedShot.image} alt={`選択中 ${selectedShot.id}`} />
            <div className="preview-grade"><small>総合</small><strong>{selectedShot.score}</strong></div>
            <div className="filename">{selectedShot.id}.JPG</div>
          </div>
          <div className="shot-picker operation-shot-picker" role="radiogroup" aria-label="代表写真を選択">
            {selectedGroup.shots.map((shot) => (
              <button key={shot.id} className={selectedShots[selectedGroup.id] === shot.id ? "active" : ""} onClick={() => onChooseShot(selectedGroup.id, shot.id)} role="radio" aria-checked={selectedShots[selectedGroup.id] === shot.id}>
                <img src={shot.image} alt={shot.id} />
                {shot.label && <span>{shot.label}</span>}
                <small>{shot.id}</small>
              </button>
            ))}
          </div>
        </div>

        <aside className="operation-controls-card">
          <div className="controls-heading"><p className="eyebrow">DECISION</p><h2>この写真をどう仕上げますか？</h2></div>
          <div className="analysis-card">
            <div className="analysis-title"><span className="spark">✦</span><strong>この一枚をおすすめ</strong><small>信頼度 {selectedShot.score}%</small></div>
            <p>{selectedGroup.reason}</p>
            <div className="score-row"><span>シャープネス</span><div><i style={{ width: `${selectedShot.sharpness}%` }} /></div><strong>{selectedShot.sharpness}</strong></div>
            <div className="score-row"><span>階調の保持</span><div><i style={{ width: `${selectedShot.highlight}%` }} /></div><strong>{selectedShot.highlight}</strong></div>
            <div className="score-row"><span>構図</span><div><i style={{ width: `${selectedShot.score - 3}%` }} /></div><strong>{selectedShot.score - 3}</strong></div>
          </div>
          <div className={`raw-callout ${selectedGroup.raw === "strong" ? "urgent" : ""}`}><span className="raw-icon">R</span><div><strong>{selectedGroup.rawLabel}</strong><p>{selectedGroup.raw === "none" ? "JPEGの階調が十分に残っています。" : selectedGroup.raw === "strong" ? "夜景・高ISO・深い暗部を検出。ノイズ低減と色調整の効果が期待できます。" : "暗部とハイライトに調整余地があります。"}</p></div></div>
          <DevelopControls
            engineStatus={engineStatus}
            engineVersion={engineVersion}
            rawPreset={rawPreset}
            onChangePreset={onChangePreset}
            isAccepted={accepted.includes(selectedGroup.id)}
            onAccept={onAccept}
            onQueueRaw={onQueueRaw}
            queueDisabled={selectedGroup.raw === "none" || processingCapture === selectedShot.id}
            isProcessing={processingCapture === selectedShot.id}
            isDeveloped={Boolean(developed[selectedShot.id])}
          />
          <p className="safety-note">元のJPG / ARWは変更されません</p>
        </aside>
      </div>

      <BeforeAfter shotId={selectedShot.id} result={developed[selectedShot.id]} comparisonRef={comparisonRef} />
    </section>
  );
}
