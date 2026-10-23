# Python 無しの単一アプリ — 段階的実装プラン

- ステータス: 案（2026-10-22。[ADR 0003](../adr/0003-python-removal-single-app.md) §6 のオーナー判断を前提にする）
- 関連: Issue #105、ADR 0003、[4 機能の移行設計](coreimage-feature-migration.md)、`native/coreimage-cli/README.md`
- 表記: 【確認】はコードを読んで確かめた事実（ファイルと行を併記）。【推測】は見立て。【要確認】は Issue 内の最初のタスクで確かめること。
- 新規 Issue の作成はメインが行う。ここでは **タイトルと範囲** だけを書く。番号は仮（`N-xx`）。

## 0. 要点

1. **最初の目標は M0「Python 無しで起動 → フォルダ選択 → 一覧 → プレビュー → 本現像 → JPEG 書き出し（EXIF 継承・GPS 保持）」が `.app` で成立すること**。現行の全機能を初版に入れない。
2. 画面は解析結果（`Analysis`）が無いと先へ進めない【確認: `src/App.tsx:825-835`。`analysisReady && analysis` が偽なら常に `AnalysisGate`】。
   したがって **「解析は後回しにして M0 では隠す」は成立しない**。M0 には `Analysis` の形を返す Rust コマンドが必ず要る。M0 では採点（ラプラシアン分散・白飛び比）を省いた **解析 lite** にし、採点の数値一致は M1 に回す。
3. 本現像は今も Python（`developRaw` → `POST /api/raw-develop`）が正【確認: `src/App.tsx:577`】。`DevelopMode::Full` は Rust に定義済み・未使用【確認: `src-tauri/src/coreimage.rs:101-105`】。これを使う「原寸現像 + EXIF 継承」が M0 のクリティカルパス（ADR 0003 新規 A）。
4. **基準値の凍結（Python が動くうちに作るフィクスチャ）は M0 の先頭 Issue** に置き、Python 起動を外す Issue（G）の前に完了させる。
5. ブラウザモード（vite dev + Python サーバ）は捨てる（オーナー決定 3）。検証は `tauri dev`（WebView）と `.app` で行う（§6）。

## 1. 現行アプリの画面と機能の棚卸し

`src/App.tsx`・`src/components/`・`src/lib/` から洗い出した。「経路」は現在データを取っている先。

| 画面 / 機能 | 実装 | 経路【確認】 | M0 での扱い |
|---|---|---|---|
| 初回セットアップ表示 | `BootstrapGate.tsx`、`lib/setup.ts`、`main.rs` の `SetupState` | `invoke("setup_status")` + `raw-setup` event（`main.rs:92-860` の venv/pip） | **削除**（G） |
| エンジン死活・バッジ | `App.tsx:114-150` の `/api/health` 再試行、`Topbar.tsx:19-21`（`rawpy x.y 接続済み`） | HTTP `/api/health`（`raw-api.ts:108`） | **置換**: Rust `engine_info` コマンド（CLI `--version` は `coreimage.rs:958 cli_version` で取得済み） |
| 現像元フォルダ（サイドバー） | `SourceFolderPanel.tsx`、`Sidebar.tsx` | GET/POST `/api/source-folder`（`raw-api.ts:217-250`）。読み側だけ Rust にある（`coreimage.rs:155 read_source_root`） | **Rust 化**（E）。ネイティブダイアログ `plugin-dialog` は既に Tauri（`capabilities/default.json`） |
| 解析開始・進捗・一覧 | `AnalysisGate.tsx`、`GroupList.tsx`、`SummaryStrip.tsx`、`GroupStrip.tsx` | `/api/analyze`・`/api/analysis` 1 秒ポーリング（`App.tsx:207-229`）。サムネは `/thumbs/*`（`raw-api.ts:30`） | **Rust 化（lite）**（C-lite）。ポーリングの形は残し transport を invoke に替える |
| 操作パネル: 選択写真・フィルムストリップ・原寸ビューア | `OperationPanel.tsx`、`ProgressiveImage.tsx`、`FullSizeViewer.tsx` | 元 JPG は `/source-jpg/<id>.JPG`（`raw-api.ts:35`、`OperationPanel.tsx:214,245,371`、`BeforeAfter.tsx:113`） | **配信を Tauri に置換**（C-lite の一部。§3.3） |
| プリセット・調整スライダー | `DevelopControls.tsx`、`presets.rs`、`adjustments.rs` | `invoke("get_presets")`【移行済み #80/#98】 | そのまま |
| プレビュー現像 | `coreimage-api.ts`、`coreimage.rs develop_preview_coreimage` | invoke【移行済み #72/#73】。失敗時 Python へフォールバック（`coreimage-api.ts:144-167`、`App.tsx:559-574`） | **フォールバックを削除**し、失敗はエラー表示に落とす |
| 本現像（原寸） | `App.tsx:577 developRaw` | HTTP `/api/raw-develop`（rawpy） | **Rust + CLI 化**（A） |
| 現像前後の比較 | `BeforeAfter.tsx` | `output_url`（プレビューは asset プロトコル、本現像は HTTP） | A で本現像も asset プロトコルへ |
| ブラケット合成 | `BracketMerge.tsx`、`App.tsx:627 runMerge` | HTTP `/api/merge-bracket` | **隠す**（M2 #83/#84 で戻す） |
| 現像候補（PSO） | `DevelopmentCandidates.tsx`、`App.tsx:593` | HTTP `/api/development-candidates` | **隠す**（M3 #86/#87 で戻す） |
| 採用・書き出し画面 | `ExportPanel.tsx`、`App.tsx:670 runExport` | HTTP `/api/export`（`export.py`。出力先はリポジトリ相対 `outputs/export/`【確認: `local_raw/export.py:27`】） | **Rust 化（lite）**（D-lite）。出力先を変える（§3.4） |
| 写真アプリへ取り込み | `ExportPanel.tsx:239`、`App.tsx:717 runImport` | HTTP `/api/import-to-photos`（osascript） | **隠す**（M1 で Rust 化。オーナー決定 2「残す」） |
| Google Drive アップロード | `ExportPanel.tsx:242-271`、`App.tsx:231-251, 731-763`、`app-state.ts` の `driveUpload`、`raw-api.ts:258-268` | HTTP `/api/drive-upload` | **初版から外す**（オーナー決定 1）。UI・state・API 関数を G で削除。Python 本体は H で削除。将来対応の Issue を別に立てる |
| サイドバーの「ライブラリ」「処理履歴」「設定」 | `Sidebar.tsx:39-41, 61` | ハンドラ無し（飾り）【確認】 | そのまま（M0 の対象外） |
| ブラウザ分岐 | `isTauri()`（`raw-api.ts:121`、`coreimage-api.ts:54`、`SourceFolderPanel.tsx:107`、`confirm.ts:15`） | — | 決定 3 で死にコード化 → G で整理 |

