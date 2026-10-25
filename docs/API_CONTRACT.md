# GeoFrame RAW Assist — Tauri コマンド契約

> **この文書はデスクトップアプリ（Tauri）のフロント ⇔ Rust 間の契約を、現行コードに合わせて記述する。**
> 正はコードであり、食い違いを見つけたらコードを読んで本書を直す（Issue #118 で HTTP 契約から全面改訂）。
>
> - 改訂日: 2026-10-23（Issue #118 / 設計 `docs/design/single-app-implementation-plan.md` §4 M4 の N-DOC）
> - 前提: [ADR 0003](adr/0003-python-removal-single-app.md)（Python を外した単一アプリ）。
>   **HTTP サーバ（`local_raw/server.py`、`http://127.0.0.1:8788`）とブラウザ単体（vite dev + Python）で動く形は廃止**した。
>   フロントは `@tauri-apps/api` の `invoke` だけを使い、`fetch` は持たない（`src/lib/raw-api.ts` / `src/lib/coreimage-api.ts`）。
> - 旧 HTTP 契約（凍結版、`/api/*` エンドポイント・`outputs/` ディレクトリ・rawpy パイプライン）は
>   コミット `f3e24b0` 時点の本ファイルを参照。現行の決定と食い違う点は §10 にまとめた。
> - TypeScript 側の写し: `src/lib/types.ts`。
> - #90/#117でPython本体を削除済み。評価用Pythonは[独立評価手順](development/evaluation-paths.md)に従い、保存画像・JSONとNumPy/Pillow/OpenCVだけを使う。旧実装を含む最後の参照点は `e838ecc`。
> - 節番号 §2（Adjustments）・§5（プリセット）・§9（resolve の規則）はコードのコメントから参照されている
>   （`src-tauri/src/adjustments.rs`・`presets.rs`、`native/coreimage-cli/Sources/CoreImageDevelop/Adjustments.swift`、
>   `native/coreimage-cli/README.md`）。**番号を変えないこと。**

---

## 0. 基本事項

### 0.1 呼び出し規約

- 登録: `src-tauri/src/main.rs` の `tauri::generate_handler!`。コマンドは 13 本と独自スキーム `geoframe://` 1 本（§1）。
- **引数名**: Rust 側は snake_case、JS の `invoke` に渡すキーは **camelCase**（Tauri 既定の変換。`rename_all` は使っていない）。
  例: Rust `capture_ids: Vec<String>` ⇔ JS `invoke("merge_bracket", { captureIds, ... })`。
- **戻り値のフィールド名はすべて snake_case**（serde の rename は無い）。
- **エラー**は `invoke` の reject 値として届く。形はコマンドで 3 種類ある:

| 形 | 使うコマンド |
|---|---|
| `CoreImageError` = `{ code: string, message: string }` | `get_analysis` `start_analysis` `engine_info` `develop_preview_coreimage` `develop_full_coreimage` `merge_bracket` `generate_development_candidates` |
| `SourceFolderError` = `{ code: string, reason: string, message: string }`（`code` と `reason` は同じ値。フロントは `reason` を読む） | `set_source_folder` |
| 素の `string`（日本語メッセージ） | `export_selection` `reveal_export` `import_to_photos` |

- `message` は利用者向けの日本語文。パスを含むことがある（ローカルアプリなので許容）。
- `get_presets` と `get_source_folder` は失敗しない（`Result` を返さない）。
- `import_to_photos` は**写真.app 側の失敗を `Ok` の中で返す**（`status: "error"`。§1.13）。

### 0.2 アプリデータディレクトリ `$APPDATA`

`app.path().app_data_dir()`。識別子は `com.geoframe.rawassist`（`src-tauri/tauri.conf.json`）なので、macOS では
`~/Library/Application Support/com.geoframe.rawassist/` になる（Tauri の既定規則による）。配下の構成は §4。

### 0.3 画像の配信

| 種別 | 方式 | URL |
|---|---|---|
| 現像・合成の出力、サムネイル | Tauri asset プロトコル。scope は `$APPDATA/coreimage/**` と `$APPDATA/thumbnails/**` のみ（`tauri.conf.json` `app.security.assetProtocol`） | `asset://localhost/<絶対パスを encodeURIComponent したもの>` |
| 現像元フォルダのカメラ JPG | 独自スキーム `geoframe://`（`analysis::source_jpg_protocol`。§1.14） | `geoframe://localhost/source-jpg/DSC12345.JPG` |

- asset URL はフロントの `convertFileSrc(path)` で作るもの（サムネイル・プレビュー）と、Rust が組み立てて返すもの
  （`develop_full_coreimage` / `merge_bracket` の `output_url`）がある。どちらも macOS の形
  （`asset://localhost/…`、パス全体を `encodeURIComponent` 相当で符号化）。
- 現像元フォルダは任意の場所にあるので asset scope に入れず、`geoframe://` で `DSC\d{5}.JPG` だけを配る。
- `csp` は `null`。capabilities（`src-tauri/capabilities/default.json`）は `core:default`・`dialog:allow-open`・
  `dialog:allow-confirm`。アプリ独自コマンドに個別の permission は付けていない。

### 0.4 共通の入力検証

- **capture_id**: `^DSC[0-9]{5}$`（ASCII のみ）。違反は `INVALID_CAPTURE_ID`（`coreimage.rs`）。ファイル存在確認より前に判定する。
- **現像元フォルダ**: `$APPDATA/config.json` の `source_root`（§4）。RAW / JPG は**そのフォルダ直下**に限る。
  `canonicalize` 後の親ディレクトリが現像元フォルダと一致しないもの（シンボリックリンクで外へ出るもの）は拒否する。
- **Core Image CLI の同時実行**: `DEVELOP_SLOT` は CLI を 1 本ずつ走らせ、待ちは最大 2 件。
  さらに来た呼び出しは即 `BUSY`。プレビュー・原寸現像・合成・候補生成で共有する。キャッシュヒットはスロットを取らない。
- **QA 専用 feature `resident-preview-qa`（通常ビルドには存在しない。#141 B1 PR-B）**: 専用 QA ID
  `com.geoframe.rawassist.qaroadmap` でのみ有効で、他の ID で起動すると `main` が `Builder::build` の前に stderr へ
  `RESIDENT_QA_WRONG_APP_ID: ...` を出して `exit(1)` し、ウィンドウは作られない（`setup` にも防御的な同じ検査がある）。
  環境変数などの実行時切替はない。IPC の引数・結果の形は通常ビルドと同じ。上の契約との差分は次のとおり。
  - プレビュー現像だけが常駐 stateless CLI（resident）で実行される。原寸・合成・候補生成・native ROI は従来の単発 CLI /
    独自セッションのまま。
  - 例外として「実行中 1 本 + 待機中の常駐 CLI 1 本」が同時に存在しうる。CPU/GPU で同時に現像するのは今までどおり 1 本で、
    プロセス総数が 1 本という意味ではない。常駐 CLI は idle TTL・要求回数・アプリ終了で閉じる（QA 用の未調整ノブ）。
  - resident のプレビューはキャッシュヒットでも CLI の同一性検査のために先にスロットを取る。他の現像が走行中なら待ち（上限超過は
    `BUSY`）になる（通常ビルドのキャッシュヒットは待たない）。
  - 来歴 JSON に `resident_qa: {schema, executor: "resident_batch", owner_pid}` が付く（resident が実行したときだけ。
    キャッシュ鍵・request には入らない）。キーが無い来歴は単発製か旧来歴で、キャッシュヒットの応答は生成時の来歴をそのまま返す
    ので、タグは「過去にその pid が実行した」事実であり現在の生存を意味しない。
  - 追加 code（§0.5 の表にも載せる）: `SIDECAR_IDENTITY_CHANGED`（CLI が差し替えられた。アプリ再起動が必要で、同一プロセス内では回復しない）、
    `RESIDENT_CLOSED`（終了処理後の要求）、`RESIDENT_QA_NOT_READY`（QA 初期化前）。

### 0.5 エラー code 一覧（`CoreImageError`）

Rust が生成する code:

