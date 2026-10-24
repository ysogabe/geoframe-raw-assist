import { useRef, type RefObject } from "react";
import { acceptedMergeLabel, comparisonSurfaceOf, type AcceptedMergeView, type DevelopMode, type EngineStatus, type ViewerGallery, type ViewerSource, type ViewingItem } from "../lib/app-state";
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
  DevelopmentCandidate,
  DevelopmentCandidatesResponse,
  MergedResult,
  GroupInfo,
  PhotoInfo,
  PresetInfo,
  PresetName,
} from "../lib/types";
import { DevelopControls } from "./DevelopControls";
import { BeforeAfter } from "./BeforeAfter";
import { BracketMerge } from "./BracketMerge";
import { ProgressiveImage } from "./ProgressiveImage";

type OperationPanelProps = {
  analysis: Analysis;
  group: GroupInfo;
  photo: PhotoInfo | null;
  /** 今見ている写真。合成を見ている間は合成の先頭フレーム。 */
  captureId: CaptureId;
  onViewCapture: (groupId: number, captureId: CaptureId) => void;
  engineStatus: EngineStatus;
  engineVersion: string;
  /** プリセット一覧（state.presets）。 */
  presets: PresetInfo[];
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
  candidatesResponse: DevelopmentCandidatesResponse | null;
  candidatesBusy: boolean;
  candidatesError: string;
  onGenerateCandidates: () => void;
  onApplyCandidate: (candidate: DevelopmentCandidate) => void;
  isBracket: boolean;
  mergeFrames: CaptureId[];
  onToggleMergeFrame: (captureId: CaptureId) => void;
  onResetMergeFrames: () => void;
  onMerge: (mode: DevelopMode) => void;
  mergeMode: DevelopMode | null;
  mergeElapsedLabel: string;
  mergeBlockReason: string | null;
  mergePreviewResult: MergedResult | undefined;
  mergeFullResult: MergedResult | undefined;
  /** 今表示している合成（本合成）が採用そのものか。✓ はこのときだけ付く。 */
  mergedAccepted: boolean;
  /** 採用済みだが今の合成設定に合わない合成（採用時のスナップショット）。無ければ null。 */
  previousAcceptedMerge: AcceptedMergeView | null;
  onAcceptMerged: () => void;
  onRemovePreviousAcceptedMerge: () => void;
  onViewMerged: () => void;
  globalBusy: boolean;
  onDevelop: (mode: DevelopMode) => void;
  developMode: DevelopMode | null;
  elapsedLabel: string;
  previewResult: DevelopedResult | undefined;
  fullResult: DevelopedResult | undefined;
  /**
   * 現像結果の全量。原寸ビューアの送り輪（グループ跨ぎの全写真）を組み立てるのに使う。
   * 単数形の previewResult / fullResult は「今見ている 1 枚」用で、こちらとは役割が別。
   */
  previewResults: Record<CaptureId, DevelopedResult>;
  fullResults: Record<CaptureId, DevelopedResult>;
  comparisonRef: RefObject<HTMLDivElement | null>;
  /** このグループで実際に出している面（effectiveViewingOf）。比較タブの選択状態はここから導く。 */
  viewing: ViewingItem;
  /** アプリ内の原寸ビューアを開く。送り輪（開いた時点のスナップショット）はこの画面が組み立てる。 */
  onOpenViewer: (gallery: ViewerGallery) => void;
};

