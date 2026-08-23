# GeoFrame RAW Assist — API 契約（凍結版）

> **凍結済み。変更には全ウェーブ停止が必要。**
> この文書は Wave 0 で凍結された、Python 側と TypeScript 側の**唯一の正**である。
> Wave 1 以降のエージェントは参照のみ行うこと。変更が必要になった場合は、
> 実装を止めて orchestrator に差し戻し、全ウェーブ停止のうえ再凍結する。
>
> 凍結日: 2026-08-10 / 出典: `.claude/plans/vectorized-snacking-falcon-agent-ad862d8a7d0ff47d3.md` §d・§f
> TypeScript 側の写し: `app/lib/types.ts`（同じく凍結）
>
> **追補 2026-08-18（現像元フォルダの選択・ユーザー承認済み）**:
> `GET/POST /api/source-folder` を §1 に追加、サムネイル命名を §4 で変更、
> `params_hash` に現像元フォルダのパスを追加（§1）、`SOURCE_ROOT` 定数を
> `config.source_root()` の呼び出し時解決へ置換（§9）。
> `Analysis.source.root` の整形を `SourceFolderInfo.display_path` と共通化（§3）。
>
> **追補 2026-08-20（ブラケット露出合成・ユーザー承認済み）**:
> `POST /api/merge-bracket` と `GET /merged/` を §1 に追加、`outputs/merged/` と
> 合成JPEGの命名を §4 に追加、`POST /api/export` の `variant` に `"merged"` を追加（§1）。
> 合成方式は Exposure Fusion（Mertens）で、HDR + トーンマッピングは採用しない。
>
> **追補 2026-08-20b（プレビュー合成の分離・セキュリティレビュー対応）**:
> プレビュー合成の出力先を `outputs/merged_preview/` に分離して `GET /merged-preview/` を
> §1 に追加、§4 のディレクトリ規約に `outputs/merged_preview/` を追加、
> `POST /api/export` の `variant:"merged"` を**本合成限定**と明記（§1）。

---

## 0. 基本事項

- ベースURL: `http://127.0.0.1:8788`（`RAW_ASSIST_PORT` で変更可）
- CORS: `Access-Control-Allow-Origin` は `http://localhost:1420` / `http://127.0.0.1:1420`（Vite開発サーバ）と `tauri://localhost`（配布用ビルドがフロントエンドを読み込む本番origin。実機で `tauri build` した `.app` から実測して確認）のみ。値の意味・チェック方式は現状維持。
- `POST` は `Origin` ヘッダが上記以外の場合 **403**（`{"error": "この画面からの操作は許可されていません。"}`）。
  `GET` に Origin チェックは**無い**（現状維持）。
- リクエストボディ上限: **64 KiB**。超過・0バイト・不正な `Content-Length` は **400**（`"リクエストサイズが不正です。"`）。
- エラーレスポンスは全て `{ "error": string }`（日本語メッセージ）。
- エラーの HTTP マッピング（現状維持）:
  - `RawDevelopmentError` / `ValueError` / `TypeError` / `OverflowError` / `JSONDecodeError` → **400**、`{"error": str(error)}`
  - それ以外の例外 → **500**、`{"error": "<エンドポイントごとの定型文>"}`（例: `"現像処理に失敗しました。"`）。
    例外詳細（内部パスを含みうる）はレスポンスに載せず**サーバログにのみ**出す
    （Wave 2-D のセキュリティ堅牢化で再凍結。旧仕様の `": <msg>"` 付き形式は廃止）

---

## 1. エンドポイント

> ### ⚠ 実装状況（Wave 0 終了時点）— 契約は「これから実装する姿」である
>
> **本章の契約は目標形であり、Wave 0 時点のサーバはまだこの形を返さない。**
> 特に Wave 1-B（サーバ）と Wave 1-C（フロント機械分割）は**並列で走る**ため、
> Wave 1-C が本契約どおりのレスポンスを期待して実装すると実行時に落ちる。注意すること。
>
> | エンドポイント | Wave 0 時点 | 契約形の実装担当 |
> |---|---|---|
> | `GET /api/health` | **契約前の形**を返す: `{status, engine, rawpy_version, presets:[{name,label}]}`。<br>**`lensfun` キーは無い**。`presets[]` に `description` / `adjustments` は**無い**。 | Wave 1-B |
> | `POST /api/raw-develop` | 動作するが**旧契約**: `adjustments` / `preview` / `params_hash` / `lens_correction` / `exif_inherited` は未対応。`filename` は旧命名 `{id}-{preset}.jpg`。 | Wave 1-A + 1-B |
> | `GET /api/analysis`・`POST /api/analyze` | **未実装**（404） | Wave 1-B |
> | `GET /preview/`・`GET /thumbs/` | **未実装**（404） | Wave 1-B |
> | `POST /api/export`・`POST /api/import-to-photos` | **未実装**（404） | Wave 2-D |
> | `GET /outputs/`・`GET /source-jpg/` | 契約どおり動作（既存） | — |
>
> Wave 1-C は「挙動不変の機械分割」であり API 駆動化は Wave 2-C2 の担当なので、
> **Wave 1-C では `app/lib/types.ts` の契約型を使った新規 fetch を書かないこと**（§6 参照）。

### `GET /api/health` → `HealthResponse`

```
{ status:"ready", engine:"rawpy/LibRaw", rawpy_version:string,
  lensfun:{available:boolean, db_version?:string},
  presets: PresetInfo[] }

PresetInfo = { name:string, label:string, description:string,
               adjustments: Adjustments }   // プリセット既定値 = UIスライダー初期値
```

- `lensfun.db_version` は **optional**。スパイク1の結果、lensfun にはDBスナップショット自体の
  バージョンを返すAPIが無いため、`lensfunpy.lensfun_version`（タプル `(0, 3, 4, 0)`）を
  `"0.3.4.0"` の形に整形して入れる。取得できない場合はキーごと省略してよい。
- `lensfun.available` は `import lensfunpy` の成否。**false でも現像は正常動作する**（§4 参照）。

### `GET /api/analysis`

```
→ { status:"absent" }
 | { status:"running", progress:{ stage:string, done:number, total:number } }
 | { status:"ready", analysis: Analysis }
```

### `POST /api/analyze`

```
body { force?:boolean } → { status:"started"|"already_running"|"ready" }
```

