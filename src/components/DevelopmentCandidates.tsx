import { useState } from "react";
import { candidateStyleLabel, summarizeStarScene } from "../lib/candidate-view";
import type {
  DevelopmentCandidate,
  DevelopmentCandidatesProgress,
  NativeRoiDiagnostic,
  DevelopmentCandidateMetrics,
  DevelopmentCandidateStyle,
  StarSceneInfo,
} from "../lib/types";

type DevelopmentCandidatesProps = {
  candidates: DevelopmentCandidate[];
  rejected: { id: DevelopmentCandidateStyle; reason: string }[];
  starScene: StarSceneInfo | null;
  busy: boolean;
  error: string;
  disabled: boolean;
  progress?: DevelopmentCandidatesProgress | null;
  diagnostic?: NativeRoiDiagnostic;
  onGenerate: (nativeRoiDiagnostics: boolean) => void;
  onApply: (candidate: DevelopmentCandidate) => void;
};

function percent(value: number): string {
  return `${Math.round(Math.min(1, Math.max(0, value)) * 100)}%`;
}

function metric(value: number | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? percent(value) : "—";
}

function diagnosticReason(reason: string | null): string {
  const messages: Record<string, string> = {
    native_roi_incomplete: "一部の原寸ROIを診断できませんでした。",
    no_eligible_natural_proxy_tile: "条件に合う領域が見つかりませんでした。",
    no_eligible_rois: "診断できる領域が見つかりませんでした。",
    natural_full_unavailable: "基準画像の原寸診断を利用できません。",
    roi_measurement_incomplete: "一部の領域に欠測または失敗があります。",
    roi_measurement_failed: "領域の計測に失敗しました。",
    SIDECAR_TIMEOUT: "原寸現像が制限時間を超えました。",
    DECODE_FAILED: "RAWの読み込みに失敗しました。",
    RENDER_FAILED: "原寸現像に失敗しました。",
    NATIVE_ROI_SETTINGS_INVALID: "原寸診断の設定を確認できませんでした。",
    NATIVE_ROI_SCRATCH_FAILED: "原寸診断の一時出力先を作成できませんでした。",
    NATIVE_ROI_IMAGE_READ_FAILED: "原寸診断画像を読み込めませんでした。",
    NATIVE_ROI_DIMENSIONS_MISMATCH: "基準画像と候補の寸法が一致しません。",
    NATIVE_ROI_CROP_FAILED: "原寸ROIを切り出せませんでした。",
  };
  return reason && Object.hasOwn(messages, reason) ? messages[reason] : "診断を完了できませんでした。";
}

function CandidateMetrics({ values }: { values: DevelopmentCandidateMetrics }) {
  return (
    <dl className="development-candidate-metrics">
      <div><dt>白飛び</dt><dd>{metric(values.highlight_clip)}</dd></div>
      <div><dt>黒つぶれ</dt><dd>{metric(values.shadow_clip)}</dd></div>
      <div><dt>平坦部ノイズ</dt><dd>{metric(values.noise_flat)}</dd></div>
      <div><dt>細部</dt><dd>{metric(values.detail)}</dd></div>
      <div><dt>ハロー</dt><dd>{metric(values.halo)}</dd></div>
      <div><dt>基準との差</dt><dd>{metric(values.source_deviation)}</dd></div>
    </dl>
  );
}

function StarSceneNote({ scene }: { scene: StarSceneInfo }) {
  const summary = summarizeStarScene(scene);
  return (
    <details className="development-star-scene">
      <summary>星空判定: {summary.verdict}</summary>
      <dl>
        {summary.facts.map((fact) => (
          <div key={fact.label}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>
        ))}
      </dl>
    </details>
  );
}