| code | 主な条件 | 出しうるコマンド |
|---|---|---|
| `INVALID_CAPTURE_ID` | §0.4 の書式違反 | develop 系、merge、candidates |
| `INVALID_CAPTURE_COUNT` | 合成の枚数が 2〜9 の外 | merge |
| `DUPLICATE_CAPTURE_ID` | 合成の ID 重複 | merge |
| `INVALID_BUDGET` | `budget` が 32〜512 の外 | candidates |
| `UNKNOWN_PRESET` | プリセット名が §5 に無い | develop_full、merge、candidates |
| `ADJUSTMENTS_INVALID` | §2 の検証違反（オブジェクトでない・未知キー・欠落キー（develop 系）・非数値・非有限・不正な `noise_reduction`） | develop 系、merge |
| `APP_DATA_DIR_UNAVAILABLE` | `app_data_dir()` を解決できない | analysis 系、develop 系、merge |
| `SOURCE_ROOT_NOT_CONFIGURED` | `config.json` に `source_root` が無い・空・相対パス | analysis 系、develop 系、candidates |
| `SOURCE_ROOT_NOT_FOUND` | 現像元フォルダを解決できない・ディレクトリでない・読めない | develop 系、candidates |
| `SOURCE_ROOT_INVALID` | 合成時に現像元フォルダを解決できない | merge |
| `ARW_NOT_FOUND` | `<ID>.ARW` が現像元フォルダ直下に無い・通常ファイルでない・stat 失敗 | develop 系、merge、candidates |
| `ARW_OUTSIDE_SOURCE_ROOT` | 解決後の ARW が現像元フォルダ直下にない | develop 系 |
| `OUTPUT_DIR_FAILED` | `$APPDATA/coreimage/<sub>` を作れない・検証できない・シンボリックリンクで外へ出る | develop 系、merge、candidates |
| `CACHE_IO_FAILED` | 合成の古いキャッシュを置き換え・検査できない | merge |
| `PROVENANCE_WRITE_FAILED` | 現像の来歴 JSON を書けない | develop 系 |
| `THUMBNAIL_FAILED` / `CACHE_WRITE_FAILED` | 解析中のサムネイル・解析キャッシュの書き込み失敗（**`get_analysis` の `error` に message だけが載る**。§1.1） | start_analysis（非同期） |
| `SOURCE_SCAN_FAILED` | 解析の開始時または終了時の照合で、現像元フォルダを走査できない（消えた・ディレクトリでない・権限・列挙の途中失敗）か、対象ファイルのサイズ・mtime を取れない（壊れたシンボリックリンク等。§3 の入力指紋）。解析中に読めなくなった場合も含む。空フォルダは失敗ではない。`error` の扱いは上と同じ | start_analysis（非同期） |
| `SOURCE_CHANGED` | 解析中に対象ファイルが追加・削除・変更された（終了時の入力指紋が開始時と違う。§3）。結果はキャッシュに保存しない。`error` の扱いは上と同じ | start_analysis（非同期） |
| `CANDIDATES_FAILED` | 候補生成の scratch・代理画像・CLI 異常終了・スレッド異常 | candidates |
| `BUSY` | §0.4 の待ち上限超過 | develop 系、merge、candidates |
| `SIDECAR_NOT_FOUND` | 実行ファイルの隣に `geoframe-ci-develop` が無い（`npm run build:coreimage` 未実行） | engine_info、develop 系、merge、candidates |
| `SIDECAR_SPAWN_FAILED` | CLI を起動できない | 同上 |
| `SIDECAR_IO_FAILED` | CLI の SHA-256 計算・stdin 書き込み・wait の失敗 | 同上 |
| `SIDECAR_TIMEOUT` | タイムアウトで kill（§1 各コマンドの値） | 同上 |
| `SIDECAR_BAD_RESPONSE` | 最終行が JSON でない・構造化エラー無しで失敗・`output` が要求と違う・ファイルが無い・寸法や必須キーが欠落 | 同上 |
| `SIDECAR_SESSION_CLOSED` | `--batch` セッションがタイムアウト等で閉じた後の要求 | candidates |
| `SIDECAR_IDENTITY_CHANGED` | **QA feature `resident-preview-qa` のみ**。resident の CLI が固定した sidecar と違う（差し替え・別パス）。sticky でアプリ再起動が必要 | develop_preview_coreimage |
| `RESIDENT_CLOSED` | **QA feature のみ**。アプリ終了処理の後の resident preview 要求 | develop_preview_coreimage |
| `RESIDENT_QA_NOT_READY` | **QA feature のみ**。QA 状態の初期化前の preview 要求（通常は起きない） | develop_preview_coreimage |
| `RESIDENT_QA_WRONG_APP_ID` | **QA feature のみ・起動時のみ**。専用 QA ID 以外で起動した。**IPC 応答ではなく** stderr（`code: message`）と終了コード 1 で、ウィンドウ作成前に拒否する | （起動時） |
| `INTERNAL_ERROR` | `spawn_blocking` のスレッドが異常終了 | analysis 系、develop 系、merge |
| `INTERNAL` | 同上（**`engine_info` だけこの code**。§10.3） | engine_info |

CLI（`geoframe-ci-develop`）の失敗応答 `{"ok":false,"error":{code,message}}` は **code・message をそのまま詰め替えて返す**
（`coreimage.rs: cli_error`）。CLI の code は `native/coreimage-cli/README.md`「エラー」節が正:
`INVALID_REQUEST` `INPUT_NOT_FOUND` `INPUT_NOT_READABLE` `OUTPUT_DIR_NOT_FOUND` `UNSUPPORTED_RAW`
`UNSUPPORTED_DECODER_VERSION` `RENDER_FAILED` `WRITE_FAILED` `INTERNAL_ERROR`、`--batch` のみ `HANDLE_NOT_FOUND`。
Rust が先に弾くので `INPUT_NOT_FOUND` / `INVALID_REQUEST` は通常届かない（ARW が判定と実行の間に消えた場合などに限る）。

---

## 1. コマンド

| # | コマンド | JS 引数 | 戻り値 | エラー形 | 実装 |
|---|---|---|---|---|---|
| 1.1 | `get_analysis` | なし | `AnalysisStatus` | `CoreImageError` | `analysis.rs` |
| 1.2 | `start_analysis` | `{ force?: boolean }` | `"started" \| "already_running" \| "ready"` | `CoreImageError` | `analysis.rs` |
| 1.3 | `get_source_folder` | なし | `SourceFolderInfo` | 失敗しない | `source_folder.rs` |
| 1.4 | `set_source_folder` | `{ path: string }` | `SourceFolderInfo` | `SourceFolderError` | `source_folder.rs` |
| 1.5 | `get_presets` | なし | `PresetInfo[]` | 失敗しない | `presets.rs` |
| 1.6 | `engine_info` | なし | `EngineInfo` | `CoreImageError` | `coreimage.rs` |
| 1.7 | `develop_preview_coreimage` | `{ captureId, adjustments }` | `CoreImagePreviewResult` | `CoreImageError` | `coreimage.rs` |
| 1.8 | `develop_full_coreimage` | `{ captureId, adjustments, preset }` | `DevelopedResult` | `CoreImageError` | `coreimage.rs` |
| 1.9 | `merge_bracket` | `{ captureIds, preset, adjustments, preview, force }` | `MergedResult` | `CoreImageError` | `bracket.rs` |
| 1.10 | `generate_development_candidates` | `{ captureId, preset, seed?, budget?, nativeRoiDiagnostics? }` | `DevelopmentCandidatesResponse` | `CoreImageError` | `candidates/command.rs` |
| 1.11 | `export_selection` | `{ batchName, items }` | `ExportResponse` | `string` | `export.rs` |
| 1.12 | `reveal_export` | `{ path }` | なし | `string` | `export.rs` |
| 1.13 | `import_to_photos` | `{ batchName, album? }` | `ImportToPhotosResponse` | `string` | `photos_import.rs` |
| 1.14 | （スキーム）`geoframe://localhost/source-jpg/<ID>.JPG` | — | JPEG バイト列 | HTTP ステータス | `analysis.rs` |

### 1.1 `get_analysis`

解析の状態を返す。進捗はイベントではなく**この呼び出しのポーリング**で取る（解析はイベントを emit しない）。

```
AnalysisStatus =
  | { status: "running", progress: { stage: "scanning" | "scoring" | "grouping", done: number, total: number } }
  | { status: "ready",   analysis: Analysis }       // §3
  | { status: "absent",  error?: string }           // error は直前の解析失敗の message（code は載らない）
```

- 実行中かどうかを先に見るので、`running` は `config.json` が無くても返る。
- `ready` はキャッシュ（§4 `analysis/<root_key>.json`）が有効なときだけ。有効条件は §3。
  同じ完了済み解析を何度読んでも `analysis.analysis_revision` と `analysis.source_id` は変わらない。
- 応答はリクエスト時点の現像元フォルダについてのもの。フロントは応答（成功・未設定・失敗とも）に
  リクエストを出した時点のフォルダ世代（`sourceFolderGeneration`）を添えて reducer に渡し、
  フォルダ変更を跨いで遅れて届いた前のフォルダの応答を捨てる（#5）。
