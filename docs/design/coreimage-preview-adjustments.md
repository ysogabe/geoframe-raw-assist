# #72 プレビューの Core Image 化 + #73 Adjustments の Core Image 対応 — 実装設計書（1 PR）

- 対象: geoframe-raw-assist、ブランチ `feat/72-coreimage-preview-adjustments`（コミット c6c9551 時点のコードを読んで作成）
- 前提: ADR 0002（`docs/adr/0002-coreimage-engine-decision.md`）。基本設定は Core Image `apple_default`・decoder 9・レンズ補正 ON。
  移行はプレビュー → 原寸・書き出しの順。対応 OS は最新 macOS のみ。
- 関連: Issue #72 / #73（#73 のオーナーコメント: 後段は CLI 内、`noise_reduction` の ISO 帯別表と `highlight_recovery` の代替を #73 の成果物に）、
  #80（プリセットの Rust 移行）、#81（プリセット値再調整・highlight_recovery 置き換え）、#82（CLI バッチ）、#88（暗所判定）、#71（逆光評価。PR #91 で実施、ハイライトの条件は「条件付き」）。
- 作成: 2026-10-20（設計のみ。コードは書いていない）。
- 実装後の更新（2026-10-21）: 実装済み。T5 の較正結果（NR full の上限 0.5、§3.7 の閾値、`wb_k0_out_of_range` ガード）を §3.3・§3.7 に反映した。判断の経緯は [比較レポート §16](../coreimage-engine-comparison.md)。


## オーナー判断（2026-10-20 確定）

§9 の 9 項目はすべて設計書の案どおりに決定した。

1. プレビュー = Core Image、本現像・書き出し = rawpy の不一致を移行期間として受け入れる。バッジ・注記で明示し、原寸・書き出しの Core Image 化は後続 Issue で行う。
2. `highlight_recovery` はこの PR では受理するが効かせない（no-op、`adjustments_applied.ignored` と UI 注記）。代替は #81 で決める。
   #71 の評価（比較レポート §13〜§15）で、EDR・boost は白飛びを減らさず、`exposure` を下げて `shadow_lift` で中間調を戻す方法だけが有効だと分かっている。
3〜9. `CoreImageExperiment` と旧フラグの削除、プレビュー長辺 2400px、sharpness のハイブリッド写像（1.0 = `apple_default` 既定）、
   NR の意味づけ、後段の Metal CIKernel 実装と生成物のコミット、キャッシュ上限 600 件 / 1.5 GB と旧試験出力の自動削除、切り戻しフラグ `GEOFRAME_PREVIEW_ENGINE=rawpy`。

## 表記

- 【確認】: コード・ドキュメントを読んで確認した事実（ファイルと関数名を併記）。
- 【推測】: 設計上の見立て・未検証の数値。実装時に検証で確かめる。
- 【決定】: この設計で決めたこと（オーナー判断が要るものは §9 に別掲）。

---

## 0. 要点（先に結論）

| 論点 | 決定 | 一言の理由 |
|---|---|---|
| なぜ #72 と #73 を 1 PR にするか | アプリの「プレビュー現像」は **プリセット + スライダー上書きを渡す**（【確認】`src/App.tsx: runDevelop` が `adjustments: state.overrides` を preview でも送る）。ADR 0002 決定 2 の「プレビューは調整パラメータが不要」はアプリの実態と合わない | Adjustments 無しの CI プレビューはスライダーを動かした瞬間に嘘の画になる。両方揃えないと既定化できない |
| プレビュー経路 | Tauri では `runDevelop("preview")` を Rust コマンド `develop_preview_coreimage` に振り替え、結果を `DevelopedResult` 互換に変換して既存の `previewResults` に入れる。ブラウザモードは従来どおり Python | 表示側（`BeforeAfter` / `FullSizeViewer` / `OperationPanel`）を変えずに済む |
| 後段処理（shadow_lift / brightness / contrast / saturation / 輝度 USM）の置き場所 | **Swift CLI 内の CIFilter 連鎖**（設計書 `coreimage-feature-migration.md` §2.2 の前提どおり）。輝度 USM と トーン式は **Metal の CIKernel** で実装（厳密式）。CLI は `adjustments` 全量を受ける | プリセット / PSO / ブラケット合成（#80〜#87）がこの前提で設計済み。#73 コメントで「変える場合は先に知らせて」とある |
| CLI バージョン | **0.3.0**（リクエストに `adjustments` ブロック、応答にトップレベル `adjustments_applied`（defaults / post / ignored / guards）を追加。`applied` と既定リクエストの挙動は不変） | 契約追加のため minor を上げる。回帰確認（#75）の既定リクエストは変えないので baseline 差分 0 件が完了条件 |
| 原寸・書き出し | **この PR では rawpy のまま**。CLI / Rust は `mode: "full"` を受けられる状態にしておく | ADR の段階方針。原寸には EXIF 継承（CLI は EXIF を書かない契約）と書き出し契約（`outputs/developed/` + レシピ）の設計が別に要る。ただし「プレビュー = CI、書き出し = rawpy」の見え方不一致が生じる（§9 オーナー判断 1） |
| フラグ | `GEOFRAME_COREIMAGE_EXPERIMENT` は廃止。既定で CI プレビュー有効。切り戻し用に **`GEOFRAME_PREVIEW_ENGINE=rawpy`** を用意 | 既定化が #72 の完了条件 |
| 既定 profile | `coreimage.rs` の `PROFILE="neutral"` → `apple_default`、`lens_correction: true` を明示。CLI 自身の既定 `neutral` は変えない | neutral v9 preview は白点が出る組み合わせ（比較レポート §12.3）。CLI 既定を変えると回帰スクリプトとハーネスの前提が崩れる |
| `highlight_recovery` | この PR では **受け取るが無効（no-op）**。`adjustments_applied.ignored` に載せ、UI に注記。代替レバーは #81（#71 の実データ待ち） | #71 のサンプルがマウントされておらず評価できない（比較レポート §13）。§9 オーナー判断 2 |

---

## 1. 現状の把握

### 1.1 プレビュー / 原寸現像の流れ（rawpy）

【確認】

```
DevelopControls（プレビュー現像 / 本現像ボタン）
  → App.tsx: runDevelop(mode)                      engineStatus==="ready" を要求、activePreset(state) と state.overrides を使う
  → raw-api.ts: developRaw(captureId, preset, {adjustments: overrides, preview: mode==="preview", maxEdge: preview ? undefined : 6000})
  → POST /api/raw-develop（local_raw/server.py: handle_raw_develop）
  → local_raw/engine.py: develop_raw(capture_id, preset, adjustments, preview, force, max_edge)
       presets.resolve_adjustments（未知キー拒否・値域クランプ・float 正規化）
       detect_dark_scene（EV100 ≤ 6 で auto_bright 停止・固定倍率 5.0・ビネット補正スキップ）
       _params_hash → キャッシュ判定 → rawpy.postprocess(**_postprocess_params) → apply_lens_correction
       → apply_post_decode_chain（_apply_shadow_lift_array → _resize_array → _apply_tone_array）→ JPEG 保存（EXIF 継承）
  → server が output_url（http://127.0.0.1:8788/preview/<file>?v=<mtime_ns>）を付けて DevelopedResult を返す
  → app-state.ts reducer "develop/ok": preview → state.previewResults[captureId]、full → state.fullResults[captureId]
```

- preview は `half_size=False`（原寸デモザイク）+ `max_edge` 1600（`PREVIEW_MAX_EDGE`）。出力 `outputs/preview/{id}-{preset}-{hash10}.jpg` + 同名 `.json`。本現像は `outputs/developed/`、長辺 6000。
- 表示側の消費者（【確認】）: `OperationPanel.tsx`（`developResult = fullResult ?? previewResult`、`photoGalleryFor` で `fullResults[id] ?? previewResults[id]`）、`BeforeAfter.tsx`（`result.output_url`、`lens_correction`、`exif_inherited`、`params_hash`、9 項目のレシピ表、`elapsed_seconds` / `cached`）、`FullSizeViewer.tsx`（`ViewerSource.afterUrl`、`preview` フラグで「プレビュー品質（長辺1600px）」の固定文言）。
- 書き出し・採用は **`fullResults` だけ**を読む（【確認】`App.tsx` 315〜321 行、624〜626 行、820 行）。プレビュー結果が書き出しに混ざる経路は無い。
- `CoreImageExperiment.tsx`（フラグ時のみ）は `developCoreImage(captureId, "preview")` を呼び、カメラ JPEG / rawpy / CI の 3 面を並べる。state には載せない。

### 1.2 キャッシュ

