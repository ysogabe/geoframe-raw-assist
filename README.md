# GeoFrame RAW Assist — UI prototype

旅行写真のJPEG/ARWペアを解析し、類似カットの代表選定とRAW現像候補の判断を支援するローカルのデスクトップアプリ（Tauri）です。グループ・スコア・推薦理由はすべて実写真の解析結果から生成されます。

現像・解析・書き出しはすべてアプリ本体（Rust + Swift の Core Image CLI）で行います。Python のサーバーやフォールバック経路はありません（単一アプリ化。[ADR 0003](docs/adr/0003-python-removal-single-app.md)）。

## 起動

```bash
npm install
```

デスクトップアプリとして起動・ビルドします。Python のセットアップやサーバー起動は不要です。

```bash
env -u CI npm run tauri:dev
env -u CI npm run tauri:build
```

開発時は Core Image CLI の同梱バイナリを先にビルドしてください: `npm run build:coreimage`。

DMG の作成（`bundle_dmg.sh`）がまれに失敗することがあります（2026-10-20 時点で 3 回連続成功し再現せず。Issue #76）。失敗したときは `src-tauri/target/release/bundle/macos/rw.*.dmg`（作りかけのイメージ）を消してから再実行してください。`.app` だけ必要なら `env -u CI npm run tauri:build -- --bundles app` で DMG を作らずにビルドできます。

## 現像する画像の置き場所

対象は**JPEGとRAWの同名ペア**です。ソニーα の既定命名（`DSC02943.JPG` と `DSC02943.ARW`）のみ対応しています。1つのフォルダに直接置いてください（サブフォルダは見ません）。

```
<選んだフォルダ>/
├── DSC02943.JPG
├── DSC02943.ARW
├── DSC02944.JPG
└── DSC02944.ARW
```

フォルダは画面左のサイドバー「現像対象フォルダ」の「フォルダを選ぶ」で選びます（macOSのフォルダ選択ダイアログが開きます）。選んだフォルダはアプリデータの `config.json` に保存され、次回の起動でも使われます。

**最初にフォルダを選んでください。** アプリにサンプル写真は同梱していません。未選択のうちは解析・現像・書き出し・候補生成が `SOURCE_ROOT_NOT_CONFIGURED`（現像元フォルダが未設定）で止まります（画面の表示は開発用にビルドしたリポジトリ内の `sample/100MSDCF` を指しますが、配布物には含まれません）。

`~/Pictures` や `~/Desktop`、SDカードなどの外部ボリュームを選ぶと、macOSが初回にアクセス許可を求めます。許可しないと読み取れず、画面には「写真が0枚」ではなく**許可を促すメッセージ**が出ます。「システム設定 > プライバシーとセキュリティ」で許可してから選び直してください。

初めて選んだフォルダは解析から始まります（撮影IDはカメラの連番で、カードが違えば別の写真に同じIDが付くため、解析・現像・サムネイルのキャッシュはフォルダごとに分かれています）。元のフォルダに戻せば、そのフォルダの解析と現像結果はそのまま再利用されます。

## 機能フロー

1. **解析** — 現像対象フォルダのJPEGをEXIF抽出・採点・サムネイル生成し、撮影グループへまとめます。結果はフォルダごとにアプリデータの `analysis/<folder>.json` にキャッシュされ、2回目以降は即座に表示されます。
2. **選別** — グループごとに代表カットと推薦理由（シャープネス・白飛びの少なさ）、RAW現像推薦レベル（強く推奨／推奨／検討／JPEGで十分）が表示されます。代表は手動で変更できます。
3. **現像** — プリセットを選びスライダーで調整すると、プレビュー現像（長辺2400px）で結果を確認できます。確定すると本現像（原寸。カメラJPEGのEXIFを引き継ぎます）を実行します。
4. **ブラケット合成** — 露出を変えて撮った2〜9枚を1枚に合成します（Exposure Fusion）。白飛びと黒つぶれを同時に抑えられます。合成パネルで使うフレームを選んでください。
5. **書き出し** — 選んだ写真（カメラJPEG／本現像／本合成）をアプリデータの `export/<バッチ名>/` へJPEGと `manifest.json` として複製します。
6. **Apple「写真」へ登録** — 書き出したバッチをアルバム `GeoFrame RAW Assist <バッチ名>` へ登録します。