- エラー: `SOURCE_ROOT_NOT_CONFIGURED`、`APP_DATA_DIR_UNAVAILABLE`、`INTERNAL_ERROR`。

### 1.2 `start_analysis`

- 引数 `force`（省略時 `false`）。戻り値は即時:
  - `"already_running"` … 解析中。
  - `"ready"` … `force=false` で有効なキャッシュがある（再解析しない）。
  - キャッシュの照合（全対象ファイルの stat）はジョブ状態のロックの外で行い、開始の直前にロックを取り直して
    二重開始を防ぐ。照合とロックの間に別の解析が終わった場合は、解析が 1 回余分に走るだけ。
  - `"started"` … バックグラウンド（`spawn_blocking`、最大 6 ワーカー）で解析を開始。直前の `error` を消す。
    完了した解析は入力が前回と同じでも新しい `analysis_revision` を持つ（`force=true` の再解析を見分けるため）。
- 解析中の失敗（`THUMBNAIL_FAILED`、`CACHE_WRITE_FAILED`、`SOURCE_SCAN_FAILED`、`SOURCE_CHANGED`）は
  `get_analysis` の `{status:"absent", error}` で見える。
  デコードできない JPG は黙って飛ばす。
- 対象ファイルは現像元フォルダ直下の `^DSC\d{5}\.(ARW|JPG)$`（拡張子は大小無視）。
- エラー: `SOURCE_ROOT_NOT_CONFIGURED`、`APP_DATA_DIR_UNAVAILABLE`、`INTERNAL_ERROR`。

### 1.3 `get_source_folder`

```
SourceFolderInfo = {
  path: string,           // canonicalize 済み絶対パス
  display_path: string,   // プロジェクト直下なら相対、HOME 配下なら "~/…"、それ以外は絶対パス
  is_default: boolean,    // 現状は常に false
  exists: boolean, readable: boolean,
  pairs: number, jpg_only: number, arw_only: number, total_bytes: number
}
```

- `config.json` が無い・読めない・`source_root` が空/相対のときは、**コンパイル時に埋め込まれた
  `<リポジトリ>/sample/100MSDCF`** の情報を返す（表示用のフォールバック。他のコマンドは同じ状況で
  `SOURCE_ROOT_NOT_CONFIGURED` を返す。§10.3）。
- 件数は拡張子 `jpg`/`arw`（大小無視）とファイル名の stem で数える。解析の `DSC\d{5}` 規則より緩いので
  `Analysis.source` の件数と一致しないことがある。

### 1.4 `set_source_folder`

- 引数 `path`（前後の空白は除く。絶対パス必須）。成功時は保存したフォルダの `SourceFolderInfo`。
- `$APPDATA/config.json` へ `{ config_version: 1, source_root: <canonicalize 済みパス> }` を書く。未知のキーは保持する。
  一時ファイル `config.<pid>-<nonce>.tmp` → rename、プロセス内の mutex で直列化。
- 古い解析キャッシュ・サムネイルは消さない（`root_key` が変わるので読まれなくなるだけ）。
- エラー `SourceFolderError.code`（判定順）:

| code | 条件 |
|---|---|
| `analysis_running` | 解析中（パスより先に判定） |
| `invalid` | 空・相対パス・その他の metadata エラー・canonicalize 失敗・`app_data_dir` 失敗・設定の保存失敗 |
| `not_found` | パスが存在しない |
| `permission_denied` | metadata が PermissionDenied、または `read_dir` 失敗（message でシステム設定 > プライバシーを案内） |
| `not_directory` | ディレクトリでない |

### 1.5 `get_presets`

`PresetInfo = { name, label, description, adjustments: Adjustments }`（§2 の 9 キー全量）。並びと値は §5。

### 1.6 `engine_info`

```
EngineInfo = { cli_version: string, macos_version: string, sidecar_resolved: boolean /* 成功時は常に true */ }
```

- `macos_version` は `/usr/bin/sw_vers -productVersion`、失敗時は CLI の自己申告。`cli_version` は CLI の `--version`
  （サイドカーのパスごとに初回成功値をキャッシュ。タイムアウト 10 秒）。
- エラー: `SIDECAR_*`、`INTERNAL`、CLI 由来 code。

### 1.7 `develop_preview_coreimage`

- 引数: `captureId`、`adjustments`（§2 の **9 キー全量**。フロントの `resolveAdjustments` でプリセット値と合成済みのもの）。
- CLI 要求の固定値: `decoder_version: "9"`、`settings: { profile: "apple_default", lens_correction: true }`、
  `format: "jpeg"`、**`max_edge: 2400`（`PREVIEW_MAX_EDGE`。ウィンドウ・画面からは決めない固定値）**。
- EXIF は書かない（`exif_inherited` は常に `false`）。タイムアウト 60 秒（スロット待ちを除く）。
- 出力: `$APPDATA/coreimage/preview/<ID>_<hash16>.jpg` + `.json`（§4）。

```
CoreImagePreviewResult = {
  capture_id, output_path /* 絶対パス */, provenance_path /* 絶対パス */,
  width, height,
  elapsed_ms: { decode, render_encode, total } | null,   // キャッシュヒット時は元の実行の値
  cache_hit: boolean, preview: boolean, exif_inherited: boolean,
  params_hash: string /* 16 hex */, decoder_version: "9", cli_version, macos_version,
  adjustments: Adjustments /* 正規化後 */, applied: object /* CLI の applied */,
  adjustments_applied: object | null /* CLI 0.3.0 以上。highlight_recovery の写像結果を含む */,
  source: object /* CLI の source（lens_model, iso, camera_model ほか） */
}
```

フロントは `toDevelopedResult`（`coreimage-api.ts`）で `DevelopedResult` に詰め替え、`convertFileSrc(output_path)` を表示に使う。

### 1.8 `develop_full_coreimage`

- 引数: `captureId`、`adjustments`（9 キー全量）、`preset`（プリセット名。**ラベル付けと名前の検証だけ**に使い、値は `adjustments` が正）。
- `max_edge` を送らない（原寸）。それ以外の固定値は §1.7 と同じ。タイムアウト 60 秒。
- **EXIF 継承あり**（§7.2）。継承に失敗しても現像は成功し `exif_inherited: false`。
- 出力: `$APPDATA/coreimage/full/<ID>_<hash16>.jpg` + `.json`。

```
DevelopedResult = {
  capture_id, preset, preset_label,
  filename /* 出力 JPEG の basename */, recipe_filename /* 来歴 JSON の basename */,
  params_hash /* hash16 の先頭 10 文字 */, adjustments, width, height,
  elapsed_seconds /* 小数 2 桁 */, cached: boolean, preview: false,
  lens_correction: { applied: boolean, lens: string | null, flags: [], reason?: "coreimage_unsupported" },
  exif_inherited: boolean,
  output_url: "asset://localhost/<符号化した絶対パス>",
  engine: "coreimage",
  highlight_recovery_effect?: object   // adjustments_applied.highlight_recovery があるときだけ
}
```

エラー（判定順）: `UNKNOWN_PRESET` → `ADJUSTMENTS_INVALID` → `INVALID_CAPTURE_ID` → `APP_DATA_DIR_UNAVAILABLE` →
`SIDECAR_NOT_FOUND` → `SOURCE_ROOT_*` → `ARW_*` → `OUTPUT_DIR_FAILED` → `BUSY` → `SIDECAR_*` / CLI 由来 →
`PROVENANCE_WRITE_FAILED`。§1.7 も `UNKNOWN_PRESET` を除き同じ。

### 1.9 `merge_bracket`

露出ブラケット（解析の `bracket` グループ）を Core Image で現像し、**MTB 位置合わせ + Mertens Exposure Fusion** で 1 枚にする（HDR + トーンマップは採用しない）。

- 引数（**すべて必須**。`max_edge` 引数は無い）:
  - `captureIds: string[]` … 2〜9 枚。**昇順に並べ替えてから処理・記録**する。重複は `DUPLICATE_CAPTURE_ID`。
    どのフレームを混ぜるかはフロントの責務（1 グループに別シーンの AEB が混ざりうるため）。
  - `preset: string`、`adjustments: object` … `resolve_adjustments(preset, adjustments)` で解決（§9。部分指定も可）。
  - `preview: boolean` … プレビュー合成か本合成か。
  - `force: boolean` … `true` でキャッシュを見ずに古い出力を消して再合成。フロントは常に `false`。