| | rawpy（Python） | Core Image 試験経路（Rust） |
|---|---|---|
| 置き場所 | `<repo>/outputs/preview/`・`outputs/developed/`（`engine.PREVIEW_ROOT` / `OUTPUT_ROOT`） | `<app_data_dir>/coreimage/<id>_<mode>_<hash16>.jpg` + `.json`（CLI 応答そのまま）【確認】`coreimage.rs: develop` |
| キー | `_params_hash`: sha1(解決後 adjustments, preset, max_edge, preview, lens_applied, source_root, dark_scene_bright_k, engine/rawpy/Pillow/OpenCV 版)[:10] | `cache_key`: sha256(schema=1, request（output 以外。input は canonicalize 済み絶対パス）, CLI `--version` 応答全文（cli_version + macos_version）, ARW size, mtime_ns)[:16] |
| ヒット判定 | レシピ JSON 内 `params_hash` の完全一致 | 画像と来歴 JSON の両方が存在し、来歴から width/height が読めること |
| 無効化 | バージョン定数を上げる（ENGINE_VERSION 等）。削除はしない | 鍵が変われば別名になる。**削除・上限なし**（肥大する） |
| 同時実行 | サーバはシングルスレッド | `DevelopSlot::try_acquire` で 1 本。使用中は即 `BUSY` |
| その他 | `force` で再現像 | 60 秒ウォッチドッグ、古い `.tmp` 掃除（`cleanup_stale_temps`） |

### 1.3 Adjustments の型と rawpy 側での意味

【確認】型は `src/lib/types.ts: Adjustments`、値域は `local_raw/presets.py: ADJUSTMENT_RANGES`（契約 `docs/API_CONTRACT.md` §2）。UI の可動域は `src/lib/format.ts: SLIDER_SPECS`（同じ範囲）。

| キー | 値域 | rawpy 側の実装（`local_raw/engine.py`） | 段 |
|---|---|---|---|
| `exposure` | −2..+2 EV | `bright = 2**ev`（auto_bright 後。暗所は `5.0 × 2**ev` で auto_bright 無し） | デコード |
| `wb_temp` | −100..100 | `camera_whitebalance` の R×(1+0.003v)、B×(1−0.003v) を `user_wb` に | デコード |
| `shadow_lift` | 0..100 | `y = x + (v/100)·2.0·x·(1−x)²`（`SHADOW_LIFT_GAIN/255 = 2.0`）、[0,1] クリップ。**リサイズ前** | 後段 |
| `highlight_recovery` | 0..100 | `HighlightMode`: 0 → Clip、<75 → Blend、≥75 → ReconstructDefault | デコード |
| `brightness` | 0.7..1.4 | `x *= b` | 後段 |
| `contrast` | 0.8..1.4 | `(x − 0.5)·c + 0.5` | 後段 |
| `saturation` | 0.0..1.5 | Rec.709 輝度 Y で `Y + (x − Y)·s`（0 はモノクロ） | 後段 |
| `sharpness` | 0.8..1.6 | 輝度限定 USM。`_capture_sharpen_params(long_edge, s, iso)`: σ 0.6→0.8、基準 gain 0.5→0.7（長辺 2000→4000 で線形補間）、`gain = base × (1 + (s−1)×1.5)` を [0, 1.2] にクランプ、ISO でコアリング（≤400: 0、≥1600: 1/255、不明 0.5/255）と gain 減衰（1600→3200 で ×1.0→×0.5） | 後段（出力サイズで） |
| `noise_reduction` | off / light / full | FBDD: off → Off、light → ISO<800 なら Off それ以外 Light、full → Full + median 1 pass | デコード |

後段の順序（固定）: shadow_lift → resize（INTER_AREA）→ brightness → contrast → saturation → 輝度 USM → [0,1] クリップ → 8bit。

### 1.4 Core Image 経路の現状

【確認】

- CLI `native/coreimage-cli` 0.2.0: stdin JSON 1 件 → `CIRAWFilter` → JPEG（sRGB ICC）/ tiff16（P3 または sRGB）。`settings` は `exposure_ev` / `neutral_temperature` / `neutral_tint` / `lens_correction` / `profile` + 個別上書き 7 キー。`Develop.swift: develop()` の順序は decoderVersion → scaleFactor → `applyProfile` → `applyOverrides` → `outputImage` → `CIContext`（extended linear sRGB、RGBAh）→ 書き出し。後段 CIFilter は無い。
- Rust `src-tauri/src/coreimage.rs`: 固定値 `DECODER_VERSION="9"`、`PROFILE="neutral"`、`PREVIEW_MAX_EDGE=2400`、`FORMAT="jpeg"`、`lens_correction` 未指定（= OFF）。`GEOFRAME_COREIMAGE_EXPERIMENT=1` のときだけ `develop_coreimage` が動く。
- `apple_default` v9 の実効値（比較レポート §10.3、baseline JSON）: ISO 3200 で sharpness 1.0・輝度 NR 0.241、ISO 6400 で sharpness 0.81・輝度 NR 0.357、color NR / detail / contrast / EDR は 0、boost 1.0 / boostShadow 0.9。**`sharpnessAmount` の値域は 0..1**（CLI の検証）。
- 白点ノイズ（§12.3）: decoder 9 で **輝度 NR 0 かつ sharpness 0 かつ scaleFactor < 1** のときだけ出る。どちらか一方が 0 でなければ出ない（sharpness 0.1 または NR 0.24 で消える）。

---

## 2. #72 プレビュー現像の Core Image 化

### 2.1 置き換える対象

| 画面 / 操作 | 現状 | この PR |
|---|---|---|
| 操作パネル「プレビュー現像」（`DevelopControls` → `runDevelop("preview")`） | Python `/api/raw-develop` preview | **Tauri かつ `preview_engine === "coreimage"` なら Rust `develop_preview_coreimage`**。結果を `DevelopedResult` に変換して `develop/ok`（mode preview） |
| 操作パネル「本現像」（`runDevelop("full")`） | Python full | 変更なし（rawpy） |
| 比較欄 `BeforeAfter`、原寸ビューア `FullSizeViewer`、`photoGalleryFor` | `previewResults` を表示 | 変更なし（データが CI 由来になるだけ）。バッジ「Core Image プレビュー」と `highlight_recovery` 注記を追加 |
| 「Core Image 試験現像」3 面比較（`CoreImageExperiment.tsx`） | フラグ時のみ | **削除**（比較はハーネス `scripts/compare_engines.py` に移っている。§9 判断 3） |
| ブラウザモード（`npm run dev:raw`、`isTauri() === false`） | Python | **従来どおり Python**（`coreimage-api.ts` は `isTauri()` false なら一切 invoke しない。現状の方針を維持） |
| ブラケット合成プレビュー・現像候補（PSO） | Python | 変更なし（#83〜#87） |

`runDevelop` の `engineStatus === "ready"` ゲートは **残す**。Tauri でも Python サイドカーがプリセット（`/api/health`）・サムネ・解析・カメラ JPEG 配信を担っており、CI プレビューの入力（プリセットの既定値）もそこから来る。「CI なら Python 不要」と最適化しないこと。

### 2.2 既定設定とリクエスト

Rust が組み立てる CLI リクエスト（§4.1 の契約）:

```json
{
  "input": "<canonicalize 済み ARW 絶対パス>",
  "output": "<app_data_dir>/coreimage/preview/<id>_<hash16>.jpg",
  "mode": "preview",
  "max_edge": 2400,
  "format": "jpeg",
  "decoder_version": "9",
  "settings": { "profile": "apple_default", "lens_correction": true },
  "adjustments": { ...解決後の 9 キー全量... }
}
```

- 【決定】`PROFILE = "apple_default"`、`LENS_CORRECTION = true`、`DECODER_VERSION = "9"`、`PREVIEW_MAX_EDGE = 2400`（比較レポート・baseline と同じ長辺。rawpy の 1600 より大きいが CI は十分速い。§9 判断 4）。
- `jpeg_quality` は CLI 既定 0.92（rawpy の `JPEG_QUALITY = 92` と同等）。
- decoder 9 は必須依存（ADR 決定 5）。`UNSUPPORTED_DECODER_VERSION` が返ったらフォールバック（§2.6）。

### 2.3 キャッシュ

