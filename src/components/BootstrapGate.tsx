import type { SetupState } from "../lib/setup";

type BootstrapGateProps = {
  setup: SetupState;
};

/**
 * `.venv` が無い初回起動時にRust側が自動実行するセットアップ（venv作成 → pip install）の
 * 進捗を表示するゲート。AnalysisGate より前段に置かれ、stage が "ready" / "succeeded" に
 * なるまで既存の解析フローには進ませない。
 */
export function BootstrapGate({ setup }: BootstrapGateProps) {
  if (setup.stage === "failed") {
    return (
      <section className="analysis-gate screen-enter" aria-label="初回セットアップの状態">
        <span className="gate-mark offline">!</span>
        <h2>初回セットアップに失敗しました</h2>
        <p>{setup.message || "セットアップ中にエラーが発生しました。"}</p>
        <p className="gate-note">
          ターミナルの出力を確認するか、<code>npm run setup:raw</code> を手動で実行してから、この画面を再読み込みしてください。
        </p>
      </section>
    );
  }

  const logTail = setup.log.slice(-20);

  return (
    <section className="analysis-gate screen-enter" aria-label="初回セットアップの進捗">
      <span className="gate-mark running" />
      <h2>初回セットアップを実行しています</h2>
      <p>{setup.message}</p>
      {logTail.length > 0 && (
        <pre className="gate-log" aria-label="セットアップログ">
          {logTail.join("\n")}
        </pre>
      )}
      <p className="gate-note">初回のみ数分かかることがあります。次回以降のこの画面はスキップされます。</p>
    </section>
  );
}