### `GET /api/source-folder` → `SourceFolderInfo`

### `POST /api/source-folder` → `SourceFolderInfo`

```
body { path:string }        // 絶対パス。保存に成功したときのみ 200
→ SourceFolderInfo

SourceFolderInfo = {
  path:string,              // 解決済みの絶対パス
  display_path:string,      // 表示用。プロジェクト配下→プロジェクト相対 /
                            // ホーム配下→"~/..." / それ以外→絶対パス
  is_default:boolean,       // 既定（sample/100MSDCF）かどうか
  exists:boolean,
  readable:boolean,         // 実際に1エントリ読めたか（0組との区別に必須）
  pairs:number,             // JPG/ARW 両方そろった組数
  jpg_only:number,
  arw_only:number,
  total_bytes:number
}
```

- 件数は `analysis.scan_pairs()` の結果をそのまま使う（重複実装しない）。
- 対象は **JPEG+RAW ペアのみ**（`DSC#####.JPG` と `DSC#####.ARW` の同名ペア）。
  RAW 単独フォルダ・他機種は対象外。`CAPTURE_ID_PATTERN` と `/source-jpg/` の
  12文字検証は現状維持。
- `GET` は設定が無い / 壊れている / 保存先がもう存在しない場合、既定フォルダの情報を返す（エラーにしない）。
- 読み取れて **0 組**の場合は**エラーにせず保存**し、`pairs: 0` を返す
  （ユーザーが後からファイルを置くことがあるため）。

POST のエラーは全て **400** で `{ "error": string, "reason": string }`:

| reason | 条件 | 保存 |
|---|---|---|
| `invalid` | `path` が文字列でない / 空 / 絶対パスでない / パスとして不正（NUL・長さ超過等） | しない |
| `not_found` | 存在しない | しない |
| `not_directory` | ディレクトリでない | しない |
| `permission_denied` | 読み取り不可（`os.scandir` が `PermissionError`） | **しない** |
| `analysis_running` | 解析ジョブ実行中 | **しない** |

- `permission_denied` を「0組」と混同させないこと。macOS は `~/Pictures` / `~/Desktop` /
  外部ボリュームへの初回アクセスで許可を求め、拒否されると `PermissionError` になる
  （`Path.glob` はこれを握り潰すので、`readable` の判定には `os.scandir` を使う）。
  メッセージはシステム設定での許可を案内する。
- §0 の共通 400（64KiB 超過・不正 JSON 等）もこのエンドポイントでは
  `reason: "invalid"` を付けて形を揃える。
- **フォルダを切り替えると解析キャッシュは自動的に無効**になる（署名にルートパスを含むため）。
  切り替え直後の `GET /api/analysis` は `{"status":"absent"}` を返すので、
  フロントは `POST /api/analyze` で再解析を促すこと。現像キャッシュとサムネイルは
  フォルダごとにキーが分かれるので、元のフォルダに戻せばそのまま再利用される。

### `POST /api/raw-develop`

```
body { capture_id:string, preset:string, adjustments?:Partial<Adjustments>,
       preview?:boolean, force?:boolean, max_edge?:number }
→ DevelopedResult
```

```
DevelopedResult = {
  capture_id, preset, preset_label,
  filename:string,          // {id}-{preset}-{hash10}.jpg
  recipe_filename:string,
  params_hash:string,
  adjustments:Adjustments,  // 解決後の全量（Partial ではない）
  width, height, elapsed_seconds, cached:boolean, preview:boolean,
  lens_correction:{applied:boolean, reason?:string, lens?:string},
  exif_inherited:boolean,
  output_url:string
}
```

- `preview:true` → rawpy `half_size=True` + `max_edge=1600`、出力先 `outputs/preview/`（2〜5秒）。
- 本現像は `outputs/developed/`（10〜20秒）。
- `max_edge` の許容範囲は **1200〜6048**（範囲外は 400 `"最大辺は1200〜6048pxで指定してください。"`）。
- **`output_url` にはキャッシュバスター `?v=<st_mtime_ns>` が付く**（現行実装の挙動を維持）。
  例: `http://127.0.0.1:8788/outputs/DSC02943-nightscape-a1b2c3d4e5.jpg?v=1786088071771443048`
  フロント（Wave 2-C2）は `output_url` を**そのまま `<img src>` に使うこと**。パスとして再構成しない。

#### キャッシュキー（既存バグの解消）

`params_hash` = `sha1(canonical JSON of { 解決後 adjustments, preset名, max_edge,
rawpy/Pillow/engine バージョン, lensfun 適用有無, **現像元フォルダの解決済み絶対パス** })[:10]`

- **現像元フォルダのパスを必ず含める**（2026-08-18 追補）。`capture_id` はカメラの連番なので
  カードが違えば別の写真に同じ ID が付く。含めないと、別フォルダの同名 ID を同じプリセットで
  現像したときに**他人の写真の現像結果が `cached: true` で返る**。

- ファイル名に `hash10` として埋め込む。
- **ヒット判定はレシピJSON内の `params_hash` の完全一致**で行う
  （現行の「id+preset のみ」でのキャッシュ判定はバグとして廃止する）。

### `POST /api/merge-bracket`

```
body { capture_ids:string[], preset:string, adjustments?:Partial<Adjustments>,
       preview?:boolean, force?:boolean, max_edge?:number }
→ MergedResult
```

```
MergedResult = {
  capture_ids:string[],     // 昇順に正規化して返す
  frames:number,
  preset, preset_label,
  filename:string,          // {先頭id}-merged{枚数}-{preset}-{hash10}.jpg
  recipe_filename:string,
  params_hash:string,
  adjustments:Adjustments,  // 解決後の全量（Partial ではない）
  width, height, elapsed_seconds, cached:boolean, preview:boolean,
  aligned:boolean,          // 位置合わせ(AlignMTB)が成立したか
  lens_correction:{applied:boolean, reason?:string, lens?:string},
  exif_inherited:boolean,
  output_url:string         // 本合成 /merged/<file>?v=<st_mtime_ns>
                            // プレビュー /merged-preview/<file>?v=<st_mtime_ns>
}
```

露出を変えて撮ったブラケット（解析の `exposure_bracket`）を **Exposure Fusion（Mertens）**
で1枚に合成する。HDR 合成 + トーンマッピングは採用しない。

