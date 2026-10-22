# GeoFrame RAW Assist — UI prototype

旅行写真のJPEG/ARWペアを解析し、類似カットの代表選定とRAW現像候補の判断を支援するローカルUIプロトタイプです。グループ・スコア・推薦理由はすべて実写真の解析結果から生成されます。

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

フォルダは画面左のサイドバー「現像対象フォルダ」から変更できます。

- **デスクトップアプリ** — 「フォルダを選ぶ」でmacOSのフォルダ選択ダイアログが開きます。

初期値は同梱の `sample/100MSDCF` です。`~/Pictures` や `~/Desktop`、SDカードなどの外部ボリュームを選ぶと、macOSが初回にアクセス許可を求めます。許可しないと読み取れず、画面には「写真が0枚」ではなく**許可を促すメッセージ**が出ます。「システム設定 > プライバシーとセキュリティ」で許可してから選び直してください。

フォルダを切り替えると解析はやり直しになります（撮影IDはカメラの連番で、カードが違えば別の写真に同じIDが付くため、解析・現像・サムネイルのキャッシュはフォルダごとに分かれています）。元のフォルダに戻せば、そのフォルダの現像結果はそのまま再利用されます。

## 機能フロー

1. **解析** — 現像対象フォルダのJPEGをEXIF抽出・採点・サムネイル生成し、撮影グループへまとめます（既定の `sample/100MSDCF` の281枚で約7秒）。結果は `outputs/analysis/analysis.json` にキャッシュされ、2回目以降は即座に表示されます。
2. **選別** — グループごとに代表カットと推薦理由（シャープネス・白飛びの少なさ）、RAW現像推薦レベル（強く推奨／推奨／検討／JPEGで十分）が表示されます。代表は手動で変更できます。
3. **現像** — プリセットを選びスライダーで調整すると、プレビュー現像（約1秒・長辺1600px）で結果を確認できます。確定すると本現像（原寸・実測3〜5秒、ノイズ低減 `full` ではさらにかかります）を実行します。
4. **ブラケット合成** — 露出を変えて撮った2〜9枚を1枚に合成します（Exposure Fusion）。白飛びと黒つぶれを同時に抑えられます。合成パネルで使うフレームを選んでください（合成プレビューは数秒、本合成は数十秒）。
5. **書き出し** — 選んだ写真を `outputs/export/<バッチ名>/` へJPEGと `manifest.json` として複製します。
6. **Apple「写真」へ登録** — 書き出したバッチをアルバム `GeoFrame RAW Assist <バッチ名>` へ登録します。

元のJPG/ARWは、いずれの操作でも変更しません。

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

各プリセットは露出・色温度・シャドウ持ち上げ・ハイライト回復・明るさ・コントラスト・彩度・シャープネス・ノイズ低減の9項目の初期値です。写真を開いたときの初期プリセットは、解析の推薦理由に合わせて選ばれます（黒つぶれ→黒つぶれ救済、白飛び→白飛び救済、両方→逆光、高ISO・夜景→夜景ノイズ低減）。画面上のスライダーで上書きでき、調整値ごとに別ファイルとして保存されます。

画面を使わず1枚だけ現像する場合:

```bash
.venv/bin/python -m local_raw.engine --capture-id DSC02943 --preset nightscape
```

### 現像候補自動生成（代理評価）

現像パネルの「自動候補を作る」は、**ARW を縮小デコード（rawpy `half_size`）した画像**に、
本番現像（`local_raw/engine.py`）と同じ後段関数（暗部補正・縮小・トーン）を当てて、
白飛び・黒つぶれ・ノイズ・細部・色・階調・基準との差を多目的に評価します。
カメラJPGの独自近似ではないため代理と本番のずれは小さいですが、レンズ補正は省略しており、
**美的な正解や最終RAW品質を保証するものではありません**。ペアの ARW が必須です（1枚あたり
数秒〜15秒程度かかります）。

- ハイライト回復は連続値ではなく3段階（Clip / Blend / Reconstruct。境界は engine と同じ0/75）で、
  各段階の代表値（0 / 50 / 85、プリセットが同じ段階ならプリセット値）を提案します。
- 露出は 0.5EV 刻みです。ホワイトバランス（wb_temp）は探索せずプリセット値のままです。
- 「自然」はプリセット値そのもの（他候補の比較基準）です。ほかに星空（EXIF が長秒・低EV100で、点光源が多数ある写真のみ）・前景・
  低ノイズ・SNS向けから、表示された指標と理由を見て**人が選び**、必要ならスライダーを調整して
  から「プレビュー現像」または「本現像」を実行してください。
- 基準より白飛び・黒つぶれが大きく悪化する候補、基準よりノイズが悪化する低ノイズ候補、暗部が
  明るくならない前景候補は返さず、API 応答の `rejected` に理由を残します。

候補を選択しただけで元のJPG/ARWが変更されたり、現像が勝手に始まったりすることはありません。