テストへの影響【確認】: `src/lib/app-state.test.ts`（745 行）と `raw-api.test.ts` に health / drive の参照が 26 箇所ある。
各 Issue の完了条件に `npm test` を入れ、G で削る reducer アクション（`health/ok` `health/fail` `drive/*`）のテストも同じ PR で消す。

## 2. マイルストーン一覧

```
M0  Python 無しで動く最小の単一アプリ          ← 最優先
    N-00 基準値の凍結（Python が動くうちに）     ← G より前に完了
    N-A  原寸現像 + EXIF 継承（CLI + Rust）      ┐
    N-E  現像元フォルダ設定の Rust 化            ├ worktree を分けて並列
    N-C1 解析 lite + サムネ/元 JPG の配信        ┘
    N-D1 書き出し（コピー + manifest）           ← N-A の出力先が決まってから
    N-G1 フロント: HTTP 全廃・機能の隠し・Drive 削除
    N-G2 main.rs の Python セットアップ削除・npm scripts 整理   ← M0 完了
M1  提案の質を戻す
    N-C2 解析の採点・推薦を Python と数値一致させる
    N-D2 写真アプリへの取り込み（osascript）
M2  ブラケット合成                 #83 → #84                      ┐ M2 と M3 は並列
M3  現像候補（PSO）と暗所          #86 ‖ N-B → #87、#88、#57。#85 は閉じる ┘
M4  後片付け
    #57 のCI proxy/原寸差を現11枚で再現 → 修正範囲を決定
    評価経路を独立化し local_raw/rawpy 無しの保存画像・JSON比較を検収
    #90 + N-H  Python 本体削除（local_raw/、旧アプリのテスト、requirements-raw.txt）
               評価スクリプトと独立したテスト・requirements-dev.txtは維持
    N-DOC  API_CONTRACT を Tauri コマンド契約に書き直す、CLAUDE.md の検証手順
    N-F    （将来）Google Drive アップロードを Rust で実装する
```

2026-10-23追補: M4の前提と検収順は
[評価経路を先に独立させる計画](evaluation-before-python-removal.md)を参照。
`compare_engines.py`の遅延importも含む実行依存を解消してから削除する。
`image_work`の昼夜混在13ケースは素材待ちであり、現11枚の評価には含めない。

### 2.1 新規 Issue の一覧（タイトルと範囲。作成はメインが行う）

