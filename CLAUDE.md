# GeoFrame RAW Assist — プロジェクト運用ルール

## 作業フロー（必須）

- 実装・変更作業は**必ず作業ブランチを作成してから実施**する。`main` への直接コミット禁止。
  - ブランチ名: `<type>/<issue番号>-<短い説明>`（例: `fix/12-ui-copy`、`feat/13-typography`）。Issue が無い場合は `<type>/<短い説明>`。
  - 完了後は検証（ビルド・型検査・必要なら実機確認）を経て PR を作成し、`main` へマージする。
  - リモートは `origin`（github.com/ysogabe/geoframe-raw-assist）。作業は GitHub Issue を立ててから対応ブランチを切り、PR 経由でマージする（`github-pr-workflow` スキルに従う）。
- **サブエージェントで並行開発するときは `git worktree` を使って実施**する。
  - 作業ブランチごとに `git worktree add` で作業ツリーを切り、サブエージェントには worktree のパスを渡す。
  - worktree には `node_modules` / `.venv` / `dist` / `target` が無いため、検証前に依存の再インストールが必要な場合がある。
  - 検証はメインスレッドが実施してからマージする（サブエージェントの完了報告は鵜呑みにしない）。
- 実装フェーズの運用契約は `orchestrator-qa` スキルに従う（メインはオーケストレータ + QA、実装コードはサブエージェントへ委譲。ただし単一ファイルの小修正・機械的変更はメインが直接実施してよい）。

## ビルド / 検証コマンド

- フロントビルド: `npm run build`（= vite build）／型検査: `npx tsc --noEmit`／lint: `npm run lint`
- フロント単体テスト: `npm test`（= `vitest run`、reducer など純粋関数。node 環境）
- デスクトップ（Tauri）ビルド: `env -u CI npm run tauri:build`
  - 環境に `CI=1` が設定されており、そのままだと tauri CLI が `--ci 1` エラーになるため `CI` を必ず外す。
- ローカル実行: `env -u CI npm run tauri:dev`（Python サーバは使わない。単一アプリ化 ADR 0003）
- Rust テスト: `cargo test --manifest-path src-tauri/Cargo.toml`（事前に `npm run build:coreimage`）／ Swift: `swift test --package-path native/coreimage-cli`
- Python（`scripts/` の評価用と移行期間の参照実装のみ。#117 で削除予定）: `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`

## 環境の注意

- macOS + zsh。`timeout` コマンドは無い（`gtimeout` かコマンド自身のタイムアウトを使う）。zsh は unquoted 変数を word-split しない。
- 画面表示の検証は Tauri（`tauri:dev`、または `tauri:build` 後の .app）で行う。ブラウザ（vite 単体）では動かない。

## orchestrator-qa スキルの OMP ハーネス対応

`orchestrator-qa` スキル本文は Claude Code 前提の記述を含む。この OMP ハーネスで適用する際の対応表:

| スキル本文（Claude Code 前提） | OMP での実態 |
|---|---|
| `model: sonnet / opus / haiku` の指定 | **不可**。`task` ツールの `agent` 型がモデルを決定（`scout` は高速モデル等）。`model` パラメータは存在しない |
| `subagent_type`: `Explore` / `general-purpose` / `claude` | `scout`（読み取り専用調査）/ `designer`（UI）/ `reviewer` / `security-reviewer` / `librarian` / `task`（汎用 worker）/ `sonic`（機械的反復のみ） |
| `EnterWorktree` ツール | 存在しない。`git worktree add` を bash で実行する |
| `ExitPlanMode` / `/goal` / `/loop` | 該当概念なし。プラン承認は会話上で行う。外部状態の待機は `hub wait` / `hub jobs` |
| `TaskCreate` / `TaskUpdate` | `todo` ツール（タスク名は内容文字列で参照） |
| `Monitor` ツール | `hub jobs` / `hub wait`（ジョブ完了の自動通知） |

契約の規範部分（メインは実装コードを書かない／委譲プロンプトの自己完結性／QA ゲート＝完了報告を鵜呑みにしない／失敗時 3 回までの再実装／コミット・PR はメインの責務）は OMP でもそのまま有効。
