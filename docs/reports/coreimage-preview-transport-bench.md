# 手動プレビューの輸送方式ベンチ：プロトコルと実行手順（Issue #141 ① Stage0 / Stage1）

「プレビュー現像」ボタンの 1 回分（2400px JPEG）を、**同じ RAW・同じ調整列・同じ出力設定**で単発 CLI と既存 `--batch`
に流して比べる。Swift ライブラリ（FFI/XPC）の PoC に進むかを、**輸送層に削れる余地があるか**で判断するための基盤。
**この文書は手順と解釈の枠組みで、結果（数値）はまだ無い。** 実測は親が実素材で行い、結果は別途記録する。

- 対象外: FFI/XPC/SwiftUI、本番バックエンドの切替、画質閾値の確定。Rust の本番現像・候補・キャッシュは変更しない。
- 実装: `src-tauri/src/coreimage/transport_bench.rs`（test 専用。`coreimage.rs` に `#[cfg(test)] mod` を 1 つ足しただけ）、
  UI フック `src/lib/preview-timing.ts`。

## 1. 何を測る（測らない）か

| 層 | 測る手段 | 測る内容 |
|---|---|---|
| Rust 内の輸送比較（Stage0） | `coreimage_transport_bench`（ignored test） | 方式ごとの要求単位 wall、系列単位 wall、CLI 応答の `elapsed_ms`、単発のみ `GEOFRAME_CI_TIMING` の段階内訳、RSS、エラー/timeout/再起動、出力 RGB 差 |
| ボタン → 画像表示（Stage1） | UI フック（localStorage で ON） | ボタンのハンドラ入口→invoke 開始、invoke の開始→戻り、戻り→`<img>` load、`decode()`、decode 後の 2 回目の rAF |

**E2E を Stage0 の wall で代用しない。** Stage0 は React / Tauri IPC / WebView 表示を含まない。逆に Stage1 は CLI 内訳を持たない。
2 つを `invoke_wall_ms`（Stage1）と `request wall`（Stage0 の `rust_develop_miss`）で突き合わせ、差を IPC・シリアライズの上限とみなす。

測らないもの（`unmeasured` として JSON にも書く）: クリックイベント→ハンドラ入口の入力遅延、**スライダー入力→数値表示の応答**（画像 E2E と別指標）、
実際の paint、OS ファイルキャッシュ/GPU の cold、連続ドラッグ中の同時実行・BUSY・古いリクエスト破棄、
`rust_develop`/batch の `GEOFRAME_CI_TIMING` 内訳、batch のピーク RSS、画質同等性の合否（閾値未決）、Swift ライブラリ。

## 2. 方式と測定ラベル

| method | 内容 | ラベル |
|---|---|---|
| `single_cli` | 既存 `run_sidecar`（本番と同じ起動・待ち方）で単発 CLI。リクエスト JSON は `batch_stateless` と同一 | 輸送比較 |
| `rust_develop_miss` | 既存 `coreimage::develop()`（解決・キャッシュ鍵・CLI・来歴書き込み・prune）。反復ごとに空の app_data | 輸送比較（アプリ全体） |
| `rust_develop_cache_hit` | 同じ app_data で `develop()` を再度。**CLI を呼ばない完成 JPEG のキャッシュヒット** | **輸送比較ではない**（別集計。cli_total は null） |
| `batch_stateless` | 既存 `BatchSession` に `op` 無しの現像行（毎回デコード） | 輸送比較 |
| `batch_decode_render` | `op: decode` → `op: render`。後段だけの系列ではハンドルを再利用し、反復の先頭で解放 | 輸送比較 |

全方式で、`mode: preview`・`max_edge: 2400`・`format: jpeg`・`decoder_version: 9`・`apple_default`・レンズ補正 ON、
`jpeg_quality` は**送らない**（CLI 既定 0.92 = 本番 Rust と同じ）、`adjustments` は `normalize_adjustments` 済みの 9 キー全量。
Rust の完成 JPEG キャッシュ（`rust_develop_cache_hit`）は、`measurement_label` で別物と明示し、輸送の統計に混ぜない。

## 3. 調整列（固定）

基準 `{exposure 0, wb_temp 0, shadow_lift 15, highlight_recovery 60, brightness 1, contrast 1.03, saturation 1.02, sharpness 1.04, noise_reduction light}`
から 1 キーだけを動かす。同じ調整を 1 系列内で 2 度出さない（`rust_develop_miss` が意図せずキャッシュヒットしないため）。