- `preview:true` → rawpy `half_size=True` + `max_edge=1600`、出力先 `outputs/merged_preview/`。
  本合成は `outputs/merged/`（2026-08-20b 追補）。**プレビューと本合成はディレクトリで分ける**:
  `params_hash` が違うのでファイル名は衝突しないが、同居しているとサーバ側で「原寸か
  プレビューか」を判定できず、`POST /api/export` の `variant:"merged"` に長辺1600pxの合成を
  マスターとして書き出せてしまう（現像側の `outputs/developed/` と `outputs/preview/` と同じ形）。
- `output_url` はサーバ側が `preview` の値でルートを切り替えて付与する
  （`preview:true` は `/merged-preview/<filename>`、それ以外は `/merged/<filename>`。
  いずれもキャッシュバスター `?v=<st_mtime_ns>` 付き）。フロントは `output_url` を
  **そのまま `<img src>` に使うこと**。パスとして再構成しない。

- **フレーム数は 2〜9 枚**。範囲外は 400（`"合成できるのは2〜9枚です。"`）。
  上限があるのは「1グループ＝1シーン」を保証できないため（実データに1グループ20枚＝
  別シーンの AEB 4セット分が存在する）。**どのフレームを混ぜるかの選択はフロントの責務**で、
  エンジンは受け取ったものを合成する。
- `capture_ids` は現像元フォルダ直下に限定し（`resolve()` 後の親一致）、重複は 400。
  戻り値・ファイル名・EXIF 継承元・位置合わせの基準は**昇順に正規化した順**に揃える。
- 処理順序（固定）: rawpy(**`no_auto_bright=True`**) → lensfun補正 → AlignMTB →
  MergeMertens → クリップ → resize → shadow_lift LUT → Brightness → Contrast →
  Color → Sharpness → save(exif, icc)
- **`no_auto_bright=True` が要**。`develop_raw` 側の自動明るさ補正はフレームごとに露出を
  正規化するため、そのまま流用すると**ブラケットの露出差が消えて合成が無意味になる**
  （実測: 同ブラケット5枚を `develop_raw` すると平均輝度が 46.7〜49.4 に揃ってしまう）。
- `Adjustments` のうち **`exposure` と `highlight_recovery` は使わない**（どちらも rawpy 段で
  フレームごとの露出を動かすパラメータであり、合成の前提を壊すため）。`wb_temp` /
  `noise_reduction` は全フレームに同一に効くので反映する。トーン調整は**合成結果に対して
  1回だけ**適用する。
- EXIF は**先頭フレームのペアJPGから継承**（`Orientation` は 1 に正規化）。
- `aligned` は結果であって**キャッシュキーには入らない**（合成を回すまで判らず、
  ファイル名の決定に間に合わないため）。`params_hash` には位置合わせの**版**が入る。

#### キャッシュキー

`params_hash` = `sha1(canonical JSON of { **ソート済み capture_ids**, 解決後 adjustments,
preset名, max_edge, preview, lensfun 適用可否, 位置合わせの版,
rawpy/Pillow/OpenCV/bracket/engine バージョン, **現像元フォルダの解決済み絶対パス** })[:10]`

- `capture_ids` と現像元フォルダのパスを**必ず含める**。`capture_id` はカメラの連番なので、
  含めないと別フォルダの同じIDに対して**他人の写真の合成結果が `cached: true` で返る**。
- **`ENGINE_VERSION` も含める**（2026-08-21 追補）。合成はレンズ補正・トーン処理・JPEG保存を
  §2 の現像パイプラインと共有しているので、そちらの出力が変わる修正で `ENGINE_VERSION` を
  上げても、含めないと**合成側のキャッシュだけが無効化されず古い結果を返し続ける**。
- ヒット判定はレシピJSON内の `params_hash` の完全一致（現像と同じ）。

### `POST /api/export`

```
body { batch_name:string,   // [A-Za-z0-9_-]+ に正規化
       items: ExportItem[] }
ExportItem = { capture_id:string, variant:"camera_jpg"|"developed"|"merged",
               developed_filename?:string,   // variant="developed" 時必須（raw-develop の filename）
               merged_filename?:string }     // variant="merged" 時必須（merge-bracket の filename）
→ { export_dir:string, manifest_path:string, files:string[],
    skipped:{capture_id:string, reason:string}[] }
```

`outputs/export/<batch_name>/` へ `shutil.copy2` + `manifest.json` 生成（マニフェスト仕様はプラン §3）。**元ファイルは非破壊**。

`variant:"merged"` は `outputs/merged/` 配下を対象とし、`"developed"` と同じ構造で扱う
（`resolve()` 後の親一致で確認・レシピJSONを manifest に取り込む）。`merged_filename` は
`{capture_id}-merged{枚数}-` で始まること（中置が無い現像済みファイル名は 400）。

`variant:"merged"` は**本合成限定**（2026-08-20b 追補）。プレビュー合成は
`outputs/merged_preview/` にあり、ここでは参照しないので構造的に書き出せない。
加えて二段目の防御として、**取り込むレシピJSON が `preview:true`（`output.preview`）の場合は
400** とする（`"プレビュー品質の合成は書き出せません。本合成をやり直してください。"`）。
「ファイルが単に無い」は `skipped[]`、「入力の取り違え・検証違反」は 400 という
既存の切り分けに従う（`variant` の取り違えを 400 にしているのと同じ扱い）。

この二段目の防御は `variant:"developed"` にも同じく効く（2026-08-21 追補）。現像レシピにも
`output.preview` が書かれるので（§2 / `engine.develop_raw`）、プレビュー現像が
`outputs/developed/` に紛れ込んだ場合に無警告でマスターとして書き出されないようにする。
文言は合成側と共用（`camera_jpg` はレシピを持たないので対象外）。

**成果物の出所を検証する**（2026-08-21 追補）。`outputs/developed/` と `outputs/merged/` は
現像元フォルダに依らない固定ディレクトリなので、フォルダ A で作った成果物は B へ
切り替えた後もそこに残っている。`capture_id` はカメラの連番でカードをまたぐと衝突するため、
取り込むレシピJSONに記録された出所（現像 `source.path` / 合成 `source.root`）が
**現在の現像元フォルダと一致しない場合は `skipped[]`** に積む（実体は A なのに manifest の
`exif` / `group` だけ B、という自己矛盾を防ぐ。解析キャッシュ側は署名照合で同じ取り違えを
防いでいる）。理由の文言は「成果物が見つからない」と同じにする。レシピに出所が
**記録されていない**場合（`camera_jpg`・`source` を持たない古い形式）は判定材料が無いので
従来どおり書き出す。