- CLI 要求（1 回）: `mode: "merge"`、`format: "jpeg"`、`jpeg_quality: 0.92`、`decoder_version: "9"`、
  `settings: { profile: "apple_default", lens_correction: true }`、`adjustments`、
  `merge: { align: true, max_shift_ratio: 0.02, edge: "crop", weights: { contrast: 1, saturation: 1, exposure: 0 } }`。
  - **preview は `max_edge: 2400`（`PREVIEW_MAX_EDGE`、固定）。本合成は `max_edge` を送らない**（原寸。共通 crop で数 px 小さくなりうる）。
  - `exposure` / `highlight_recovery` は CLI が全フレームで 0 に固定し `adjustments_applied.ignored` に記録する。
    `wb_temp` / `noise_reduction` / デコーダのシャープは各フレームのデコードに、残りの後段は合成結果に 1 回だけ効く。
- **基準フレームの規則（2 つは別物）**:
  - 位置合わせの基準 = **昇順に並べた列の中央** `index = count / 2`（整数除算。2 枚→1、3 枚→1、5 枚→2。
    `MergeCore.swift`、来歴の `merge.reference_index`）。
  - **ファイル名の先頭 ID・EXIF 継承元 = 昇順の先頭** `capture_ids[0]` のペア JPG（`bracket.rs`）。
  - 位置合わせが成立しない（`align_failed` / `shift_exceeds_ratio` / `empty_common_crop`）ときはシフト無しで合成し
    `aligned: false`、理由は来歴の `merge.reason`。探索上限は `floor(min(w,h) × 0.02)` px。
- EXIF: 本合成のみ先頭フレームのペア JPG から継承（§7.2）。preview は継承しない。継承できなくても合成は成功。
- タイムアウト: preview 120 秒、本合成 240 秒。
- 出力: 本合成 `$APPDATA/coreimage/merged/`、プレビュー `$APPDATA/coreimage/merged_preview/`（**ディレクトリで分ける**。
  書き出しが「原寸かどうか」をディレクトリと来歴で判定するため。§1.11）。命名・キャッシュは §4。

```
MergedResult = {
  capture_ids: string[] /* 昇順 */, frames: number, preset, preset_label,
  filename, recipe_filename, params_hash /* 10 hex */,
  adjustments /* 解決後の 9 キー全量 */, width, height, elapsed_seconds,
  cached: boolean, preview: boolean, aligned: boolean,
  lens_correction: { applied: boolean, flags: [], reason?: "coreimage_unsupported" | "inconsistent_across_frames",
                     lens?: string, lens_model?: string /* 先頭フレームの source.lens_model */ },
  exif_inherited: boolean,
  output_url: "asset://localhost/<符号化した絶対パス>?v=<mtime_ns>"
}
```

- `lens_correction.reason` の語彙は 2 値（`tests/fixtures/coreimage_merge_lens_reasons.json` が正）:
  `coreimage_unsupported`（全フレームで CI のレンズ補正を適用できなかった）、
  `inconsistent_across_frames`（フレーム間で適用状態が揃わなかった）。
- エラー: `INVALID_CAPTURE_COUNT` `INVALID_CAPTURE_ID` `DUPLICATE_CAPTURE_ID` `UNKNOWN_PRESET` `ADJUSTMENTS_INVALID`
  `APP_DATA_DIR_UNAVAILABLE` `SOURCE_ROOT_INVALID` `ARW_NOT_FOUND` `CACHE_IO_FAILED` `OUTPUT_DIR_FAILED` `BUSY`
  `SIDECAR_*` `INTERNAL_ERROR`、CLI 由来。

### 1.10 `generate_development_candidates`

1 枚の RAW について、スタイル別の現像候補（`Adjustments`）を多目的 PSO で探す。

- 引数: `captureId`、`preset`、`seed?: number`（u32）、`budget?: number`、`nativeRoiDiagnostics?: boolean`（省略時false）。
  - `budget` 既定 48、範囲 32〜512（外れは `INVALID_BUDGET`）。
  - `seed` 既定 = SHA-256(`"{capture_id}:{preset}"`) の先頭 4 バイト（big-endian）。
- `nativeRoiDiagnostics: true` は探索終了後、naturalと最終候補だけを原寸ROIで診断する明示opt-in。
  候補ID・adjustments・proxy metrics・スコア・閾値・順位・採否は変えない。sharpness上限1.6、seed/budgetも維持。
- 評価は長辺 **640 px の代理画像**（`renderer.rs: MAX_EDGE`。プレビューの 2400 とは別）で行う。
  CLI は `--batch` セッション（要求ごとに 60 秒のタイムアウト。全体の締め切りは無い）。
- スタイル: `natural`（プリセット値そのもの・最適化しない）と `social` は常に。`stars`（星空と判定）、
  `foreground`（暗部画素の比率が大きい）、`low_noise`（ISO ≥ 1600 などノイズが多い）は条件付き。
- 進捗イベント: `development-candidates-progress` を emit
  `{ capture_id, preset, stage: "decode" | <style id> | "native_roi" | "done", completed, total }`。
  フロントは探索と原寸診断の両方を待って表示する。native_roiのcompleted/totalは原寸設定数、探索の単位とは別。
- ファイル: `$APPDATA/coreimage/candidates/pso-<pid>-<n>/`（0700）を scratch に使い、終了時（成功・失敗・panic）に消す。結果のキャッシュは無い。

```
DevelopmentCandidatesResponse = {
  capture_id, preset, seed /* 実効値 */, budget /* 実効値 */,
  proxy: { max_edge /* 実際の代理画像の長辺 */, width, height, source: "coreimage_proxy",
           decode_edge: 640, decodes, renders, evaluations },
  star_scene: { is_star_scene, star_count, star_point_density /* null 可 */, dark_background_ratio,
                bright_point_ratio, exif_night, exif_state: "night" | "daylight" | "unknown",
                star_halo, star_color_saturation },
  candidates: [{ id, label, description, adjustments /* 9 キー全量 */, metrics, score /* 0..100 */, reasons: string[] /* ≤4 */ }],
  rejected: [{ id, reason }],
  elapsed_ms: { total /* 原寸診断を含む */, decode /* 代理のみ */, render /* 代理のみ */ },
  native_roi_diagnostics: NativeRoiDiagnostic
}
```

- `metrics` は 21 キー: `highlight_clip` `shadow_clip` `noise_flat` `source_noise_flat` `detail` `source_detail` `halo`
  `color_saturation_clip` `tonal_range` `source_deviation` `shadow_mean` `star_count` `star_halo` `star_color_saturation`、
  旧 Python 互換の別名 `highlight_clipping` `shadow_crushing` `flat_region_noise` `detail_halo` `color_clip` `tone_range`
  `baseline_deviation`（`candidates/metrics.rs`）。
- `candidates` は `natural` が先頭、以降はスタイル順。乱数列の Python 実装との一致は求めない（ADR 0003 §4）。
- エラー: `INVALID_CAPTURE_ID` `INVALID_BUDGET` `UNKNOWN_PRESET` `SOURCE_ROOT_*` `ARW_NOT_FOUND` `OUTPUT_DIR_FAILED`
  `BUSY` `CANDIDATES_FAILED` `SIDECAR_*`（`SIDECAR_SESSION_CLOSED` を含む）、Rust の `--batch` セッションが出す `INVALID_REQUEST`、CLI 由来（`HANDLE_NOT_FOUND` を含む）。

原寸診断の独立フィールド（完了は画質合格や検証済み採用を意味しない）:

```ts
NativeRoiDiagnostic = {
  status: "not_run" | "missing" | "failed" | "complete",
  reason: string | null,
  unique_settings: number, renders: number,
  elapsed_ms: { total: number },
  rois: [{ name: "center" | "detail" | "flat_dark" | "flat_mid",
           status: "selected" | "missing", normalized: [x0,y0,x1,y1] | null, reason: string | null }],
  sets: [{ candidate_ids: string[], adjustments /* 原寸用に正規化したコピー */,
           status: "missing" | "failed" | "complete", reason: string | null,
           dimensions?: [width,height], total_ms, render_ms?, crop_ms?, metrics_ms?,
           rois: [{ name, normalized, status: "complete" | "failed", reason,
                    pixels?: [x,y,width,height], absolute?: { detail, noise_flat, halo },
                    delta_vs_natural?: { detail, noise_flat, halo },
                    bands?: { grad_mean, band_px, band_matched, band_matched_sigma, mean_luma },
                    bands_delta_vs_natural?: { band_px, band_matched } }] }]
}
```