| 系列 | class | ステップ |
|---|---|---|
| `decode_exposure` | decode_changing | −0.4 / −0.2 / 0 / 0.2 / 0.4 |
| `decode_highlight_recovery` | decode_changing | 0 / 30 / 60 / 90 / 100（CLI 内で exposure と shadow_lift に写像） |
| `decode_wb_temp` | decode_changing | −20 / −10 / 0 / 10 / 20 |
| `decode_min_sharpness` | decode_changing | 0.8 / 0.85 / 0.9 / 0.95 / 1.0（`min(sharpness,1)` がデコード段） |
| `decode_noise_reduction` | decode_changing | off / light / full |
| `post_shadow_lift` | post_only | 0 / 10 / 20 / 30 / 40 |
| `post_brightness` | post_only | 0.95 / 0.975 / 1 / 1.025 / 1.05 |
| `post_contrast` | post_only | 0.95 / 1 / 1.03 / 1.06 / 1.1 |
| `post_saturation` | post_only | 0.9 / 0.95 / 1.02 / 1.05 / 1.1 |
| `post_sharpness_gt1` | post_only | 1.05 / 1.15 / 1.25 / 1.35 / 1.45（USM 増分だけが後段） |

分類の正しさ（decode_changing は decode key が毎ステップ異なり、post_only は全ステップで同一）は通常のテストが検査する。

## 4. 反復・cold / warm・統計

1 回の実行 = 撮影 ID × 系列 × 方式。各（方式, 系列）で **cold 1 反復 + warm N 反復**（`WARM`、既定 3）を、全ステップ順に再生する。

- **cold**: その方式×系列の最初の反復。batch は新規プロセス/セッション（起動時間は `session_start_ms` に別記）。
  **OS のファイルキャッシュ・GPU 状態の cold は保証しない**（SMB の初回読み込みが cold に混ざるかは未制御）。単発は毎回新規プロセスなので、
  cold/warm の差は OS キャッシュ等の差だけを表す。
- **warm**: 同じ方式×系列の 2 回目以降（batch は同じセッション）。`rust_develop_cache_hit` は常に warm。
- **p50/p95**: 最近順位法。`small_sample`（n < 20）の p95 は実質 max で、率として読まない。
- **要求単位 wall** と **系列単位 wall**（1 反復の全ステップ wall の合計。全ステップ成功の反復のみ。cold は n=1）を別に出す。
- `transport_overhead_ms = wall − CLI 応答の elapsed_ms.total`（decode+render 方式は両者の和）。プロセス起動・JSON・待ち・ファイル書き込み
  （`rust_develop` は解決/鍵/来歴/prune も）を含む。`run_sidecar` は子の終了を 25ms 間隔でポーリングするため最大 25ms の量子化が入る（本番と同じ）。
- 段階内訳 `GEOFRAME_CI_TIMING`: `single_cli` だけ stderr から取得（`create/configure/graph/render/encode_write/other/total`）。
  `rust_develop`（stderr を捨てる）と batch（`BatchSession` の stderr_tail が非公開）は**欠測**で、応答の `elapsed_ms`（decode/render_encode/total）で代用する。
  捏造しない。必要なら `BatchSession` に cfg(test) の取り出し口を足す（本番現像の変更なし。判断は親）。
- RSS: `single_cli` は別走行（計時対象外）で `/usr/bin/time -l` の最大 RSS / peak memory footprint。batch は各要求直後の `ps` サンプル
  （要求境界の下限値でピークではない）。`rust_develop` は欠測（子は `single_cli` と同じ）。ベンチ本体の RSS も開始/終了で記録。
- エラー・timeout・session 再起動は panic せず要求単位で記録し、系列を続行する（batch は kill 後に次の要求の前で再起動し `restarts` を数える）。

## 5. 出力 RGB の比較

`single_cli` の出力 JPEG をデコードした RGB を基準に、他方式の最終反復の出力をステップごとに比較し、
`rawpixels_identical` / `bytes_identical` / `max_abs_diff` / `mean_abs_diff` / `differing_pixels` を記録する。**合否にしない**（画質閾値未決）。

期待: `rust_develop_*` と `batch_stateless` は同じ CLI 経路なので画素一致の見込み。`batch_decode_render` は保持画素が half float のため、
README に記載のとおり 8bit JPEG で 1 段以内の差が出うる（`DecodeCacheTests`）。差があっても失敗扱いにせず、そのまま記録する。

## 6. 素材・保管場所の記録（公平比較）