function ScoreRow({ label, value, unscored = false }: { label: string; value: number; unscored?: boolean }) {
  // 未採点（analysis.scored === false）の 0 は「悪い点数」ではなく「計算していない」。0 と表示しない。
  return (
    <div className="score-row">
      <span>{label}</span>
      <div><i style={{ width: unscored ? "0%" : `${Math.min(Math.max(value, 0), 100)}%` }} /></div>
      <strong>{unscored ? "未採点" : value}</strong>
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
  presets,
  presetName,
  adjustments,
  changedKeys,
  onChangePreset,
  onChangeAdjustment,
  onResetAdjustments,
  isPhotoAccepted,
  onAcceptPhoto,
  candidatesResponse,
  candidatesBusy,
  candidatesError,
  onGenerateCandidates,
  onApplyCandidate,
  isBracket,
  mergeFrames,
  onToggleMergeFrame,
  onResetMergeFrames,
  onMerge,
  mergeMode,
  mergeElapsedLabel,
  mergeBlockReason,
  mergePreviewResult,
  mergeFullResult,
  mergedAccepted,
  previousAcceptedMerge,
  onAcceptMerged,
  onRemovePreviousAcceptedMerge,
  onViewMerged,
  globalBusy,
  onDevelop,
  developMode,
  elapsedLabel,
  previewResult,
  fullResult,
  previewResults,
  fullResults,
  comparisonRef,
  viewing,
  onOpenViewer,
}: OperationPanelProps) {
  const mergePanelRef = useRef<HTMLElement>(null);
  const unscored = analysis.scored === false;
  const developResult = fullResult ?? previewResult;
  // App 側で「今の選択・プリセット・調整に合う結果」だけに絞って渡している。
  // 本合成があればそれを、無ければプレビューを見せる。
  const mergedResult = isBracket ? (mergeFullResult ?? mergePreviewResult) : undefined;
  const mergedFrameCount = mergedResult?.frames ?? mergeFrames.length;
  // 合成を見せるのは viewing が merged のときだけ（写真が未現像でも合成へ黙って切り替えない）。
  const { showMerged, result, canToggle: canToggleComparison } = comparisonSurfaceOf(viewing, mergedResult, developResult);

  /**
   * 原寸ビューアの送り輪を組み立てる。**グループを跨いだ全写真**を一覧と同じ並び順で並べ、
   * 今開こうとしている 1 枚をその中の位置として渡す。ビューアは自分の添字だけを動かすので、
   * 送っても selectedGroupId / viewing は動かない（閉じれば元の操作パネルがそのまま残る）。
   *
   * 現像後の URL は `output_url` をそのまま使う。キャッシュバスター付きの完全URLなので
   * assetUrl() を通してはならない。
   */
  function photoGalleryFor(initial: "before" | "after"): ViewerGallery {
    const items: ViewerSource[] = analysis.groups
      .flatMap((item) => item.capture_ids)
      .map((id) => {
        // 本現像があればそちら、無ければプレビュー。どちらも無ければ現像前しか見せられない。
        const developed = fullResults[id] ?? previewResults[id];
        return {
          captureId: id,
          beforeUrl: sourceJpgUrl(id),
          afterUrl: developed?.output_url,
          beforeLabel: "現像前",
          afterLabel: "現像後",
          // まだ現像していなければ見せられるのは現像前だけ。
          initial: developed ? initial : "before",
          preview: Boolean(developed?.preview),
          // 長辺はプレビューと本現像で違うので結果の寸法から渡す。
          previewEdge: developed?.preview ? Math.max(developed.width, developed.height) : undefined,
          title: id,
        };
      });
    // 表示カーソルがどの写真にも当たらない状況（想定外）でも先頭から開ければ十分。
    const index = items.findIndex((item) => item.captureId === captureId);
    return { items, index: index < 0 ? 0 : index };
  }

  function mergedGalleryFor(merged: MergedResult, initial: "before" | "after"): ViewerGallery {
    // 合成の先頭フレームを現像前として見せる。表示中の合成なら captureId と一致する。
    const lead = merged.capture_ids[0];
    return { items: [{
      captureId: lead,
      beforeUrl: sourceJpgUrl(lead),
      afterUrl: merged.output_url,
      beforeLabel: "現像前",
      afterLabel: "合成後",
      initial,
      preview: Boolean(merged.preview),
      previewEdge: merged.preview ? Math.max(merged.width, merged.height) : undefined,
      title: `${lead}（ブラケット合成 ${merged.frames}枚）`,
    }], index: 0 };
  }

  function viewerGalleryFor(initial: "before" | "after"): ViewerGallery {
    const merged = result && isMergedResult(result) ? result : null;
    if (!merged) return photoGalleryFor(initial);
    return mergedGalleryFor(merged, initial);
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

          {/*
            フィルムストリップ。以前はラジオグループとして組んでいたが、ラジオは
            「1 つだけ選ぶ」を表すので複数採用と両立しない（そもそもラジオの中に
            2 つ目のボタンを置くのは無効な ARIA でもある）。
            素の <ul> に戻し、タイルごとに「表示を切り替える」ボタンと
            「採用をトグルする」ボタンを 1 つずつ置く。状態は色ではなく
            aria-current（表示中）と aria-pressed（採用）で読める。
            置き場所は大きい写真の **上**。写真を選ぶ → 大きく見る、の順に目が流れるようにし、
            大きい写真を固定帯の直下に合わせたときも選択肢が画面外へ押し出されないようにする。
          */}
          <ul className="shot-picker operation-shot-picker" aria-label="このグループの写真">
            {isBracket && (
              <li className={`shot-tile shot-merged${mergedAccepted ? " accepted" : ""}`}>
                <button type="button" className="shot-open" aria-current={viewing.kind === "merged" ? "true" : undefined}
                  aria-label="ブラケット合成の結果を表示する" disabled={!mergedResult} onClick={onViewMerged}>
                  {mergedResult ? <img src={mergedResult.output_url} alt="ブラケット合成の結果" decoding="async" /> :
                    <span className="shot-well"><b aria-hidden="true">▤</b><em>本合成すると採用できます</em></span>}
                </button>
                <span className="merged-tag">合成 {mergedFrameCount}枚</span>
                {/* 宛先は今表示している本合成だけ。本合成が無い/合わないときは押せない。 */}
                <button type="button" className="shot-accept" aria-pressed={mergedAccepted}
                  aria-label={mergedAccepted ? "合成の採用を外す" : "合成した1枚を採用する"}
                  disabled={!mergeFullResult} title={mergeFullResult ? undefined : "本合成すると採用できます"}
                  onClick={onAcceptMerged}><span aria-hidden="true">{mergedAccepted ? "✓" : "○"}</span></button>
              </li>
            )}

            {/* 今の設定に合わなくなった採用済みの合成。今の合成へ✓を移さず、別タイルで確認と取り消しを残す。 */}
            {isBracket && previousAcceptedMerge && (
              <li className="shot-tile shot-merged shot-merged-previous accepted">
                <button type="button" className="shot-open" aria-label="以前の設定で採用した合成を原寸で確認する"
                  disabled={!previousAcceptedMerge.result}
                  onClick={() => previousAcceptedMerge.result && onOpenViewer(mergedGalleryFor(previousAcceptedMerge.result, "after"))}>
                  {previousAcceptedMerge.result ? <img src={previousAcceptedMerge.result.output_url} alt="以前の設定で採用した合成" decoding="async" /> :
                    <span className="shot-well"><b aria-hidden="true">▤</b><em>成果物なし</em></span>}
                </button>
                <span className="merged-tag">{acceptedMergeLabel(previousAcceptedMerge)}</span>
                <button type="button" className="shot-accept" aria-pressed="true"
                  aria-label="以前の設定で採用した合成の採用を外す"
                  onClick={onRemovePreviousAcceptedMerge}><span aria-hidden="true">✓</span></button>
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
                    <span aria-hidden="true">{accepted ? "✓" : "○"}</span>
                  </button>
                </li>
              );
            })}
          </ul>

          <div className="selected-preview operation-preview">
            {photo ? (
              <ProgressiveImage thumbSrc={assetUrl(photo.thumb)} fullSrc={sourceJpgUrl(captureId)} alt={`選択中 ${captureId}`} />
            ) : null}
            {photo && !unscored && <div className="preview-grade"><small>総合</small><strong>{formatScore(photo.overall_score)}</strong></div>}
            <div className="filename">{captureId}.JPG</div>
            {/* サムネそのものをクリックしても原寸ビューアが開くようにする（一番押したくなる場所なので）。 */}
            <button
              type="button"
              className="preview-open"
              onClick={() => onOpenViewer(photoGalleryFor("before"))}
              aria-label={`${captureId} を原寸で表示`}
            >
              <span>原寸で見る</span>
            </button>
          </div>
          <div className="media-footnote">
            {photo && <span>{formatDimensions(photo)}{isPortrait(photo.orientation) ? "・縦位置" : ""}</span>}
            <button type="button" onClick={() => onOpenViewer(photoGalleryFor("before"))}>
              カメラJPGを原寸で開く
            </button>
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
              {analysis.scored !== false && <small>信頼度 {formatConfidence(group.confidence)}%</small>}
            </div>
            <ul className="reason-list">
              {group.recommendation_reasons.map((reason) => (
                <li key={reason.code}>{reason.text}</li>
              ))}
            </ul>
            {photo ? (
              <>
                {unscored && <p className="reason-empty">採点は未対応です（グループ分けのみ）。</p>}
                <ScoreRow label="総合スコア" value={formatScore(photo.overall_score)} unscored={unscored} />
                <ScoreRow label="シャープネス" value={formatScore(photo.sharpness_score)} unscored={unscored} />
                <ScoreRow label="ハイライト保持" value={100 - formatPercent(photo.highlight_clip_ratio)} unscored={unscored} />
                <ScoreRow label="暗部の保持" value={100 - formatPercent(photo.shadow_clip_ratio)} unscored={unscored} />
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
                <p>下の「ブラケット合成」でフレームを選ぶと、白飛びと黒つぶれを同時に抑えた1枚に合成できます。</p>
                <button type="button" className="bracket-jump" onClick={() => mergePanelRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })}>合成パネルへ移動 ↓</button>
              </div>
            </div>
          )}

          <DevelopControls
            engineStatus={engineStatus}
            engineVersion={engineVersion}
            presets={presets}
            presetName={presetName}
            adjustments={adjustments}
            changedKeys={changedKeys}
            onChangePreset={onChangePreset}
            onChangeAdjustment={onChangeAdjustment}
            onResetAdjustments={onResetAdjustments}
            photoAccepted={isPhotoAccepted(captureId)}
            onAcceptPhoto={() => onAcceptPhoto(captureId)}
            candidatesResponse={candidatesResponse}
            candidatesBusy={candidatesBusy}
            candidatesError={candidatesError}
            onGenerateCandidates={onGenerateCandidates}
            onApplyCandidate={onApplyCandidate}
            onDevelop={onDevelop}
            developMode={developMode}
            elapsedLabel={elapsedLabel}
            hasPreview={Boolean(previewResult)}
            hasFull={Boolean(fullResult)}
            mergeBusy={globalBusy}
          />
          <p className="safety-note">元のJPG / ARWは変更されません（{groupKindLabel(group.kind)}）</p>
        </aside>
      </div>

      {isBracket && (
        <BracketMerge
          analysis={analysis} group={group} frames={mergeFrames}
          onToggleFrame={onToggleMergeFrame} onResetFrames={onResetMergeFrames} onMerge={onMerge}
          mergeMode={mergeMode} elapsedLabel={mergeElapsedLabel} blockReason={mergeBlockReason}
          busy={globalBusy} engineStatus={engineStatus} presetLabel={presets.find((item) => item.name === presetName)?.label ?? presetName}
          hasPreview={Boolean(mergePreviewResult)} hasFull={Boolean(mergeFullResult)}
          accepted={mergedAccepted} onAcceptMerged={onAcceptMerged}
          previousAccepted={previousAcceptedMerge} onRemovePreviousAccepted={onRemovePreviousAcceptedMerge}
          panelRef={mergePanelRef}
        />
      )}

      <BeforeAfter
        captureId={captureId}
        result={result}
        comparisonRef={comparisonRef}
        view={canToggleComparison ? (showMerged ? "merged" : "develop") : undefined}
        onChangeView={canToggleComparison ? (next) => next === "merged" ? onViewMerged() : onViewCapture(group.id, captureId) : undefined}
        onOpenFullSize={() => onOpenViewer(viewerGalleryFor("after"))}
      />
    </section>
  );
}
