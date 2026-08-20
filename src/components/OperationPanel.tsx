import { useRef, type RefObject } from "react";
import type { DevelopMode, EngineStatus, ViewerSource, ViewingItem } from "../lib/app-state";
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
  /** 今見ている写真。合成を見ている間は合成の先頭フレーム。 */
  captureId: CaptureId;
  onViewCapture: (groupId: number, captureId: CaptureId) => void;
  engineStatus: EngineStatus;
  engineVersion: string;
  health: HealthResponse | null;
  presetName: PresetName;
  adjustments: Adjustments | null;
  changedKeys: (keyof Adjustments)[];
  onChangePreset: (presetName: PresetName) => void;
  onChangeAdjustment: (key: keyof Adjustments, value: Adjustments[keyof Adjustments]) => void;
  onResetAdjustments: () => void;
  /**
   * その撮影IDを採用しているか。フィルムストリップは 1 枚ずつ採用できるので、
   * 「表示中の 1 枚」ではなくタイルごとに引ける関数で受け取る。
   */
  isPhotoAccepted: (captureId: CaptureId) => boolean;
  /** 写真の採用トグル。**引数の撮影ID**が宛先（表示カーソルではない）。 */
  onAcceptPhoto: (captureId: CaptureId) => void;
  mergedAccepted: boolean;
  onAcceptMerged: () => void;
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
  /** このグループで今見ている面。比較タブの選択状態はここから導く。 */
  viewing: ViewingItem;
  onViewMerged: () => void;
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
  onViewCapture,
  engineStatus,
  engineVersion,
  health,
  presetName,
  adjustments,
  changedKeys,
  onChangePreset,
  onChangeAdjustment,
  onResetAdjustments,
  isPhotoAccepted,
  onAcceptPhoto,
  mergedAccepted,
  onAcceptMerged,
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
  viewing,
  onViewMerged,
  onOpenViewer,
}: OperationPanelProps) {
  const mergePanelRef = useRef<HTMLElement>(null);
  const developResult = fullResult ?? previewResult;
  const mergedResult = isBracket ? (mergeFullResult ?? mergePreviewResult) : undefined;
  // 現像と合成の両方があるときだけ切替タブを出す。片方しか無いときは自動でそちらを見せる。
  const canToggleComparison = Boolean(developResult && mergedResult);
  // 合成タイルに出す枚数。まだ合成していなければ「これから合成する枚数」を出す。
  const mergedFrameCount = mergedResult?.frames ?? mergeFrames.length;
  const showMerged = Boolean(mergedResult) && (viewing.kind === "merged" || !developResult);
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

          {/*
            フィルムストリップ。以前はラジオグループとして組んでいたが、ラジオは
            「1 つだけ選ぶ」を表すので複数採用と両立しない（そもそもラジオの中に
            2 つ目のボタンを置くのは無効な ARIA でもある）。
            素の <ul> に戻し、タイルごとに「表示を切り替える」ボタンと
            「採用をトグルする」ボタンを 1 つずつ置く。状態は色ではなく
            aria-current（表示中）と aria-pressed（採用）で読める。
          */}
          <ul className="shot-picker operation-shot-picker" aria-label="このグループの写真">
            {/*
              合成はブラケットのグループでだけ、**写真と同じ形の先頭タイル**として常に出す。
              合成したのに採用し忘れたグループが無言で書き出しから落ちる、という欠陥が
              「未点灯の採用チップが見えている」だけで構造的に消える。
            */}
            {isBracket && (
              <li className={`shot-tile shot-merged${mergedAccepted ? " accepted" : ""}`}>
                <button
                  type="button"
                  className="shot-open"
                  aria-current={viewing.kind === "merged" ? "true" : undefined}
                  aria-label="ブラケット合成の結果を表示する"
                  // 合成結果が 1 つも無いと表示を切り替えても比較欄は変わらない（左ペインだけ
                  // 動いて見える）。押せないようにしておく。
                  disabled={!mergedResult}
                  onClick={onViewMerged}
                >
                  {mergeFullResult ? (
                    <img src={mergeFullResult.output_url} alt="ブラケット合成の結果" decoding="async" />
                  ) : (
                    <span className="shot-well">
                      <b aria-hidden="true">▤</b>
                      <em>本合成すると採用できます</em>
                    </span>
                  )}
                </button>
                <span className="merged-tag">合成 {mergedFrameCount}枚</span>
                <button
                  type="button"
                  className="shot-accept"
                  aria-pressed={mergedAccepted}
                  aria-label={mergedAccepted ? "合成の採用を外す" : "合成した1枚を採用する"}
                  // 書き出しに使えるのは本合成の結果だけ。プレビューでは採用させない。
                  disabled={!mergeFullResult}
                  title={mergeFullResult ? undefined : "本合成すると採用できます"}
                  onClick={onAcceptMerged}
                >
                  <span aria-hidden="true">✓</span>
                </button>
              </li>
            )}

            {group.capture_ids.map((id) => {
              const candidate = analysis.photos[id];
              // 合成を見ている間はどの写真タイルも「表示中」ではない（カーソルは 1 本）。
              const isViewing = viewing.kind === "photo" && viewing.captureId === id;
              const accepted = isPhotoAccepted(id);
              return (
                <li key={id} className={`shot-tile${accepted ? " accepted" : ""}`}>
                  <button
                    type="button"
                    className="shot-open"
                    aria-current={isViewing ? "true" : undefined}
                    aria-label={`${id} を表示する`}
                    onClick={() => onViewCapture(group.id, id)}
                  >
                    {candidate ? <img src={assetUrl(candidate.thumb)} alt="" loading="lazy" decoding="async" /> : null}
                  </button>
                  {/* 推薦はアンバー。緑は「表示中」と「採用」で使い切っているため。 */}
                  {id === group.recommended_capture_id && (
                    <span className="shot-recommended"><i aria-hidden="true">✦</i> 推薦</span>
                  )}
                  <small className="shot-id">{id}</small>
                  <button
                    type="button"
                    className="shot-accept"
                    aria-pressed={accepted}
                    aria-label={accepted ? `${id} の採用を外す` : `${id} を採用する`}
                    onClick={() => onAcceptPhoto(id)}
                  >
                    <span aria-hidden="true">✓</span>
                  </button>
                </li>
              );
            })}
          </ul>

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
            photoAccepted={isPhotoAccepted(captureId)}
            onAcceptPhoto={() => onAcceptPhoto(captureId)}
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
          accepted={mergedAccepted}
          onAcceptMerged={onAcceptMerged}
          panelRef={mergePanelRef}
        />
      )}

      <BeforeAfter
        captureId={captureId}
        result={result}
        comparisonRef={comparisonRef}
        view={canToggleComparison ? (showMerged ? "merged" : "develop") : undefined}
        // タブは表示カーソル 1 本を書き換える。「単写の現像」側は今見えているカット
        // （合成中なら合成の先頭フレーム）へ戻す。
        onChangeView={
          canToggleComparison
            ? (next) => (next === "merged" ? onViewMerged() : onViewCapture(group.id, captureId))
            : undefined
        }
        onOpenFullSize={() => onOpenViewer(viewerSourceFor("after"))}
      />
    </section>
  );
}
