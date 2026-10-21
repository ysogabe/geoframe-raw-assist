import type { DevelopMode, EngineStatus } from "../lib/app-state";
import { NOISE_REDUCTION_LABELS, SLIDER_SPECS } from "../lib/format";
import type { Adjustments, NoiseReduction, PresetInfo, PresetName } from "../lib/types";
import type { DevelopmentCandidate, DevelopmentCandidatesResponse, StarSceneInfo } from "../lib/types";
import { DevelopmentCandidates } from "./DevelopmentCandidates";

type DevelopControlsProps = {
  engineStatus: EngineStatus;
  engineVersion: string;
  /** プリセット一覧（state.presets）。health とは独立に届く（#98）。 */
  presets: PresetInfo[];
  presetName: PresetName;
  adjustments: Adjustments | null;
  changedKeys: (keyof Adjustments)[];
  onChangePreset: (presetName: PresetName) => void;
  onChangeAdjustment: (key: keyof Adjustments, value: Adjustments[keyof Adjustments]) => void;
  onResetAdjustments: () => void;
  /**
   * 今表示している写真そのものを採用しているか。
   * 以前はグループ単位のフラグで、「この写真を採用」という文言が実態と食い違っていた。
   */
  photoAccepted: boolean;
  onAcceptPhoto: () => void;
  onDevelop: (mode: DevelopMode) => void;
  developMode: DevelopMode | null;
  elapsedLabel: string;
  hasPreview: boolean;
  hasFull: boolean;
  /** ブラケット合成の実行中。重いRAWデコードを同時に走らせないため現像側も止める。 */
  mergeBusy: boolean;
  /** RAW縮小デコード代理評価による候補生成の状態。候補選択は現像を開始しない。 */
  developmentCandidates: DevelopmentCandidate[];
  developmentRejected: DevelopmentCandidatesResponse["rejected"];
  developmentStarScene: StarSceneInfo | null;
  developmentCandidatesBusy: boolean;
  developmentCandidatesError: string;
  onGenerateCandidates: () => void;
  onApplyCandidate: (candidate: DevelopmentCandidate) => void;
};

export function DevelopControls({
  engineStatus,
  engineVersion,
  presets,
  presetName,
  adjustments,
  changedKeys,
  onChangePreset,
  onChangeAdjustment,
  onResetAdjustments,
  photoAccepted,
  onAcceptPhoto,
  onDevelop,
  developMode,
  elapsedLabel,
  hasPreview,
  hasFull,
  mergeBusy,
  developmentCandidates,
  developmentRejected,
  developmentStarScene,
  developmentCandidatesBusy,
  developmentCandidatesError,
  onGenerateCandidates,
  onApplyCandidate,
}: DevelopControlsProps) {
  const activePreset = presets.find((preset) => preset.name === presetName) ?? null;
  const busy = developMode !== null || mergeBusy || developmentCandidatesBusy;
  const disabled = engineStatus !== "ready" || !adjustments;

  return (
    <>
      <div className="raw-controls">
        <div className="engine-row">
          <label htmlFor="raw-preset">現像プリセット</label>
          <span className={`engine-badge ${engineStatus}`}>
            <i />
            {engineStatus === "ready" ? `${engineVersion} 接続済み` : engineStatus === "checking" ? "確認中" : "ローカルエンジン停止中"}
          </span>
        </div>
        <select
          id="raw-preset"
          value={presetName}
          disabled={presets.length === 0 || busy}
          onChange={(event) => onChangePreset(event.target.value)}
        >
          {presets.length === 0 ? (
            <option value={presetName}>プリセットを取得しています…</option>
          ) : (
            presets.map((preset) => (
              <option key={preset.name} value={preset.name}>
                {preset.label} — {preset.description}
              </option>
            ))
          )}
        </select>
        {activePreset && <p className="preset-description">{activePreset.description}</p>}

        {adjustments && (
          <div className="adjust-panel">
            <div className="adjust-head">
              <strong>現像パラメータ</strong>
              <button
                className="adjust-reset"
                onClick={onResetAdjustments}
                disabled={changedKeys.length === 0 || busy}
              >
                プリセットに戻す{changedKeys.length > 0 ? `（${changedKeys.length}）` : ""}
              </button>
            </div>

            {SLIDER_SPECS.map((spec) => {
              const value = adjustments[spec.key];
              const changed = changedKeys.includes(spec.key);
              return (
                <div className={`adjust-row${changed ? " changed" : ""}`} key={spec.key}>
                  <label htmlFor={`adjust-${spec.key}`}>{spec.label}</label>
                  <input
                    id={`adjust-${spec.key}`}
                    type="range"
                    min={spec.min}
                    max={spec.max}
                    step={spec.step}
                    value={value}
                    disabled={busy}
                    onChange={(event) => onChangeAdjustment(spec.key, Number(event.target.value))}
                  />
                  <output htmlFor={`adjust-${spec.key}`}>{value.toFixed(spec.digits)}</output>
                </div>
              );
            })}

            <div className={`adjust-row select-row${changedKeys.includes("noise_reduction") ? " changed" : ""}`}>
              <label htmlFor="adjust-noise">ノイズ低減</label>
              <select
                id="adjust-noise"
                value={adjustments.noise_reduction}
                disabled={busy}
                onChange={(event) => onChangeAdjustment("noise_reduction", event.target.value as NoiseReduction)}
              >
                {(Object.keys(NOISE_REDUCTION_LABELS) as NoiseReduction[]).map((key) => (
                  <option key={key} value={key}>{NOISE_REDUCTION_LABELS[key]}</option>
                ))}
              </select>
            </div>

            <p className="adjust-note">スライダーを動かしても自動では現像しません。下のボタンで実行します。</p>
          </div>
        )}

        <DevelopmentCandidates
          candidates={developmentCandidates}
          rejected={developmentRejected}
          starScene={developmentStarScene}
          busy={developmentCandidatesBusy}
          error={developmentCandidatesError}
          disabled={disabled || mergeBusy || developMode !== null}
          onGenerate={onGenerateCandidates}
          onApply={onApplyCandidate}
        />

        <div className="develop-actions">
          <button className="secondary-action" onClick={() => onDevelop("preview")} disabled={disabled || busy}>
            {developMode === "preview" ? `プレビュー中… ${elapsedLabel}` : hasPreview ? "プレビューを更新" : "プレビュー現像"}
          </button>
          <button className="primary-action" onClick={() => onDevelop("full")} disabled={disabled || busy}>
            {developMode === "full" ? `本現像中… ${elapsedLabel}` : hasFull ? "本現像をやり直す" : "本現像"}
          </button>
        </div>
        <p className="develop-hint">プレビューは約1〜3秒、本現像は約5〜20秒かかります。</p>
      </div>

      <div className="operation-actions">
        {/* 採用は二値のトグル。色だけでなく aria-pressed でも状態が読めるようにする。 */}
        <button
          className={`primary-action accept-toggle${photoAccepted ? " on" : ""}`}
          onClick={onAcceptPhoto}
          aria-pressed={photoAccepted}
        >
          {photoAccepted ? "採用を外す" : "この写真を採用する"}
        </button>
      </div>
    </>
  );
}