### `POST /api/import-to-photos`

```
body { batch_name:string, album?:string }   // album 既定 "GeoFrame RAW Assist <batch>"
→ { status:"ok", imported:number, album:string }
 | { status:"error", error:string, stderr?:string }
```

`outputs/export/<batch>/` 配下の `*.jpg` のみ対象。`osascript` は**配列形式で呼ぶ**（文字列連結でのスクリプト生成は禁止）。

### `POST /api/drive-upload`

```
body { batch_name:string }
→ { status:"started"|"already_running" }
```

`outputs/export/<batch_name>/` 直下のファイルを Google Drive の「GeoFrame」フォルダ
**直下**へアップロードする（バッチごとのサブフォルダは作らない。消費側の GeoFrame 本体
アプリが「GeoFrame」フォルダを**非再帰**で走査するため、サブフォルダへ入れると認識されない）。
Drive 上のファイル名には `<batch_name>-` プレフィックスを付ける
（例 `26_08_hiuchi-DSC04431.JPG`）。平置きではバッチ間で `DSC04431.JPG` や
`manifest.json` が衝突するため。`batch_name` は正規化済みの名前。
OAuth（デスクトップアプリ、`local_raw/drive_client_secret.json`）経由。
バックグラウンドジョブとして実行し、進捗は `GET /api/drive-upload` でポーリングする。

Drive 上のファイル名（**プレフィックス込み**）が既に存在する場合は既アップロード済みと
みなしスキップする（上書きしない。部分失敗からの再実行で「まだ無いものだけ」を送る
レジューム動作）。

### `GET /api/drive-upload`

```
→ { status:"idle", configured:boolean }
 | { status:"running", batch_name:string, progress:{ stage:string, done:number, total:number, bytes_done:number, bytes_total:number, skipped:string[] } }
 | { status:"done", batch_name:string, uploaded:number, total:number, skipped:string[], folder_url:string }
 | { status:"error", batch_name:string, error:string, error_kind:string }
```

`configured:false` は `local_raw/drive_client_secret.json` が未配置・不正な場合。
`error_kind` は `auth_revoked` / `quota` / `rate_limit` / `dependencies_missing` /
`network` / `consent_timeout` / `unknown` のいずれか。

`stage` は `authenticating` → （初回・失効時のみ）`waiting_for_consent` →
`resolving_folder` → `uploading` の順に進む。`progress.done` / `bytes_done` は
**スキップ分も含む「処理済み」**（含めないと再実行時に進捗バーが 100% に届かない）。
実際に送った件数は `done` 応答の `uploaded` が持ち、`uploaded + skipped.length == total`。

`skipped` の要素は**ローカルのファイル名**（`<batch_name>-` プレフィックスを**含まない**。
画面にはバッチ内で見慣れた名前を出すため）。スキップ判定に使う Drive 側の名前は
プレフィックス込みなので、両者は一致しない。
`folder_url` は「GeoFrame」フォルダ**自体**への URL（バッチごとのフォルダは無い）。
`done` / `error` はメモリ上のみ保持し、サーバ再起動で `idle` に戻る。

### 静的ファイル配信

| ルート | 実体 | 状態 |
|---|---|---|
| `GET /outputs/<file>` | `outputs/developed/` | 既存 |
| `GET /preview/<file>` | `outputs/preview/` | 新規（Wave 1-B） |
| `GET /merged/<file>` | `outputs/merged/` | 新規（2026-08-20 追補） |
| `GET /merged-preview/<file>` | `outputs/merged_preview/` | 新規（2026-08-20b 追補） |
| `GET /thumbs/<file>` | `outputs/thumbnails/` | 新規（Wave 1-B） |
| `GET /source-jpg/<DSCxxxxx.JPG>` | 現像元フォルダ（`config.source_root()`。既定 `sample/100MSDCF/`） | 既存 |

- いずれも `resolve()` 後に親ディレクトリが所定のルートと一致することを検証する（パストラバーサル防止、現状維持）。
- `/source-jpg/` のファイル名検証（現状維持）: `DSC` 始まり・`.JPG` 終わり・**長さ12文字**・4〜8文字目が数字。
  不一致は **400** `"無効なJPGファイル名です。"`、存在しない場合は **404** `"元JPGが見つかりません。"`。
- 未定義URLは **404** `"未対応のURLです。"`。

---

## 2. `Adjustments`（型と範囲）

```
Adjustments = {
  exposure: number,           // EV -2.0..+2.0 → rawpy exp_shift = clamp(2**ev, 0.25, 8)
  wb_temp: number,            // -100..100。camera_whitebalance の R/B ゲインをスケールし user_wb へ
  shadow_lift: number,        // 0..100。256エントリLUT（下半分中心の持ち上げ）
  highlight_recovery: number, // 0..100 → exp_preserve_highlights 0.4..1.0、>=75 で HighlightMode.Reconstruct
  brightness: number,         // 0.7..1.4  (ImageEnhance.Brightness)
  contrast: number,           // 0.8..1.4
  saturation: number,         // 0.0..1.5  (0 = モノクロ)
  sharpness: number,          // 0.8..1.6
  noise_reduction: "off" | "light" | "full"
}
```

- リクエストの `adjustments` は `Partial<Adjustments>`。未指定キーはプリセット既定値で埋める（`resolve_adjustments`）。
- **数値の範囲外は上記範囲へクランプ、未知キー・不正型・不正な noise_reduction は reject（400）。**（§9 と統一。Wave 1-A 実装済みの挙動を正とする） `DevelopedResult.adjustments` は解決後（クランプ後）の全量を返す。

### 実装マッピング（プラン §f 後半）

- `exposure` → `exp_shift = clamp(2**ev, 0.25, 8)`
- `wb_temp` → `raw.camera_whitebalance` を基準に R×(1+0.003·t)、B×(1−0.003·t) で `user_wb`
  （t=0 のときは従来どおり `use_camera_wb=True`）