| 仮番号 | タイトル | 範囲（2〜4 行） | M |
|---|---|---|---|
| N-00 | Python 基準値の凍結（解析 / EXIF / 比較画像 / PSO / 合成 / manifest） | Python が動くうちに §3.8 の 7 件を生成し `tests/fixtures/baseline/` と README に置く。生成コマンドを README に残す。実機写真はコミットせず外部パスを明記 | M0 |
| N-A | 原寸現像・書き出し JPEG を Core Image 化し、ペア JPG の EXIF（GPS 含む）を継承する | CLI に `inherit_exif_from` を足し ImageIO で継承（Orientation 1・寸法更新・MakerNote 破棄・Software・GPS IFD 保持）。Rust `develop_full_coreimage`、出力先 `$APPDATA/coreimage/full/`、レシピ、`DevelopedResult` 契約。Python フォールバックの削除。原寸の写像較正は別 PR | M0 |
| N-E | 現像元フォルダ設定（config.json）とエンジン情報を Rust コマンドにする | PR(1): `engine_info`（CLI `--version` と macOS 版。`/api/health` の置き換え）と `root_key_for`。PR(2): `get_source_folder` / `set_source_folder`（`CONFIG_VERSION 1` 互換、検証 reason、TCC 文言）。`is_default` は常に false | M0 |
| N-C1 | 解析 lite（走査・EXIF・サムネ・グループ化）を Rust にし、サムネと元 JPG を Tauri 経由で配信する | 採点を除く `analysis.py` の移植。サムネは JPEG で `$APPDATA/thumbnails/`。元 JPG は動的 scope（案 1）か自前 URI scheme（案 2）。「フォルダ未設定」画面。`analysis_version 3` + `scored: false` | M0 |
| N-D1 | 書き出し（JPEG コピー + manifest）を Rust にし、出力先を `$APPDATA/export/` に変える | `camera_jpg` / `developed` の 2 variant、manifest、sha256、複製元の検証（source_root 一致・プレビュー拒否）。「Finder で表示」。`merged` は #84 で追加 | M0 |
| N-G1 | フロントから Python HTTP 経路を全廃し、合成・候補・取り込みを隠し、Drive を削除する | `raw-api.ts` の `fetch` 削除、`engineStatus` を `engine_info` に、`BootstrapGate` / `setup.ts` 削除、Drive の UI・state・型・テスト削除、`isTauri()` 分岐の整理、文言修正 | M0 |
| N-G2 | main.rs の venv / pip / Python サーバ起動を削除し npm scripts を整理する | `main.rs:92-860`、`signal-hook`、`setup:raw` / `dev:raw` / `develop:raw` / `test:raw`、`scripts/dev-raw.sh`。`requirements-raw.txt` は N-H まで残す。M0 の完了条件（§3.7）をこの Issue で確認 | M0 |
| N-C2 | 解析の採点（シャープ・白飛び・黒つぶれ）と推薦を Rust にし、Python と数値一致させる | `score_array` / `recommend` / `raw_recommendation` / `_rank_scores`。N-00 の `analysis.json` と比較（許容差は §4）。`scored: true` | M1 |
| N-D2 | 「写真」アプリへの取り込みを Rust（osascript）にする | `import_batch` の移植、AppleScript を `bundle.resources` へ、TCC 許可先がアプリ本体になる文言と導線、`ExportPanel` のボタンを戻す | M1 |
| N-B | CLI `--batch` に decode キャッシュ op（`op: decode / render`）を足す | 1 回デコードした RGBAh を保持し、後段だけを繰り返す（#102 §5.3、後段 1 回 7.2 ms が目安）。保持する画像の寿命と上限、`clearCaches()` との関係。#87 の代理レンダに使う | M3 |
| N-H | Python 本体を削除する（#90 と同時） | `local_raw/`、`tests/test_*.py`（`scripts/` のテストは残す）、`requirements-raw.txt` → `requirements-dev.txt`、CLAUDE.md の `test:raw`。M2 / M3 の参照実装が不要になってから | M4 |
| N-DOC | API_CONTRACT を Tauri コマンド契約に書き直し、CLAUDE.md の検証手順を更新する | コマンド名・引数・戻り値・エラー code の表、§4 のディレクトリを `$APPDATA` に、§6 の検証手順を CLAUDE.md へ | M4 |
| N-F | （将来）Google Drive アップロードを Rust で実装する | #62 / #17 / #13 と統合して設計からやり直す。`drive_client_secret.json` の置き場、OAuth ループバック。ラベル `future` | 将来 |

## 3. M0 — Python 無しで動く最小の単一アプリ

### 3.0 先に出す小さい PR: `engine_info`【確認】

`App.tsx:221` は `state.engineStatus === "ready"` でないと解析のポーリングを始めず、`engineStatus` は `/api/health` の `health/ok` でしか `ready` にならない（`app-state.ts:496`）。
つまり N-A / N-E / N-C1 / N-D1 を `tauri dev` で確認するとき、**G1 までは Python サーバが動いている前提**になる。
これを避けるため、N-E の PR(1) として `engine_info` コマンド（`coreimage.rs:958 cli_version` を返す）と `health/ok` の置き換えを **M0 の最初に小さく出す**。
以後の各レーンは Python を止めたまま確認できる。

### 3.1 M0 に含めるもの / 外すもの

含める:

- 起動（venv / pip / Python サーバ無し）→ 現像元フォルダの選択（ネイティブダイアログ）→ 解析 lite → グループ一覧 → 操作パネル（元 JPG 表示、フィルムストリップ、原寸ビューア）→ プリセット・調整 → プレビュー現像（既存）→ 本現像（原寸、CI）→ 採用 → 書き出し（JPEG コピー + manifest。EXIF 継承、GPS IFD 保持）。
- エンジン表示は「Core Image <cli_version> / macOS <version>」。

外す（隠す・削除）:

- 削除: 初回セットアップ表示、`/api/health`、`HealthResponse`（`src/lib/types.ts:106`）、Python プレビューへのフォールバック、Drive アップロード UI。
- 隠す（後の M で戻す）: ブラケット合成、現像候補、写真アプリ取り込み。「隠す」は該当セクションを描かない（disabled ではなく非表示。準備中文言 `MERGE_UNAVAILABLE` 等の経路も使わない）。state とコンポーネントは残してよい（M2/M3 で戻すため）。
- 文言の修正: `AnalysisGate.tsx:21` / `App.tsx:537,633`「`npm run dev:raw` で起動してください」、`BootstrapGate.tsx:20`「`npm run setup:raw`」。

### 3.2 既定フォルダが無いことへの対応【確認】

`local_raw/config.py:32` の `DEFAULT_SOURCE_ROOT` はリポジトリ相対 `sample/100MSDCF`。この checkout に `sample/` は無く、バンドル後も無い。
Rust 側 `read_source_root`（`coreimage.rs:155`）は設定が無いと `SOURCE_ROOT_NOT_CONFIGURED` を返す。
M0 では **「フォルダ未設定」状態を新設**し、`AnalysisGate` に「まず現像元フォルダを選んでください」の画面を足す（現状の `AnalysisGate` にこの状態は無い。`absent` は「フォルダはあるが解析していない」の意）。
`SourceFolderInfo.is_default`（`types.ts:600`）は常に false にする（型は残す。意味が無くなった旨をコメントに書く）。

### 3.3 N-C1: 解析 lite とサムネ/元 JPG の配信