- OFFは `not_run / opt_in_disabled`、空rois/sets・0回。ONでも欠測・失敗を成功や合格へ変換しない。
  一部ROIを選べない場合は `missing` と理由を返す。原寸CLIエラー/タイムアウトは `failed` とコードを記録し、既存候補は返す。
  natural原寸が失敗した場合、後続は `missing / natural_full_unavailable`、Δを捏造しない。
- naturalの640代理から決定的に選んだ4種の正規化座標を全設定で共有する。選べないROIの代替はしない。
  `band_px` のσ=1.1px、`band_matched` のσ=1.1×原寸長辺/640。星の指標は従来の640全体のみ。
- 原寸設定は正規化後に重複排除し、naturalを含め最大5設定。CLIは本現像と同じdecoder 9/profile/lens/JPEG品質で
  順次full描画（各180秒上限）する。JPEG全体は一時的にu8で読み、floatはROIのみ保持する。
  scratch内のJPEGを設定ごとに削除し、終了時に専用ディレクトリも回収する。通常/採用済みの出力とキャッシュは触らない。
- プロキシbatch終了後も同じ実行枠を保持した内部処理を使い、枠を二重取得しない。CLI同時1本を保つ。
- 待ち開始前にsourceRoot/RAW/CLIを固定し、設定ファイル・RAW・CLIの同一性と最新要求を最後まで確認する。
  フォルダ切替・同名差替え・再要求で古くなった処理は `CANDIDATES_STALE` でrejectし、旧結果は公開しない。
  UIもsource世代と要求番号で古い完了を破棄する。原寸診断はFFI正式採用を前提にしない。

### 1.11 `export_selection`

選んだ写真を `$APPDATA/export/<batch>/` へ**バイト列のままコピー**し、`manifest.json` を書く（再エンコードしない）。

- 引数: `batchName: string`、`items: ExportItem[]`（1〜500 件）。
  - `batch_name`: 前後空白除去、1〜64 文字。`/` `\` NUL `..` を含むとエラー。それ以外の `[A-Za-z0-9_-]` 以外の文字は `_` に置換。
  - `ExportItem = { capture_id, variant: "camera_jpg" | "developed" | "merged", developed_filename?, merged_filename? }`
    （`capture_id` は前後空白除去・大文字化して §0.4 で検証）。
- variant ごとの規則:
  - `camera_jpg` … 現像元フォルダの `<ID>.JPG` をコピー。
  - `developed` … `$APPDATA/coreimage/full/<developed_filename>` と来歴 `.json`（`mode == "full"`）が必要。
    `coreimage/preview/` にしか無いものは「プレビュー品質の画像は書き出せません。」で拒否。
  - `merged` … `$APPDATA/coreimage/merged/<merged_filename>` だけを見る（`merged_preview/` は拒否）。来歴の
    `kind == "exposure_fusion"`・`preview == false`・`request.max_edge` 無し・`capture_ids` が昇順 2〜9 で先頭が
    `capture_id` と一致・ファイル名の ID/枚数/プリセット/ハッシュ・`output_size` と `output_sha256` の一致を検証する。
    **`merged` の `capture_id` は合成の先頭（最小）ID を渡す**。
- 元ファイルが無いものはエラーにせず `skipped` に入れる。出力名の重複も `skipped`。
- manifest の `group` は解析キャッシュ（§3）から引く。キャッシュが**無い**（未解析・フォルダ変更直後）なら
  `group: null` のまま書き出す。キャッシュは**あるのに今の写真と一致しない・読めない**（写真の追加・削除・
  差し替え、旧形式、壊れている、サムネイル欠け）なら、コピーと manifest を作る前に
  「解析結果が現在の写真フォルダの内容と一致しません（…）。解析をやり直してから書き出してください。」で止める
  （古い group / was_recommended を黙って落とさないため。#32）。
- 書き出し全体をプロセス内 mutex で直列化。一時ファイル `.export-<pid>-<n>.tmp` → rename。

```
ExportResponse = { export_dir: string, manifest_path: string, files: string[] /* 絶対パス、manifest を除く */,
                   skipped: [{ capture_id, reason }] }
```

`manifest.json`（整形 JSON + 末尾改行）:

```
{ manifest_version: 1, app: "geoframe-raw-assist", batch_name, created_at /* UTC RFC3339 */,
  source_root /* manifest からの相対パス（無理なら絶対） */,
  items: [{ capture_id, variant, exported_file, exported_sha256,
            source: { jpg, arw, jpg_sha256 }, recipe /* developed/merged は来歴 JSON 全量、camera_jpg は null */,
            group: { id, kind, raw_level, was_recommended, user_selected: true } | null,
            exif: { captured_at, iso, lens_model, focal_length } }] }
```

エラーは日本語の `string`（例: 「バッチ名にパス区切りは使用できません。」「書き出す写真は1〜500枚で指定してください。」
「variant は camera_jpg / developed / merged を指定してください。」「合成JPEGがレシピに記録された出力と一致しません。」）。

### 1.12 `reveal_export`

- 引数 `path`（`ExportResponse.export_dir` を渡す）。canonicalize 後に `$APPDATA/export` の**中**（それ自身は不可）である
  ことを確かめ、`/usr/bin/open -R <path>` で Finder に表示する。
- エラー: 「書き出し先以外は表示できません。」「Finder で表示できませんでした。」、存在しないパスは I/O エラー文。

### 1.13 `import_to_photos`

`$APPDATA/export/<batch>/` 直下の `*.jpg`（大小無視・名前順。manifest に無い JPG も含む）を写真.app のアルバムへ取り込む。

- 引数: `batchName`、`album?`（省略時 `"GeoFrame RAW Assist " + 正規化後の batch 名`。指定時は空・120 文字超・改行/NUL 不可）。
- `osascript <bundle の photos_import.applescript> <album> <file>...`（引数配列で渡し、文字列連結しない）。
  タイムアウト 180 秒（スクリプト側は 170 秒）。スクリプトは `bundle.resources`、開発時は `scripts/photos_import.applescript`。
- 入力の不正（batch 名・書き出しフォルダが無い・対象 JPG が無い・album 名）は `Err(string)`。
- **osascript の結果は `Ok` で返す**:

```
ImportToPhotosResponse =
  | { status: "ok", imported: number, album: string }
  | { status: "error", error: string, stderr?: string /* 先頭 500 文字 */ }
```

- 許可エラー（stderr に `-1743` `-1728` `Not authorized` 等）は「「写真」の操作が許可されていません。」+ 案内
  「「システム設定 > プライバシーとセキュリティ > オートメーション」で、GeoFrame RAW Assist（アプリ本体）に「写真」の操作を許可してください。」。
  TCC の許可先はアプリ本体。

### 1.14 `geoframe://` スキーム（元 JPG 配信）

- URL: `geoframe://localhost/source-jpg/DSC12345.JPG`。パスは `^/source-jpg/(DSC[0-9]{5})\.JPG$` に限る
  （URL 上は大文字 `.JPG` 固定。実ファイルの拡張子の大小は問わない）。フロントは文字列で組み立てる
  （`convertFileSrc` は `/` を `%2F` にするため使わない。`raw-api.ts`）。
- 応答: 200 `image/jpeg` / 400「無効なJPGファイル名です。」/ 404「現像元フォルダが未設定です。」「元JPGが見つかりません。」/
  500「元JPGを読めません。」「アプリのデータフォルダを取得できません。」。エラー本文は `text/plain`。
  全応答に `Access-Control-Allow-Origin: *`。Range・キャッシュヘッダは無い。

---

## 2. `Adjustments`（型と範囲）

```
Adjustments = {
  exposure: number,           // EV -2.0..+2.0
  wb_temp: number,            // -100..100
  shadow_lift: number,        // 0..100
  highlight_recovery: number, // 0..100。CLI 0.6.0（#81）が exposure / shadow_lift へ写像して加算する
  brightness: number,         // 0.7..1.4
  contrast: number,           // 0.8..1.4
  saturation: number,         // 0.0..1.5（0 = モノクロ）
  sharpness: number,          // 0.8..1.6（1.0 = カメラJPEG相当の撮影時シャープ）
  noise_reduction: "off" | "light" | "full"   // 大文字小文字を区別
}
```

検証・正規化は `src-tauri/src/adjustments.rs: normalize_adjustments` の 1 か所:

- **数値の範囲外は上記範囲へクランプ**（拒否しない）。整数は f64 に、`-0.0` は `0.0` に正規化。
- 未知キー・bool/文字列/null/配列/オブジェクト・非有限値・不正な `noise_reduction` は `ADJUSTMENTS_INVALID`。
- 出力のキー順は固定（入力の順序や int/float の違いでキャッシュキーが変わらない）。
- `develop_preview_coreimage` / `develop_full_coreimage` は **9 キー全量が必須**（欠落も `ADJUSTMENTS_INVALID`）。
  部分指定を許すのは `resolve_adjustments` を通す `merge_bracket`（§9）。
