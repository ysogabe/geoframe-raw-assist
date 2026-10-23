# Core Image ブラケット合成 UI 復帰レポート

## 変更内容

- `mergeBracketCoreImage` を追加し、Rust の `merge_bracket` コマンドへ `captureIds`、preset 名、9 キー調整値、preview/full、`force: false` を渡すようにしました。Rust が返す `MergedResult` は変換せず利用します。
- `OperationPanel` にブラケットフレーム選択、プレビュー/本合成、合成タイル、比較タブ、原寸ビューア、採用導線を再接続しました。グループと capture ID の既存selectorを通し、結果を別グループから引き当てません。
- 現像・候補生成・合成のglobal busyを共有し、重い処理の重複開始と実行中の設定変更を止めました。グループ移動は引き続き可能です。現在のフレーム構成・プリセット・調整値に合わない合成結果は操作画面に表示せず、古い出力と現行設定が混ざらないようにしました。
- `merge/fail` にrequest generationとgroup IDを追加しました。フォルダ世代または実行中groupが一致しない失敗はreducerで破棄し、現在のbusy/noticeを維持します。
- reducerでも新規採用は現在のgroupに属する本合成だけを許可し、プレビューや別groupの結果を直接actionで採用できないようにしました。既存採用の解除は引き続き可能です。
- canonical fixture `tests/fixtures/coreimage_merge_lens_reasons.json` にレンズ理由2語を追加し、それぞれ異なる日本語labelに変換され、生のcodeが表示されないことを検証します。

## Backend契約

呼び出し形は `invoke<MergedResult>("merge_bracket", { captureIds, preset: preset.name, adjustments, preview: mode === "preview", force: false })` です。maxEdgeは渡しません。プレビュー2400固定、EXIF/GPS継承、本合成のファイル構成はRust側の責務です。CLI/実装情報は画面に追加していません。処理時間案内は「プレビューでも数秒、本合成は数十秒」の既存表現を保っています。

## 検証

- `npm ci` 成功
- `npx tsc --noEmit` 成功
- `npm test` 成功（6 files / 132 tests）
- `npm run lint` 成功
- `npm run build` 成功
- `git diff --check` 成功
- dev server / Tauri app は起動していません。実GUIとRust応答の結合確認は親側の後続QAが必要です。

## 作業状態

- branch: `codex/84-merge-ui`
- worktree: `/Users/yoshio/.t3/worktrees/40-raw-assistant/t3-9130b8a6`
- 変更は未stage・未commitです。
- 親担当のbranch管理、stage、commit、push、PR作成は行っていません。