Python `analysis.py` の構造【確認】: `scan_pairs`（`:156`）→ `extract_exif`（`:229`）→ `_decode_small`（`:292`、PIL `draft` で 1/8 デコード）→ `score_array`（`:281`、cv2 の `cvtColor` + `Laplacian(...).var()`）→ `make_thumbnail`（`:313`、長辺 640 WEBP q80）→ `group_photos`（`:433`）→ `recommend`（`:491`）→ `raw_recommendation`（`:561`）→ `build_analysis`（`:621`）。

M0 で移すもの:

- 走査: `source_files.py` の `DSC\d{5}.(ARW|JPG)` 規則（Rust の `capture_id_pattern` `coreimage.rs:257` と同根）。
- EXIF 要約: `PhotoInfo` の `captured_at / iso / f_number / shutter / focal_length / exposure_bias / lens_model / orientation / width / height`（`types.ts:127-152`）。`kamadak-exif`（読み込み専用。ADR 0003 §3.1）。
- サムネイル: 長辺 640。**形式は JPEG に変える**（オーナー判断 §6 で変更可。WebP lossy は `webp` crate が要る）。命名 `{root_key}-{capture_id}.jpg` の接頭辞規則は保つ（`API_CONTRACT.md` §4、別フォルダの同名衝突防止）。置き場は `$APPDATA/thumbnails/`。
- デコード手段【要確認。Issue 内の最初のスパイク】: (a) `turbojpeg` crate（libjpeg-turbo の scale 1/8 デコード。PIL `draft` 相当）、(b) `image` crate の全デコード + 縮小（24MP で 1 枚 0.1 秒前後【推測】、6 並列で許容か計測）、(c) CLI に `op: thumbnail`（ImageIO `kCGImageSourceThumbnailMaxPixelSize`）を足す。(a) を第一候補にし、ビルドの手間（C ライブラリ）が重ければ (b)。
- グループ化と kind 判定: `_starts_new_group`（`:390`、60 秒 / 焦点距離比 1.5）、`_is_bracket`（`:408`）、`_classify_kind`（`:416`）は EXIF だけで決まる純関数。そのまま移す。
- **採点は省く。** M0 の値の決め方（UI が嘘を出さないため明文化する）:
  - `sharpness_raw / sharpness_score / highlight_clip_ratio / shadow_clip_ratio / mean_luma / overall_score` は `0`。`OperationPanel` の `ScoreRow` は 0 を表示する（「未採点」表示にするかは N-C1 で決める）。
  - `recommended_capture_id` は時刻順の先頭。`confidence` は `0.5`（`CONFIDENCE_FLOOR`）。`recommendation_reasons` は空。
  - `raw.level / reason_codes` は EXIF 由来のもの（`high_iso` `exposure_bracket`）だけで決める。`highlight_clipping` `deep_shadows` `night_scene` は採点が要るので出さない。
  - `summary.needs_review` は 0。
- 進捗: `spawn_blocking` で走らせ、`get_analysis` コマンドが `{status: absent|running|ready}` を返す。フロントの 1 秒ポーリング（`App.tsx:225-229`）は残す。
- キャッシュ: `analysis.json` を `$APPDATA/analysis/{root_key}.json` に置き、`_source_signature`（`:189`）相当でファイル名一覧が変わったら無効化（#32 の指摘は据え置き）。
- 版: `Analysis.analysis_version` は Python が `2`（`types.ts:212`）。Rust 出力は **`3`** にし、`scored: boolean` を足す（lite は false、N-C2 で true）。
  版を分けないと、N-C2 の数値一致テストとキャッシュの無効化が「採点済みか」を判別できない。Python の v2 キャッシュは読まない（捨てる）。

配信（`assetUrl` / `sourceJpgUrl` が唯一の差し替え点【確認: `raw-api.ts:30-37`】）:

- サムネ: `$APPDATA/thumbnails/**` を `tauri.conf.json` の `assetProtocol.scope` に足し、`convertFileSrc`。
- 元 JPG: 撮影フォルダは実行時に変わる。**案 1**: `app.asset_protocol_scope().allow_directory(root, false)`【確認: tauri 2.11.5 `src/scope/fs.rs:351` に存在。SMB 上の挙動は未確認】。**案 2**: `register_uri_scheme_protocol`（`src/app.rs:2130`）で `geoframe://source-jpg/<id>.JPG` を自前配信し、`server.py` の検証規則（`DSC` + 5 桁 + `.JPG`、`resolve()` 後の親一致）を移植。案 1 を先に試し、動的 scope の解除（フォルダ切替時に前のフォルダを許可したままにしない）ができなければ案 2。
- `DevelopedResult.output_url` のキャッシュバスター `?v=` は asset プロトコルでは不要（ハッシュがファイル名に入る。`coreimage-api.ts:124`）。

完了条件: `cargo test`（走査・EXIF・グループ化の単体）、`npm test`、`tauri dev` でフォルダ選択 → 一覧 → 操作パネルに元 JPG とサムネが出る。N-00 で凍結した `analysis.json` と比べ、`photos` の EXIF 項目と `groups` の `capture_ids / kind` が一致する（採点由来の項目は比べない）。

PR の切り方: (1) Rust 解析 lite + 配信 scope、(2) フロントの `getAnalysis / startAnalyze / assetUrl / sourceJpgUrl` を invoke / asset に切替 + 未設定画面。

### 3.4 N-A: 原寸現像 + EXIF 継承（クリティカルパス）

ADR 0003 §3 の新規 A。範囲:

1. **Swift CLI**: リクエストに `inherit_exif_from`（ペア JPG のパス）を足し、ImageIO で EXIF を継承して書く。規則は現行 `local_raw/exif.py:114-134 build_inherited_exif` と同じ: Orientation 1、ExifIFD `PixelX/YDimension` を出力寸法に、`MakerNote` 破棄、`Software` 設定、**GPS IFD はそのまま継承**（オーナー決定 5）。
   - 最初のタスクは **実機 Sony JPEG での保全確認**（ADR 0003 §3.1 の表）。properties 辞書経由と `CGImageMetadata` コピー経由の両方を試し、GPS IFD・サムネイル IFD・私用タグの残り方を比べる。欠けがあれば Rust の APP1 バイトコピーへ切り替える（その場合 CLI は EXIF を書かず Rust が後処理する）。
   - 現行 CLI は EXIF を書かない【確認: `Develop.swift:698`】。`exif_inherited: false` を固定している `coreimage-api.ts:123` を結果から埋める。
2. **Rust**: `develop_full_coreimage` コマンド。`develop(... DevelopMode::Full ...)` を呼び（`coreimage.rs:1483`）、出力先 `$APPDATA/coreimage/full/`、レシピ JSON、`DevelopedResult` 契約（`filename` `recipe_filename` `params_hash` `exif_inherited` `output_url`）。`max_edge 6000` の扱い（`App.tsx:580`）は CLI の full に合わせて決める。
3. **原寸の写像の較正**: #73 の 9 キー写像は 1600px で較正したもの。デコード段の sharpness / NR は倍率で効き方が変わる（#102 §3.4、#57）。比較ハーネス（`scripts/compare_engines.py`）の full 行で現行 rawpy 出力と並べ、ADR 0002 の「原寸で現行と同等」の条件を別に確かめる。rawpy 側は N-00 で凍結した基準画像を使う。
4. **フロント**: `runDevelop` の full 分岐を invoke に替え、Python フォールバック（`shouldFallbackToRawpy`、`previewEngine`、`GEOFRAME_PREVIEW_ENGINE`）を削除。失敗はエラー表示。

完了条件: `swift test`（EXIF 継承の合成テスト + 実機 round-trip）、`cargo test`、`tauri dev` で本現像 → `BeforeAfter` に原寸結果が出る。出力 JPEG に `DateTimeOriginal / ISO / LensModel / GPS IFD` が残り `MakerNote` が無く `Orientation = 1`（確認手段は §6.3）。

PR の切り方: (1) CLI の EXIF 継承（Swift のみ）、(2) Rust コマンド + フロント切替、(3) 原寸較正（計測と写像の調整。結果次第で無しでもよい）。

### 3.5 N-E: 現像元フォルダ設定の Rust 化

- `get_source_folder` / `set_source_folder` コマンド。`config.json` の `CONFIG_VERSION 1`・`source_root` キーを保ち（`config.py:37-40`）、既存インストールを引き継ぐ。
- 検証（`_validate_source_root` `config.py:222`: invalid / not_found / not_directory / permission_denied / analysis_running）と `SourceFolderErrorReason`（`types.ts:620`）をそのまま返す。`permission_denied` の案内（`PERMISSION_MESSAGE` `config.py:46`）は許可先が **アプリ本体** になる文言に変える。
- `root_key_for`（sha1 先頭 8 文字 `config.py:89`）はサムネ名・解析キャッシュ名で N-C1 と共有する。
- `is_default` は常に false（§3.2）。
- フロント: `getSourceFolder / setSourceFolder` を invoke に。`SourceFolderApiError` は invoke の reject 値 `{code, reason, message}` から組み立てる。

完了条件: `cargo test`（検証の各 reason、`config.json` の読み書き・版）、`npm test`、`tauri dev` でフォルダ切替 → 再解析が動く。

PR: 2 本。(1) `engine_info` + `root_key_for`（§3.0。N-C1 が使うので最初に出す）、(2) フォルダ設定のコマンドとフロント切替。

### 3.6 N-D1: 書き出し（コピー + manifest）

- `export_batch`（`export.py:332`）の Rust 化。`camera_jpg` と `developed` の 2 variant（`merged` は M2 で足す）。manifest JSON（`ExportManifest` `types.ts:570`）、sha256、`batch_name` の正規化（`export.py:58`）。
- **出力先を変える**: 現行は `PROJECT_ROOT/outputs/export/<batch>`【確認: `export.py:27`】でバンドル後には無い。M0 は `$APPDATA/export/<batch>/` を既定にし、書き出し後に「Finder で表示」（`tauri-plugin-opener` か `open -R`）を出す。ユーザー指定フォルダ（`dialog:allow-open` のディレクトリ選択）は M1 以降の任意。
- manifest の相対パス基準は `$APPDATA` 配下に変わる（ADR 0003 §3 新規 D の契約変更）。
- 複製元の検証（`_recipe_source_root` `export.py:246`: レシピの source_root が今のフォルダと一致、プレビューは書き出し不可 `:279`）も移す。
- 書き出し画面から Drive ボタンと取り込みボタンを外す（N-G1 と分担。ここでは Rust コマンドとフロントの `exportSelection` の切替のみ）。

完了条件: `cargo test`、`tauri dev` で採用 → 書き出し → `$APPDATA/export/<batch>/` に JPEG と `manifest.json` ができ、JPEG の EXIF が N-A の出力と一致する（コピーなので当然だが、確認手順に入れる）。

PR: 1 本。N-A の出力先・レシピ形式が決まってから着手。

### 3.7 N-G1 / N-G2: Python の切り離し

N-G1（フロント）:

- `raw-api.ts` の `fetch` 全廃。`LOCAL_RAW_API` 定数、`RawApiError`、`isEndpointUnavailable` を削除（`isEndpointUnavailable` に依存する「準備中」文言 `EXPORT_UNAVAILABLE` `MERGE_UNAVAILABLE` も）。
- `/api/health` の再試行と `health/ok` `health/fail` を `engine_info` に置換。`engineStatus` は `engine_info` 成功で `ready`、`SIDECAR_NOT_FOUND` 等で `offline`。`Topbar` のバッジ文言を Core Image に。
- `BootstrapGate.tsx`・`lib/setup.ts` の `SetupState` 部分を削除（`isTauri` の再エクスポートだけ残すか、`@tauri-apps/api/core` から直接 import に替える）。
- ブラケット合成・現像候補・写真取り込みのセクションを描かない。Drive の UI・state・API 関数・型を削除（`types.ts:472-535` の `DriveUpload*`）。
- `isTauri()` 分岐の整理（`SourceFolderPanel.tsx:107` のテキスト入力フォールバック、`coreimage-api.ts:54`、`confirm.ts:15`）。
- `app-state.test.ts` / `raw-api.test.ts` の該当テストを同じ PR で消す。

N-G2（Rust と npm scripts）:

- `main.rs:92-860` の venv / pip / spawn / signal cleanup（`RawEngineState` `SetupState` `setup_status` `spawn_signal_cleanup_thread`）を削除。`signal-hook` 依存を外す。
- `package.json` の `setup:raw` `dev:raw` `develop:raw` `test:raw` を削除、`scripts/dev-raw.sh` を削除。`requirements-raw.txt` は N-H まで残す（`scripts/` と `tests/` が使う）。
- `capabilities/default.json` の説明文から Python sidecar の記述を外す。
- `tauri.conf.json` の `assetProtocol.scope` を最終形に。

**M0 の完了条件**（`.app` で確認する）:

1. `.venv` をリネームして退避した Mac で `env -u CI npm run tauri:build` の `.app` を起動する。初回セットアップ画面が出ず、ターミナルも要らない。
2. フォルダ未設定の画面 → 「フォルダを選ぶ」→ 解析 lite が走り一覧が出る（進捗表示あり）。
3. 操作パネルで元 JPG とサムネが表示される。プリセット変更 → プレビュー現像 → 本現像 → 前後比較と原寸ビューアで原寸結果が見える。
4. 採用 → 書き出し → `$APPDATA/export/<batch>/` の JPEG に撮影 EXIF と GPS IFD が残り、MakerNote が無く、Orientation が 1。
5. `ps` に `python` が居ない。`lsof -i :8788` が空。
6. `npm test` / `npx tsc --noEmit` / `npm run lint` / `cargo test` / `swift test` が通る。

### 3.8 N-00: 基準値の凍結（Python が動くうちに。M0 の先頭）

Python 環境はこの時点では `npm run setup:raw` で作れる。凍結物の置き場: JSON と小さい合成画像はリポジトリ（`tests/fixtures/` には現在 `presets_legacy_rawpy.json` 1 件【確認】。`tests/fixtures/baseline/` を新設）、実機写真はコミットできないので外部パス（`/Volumes/Backup/100MSDCF` 等）をフィクスチャの README に明記。

| 凍結するもの | 使う Issue |
|---|---|
| 代表フォルダ（星空 11 組を含む、#86 の記述）の Python `analysis.json` と `source_signature` | N-C1（EXIF・グループ）、N-C2（採点・推薦の数値一致） |
| 実機 Sony JPEG（GPS IFD を持つもの。`docs/reports/spikes-wave0.md:125` の根拠ファイル）の `build_inherited_exif` 出力のタグ一覧（タグ ID と値を JSON 化。画像はコミットしない） | N-A（EXIF 継承の受け入れ基準） |
| `scripts/compare_engines.py` の rawpy 基準画像（preview と **full** の両方。ADR 0002 決定 4 の OS 更新回帰用。#90 に記載済み） | N-A（原寸較正）、#90 |
| #86 のフィクスチャ（固定入力 PNG に対する `_image_metrics` / `_star_measurements` / `pareto_front` の JSON） | #86 |
| `scripts/eval_cases/candidates_baseline.json` の Python 経路出力（候補の adjustments と指標） | #87 |
| ブラケット合成の OpenCV 出力（代表 3 組。MTB シフト量と Mertens 出力の PSNR 用） | #83 |
| 書き出し manifest のサンプル（現行形式） | N-D1 |

完了条件: 上の 7 件がリポジトリ（または外部パス付き README）に置かれ、生成コマンドが `tests/fixtures/baseline/README.md` に書かれている。PR 1〜2 本。

## 4. M1 以降

### M1 — 提案の質を戻す

| Issue | 範囲 | 依存 | 完了条件 |
|---|---|---|---|
| N-C2 解析の採点・推薦 | `score_array`（ラプラシアン分散は自前数十行、clip 比・mean_luma）、`recommend` `raw_recommendation` `_rank_scores`（`analysis.py:476-620`、純関数）。`PhotoInfo` の 0 固定を解除 | N-C1、N-00 | 凍結 `analysis.json` と `sharpness_raw` は相対誤差 1e-3、clip 比は 1e-4、`recommended_capture_id` と `raw.level` が全グループで一致。`cargo test` |
| N-D2 写真アプリへの取り込み | `photos_import.py:74 import_batch` → Rust `Command::new("osascript")`（配列引数）。`scripts/photos_import.applescript` を `bundle.resources` へ。TCC の許可先がアプリ本体になるので `PERMISSION_HINT`（`:27`）を書き換え、初回の許可導線を出す。`ExportPanel` のボタンを戻す | N-D1 | `.app` から取り込み → 写真.app に `GeoFrame RAW Assist <batch>` アルバムができる。許可拒否時の案内が出る |