元のJPG/ARWは、いずれの操作でも変更しません。

処理時間・メモリの目安（macOS 27.0.1 / M3、ISO 6400 の 1 カット、Core Image CLI 単体）: プレビュー約1.0秒・RSS 約170MB、原寸約2.0秒・RSS 約270MB（[回帰確認手順](docs/coreimage-regression-check.md) §1）。

## 現像プリセット

| name | 表示名 | 用途 |
|---|---|---|
| `neutral_recovery` | 階調回復 | 自然な明暗差を保つ標準仕上げ |
| `landscape_clear` | 風景クリア | 遠景の輪郭と色をくっきりさせる |
| `nightscape` | 夜景ノイズ低減 | 高ISO夜景のノイズを抑え暗部を持ち上げる |
| `sunset_glow` | 夕景・マジックアワー | 夕焼けの暖色を強め空の階調を守る |
| `indoor_warm` | 室内・料理 | 室内照明の暖かさを残し料理を明るく見せる |
| `portrait_soft` | 人物・柔らか | 肌を滑らかに見せる柔らかいコントラスト |
| `monochrome_street` | モノクロ | 彩度を落としコントラストで見せる白黒仕上げ |
| `highlight_rescue` | 白飛び救済 | 飛びかけた空や光源の階調を最大限引き戻す |
| `shadow_rescue` | 黒つぶれ救済 | つぶれた暗部を大きく持ち上げ、増えるノイズを抑える |
| `backlit_rescue` | 逆光 | 明るい背景の階調を残しつつ、影になった被写体を持ち上げる |
| `vivid_travel` | ビビッド | 旅の記録向けに色を鮮やかに強調する |

各プリセットは露出・色温度・シャドウ持ち上げ・ハイライト回復・明るさ・コントラスト・彩度・シャープネス・ノイズ低減の9項目の初期値です（値の正は `src-tauri/src/presets.rs`。Core Image の `apple_default` を基準に再調整済み。[再調整レポート](docs/coreimage-preset-retune-report.md)）。写真を開いたときの初期プリセットは、解析の推薦理由に合わせて選ばれます（黒つぶれ→黒つぶれ救済、白飛び→白飛び救済、両方→逆光、高ISO・夜景→夜景ノイズ低減）。画面上のスライダーで上書きでき、調整値ごとに別ファイルとして保存されます。

### 現像候補自動生成（代理評価）

現像パネルの「自動候補を作る」は、**ARW を Core Image で縮小デコード（長辺640px、`apple_default`・decoder 9・レンズ補正 ON）した代理画像**に、
プレビュー・本現像と同じ調整の写像を当て、Rust の PSO（粒子群最適化）で調整値を探索します（`src-tauri/src/candidates/`。Issue #87）。
白飛び・黒つぶれ・ノイズ・細部・色・階調・基準との差を多目的に評価しますが、代理画像での評価なので
**美的な正解や最終RAW品質を保証するものではありません**。ペアの ARW が必須です。

- 露出はプリセット値と ±0.5EV の3格子です。ハイライト回復（`highlight_recovery`）とホワイトバランス（`wb_temp`）は探索せずプリセット値のままです。
- 「自然」はプリセット値そのもの（他候補の比較基準）です。ほかに星空（EXIF が長秒・低EV100で、点光源が多数ある写真のみ）・前景・
  低ノイズ・SNS向けから、表示された指標と理由を見て**人が選び**、必要ならスライダーを調整して
  から「プレビュー現像」または「本現像」を実行してください。
- 基準より白飛び・黒つぶれが大きく悪化する候補などは返さず、応答の `rejected` に理由を残します。

