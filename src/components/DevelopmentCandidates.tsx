import { candidateStyleLabel, summarizeStarScene } from "../lib/candidate-view";
import type {
  DevelopmentCandidate,
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
  onGenerate: () => void;
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
  onGenerate,
  onApply,
}: DevelopmentCandidatesProps) {
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
          onClick={onGenerate}
          disabled={disabled || busy}
          aria-busy={busy}
        >
          {busy ? "候補を探索中…" : candidates.length > 0 ? "候補を作り直す" : "自動候補を作る"}
        </button>
      </div>
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