#### 候補生成の評価（チューニング用）

`scripts/evaluate_candidates.py` は、各候補の調整値で**実RAW現像**（full デコード → レンズ補正 → engine 後段）を行い、
候補生成と同じ指標で代理 vs 実RAW のずれ・壊れた候補・スタイルの狙い達成などを集計します
（`scripts/evaluate_raw.py` がプリセットのガードレール評価なのに対し、こちらは候補生成のチューニング前後比較用）。
現像元フォルダ・アプリ設定・`outputs/` の通常成果物には書き込みません。出力は `outputs/eval_candidates/<日時>/`
（`summary.json` / `summary.md` / `cases/` / `frames/`、`--contact-sheets` で `contact_sheets/`）。1ケース約40秒です。
`--out` に既存の結果があると古い結果が混ざらないよう停止します（`--force` でその出力先の結果だけ消して再実行）。
`--aggregate-only --out <dir>` は既存の `cases/` から集計だけやり直し、元の実行時情報とエラー件数を引き継ぎます。

```bash
# 基準ケース集（scripts/eval_cases/candidates_baseline.json）。root キーの実フォルダは --root で渡す
.venv/bin/python scripts/evaluate_candidates.py \
  --cases-file scripts/eval_cases/candidates_baseline.json \
  --root image_work=/Volumes/KIOXIA/image_work --root stars=<星空ARW/JPGのフォルダ> --contact-sheets

# 単発ケース
.venv/bin/python scripts/evaluate_candidates.py --source-root <フォルダ> --case DSC02849:neutral_recovery
```

将来はHDR、連写の選好学習、パノラマ、参照プリセットへの対応を予定しています。

## 生成物の構成

```
outputs/
├── analysis/analysis.json          解析結果（グループ・スコア・推薦）のキャッシュ
├── thumbnails/<folder>-<ID>.webp   一覧表示用サムネイル（長辺640）
├── preview/<ID>-<preset>-<hash>.jpg  プレビュー現像（長辺1600）
├── developed/<ID>-<preset>-<hash>.jpg  本現像（原寸）
├── merged/<先頭ID>-merged<枚数>-<preset>-<hash>.jpg  ブラケット合成（原寸・書き出し可）
├── merged_preview/ 同名                ブラケット合成のプレビュー（長辺1600・書き出し不可）
│   └── 同名 .json                  レシピ（元ARWのSHA-256・調整値・レンズ補正・EXIF）
└── export/<バッチ名>/
    ├── <現像ファイル名>.jpg
    └── manifest.json               書き出し内容とレシピの記録
```

アプリの設定（現像対象フォルダ・Google DriveのOAuthトークン）は `outputs/` ではなく、OS標準のアプリデータディレクトリ（macOSでは `~/Library/Application Support/com.geoframe.rawassist/`）に保存されます。

`<hash>` は調整値・プリセット・出力サイズ・エンジンバージョン・**現像元フォルダ**から決まる10桁の識別子です。同じ条件で再実行するとキャッシュが再利用されます。`<folder>` は現像元フォルダを識別する8桁で、別フォルダの同じ撮影IDが混ざらないようにするためのものです。

## ブラケット合成

露出を変えて連続撮影したカット（AEB）は、解析で「ブラケット」として検出されます。操作パネルの「ブラケット合成」で使うフレームを選び、合成できます。

- **方式は Exposure Fusion（Mertens）** です。露出時間の情報を使わず、各フレームの「よく撮れている部分」を重み付けして直接1枚にします。トーンマッピング特有の不自然さが出ません。
- 合成用のデコードは**自動明るさ補正を切って**います。フレームごとに明るさを正規化すると露出差が消え、合成の意味が無くなるためです。プリセットのトーン調整は合成後にかかります。
- 手持ちのズレは合成前に位置合わせします（結果に「位置合わせ済み」と表示）。
- **枚数は2〜9枚**。1つのグループに別シーンのブラケットが複数セット混ざることがあるため、フレーム選択は手動です（露出補正と平均輝度が階段状に並ぶひと続きを選んでください）。
- 実測（原寸・5枚で12〜20秒、9枚で約38秒／ピークメモリ 6.1GB）。書き出しに使えるのは本合成の結果だけです。

## Apple フォトへの登録

初回だけ、macOSの自動化許可ダイアログ（ターミナルが「写真」を操作しようとしています）が表示されます。「OK」を選ぶと登録が進みます。

許可しなかった場合は「システム設定 > プライバシーとセキュリティ > オートメーション」を開き、実行元のアプリ（ターミナルなど）の下にある「写真」をオンにしてから、もう一度実行してください。許可するまでは `{"status": "error"}` が返り、写真は登録されません。

## サンプルデータ

`sample/100MSDCF` には同名のJPG/ARWが281組あります。これが現像対象フォルダの初期値で、表示用のサムネイルは `outputs/thumbnails/` に生成されます。別のフォルダへの変更方法は「現像する画像の置き場所」を参照してください。

