import { useRef, type RefObject } from "react";
import type { ComparisonView, DevelopMode, EngineStatus, ViewerSource } from "../lib/app-state";
import {
  formatAperture,
  formatConfidence,
  formatDimensions,
  formatExposureBias,
  formatFocalLength,
  formatIso,
  formatPercent,
  formatScore,
  formatShutter,
  groupKindLabel,
  isMergedResult,
  isPortrait,
  rawPillClass,
} from "../lib/format";
import { assetUrl, sourceJpgUrl } from "../lib/raw-api";
import type {
  Adjustments,
  Analysis,
  CaptureId,
  DevelopedResult,
  GroupInfo,
  HealthResponse,
  MergedResult,
  PhotoInfo,
  PresetName,
} from "../lib/types";
import { DevelopControls } from "./DevelopControls";
import { BeforeAfter } from "./BeforeAfter";
import { BracketMerge } from "./BracketMerge";

type OperationPanelProps = {
  analysis: Analysis;
  group: GroupInfo;
  photo: PhotoInfo | null;
  captureId: CaptureId;
  onChooseCapture: (groupId: number, captureId: CaptureId) => void;
  engineStatus: EngineStatus;
  engineVersion: string;
  health: HealthResponse | null;
  presetName: PresetName;
  adjustments: Adjustments | null;
  changedKeys: (keyof Adjustments)[];
  onChangePreset: (presetName: PresetName) => void;
  onChangeAdjustment: (key: keyof Adjustments, value: Adjustments[keyof Adjustments]) => void;
  onResetAdjustments: () => void;
  isAccepted: boolean;
  onAccept: () => void;
  onDevelop: (mode: DevelopMode) => void;
  developMode: DevelopMode | null;
  elapsedLabel: string;
  previewResult: DevelopedResult | undefined;
  fullResult: DevelopedResult | undefined;
  comparisonRef: RefObject<HTMLDivElement | null>;
  /** 露出ブラケットのグループか。false なら合成の導線を一切出さない。 */
  isBracket: boolean;
  /** 以下はブラケット合成用（isBracket が false のときは使われない）。 */
  mergeFrames: CaptureId[];
  onToggleMergeFrame: (captureId: CaptureId) => void;
  onResetMergeFrames: () => void;
  onMerge: (mode: DevelopMode) => void;
  /** このグループの合成が実行中ならそのモード。他グループの合成中は null。 */
  mergeMode: DevelopMode | null;
  /** どのグループであれ合成が走っているか。重い処理の同時実行を止めるのに使う。 */
  mergeBusy: boolean;
  mergeElapsedLabel: string;
  mergeBlockReason: string | null;
  mergePreviewResult: MergedResult | undefined;
  mergeFullResult: MergedResult | undefined;
  comparisonView: ComparisonView;
  onChangeComparisonView: (view: ComparisonView) => void;
  /** アプリ内の原寸ビューアを開く。渡す 2 面はこの画面が組み立てる。 */
  onOpenViewer: (source: ViewerSource) => void;
};

function ScoreRow({ label, value }: { label: string; value: number }) {
  return (
    <div className="score-row">
      <span>{label}</span>
      <div><i style={{ width: `${Math.min(Math.max(value, 0), 100)}%` }} /></div>
      <strong>{value}</strong>
    </div>
  );
}

