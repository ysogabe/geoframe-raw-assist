# GeoFrame RAW Assist — ドキュメント

アプリの紹介は [README](../README.md) にあります。ここには使い方・設計・開発の記録をまとめています。

## 利用者向け

| 文書 | 内容 |
|---|---|
| [README](../README.md) | アプリの紹介、機能、ビルド方法 |
| [USAGE.md](USAGE.md) | 使い方。ビルドと起動、写真の置き場所、操作の流れ（解析から書き出し・「写真」登録まで）、現像プリセット、うまくいかないとき、開発者向けのテストと確認手順 |
| [DESIGN.md](DESIGN.md) | 技術設計。3 つのツリーの役割と処理の流れ、保存先とキャッシュ、現像・解析・ブラケット合成・現像候補、書き出し、テストの構成、設計判断の要約 |
| [LP.md](LP.md) | LP（紹介ページ `site/`）の管理。紹介文と画像の更新、ローカルでの確認、公開の手順 |

## 仕様と設計

| 文書 | 内容 |
|---|---|
| [API_CONTRACT.md](API_CONTRACT.md) | フロント（React）⇔ Rust（Tauri）のコマンド契約。コマンド名・引数・戻り値・エラー code・保存先・EXIF/GPS 継承 |
| [adr/0001-coreimage-raw-poc.md](adr/0001-coreimage-raw-poc.md) | ADR 0001: Core Image（CIRAWFilter）で Sony ARW を現像する Swift CLI の PoC と、その CLI 契約（stdin に JSON 1 個、stdout に JSON 1 行） |
| [adr/0002-coreimage-engine-decision.md](adr/0002-coreimage-engine-decision.md) | ADR 0002: 現像エンジンを rawpy から Core Image（`apple_default`・decoder 9・レンズ補正 ON）へ切り替える決定と、置き換えの条件 |
| [adr/0003-python-removal-single-app.md](adr/0003-python-removal-single-app.md) | ADR 0003: Python を全廃し、現像エンジンを組み込んだ単一の Tauri アプリにする決定 |
| [design/coreimage-feature-migration.md](design/coreimage-feature-migration.md) | rawpy 取りやめに伴う 4 機能（プリセット・PSO・ブラケット合成・星景）の Core Image への移行設計 |
| [design/coreimage-preview-adjustments.md](design/coreimage-preview-adjustments.md) | プレビューの Core Image 化と、Adjustments 9 キーの Core Image 対応（後段の Metal カーネルを含む）の実装設計 |
| [design/coreimage-highlight-diagnosis.md](design/coreimage-highlight-diagnosis.md) | Core Image の白飛びの原因を処理段ごとに切り分ける診断モードと比較マトリクスの設計（Issue #92） |
| [design/coreimage-preset-retune.md](design/coreimage-preset-retune.md) | 11 プリセットを Core Image 向けに再調整し、`highlight_recovery` を露出とシャドウ持ち上げへ写像する設計（Issue #81） |
| [design/single-app-implementation-plan.md](design/single-app-implementation-plan.md) | Python 無しの単一アプリへ段階的に移行する実装プラン（マイルストーン M0〜M4 と各 Issue の範囲） |
| [design/evaluation-before-python-removal.md](design/evaluation-before-python-removal.md) | Python 本体を削除する前に評価経路を独立させる実施順と検収条件 |
| [design/coreimage-candidate-native-roi-plan.md](design/coreimage-candidate-native-roi-plan.md) | #57 の次の実装計画。回帰用CI基準の再確保、最終候補の原寸ROI診断、素材追加後の較正と採否への接続 |

## 開発者向け

| 文書 | 内容 |
|---|---|
| [development/evaluation-paths.md](development/evaluation-paths.md) | `scripts/` の評価スクリプトの使い方（モード、前提、削除後の扱い）と、OS 更新時の回帰確認の手順 |
| [development/coreimage-regression-check.md](development/coreimage-regression-check.md) | macOS 更新で現像結果が変わっていないかを、ベースラインの実効パラメータと画質指標で確かめる手順 |
| [development/coreimage-parameter-mapping.md](development/coreimage-parameter-mapping.md) | Adjustments の各キーと Core Image（CIRAWFilter・後段 CIFilter）の対応表。「対応」「近似」「対応なし」の区別と、実装時の決定 |

## 計測・調査の記録

