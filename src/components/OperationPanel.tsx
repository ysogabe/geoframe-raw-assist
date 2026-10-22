import type { RefObject } from "react";
import type { DevelopMode, EngineStatus, ViewerGallery, ViewerSource, ViewingItem } from "../lib/app-state";
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
  PhotoInfo,
  PresetInfo,
  PresetName,
} from "../lib/types";
import { DevelopControls } from "./DevelopControls";
import { BeforeAfter } from "./BeforeAfter";
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
  /** このグループで今見ている面。比較タブの選択状態はここから導く。 */
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
  const unscored = analysis.scored === false;
  const result = fullResult ?? previewResult;

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
          // 長辺はエンジンで違う（rawpy 1600 / Core Image 2400）ので結果の寸法から渡す。
          previewEdge: developed?.preview ? Math.max(developed.width, developed.height) : undefined,
          title: id,
        };
      });
    // 表示カーソルがどの写真にも当たらない状況（想定外）でも先頭から開ければ十分。
    const index = items.findIndex((item) => item.captureId === captureId);
    return { items, index: index < 0 ? 0 : index };
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
            {/* ブラケット合成の先頭タイルは M2 まで隠す。戻すときはここに合成タイル（isBracket 時のみ）を差し込む。 */}

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
            onDevelop={onDevelop}
            developMode={developMode}
            elapsedLabel={elapsedLabel}
            hasPreview={Boolean(previewResult)}
            hasFull={Boolean(fullResult)}
          />
          <p className="safety-note">元のJPG / ARWは変更されません（{groupKindLabel(group.kind)}）</p>
        </aside>
      </div>

      {/* ブラケット合成パネル（BracketMerge）は M2 まで隠す。戻すときはここに差し込む。 */}

      <BeforeAfter
        captureId={captureId}
        result={result}
        comparisonRef={comparisonRef}
        onOpenFullSize={() => onOpenViewer(photoGalleryFor("after"))}
      />
    </section>
  );
}
