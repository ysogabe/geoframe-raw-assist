# #84 Core Image ブラケット合成 Rust 実装報告

## 実装範囲

- `src-tauri/src/bracket.rs` を追加し、Tauri コマンド `merge_bracket` を登録した。ID を `DSC` + 数字5桁に限定し、2〜9枚・重複なしを検証して昇順に整列する。プリセット既定値へ調整値をマージし、既存の normalize 規則で9キーを正規化する。
- Swift CLI へは1リクエストで `mode: merge` を送る。プレビューは `max_edge: 2400`、本合成は `max_edge` 無し。設定、JPEG品質、重み、位置合わせ指定、タイムアウト（preview 120秒 / full 240秒）を契約値に固定し、`DEVELOP_SLOT` を共用する。
- 保存先を `coreimage/merged` と `coreimage/merged_preview` に分離し、既存の出力先 symlink 防止・canonical containment、sidecar実行、通常ファイルのみのcache hit、force、原子的なレシピ保存、cache pruneを再利用した。キャッシュはリクエスト、プリセット、source root、全RAWのサイズ/mtime、CLI version応答とbinary SHA、full時の先頭ペアJPEG SHAとEXIF schema/softwareを含む。
- 本合成のみ先頭IDのペアJPGから既存 `exif_inherit` でEXIF APP1を継承する。GPS IFDはJPEGバイト内で保持し、GPS値をレシピやログに抽出しない。継承失敗は `exif_inherited: false` で続行する。
- `export.rs` に `merged` variant を追加した。本合成の通常JPEGと正規レシピだけを許可し、preview/max_edge、capture ID形式・枚数・順序・重複・先頭ID、preset/filename/hash、source root、全入力RAW、実出力先を再検証する。manifestにはレシピ全体を格納し、leadのARWをsourceに記録する。

## API / フロント連携

戻り値は `src/lib/types.ts` の既存 `MergedResult` に合わせた。`output_url` はTauri asset URLとmtime query、`elapsed_seconds` はCLI total msから秒へ変換する。レンズ補正理由語彙はRust定数・テストで固定した。

- 全フレーム適用: `lens_correction.applied = true`
- 全フレーム未対応: `reason = "coreimage_unsupported"`
- フレーム間で不一致: `reason = "inconsistent_across_frames"`

フロント担当はこの2つの理由文字列をそのまま照合できる。UI型やフロントファイルは変更していない。

## 検証

- `npm ci`: 成功。
- `npm run build:coreimage`: 成功。作成したCLIはbaseの0.7.0で、merge modeはまだ未対応。
- `cargo test`: 146 passed、0 failed、7 ignored。テスト用synthetic/tmpのmock CLIで、リクエストJSON、sorted inputs、merged/preview出力分離、recipe、cache hit、force、source root切替、ペアJPEG/RAWサイズ/CLI binary変更、CLI構造化エラーのcache key更新・応答を確認した。exportはfull manifest成功とpreview、lead/count、root外、順序/重複、max_edge、symlinkの拒否を確認した。
- `cargo build`: 成功、warning 0。変更したRustファイルの `rustfmt --check` と `git diff --check` も成功。

## 未完了 / 結合条件

- Swift CLI 0.8の実merge経路との結合は未実施。Rustテストではmock CLIを使用した。
- 実ブラケットARWでの合成画質、実EXIF/GPS保持を伴う実写確認は未実施。実画像・ユーザーフォルダへの書き出しは行っていない。
- `MergedResult` のフロント呼び出し/UI復帰は後続担当。
- #84本文・コメントはGitHub API接続エラーのため取得できず、指定briefと設計原本 §2/§3/§6 に基づいて実装した。
