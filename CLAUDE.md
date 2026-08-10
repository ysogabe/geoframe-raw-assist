# GeoFrame RAW Assist — プロジェクト運用ルール

## 作業フロー（必須）

- 実装・変更作業は**必ず作業ブランチを作成してから実施**する。`main` への直接コミット禁止。
  - ブランチ名: `<type>/<短い説明>`（例: `fix/ui-copy`、`feat/typography`）
  - 完了後は検証（ビルド・型検査・必要なら実機確認）を経て `main` へマージする。
  - このリポジトリはローカル専用（リモートなし）。PR 運用は行わず、ブランチ → マージをローカルで完結させる。
- **サブエージェントで並行開発するときは `git worktree` を使って実施**する。
  - 作業ブランチごとに `git worktree add` で作業ツリーを切り、サブエージェントには worktree のパスを渡す。
  - worktree には `node_modules` / `.venv` / `dist` / `target` が無いため、検証前に依存の再インストールが必要な場合がある。
  - 検証はメインスレッドが実施してからマージする（サブエージェントの完了報告は鵜呑みにしない）。
- 実装フェーズの運用契約は `orchestrator-qa` スキルに従う（メインはオーケストレータ + QA、実装コードはサブエージェントへ委譲。ただし単一ファイルの小修正・機械的変更はメインが直接実施してよい）。

## ビルド / 検証コマンド

- フロントビルド: `npm run build`（= vite build）／型検査: `npx tsc --noEmit`／lint: `npm run lint`
- デスクトップ（Tauri）ビルド: `env -u CI npm run tauri:build`
  - 環境に `CI=1` が設定されており、そのままだと tauri CLI が `--ci 1` エラーになるため `CI` を必ず外す。
- ローカル実行: `npm run dev:raw`（RAW エンジン http://127.0.0.1:8788 + vite http://localhost:1420）
- Python テスト: `npm run test:raw`

## 環境の注意

- macOS + zsh。`timeout` コマンドは無い（`gtimeout` かコマンド自身のタイムアウトを使う）。zsh は unquoted 変数を word-split しない。
- 画面表示の検証はブラウザ（vite dev）で行う。デスクトップアプリの確認は `tauri:build` 後の .app を起動して行う。