探索空間・安全判定・実測は [PSO 移植の報告](docs/coreimage-pso-loop-report.md) を参照してください。代理（640px）と本現像（原寸）の見え方の比較は Issue #57 で計測中です（結果は `docs/coreimage-proxy-full-measurement.md` にまとめる予定）。

候補を選択しただけで元のJPG/ARWが変更されたり、現像が勝手に始まったりすることはありません。

将来はHDR、連写の選好学習、パノラマ、参照プリセットへの対応を予定しています。

## 生成物の構成

生成物はリポジトリ内ではなく、OS標準のアプリデータディレクトリ（macOSでは `~/Library/Application Support/com.geoframe.rawassist/`、以下 `$APPDATA`）に保存されます。

```
$APPDATA/
├── config.json                          アプリ設定（現像対象フォルダ）
├── analysis/<folder>.json               解析結果（グループ・スコア・推薦）のキャッシュ
├── thumbnails/<folder>-<ID>.jpg         一覧表示用サムネイル（長辺640）
├── coreimage/
│   ├── preview/<ID>_<hash>.jpg|.json    プレビュー現像（長辺2400）と来歴
│   ├── full/<ID>_<hash>.jpg|.json       本現像（原寸）と来歴
│   ├── merged/<先頭ID>-merged<枚数>-<preset>-<hash>.jpg|.json  ブラケット合成（原寸・書き出し可）
│   ├── merged_preview/ 同名              ブラケット合成のプレビュー（長辺2400・書き出し不可）
│   └── candidates/                      候補生成の一時ファイル（終了時に削除）
└── export/<バッチ名>/
    ├── <現像ファイル名>.jpg
    └── manifest.json                    書き出し内容とレシピの記録
```

`<hash>` は調整値・プリセット・CLI の版・元ARWなどから決まる識別子です。同じ条件で再実行するとキャッシュが再利用されます。`<folder>` は現像元フォルダを識別する8桁で、別フォルダの同じ撮影IDが混ざらないようにするためのものです。命名・キャッシュキーの詳細は [API 契約](docs/API_CONTRACT.md) を参照してください。

旧版（Python 時代）の `outputs/` やアプリデータ内の `logs/`・`drive_auth.json` が残っていることがありますが、現行のアプリは読み書きしません。

## ブラケット合成

露出を変えて連続撮影したカット（AEB）は、解析で「ブラケット」として検出されます。操作パネルの「ブラケット合成」で使うフレームを選び、合成できます。

- **方式は Exposure Fusion（Mertens）** です。露出時間の情報を使わず、各フレームの「よく撮れている部分」を重み付けして直接1枚にします。トーンマッピング特有の不自然さが出ません。合成は Core Image CLI の1リクエストで行います（位置合わせ MTB + Mertens。Issue #83 / #84）。
- 合成用のデコードはフレームごとの明るさ正規化をしません。正規化すると露出差が消え、合成の意味が無くなるためです。プリセットのトーン調整は合成後にかかります。
- 手持ちのズレは合成前に位置合わせします（結果に「位置合わせ済み」と表示）。
- **枚数は2〜9枚**。1つのグループに別シーンのブラケットが複数セット混ざることがあるため、フレーム選択は手動です（露出補正と平均輝度が階段状に並ぶひと続きを選んでください）。
- 書き出しに使えるのは本合成の結果だけです。

## Apple フォトへの登録

初回だけ、macOSの自動化許可ダイアログ（実行元のアプリが「写真」を操作しようとしています）が表示されます。「OK」を選ぶと登録が進みます。

許可しなかった場合は「システム設定 > プライバシーとセキュリティ > オートメーション」を開き、実行元のアプリ（GeoFrame RAW Assist、`tauri:dev` ならターミナルなど）の下にある「写真」をオンにしてから、もう一度実行してください。許可するまでは写真は登録されず、画面にエラーが表示されます。

## RAW現像エンジン

macOS 標準の Core Image（`CIRAWFilter`）を、Swift 製の CLI `geoframe-ci-develop`（`native/coreimage-cli`）経由で使います。Tauri（Rust）が CLI を子プロセスとして呼び、プレビュー・本現像・ブラケット合成・候補生成の代理デコードをすべてこの経路で行います。