| 項目 | 決定 |
|---|---|
| 置き場所 | `<app_data_dir>/coreimage/preview/`（`~/Library/Application Support/com.geoframe.rawassist/coreimage/preview/`）。既存の rawpy キャッシュ（`<repo>/outputs/`）とは別。将来の原寸は `coreimage/full/`。`tauri.conf.json` の asset scope `$APPDATA/coreimage/**` はサブディレクトリを含むので変更不要【確認】 |
| ファイル名 | `<capture_id>_<hash16>.jpg` と `<capture_id>_<hash16>.json`（来歴 = CLI 応答 JSON 全文。`applied` / `defaults` / `post` を含む） |
| キー（`CACHE_KEY_SCHEMA = 2`） | sha256 の先頭 16 桁。入力: `schema`、`request`（`output` 以外の全キー。`input` の canonical 絶対パス、`mode`、`max_edge`、`format`、`decoder_version`、`settings` 全量、**`adjustments` 全量（正規化後の float / 文字列）**）、CLI `--version` 応答全文（`cli_version` + `macos_version`。OS 更新で `apple_default` の既定値が変わる懸念を鍵に含める）、ARW の `size` と `mtime_ns`。入力パスを含めるので別カード同名 ID の衝突（Python が `source_root` で防いでいる問題）は防げる【確認】`coreimage.rs: cache_key` の docstring |
| Rust 側の写像バージョン | Adjustments → CLI 値の写像は **すべて CLI 側**で行う（§3.3）。Rust は検証・正規化のみ。したがって写像の変更は `cli_version` で鍵に反映され、Rust 側に別の版定数は不要。Rust 側の正規化規則（クランプ・float 化）を変えたら `CACHE_KEY_SCHEMA` を上げる |
| ヒット判定 | 現状と同じ（画像と来歴 JSON の両方があり、来歴から width/height が読める）。`preset` 名は鍵に入れない（同じ adjustments なら同じ画。Python は preset を入れているがファイル名のため） |
| 無効化 | 鍵の変化で自然に別名になる。加えて**上限付き LRU 削除**: 現像成功後に `coreimage/preview/` を走査し、ファイル数 > 600 または合計 > 1.5 GB なら mtime の古い順に `.jpg` + `.json` の組を削除して 80% まで落とす（best effort、失敗は無視）【推測: 2400px JPEG q92 は約 1〜2 MB。数値は §9 判断 8】 |
| 旧出力の掃除 | 試験経路が `coreimage/` 直下に残した `*_preview_*.jpg/json`・`*_full_*.jpg/json` は、新経路の初回実行時に 1 回だけ best effort で削除する（アプリ自身が作ったキャッシュのみ。他のファイルには触れない） |
| `force` | UI に無いので今回は実装しない（`develop_preview_coreimage` に `force?: bool` を予約しておくだけ） |

### 2.4 同時実行

- CLI 子プロセスは **同時 1 本**を維持（RSS 約 300 MB + GPU。比較レポート §4）。
- 【決定】`DevelopSlot` を「即 BUSY」から「**待つ**」に変える: `Mutex<()>` で直列化し、待ち行列の上限 2（`AtomicUsize` で数える）。3 件目以降は `BUSY`。UI は現像中はボタンを無効にしているが、フォールバックや連打で重なりうるため。
- ウォッチドッグ 60 秒は維持（preview は約 1.3 秒、full 約 2.2 秒）。待ち時間はウォッチドッグに含めない。

### 2.5 フラグ

| | 現状 | この PR |
|---|---|---|
| 有効化 | `GEOFRAME_COREIMAGE_EXPERIMENT=1` のときだけ CI | **既定で CI プレビュー**。環境変数は廃止（読まない） |
| 切り戻し | なし | `GEOFRAME_PREVIEW_ENGINE=rawpy` で Python プレビューに戻す（値がちょうど `rawpy` のときだけ。それ以外・未設定は `coreimage`） |
| 問い合わせ | `coreimage_experiment_enabled() -> bool` | `preview_engine() -> "coreimage" \| "rawpy"`（Rust コマンド。フロントは起動時に 1 回だけ読む） |
| 削除するもの | — | `coreimage_experiment_enabled`、`develop_coreimage`、`CoreImageExperiment.tsx`、`useCoreImageExperiment`、関連 CSS、README の「試験機能」節 |

### 2.6 失敗時のフォールバックとエラー表示

フロント `runDevelop("preview")` の流れ:

1. Tauri かつ `preview_engine === "coreimage"` → `develop_preview_coreimage` を呼ぶ。
2. 成功 → `toDevelopedResult` で変換して `develop/ok`。通知文に「Core Image」を含める（例: `DSC04430 のプレビュー現像が完了しました（Core Image・1.3秒）`）。
3. 失敗 → エラーコードで分岐:

| コード | 扱い |
|---|---|
| `SIDECAR_NOT_FOUND` / `SIDECAR_SPAWN_FAILED` / `SIDECAR_TIMEOUT` / `SIDECAR_IO_FAILED` / `SIDECAR_BAD_RESPONSE` / `UNSUPPORTED_RAW` / `UNSUPPORTED_DECODER_VERSION` / `RENDER_FAILED` / `WRITE_FAILED` / `OUTPUT_DIR_FAILED` / `PROVENANCE_WRITE_FAILED` / `APP_DATA_DIR_UNAVAILABLE` / `INTERNAL_ERROR`、および `SOURCE_ROOT_NOT_CONFIGURED` / `CONFIG_UNREADABLE` / `CONFIG_INVALID` / `SOURCE_ROOT_NOT_FOUND`（Python の `source_root()` は設定が無い・壊れている・フォルダが無いと既定フォルダへ逃がすので、rawpy なら現像できる場合がある。PR #93 レビュー）と CLI の `INPUT_NOT_FOUND` / `INPUT_NOT_READABLE` / `OUTPUT_DIR_NOT_FOUND` | **Python へフォールバック**（従来の `developRaw(preview: true)`）。成功したら通知: `Core Image での現像に失敗したため rawpy でプレビューしました（<code>）`（tone: info）。フォールバックも失敗したら `develop/fail` |
| `BUSY` | フォールバックしない（Python でも同じ重さの現像が走るだけ）。`develop/fail` で「別の現像を実行中です」 |
| `INVALID_REQUEST`（CLI）/ `ADJUSTMENTS_INVALID` / `INVALID_CAPTURE_ID`（Rust） | バグ。フォールバックせず `develop/fail` にコードを含めて表示（開発中に気づけるように） |
| `ARW_NOT_FOUND` / `ARW_OUTSIDE_SOURCE_ROOT` | 入力の問題。同じフォルダを見る Python でも同じ結果になるのでフォールバックせず `develop/fail` |

- フォールバック時も `generation`（フォルダ世代）の照合は従来どおり（`develop/ok` の reducer が捨てる）。
- Rust 側は `sidecar_path()` の不在を `SIDECAR_NOT_FOUND` で返すだけでよい（起動時に落とさない）。
- `preview_engine` が `rawpy` のとき、またはブラウザモードでは最初から Python。

### 2.7 `DevelopedResult` への変換（フロント `coreimage-api.ts: toDevelopedResult`）

| `DevelopedResult` | 値 |
|---|---|
| `capture_id` | そのまま |
| `preset` / `preset_label` | フロントが持つ `activePreset(state)` の `name` / `label` |
| `filename` / `recipe_filename` | 出力 / 来歴のファイル名 |
| `params_hash` | Rust の `hash16` の先頭 10 桁（表示用。`BeforeAfter` の「レシピ」バッジ） |
| `adjustments` | Rust が返す正規化後の全量（`result.adjustments`） |
| `width` / `height` | CLI 応答 |
| `elapsed_seconds` | `elapsed_ms.total / 1000`（小数 2 桁） |
| `cached` | `cache_hit` |
| `preview` | `true` |
| `lens_correction` | `{ applied: applied.lens_correction_enabled, lens: source.lens_model, flags: [] }`。**`flags` は空**（CI は歪曲 / 色収差 / 周辺減光を分けて報告できない。`lensFlagLabels` に嘘を出さない）。`reason` は applied が false のときだけ `"coreimage_unsupported"` |
| `exif_inherited` | `false`（CLI は EXIF を書かない契約） |
| `output_url` | `convertFileSrc(output_path)`（asset プロトコル）。ハッシュがファイル名に入るのでキャッシュバスター不要 |
| `engine`（新設・任意） | `"coreimage"`。Python 由来は未設定（= rawpy） |

`types.ts` は「凍結」とあるが、凍結後の追加と同じ形で **任意フィールド `engine?: "rawpy" | "coreimage"`** を足す（契約変更として `docs/API_CONTRACT.md` に追記。§5 タスク T5）。

### 2.8 表示の調整

- `BeforeAfter`: `result.engine === "coreimage"` のとき見出し横に `<span class="preview-tag">Core Image プレビュー</span>`、レシピ表の下に注記「ハイライト回復は Core Image プレビューでは適用されません（#81 で対応予定）」。`exif_inherited` バッジは false 表示でよい（事実）。
- `FullSizeViewer`: 固定文言「プレビュー品質（長辺1600px）」を結果の寸法から生成する（`ViewerSource` に `previewEdge?: number` を追加し、`OperationPanel.photoGalleryFor` / `viewerGalleryFor` で `Math.max(width, height)` を渡す）。
- `DevelopControls` のエンジンバッジ（`engineVersion = "rawpy x.y"`）はそのまま（Python の状態表示）。プレビュー側のエンジンは `BeforeAfter` のバッジで示す。

---

## 3. #73 Adjustments の Core Image 対応

### 3.1 対応表（この PR / 後続 / 廃止候補）