export function OperationPanel({
  analysis,
  group,
  photo,
  captureId,
  onChooseCapture,
  engineStatus,
  engineVersion,
  health,
  presetName,
  adjustments,
  changedKeys,
  onChangePreset,
  onChangeAdjustment,
  onResetAdjustments,
  isAccepted,
  onAccept,
  onDevelop,
  developMode,
  elapsedLabel,
  previewResult,
  fullResult,
  comparisonRef,
  isBracket,
  mergeFrames,
  onToggleMergeFrame,
  onResetMergeFrames,
  onMerge,
  mergeMode,
  mergeBusy,
  mergeElapsedLabel,
  mergeBlockReason,
  mergePreviewResult,
  mergeFullResult,
  comparisonView,
  onChangeComparisonView,
  onOpenViewer,
}: OperationPanelProps) {
  const mergePanelRef = useRef<HTMLElement>(null);
  const developResult = fullResult ?? previewResult;
  const mergedResult = isBracket ? (mergeFullResult ?? mergePreviewResult) : undefined;
  // 現像と合成の両方があるときだけ切替タブを出す。片方しか無いときは自動でそちらを見せる。
  const canToggleComparison = Boolean(developResult && mergedResult);
  const showMerged = Boolean(mergedResult) && (comparisonView === "merged" || !developResult);
  const result = showMerged ? mergedResult : developResult;
  const presetLabel = health?.presets.find((preset) => preset.name === presetName)?.label ?? presetName;

  /**
   * 原寸ビューアに渡す 2 面をここで組み立てる（起動元が 3 か所あるので 1 か所にまとめる）。
   * 現像後の URL は `output_url` をそのまま使う。キャッシュバスター付きの完全URLなので
   * assetUrl() を通してはならない。
   */
  function viewerSourceFor(initial: "before" | "after"): ViewerSource {
    // ここは `result &&` のガードで書く（変数経由だと result の型が絞られない）。
    const merged = result && isMergedResult(result) ? result : null;
    return {
      beforeUrl: sourceJpgUrl(captureId),
      afterUrl: result?.output_url,
      beforeLabel: "現像前",
      afterLabel: merged ? "合成後" : "現像後",
      // まだ現像していなければ見せられるのは現像前だけ。
      initial: result ? initial : "before",
      preview: Boolean(result?.preview),
      title: merged ? `${captureId}（ブラケット合成 ${merged.frames}枚）` : captureId,
    };
  }

  return (
    <section className="operation-screen screen-enter" aria-label="写真の操作パネル">
      <div className="operation-progress">
        <span className="done">✓</span>
        <div><strong>候補を選択</strong><small>{group.capture_ids.length}枚から比較</small></div>
        <i />
        <span className="current">2</span>
        <div><strong>仕上げを決定</strong><small>採用またはRAW現像</small></div>
      </div>

      <div className="operation-layout">
        <div className="operation-media-card">
          <div className="media-heading">
            <div><p className="eyebrow">SELECTED PHOTO</p><h2>{captureId}</h2></div>
            <span className={rawPillClass(group.raw.level)}>{group.raw.label}</span>
          </div>
          <div className="selected-preview operation-preview">
            {photo ? <img src={assetUrl(photo.thumb)} alt={`選択中 ${captureId}`} decoding="async" /> : null}
            {photo && <div className="preview-grade"><small>総合</small><strong>{formatScore(photo.overall_score)}</strong></div>}
            <div className="filename">{captureId}.JPG</div>
            {/* サムネそのものをクリックしても原寸ビューアが開くようにする（一番押したくなる場所なので）。 */}
            <button
              type="button"
              className="preview-open"
              onClick={() => onOpenViewer(viewerSourceFor("before"))}
              aria-label={`${captureId} を原寸で表示`}
            >
              <span>原寸で見る</span>
            </button>
          </div>
          <div className="media-footnote">
            {photo && <span>{formatDimensions(photo)}{isPortrait(photo.orientation) ? "・縦位置" : ""}</span>}
            <button type="button" onClick={() => onOpenViewer(viewerSourceFor("before"))}>
              カメラJPGを原寸で開く
            </button>
          </div>

          <div className="shot-picker operation-shot-picker" role="radiogroup" aria-label="代表写真を選択">
            {group.capture_ids.map((id) => {
              const candidate = analysis.photos[id];
              return (
                <button
                  key={id}
                  className={captureId === id ? "active" : ""}
                  onClick={() => onChooseCapture(group.id, id)}
                  role="radio"
                  aria-checked={captureId === id}
                >
                  {candidate ? <img src={assetUrl(candidate.thumb)} alt={id} loading="lazy" decoding="async" /> : null}
                  {id === group.recommended_capture_id && <span>推薦</span>}
                  <small>{id}</small>
                </button>
              );
            })}
          </div>

          {photo && (
            <dl className="exif-grid">
              <div><dt>撮影</dt><dd>{photo.captured_at.replace("T", " ")}</dd></div>
              <div><dt>感度</dt><dd>{formatIso(photo.iso)}</dd></div>
              <div><dt>絞り</dt><dd>{formatAperture(photo.f_number)}</dd></div>
              <div><dt>シャッター</dt><dd>{formatShutter(photo.shutter)}</dd></div>
              <div><dt>焦点距離</dt><dd>{formatFocalLength(photo.focal_length)}</dd></div>
              <div><dt>露出補正</dt><dd>{formatExposureBias(photo.exposure_bias)}</dd></div>
              <div className="exif-wide"><dt>レンズ</dt><dd>{photo.lens_model ?? "—"}</dd></div>
            </dl>
          )}
        </div>

        <aside className="operation-controls-card">
          <div className="controls-heading"><p className="eyebrow">DECISION</p><h2>この写真をどう仕上げますか？</h2></div>

          <div className="analysis-card">
            <div className="analysis-title">
              <span className="spark">✦</span>
              <strong>{group.recommended_capture_id === captureId ? "この一枚をおすすめ" : "おすすめは別の一枚です"}</strong>
              <small>信頼度 {formatConfidence(group.confidence)}%</small>
            </div>
            <ul className="reason-list">
              {group.recommendation_reasons.map((reason) => (
                <li key={reason.code}>{reason.text}</li>
              ))}
            </ul>
            {photo ? (
              <>
                <ScoreRow label="総合スコア" value={formatScore(photo.overall_score)} />
                <ScoreRow label="シャープネス" value={formatScore(photo.sharpness_score)} />
                <ScoreRow label="ハイライト保持" value={100 - formatPercent(photo.highlight_clip_ratio)} />
                <ScoreRow label="暗部の保持" value={100 - formatPercent(photo.shadow_clip_ratio)} />
              </>
            ) : (
              <p className="reason-empty">この写真の解析データが見つかりません。</p>
            )}
          </div>

          <div className={`raw-callout ${group.raw.level === "strong" ? "urgent" : ""}`}>
            <span className="raw-icon">R</span>
            <div>
              <strong>{group.raw.label}</strong>
              {group.raw.reasons.length ? (
                group.raw.reasons.map((reason) => <p key={reason}>{reason}</p>)
              ) : (
                <p>この写真はJPEGのままでも十分です。</p>
              )}
            </div>
          </div>

          {isBracket && (
            <div className="bracket-hint">
              <span className="bracket-icon">▤</span>
              <div>
                <strong>露出ブラケットとして検出されています</strong>
                <p>
                  下の「ブラケット合成」でフレームを選ぶと、白飛びと黒つぶれを同時に抑えた1枚に合成できます。
                </p>
                <button
                  type="button"
                  className="bracket-jump"
                  onClick={() => mergePanelRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })}
                >
                  合成パネルへ移動 ↓
                </button>
              </div>
            </div>
          )}

          <DevelopControls
            engineStatus={engineStatus}
            engineVersion={engineVersion}
            health={health}
            presetName={presetName}
            adjustments={adjustments}
            changedKeys={changedKeys}
            onChangePreset={onChangePreset}
            onChangeAdjustment={onChangeAdjustment}
            onResetAdjustments={onResetAdjustments}
            isAccepted={isAccepted}
            onAccept={onAccept}
            onDevelop={onDevelop}
            developMode={developMode}
            elapsedLabel={elapsedLabel}
            hasPreview={Boolean(previewResult)}
            hasFull={Boolean(fullResult)}
            mergeBusy={mergeBusy}
          />
          <p className="safety-note">元のJPG / ARWは変更されません（{groupKindLabel(group.kind)}）</p>
        </aside>
      </div>

      {isBracket && (
        <BracketMerge
          analysis={analysis}
          group={group}
          frames={mergeFrames}
          onToggleFrame={onToggleMergeFrame}
          onResetFrames={onResetMergeFrames}
          onMerge={onMerge}
          mergeMode={mergeMode}
          elapsedLabel={mergeElapsedLabel}
          blockReason={mergeBlockReason}
          busy={developMode !== null || mergeBusy}
          engineStatus={engineStatus}
          presetLabel={presetLabel}
          hasPreview={Boolean(mergePreviewResult)}
          hasFull={Boolean(mergeFullResult)}
          panelRef={mergePanelRef}
        />
      )}

      <BeforeAfter
        captureId={captureId}
        result={result}
        comparisonRef={comparisonRef}
        view={canToggleComparison ? (showMerged ? "merged" : "develop") : undefined}
        onChangeView={canToggleComparison ? onChangeComparisonView : undefined}
        onOpenFullSize={() => onOpenViewer(viewerSourceFor("after"))}
      />
    </section>
  );
}