- `highlight_recovery` → `exp_preserve_highlights = 0.4 + 0.006·hr`、hr≥75 で `HighlightMode.Reconstruct`
- `shadow_lift` → 256エントリLUT `y = x + (lift/100)·0.5·255·(x/255)·(1−x/255)²·4`（係数は Wave 3 で目視調整）
- `saturation == 0` → `convert("L").convert("RGB")`
- **処理順序（固定）**: rawpy → lensfun補正 → shadow_lift LUT → resize → Brightness → Contrast → Color → Sharpness → save(exif, icc)

---

## 3. 分析データ型

```
Analysis = {
  analysis_version: 2, generated_at: string,
  source: { root:string,         // 表示用。SourceFolderInfo.display_path と同じ整形
            pairs:number, jpg_only:number, arw_only:number, total_bytes:number },
  summary: { groups:number, raw_candidates:number, needs_review:number },
  photos: Record<string, PhotoInfo>,
  groups: GroupInfo[]
}

PhotoInfo = { capture_id, captured_at, iso, f_number, shutter, focal_length,
  exposure_bias, lens_model, orientation, width, height,
  sharpness_raw:number,
  sharpness_score:number,        // グループ内正規化 0-100
  highlight_clip_ratio:number, shadow_clip_ratio:number, mean_luma:number,
  overall_score:number,
  thumb:string }                 // 例 "/thumbs/3f9a1c02-DSC02703.webp"（§4 の命名）

GroupInfo = { id:number, kind:"burst"|"bracket"|"similar"|"single",
  title:string,                  // 例 "10:42 連写 3枚"
  start_at:string, end_at:string,
  capture_ids:string[], recommended_capture_id:string, confidence:number,
  recommendation_reasons:{code:string, text:string}[],
  raw:{ level:"none"|"maybe"|"recommended"|"strong", label:string,
        reason_codes:string[], reasons:string[] } }
```

`raw.reason_codes` の語彙: `high_iso` / `night_scene` / `highlight_clipping` / `deep_shadows` / `exposure_bracket`
（日本語文は `raw.reasons[]` に併記）。

---

## 4. ファイル命名・ディレクトリ規約（凍結）

### 命名

| 種別 | 命名 |
|---|---|
| 現像JPEG | `{capture_id}-{preset}-{hash10}.jpg` 例: `DSC02943-nightscape-a1b2c3d4e5.jpg` |
| 合成JPEG | `{先頭capture_id}-merged{枚数}-{preset}-{hash10}.jpg` 例: `DSC04510-merged5-neutral_recovery-a1b2c3d4e5.jpg` |
| レシピJSON | 上記と同一ステム + `.json` |
| サムネイル | `{root_key}-{capture_id}.webp`（長辺640 / quality 80） |
| 解析キャッシュ | `analysis.json` |

`hash10` は §1 の `params_hash`（sha1 先頭10文字）。

`root_key` は**現像元フォルダの解決済み絶対パスの sha1 先頭8文字**（`config.root_key_for()`）。
`capture_id` はカメラの連番でカードをまたぐと衝突するため、接頭辞が無いと別フォルダの解析が
サムネイルを上書きする。**サブディレクトリは掘らずフラットな接頭辞にする**こと:
`server._serve_from_root()` が `resolve()` 後の親ディレクトリ一致でパストラバーサルを弾いており、
ネストするとこの防御を緩める必要が出る。

> 旧命名のサムネイル `outputs/thumbnails/{capture_id}.webp` は**無視する。削除しない**
> （ヒットしなくなるだけ）。

> 既存キャッシュ `outputs/developed/{id}-{preset}.jpg`（旧命名、数件）は新命名移行後は**無視する。削除しない**。

### ディレクトリ

```
outputs/
  developed/    本現像JPEG + レシピJSON          → GET /outputs/
  preview/      プレビュー現像(half_size,1600px) → GET /preview/
  merged/       ブラケット露出合成JPEG + レシピJSON → GET /merged/
  merged_preview/ 合成プレビュー(half_size,1600px)  → GET /merged-preview/
  thumbnails/   解析用サムネ .webp               → GET /thumbs/
  analysis/     analysis.json                    （HTTP直接配信なし。/api/analysis 経由）
  export/       <batch_name>/ + manifest.json    （HTTP直接配信なし）
```

`outputs/` 全体が `.gitignore` 済み。JSON の書き込みは全て **tmp + `os.replace`** のアトミック方式
（既存 `_save_recipe` と同方式）。

`config.json`（現像対象フォルダ等の設定）と Drive の `drive_auth.json`（OAuthトークン）は
`outputs/` には含まれず、OS標準のアプリデータディレクトリ（`local_raw/paths.app_data_dir()`）に
保存される（2026-08-23 追補）。

---

## 5. プリセット11種（凍結版・プラン §f + 2026-08-19 追補）

全プリセットは `Adjustments` の全キーを持つ。rawpy 固定値は AHD / sRGB / 8bit。
数値は初期案であり、**Wave 3 で実画像を見ながら微調整してよい**（キー構成と name は凍結）。

| name | label | exposure | wb_temp | shadow_lift | highlight_recovery | brightness | contrast | saturation | sharpness | noise_reduction |
|---|---|---|---|---|---|---|---|---|---|---|
| `neutral_recovery` | 階調回復 | 0.0 | 0 | 15 | 60 | 1.0 | 1.03 | 1.02 | 1.04 | light |
| `landscape_clear` | 風景クリア | 0.0 | 0 | 10 | 55 | 1.0 | 1.10 | 1.08 | 1.08 | light |
| `nightscape` | 夜景ノイズ低減 | +0.2 | -5 | 25 | 50 | 1.05 | 1.07 | 1.04 | 1.03 | full |
| `sunset_glow` | 夕景・マジックアワー | -0.1 | +25 | 15 | 80 | 1.0 | 1.08 | 1.12 | 1.05 | light |
| `indoor_warm` | 室内・料理 | +0.3 | +10 | 30 | 45 | 1.05 | 1.02 | 1.05 | 1.04 | light |
| `portrait_soft` | 人物・柔らか | +0.1 | +5 | 20 | 55 | 1.02 | 0.96 | 1.03 | 0.95 | light |
| `monochrome_street` | モノクロ | 0.0 | 0 | 10 | 60 | 1.0 | 1.15 | 0.0 | 1.10 | light |
| `highlight_rescue` | 白飛び救済 | -0.5 | 0 | 40 | 100 | 1.05 | 1.0 | 1.0 | 1.02 | light |
| `shadow_rescue` | 黒つぶれ救済 | +0.3 | 0 | 80 | 65 | 1.05 | 0.98 | 1.06 | 1.02 | full |
| `backlit_rescue` | 逆光 | +0.35 | +5 | 80 | 90 | 1.04 | 0.96 | 1.08 | 1.05 | light |
| `vivid_travel` | ビビッド | 0.0 | +5 | 10 | 55 | 1.0 | 1.12 | 1.18 | 1.10 | light |