export function DevelopmentCandidates({
  candidates,
  rejected,
  starScene,
  busy,
  error,
  disabled,
  progress,
  diagnostic,
  onGenerate,
  onApply,
}: DevelopmentCandidatesProps) {
  const [nativeRoi, setNativeRoi] = useState(false);
  return (
    <section className="development-candidates" aria-labelledby="development-candidates-title">
      <div className="development-candidates-head">
        <div>
          <strong id="development-candidates-title">現像候補自動生成</strong>
          <p>RAWの縮小デコードによる代理評価。候補を選んでも現像は始まりません。</p>
        </div>
        <button
          type="button"
          className="secondary-action candidate-generate"
          onClick={() => onGenerate(nativeRoi)}
          disabled={disabled || busy}
          aria-busy={busy}
        >
          {busy ? (progress?.stage === "native_roi" ? "原寸ROIを診断中…" : "候補を探索中…") : candidates.length > 0 ? "候補を作り直す" : "自動候補を作る"}
        </button>
      </div>
      <label>
        <input type="checkbox" checked={nativeRoi} onChange={(event) => setNativeRoi(event.target.checked)} disabled={disabled || busy} />
        最終候補の原寸ROIも診断する（時間がかかります・採否は変わりません）
      </label>
      {busy && progress && <p role="status">{progress.stage === "native_roi" ? "探索完了・原寸ROI" : progress.stage === "done" ? "完了" : "代理画像を探索中"} {progress.completed}/{progress.total}</p>}
      {diagnostic && diagnostic.status !== "not_run" && <details>
        <summary>原寸ROI診断: {{ not_run: "未実行", missing: "欠測", failed: "失敗", complete: "完了（画質合格の判定なし）" }[diagnostic.status]}</summary>
        <p>{diagnostic.reason || diagnostic.status !== "complete" ? diagnosticReason(diagnostic.reason) : "naturalとの同一ROI比較を記録しました。"} / 原寸現像{diagnostic.renders}回 / {(diagnostic.elapsed_ms.total / 1000).toFixed(1)}秒</p>
        {diagnostic.rois.filter((roi) => roi.status === "missing").map((roi) => <p key={roi.name}>{roi.name}: 欠測（{diagnosticReason(roi.reason)}）</p>)}
        {diagnostic.sets.map((set) => <div key={set.candidate_ids.join(",")}>
          <strong>{set.candidate_ids.map(candidateStyleLabel).join(" / ")}</strong>
          {set.reason && <p>{diagnosticReason(set.reason)}</p>}
          {set.rois.map((roi) => <p key={roi.name}>
            {roi.name} {roi.pixels?.join(", ")}：{roi.absolute && roi.delta_vs_natural
              ? `細部 ${roi.absolute.detail.toFixed(6)} (Δ${roi.delta_vs_natural.detail.toFixed(6)}) / ノイズ ${roi.absolute.noise_flat.toFixed(6)} (Δ${roi.delta_vs_natural.noise_flat.toFixed(6)}) / ハロー ${roi.absolute.halo.toFixed(6)} (Δ${roi.delta_vs_natural.halo.toFixed(6)})`
              : diagnosticReason(roi.reason)}
          </p>)}
        </div>)}
      </details>}
      {error && <p className="candidate-error" role="alert">{error}</p>}
      {candidates.length > 0 && (
        <div className="development-candidate-list">
          {candidates.map((candidate) => (
            <article className="development-candidate-card" key={candidate.id}>
              <div className="development-candidate-title">
                <div><strong>{candidate.label}</strong><small>{candidate.id}</small></div>
                <span>{candidate.score.toFixed(1)}<small>/100</small></span>
              </div>
              <p className="development-candidate-description">{candidate.description}</p>
              <CandidateMetrics values={candidate.metrics} />
              <ul className="development-candidate-reasons">
                {candidate.reasons.map((reason) => <li key={reason}>{reason}</li>)}
              </ul>
              <button
                type="button"
                className="candidate-apply"
                onClick={() => onApply(candidate)}
                disabled={disabled || busy}
              >
                この候補をスライダーへ反映
              </button>
            </article>
          ))}
        </div>
      )}
      {rejected.length > 0 && (
        <div className="development-candidate-rejected">
          <strong>出なかった候補</strong>
          <ul>
            {rejected.map((item) => (
              <li key={item.id}><b>{candidateStyleLabel(item.id)}</b>{item.reason}</li>
            ))}
          </ul>
        </div>
      )}
      {starScene && <StarSceneNote scene={starScene} />}
    </section>
  );
}