| Adjustments | この PR | 写像先 | 段 | 備考・後続 |
|---|---|---|---|---|
| `exposure` | **対応** | `CIRAWFilter.exposure`（EV、1:1） | デコード | 基準が rawpy（auto_bright 後）と違うため同じ数値で同じ明るさにはならない【確認: 対応表】。暗所（#50）の固定倍率経路は CI に無い。プリセット値の再調整は #81、暗所判定は #88 |
| `wb_temp` | **対応（近似）** | カメラ WB の K を mired シフト → `neutralTemperature`。tint は不変 | デコード | 係数は【推測】、ハーネスで較正（§3.7） |
| `shadow_lift` | **対応（厳密式）** | 後段 Metal カーネル（sRGB ガンマ空間） | 後段 | rawpy は resize 前、CI は scaleFactor（デコーダ内縮小）の後に掛かる。許容する順序差（`bracket.py` も resize → lift）。§3.7 のオラクル比較で差を確認 |
| `highlight_recovery` | **受け取るが無効** | なし | — | `applied.ignored: ["highlight_recovery"]`。代替レバー（EDR / boost / `CIHighlightShadowAdjust` / 廃止）は **#81**（#71 の実データ待ち）。§9 判断 2 |
| `brightness` | **対応（厳密式）** | 後段カーネル `x·b` | 後段 | |
| `contrast` | **対応（厳密式）** | 後段カーネル `(x−0.5)·c+0.5` | 後段 | |
| `saturation` | **対応（厳密式）** | 後段カーネル Rec.709 輝度基準 | 後段 | 0 でモノクロ |
| `sharpness` | **対応（ハイブリッド）** | ≤1.0: `sharpnessAmount = default × s`、>1.0: default のまま + 後段 輝度 USM（増分のみ） | デコード + 後段 | §3.4。`apple_default` のデコーダ内シャープが「1.0 = カメラ相当」の基準（比較レポート §9: エッジ幅比 0.95 / 0.84）。重ねがけしない |
| `noise_reduction` | **対応（表は暫定）** | off → (0, 0)、light → `apple_default` 既定、full → 既定より強い値（ISO 帯別表） | デコード | 表の数値は【推測】、§3.7 で確定。**off でも白点を出さない制約**を CLI が保証（§3.4） |

廃止候補: **なし**（この PR では型を変えない）。`highlight_recovery` の廃止可否は #81 で決める。

### 3.2 後段処理の置き場所と全体の順序

【決定】後段は **Swift CLI 内**（`Develop.swift: develop()` の `filter.outputImage` の後、`CIContext` レンダの前）。設計書 §2.2 の前提どおりで、#73 コメントの要請に沿う。

CLI 内の処理順（`develop()`）:

```
1. CIRAWFilter 生成 → decoderVersion（最初。他が既定に戻るため）→ scaleFactor → isDraftModeEnabled=false
2. applyProfile(settings)                  … apple_default なら何もしない / neutral なら 0 群。exposure_ev / K / tint / lens
3. defaults = 読み戻し                      … sharpnessAmount / luminanceNR / colorNR / neutralTemperature / neutralTint（応答 applied.defaults）
4. applyAdjustmentsDecoder(adjustments, defaults)   … exposure / wb_temp / sharpness(≤1) / noise_reduction → filter へ（§3.3）
5. applyOverrides(settings)                … 個別上書き 7 キー（#79）。adjustments より後 = 明示の settings が勝つ
6. 白点ガード（§3.4、adjustments 指定時のみ） … luminanceNR == 0 && sharpnessAmount == 0 && scaleFactor < 1 → sharpnessAmount = 0.1
7. image = filter.outputImage（extended linear sRGB）
8. 後段チェーン（adjustments があるときだけ。無ければ現状どおり素通し）:
     CIColorClamp(0..1)                       … rawpy の _apply_shadow_lift_array 冒頭のクリップに相当
   → CILinearToSRGBToneCurve                  … 以降 sRGB ガンマ空間の 0..1（RGBAh）
   → geoframe_tone カーネル（lift → clip → brightness → contrast → saturation）
   → [USM gain > 0 のとき] 輝度画像 Y（CIColorMatrix で Rec.709 Y を RGB に複製）→ clampedToExtent → CIGaussianBlur(σ) → extent に crop
   → geoframe_usm カーネル（入力: tone 後の画像, ぼかした Y。d = Y − Yblur、コアリング、x + d·gain、[0,1] クリップ）
   → CISRGBToneCurveToLinear                   … 作業空間へ戻す
9. CIContext（既存と同じ）でレンダ → JPEG / tiff16
```

- 後段カーネルは **Metal の CIKernel**（`CIColorKernel` 2 本。`geoframe_tone` と `geoframe_usm`）。ソースは **`Sources/` の外** `native/coreimage-cli/Kernels/GeoFrameKernels.metal` に置く（`Sources/` 配下に置くと SwiftPM が "unhandled file" で失敗する。置くなら `Package.swift` の `exclude:` が要る）。`xcrun metal -fcikernel -c GeoFrameKernels.metal -o x.air` → `xcrun metallib -cikernel x.air -o x.metallib` でビルドし、**`.metallib` を base64 文字列として Swift ソースに埋め込む**（`Sources/CoreImageDevelop/Generated/KernelLibrary.swift`、生成物はコミット）。`CIKernel(functionName:fromMetalLibraryData:)` で読む。SwiftPM の `resources:` は実行ファイルの隣に `.bundle` を作るため、単一ファイルの `externalBin`（`src-tauri/binaries/`）と相性が悪い。
  - 【確認】この機で `xcrun -f metal` は Metal Toolchain（Xcode 27.1 のダウンロードコンポーネント）に解決した。他の機では `xcodebuild -downloadComponent MetalToolchain` が要る（§6 リスク 1）。
  - 代替（Metal が使えない場合の案 B）: lift / brightness / contrast / saturation は標準フィルタ（`CIColorCube`（lift の 1D 曲線を 3D LUT 化）→ `CIColorMatrix`（brightness·contrast の アフィン）→ `CIColorMatrix`（彩度行列）→ `CIColorClamp`）、USM は非推奨の CIKL `CIColorKernel(source:)`。精度（LUT 補間）と非推奨 API のリスクがあるため第一候補にしない。
- 数式（Metal、`sample_t s` は sRGB ガンマ空間）:

```metal
// geoframe_tone(s, lift, brightness, contrast, saturation)
float3 x = clamp(s.rgb, 0, 1);
x = clamp(x + lift * 2.0 * x * (1 - x) * (1 - x), 0, 1);   // lift = shadow_lift/100、2.0 = SHADOW_LIFT_GAIN/255
x = x * brightness;
x = (x - 0.5) * contrast + 0.5;                           // クリップしない（rawpy も最後まで float）
float y = dot(x, float3(0.2126, 0.7152, 0.0722));
x = y + (x - y) * saturation;
return float4(x, s.a);
// geoframe_usm(s, blurredY, gain, coring)
float y = dot(s.rgb, float3(0.2126, 0.7152, 0.0722));
float d = y - blurredY.r;
d = sign(d) * max(abs(d) - coring, 0.0);
return float4(clamp(s.rgb + d * gain, 0, 1), s.a);
```

  rawpy の `_apply_tone_array` では USM の輝度は contrast 後（saturation 前）の Y だが、saturation は Y を保存するので同値【確認】。USM 無しのときも最後に [0,1] へクリップする（`geoframe_tone` の後に `CIColorClamp`）。
- `CIGaussianBlur` の `inputRadius` は σ に相当する【推測: Apple の記述は "radius"。単体テスト（§3.7 単体）でガウス段の 10–90% 幅 = 2.563σ を確かめる】。端は `clampedToExtent()` → ぼかし → `cropped(to: extent)` で透明縁の暗化を防ぐ。
- 作業フォーマットは現状の RGBAh（half）。8bit 出力に対し ±1/255 程度の差は許容（§3.7 の許容値に含める）。差が大きければ `.RGBAf` に切り替える（メモリ 2 倍）。

### 3.3 各パラメータの写像（CLI `applyAdjustmentsDecoder` / 後段）

| パラメータ | 写像 | 値域・単位 | 応答 `applied` |
|---|---|---|---|
| `exposure` (ev) | `filter.exposure = Float(ev)` | −2..2 ⊂ CLI の −5..5 | `exposure` |
| `wb_temp` (v) | `K0 = defaults.neutralTemperature`（profile 適用後の読み戻し = 撮影時 WB）。`mired = 1e6/K0 − v × MIRED_PER_UNIT`、`K = clamp(1e6/mired, 2000, 50000)`。`filter.neutralTemperature = K`。tint は触らない。`MIRED_PER_UNIT = 0.85`（T5 で較正し据え置き。比較レポート §16.4）。K0 が有限で 2000..50000 の外なら写像を飛ばし `guards: ["wb_k0_out_of_range"]`（`WhiteBalanceMapping.isUsableK0`） | v>0（暖）→ mired 減 → K 増 → 暖色に描く（UI のヒント「青 ←→ 暖」と同方向であることをハーネスで確認） | `neutral_temperature`、`post.wb.k0`, `post.wb.mired_shift` |
| `shadow_lift` | `lift = v / 100` → `geoframe_tone` | 0..1 | `post.shadow_lift` |
| `brightness` / `contrast` / `saturation` | そのまま `geoframe_tone` | 契約の値域 | `post.brightness` 等 |
| `sharpness` (s) | s ≤ 1: `filter.sharpnessAmount = defaults.sharpnessAmount × s`、USM gain 0。s > 1: sharpnessAmount は既定のまま、USM: `sigma, baseGain, coring = captureSharpenParams(longEdge, iso)`（rawpy の定数を移植。長辺は `extent` の長辺）、`gain = min(1.2, baseGain × (s − 1) × 1.5) × isoMult`。ISO は `filter.properties` の EXIF（不明なら coring 0.5/255、減衰なし） | 0.8..1.6 | `sharpness_amount`、`post.usm.{sigma,gain,coring,long_edge,iso}` |
| `noise_reduction` | off: `(0, 0)`。light: 既定のまま（null）。full: `lum = max(defaults.lum, min(0.5, max(defaults.lum + 0.3, 0.3)))`（既定が 0.5 を超えるときは既定のまま。full < light を防ぐ）、`color = max(defaults.color, 0.3)`（上限 0.5 は T5 で確定。比較レポート §16.4） | 0..1 | `luminance_noise_reduction_amount`、`color_noise_reduction_amount` |
| `highlight_recovery` | 何もしない（0.5.0 まで）。**#81（CLI 0.6.0）で `exposure` / `shadow_lift` への区分線形の写像に置き換え**（[設計書](coreimage-preset-retune.md) §2・§3） | — | 0.5.0: `ignored: ["highlight_recovery"]`、0.6.0: `ignored: []` + `adjustments_applied.highlight_recovery` |