## RAW現像エンジン

- rawpy 0.27.0 / LibRaw（MIT。LibRaw本体はLGPL-2.1またはCDDL-1.0のデュアルライセンス）
- Pillow 12.3.0（MIT-CMU）
- NumPy 2.5.1（BSD-3-Clauseほか）
- lensfunpy（レンズ補正。同梱DBに無いレンズは補正なしで現像します）
- OpenCV（シャープネス計測）
- 専用環境の使用量は約67MB

最終品質でカタログ管理まで必要になった場合は、同じAPIの背後をdarktable CLIへ差し替えられる構成です。現在の実装は依存が小さく、Sony ARWをローカルだけで試すことを優先しています。

### Core Image プレビュー（既定、最新の macOS）

デスクトップアプリ（Tauri）のプレビュー現像は、Python エンジンを経由せず、macOS 標準の Core Image（CIRAWFilter）で行います（Issue #65、#72、#73）。調整（Adjustments）の 9 キーも Core Image に対応づけてあります（`highlight_recovery` だけは当面効きません。代替は #81）。

- 既定が Core Image です。プレビューには「Core Image プレビュー」のバッジと注記が出ます。CLI の起動失敗などインフラ側のエラー時は Python（rawpy）のプレビューにフォールバックします。ブラウザモード（旧 Python ブラウザ開発モード）は Python のままです
- 切り戻し: 環境変数 `GEOFRAME_PREVIEW_ENGINE=rawpy` で起動すると、プレビューも Python（rawpy）になります（`GEOFRAME_PREVIEW_ENGINE=rawpy npm run tauri:dev`、ビルド済み `.app` は `open --env GEOFRAME_PREVIEW_ENGINE=rawpy "<アプリのパス>.app"`）
- **書き出しと原寸（本現像）は当面 rawpy のままです。** プレビューと書き出しで、明るさ・色・ノイズなどの見た目が一致しない場合があります（移行完了までの既知の差。オーナー受容済み）

- ビルド: `npm run build:coreimage`（Swift CLI `native/coreimage-cli` をビルドし `src-tauri/binaries/` に置く。`tauri:dev` / `tauri:build` からも自動で実行。`cargo build` / `cargo test` の前には一度必要）
- 出力は `<app_data_dir>/coreimage/` だけに書き、原本や既存のキャッシュ・書き出し先には書きません
- 比較: `.venv/bin/python scripts/compare_engines.py --source-root <フォルダ> --select auto`（出力は `work/engine-comparison/`）
- macOS を更新したら [回帰確認手順](docs/coreimage-regression-check.md) で基本設定（`apple_default`・decoder 9・レンズ補正 ON）の画が変わっていないかを確かめます（`scripts/coreimage-regression-applied.sh <ARW のフォルダ>`）

採否と比較結果は [ADR 0002](docs/adr/0002-coreimage-engine-decision.md)、[比較レポート](docs/coreimage-engine-comparison.md)、CLI の設計は [ADR 0001](docs/adr/0001-coreimage-raw-poc.md) を参照してください。現像エンジンは Core Image（`apple_default`・decoder 9・レンズ補正 ON）へ段階的に移行する方針で、プレビューは段階 1 として Core Image に移行済みで、原寸・書き出しの移行条件は [ADR 0002](docs/adr/0002-coreimage-engine-decision.md) を参照してください。

## 既知の制限

- 現像は同期処理のため、本現像は完了まで画面で数秒待ちます（キャンセル不可）。
- DB保存・非同期ジョブキューは未接続です。解析結果はJSONファイルのキャッシュのみです。
- 同一バッチ名で再度書き出すと、以前の現像JPEGがフォルダに残ったままになります。
- 露出ブラケット撮影の検出はサンプルに該当カットが無く、実データで未検証です。
- 対応する命名は `DSC#####.JPG` / `.ARW` だけです。他機種の命名やRAW単独のフォルダ、他社のRAW形式（CR2/NEF等）は解析対象になりません。
- 解析キャッシュは1フォルダ分のみ保持します。フォルダを行き来すると、そのたびに解析をやり直します（現像結果とサムネイルのキャッシュはフォルダごとに残ります）。
- ビルドした `.app` は、フォルダ選択とは別に、Pythonエンジン（`local_raw/`）と `.venv` をビルドしたマシンのリポジトリパスから読み込みます。配布先マシンでそのまま動く状態にはなっていません。

詳細な実装状況は `docs/GEOFRAME_RAW_ASSIST_HANDOFF.md` の「MVP 実装状況」を参照してください。

## テスト

```bash
npm test           # フロント単体テスト（vitest。reducer など純粋関数）
npm run build      # ビルド検証（vite build）
 .venv/bin/python -m unittest discover -s tests -p 'test_*.py'   # Python側（解析・現像・書き出し・フォルダ設定・レンズ補正・ブラケット合成）294件
npm run lint
```