各時点の記録で、数値はその時点の実装と環境のものです。現在の挙動は [API_CONTRACT.md](API_CONTRACT.md) とコードを正としてください。

| 文書 | 内容 |
|---|---|
| [reports/coreimage-engine-comparison.md](reports/coreimage-engine-comparison.md) | rawpy と Core Image を同じ ARW で比較した記録（処理時間、明るさ、解像感、夜景・星空、逆光、Adjustments の較正）。Core Image は速く省メモリで、`apple_default` は解像感・明るさがカメラ JPEG に最も近く、neutral 設定は甘くなると整理した |
| [reports/coreimage-highlight-diagnosis-report.md](reports/coreimage-highlight-diagnosis-report.md) | Core Image の白飛びの原因を段階別に診断。白飛びは線形段で表示白 1.0 を超えた画素がトーン段で 1.0 に固定されて生まれ、露出 −0.7 とシャドウ持ち上げ 40 で戻せると判定（判定 A、Core Image 継続） |
| [reports/coreimage-preset-retune-report.md](reports/coreimage-preset-retune-report.md) | プリセットの再調整と `highlight_recovery` 写像の計測。完了条件 1〜10 のうち条件 6 以外は合格し、条件 6 は誤差レベルの不一致のみ。目視もオーナーが合格と判断 |
| [reports/coreimage-small-render-performance.md](reports/coreimage-small-render-performance.md) | PSO の代理画像（長辺 640px）の速度と画質。遅さの大半は SMB 上の ARW 読み込みで、1 回デコードして後段だけ繰り返す方式を推奨（1 枚 50 評価で約 73 秒が 2〜6 秒の見込み） |
| [reports/coreimage-pso-loop-report.md](reports/coreimage-pso-loop-report.md) | PSO 探索ループの Rust 移植と実測。探索空間、decode 共有、しきい値の見直し（sharpness 探索上限を 1.6 にし、他は据え置き） |
| [reports/coreimage-proxy-full-measurement.md](reports/coreimage-proxy-full-measurement.md) | 候補生成の 640px 代理と原寸の指標のずれの計測（11 枚・44 候補）。1280px 代理でも原寸との向きの不一致が残るため、最終候補を原寸 ROI で再評価する修正範囲に絞ると決めた |
| [reports/coreimage-merge-core-report.md](reports/coreimage-merge-core-report.md) | ブラケット合成の核（位置合わせ MTB と Mertens 合成）の Swift 実装報告。OpenCV 基準との PSNR 55 dB 以上を確認 |
| [reports/coreimage-merge-mode-report.md](reports/coreimage-merge-mode-report.md) | Swift CLI の合成モード（`mode: "merge"`、CLI 0.8.0）の実装報告。同一画像 9 枚の原寸 smoke は 57.6 秒、最大 RSS 3.28 GiB |
| [reports/coreimage-bracket-rust-report.md](reports/coreimage-bracket-rust-report.md) | Rust の `merge_bracket` コマンドと書き出しの実装・検証報告。実 CLI の結合テストと GUI 接続は確認したが、実ブラケットでの画質は未検証 |
| [reports/coreimage-bracket-ui-report.md](reports/coreimage-bracket-ui-report.md) | ブラケット合成を UI へ再接続した報告。操作の排他、古い合成結果の非表示、レンズ補正理由の表示を確認 |
| [reports/analysis-scoring-validation.md](reports/analysis-scoring-validation.md) | 解析の採点・推薦の Rust 移植と、Python の凍結基準との比較。推薦は 8 グループすべてで一致し、JPEG デコーダ差による数値の誤差は未達のまま固定 |
| [reports/python-removal-verification.md](reports/python-removal-verification.md) | Python 本体の削除の検証。削除後も Python・Rust・Swift・フロントの検証と、Tauri アプリのビルドが通ることを確認 |
| [reports/spikes-wave0.md](reports/spikes-wave0.md) | 初期開発（Wave 0）のスパイク結果。同梱の lensfun DB だけでは 10-18mm が足りないこと、Pillow だけで EXIF（GPS を含む）を継承できることを確認 |

## 初期の構想

| 文書 | 内容 |
|---|---|
| [history/GEOFRAME_RAW_ASSIST_HANDOFF.md](history/GEOFRAME_RAW_ASSIST_HANDOFF.md) | 2026-08 時点の初期構想の引継ぎ文書。現在の実装とは異なる点があります |
