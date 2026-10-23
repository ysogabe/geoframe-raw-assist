# Issue #87: PSO 探索ループの Rust 移植・実測報告

2026-10-22。ブランチ `feat/87-pso-loop-rust`、作業ツリー
`/Users/yoshio/works/60-misc/wt-87-pso-loop`。コミット・push・PR 作成なし。
Issue #87 の本文と全コメント、#57、ADR 0003 §4、M3 計画、CLI 0.7.0 の decode 契約、
Swift `Adjustments.swift`、PR #126 の削除前ハンドラを確認した。

## 実装

- `src-tauri/src/candidates/pso.rs`: 粒子初期化、慣性 0.56 / 個人最良 1.25 / Pareto リーダー 1.40 の更新、目的関数、スタイル目標、探索後の安全判定と候補選択。
- `src-tauri/src/candidates/renderer.rs`: 1本の `BatchSession`、decode ハンドル共有、指標のメモ化、一時ファイルの回収、ハンドル消失時の1回再decode。
- `src-tauri/src/candidates/command.rs`: `generate_development_candidates`、予算・ID・プリセット検証、スタイル発動条件、理由と rejected、重複時の目標寄せ、時間と進捗。
- `src-tauri/src/candidates/loop_tests.rs`: seed / 予算 / 固定系列 / 安全策 / decode 共有と復旧 / 後片付け、および手動 release 計測としきい値調査。
- `src-tauri/src/candidates/mod.rs`: 探索ループを接続し、モジュール全体の `allow(dead_code)` を撤去。
- `src-tauri/src/coreimage.rs`: 既存 BatchSession、sidecar 解決、現像実行枠を crate 内で共有。セッションの検査専用メソッドは test のみ。
- `src-tauri/src/main.rs`: コマンド登録。
- `src/App.tsx`: invoke と既存 `candidates/start / ok / fail / apply` の接続。フォルダ世代を await 前に確保。
- `src/components/DevelopControls.tsx` / `OperationPanel.tsx`: `<DevelopmentCandidates />` を復帰。探索中のプリセット変更・現像・再生成を抑止。
- `src/lib/types.ts`: `proxy.source = coreimage_proxy`、decode / render / 評価回数、`elapsed_ms`。候補・理由・rejected の構造は継続。
- 本報告。Swift と Python の参照実装・凍結基準は変更していない。

コマンド引数は `{ captureId, preset, seed?, budget? }`。seed は u32、budget は32〜512、既定48。
async コマンドの `spawn_blocking` 内で既存の `DEVELOP_SLOT` を確保し、現像と同時に CLI を増やさない。
進捗イベント `development-candidates-progress` は `{ capture_id, preset, stage, completed, total }`。
UI は探索中表示を使い、イベントの細かな数値表示は追加していない。
応答の `elapsed_ms.total` はソース解決・EXIF・CLI 起動・全評価・release / 終了待ちを含む。
実行枠の待機時間は含まない。decode / render は CLI 応答の total の合計。

## 探索空間と decode 回数

`Adjustments.swift` の `HighlightRecoveryMapping` は hr を実効 exposure と shadow_lift に加算する。
実効 exposure は CIRAWFilter のデコード段、brightness はその後の表示用トーン処理なので同値ではない。
brightness で露出を代用すると、ハイライトの残り方と RAW デコード結果の一致を保証できない。

- `highlight_class` を除去し、`highlight_recovery` と `wb_temp` はプリセットのまま固定する。
- 露出は **プリセット値と ±0.5EV の3格子**。両端は −1.5〜1.5 へ制限する。PSO の露出座標を最近傍格子へ写像する。
- 後段は shadow_lift / brightness / contrast / saturation / sharpness を探索。shadow_lift は小数1桁、その他は小数4桁へ丸め、実際に採用する値で指標・安全性・距離を計算する。
- sharpness >= 1 は decoder sharpness が共通なので、1〜1.6 の後段 USM だけを探索。プリセットが1未満（portrait_soft 等）ならその値に固定し、decoder sharpness を変える探索は行わない。
- NR 固定表: natural = プリセットそのもの、stars / foreground / social = light、low_noise = full。off は探索しない。#81 の確定済み CI NR 写像をそのまま使う（light は Apple 既定、full の輝度増分0.3・上限0.5、色下限0.3）。
- decode キーは exposure / hr / WB / min(sharpness,1) / NR。CLI には decoder 9、apple_default、レンズ補正ON、長辺640を渡す。
- 1実行で最大 **3露出 × 2NR = 6 decode**。low_noise が発動せず NR が1種類なら最大3回。CLI 上限32件・256MiBより十分小さいため、この有限集合を実行終了まで共有する。全画像の decode は約13MiB。
- 最初の世代で格子を巡回させ、その後の PSO でも同じ格子を再利用する。release は最後にまとめて行い、成功・エラー・panic でセッションと、この実行だけの一時ディレクトリを回収する。
- 既定48評価は natural 以外に均等配分。粒子数 `min(8,max(4,per_style/2))`、世代数 `max(1,per_style/particles)` は移植元と同じ。端数を切り捨てるため48未満の場合がある。重複候補の目標寄せは移植元と同様に追加評価。
- 640px sRGB、品質1.0の一時JPEGを読み取って評価する。画像は保持しない。応答の `proxy.renders` はメモ化を除いた実描画数、`proxy.evaluations` は PSO の評価要求数（追加の重複解消を除く）。