- 基本設定は `apple_default` profile・decoder 9・レンズ補正 ON（採否は [ADR 0002](docs/adr/0002-coreimage-engine-decision.md)、比較は [比較レポート](docs/coreimage-engine-comparison.md)、CLI の設計は [ADR 0001](docs/adr/0001-coreimage-raw-poc.md)）。
- 調整（Adjustments）の 9 キーは Core Image の RAW デコードと後段の CIFilter に写像しています（[パラメータ対応表](docs/coreimage-parameter-mapping.md)）。`highlight_recovery` は露出とシャドウ持ち上げへの写像です（Issue #81）。
- 書き出す本現像・合成の JPEG には、ペアのカメラ JPEG の EXIF を引き継ぎます。
- 外部ライブラリ（rawpy / LibRaw、lensfunpy など）には依存しません。対応 OS は最新の macOS です。
- ビルド: `npm run build:coreimage`（Swift CLI をビルドし `src-tauri/binaries/` に置く。`tauri:dev` / `tauri:build` からも自動で実行。`cargo build` / `cargo test` の前には一度必要）。
- macOS を更新したら [回帰確認手順](docs/coreimage-regression-check.md) で基本設定の画が変わっていないかを確かめます（`scripts/coreimage-regression-applied.sh <ARW のフォルダ>`）。

## 評価スクリプト（開発者向け・Python）

`scripts/` には、エンジン比較・候補生成のチューニング・回帰確認のための評価スクリプトがあります。**アプリの実行には使いません。** 評価用の Python 環境に入れるのは NumPy / Pillow / OpenCV（`requirements-dev.txt`）だけで、rawpy・lensfunpy は不要です。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

- `scripts/compare_engines.py` — カメラ JPEG と Core Image の出力を同じ ARW で比べる（既定は Core Image のみ）。旧エンジン（rawpy）の列は、凍結済みの基準画像と `--frozen-manifest` で比べます。
- `scripts/evaluate_candidates.py` — 候補生成の評価・集計。
- 旧エンジン（rawpy / `local_raw/`）で基準画像や候補を作り直す経路は**アーカイブ扱い**です。Python 本体（`local_raw/`）を削除する前のコミットを checkout し、その時点の `requirements-raw.txt` の環境で `--legacy-rawpy` を明示して実行します。

経路と使い分けは `docs/evaluation-paths.md`（整備中）と各スクリプトの `--help` を参照してください。過去の実験の記録（比較レポートなどの表と数値）はそのまま残しています。

## 既知の制限

- 本現像・合成は完了まで画面で待ちます（キャンセル不可）。
- DB保存・非同期ジョブキューは未接続です。解析結果はJSONファイルのキャッシュのみです。
- 同一バッチ名で再度書き出すと、以前の現像JPEGがフォルダに残ったままになります。
- 露出ブラケット合成は、実際のAEB・三脚長秒ブラケットでの画質を未検証です（[ブラケット Rust 移植の報告](docs/coreimage-bracket-rust-report.md)）。
- 対応する命名は `DSC#####.JPG` / `.ARW` だけです。他機種の命名やRAW単独のフォルダ、他社のRAW形式（CR2/NEF等）は解析対象になりません。
- `apple_default` の内部パラメータは非公開で、macOS の更新で画が変わりうります（回帰確認手順で確認）。

詳細な実装状況は `docs/GEOFRAME_RAW_ASSIST_HANDOFF.md` の「MVP 実装状況」を参照してください。

## テスト

```bash
npm test                 # フロント単体テスト（vitest。reducer など純粋関数）
npx tsc --noEmit         # 型検査
npm run build            # ビルド検証（vite build）
npm run lint
npm run build:coreimage && cargo test --manifest-path src-tauri/Cargo.toml   # Rust（解析・現像・合成・書き出し・候補生成）
swift test --package-path native/coreimage-cli                             # Swift（Core Image CLI）
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'              # 評価スクリプトのテスト（requirements-dev.txt）
```