- 値から Core Image（`CIRAWFilter` と後段チェーン）への写像は CLI が持つ（`native/coreimage-cli/README.md`）。
  `highlight_recovery` の写像は区分線形 `0 → (0EV, 0)`・`60 → (−0.7EV, +40)`・`90 → (−1.0EV, +55)`・`100 → (−1.1EV, +60)` を
  `exposure` / `shadow_lift` に加算（lift は 100 で頭打ち）。詳細は [`design/coreimage-preset-retune.md`](design/coreimage-preset-retune.md) §2・§3。

---

## 3. 分析データ型

`get_analysis` の `analysis`（`analysis.rs: build_analysis`。`ANALYSIS_VERSION = 3`）:

```
Analysis = {
  analysis_version: 3, scored: true,
  generated_at: string,            // ローカル時刻 %Y-%m-%dT%H:%M:%S%:z（表示用。秒精度なので世代の判定に使わない）
  analysis_revision: string,       // 完了した解析ごとの一意な世代（不透明。現行は 32 桁 hex）
  source_id: string,               // 解析した入力の識別子（不透明。現行は入力指紋 v2 の SHA-1 40 桁 hex）
  source: { root /* display_path と同じ整形 */, pairs, jpg_only, arw_only, total_bytes },
  summary: { groups, raw_candidates /* raw.level が recommended|strong のグループ数 */, needs_review /* confidence < 0.6 */ },
  photos: { [capture_id]: PhotoInfo },
  groups: GroupInfo[]
}

PhotoInfo = {
  capture_id, captured_at: string | null, iso: number | null,
  f_number, shutter, focal_length, exposure_bias: number | null, lens_model: string | null,
  orientation: number, width, height,
  sharpness_raw /* 3 桁 */, sharpness_score /* 0..100 */,
  highlight_clip_ratio, shadow_clip_ratio /* 5 桁 */, mean_luma /* 2 桁 */, overall_score,
  thumb: string                    // サムネイル JPEG の絶対パス（URL ではない。表示は convertFileSrc）
}

GroupInfo = {
  id: number, kind: "burst" | "bracket" | "similar" | "single",
  title: string,                   // "HH:MM 連写|ブラケット|類似|単写 N枚"（時刻不明は --:--）
  start_at: string | null, end_at: string | null,
  capture_ids: string[],           // 時刻順
  recommended_capture_id: string, confidence: number,
  recommendation_reasons: [{ code: "single_shot" | "sharpest" | "highlight_retained" | "balanced" | "close_call", text }],
  raw: { level: "none" | "maybe" | "recommended" | "strong", label: string,
         reason_codes: ("night_scene" | "high_iso" | "highlight_clipping" | "deep_shadows" | "exposure_bracket")[],
         reasons: string[] }
}
```

- `close_call` は confidence < 0.6 のとき付く。
- **`analysis_revision`（解析の世代）**: 解析が完了するたびに新しく作る一意値（ナノ秒の現在時刻・プロセス ID・
  プロセス内の連番と、std `RandomState` の値の SHA-1 先頭 32 桁）。一意性を担うのは時刻 + PID + 連番の組で
  （同じプロセス内では連番が必ず違い、再起動後は時刻か PID が違う）、`RandomState` は推測しにくくするための補助
  （鍵はスレッドごとに 1 度だけ乱数で初期化され以後は増分されるだけで、呼び出しごとの新しい乱数ではない）。
  入力からは導かないので、同じ入力・同じ秒の強制再解析でも変わり、
  キャッシュに保存されるので同じ解析の再取得では変わらない。フロントは採用（accepted）の結び直しを
  この値の変化で判定する（#3 B1）。値が無い v2（Python）の解析だけ `generated_at` で代用する。
- **`source_id`（入力の識別子）**: 入力指紋 v2。現像元フォルダの解決済みパスと、対象ファイル
  （`^DSC\d{5}\.(ARW|JPG)$`、拡張子の大小無視。表記違いの同 ID も別エントリとして全部）の
  **実名・バイト数・mtime（ナノ秒）** を実名の辞書順に並べた SHA-1。入力が変わらなければ再解析しても同じ値。
  解析の**読み込み前**（scanning）に取り、終了時に走査し直して比べる。違えば `SOURCE_CHANGED`、
  読めなくなっていれば `SOURCE_SCAN_FAILED` で失敗し、結果をキャッシュに保存しない
  （終了時の照合より後の変更は、次の `get_analysis` の照合で外れる）。
  中身はハッシュしない（ポーリングごとに 24MP の JPG/ARW を読まないため。照合は stat のみ）。
- **stat 方式の検出限界**: サイズと mtime を保ったままの差し替え（`cp -p`、`rsync -t`、mtime を戻すツール等）は
  検出しない。FAT32 の mtime は 2 秒、exFAT は 10ms 単位なので、その粒度内でサイズも同じ書き換えは見逃しうる。
  逆に mtime だけ変わる操作（`touch`、コピーし直し）でも再解析になる（安全側）。
- **キャッシュの有効条件**（すべて満たすときだけ `ready`）: `analysis_version == 3`、`scored == true`、
  内部キー `_source_fingerprint: { version: 2, digest }`（返却時には除く）の版が一致し、`digest` が
  `source_id` と同じで、今のフォルダから取り直した入力指紋とも一致する、`analysis_revision` が空でない、
  全 `photos[*].thumb` が存在。指紋を取れない（フォルダの走査・エントリ列挙・stat のどれかが失敗）ときも無効で、
  一部だけ読めた一覧で一致させることはない。
  無効なキャッシュは削除せず「無い」扱い（再解析でサムネイルも作り直す）。
  旧形式（`_source_signature` = パスと名前一覧のみ）のキャッシュは `_source_fingerprint` を持たないので、
  更新後の初回に一度だけ無効になる。`analysis_version` は 3 のまま（公開の形は追加フィールドのみ）。
- 今回の範囲外: `source_id` が変わっても、現像・合成キャッシュ（§4 `coreimage/`。こちらは元 ARW の
  サイズ・mtime を別途キーに含む）の一括削除や、選択グループ等の state の再結合はしない。
  フロントが採用を結び直す契機は `analysis_revision` の変化だけ。
- `tests/fixtures/baseline/analysis.json` は旧形式の凍結記録で、`analysis_revision`・`source_id` を持たない。
  現行契約に合わせて書き換えない（基準比較は採点値・グループ・推薦・`summary` だけを見る）。

---

## 4. ファイル命名・ディレクトリ規約

すべて `$APPDATA`（§0.2）配下。リポジトリ内の `outputs/` は使わない。

```
$APPDATA/
  config.json                    { config_version: 1, source_root: <絶対パス>, ...未知キーは保持 }
  analysis/<root_key>.json       解析キャッシュ（.json.tmp → rename）
  thumbnails/<root_key>-<ID>.jpg 解析用サムネイル（長辺 ≤640・拡大しない・JPEG quality 80・Orientation 適用済み）  [asset scope]
  coreimage/                                                                                                    [asset scope]
    preview/<ID>_<hash16>.jpg|.json   プレビュー現像（長辺 2400）
    full/<ID>_<hash16>.jpg|.json      原寸現像（EXIF 継承）
    merged_preview/<stem>.jpg|.json   合成プレビュー（長辺 2400）
    merged/<stem>.jpg|.json           本合成（EXIF 継承）
    candidates/pso-<pid>-<n>/         候補生成の scratch（0700、終了時に削除）
  export/<batch>/                  書き出し（コピーした JPEG + manifest.json）
```

- 旧版（Python / Drive 時代）の残骸 `logs/`（`raw-engine.log`）・`drive_auth.json` が残っていることがあるが、現行コードは読み書きしない。

- `root_key` = 現像元フォルダの解決済み絶対パスの SHA-1 先頭 8 文字（`coreimage.rs: root_key_for`）。
  `capture_id` はカメラの連番でカードをまたぐと衝突するため接頭辞にする。
- 合成の `stem` = `{昇順先頭ID}-merged{枚数}-{preset}-{params_hash10}`（例 `DSC04510-merged5-neutral_recovery-a1b2c3d4e5`）。

### 4.1 キャッシュキーと来歴 JSON