`noise_reduction` の ISO 帯別の表（`apple_default` v9、macOS 27.0.1 の既定値から。T5 で上限 0.5 に確定済み。CLI README にも同じ表がある）:

| ISO 帯 | 既定（light） lum / color | off | full（暫定） |
|---|---|---|---|
| < 800 | ≈0.01 / 0【確認: 対応表 ADR §4 の v9 ISO100 = 0.01】 | 0 / 0 | 0.31 / 0.30 |
| 800〜1600 | 未計測【推測: 0.05〜0.15】 | 0 / 0 | min(0.5, 既定 + 0.3) / 0.30 |
| 3200 | 0.241 / 0【確認】 | 0 / 0 | 0.50 / 0.30（旧暫定 0.541） |
| 6400 | 0.357 / 0【確認】 | 0 / 0 | 0.50 / 0.30（旧暫定 0.657） |

- `light` を既定にする根拠: rawpy の `light` は ISO<800 で Off、以上で Light（【確認】`_fbdd_mode_for`）。`apple_default` の輝度 NR も低 ISO でほぼ 0、高 ISO で効く同じ形。
- `full` の合否は「rawpy full と同じノイズ量」ではない（rawpy full の空ノイズはカメラ比 1.72 で CI 既定 1.05 より多い【確認: §10.5】）。「light より目に見えて滑らかで、塗りつぶし・星の消失が無い」を基準にする（§3.7）。
- **写像は相対値（既定値基準）なので `apple_default` 以外の profile では意味が変わる。** `profile: "neutral"` と `adjustments` の同時指定は `INVALID_REQUEST` にする（neutral の既定は 0 なので `light` = off、`sharpness ≤ 1` = 0 になり白点が出る）。

### 3.4 シャープ / NR の重ねがけ回避と白点の制約

- 「1.0 = カメラ JPEG 相当」の基準を **`apple_default` のデコーダ内シャープ**に置く。rawpy の基準 gain（撮影時シャープ）は CI では掛けない（掛けると比較レポート §9.6 の「塗り絵」になる）。スライダー >1 の増分だけを後段 USM で足す。
- `sharpnessAmount` の値域が 0..1 で、ISO ≤ 3200 の既定が既に 1.0 のため、>1 をデコーダ側で表現できない。これがハイブリッドにする理由。
- 白点（§1.4）の不変条件: **`luminanceNoiseReductionAmount == 0 ∧ sharpnessAmount == 0 ∧ scaleFactor < 1` を作らない。** `adjustments` 指定時に限り、CLI の `applyOverrides` の後（手順 6）で `sharpnessAmount = max(sharpnessAmount, 0.1)` に補正し、`adjustments_applied.guards: ["white_dot_sharpness_floor"]` に記録する（既定リクエスト・neutral の評価用リクエストには触れない）。スライダー最小 0.8 × 既定 0.81（ISO 6400）= 0.65 なので通常は発動しない。`noise_reduction: off` + `sharpness_amount: 0` の明示上書き（評価用途）でだけ発動する。ガードは **CLI** に置く（両方の最終値を知っているのは CLI だけ）。ハーネスの `impulse_vs_full` で回帰確認する。

### 3.5 CLI のリクエスト契約（0.3.0）

→ §4.1 に凍結ブロック。要点:

- 新規トップレベル `adjustments`（object、省略可）。9 キーすべて必須（Rust が解決済み全量を渡す。部分指定は `INVALID_REQUEST`）。値域は契約 §2。範囲外は `INVALID_REQUEST`（CLI はクランプしない。クランプは Rust / Python の責務）。
- `adjustments` と `settings.*` の優先: **明示の `settings.*` が勝つ**（`adjustments` 写像の後に `applyOverrides`）。例外: `settings.exposure_ev` / `neutral_temperature` / `neutral_tint` と `adjustments` の同時指定は `INVALID_REQUEST`（同じ量を 2 通りで指定する意味が無い）。個別上書き 7 キーは評価用（例: `adjustments` + `luminance_noise_reduction: 0`）に許す。
- `adjustments` は `settings.profile == "apple_default"` のときだけ許す。
- 応答は **`applied` を変えず**（既定リクエストでは 0.2.0 とバイト同一。`coreimage-regression-applied.sh` は `applied` のキー集合の差も差分として数えるため）、`adjustments` 指定時だけ **トップレベル** に `adjustments_applied: {defaults, post, ignored, guards}` を追加する（`encodeIfPresent` で省略時はキー自体を出さない）。`cli_version: "0.3.0"`。`--capabilities` に `adjustments: {supported: true, ignored: ["highlight_recovery"], post_kernel: "metal"}`。
- 既定リクエスト（`adjustments` 無し）の出力・`applied` は 0.2.0 と同一であること（回帰スクリプトで差分 0 件。`cli_version` は比較対象外【確認: スクリプトは `applied` だけを比べる】）。
- 白点ガード（§3.4）は **`adjustments` 指定時だけ**動く。ハーネスの `ci_v9_l1` / `--night-scales` 等は neutral + sharpness 0 で白点を意図的に再現しているため、既定リクエストの挙動は変えない。

### 3.6 原寸・書き出しの扱い

【決定】この PR では **rawpy のまま**。

理由:
1. ADR 0002 決定 2「原寸・書き出しは Adjustments の対応が揃ってから」。この PR で対応は揃うが、**検証（§3.7）と `highlight_recovery` / プリセット値の確定（#81、#71）が未了**で、「現行と同等の結果」の条件はまだ満たさない。
2. 原寸には追加の設計が要る: EXIF 継承（CLI は EXIF を書かない契約。rawpy は `build_inherited_exif` でペア JPG から継承し GPS は書かない）、書き出し契約（`ExportItem.developed_filename` は `outputs/developed/` のファイル名、マニフェストにレシピ JSON を inline）、`export.py` が Python 側にある。
3. 代わりに CLI / Rust は `mode: "full"` を受けられる形を保ち（CLI は既に可）、後続 Issue（§7 案 1）で切り替える。

副作用: **プレビューは CI の画、本現像・書き出しは rawpy の画**になり、見たものと書き出す物が一致しない期間が生じる（特に exposure の基準、色、ノイズ）。緩和として「Core Image プレビュー」バッジと注記を出す（§2.8）。これは §9 判断 1。

### 3.7 検証方法

#### 単体（Swift、`swift test`。ARW 不要）

- `RequestParserTests`: `adjustments` の受理 / 9 キー必須 / 値域 / profile 制約 / `settings.exposure_ev` との排他。
- `AdjustmentMappingTests`: `captureSharpenParams` が rawpy の `_capture_sharpen_params` と同じ値を返す（固定ケース表: 長辺 1600 / 2400 / 6000 × ISO 100 / 800 / 3200 / 不明）。`wbShift` の単調性と境界。`noiseReductionTable`。白点ガードの発動条件。
- `PostChainTests`: 合成画像（256 段の水平グラデーション + 色パッチ + 1 本の階段エッジ）を `CIImage` にし、後段チェーンだけを通して読み戻し、Swift で書いた numpy 式の再実装と比較。許容: tone 段は最大誤差 ≤ 1.5/255【推測: half float】、USM 段はガウス 10–90% 幅 = 2.563σ ± 10%。埋め込み metallib から 2 関数が読めること。

#### 結合（Rust `cargo test`、実 ARW があるときだけ）

- 既存 `develops_real_arw_when_available` を `apple_default` + adjustments 版に更新。サンプルのルートを `GEOFRAME_CI_TEST_ARW_ROOT`（既定 `/Volumes/Backup/100MSDCF`）で差し替え可能にする（Swift の `GEOFRAME_CI_SMOKE_ARW` と同じ流儀。`/Volumes/Backup` は最近マウントされていない【確認: 比較レポート §13】）。
- 同じ adjustments で 2 回目はキャッシュヒット、adjustments を 1 つ変えると別ハッシュ、原本不変。

#### 比較ハーネス（`scripts/compare_engines.py` に `--adjustments` モードを追加）

