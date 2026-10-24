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
      {diagnostic && <details>
        <summary>原寸ROI診断: {{ not_run: "未実行", missing: "欠測", failed: "失敗", complete: "完了（画質合格の判定なし）" }[diagnostic.status]}</summary>
        <p>{diagnostic.reason ?? "naturalとの同一ROI比較を記録しました。"} / 原寸現像{diagnostic.renders}回 / {(diagnostic.elapsed_ms.total / 1000).toFixed(1)}秒</p>
        {diagnostic.rois.filter((roi) => roi.status === "missing").map((roi) => <p key={roi.name}>{roi.name}: 欠測（{roi.reason}）</p>)}
        {diagnostic.sets.map((set) => <div key={set.candidate_ids.join(",")}>
          <strong>{set.candidate_ids.map(candidateStyleLabel).join(" / ")}</strong>
          {set.reason && <p>{set.reason}</p>}
          {set.rois.map((roi) => <p key={roi.name}>
            {roi.name} {roi.pixels?.join(", ")}：{roi.absolute && roi.delta_vs_natural
              ? `細部 ${roi.absolute.detail.toFixed(6)} (Δ${roi.delta_vs_natural.detail.toFixed(6)}) / ノイズ ${roi.absolute.noise_flat.toFixed(6)} (Δ${roi.delta_vs_natural.noise_flat.toFixed(6)}) / ハロー ${roi.absolute.halo.toFixed(6)} (Δ${roi.delta_vs_natural.halo.toFixed(6)})`
              : roi.reason}
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
