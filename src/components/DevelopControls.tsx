import type { DevelopMode, EngineStatus } from "../lib/app-state";
import { NOISE_REDUCTION_LABELS, SLIDER_SPECS } from "../lib/format";
import type { Adjustments, HealthResponse, NoiseReduction, PresetName } from "../lib/types";

type DevelopControlsProps = {
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
   * 今表示している写真そのものが採用済みか。
   * 以前はグループ単位のフラグで、「この写真を採用」という文言が実態と食い違っていた。
   */
  photoAccepted: boolean;
  onAcceptPhoto: () => void;
  /**
   * 合成の採用。本合成の結果があるときだけボタンを出す（プレビューは書き出せない）。
   * 表示中の面に依存させず常に並べるので、合成を見ていなくても採用できる。
   */
  canAcceptMerged: boolean;
  mergedAccepted: boolean;
  onAcceptMerged: () => void;
  onDevelop: (mode: DevelopMode) => void;
  developMode: DevelopMode | null;
  elapsedLabel: string;
  hasPreview: boolean;
  hasFull: boolean;
  /** ブラケット合成の実行中。重いRAWデコードを同時に走らせないため現像側も止める。 */
  mergeBusy: boolean;
};

export function DevelopControls({
  engineStatus,
  engineVersion,
  health,
  presetName,
  adjustments,
  changedKeys,
  onChangePreset,
  onChangeAdjustment,
  onResetAdjustments,
  photoAccepted,
  onAcceptPhoto,
  canAcceptMerged,
  mergedAccepted,
  onAcceptMerged,
  onDevelop,
  developMode,
  elapsedLabel,
  hasPreview,
  hasFull,
  mergeBusy,
}: DevelopControlsProps) {
  const presets = health?.presets ?? [];
  const activePreset = presets.find((preset) => preset.name === presetName) ?? null;
  const busy = developMode !== null || mergeBusy;
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
        <button className="primary-action" onClick={onAcceptPhoto}>
          {photoAccepted ? "✓ この写真を採用済み" : "この写真を採用"}
        </button>
        {canAcceptMerged && (
          <button className="secondary-action" onClick={onAcceptMerged}>
            {mergedAccepted ? "✓ 合成を採用済み" : "合成した1枚を採用"}
          </button>
        )}
      </div>
    </>
  );
}