> 下表の「合否の目安【推測】」列は**設計時の暫定値**。T5（2026-10-20〜21、昼 6 + 星空 3 カット）で確定した最終値は表の後ろの「確定した閾値」を正とする（根拠は比較レポート §16、定数は `scripts/compare_engines.py` の `ADJ_*` / `RB_PIXEL_RANGE` / `IMPULSE_REFERENCE_RATIO`）。

対象カット: 日中低 ISO 3（例 DSC04670 / DSC04651 / DSC04872）、高 ISO 2（DSC04430 / DSC04453）、星空 2（DSC07415 / DSC07407）、逆光（マウントされていれば DSC04473）。preview 2400px。

| パラメータ | 掃引 | rawpy と比べる指標（方向と大きさ） | CI 内オラクル | 合否の目安【推測】 |
|---|---|---|---|---|
| exposure | −1, 0, +1 | 平均輝度（リニア化）の log2 差。CI は ≈ ±1.0 stop、rawpy は auto_bright で小さい（既知差） | — | CI の log2 差が ±1.0 の ±0.15 以内、単調 |
| wb_temp | −60, 0, +60 | 近中立パッチ（カメラ JPEG で彩度下位 10% のタイル）の R/B 平均比の ln。rawpy の ±60 と **同方向**で大きさの比が 0.7〜1.4 | — | 方向一致。比が外れたら `MIRED_PER_UNIT` を較正して再実行 |
| shadow_lift | 0, 45 | 暗部（輝度下位 25%）の平均輝度の増分 | 基準: tiff16 sRGB（adjustments 無し）。CI 側: `adjustments` の **デコード段を恒等**（exposure 0、wb_temp 0、sharpness 1.0、noise_reduction light）にし後段だけ動かし、**こちらも `format: tiff16, output_color_space: srgb`** で出す。基準に Python `_apply_shadow_lift_array` + `_apply_tone_array`（USM gain を 0 に mock、`rawpy_nosharp` と同じ手法）を掛けて比較 | オラクル: ΔE76 中央値 ≤ 0.3、チャンネル差 p99 ≤ 1/255（half float のみ。JPEG の混入なし） |
| brightness / contrast / saturation | 0.7 / 1.4、0.8 / 1.4、0 / 1.5 | 平均輝度・標準偏差・a\*b\* の標準偏差 | 同上（同じオラクル。saturation 0 でモノクロ） | 同上 |
| sharpness | 0.8, 1.0, 1.6 | `edge_width_ratio`（§9.4）と `flat_noise` | — | 0.8 > 1.0 > 1.6 の順でエッジ幅が狭くなる（単調）。1.6 の `flat_noise` がカメラ比 1.5 倍以内。1.0 は baseline（0.95）から ±0.05 |
| noise_reduction | off / light / full | 星空セットの `sky_noise_ratio` / `sky_chroma_noise` / `star_recall` / `sky_hf_ratio`（§10.4） | — | off > light > full の順にノイズが減る。full でも `star_recall ≥ 0.99`、`sky_hf_ratio ≥ 0.7 × camera`（塗りつぶし防止）。表の値はここで確定 |
| 白点 | off + 既定 / off + `sharpness_amount: 0`（明示上書き → ガード発動） | `impulse_vs_full` | — | 全ケースで ≤ 10/枚（§10.5 の `ci_apple_v9_l1` と同水準） |
| 既定リクエスト回帰 | `adjustments` 無し | `scripts/coreimage-regression-applied.sh` | — | 27.0.1 baseline と差分 0 件 |

**確定した閾値（T5、比較レポート §16.3〜§16.5）**

| チェック | 暫定 | 確定 | 定数 |
|---|---|---|---|
| exposure ±1 EV の log2 差（絶対値） | ±1.0 の ±0.15 | ±1.0 の **±0.4**（粗い健全性確認。指標自身の偏りで rawpy も ±1.15〜1.21 を読む） | `ADJ_EXPOSURE_STOP_TOL = 0.4` |
| exposure の大きさ（新設） | — | CI の log2 差 / rawpy の log2 差が **0.8..1.25**（実測 0.81〜1.18） | `ADJ_EXPOSURE_RATIO_RANGE` |
| wb_temp ±60 の R/B 比 | rawpy 比 0.7〜1.4 | 同左。ただし測る画像自身の最大チャンネルが **0.05..0.9** の画素だけを使う（飽和画素は WB を動かしても R/B が動かない） | `ADJ_WB_RATIO_RANGE = (0.7, 1.4)`、`RB_PIXEL_RANGE = (0.05, 0.9)` |
| sharpness 1.0 のエッジ幅（カメラ比） | 0.95 ± 0.05 | 0.95 ± **0.15**（`sharpness_ci_10_edge_ratio`。実測 0.87〜1.02） | `ADJ_SHARP_BASELINE_WIDTH = 0.95`、`ADJ_SHARP_BASELINE_TOL = 0.15` |
| sharpness 1.6 の flat_noise | カメラ比 1.5 以内 | **identity（1.0）比** ≤ 1.5（`sharpness_ci_16_flat_noise_vs_identity`。実測 1.00〜1.21。カメラ比は参考値として残す） | `ADJ_SHARP16_NOISE_MAX = 1.5` |
| NR full の星の残存率 | `star_recall ≥ 0.99` | `≥ 0.99` **または** light から `max(0.01, 1.5/n)` 以上減らない（相対条件） | `ADJ_NR_FULL_RECALL_MIN = 0.99`、`ADJ_NR_FULL_RECALL_DROP_MAX = 0.01`、`ADJ_NR_FULL_RECALL_DROP_POINTS = 1.5` |
| NR full の空の高周波比 | `sky_hf_ratio ≥ 0.7 × camera` | camera 比 **0.5 以上**、または light 比 **0.9 以上** | `ADJ_NR_FULL_HF_MIN = 0.5`、`ADJ_NR_FULL_HF_VS_LIGHT_MIN = 0.9` |
| 白点（`impulse_vs_full`） | ≤ 10/枚 | ≤ 10/枚（変更なし）。ただし基準側でも 3x3 メディアン比 `24 × 0.5 = 12` 以上明るい点は数えない（昼の葉の照り返しの誤検出を除く。全呼び出しで適用） | `ADJ_IMPULSE_MAX = 10`、`IMPULSE_REFERENCE_RATIO = 0.5` |
| その他（方向・単調・オラクル・ガード・既定リクエスト回帰・処理時間 1.5 倍以内） | §3.7 のまま | 変更なし（全合格） | — |

- オラクル比較は設計書 §2.1 の中間案（CLI の sRGB tiff16 → Python 後段）をそのまま検証に使う。`apply_rawpy_finish` の実装パターンを流用する。
- 出力は `work/engine-comparison/adjustments1/`（gitignore）に `adjustments_metrics.csv` / `adjustments_summary.json` / `sheets/`。純粋関数（R/B 比、暗部増分）は `tests/test_compare_engines.py` に追加。
- 処理時間: 後段チェーン込みの preview が baseline（約 1.0〜1.3 秒）の 1.5 倍以内（回帰確認の目安と同じ）。

---

## 4. 凍結する契約（並行実装の前にこの節を確定する）

### 4.1 CLI 0.3.0 リクエスト（追加分）

```jsonc
{
  "input": "...", "output": "...", "mode": "preview", "max_edge": 2400, "format": "jpeg", "decoder_version": "9",
  "settings": { "profile": "apple_default", "lens_correction": true },
  "adjustments": {                       // 省略可。指定時は 9 キーすべて必須、未知キーは INVALID_REQUEST
    "exposure": 0.0,                     // number −2..2
    "wb_temp": 0.0,                      // number −100..100
    "shadow_lift": 15.0,                 // number 0..100
    "highlight_recovery": 60.0,          // number 0..100（受理するが無効）
    "brightness": 1.0,                   // number 0.7..1.4
    "contrast": 1.03,                    // number 0.8..1.4
    "saturation": 1.02,                  // number 0.0..1.5
    "sharpness": 1.04,                   // number 0.8..1.6
    "noise_reduction": "light"           // "off" | "light" | "full"
  }
}
```

規則:
- `adjustments` は `settings.profile` が `apple_default` のときだけ可（それ以外は `INVALID_REQUEST`）。
- `adjustments` と `settings.exposure_ev` / `neutral_temperature` / `neutral_tint` の同時指定は `INVALID_REQUEST`。
- 個別上書き 7 キー（`luminance_noise_reduction` 等）は同時指定可で、`adjustments` の写像の**後**に適用される（明示が勝つ）。
- 値域違反は `INVALID_REQUEST`（クランプしない）。bool は数値として受けない（既存の `number()` と同じ）。
- 既定リクエスト（`adjustments` 無し）は 0.2.0 と同じ処理。

### 4.2 CLI 0.3.0 応答（追加分）