並列: N-C2 ‖ N-D2。PR は各 1 本。

### M2 — ブラケット合成（#83 → #84）

- #83: Swift CLI の合成モード（MTB 位置合わせ + Mertens。設計書 §3.3 B1）。N-00 の OpenCV 出力と PSNR で比較。EXIF 継承は N-A の `inherit_exif_from` を先頭フレームのペア JPG で使う。
- #84: Rust `merge_bracket` コマンド、`MergedResult` 契約、出力先 `$APPDATA/coreimage/merged{,_preview}/`、`BracketMerge` セクションを戻す、`ExportItem.variant = "merged"` を N-D1 に足す。**#84 本文の「GPS・MakerNote は書かない」は「GPS IFD は継承する（現行どおり）。MakerNote は破棄」に直す**（メインが Issue 本文を修正）。
- 完了条件: `swift test`、`cargo test`、`.app` でブラケットグループの合成（プレビュー・原寸）→ 採用 → 書き出し。#8（黒帯）#38（メモリ）の確認観点を #83 に載せる。
- PR: #83 で 1〜2 本、#84 で 1〜2 本。M3 と worktree を分けて並列。

### M3 — 現像候補（PSO）と暗所

- #85 は閉じる（ADR 0003 §4。やることの残りは #87 本文へ）。
- #86（純関数。N-00 のフィクスチャと数値一致）‖ N-B（CLI `--batch` に `op: decode / render` の decode キャッシュ op。#102 §5.3）→ #87（探索ループ、Tauri コマンド、`DevelopmentCandidates` を戻す）。
- #88（暗所判定 N1/N2）と #57（代理 640px と原寸のシャープの差）は #87 と同時。
- 完了条件: `cargo test`、`.app` で候補生成が 1 枚 10 秒以内【推測: #102 §5.2 の 3〜6 秒 + 指標計算】、`candidates_baseline.json` の候補集合と指標で比較（乱数列の一致は求めない。ADR 0003 §4）。
- PR: #86 1 本、N-B 1 本、#87 1〜2 本。

### M4 — 後片付け

| Issue | 範囲 |
|---|---|
| #90 + N-H Python 全削除 | `local_raw/`、`tests/test_*.py`（`scripts/` のテスト `test_compare_engines.py` `test_evaluate_*.py` は残す）、`requirements-raw.txt` → `requirements-dev.txt`（numpy / Pillow / cv2 / rawpy。`scripts/` 用。rawpy は compare_engines の基準画像生成に使うなら残す、使わないなら外す）、CLAUDE.md の `test:raw` 記述。**`local_raw/bracket.py` と `development_candidates.py` は M2 / M3 の参照実装なので、H は M2・M3 の後**に置く |
| N-DOC 契約文書 | `docs/API_CONTRACT.md` を Tauri コマンド契約（コマンド名・引数・戻り値・エラー code）に書き直す。§4 のディレクトリを `$APPDATA` 配下に。CLAUDE.md の検証手順を §6 の内容に更新（メインが編集） |
| N-F（将来）Drive アップロード | Rust 移植。#62 / #17 / #13 と統合して設計からやり直す。`drive_client_secret.json` の置き場（`$APPDATA`）、OAuth ループバック受け口。優先度は低。ラベル `future` |

## 5. 並列化と worktree

| 期間 | レーン（worktree） | 待ち |
|---|---|---|
| M0 前半 | N-00（1 本。他と並列可だが G の前に終える） | — |
| M0 前半 | N-E PR(1) `engine_info` + `root_key_for` を最初に（§3.0）→ N-A ‖ N-E PR(2) ‖ N-C1（3 本） | N-C1 は `root_key_for` を、全レーンは `engine_info`（Python 無しで `engineStatus` が ready になる）を使う |
| M0 後半 | N-D1 → N-G1 → N-G2（直列） | N-D1 は N-A の出力先、N-G1 は A/C1/E/D1 全部 |
| M1 | N-C2 ‖ N-D2 | — |
| M2 / M3 | #83→#84 ‖ (#86 ‖ N-B)→#87 | EXIF 継承（N-A）、`ExportItem.merged`（N-D1） |
| M4 | #90+N-H → N-DOC | M2 / M3 完了 |

worktree には `node_modules` / `.venv` / `src-tauri/binaries/` が無い。各レーンで `npm ci && npm run build:coreimage` を先に回す（`cargo test` は externalBin の存在を要求する【確認: `scripts/build-coreimage-cli.sh` 冒頭のコメント】）。N-00 のレーンだけ `npm run setup:raw` も要る。

## 6. 検証手順（ブラウザ検証の代わり）

CLAUDE.md の「画面表示の検証はブラウザ（vite dev）で行う」は決定 3 で成り立たなくなる。代案（CLAUDE.md 自体の更新は N-DOC でメインが行う）:

### 6.1 コマンド