| 対象 | キー | 含めるもの |
|---|---|---|
| 現像（preview / full） | SHA-256 の先頭 16 hex（`hash16`）。`DevelopedResult.params_hash` はその先頭 10 文字 | `CACHE_KEY_SCHEMA = 3`、`output` を除く CLI 要求全量（ARW の絶対パス・mode・`max_edge`（preview のみ）・format・decoder_version・settings・正規化後 adjustments）、CLI `--version` 応答、CLI バイナリの SHA-256、ARW のサイズと mtime(ns)、full のみ `rust_exif { schema: 1, ペア JPG の SHA-256, software }` |
| 合成 | SHA-256 の先頭 10 hex | `MERGE_SCHEMA = 2`、CLI 要求全量、preset、source_root、各 ARW の `{size, mtime_ns}`、CLI の版とバイナリ SHA-256、本合成のみ `rust_exif` |

- 来歴 JSON（出力 JPEG と同じステム）= CLI 応答 + Rust が足したもの。現像は `exif_inherited`・`mode`・`request`。
  合成は `kind: "exposure_fusion"`・`capture_ids`・`preset {name,label}`・`adjustments`・`preview`・`source_root`・
  `params_hash`・`exif_inherited`・`engine { name: "coreimage mtb+mertens", cli_version, macos_version, merge_version }`・
  `request`・`lens_correction`・`output_size`・`output_sha256`（EXIF 継承後の値）。
- ヒット条件: JPEG と JSON がともに通常ファイルで JSON の `ok == true`（合成はさらに要求・ハッシュ・`output_sha256` 等の一致）。
  JPEG だけが残っているのは前回の途中終了とみなして作り直す。ヒット時は JPEG の mtime を更新する。
- 書き込みは一時ファイル → rename のアトミック方式。10 分より古い一時ファイルは次の現像時に掃除する。
- 容量上限: 各ディレクトリで 600 組または 1.5 GB を超えたら、mtime の古い順に 80% まで削除する（直前に書いた組は残す）。

---

## 5. プリセット 11 種

定義の正は `src-tauri/src/presets.rs`（`PRESETS`）。フィクスチャ `src-tauri/tests/fixtures/presets.json` と Rust のテストが一致を守る
（再生成: `UPDATE_PRESETS_FIXTURE=1 cargo test write_presets_fixture -- --ignored`）。UI は `get_presets` で一覧を取る。
値は #81（2026-10-22）で Core Image 向けに再調整した初期案（[`design/coreimage-preset-retune.md`](design/coreimage-preset-retune.md) §7.2。
計測と目視は [`coreimage-preset-retune-report.md`](reports/coreimage-preset-retune-report.md) で継続）。
既定プリセットは `neutral_recovery`（`DEFAULT_PRESET`）。右端は `highlight_recovery` を写像して加算した実効値。

| name | label | exposure | wb_temp | shadow_lift | highlight_recovery | brightness | contrast | saturation | sharpness | noise_reduction | 実効 (EV, lift) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `neutral_recovery` | 階調回復 | 0.0 | 0 | 15 | 0 | 1.0 | 1.03 | 1.02 | 1.04 | light | (0, 15) |
| `landscape_clear` | 風景クリア | 0.0 | 0 | 10 | 0 | 1.0 | 1.10 | 1.08 | 1.15 | light | (0, 10) |
| `nightscape` | 夜景ノイズ低減 | 0.0 | -3 | 15 | 0 | 1.0 | 1.04 | 1.0 | 1.02 | full | (0, 15) |
| `sunset_glow` | 夕景・マジックアワー | 0.0 | 12 | 4 | 60 | 1.0 | 1.05 | 1.07 | 1.03 | light | (−0.7, 44) |
| `indoor_warm` | 室内・料理 | 0.1 | 5 | 18 | 0 | 1.0 | 1.02 | 1.03 | 1.02 | light | (+0.1, 18) |
| `portrait_soft` | 人物・柔らか | 0.0 | 3 | 12 | 0 | 1.0 | 0.98 | 1.0 | 0.96 | light | (0, 12) |
| `monochrome_street` | モノクロ | 0.0 | 0 | 10 | 0 | 1.0 | 1.08 | 0.0 | 1.04 | light | (0, 10) |
| `highlight_rescue` | 白飛び救済 | 0.0 | 0 | 0 | 90 | 1.0 | 1.0 | 1.0 | 1.02 | light | (−1.0, 55) |
| `shadow_rescue` | 黒つぶれ救済 | 0.0 | 0 | 45 | 0 | 1.0 | 1.0 | 1.0 | 1.01 | full | (0, 45) |
| `backlit_rescue` | 逆光 | 0.0 | 0 | 30 | 60 | 1.0 | 1.0 | 1.02 | 1.02 | light | (−0.7, 70) |
| `vivid_travel` | ビビッド | 0.0 | 0 | 10 | 0 | 1.0 | 1.07 | 1.10 | 1.05 | light | (0, 10) |

> **2026-08-19 の 2 種追加の理由（ユーザー承認済み）**: 解析は「黒つぶれ」「白飛び」を推薦理由に出すのに救済プリセットが
> 白飛び側にしか無かったため、`shadow_rescue` と `backlit_rescue` を足した。逆光は専用の検出コードを持たず、
> `highlight_clipping` と `deep_shadows` が同時に立つことをシグナルにする（初期プリセット選択はフロント `app-state.ts`）。

旧 rawpy 向けの値（legacy 表、`tests/fixtures/presets_legacy_rawpy.json`）は凍結した評価参照として保持し、アプリでは読まない。本契約の対象外で、#90/#117で削除したPython本体とは独立している。

---

## 6. TypeScript 側の取り決め

- 契約型の写しは `src/lib/types.ts`。`invoke` を呼ぶのは `src/lib/raw-api.ts`（解析・フォルダ・プリセット・書き出し・取り込み）と
  `src/lib/coreimage-api.ts`（エンジン情報・現像・合成）、候補生成は `src/App.tsx`。
- プリセットは `string` + `get_presets` の `PresetInfo[]` で扱う（union 型は持たない）。
- 現像・合成に渡す `adjustments` はフロントの `resolveAdjustments`（`app-state.ts`）で 9 キー全量にしてから送る。
- reject 値はコマンドごとに形が違う（§0.1）。`CoreImageError` は `code` で、`SourceFolderError` は `reason` で分岐する。

---

## 7. エンジンと EXIF の確定事項

### 7.1 Core Image CLI（サイドカー）

- バイナリ `geoframe-ci-develop`（`tauri.conf.json` の `externalBin`。`npm run build:coreimage` が作る）。
  実行ファイルと同じディレクトリ（開発時 `target/debug/`、`.app` では `Contents/MacOS/`）から探す。
- 単発要求は stdin に JSON 1 個 → 最終の空でない stdout 行を JSON 応答として読む。候補生成は `--batch`（JSON Lines、
  `sequence` 照合、op `decode` / `render` / `release`）。CLI の要求・応答の詳細は `native/coreimage-cli/README.md`。
- CLI は出力 JPEG に撮影 EXIF / GPS を書かない（sRGB ICC は書く）。EXIF は Rust が後から付ける（§7.2）。

### 7.2 EXIF 継承（`src-tauri/src/exif_inherit.rs`。ADR 0003 §3.1・§6 決定 5、2026-10-22 オーナー確定）

- **対象**: 原寸現像（`develop_full_coreimage`）と本合成（`merge_bracket` の `preview: false`）だけ。プレビューは継承しない。
- **継承元**: 現像元フォルダ直下のカメラのペア JPG `<ID>.JPG`（合成は**昇順先頭 ID** のもの）。現像元フォルダ外を指すものは使わない。
- **方式**: ペア JPG の EXIF APP1 を**バイト列のままコピー**し、必要なエントリだけ同じ長さで書き換える（再シリアライズしない）。
  コピーした APP1 は出力 JPEG の SOI の直後に置き、出力側の既存 EXIF APP1 は捨てる（ICC の APP2 などは保つ）。
- **変えるもの**: `Orientation`(0x112) → 1（CLI 出力は正立のため）、`PixelXDimension` / `PixelYDimension` → 出力寸法、
  `Software`(0x131) → `"GeoFrame RAW Assist (Core Image)"`（無ければ足す）。
- **落とすもの**: `MakerNote`(0x927C) のエントリ（実体バイトは参照されないまま残る）。IFD1（サムネイル IFD）へのリンク。
- **GPS**: **GPS IFD (0x8825) はカメラのペア JPG にあればそのまま継承する（消さない）。アプリが新たに GPS を書くことはしない。**
- **失敗時**: 現像・合成は失敗させない。ペア JPG が無い・構造が想定外・必須タグ欠落・APP1 が 65533 バイト超などは
  `exif_inherited: false` で CLI の JPEG をそのまま残す（理由は返さない・GPS や画素値はログに出さない）。