> **追補 2026-08-19（プリセット2種追加・ユーザー承認済み）**
> 解析は「暗部の黒つぶれ」「白飛び」を推薦理由として出すのに、救済プリセットは
> 白飛び側にしか無かった。`shadow_rescue`（黒つぶれ救済）と `backlit_rescue`（逆光）を追加する。
> 逆光は専用の検出コードを持たず、`highlight_clipping` と `deep_shadows` が
> **同時に立つこと**をシグナルとして扱う（初期プリセット選択の規則は §6 を参照）。
>
> 数値は実写で確認済み（プレビュー現像・SDカードの実データ）:
> - `shadow_rescue` — 黒つぶれ 45.04% → **14.31%**、平均輝度 34.8 → 58.2（白飛びは 1.12% → 1.67%）
> - `backlit_rescue` — 白飛び 1.41% → **0.13%**、黒つぶれ 34.37% → **19.06%**
>   （`highlight_rescue` を同じ写真に当てると平均輝度 23.6・黒つぶれ 42.83% まで沈むため、
>   逆光には別プリセットが要る、という判断の根拠）

> **Wave 0 時点の実装は旧3種**（`neutral_recovery` / `landscape_clear` / `nightscape`、旧キー体系
> `exp_shift` / `auto_bright_thr` / `contrast` / `color` / `sharpness`）が `local_raw/presets.py` に
> 値を変えずに移設された状態。9種化と `Adjustments` キー体系への移行は **Wave 1-A** の担当。

---

## 6. TypeScript 側の取り決め

- 契約型の写しは **`app/lib/types.ts`**（凍結）。
- `RawPreset` の union 型は **廃止**。プリセットは `string` + `PresetInfo[]` を `/api/health` から動的取得する
  （Python 側とのプリセット二重定義を解消するため）。
- Wave 1-C の機械分割では `app/lib/types.ts` に**既存定義のまま**移設し、`types.contract.ts` と一時併存させた。
  Wave 2-C2 で `app/lib/types.ts` へ一本化済み（`types.contract.ts` は削除）。
- `app/page.tsx` は Wave 0 時点では契約型を**インポートしない**（挙動不変を保つため）。

---

## 7. Wave 0 スパイク結果の反映（詳細は `docs/spikes-wave0.md`）

### lensfun DB の扱い（契約に影響する確定事項）

1. **同梱DB（lensfunpy 1.18.0 / lensfun 0.3.4）に Sigma 10-18mm F2.8 DC DN は無い。**
   カメラ `Sony ILCE-6400` と `E 55-210mm f/4.5-6.3 OSS` は同梱DBでヒットする。
2. 追加DBは **`local_raw/lensfun_db/*.xml`** を `lensfunpy.Database(paths=[...])` に渡す方式。
   同梱DBとマージされる（`load_bundled=True` が既定）。
   **`local_raw/lensfun_db/` は Wave 0 時点で存在しない。** Wave 1-A が作成すること。
   **ディレクトリが存在しない場合・空の場合の両方で正常動作する**ように実装する
   （`glob` が空リストなら `lensfunpy.Database()` と等価に扱う）。
3. **EXIF の `LensModel` にはレンズメーカー名が入らない**（実測: `10-18mm F2.8 DC DN | Contemporary 023`）。
   `find_lenses(camera, maker=None, lens=<EXIF値>)` と、`maker=None` で呼ぶこと。
4. `db_version` は `lensfunpy.lensfun_version` タプルを整形した文字列。DBスナップショット固有の版番号は存在しない。
5. lensfun が使えない／レンズが見つからない場合も **現像は必ず成功させる**。
   `lens_correction` に `{applied:false, reason:"lens_not_in_db"|"camera_not_found"|"lensfunpy_unavailable"|"error:<msg>"}` を記録する。

### EXIF 継承（契約に影響する確定事項）

1. **Pillow 単独で実装する。piexif は導入しない**（実機 Sony JPEG での round-trip 検証済み）。
2. `DevelopedResult.exif_inherited` は、`build_inherited_exif()` が成功して
   `image.save(..., exif=...)` に渡せた場合に `true`。
3. `Orientation` は**必ず 1 に正規化**（rawpy 出力は正立のため、元の 8 を継承すると二重回転になる）。
4. `MakerNote`(0x927C) は**意図的に破棄**する。レシピに `"makernote": "dropped"` を記録する。

### 撮影データ実測に基づく注意（Wave 1-B のロジックに影響）

- `SubsecTimeOriginal` は sample 281枚すべてで**存在しない** → 撮影時刻の分解能は **1秒**。
  `PhotoInfo.captured_at` は秒精度になる。グループ化のソートは `DateTimeOriginal` + ファイル名連番で安定化させること。
- `ExposureBiasValue` は **96.4% が非ゼロ**（-0.3 が182枚、+0.3 が89枚、0.0 が10枚）。
  常用の露出補正であってブラケット撮影ではない。`raw.level = "maybe"` の判定条件
  「ExposureBias ≠ 0」および `GroupInfo.kind = "bracket"` の判定は、
  **そのままでは大量誤検出になる**ため Wave 1-B で条件を見直すこと（しきい値は定数として外出しする）。

---

## 8. テスト運用（Wave 0 で変更済み）

- `package.json` の `test:raw` の unittest discover パターンを
  `-p 'test_raw_*.py'` → **`-p 'test_*.py'`** に拡大した。
  Wave 1 以降に追加される `tests/test_exif.py` / `test_lens.py` / `test_analysis.py` / `test_export.py` が
  自動的に収集される。**新規テストファイルは `tests/test_*.py` の命名にすること。**
- `tests/rendered-html.test.mjs` の `281組のペアを解析しました` assert は
  **Wave 2-C2 まで変更しない**（Wave 1-C の機械分割が挙動不変であることの証明に使うため）。

---