既定 seed は `SHA256(capture_id:preset)` の先頭4バイトを big endian の u32 にする。
スタイル seed は `SHA256(seed:style)` の先頭4バイトを little endian で読む。
乱数は SplitMix64 + Box-Muller。numpy の乱数列は再現しない（ADR 0003）。
同じ seed と同じ指標なら同じ結果になることをテストし、#107 の48点を再生して
Pareto 選択後の候補が固定済み Python 出力と一致することも確認した。

## しきい値の見直し

| 項目 | Python / 移植前 | Rust の採用値 | 根拠 |
|---|---|---|---|
| sharpness 探索上限 | 1.47 | **1.6** | rawpy の「基準gainも含む USM」の飽和式は CI に当てはまらない。CI の原寸増分は `min(1.2, 0.7*(s-1)*1.5)`、飽和は `1 + 1.2/(0.7*1.5) = 2.142857`。契約上限1.6まで差があるため1.6を使う。 |
| low_noise の画像側発動値 | noise >= 0.045 | **据え置き0.045** | 実測残差は下記。今回は全件 ISO>=1600 側で発動し、誤検出も欠落もない。昼夜混在13ケースが入力不足のため、ISO 条件のない低ISO画像での妥当な境界を推定できない。 |
| detail | mean(gradient)*7 + std(gradient)*2 | **据え置き7 / 2** | 下記の自然候補で両経路の値域が重なる。一律の倍率変更は低コントラストと高コントラストを同時に改善する根拠がない。source_detail の5 / 1も継続。 |
| 星空 EXIF ゲート | 露光>=2s、EV100<=−2 | **据え置き** | 固定基準と全11枚で一致。DSC07402 は露光1/13sであり、星密度の調整によって長秒条件を迂回させない。 |
| 星空の局所点検出 | sigma3、差0.04 / 0.08、密度>=0.0008 | **据え置き** | 夜間10枚は密度が十分に上回る。閾値を下げる必要がない。 |
| 大面積光源 / EXIF無し | 大光源0.85以上の面積<=0.02、EXIF無しの密度>=0.002・暗部>=0.72 | **据え置き** | 新しい非星空の対照群がないため緩めない。 |
| 最終 stars 選択 | star_loss の目的関数のみ | **基準以上の星数も必須** | 元の目的関数を維持しつつ、star_loss が他指標との兼ね合いで星数減少を許す穴を最終選択・重複解消の両方で閉じる。 |

apple_default の発動用ノイズ実測は 0.012296〜0.026866（Gaussian sigma1 の残差標準偏差）。
natural の detail は Python 比 0.707〜1.121 倍で、増減の両方がある。夜間10枚の局所点密度は 0.002631〜0.013071。

## 実測条件・所要時間

- macOS 27.0.1 (26A434)、aarch64、Rust 1.99.1、CLI 0.7.0 / decoder 9。
- 指定の DSC074xx ARW / ペアJPGを読み取り専用で使用。seed は既定、budget48、nightscape、640px。
- `npm run tauri:build -- --bundles app` で release `.app` を生成。
- `.app/Contents/MacOS/geoframe-ci-develop` を使い、コマンドと同じ `generate` 関数を `cargo test --release` の手動テストから実行。アプリ UI / IPC の往復時間は未測定。dev サーバー・tauri dev は起動していない。
- 1ケースごとに CLI を新規起動。以下は起動・終了を含む時間で、warm セッション50回の平均値ではない。
- `.app` 同梱 sidecar での最初の1枚は9秒台だった（decode合計6.841秒、render合計0.469秒）。独立した先行測定の同じ画像は5.306秒だった。遅延原因は特定しておらず、速い反復値だけに置き換えない。