```jsonc
{
  "cli_version": "0.3.0", "...": "既存キーはそのまま",
  "applied": { "...": "既存キーのまま（最終的に filter から読み戻した値）。キーを足さない" },
  "adjustments_applied": {               // adjustments 指定時のみ出る（省略時はキー自体が無い）
    "defaults": {                        // profile 適用直後・adjustments 適用前の読み戻し
      "sharpness_amount": 1.0, "luminance_noise_reduction_amount": 0.241, "color_noise_reduction_amount": 0,
      "neutral_temperature": 5749.2, "neutral_tint": 14.3
    },
    "post": {                            // 後段に実際に使った値
      "shadow_lift": 0.15, "brightness": 1.0, "contrast": 1.03, "saturation": 1.02,
      "usm": { "sigma": 0.6, "gain": 0.0, "coring": 0.0, "long_edge": 2400, "iso": 100 },
      "wb": { "k0": 5749.2, "mired_shift": 0.0, "mired_per_unit": 0.85 },
      "noise_reduction": "light"
    },
    "ignored": ["highlight_recovery"],
    "guards": []                         // 例: ["white_dot_sharpness_floor"]、["wb_k0_out_of_range"]
  }
}
```

優先順位（同じ CIRAWFilter プロパティに複数の指定が当たるとき）: 白点ガード ＞ 明示の `settings.*` 上書き ＞ `adjustments` の写像 ＞ profile。

`--capabilities` に `"adjustments": {"supported": true, "ignored": ["highlight_recovery"], "post_kernel": "metal"}` を追加。

### 4.3 Rust コマンド

```rust
// src-tauri/src/coreimage.rs
#[tauri::command] pub fn preview_engine() -> &'static str;           // "coreimage" | "rawpy"（GEOFRAME_PREVIEW_ENGINE=rawpy で後者）
#[tauri::command] pub async fn develop_preview_coreimage(
    app: AppHandle, capture_id: String, adjustments: serde_json::Value,   // 9 キー全量。フロントが preset 既定値 + overrides をマージ済み
) -> Result<CoreImagePreviewResult, CoreImageError>;

pub struct CoreImagePreviewResult {
    pub capture_id: String, pub output_path: String, pub provenance_path: String,
    pub width: u64, pub height: u64, pub elapsed_ms: Value, pub cache_hit: bool,
    pub params_hash: String,                 // hash16
    pub decoder_version: String, pub cli_version: String, pub macos_version: String,
    pub adjustments: Value,                  // Rust が正規化（クランプ・float 化）した全量
    pub applied: Value,                      // CLI の applied そのまま（キーを足さない。§4.2）
    pub adjustments_applied: Value,          // CLI のトップレベル adjustments_applied（defaults / post / ignored / guards）そのまま。調整が無い・CLI が返さない場合は null
    pub source: Value,                       // CLI の source（lens_model / iso / camera_model）
}
```

- 新規 `src-tauri/src/adjustments.rs`: `pub fn normalize_adjustments(&Value) -> Result<Value, CoreImageError>`（9 キー必須、未知キー拒否 → `ADJUSTMENTS_INVALID`、数値は契約の値域へ**クランプ**、float 正規化、`noise_reduction` は enum）。`ADJUSTMENT_RANGES` を Rust 定数で持つ。#80 はこのモジュールにプリセット表と `resolve_adjustments` を足す形で拡張する（コマンドの署名は変えない）。
- 旧 `coreimage_experiment_enabled` / `develop_coreimage` / `EXPERIMENT_ENV` は削除。

### 4.4 フロント

```ts
// src/lib/types.ts（追加のみ）
export type DevelopEngine = "rawpy" | "coreimage";
export type DevelopedResult = { ...既存..., engine?: DevelopEngine };
// src/lib/app-state.ts
export type ViewerSource = { ...既存..., previewEdge?: number };
// src/lib/coreimage-api.ts（全面書き換え）
export type CoreImagePreviewResult = { /* 4.3 と同名フィールド */ };
export function previewEngine(): Promise<DevelopEngine>;            // 非 Tauri は "rawpy"
export function developPreviewCoreImage(captureId, adjustments: Adjustments): Promise<CoreImagePreviewResult>;
export function toDevelopedResult(r: CoreImagePreviewResult, preset: PresetInfo): DevelopedResult;   // §2.7
export function shouldFallbackToRawpy(error: unknown): boolean;    // §2.6 の表
export function resolveAdjustments(preset: PresetInfo, overrides: Partial<Adjustments>): Adjustments;  // {...preset.adjustments, ...overrides}
```

---

## 5. 実装タスクの分割・順序・完了条件

### 5.1 順序

```
T0 契約確定（§4。この設計書の承認で完了）
 ├─ T1 Swift CLI 0.3.0 ─────────────┐
 ├─ T2 Rust（coreimage.rs / adjustments.rs）│ 並行可（互いのファイルに触れない）
 ├─ T3 フロント                          │
 └─ T4 ハーネス --adjustments ───────────┘
T5 結合: npm run build:coreimage → cargo test → tauri:dev で手動確認 → ハーネス実行（§3.7）→ 較正値（wb 係数・NR 表）を T1 に反映
T6 ドキュメント（T5 の結果を反映）
```

T2 は T1 の実バイナリ無しで進められる（契約に対する JSON 組み立てと `/bin/cat` 等のモックテスト）。結合テストだけ T1 の成果物が要る。

### 5.2 タスク

| ID | 内容 | 触るファイル（所有） | 完了条件 |
|---|---|---|---|
| T1 Swift CLI | `Request.swift`: `adjustments` 解析・値域・排他。`Develop.swift`: `defaults` 読み戻し、`applyAdjustmentsDecoder`、白点ガード、後段チェーン、`applied` 拡張、`cliVersion = "0.3.0"`、`Capabilities`。新規 `Adjustments.swift`（写像・`captureSharpenParams`・NR 表）、`PostChain.swift`、`Kernels/GeoFrameKernels.metal`、`Generated/KernelLibrary.swift`。テスト 3 群（§3.7 単体）。README 更新 | `native/coreimage-cli/**`、`scripts/build-coreimage-cli.sh`（metal → metallib → Swift 生成を追加。生成物が .metal より古いときだけ再生成）、新規 `scripts/build-coreimage-kernels.sh` | `swift test` 緑（ARW 無しで単体が走る）。既定リクエストの `applied` が 0.2.0 と同一（`scripts/coreimage-regression-applied.sh` 差分 0 件）。`--capabilities` に adjustments |
| T2 Rust | `coreimage.rs`: 既定値変更（apple_default / lens ON）、`adjustments` をリクエストへ、出力先 `coreimage/preview/`、`CACHE_KEY_SCHEMA = 2`、LRU 削除、待ち付きスロット、`preview_engine`、`develop_preview_coreimage`、旧コマンド削除、旧出力掃除。新規 `adjustments.rs`。`main.rs` の `generate_handler!` 更新。**既存テストの更新（「修正」ではなく仕様変更）**: `base_request_respects_cli_contract`（`{"profile":"neutral"}` → `apple_default` + `lens_correction` + `adjustments`）、`cleanup_stale_temps` 系（対象ディレクトリが `coreimage/preview/`）、`develops_real_arw_when_available`（apple_default + adjustments、`GEOFRAME_CI_TEST_ARW_ROOT`） | `src-tauri/src/coreimage.rs`、`src-tauri/src/adjustments.rs`（新規）、`src-tauri/src/main.rs`（handler 行のみ） | `npm run build:coreimage` 後 `cargo test` 緑（`normalize_adjustments` のクランプ・拒否、鍵の感度（adjustments 1 キーで変わる）、LRU 削除、待ちスロットの上限 → BUSY、実 ARW 結合テスト（あれば））。`cargo clippy` 警告なし |
| T3 フロント | `coreimage-api.ts` 書き換え（§4.4）、`App.tsx: runDevelop` の分岐とフォールバック、`types.ts` `engine?`、`app-state.ts`: `ViewerSource.previewEdge` と **`develop/ok` の通知文に `action.result.engine === "coreimage"` なら「Core Image・」を入れる**（通知は reducer が結果から組む【確認】）、`format.ts: lensReasonLabel` に `coreimage_unsupported` を追加、`BeforeAfter.tsx` バッジ・注記、`FullSizeViewer.tsx` 文言、`OperationPanel.tsx`（`CoreImageExperiment` 除去、`previewEdge` 受け渡し）、`CoreImageExperiment.tsx` 削除、CSS の不要クラス削除。テスト: `coreimage-api.test.ts`（`toDevelopedResult` / `resolveAdjustments` / `shouldFallbackToRawpy`）、`app-state.test.ts` に engine 付き結果の通知文のケースを 1 つ追加 | `src/lib/coreimage-api.ts`、`src/lib/coreimage-api.test.ts`（新規）、`src/lib/types.ts`、`src/lib/app-state.ts`、`src/lib/app-state.test.ts`、`src/lib/format.ts`、`src/App.tsx`、`src/components/{BeforeAfter,FullSizeViewer,OperationPanel}.tsx`、`src/components/CoreImageExperiment.tsx`（削除）、`src/App.css` 等 | `npm test`、`npx tsc --noEmit`、`npm run lint`、`npm run build` 緑。ブラウザモードで挙動不変 |
| T4 ハーネス | `compare_engines.py`: `--adjustments` モード（§3.7 の掃引・オラクル・指標・シート）、`run_coreimage` に `adjustments=` 引数、rawpy ワーカーに掃引値を渡す `--worker-adjustments <JSON>`（現状の `worker_rawpy` は既定プリセットのみ【確認】）。純粋関数のテスト | `scripts/compare_engines.py`、`tests/test_compare_engines.py` | `npm run test:raw` 緑。`--dry-run` が通る |
| T5 結合・較正 | 上記を結合してハーネスを実行。`MIRED_PER_UNIT`・NR 表・許容値の確定。`tauri:dev` で手動確認（プレビュー → 比較欄 → 原寸ビューア、フォールバック（CLI を一時的に退避して確認）、切り戻しフラグ、カット切替、フォルダ切替） | T1 の定数のみ | §3.7 の合否をすべて満たす。結果を `work/engine-comparison/adjustments1/` と比較レポートの新節に残す |
| T6 ドキュメント | `docs/API_CONTRACT.md`（`engine?` 追加、Tauri のプレビュー経路、`outputs/preview` は rawpy 用のまま）、`docs/coreimage-parameter-mapping.md`（「#73 の決定」列）、`docs/adr/0002-coreimage-engine-decision.md` 追補（段階 1 完了、見え方不一致の注記）、`docs/coreimage-regression-check.md`（記録行: CLI 0.3.0 差分 0 件。`--adjustments` を回帰項目に追加）、`docs/design/coreimage-feature-migration.md` §2.2（前提どおり実装された旨）、`README.md`（試験機能 → 既定、切り戻しフラグ）、CLI README（T1 内） | docs 配下、`README.md` | 記述が実装と一致（メインがレビュー） |