## 9. モジュール間インターフェース（orchestrator が Wave 1 dispatch 前に凍結）

Wave 1 並列実装（A: engine 系 / B: analysis+server）間の import 契約。ここに書かれた名前・シグネチャは両者とも変更禁止（変更が必要なら orchestrator へ差し戻し）。

### `local_raw/presets.py`（所有: Wave 1-A）

- `PRESETS: dict[str, dict]` — key = preset name（9種）。各値は `{"label": str, "description": str, "adjustments": {<§2 Adjustments 全キー>}}`
- `preset_infos() -> list[dict]` — 契約 `PresetInfo[]` そのもの（`/api/health` 用。Wave 1-B はこれを呼ぶだけ）
- `resolve_adjustments(preset_name: str, overrides: dict | None = None) -> dict` — プリセット既定値に overrides をマージし **Adjustments 全キー**を返す。未知キー・不正型・未知 preset 名は `RawDevelopmentError`、数値の範囲外は §2 の範囲へクランプ
- `DEFAULT_PRESET = "neutral_recovery"`

### `local_raw/paths.py`（2026-08-23 追補）

アプリの状態ファイル（設定・認証トークン）の保存先ディレクトリ解決と、旧パスからの移行。
他のどのモジュールにも依存しない葉ノード（`config.py` はほぼ全エントリポイントの最上流にあるため）。
外部ライブラリは使わない（標準ライブラリのみ）。

- `APP_IDENTIFIER = "com.geoframe.rawassist"` — `tauri.conf.json` の `identifier` と一致させること
- `app_data_dir() -> Path` — OS標準のアプリデータディレクトリ（macOS: `~/Library/Application Support/<APP_IDENTIFIER>`、Windows: `%APPDATA%/<APP_IDENTIFIER>`、それ以外: `$XDG_DATA_HOME/<APP_IDENTIFIER>`）。ディレクトリ自体は作成しない
- `migrate_legacy_file(old_path, new_path, *, mode=None) -> bool` — `old_path` にだけ実体があれば `new_path` へ一度だけ移す。**例外は一切投げない**。冪等性の条件は「`new_path` が既に存在するか」だけ

> ⚠️ **`ensure_migrated()` は import 時には呼ばない契約。** `config.py` / `drive_upload.py` の `ensure_migrated()` はどちらも、モジュールロード時ではなく実アプリの `__main__` エントリポイント（`server.main()` / `engine.main()` / `bracket.main()`）から明示的に呼ぶ設計になっている。`npm run test:raw`（`unittest discover`）は対象モジュールを `import` するため、import 時に移行を走らせるとテスト実行だけで実データ（現像元フォルダの設定・生のOAuthリフレッシュトークン）が動いてしまうため。
>
> **新しい `__main__` エントリポイントを追加する場合は、対応する `ensure_migrated()`（`config.ensure_migrated()` は必須、Google Drive機能を使うなら `drive_upload.ensure_migrated()` も）を、設定を読む処理より前に呼ぶこと。** 呼び忘れると、そのエントリポイント経由では移行が起きず、`config.source_root()` の「例外を投げない」設計により**静かに** `DEFAULT_SOURCE_ROOT` へフォールバックする（気づきにくい形で壊れる）。
>
> 呼ぶ位置は「意味のある実行だと確定してから」（2026-08-23 追補）。argparse を使う CLI（`engine.main()` / `bracket.main()`）は `parse_args()` の**直後**で呼ぶ。`--help` や引数エラーは `SystemExit` で抜けるため、その前に置くと**起動しない実行でも不可逆なファイル移動が走る**。argparse を持たない `server.main()` は `main()` の先頭でよい。あわせて、移行通知は stdout が機械可読な契約を持つ CLI（末尾で `json.dumps` を出す）では **stderr** へ出すこと。

### `local_raw/config.py`（2026-08-18 追補）

現像元フォルダの検証・永続化。保存先は `paths.app_data_dir() / "config.json"`（tmp + `os.replace` のアトミック書き込み。2026-08-23 追補: 旧保存先は `outputs/config.json`）。

- `DEFAULT_SOURCE_ROOT = PROJECT_ROOT / "sample" / "100MSDCF"`
- `source_root() -> Path` — 現在の設定を解決して返す。設定が無い / 壊れている / 保存先がもう
  存在しない場合は `DEFAULT_SOURCE_ROOT` にフォールバックし、**例外は投げない**。
  「読めないだけ」のフォルダはフォールバックしない（UI が許可を案内できるようにするため）
- `set_source_root(path, *, analysis_running: bool = False) -> Path` — 検証して保存。
  失敗は `SourceFolderError`（`ValueError` 派生・`reason` 属性つき）で、**保存しない**
- `root_key_for(path) -> str` / `source_root_key() -> str` — sha1 先頭8文字（§4 の `root_key`）。
  内部で必ず `resolve()` し、`params_hash` に混ぜるパス文字列と正規化を共有する
- `display_path(path) -> str` — §1 の `display_path`
- `source_folder_info(root=None) -> dict` — §1 の `SourceFolderInfo`（件数は `scan_pairs()` 由来）
- `ensure_migrated() -> bool`（2026-08-23 追補） — 旧パス `outputs/config.json` からの一度きりの移行。呼び出しは実アプリの起動経路（`main()`）からのみ、モジュールロード時には呼ばない

> ⚠️ **どのモジュールでも `SOURCE_ROOT = config.source_root()` のようにモジュールレベルで
> 束縛しないこと。** import 時に固定され、フォルダ切り替えが半分しか効かなくなる。
> 参照はすべてリクエスト処理時の関数呼び出しで解決する。

### `local_raw/engine.py`（所有: Wave 1-A）

- 既存 export 維持: `OUTPUT_ROOT` / `PROJECT_ROOT` / `CAPTURE_ID_PATTERN` / `RawDevelopmentError` / `develop_raw` / `PRESETS`（構造は新 presets.py 準拠に変わる）
- **`SOURCE_ROOT` 定数は廃止（2026-08-18 追補）**。現像元フォルダは `config.source_root()` で
  **呼び出し時に**解決する。`develop_raw()` は 1 リクエストにつき 1 回だけ解決し、パス組み立て・
  トラバーサル検査・`params_hash` で同じ値を使う。レシピの `source.path` は
  プロジェクト外フォルダでも壊れないよう相対化に失敗したら絶対パスのまま入る
