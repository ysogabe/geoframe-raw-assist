import type { RefObject } from "react";
import {
  MERGE_MAX_FRAMES,
  MERGE_MIN_FRAMES,
  defaultMergeFrames,
  type DevelopMode,
  type EngineStatus,
} from "../lib/app-state";
import { formatExposureBias, formatMeanLuma, formatPercent } from "../lib/format";
import { assetUrl } from "../lib/raw-api";
import type { Analysis, CaptureId, GroupInfo } from "../lib/types";

type BracketMergeProps = {
  analysis: Analysis;
  group: GroupInfo;
  /** 合成対象として選択中のフレーム（撮影順）。 */
  frames: CaptureId[];
  onToggleFrame: (captureId: CaptureId) => void;
  onResetFrames: () => void;
  onMerge: (mode: DevelopMode) => void;
  /** このグループの合成が実行中ならそのモード。 */
  mergeMode: DevelopMode | null;
  elapsedLabel: string;
  /** 合成できない理由（枚数が 2〜9 の外）。null なら実行可。 */
  blockReason: string | null;
  /** 現像・合成のいずれかが実行中。重い処理を同時に走らせない。 */
  busy: boolean;
  engineStatus: EngineStatus;
  presetLabel: string;
  hasPreview: boolean;
  hasFull: boolean;
  /**
   * 合成の採用トグル。フィルムストリップ先頭の合成タイルと **同じ accept/merged を撃つ**。
   * 導線が二重なのは意図的で、合成し終えた直後にその場で採用できるのが一番自然なため。
   */
  accepted: boolean;
  onAcceptMerged: () => void;
  panelRef: RefObject<HTMLElement | null>;
};

export function BracketMerge({
  analysis,
  group,
  frames,
  onToggleFrame,
  onResetFrames,
  onMerge,
  mergeMode,
  elapsedLabel,
  blockReason,
  busy,
  engineStatus,
  presetLabel,
  hasPreview,
  hasFull,
  accepted,
  onAcceptMerged,
  panelRef,
}: BracketMergeProps) {
  const selected = new Set(frames);
  const total = group.capture_ids.length;
  // 5枚AEB が複数セット同じグループに入ることがある（実データに20枚の例がある）。
  // 上限で切った既定値も**シーンをまたぐ**ため、枚数で気づけるようにする。
  const multiScene = total > MERGE_MAX_FRAMES;
  const disabled = engineStatus !== "ready" || busy || blockReason !== null;
  // 既定（9枚以下なら全部、10枚以上なら先頭9枚）に戻すボタンは、既に既定と同じなら押させない。
  const defaults = defaultMergeFrames(group);
  const isDefaultSelection =
    frames.length === defaults.length && frames.every((id, index) => id === defaults[index]);

  return (
    <section ref={panelRef} className="bracket-merge" aria-label="ブラケット合成">
      <div className="bracket-head">
        <div>
          <p className="eyebrow">BRACKET MERGE</p>
          <h3>露出ブラケットを1枚に合成する</h3>
        </div>
        <span className="bracket-count">{frames.length} / {total}枚を選択中</span>
      </div>

      <p className="bracket-lead">
        露出を変えて撮った{MERGE_MIN_FRAMES}〜{MERGE_MAX_FRAMES}枚を合成し、白飛びと黒つぶれを同時に抑えます。
        現像プリセット「{presetLabel}」と右の現像パラメータをそのまま使います。
      </p>

      {multiScene && (
        <p className="bracket-warning">
          このグループは{total}枚あり、別のシーンのブラケットが混ざっている可能性があります。
          既定では先頭{MERGE_MAX_FRAMES}枚を選んでいますが、<strong>露出補正と平均輝度が階段状に並ぶひと続き</strong>だけを残して合成してください。
        </p>
      )}

      <ul className="bracket-frames">
        {group.capture_ids.map((id) => {
          const photo = analysis.photos[id];
          const checked = selected.has(id);
          return (
            <li key={id} className={checked ? "selected" : ""}>
              <label>
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={busy}
                  onChange={() => onToggleFrame(id)}
                  aria-label={`${id} を合成に使う`}
                />
                <span className="bracket-thumb">
                  {photo ? <img src={assetUrl(photo.thumb)} alt="" loading="lazy" decoding="async" /> : null}
                </span>
                <span className="bracket-id">
                  <strong>{id}</strong>
                  <small>{photo ? formatExposureBias(photo.exposure_bias) : "解析データなし"}</small>
                </span>
                <span className="bracket-stats">
                  <span>平均輝度 <b>{photo ? formatMeanLuma(photo.mean_luma) : "—"}</b></span>
                  <span>白飛び <b>{photo ? `${formatPercent(photo.highlight_clip_ratio)}%` : "—"}</b></span>
                  <span>黒つぶれ <b>{photo ? `${formatPercent(photo.shadow_clip_ratio)}%` : "—"}</b></span>
                </span>
              </label>
            </li>
          );
        })}
      </ul>
      <p className="bracket-legend">平均輝度は 0〜255。白飛び・黒つぶれはその階調に張り付いた画素の割合です。</p>

      <div className="bracket-actions">
        <button className="secondary-action" onClick={onResetFrames} disabled={busy || isDefaultSelection}>
          既定の{defaults.length}枚に戻す
        </button>
        <button className="secondary-action" onClick={() => onMerge("preview")} disabled={disabled}>
          {mergeMode === "preview" ? `合成中… ${elapsedLabel}` : hasPreview ? "合成プレビューを更新" : "合成プレビュー"}
        </button>
        <button className="primary-action" onClick={() => onMerge("full")} disabled={disabled}>
          {mergeMode === "full" ? `本合成中… ${elapsedLabel}` : hasFull ? "本合成をやり直す" : "ブラケット合成"}
        </button>
      </div>

      {/* 採用は本合成の結果があるときだけ。プレビューは書き出しに使えない。 */}
      <div className="bracket-accept">
        <button
          type="button"
          className={`accept-toggle${accepted ? " on" : ""}`}
          aria-pressed={accepted}
          disabled={!hasFull && !accepted}
          onClick={onAcceptMerged}
        >
          {accepted ? "採用を外す" : "合成した1枚を採用する"}
        </button>
        <small>
          {hasFull
            ? "上のフィルムストリップ先頭にある合成タイルと同じ操作です。"
            : "本合成すると採用できます。"}
        </small>
      </div>

      {blockReason ? (
        <p className="bracket-block" role="status">{blockReason}</p>
      ) : engineStatus !== "ready" ? (
        <p className="bracket-block" role="status">現像エンジンが停止中です。合成は実行できません。</p>
      ) : (
        <p className="develop-hint">
          フレーム数ぶんのRAWを読むため、合成プレビューでも数秒、本合成は数十秒かかります。書き出しに使えるのは本合成の結果だけです。
        </p>
      )}
    </section>
  );
}
