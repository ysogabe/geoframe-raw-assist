# GeoFrame RAW Assist — API 契約（凍結版）

> **凍結済み。変更には全ウェーブ停止が必要。**
> この文書は Wave 0 で凍結された、Python 側と TypeScript 側の**唯一の正**である。
> Wave 1 以降のエージェントは参照のみ行うこと。変更が必要になった場合は、
> 実装を止めて orchestrator に差し戻し、全ウェーブ停止のうえ再凍結する。
>
> 凍結日: 2026-08-10 / 出典: `.claude/plans/vectorized-snacking-falcon-agent-ad862d8a7d0ff47d3.md` §d・§f
> TypeScript 側の写し: `app/lib/types.contract.ts`（同じく凍結）

---

## 0. 基本事項

- ベースURL: `http://127.0.0.1:8788`（`RAW_ASSIST_PORT` で変更可）
- CORS: 現状維持。`Access-Control-Allow-Origin` は `http://localhost:3000` / `http://127.0.0.1:3000` のみ。
- `POST` は `Origin` ヘッダが上記以外の場合 **403**（`{"error": "この画面からの操作は許可されていません。"}`）。
  `GET` に Origin チェックは**無い**（現状維持）。
- リクエストボディ上限: **64 KiB**。超過・0バイト・不正な `Content-Length` は **400**（`"リクエストサイズが不正です。"`）。
- エラーレスポンスは全て `{ "error": string }`（日本語メッセージ）。
- エラーの HTTP マッピング（現状維持）:
  - `RawDevelopmentError` / `ValueError` / `TypeError` / `JSONDecodeError` → **400**、`{"error": str(error)}`
  - それ以外の例外 → **500**、`{"error": "現像処理に失敗しました: <msg>"}`

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
> **Wave 1-C では `types.contract.ts` を使った新規 fetch を書かないこと**（§6 参照）。

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
rawpy/Pillow/engine バージョン, lensfun 適用有無 })[:10]`

- ファイル名に `hash10` として埋め込む。
- **ヒット判定はレシピJSON内の `params_hash` の完全一致**で行う
  （現行の「id+preset のみ」でのキャッシュ判定はバグとして廃止する）。

### `POST /api/export`

```
body { batch_name:string,   // [A-Za-z0-9_-]+ に正規化
       items: ExportItem[] }
ExportItem = { capture_id:string, variant:"camera_jpg"|"developed",
               developed_filename?:string }   // variant="developed" 時必須（raw-develop の filename）
→ { export_dir:string, manifest_path:string, files:string[],
    skipped:{capture_id:string, reason:string}[] }
```

`outputs/export/<batch_name>/` へ `shutil.copy2` + `manifest.json` 生成（マニフェスト仕様はプラン §3）。**元ファイルは非破壊**。

### `POST /api/import-to-photos`

```
body { batch_name:string, album?:string }   // album 既定 "GeoFrame RAW Assist <batch>"
→ { status:"ok", imported:number, album:string }
 | { status:"error", error:string, stderr?:string }
```

`outputs/export/<batch>/` 配下の `*.jpg` のみ対象。`osascript` は**配列形式で呼ぶ**（文字列連結でのスクリプト生成は禁止）。

### 静的ファイル配信

| ルート | 実体 | 状態 |
|---|---|---|
| `GET /outputs/<file>` | `outputs/developed/` | 既存 |
| `GET /preview/<file>` | `outputs/preview/` | 新規（Wave 1-B） |
| `GET /thumbs/<file>` | `outputs/thumbnails/` | 新規（Wave 1-B） |
| `GET /source-jpg/<DSCxxxxx.JPG>` | `sample/100MSDCF/` | 既存 |

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
  source: { root:string, pairs:number, jpg_only:number, arw_only:number, total_bytes:number },
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
  thumb:string }                 // 例 "/thumbs/DSC02703.webp"

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
| レシピJSON | 上記と同一ステム + `.json` |
| サムネイル | `{capture_id}.webp`（長辺640 / quality 80） |
| 解析キャッシュ | `analysis.json` |

`hash10` は §1 の `params_hash`（sha1 先頭10文字）。

> 既存キャッシュ `outputs/developed/{id}-{preset}.jpg`（旧命名、数件）は新命名移行後は**無視する。削除しない**。

### ディレクトリ

```
outputs/
  developed/    本現像JPEG + レシピJSON          → GET /outputs/
  preview/      プレビュー現像(half_size,1600px) → GET /preview/
  thumbnails/   解析用サムネ .webp               → GET /thumbs/
  analysis/     analysis.json                    （HTTP直接配信なし。/api/analysis 経由）
  export/       <batch_name>/ + manifest.json    （HTTP直接配信なし）
