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

## 初稿時点の未完了 / 結合条件

- Swift CLI 0.8の実merge経路との結合は未実施。Rustテストではmock CLIを使用した。
- 実ブラケットARWでの合成画質、実EXIF/GPS保持を伴う実写確認は未実施。実画像・ユーザーフォルダへの書き出しは行っていない。
- `MergedResult` のフロント呼び出し/UI復帰は後続担当。
- #84本文・コメントはGitHub API接続エラーのため取得できず、指定briefと設計原本 §2/§3/§6 に基づいて実装した。

## 追補: UI語彙契約と実CLIスモーク

- `lens_reason_vocabulary_is_stable` は UI が参照する `tests/fixtures/coreimage_merge_lens_reasons.json` を Rust 側で読み、2定数と完全一致することを確認する。
- `docs/API_CONTRACT.md` に Core Image 固有の2理由語彙と意味、preview の `max_edge: 2400` / full の `max_edge` 省略、固定 Tauri 引数を追記した。既存 Python ブラケット仕様は変更していない。
- `real_cli_merge_export_smoke` は `#[ignore]` の実CLI結合テスト。実RAWを一時sourceへ2つの異なるIDでコピーし、実位置情報を含まないsynthetic GPS JPEGをlead pairに使う。preview/full・cache hit・full export/manifest・preview export拒否を確認し、元RAWのバイト一致も末尾で確認する。出力ログにGPS値や画像データを表示しない。
- 既存のmock CLIテストはSwift CLI 0.7.0を模したテスト用プロセスで、CLI 0.7.0ビルド時点のRust単体検証。merge mode未対応のため、実CLIのmerge結合を検証したものではない。
- 指定された実行ファイルは `--version` で CLI 0.8.0 / macOS 27.0.1、arm64 と確認した。このsandbox内の実行ではMetal kernel library初期化エラーが発生した（初稿時点の記録）。

実スモークはCargoの書き込み可能な一時targetを使い、次の形で実行した（実際のパスは親brief記載のもの）。

```sh
CARGO_TARGET_DIR="/private/tmp/codex-merge-smoke-target-84" \
GEOFRAME_MERGE_SMOKE_CLI="/Users/yoshio/.t3/worktrees/40-raw-assistant/t3-11fe9636/src-tauri/binaries/geoframe-ci-develop-aarch64-apple-darwin" \
GEOFRAME_MERGE_SMOKE_ARW="/Users/yoshio/works/60-misc/40-raw-assistant/work/raw/DSC07415.arw" \
cargo test --manifest-path src-tauri/Cargo.toml real_cli_merge_export_smoke -- --ignored --nocapture
```

## 追補: 合成cacheレシピの整合性

- cache hit は `capture_ids`、preset名/label、adjustments、preview、canonical source root、出力パスを含む完全な request、`params_hash` が今回の期待値と一致する場合に限る。結果を復元する必須metadataと各frameを検証し、`lens_correction` 要約もframe群から再計算した値と一致する場合だけ採用する。
- cacheの不一致・JSON破損はmissとして扱い、slot lock内の再確認後に期待する出力先の合成JPEGと対応するレシピJSONだけを取り除いて再生成する。これは同じ出力パスに残った不正cacheが再書き込みを妨げるのを避けるためで、他のファイルは削除しない。通常のCore Image現像は `ResultContext` 側で入力と調整値を強制する経路のため、この修正対象ではない。
- `merge_cache_requires_matching_complete_recipe` は正常cache hitと、hash/request/preview/ID/preset/adjustments/source root/lens summaryの不一致、不完全JSONそれぞれの再生成を確認する。`mock_cli_accepts_canonical_aliases_and_rejects_mismatches` は同じ実体を指すalias入力・frame入力・出力を受理してcanonical pathに正規化し、別ファイル・別root・順序違い・非文字列要素・不一致frame・不一致出力を拒否する。
- `CARGO_TARGET_DIR=/private/tmp/codex-merge-smoke-target-84 cargo test --manifest-path src-tauri/Cargo.toml bracket::tests:: -- --nocapture` は **6 passed / 0 failed / 1 ignored**。ignored は環境変数が必要な実CLIスモークで、親担当の再実行対象。
- sandbox内の実CLI 0.8.0スモークはMetal kernel library初期化エラーでpreview中に停止。親の権限昇格実行でpreview/full成功済みとの確認を受け、cache修正後のignoredスモーク最終実行は親担当。

## 最終QA追記

- 親担当の権限昇格による実CLI `real_cli_merge_export_smoke` は **1 passed / 16.91s**。preview/full、cache hit、EXIF/GPS・orientation・dimensions・Software・MakerNote、export manifest bytes、preview export拒否、source RAW不変を確認した。
- Rust全体は **148 passed / 8 ignored / 0 failed**。フロントは132 tests、types、lint、build、diffcheckが成功。
- `cargo build` は成功。`src-tauri/src/bracket.rs` の `PathBuf` unused import warningはこの小修正でtests module内へ移動した。
- 同一RAWの複製とsynthetic GPSを使った確認であり、実AEB画質と実機GUI確認は引き続きpending。


### 出力JPEG破損検知

- cache recipeへEXIF継承後の出力JPEGの `output_sha256` と `output_size` を記録する。`MERGE_SCHEMA` を2へ上げ、以前のレシピを再生成させる。
- cache hitでは通常ファイル検査に加え、非ゼロサイズと実ファイルのSHA-256/sizeがrecipeと一致することを要求する。recipe欠落・不正値・JPEGの置換/破損/空ファイルはcache missとして再生成する。
- merged exportも同じ指紋を再検証し、指紋が欠落または不一致の画像は拒否する。JPEGデコードは追加せず、mockのsynthetic bytesも保存時の内容fingerprintで検査する。
- `merge_cache_requires_matching_complete_recipe` に出力差替えと0 byteの再生成を追加し、`exports_only_verified_full_merged_recipe` に改変/空JPEGの拒否を追加した。