`.app` 同梱 sidecar の測定は **5.271〜9.078秒、平均5.860秒、中央値5.566秒**。全11枚が10秒以内。
最初の sidecar 直接パスでの独立測定は 5.143〜6.094秒、平均5.626秒。
Python 凍結時の候補探索は全11枚162.178秒（平均14.743秒、基準 README）。ビルドや依存準備を含めず比較すると、新経路は2倍以内の旧目安も満たす。

## Python 経路との比較表

N=natural、T=stars、F=foreground、L=low_noise、S=social。各行のプリセットは nightscape。星数欄はそれぞれの経路の natural → stars 候補。

| 画像ID | Python 候補 / 数 | Rust 候補 / 数 | Python 星数 | Rust 星数 | 秒（.app sidecar） | decode / render |
|---|---|---|---:|---:|---:|---:|
| DSC07402 | N/F/L/S / 4 | N/F/L/S / 4 | 対象外 | 対象外 | 9.078 | 6 / 47 |
| DSC07404 | N/T/L/S / 4 | N/T/L/S / 4 | 31 → 176 | 67 → 68 | 5.576 | 6 / 49 |
| DSC07405 | N/T/L/S / 4 | N/T/L/S / 4 | 16 → 316 | 37 → 48 | 5.566 | 6 / 49 |
| DSC07407 | N/T/L/S / 4 | N/T/L/S / 4 | 429 → 354 | 301 → 306 | 5.734 | 6 / 50 |
| DSC07408 | N/T/L/S / 4 | N/T/L/S / 4 | 76 → 73 | 61 → 61 | 5.876 | 6 / 50 |
| DSC07412 | N/T/L/S / 4 | N/T/L/S / 4 | 1355 → 1317 | 919 → 920 | 5.886 | 6 / 51 |
| DSC07415 | N/T/L/S / 4 | N/T/L/S / 4 | 359 → 370 | 1045 → 1047 | 5.377 | 6 / 51 |
| DSC07416 | N/T/L/S / 4 | N/T/L/S / 4 | 312 → 321 | 1016 → 1017 | 5.292 | 6 / 51 |
| DSC07419 | N/T/L/S / 4 | N/T/L/S / 4 | 45 → 48 | 77 → 85 | 5.271 | 6 / 50 |
| DSC07422 | N/T/L/S / 4 | N/T/L/S / 4 | 690 → 696 | 796 → 798 | 5.418 | 6 / 51 |
| DSC07423 | N/T/L/S / 4 | N/T/L/S / 4 | 347 → 361 | 1031 → 1032 | 5.380 | 6 / 50 |

全11枚で種類・数が一致（計44候補）、rejected は双方0。星空判定された10枚の Rust stars は全件 natural 以上。
DSC07402 の「stars なし」は欠落ではなく、両経路の同じ EXIF ゲートの結果。「星空11ケースすべてにstars」という Issue 本文の期待と、凍結基準の実際の出力にはこの1件の差がある。

自然候補の主要指標（**Python → Rust**）。白飛び・黒つぶれは画素割合の%、noise / detail は0..1。

| 画像ID | 白飛び % | 黒つぶれ % | 平坦部noise | detail |
|---|---:|---:|---:|---:|
| DSC07402 | 0.0613 → 0.0904 | 17.074 → 23.424 | 0.007038 → 0.006923 | 0.210 → 0.212 |
| DSC07404 | 0.0336 → 0.0348 | 8.294 → 10.319 | 0.008675 → 0.005774 | 0.277 → 0.211 |
| DSC07405 | 0.0026 → 0.0051 | 8.439 → 8.905 | 0.007750 → 0.004733 | 0.239 → 0.169 |
| DSC07407 | 0.0051 → 0.0307 | 1.301 → 3.217 | 0.008463 → 0.007251 | 0.282 → 0.244 |
| DSC07408 | 0.4717 → 0.5255 | 2.459 → 8.905 | 0.012423 → 0.010751 | 0.425 → 0.371 |
| DSC07412 | 0.0500 → 0.1614 | 0.000 → 0.004 | 0.009224 → 0.010124 | 0.344 → 0.325 |
| DSC07415 | 0.0095 → 0.0615 | 0.001 → 0.019 | 0.009924 → 0.012393 | 0.360 → 0.404 |
| DSC07416 | 0.0077 → 0.0527 | 0.001 → 0.021 | 0.009792 → 0.012085 | 0.356 → 0.397 |
| DSC07419 | 0.0029 → 0.0121 | 0.809 → 0.966 | 0.006722 → 0.005342 | 0.222 → 0.192 |
| DSC07422 | 0.0069 → 0.0454 | 0.000 → 0.012 | 0.010780 → 0.012660 | 0.391 → 0.406 |
| DSC07423 | 0.0150 → 0.0457 | 0.001 → 0.008 | 0.008080 → 0.010056 | 0.297 → 0.328 |