並行時の衝突回避: T1〜T4 は互いのディレクトリに触れない。`main.rs` は T2 のみ、`types.ts` / `app-state.ts` は T3 のみ。T6 は T5 の後にメインまたは 1 人で。

### 5.3 検証コマンドのまとめ

```sh
cd native/coreimage-cli && swift test                      # T1
npm run build:coreimage && (cd src-tauri && cargo test && cargo clippy)   # T2（build:coreimage が先）
npm test && npx tsc --noEmit && npm run lint && npm run build             # T3
npm run test:raw                                           # T4
scripts/coreimage-regression-applied.sh <星空 ARW のフォルダ>  # 既定リクエストの回帰（差分 0 件）
.venv/bin/python scripts/compare_engines.py --source-root <ARW> --adjustments --captures ... --out work/engine-comparison/adjustments1
env -u CI npm run tauri:dev                                # 手動確認
```

---

## 6. リスク

1. **Metal toolchain**: `xcrun metal` は Xcode 27 ではダウンロードコンポーネント（この機にはある【確認】）。他の機・CI では `xcodebuild -downloadComponent MetalToolchain` が要る。生成物 `KernelLibrary.swift` をコミットしておけば toolchain 無しでも `swift build` / `swift test` は通る（再生成が要るのは .metal を変えたときだけ）。案 B（標準フィルタ + CIKL）は非推奨 API 依存。
2. **`apple_default` の既定値は OS 更新で変わる**（ADR 決定 4）。写像が既定値基準なので `sharpness ≤ 1` / `noise_reduction` の結果も変わる。鍵に `macos_version` を含めるのでキャッシュは割れるが、見え方は変わる。#75 の回帰項目に `--adjustments` を加える。
3. **`wb_temp` の係数は未検証**（`neutralTemperature` の意味・方向も対応表で「未確認」）。ハーネスで較正する前提。較正後も非線形（カメラ WB の K に依存）で rawpy と厳密には一致しない。
4. **exposure の基準差**: rawpy は auto_bright 後、CI は固定 baselineExposure。同じプリセット値でも明るさが違う。暗所（#50 経路）は CI に無い。プリセット値の再調整は #81、暗所判定は #88。→ **移行期間中、プレビュー（CI）と本現像（rawpy）の画が一致しない**（§9 判断 1）。
5. **half float の精度**（±1/255）と JPEG q92 がオラクル比較の許容値に入る。超えるなら `.RGBAf`。
6. **`CIGaussianBlur` の radius の意味**と端処理。単体テストで σ を確かめ、`clampedToExtent` で端を処理する。
7. **キャッシュ肥大**: LRU 削除で抑えるが、数値は暫定。
8. **BUSY の競合**: 待ち付きスロットで緩和。Python 側の現像（本現像）と CLI が同時に走ると CPU / メモリが重なる（従来も PSO + 現像で同じ）。
9. **Python サイドカーは引き続き必須**（プリセット・解析・サムネ・書き出し）。「Python 不要」にはならない。
10. **`/Volumes/Backup` のサンプルがマウントされていない**ため、Rust の結合テストと逆光の検証はスキップされうる。星空セット（`~/works/.../work/raw`）で代替できるよう環境変数を用意する。
11. **`types.ts` の凍結**: 任意フィールド追加に留め、契約文書に追記する。

---

## 7. 後続 Issue の案（この PR から立てるもの）

1. 原寸・書き出しの Core Image 化（`coreimage/full/`、EXIF 継承（CLI に `inherit_exif_from` を足すか Rust で継承）、`ExportItem` / マニフェストとの接続、`outputs/developed/` との共存）。#73 の検証と #81 の後。
2. `GEOFRAME_PREVIEW_ENGINE` 切り戻しフラグの撤去（安定後）。
3. キャッシュ上限・削除方針の見直し（実使用量を見て）。
4. （既存）#81: `highlight_recovery` の代替、プリセット値の再調整。この PR の `adjustments_applied.ignored` と `--adjustments` ハーネスを材料にする。
5. （既存）#88: 暗所判定の扱い。CI プレビューと rawpy 原寸の明るさ差の主要因の 1 つ。

---

## 8. 推測と事実の区別（まとめ）

- 事実（コードで確認）: §1 全体、§1.4 の実効値と白点条件、`sharpnessAmount` の値域、表示側の消費者と書き出しが `fullResults` のみを読むこと、asset scope、Metal toolchain の存在。
- 推測（検証で確定）: `MIRED_PER_UNIT = 0.85`、NR 表の `full` の数値、`CIGaussianBlur.inputRadius ≈ σ`、オラクル比較の許容値、LRU 上限、後段チェーンの処理時間増、CIKL の可用性。

---

## 9. オーナー判断が要る事項

| # | 事項 | 選択肢 | 設計書の案 | 影響 |
|---|---|---|---|---|
| 1 | **プレビュー = Core Image、本現像・書き出し = rawpy** の不一致を移行期間として受け入れるか | (a) 受け入れ、バッジ・注記で明示し後続 Issue で原寸を切り替える。(b) 原寸も同時に CI に切り替える（EXIF 継承・書き出し契約の設計が増え、#81 前に画が確定しない）。(c) 既定化を #81 まで待つ | **(a)** | PR の範囲。(a) を取るなら「見たものと書き出す物が違う」ことを README / UI に明記 |
| 2 | **`highlight_recovery` をこの PR では無効（no-op）にする**こと（#73 コメントは代替の決定を #73 の成果物に求めているが、#71 の実データが無い） | (a) 無効 + `adjustments_applied.ignored` + UI 注記、代替は #81。(b) 暫定で EDR / boost を割り当てる（未評価のまま出す）。(c) 逆光サンプルを用意してから #73 を進める | **(a)** | 救済系 3 プリセットのプレビューが rawpy と異なる（スライダーは効かない） |
| 3 | `CoreImageExperiment`（3 面比較 UI）と `GEOFRAME_COREIMAGE_EXPERIMENT` の削除 | 削除 / 残す | **削除**（比較はハーネスで） | UI 項目の消失 |
| 4 | プレビュー長辺 2400px（rawpy は 1600） | 2400 / 1600 | **2400**（baseline と同一、CI は速い） | キャッシュ容量 約 1.5〜2 倍、原寸ビューアの見え方 |
| 5 | `sharpness` のハイブリッド写像（≤1.0 はデコーダ内シャープを弱める、>1.0 は増分を USM で足す）と「1.0 = `apple_default` 既定」の意味づけ | 案どおり / 「常に既定 + USM」/ 「常にデコーダのみ（>1 は効かない）」 | **ハイブリッド** | スライダーの体感。PSO の `_SHARPNESS_SEARCH_MAX` 導出（#86）に影響 |
| 6 | `noise_reduction: light` = `apple_default` の既定という意味づけ、`full` の暫定表 | 案どおり（ハーネスで確定）/ 別表 | **案どおり** | `nightscape` / `shadow_rescue` の見え方 |
| 7 | 後段を Metal CIKernel で実装すること（toolchain 依存）と、生成物（metallib の埋め込み Swift）をコミットすること | Metal / 案 B（標準フィルタ + CIKL） | **Metal** | ビルド環境の要件 |
| 8 | キャッシュ上限（600 ファイル / 1.5 GB）と旧試験出力の自動削除 | 数値 | 案どおり | ディスク使用量 |
| 9 | 切り戻しフラグ名 `GEOFRAME_PREVIEW_ENGINE=rawpy` | 名前 | 案どおり | 運用手順 |
