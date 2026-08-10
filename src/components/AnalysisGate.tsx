import type { AnalysisState, EngineStatus } from "../lib/app-state";
import { analysisStageLabel } from "../lib/format";
import type { AnalysisProgress } from "../lib/types";

type AnalysisGateProps = {
  engineStatus: EngineStatus;
  analysisState: AnalysisState;
  progress: AnalysisProgress | null;
  errorMessage: string;
  busy: boolean;
  onStart: () => void;
};

export function AnalysisGate({ engineStatus, analysisState, progress, errorMessage, busy, onStart }: AnalysisGateProps) {
  if (engineStatus === "offline") {
    return (
      <section className="analysis-gate screen-enter" aria-label="ローカルRAWエンジンの状態">
        <span className="gate-mark offline">!</span>
        <h2>ローカルRAWエンジンが停止しています</h2>
        <p>
          写真の解析と現像はこのMacの中だけで行います。ターミナルで <code>npm run dev:raw</code> を実行してから、
          この画面を再読み込みしてください。
        </p>
        <p className="gate-note">画像が外部へ送信されることはありません。</p>
      </section>
    );
  }

  if (analysisState === "running") {
    const total = progress?.total ?? 0;
    const done = progress?.done ?? 0;
    const ratio = total > 0 ? Math.min(Math.max(done / total, 0), 1) : 0;
    return (
      <section className="analysis-gate screen-enter" aria-label="解析の進捗">
        <span className="gate-mark running" />
        <h2>写真を解析しています</h2>
        <p>{analysisStageLabel(progress?.stage ?? "")}</p>
        <div
          className="gate-progress"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={total || 1}
          aria-valuenow={done}
        >
          <i style={{ width: `${Math.round(ratio * 100)}%` }} />
        </div>
        <p className="gate-note">
          {total > 0 ? `${done} / ${total} 枚` : "準備しています"}
          {total > 0 ? `（${Math.round(ratio * 100)}%）` : ""}
        </p>
      </section>
    );
  }

  if (analysisState === "error") {
    return (
      <section className="analysis-gate screen-enter" aria-label="解析エラー">
        <span className="gate-mark offline">!</span>
        <h2>解析を開始できませんでした</h2>
        <p>{errorMessage || "しばらく待ってからもう一度お試しください。"}</p>
        <button className="primary-action gate-action" onClick={onStart} disabled={busy}>
          もう一度試す
        </button>
      </section>
    );
  }

  if (analysisState === "absent") {
    return (
      <section className="analysis-gate screen-enter" aria-label="解析の開始">
        <span className="gate-mark">✦</span>
        <h2>まず写真を解析します</h2>
        <p>
          撮影フォルダのJPGとARWを読み取り、似たカットのグループ分け・ピント・白飛び・黒つぶれの採点までを行います。
          初回は数十秒かかります。
        </p>
        <button className="primary-action gate-action" onClick={onStart} disabled={busy}>
          {busy ? "開始しています…" : "解析を開始"}
        </button>
        <p className="gate-note">元のJPG / ARWは変更されません。</p>
      </section>
    );
  }

  return (
    <section className="analysis-gate screen-enter" aria-label="解析状況の確認">
      <span className="gate-mark running" />
      <h2>解析状況を確認しています</h2>
      <p>ローカルRAWエンジンに接続しています。しばらくお待ちください。</p>
    </section>
  );
}