---

## 8. テスト運用

検証コマンドと画面の確認観点は [USAGE.md](USAGE.md) §7.1「テストと検証」・§7.5「画面の手動確認の観点」に置く
（`docs/design/single-app-implementation-plan.md` §6 の内容）。契約に関わる自動テスト:

- `cargo test --manifest-path src-tauri/Cargo.toml`（事前に `npm run build:coreimage`）… 入力検証・キャッシュキー・出力先分離・
  EXIF 継承（合成 GPS の保持・MakerNote の除去）・プリセットのフィクスチャ一致。
- `swift test --package-path native/coreimage-cli` … CLI の要求検証・合成モード（中央 pivot、境界枚数）。
- `npm test` … reducer と `raw-api` / `coreimage-api` の invoke 引数。

---

## 9. resolve の規則（`presets.rs: resolve_adjustments`）

- `resolve_adjustments(preset_name, overrides)` はプリセット既定値に `overrides` を上書きマージし、§2 の `normalize_adjustments` に通して 9 キー全量を返す。
  - 未知のプリセット名 → `UNKNOWN_PRESET`。
  - `overrides` が `null` / 省略 → プリセット値そのまま。オブジェクト以外 → `ADJUSTMENTS_INVALID`。
  - 未知キー・不正な型・非有限値・不正な `noise_reduction` → `ADJUSTMENTS_INVALID`。数値の範囲外はクランプ。
- 使う場所: `merge_bracket`（フロントの `adjustments` を overrides として渡す）、`generate_development_candidates`（`overrides = None` で基準値を作る）。
- `develop_preview_coreimage` / `develop_full_coreimage` は resolve を通さず、フロントが解決した 9 キー全量を `normalize_adjustments` で検証するだけ。
  （`presets.rs` の docコメント「現状 Tauri コマンドからは使っていない」は古い。§10.3）

---

## 10. 旧記述との差分（現行の決定との区別）

Issue #83 / #84 の本文、旧 HTTP 契約（`f3e24b0` 時点の本ファイル）、設計書には、現行コード・オーナー決定と違う記述が残っている。
**下表の「現行」が正**。

### 10.1 ブラケット合成（#83 / #84）

| 旧記述 | 出典 | 現行 | 根拠 |
|---|---|---|---|
| 位置合わせの基準フレームは `inputs` の先頭 / 昇順先頭 ID を基準フレーム・EXIF 継承元・ファイル名先頭にする | #83「やること」2、#84「capture_ids の検証」、旧 §1 `merge-bracket`、`coreimage-feature-migration.md:271` | **位置合わせの基準は中央 pivot（`count / 2`）**。昇順先頭 ID を使うのは **EXIF 継承元とファイル名の先頭**だけ | `MergeCore.swift`（`let pivot = images.count / 2`）、`bracket.rs`（`pair_jpeg(&source_root, &capture_ids[0])`）、`coreimage-merge-core-report.md` |
| `merge_bracket(capture_ids, preset, adjustments, preview, force, max_edge)` / プレビューの長辺はフロントが `max_edge` で決める | #84「やること」、旧 §1 `body.max_edge` | **`max_edge` 引数は無い**。preview は `PREVIEW_MAX_EDGE = 2400` 固定、本合成は `max_edge` を送らず原寸 | `bracket.rs: merge_bracket`、`coreimage.rs: PREVIEW_MAX_EDGE` |
| プレビュー合成は長辺 1600（rawpy full-size デモザイク） | 旧 §1・§4 | 長辺 2400（Core Image、`apple_default`・decoder 9・レンズ補正 ON） | 同上 |
| JPEG 保存で GPS は書かない / Rust 側で EXIF を継承するとき GPS・MakerNote は書かない | #83「やること」5、#84「EXIF 継承」 | CLI は EXIF / GPS を書かない（これは正しい）。**Rust の継承でカメラ JPG の GPS IFD はそのまま残す**。MakerNote は破棄。アプリが新たに GPS を書くことはしない | ADR 0003 §6 決定 5、`exif_inherit.rs`（テスト `assert_synthetic_gps_metadata`） |
| フロントはブラウザモードでは従来の HTTP（`POST /api/merge-bracket`） | #84「フロント」 | HTTP は廃止。Tauri の `invoke("merge_bracket")` のみ | ADR 0003、`coreimage-api.ts` |
| キャッシュキーは sha1 先頭 10 文字、rawpy/Pillow/OpenCV/lensfun の版を含む | 旧 §1 キャッシュキー | SHA-256 先頭 10 hex、`MERGE_SCHEMA`・CLI の版とバイナリ SHA-256・ARW の size/mtime・`rust_exif` を含む | `bracket.rs`、§4.1 |
| レンズ補正の理由は lensfun 由来の多数の語彙（`lens_params_inconsistent_across_frames` 等） | 旧 §1 | 2 値 `coreimage_unsupported` / `inconsistent_across_frames` | `bracket.rs`、`tests/fixtures/coreimage_merge_lens_reasons.json` |

### 10.2 アプリ全体（HTTP 廃止・保存先）

| 旧記述 | 現行 |
|---|---|
| `http://127.0.0.1:8788` の `/api/*`、CORS・Origin チェック・64 KiB 上限、エラーは `{error}` + HTTP ステータス | Tauri コマンド 13 本 + `geoframe://` スキーム（§1）。エラーは §0.1 の 3 形。`geoframe://` スキームだけが HTTP 風ステータスを返す |
| `outputs/{developed,preview,merged,merged_preview,thumbnails,analysis,export}/`（リポジトリ内） | `$APPDATA/{coreimage/{full,preview,merged,merged_preview,candidates},thumbnails,analysis,export}/`（§4）。`export` は単数形 |
| 現像 JPEG 名 `{id}-{preset}-{hash10}.jpg` | `{ID}_{hash16}.jpg`（`coreimage/full/` と `coreimage/preview/`） |
| サムネイル `{root_key}-{id}.webp` を `/thumbs/` で配信、`PhotoInfo.thumb` は URL | `{root_key}-{ID}.jpg`、`thumb` は絶対パス（asset プロトコルで表示） |
| 書き出しの `developed_filename` は `raw-develop` の `{id}-{preset}-{hash10}.jpg`、検証は `outputs/developed/` | `variant` の 3 値（`camera_jpg` / `developed` / `merged`）は不変。`developed_filename` は `coreimage/full/` の `{ID}_{hash16}.jpg`、検証は来歴 JSON の `mode == "full"` |
| プリセットは `GET /api/health` から取得、legacy rawpy 値とも二重管理 | `get_presets`（Rust）のみ。legacy 値は契約外 |
| 解析は `POST /api/analyze` → `GET /api/analysis` | `start_analysis` → `get_analysis` のポーリング |
| Drive アップロード（`/api/drive-upload`） | アプリには無い（将来 N-F で設計からやり直し） |
| ブラウザ（vite dev）で画面を検証する | Tauri（`tauri:dev` / `.app`）で検証する。vite 単体では動かない |

### 10.3 既知の不整合（コード側の宿題。挙動は上の各節のとおり）

- `engine_info` だけスレッド異常の code が `INTERNAL`（他は `INTERNAL_ERROR`）。
- `SourceFolderError` は `code` と `reason` に同じ値を二重に持つ。
- `get_source_folder` は未設定時にビルド機のパス `<リポジトリ>/sample/100MSDCF` を返すが、解析・現像は `SOURCE_ROOT_NOT_CONFIGURED`。
- 書き出し系 3 コマンドのエラーが `code` を持たない素の文字列。`import_to_photos` は失敗を `Ok` で返す。
- `DevelopedResult.output_url` / `MergedResult.output_url` は Rust が macOS の asset URL 形を直書きしている（他 OS では未検証）。
  `develop_full_coreimage` の URL にはキャッシュバスター `?v=` が無い。
- `src/lib/types.ts` の古い記述: 冒頭の「Python 側との唯一の正」、`PhotoInfo.thumb` の `/thumbs/…webp` 例、書き出し先の `outputs/export/<batch>/`、
  `PhotoInfo.captured_at` / `GroupInfo.start_at` / `end_at` が `string`（実際は null 可）、`PhotoInfo.orientation` が `number | null`（実際は常に数値）。
- `presets.rs: resolve_adjustments` の docコメント「Tauri コマンドからは使っていない」は古い（§9）。
- `generate_development_candidates` の進捗イベントをフロントが listen していない。