| 目的 | コマンド | 備考 |
|---|---|---|
| 型・lint・reducer | `npx tsc --noEmit` / `npm run lint` / `npm test` | 変更なし。`vitest` は node 環境のまま |
| Rust | `cargo test --manifest-path src-tauri/Cargo.toml` | 事前に `npm run build:coreimage`（externalBin 必須） |
| Swift CLI | `swift test --package-path native/coreimage-cli` | EXIF 継承・合成モードの単体 |
| 画面（開発中） | `env -u CI npm run tauri:dev` | `beforeDevCommand` が CLI ビルド + vite を起こす（`tauri.conf.json`）。WebView は右クリック → 「要素の詳細を表示」で DevTools（debug ビルド）。`CI=1` の環境で `--ci` エラーになるかは `tauri:build` と同じ理由で起きうる【要確認: この shell では `CI` が空だった】 |
| 画面（配布形） | `env -u CI npm run tauri:build` → `open src-tauri/target/release/bundle/macos/GeoFrame\ RAW\ Assist.app` | M0 の完了条件はこちらで確認する。`.venv` を退避してから起動する |

### 6.2 画面の確認観点（手動。各 PR の本文にチェックリストとして載せる）

1. 起動直後の画面（未設定 / 解析前 / 一覧）が意図どおりか。初回セットアップ表示が出ないこと。
2. フォルダ選択 → 解析進捗 → 一覧（サムネが出る。出ないときは asset scope か配信規則）。
3. 操作パネル: 元 JPG、フィルムストリップ、原寸ビューア、←→ 移動、調整値破棄の確認ダイアログ。
4. プレビュー → 本現像 → 前後比較。エラー時の文言（Python へのフォールバックが無いので SIDECAR_* がそのまま出る）。
5. 書き出し → Finder で成果物を開く。
6. 隠した機能（合成・候補・取り込み）が見えないこと。Drive の痕跡が無いこと。

ブラウザ検証で失うもの: Playwright 等のブラウザ自動化と `vite` 単体の高速起動。代わりに reducer の `vitest` を厚くし（`app-state.test.ts` は既にある）、Rust / Swift のコマンド層を単体テストで押さえる。WebView の自動化は M0 では持たない【推測: `tauri-driver` は macOS 非対応のため】。

### 6.3 EXIF / GPS の確認手段

`exiftool` はこの Mac に無い【確認: `which exiftool` 空】。依存を増やさない手段:

- `mdls -name kMDItemLatitude -name kMDItemLongitude -name kMDItemAcquisitionModel <出力.jpg>`（Spotlight のメタデータ。GPS があれば緯度経度が出る）【要確認: この手順はまだ実行していない。N-00 の実機サンプルで確かめてから README に載せる】。
- 開発用 Python（決定 4 で残る）: `.venv/bin/python -c "from PIL import Image; im=Image.open('<出力.jpg>'); ex=im.getexif(); print(ex.get_ifd(0x8825)); print(0x927C in ex.get_ifd(0x8769)); print(ex.get(274))"` → GPS 辞書が空でない・MakerNote が False・Orientation が 1。
- `swift test` の実機 round-trip は、N-00 で凍結したタグ一覧 JSON と比較する。

## 7. 既存 Issue との対応

| 既存 Issue | 本プランでの位置 |
|---|---|
| #105 | この文書と ADR 0003 で判断。M0 の Issue 群を立てたら閉じる |
| #90 rawpy 取りやめ | M4（N-H と同時。基準画像は N-00） |
| #83 / #84 ブラケット合成 | M2。#84 の GPS 文言を直す |
| #85 PSO 代理レンダの CLI 切替 | **閉じる**（#87 へ統合。ADR 0003 §4） |
| #86 / #87 PSO | M3 |
| #88 暗所判定、#57 シャープの代理差 | M3（#87 と同時） |
| #89 リニア出力 | 任意のまま |
| #62 / #17 / #13 Drive | N-F（将来）に統合。初版の対象外 |
| #58 ISO 分岐点 | N-A の原寸較正で参照（独立に進めてよい） |
| #8 / #38 合成の黒帯・メモリ | #83 の確認観点 |
| #32 / #33 解析キャッシュ署名・pairs 数 | N-C1 で Rust 化するときに同時に直すか据え置くかを Issue 本文で決める |
| #5 フォルダ切替と競合 | N-E / N-C1 の `sourceFolderGeneration` 維持で現状どおり |
| #9 stale 成果物の export | N-D1 の複製元検証（`_recipe_source_root`）で現状と同等を保つ |
| #3 / #28 フロントの残課題 | M0 対象外 |
| #10 lens reason ラベル、#39〜#42 Python のテスト・契約 | Python 側の指摘は N-H で消える。#10 は CI の `lens_correction.reason` に合わせて残すか判断 |

## 8. リスク

- **EXIF の実機保全（N-A）**: ImageIO 経由で Sony の私用タグ・サムネイル IFD が保てるかは未確認。欠ければ Rust の APP1 バイトコピーへ（ADR 0003 §3.1）。M0 のクリティカルパスなので最初に確かめる。
- **原寸の写像**: プレビューと原寸で sharpness / NR の効き方が違う（#102 §3.4）。N-A の PR(3) を独立にして、較正が長引いても M0 の他 Issue を止めない。
- **元 JPG の配信**: 動的 scope の挙動（特に SMB、フォルダ切替時の解除）が未確認。案 2（自前 URI scheme）を用意しておく。
- **解析 lite の表示**: 採点 0 のまま一覧を出すと「おすすめ」の意味が薄い。M0 の期間を短くし N-C2 を直後に置く。
- **TCC**: 撮影フォルダ（外付け / SMB）の読み取り許可がアプリ本体に付く。`permission_denied` の導線を N-E で用意する。
- **M0 の規模**: Rust 新規 2,000〜2,800 行【推測: ADR 0003 §3 の A 600〜900 + C の半分 500〜600 + D 300 + E 300〜400 + G の削除】、Swift 300〜500 行（EXIF 継承）。Issue 化のとき各 Issue で見直す。