```

`outputs/` 全体が `.gitignore` 済み。JSON の書き込みは全て **tmp + `os.replace`** のアトミック方式
（既存 `_save_recipe` と同方式）。

---

## 5. プリセット9種（凍結版・プラン §f）

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
| `vivid_travel` | ビビッド | 0.0 | +5 | 10 | 55 | 1.0 | 1.12 | 1.18 | 1.10 | light |

> **Wave 0 時点の実装は旧3種**（`neutral_recovery` / `landscape_clear` / `nightscape`、旧キー体系
> `exp_shift` / `auto_bright_thr` / `contrast` / `color` / `sharpness`）が `local_raw/presets.py` に
> 値を変えずに移設された状態。9種化と `Adjustments` キー体系への移行は **Wave 1-A** の担当。

---

## 6. TypeScript 側の取り決め

- 契約型の写しは **`app/lib/types.contract.ts`**（凍結）。
- `RawPreset` の union 型は **廃止**。プリセットは `string` + `PresetInfo[]` を `/api/health` から動的取得する
  （Python 側とのプリセット二重定義を解消するため）。
- Wave 1-C の機械分割では `app/lib/types.ts` に**既存定義のまま**移設し、`types.contract.ts` と一時併存させる。
  Wave 2-C2 で `types.contract.ts` へ一本化する。
- `app/page.tsx` は Wave 0 時点では `types.contract.ts` を**インポートしない**（挙動不変を保つため）。

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

### `local_raw/engine.py`（所有: Wave 1-A）

- 既存 export 維持: `SOURCE_ROOT` / `OUTPUT_ROOT` / `PROJECT_ROOT` / `CAPTURE_ID_PATTERN` / `RawDevelopmentError` / `develop_raw` / `PRESETS`（構造は新 presets.py 準拠に変わる）
- 追加 export: `PREVIEW_ROOT = PROJECT_ROOT / "outputs" / "preview"`
- `develop_raw(capture_id: str, preset_name: str, *, adjustments: dict | None = None, preview: bool = False, force: bool = False, max_edge: int = 6000) -> dict`
  — 戻り値は契約 `DevelopedResult` から `output_url` を除いた全キー。**`output_url` はサーバ側（Wave 1-B）が付与**する: `preview == true` のファイルは `/preview/<filename>`、それ以外は `/outputs/<filename>` として URL を組む（既存の `?v=<mtime_ns>` 方式を踏襲）

### `local_raw/lens.py`（所有: Wave 1-A）

- `lensfun_info() -> dict` — `{"available": bool, "db_version": "0.3.4.0"?}`（`/api/health` の `lensfun` キーへそのまま入る形。db_version 不明時はキー省略）

### `local_raw/analysis.py`（所有: Wave 1-B）

- `THUMBS_ROOT = PROJECT_ROOT / "outputs" / "thumbnails"`（`/thumbs/` 配信元）
- 分析ジョブ管理（開始/状態取得）の内部 API は 1-B が自由に設計してよい（server.py も 1-B 所有のため外部契約は §1 の HTTP 契約のみ）

### 並列作業の約束事

- Wave 1-A は **local_raw/server.py を起動も編集もしない**（ポート 8788 は 1-B の検証専用）
- 作業中のテストは自分の所有ファイルを対象に module 指定で実行する（例: `.venv/bin/python -m unittest tests.test_analysis`）。`npm run test:raw` のフルスイートは他エージェントの編集途中ファイルを拾いうるため、失敗が自分の所有外なら無視し、最終判定は orchestrator が行う