新経路の候補による白飛び増加は natural 比最大 **0.2466ポイント**、黒つぶれ増加は最大 **2.079ポイント**で、安全策の10ポイントより小さい。
low_noise は全11枚で natural 以下の平坦部ノイズを維持した。
白飛びが極端に増える・候補が消える事象はなかった。

基準画像そのものの黒つぶれ増加（DSC07402 / DSC07408）は、同じプリセット名でも
#81 で nightscape の hr が55から0へ変わったことと、LibRaw / CIRAWFilter のトーン・NR・縮小デコードが異なることを含む。
今回の PSO が natural に調整を加えた結果ではない。natural はプリセットそのもの。
DSC07402 は foreground で黒つぶれ23.424%から約0.003%まで減り、白飛びも増えていない。
星数の絶対値が経路間で最大約3倍になる画像は、閾値・領域分割をデコーダとプリセットの異なる出力へ適用した結果。
Python の絶対星数に合わせる係数は追加せず、同じ CI natural と CI 候補の比較に使う。

## 再実行

```sh
npm ci && npm run build:coreimage
npm run tauri:build -- --bundles app
GEOFRAME_PSO_RAW_ROOT=/Users/yoshio/works/60-misc/40-raw-assistant/work/raw \
GEOFRAME_PSO_SIDECAR="$PWD/src-tauri/target/release/bundle/macos/GeoFrame RAW Assist.app/Contents/MacOS/geoframe-ci-develop" \
cargo test --release --manifest-path src-tauri/Cargo.toml candidates_release_benchmark -- --ignored --nocapture
GEOFRAME_PSO_RAW_ROOT=/Users/yoshio/works/60-misc/40-raw-assistant/work/raw \
cargo test --release --manifest-path src-tauri/Cargo.toml candidate_threshold_survey -- --ignored --nocapture
```

結果: `work/issue87/measurements.json` / `threshold-survey.json`。
先行測定は `measurements-initial.json` に保存。これらは gitignore 対象。
結果は候補調整値・指標・時間・許可した撮影技術項目のみで、画像や位置情報は保存しない。

## 検証

- `cargo test --manifest-path src-tauri/Cargo.toml`: **137 passed / 7 ignored**。手動テストは上記で別途実行。
- `cargo build --manifest-path src-tauri/Cargo.toml`: **警告0**。
- `npx tsc --noEmit`: 成功。
- `npm test`: **127 passed**。
- `npm run lint`: 成功。
- `npm run build`: 成功。
- `npm run tauri:build -- --bundles app`: 成功。
- Swift の変更なしのため `swift test` は実行していない。
- `git diff --check`: 成功。

## 残課題

- **#57 は継続**。CI の同じ後段コードを使うが、640px と原寸では captureSharpenBase が sigma0.6 / baseGain0.5 と sigma0.8 / baseGain0.7 に変わる。1.47上限の由来をCIに合わせて修正しただけでは、画像の画素スケールを含む差を消せない。full-size 現像と代理の detail / halo の誤差測定・補正は別途必要。
- CLI decode キャッシュの half-float 中間固定と通常の一体グラフの差（#116 README）、品質1.0 JPEG の量子化も代理と原寸出力の差に残る。今回の比較は凍結された Python の代理指標との比較であり、原寸出力の画素一致試験ではない。
- `image_work` の昼夜混在13ケースは元ARWがなく未比較。noise 発動閾値、星の誤陽性、救済系プリセットの hr 固定時の品質は追加データで確認する。
- 露出の探索は±0.5EVの局所3格子に意図的に絞った。遠い露出や sharpness<1 の連続探索は decode 増大と引き換えになるため、要件が出たら予算と一緒に見直す。
- `.app` 自体はビルド済みだが UI からの手動操作確認・Tauri IPC の往復時間は未測定。測定は本番と同じ Rust 関数の release 手動テストで行った。
- apple_default は OS 依存。OS更新時は本テストと #75 の回帰で再測定する。

## 引き渡し時の Git 状態

作業ブランチは `feat/87-pso-loop-rust`、開始コミット `3297941`。
変更はすべて未コミット・未ステージ。別作業ツリーは編集していない。
primary の main は追跡ファイルの変更なし、既存の未追跡 `resume.txt` があるため完全な clean 状態ではない。このファイルは未変更。