素材は列挙も自動現像もしない。**撮影 ID・現像元フォルダ・CLI・出力先はすべて環境変数で明示する。**
ローカルと SMB の比較は、同じ ARW をローカルへコピーしたフォルダと SMB 上のフォルダを `SOURCE_ROOT` だけ替えて 2 回実行する。

JSON の `environment.storage` に、ユーザー付与の `user_label`（必須。例 `local` / `smb-backup`）と、`/sbin/mount` の最長一致から得た
`detected_fstype` / `detected_is_network`、ラベルと食い違うときの `label_mismatch_warning`、パスの短いハッシュを残す。
デバイス名（`user@host`）・マウントパス・ARW パスは記録しない。

## 7. 実行コマンド（親が実行）

実 CLI が必要。`src-tauri/binaries/` が `exit 1` のスタブのままだと、テストは「実 CLI ではない」と明示エラーで止まる。

```sh
npm run build:coreimage     # 実 CLI を src-tauri/binaries/ へ（または親の release CLI を SIDECAR で指定）

# ローカル素材（ARW を手元へコピーしたフォルダ）
GEOFRAME_TRANSPORT_BENCH_SIDECAR=<実CLIのパス> \
GEOFRAME_TRANSPORT_BENCH_SOURCE_ROOT=<ARWのあるフォルダ> \
GEOFRAME_TRANSPORT_BENCH_CAPTURES=DSC00001,DSC00002 \
GEOFRAME_TRANSPORT_BENCH_STORAGE_LABEL=local \
GEOFRAME_TRANSPORT_BENCH_OUT=work/issue141/transport-local \
GEOFRAME_TRANSPORT_BENCH_WARM=3 \
  cargo test --release --manifest-path src-tauri/Cargo.toml coreimage_transport_bench -- --ignored --nocapture --test-threads=1

# SMB 素材: SOURCE_ROOT / STORAGE_LABEL(=smb-backup) / OUT を替えて同じ撮影 ID で再実行
```

| 環境変数（接頭辞 `GEOFRAME_TRANSPORT_BENCH_`） | 必須 | 既定 | 内容 |
|---|---|---|---|
| `SOURCE_ROOT` | ✓ | — | ARW のフォルダ（`config.json` はベンチが出力先の app_data に作る。アプリの設定には触れない） |
| `CAPTURES` | ✓ | — | `DSC00000` 形式をカンマ区切り |
| `STORAGE_LABEL` | ✓ | — | 保管場所のユーザーラベル |
| `OUT` | ✓ | — | 出力先（JSON・画像・app_data）。`work/` 配下（gitignore 済み）推奨 |
| `SIDECAR` | | `src-tauri/binaries/geoframe-ci-develop-aarch64-apple-darwin` | 実 CLI |
| `WARM` | | 3 | warm 反復数（cold は常に 1） |
| `TIMEOUT_S` | | 60 | 要求ごとのタイムアウト（本番と同じ） |
| `SERIES` | | 全系列 | 系列名をカンマ区切りで絞る |
| `METHODS` | | 全方式 | 方式名をカンマ区切りで絞る |
| `RSS_PROBE` | | 1 | `0` で `/usr/bin/time -l` の別走行を省く |
| `CI_TIMING` | | 1 | `0` で `GEOFRAME_CI_TIMING` を子へ渡さない |

スモーク（1 撮影 ID・1 系列）: `SERIES=post_shadow_lift WARM=1`。実行は 1 スレッド（`--test-threads=1`。`GEOFRAME_CI_TIMING` を環境へ設定するため）。

期待される出力: `<OUT>/transport-bench-<label>-<unix>.json`（スキーマ `geoframe.transport-bench.v1`）と標準出力の「結果: <パス>」。
`<OUT>/images/`（方式×系列×ステップの JPEG）と `<OUT>/app_data/`（`rust_develop` 用）はコミットしない。
JSON は `captures[].series[].methods.<method>` に `phases.{cold,warm}.{request_wall_ms,series_wall_ms,transport_overhead_ms,cli_total_ms,ci_timing_ms}`、
`errors`/`timeouts`/`restarts`/`rss`、`rgb_vs_single_cli[]`、`requests[]`（要求ごとの生値）を持つ。

fake CLI の契約テスト（実 CLI 不要）: `cargo test --manifest-path src-tauri/Cargo.toml transport_bench`。

## 8. Stage1: ボタン → 画像表示の計測フック

```js
// devtools（Tauri dev）
localStorage.setItem("geoframe.previewTiming", "1");   // ON（次のクリックから）
// …「プレビュー現像」を押す…
JSON.stringify(window.__geoframePreviewTimings)          // 最大 200 行。console にも 1 行ずつ出る
localStorage.removeItem("geoframe.previewTiming");      // OFF（既定）
```