- 追加 export: `PREVIEW_ROOT = PROJECT_ROOT / "outputs" / "preview"`
- `develop_raw(capture_id: str, preset_name: str, *, adjustments: dict | None = None, preview: bool = False, force: bool = False, max_edge: int = 6000) -> dict`
  — 戻り値は契約 `DevelopedResult` から `output_url` を除いた全キー。**`output_url` はサーバ側（Wave 1-B）が付与**する: `preview == true` のファイルは `/preview/<filename>`、それ以外は `/outputs/<filename>` として URL を組む（既存の `?v=<mtime_ns>` 方式を踏襲）

### `local_raw/bracket.py`（2026-08-20 追補）

ブラケット露出合成（Exposure Fusion / Mertens）。`server.py` は呼び出し時に import する。

- `MERGED_ROOT = PROJECT_ROOT / "outputs" / "merged"`（`/merged/` 配信元。**本合成のみ**）
- `MERGED_PREVIEW_ROOT = PROJECT_ROOT / "outputs" / "merged_preview"`
  （`/merged-preview/` 配信元。2026-08-20b 追補）
- `MIN_FRAMES = 2` / `MAX_FRAMES = 9`
- `merge_bracket(capture_ids: list[str], preset_name: str, *, adjustments: dict | None = None, preview: bool = False, force: bool = False, max_edge: int = 6000) -> dict`
  — 戻り値は契約 `MergedResult` から `output_url` を除いた全キー。**`output_url` はサーバ側が付与**する: `preview == true` のファイルは `/merged-preview/<filename>`、それ以外は `/merged/<filename>`（いずれも `?v=<mtime_ns>` 付き。`develop_raw` の `/preview/` と `/outputs/` の切り替えと同じ形）
- 保存先も `preview` で分ける（`preview == true` → `MERGED_PREVIEW_ROOT`）。同居させると
  サーバ側で原寸とプレビューを区別できず、`POST /api/export` にプレビューをマスターとして
  書き出せてしまう
- 合成用のデコードは `engine._postprocess_params()` を**流用しない**。露出差を保つ
  `_merge_postprocess_params()`（`no_auto_bright=True`）を使う。LUT とトーンは
  `engine._shadow_lift_table()` / `engine._apply_tone()` を再利用する
- `cv2.createAlignMTB().process(src, dst)` は **src と同じリストを渡して in-place で受ける**。
  空リストは何も書かれず、事前確保したリストは**1枚が全面0になる**（実測 OpenCV 5.0.0）

### `local_raw/lens.py`（所有: Wave 1-A）

- `lensfun_info() -> dict` — `{"available": bool, "db_version": "0.3.4.0"?}`（`/api/health` の `lensfun` キーへそのまま入る形。db_version 不明時はキー省略）

### `local_raw/analysis.py`（所有: Wave 1-B）

- `THUMBS_ROOT = PROJECT_ROOT / "outputs" / "thumbnails"`（`/thumbs/` 配信元）
- **`SOURCE_ROOT` 定数は廃止（2026-08-18 追補）**。`scan_pairs()` / `_source_signature()` /
  `read_cached_analysis()` / `build_analysis()` は引数省略時に `config.source_root()` を
  呼び出し時解決する。`_source_signature()` は**解決済みルートパスを署名に含む**
  （含めないと似た構成の別フォルダで解析キャッシュが流用される）
- `is_analysis_running() -> bool` — `POST /api/source-folder` の `analysis_running` 判定用
- 分析ジョブ管理（開始/状態取得）の内部 API は 1-B が自由に設計してよい（server.py も 1-B 所有のため外部契約は §1 の HTTP 契約のみ）

### `local_raw/drive_upload.py`（2026-08-22 追補）

書き出し済みバッチの Google Drive アップロード（OAuth デスクトップアプリ）。
`server.py` はハンドラ関数内で呼び出し時に import する（ただし `server.main()` 自身は
`ensure_migrated()` を呼ぶため起動時に import する。2026-08-23 追補）。

- `CLIENT_SECRET_PATH = PROJECT_ROOT / "local_raw" / "drive_client_secret.json"`（**source control 対象外**）
- `TOKEN_PATH = paths.app_data_dir() / "drive_auth.json"` — リフレッシュトークン（2026-08-23 追補: 旧保存先は `outputs/drive_auth.json`）。
  `os.open(..., O_EXCL, 0o600)` で作成した tmp へ書いてから `os.replace`（`write_text` 後の
  `chmod` だと一瞬 world-readable な窓が空くため）
- `SCOPES = ["https://www.googleapis.com/auth/drive"]` / `DRIVE_FOLDER_NAME = "GeoFrame"`
- `is_drive_configured() -> bool` — §1 の `configured`（client secret が存在し `installed` キーを持つか）
- `start_drive_upload(batch_name: str) -> str` — `"started"` | `"already_running"`。
  同期検証（バッチ名・書き出しフォルダの存在・対象0件・client secret）の失敗は `ValueError`（→400）
- `get_drive_upload_state() -> dict` — §1 の `GET /api/drive-upload` ペイロード
- `DriveUploadError(kind, message)` — `kind` がそのまま `error_kind` になる
- `ensure_migrated() -> bool`（2026-08-23 追補） — 旧パス `outputs/drive_auth.json` からの一度きりの移行（`mode=0o600`）。呼び出しは `server.main()` からのみ、モジュールロード時には呼ばない

> ⚠️ **google 系ライブラリをモジュールのトップレベルで import しないこと。**
> 未インストール環境でも `import local_raw.drive_upload` は成功する必要がある
> （`GET /api/drive-upload` が `configured` を返せるようにするため）。遅延 import は
> `_google_error_types()` / `_credentials_from_payload()` / `_refresh_credentials()` /
> `_build_service()` / `_media_file_upload()` / `_run_oauth_consent()` に閉じ込める。

### 並列作業の約束事

- Wave 1-A は **local_raw/server.py を起動も編集もしない**（ポート 8788 は 1-B の検証専用）
- 作業中のテストは自分の所有ファイルを対象に module 指定で実行する（例: `.venv/bin/python -m unittest tests.test_analysis`）。`npm run test:raw` のフルスイートは他エージェントの編集途中ファイルを拾いうるため、失敗が自分の所有外なら無視し、最終判定は orchestrator が行う