既定 OFF。OFF では `begin()` が null を返し、`<img onLoad>` も何もしない。ON でも画像の先読み・再描画・キャッシュ変更はしない
（表示中の `<img>` の load を観測するだけ）。起点は **「プレビュー現像」ボタンのハンドラ（`App.tsx: runDevelop`）の入口**で取った時刻で、
`developPreviewCoreImage` の第 3 引数 `{ handlerEntryMs }` として渡す（`runDevelop` の先頭 1 行と呼び出し引数だけの局所変更。
解析・候補・合成のコードには触れない）。この起点が渡されない呼び出しは `origin: "api_call"` / `kind: "api_call_to_image"` になり、
**ボタン→画像とは呼ばない**（ハンドラ前段は未計測）。1 クリックにつき 1 行（`request_id` で突合）:

| フィールド | 意味 |
|---|---|
| `request_id` / `capture_id` | 突合用の ID と撮影 ID（パスではない） |
| `origin` / `kind` | `button_handler` / `manual_preview_button_to_image`（ハンドラ入口が起点）、または `api_call` / `api_call_to_image` |
| `pre_invoke_ms` | ハンドラ入口→invoke 開始（`dispatch` など）。`api_call` では null |
| `invoke_wall_ms` | `develop_preview_coreimage` の invoke 開始→戻り（Rust の枠待ち・キャッシュ・CLI・IPC を含む） |
| `invoke_to_load_ms` | 戻り→表示中 `<img>` の load |
| `load_to_decode_ms` | `img.decode()` |
| `decode_to_raf_ms` | decode 後、2 回目の `requestAnimationFrame` まで |
| `invoke_start_to_raf_ms` | invoke 開始→2 回目の rAF |
| `origin_to_raf_ms` | 起点（`origin` の示すもの）→2 回目の rAF = `pre_invoke_ms + invoke_start_to_raf_ms`。ボタン→画像の主指標 |
| `cache_hit` / `cli_elapsed_ms` / `invoke_minus_cli_ms` | キャッシュヒット時は `invoke_minus_cli_ms` を出さない |
| `width` / `height` | 返却された画像の寸法 |
| `status` | `ok` / `invoke_error` / `image_not_observed`（4 秒以内に load が来ない: 同じ src が表示済みで load が再発火しない場合など）/ `decode_failed` |

**`<img>` の load を paint 完了と断定しない。** `origin_to_raf_ms` は「decode が終わり 2 フレーム進んだ」までの **paint の代理観測**で、
実際の合成・表示は確認していない（`paint_proxy` に明記）。

記録しない: 表示 URL / `output_url`（絶対 APPDATA パスを含む）・パス・GPS・画像の値・調整値。URL は `<img>` の load との突合に
モジュール内部でだけ使い、公開する行・`window.__geoframePreviewTimings`・console には出さない。送信もしない。
クリックイベント→ハンドラ入口の入力遅延（`event.timeStamp`）と、スライダー入力の応答は測らない（画像 E2E に混ぜない）。

## 9. 結果の解釈（FFI 判断への入口）

実測後に埋める。判断の材料は次の順に読む。

1. `single_cli` の `transport_overhead_ms`（warm）と `cli_total_ms` の比。overhead が cli の数 % なら、輸送層（プロセス起動/JSON/ポーリング）を
   削っても E2E はほとんど動かない。`ci_timing_ms` の `render` が支配的なら Core Image 本体の問題で FFI では縮まない。
2. `batch_stateless` − `single_cli`（プロセス起動の節約分）と、`batch_decode_render` の `decode_reused` 要求（後段だけ）の wall。
3. Stage1 の `pre_invoke_ms` と `invoke_minus_cli_ms`（IPC・枠・書き込み）と `invoke_to_load_ms`/`load_to_decode_ms`（WebView 側）。
4. `rust_develop_cache_hit` は輸送ではなく Rust キャッシュの下限として別枠で読む。
5. SMB とローカルの差（`STORAGE_LABEL` 違いの 2 実行）。

| 項目 | 結果 |
|---|---|
| ローカル / SMB 各方式の p50/p95 | 未測定 |
| `transport_overhead_ms` の割合 | 未測定 |
| `batch_decode_render` の RGB maxdiff | 未測定 |
| ピーク RSS | 未測定 |
| Stage1 の ボタン→rAF（`origin_to_raf_ms`） | 未測定 |
| FFI PoC への進否 | 未判断 |
