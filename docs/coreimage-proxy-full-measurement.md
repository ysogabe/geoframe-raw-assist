# Core Image 代理 vs 原寸 計測プロトコル（#57）

候補生成の代理（640px Core Image）で測った指標が、同じ adjustments を原寸で現像したときと
どれだけ・どの向きにずれるかを、**本番のアルゴリズムと閾値を変えずに**再現可能な形で測る。
旧 Python（rawpy）の閾値は移植しない。差がここで再現されるまで本番の調整もしない。

実装: `src-tauri/src/candidates/proxy_full_measurement.rs`（`#[ignore]` の release テスト）。
指標は Rust の純粋関数（`candidates::metrics::image_metrics` / `candidates::stars::star_measurements`）だけで
計算し、`local_raw` は使わない。

## 実行

```sh
# 1. 単体テスト + ビルド確認（sidecar 不要）
CARGO_TARGET_DIR=<自前の target> env -u CI \
  cargo test --release --manifest-path src-tauri/Cargo.toml proxy_full_measurement

# 2. 実測（stars 11 件。src-tauri/binaries/ に externalBin が必要）
CARGO_TARGET_DIR=<自前の target> env -u CI \
  GEOFRAME_PSO_RAW_ROOT=/Users/yoshio/works/60-misc/40-raw-assistant/work/raw \
  GEOFRAME_PSO_SIDECAR=<repo>/src-tauri/binaries/geoframe-ci-develop-aarch64-apple-darwin \
  cargo test --release --manifest-path src-tauri/Cargo.toml \
  coreimage_proxy_full_measurement -- --ignored --nocapture

# 3. Python 側の集計で検算（cases/*.json は evaluate_candidates.py の record 形式）
.venv/bin/python scripts/evaluate_candidates.py --aggregate-only --out work/issue57/proxy-full
```

| 環境変数 | 既定 | 意味 |
|---|---|---|
| `GEOFRAME_PSO_RAW_ROOT` | 必須 | manifest の `stars` root |
| `GEOFRAME_PSO_SIDECAR` | `src-tauri/binaries/geoframe-ci-develop-aarch64-apple-darwin` | Core Image CLI |
| `GEOFRAME_PROXY_MEASURE_OUT` | `work/issue57/proxy-full` | 出力先（git 管理外） |
| `GEOFRAME_PROXY_MEASURE_FORCE` | 0 | 前回の `cases/ measurements/ app_data/ scratch/` を消して再実行（消せなければ停止） |
| `GEOFRAME_PROXY_MEASURE_BUDGET` | 48 | 候補生成の budget（アプリ既定と同じ） |
| `GEOFRAME_PROXY_MEASURE_SEED` | なし | 未指定なら `pso::default_seed(capture, preset)`（アプリと同じ）。値は記録する |
| `GEOFRAME_PROXY_MEASURE_CASES` | 全件 | `3`（先頭 3 件）または `DSC07402,DSC07404` |
| `GEOFRAME_PROXY_MEASURE_1280` | 1 | 1280 代理と 1280 格子を測る |
| `GEOFRAME_PROXY_MEASURE_SWEEP` | 1 | sharpness / NR 掃引を測る |
| `GEOFRAME_PROXY_MEASURE_IMAGE_WORK_ROOT` | なし | 昼夜ミックス 13 件（manifest `image_work`）。素材待ちのため未指定時は `provenance.json` の `pending_cases` に記録だけする |
| `GEOFRAME_PROXY_MEASURE_AGGREGATE_ONLY` | 0 | `measurements/*.json` から要約だけ作り直す |

選択ケースが 0 件、ケース単位の失敗、描画・指標・CLI セッション終了のエラーが 1 件でもあればテストは失敗する
（出力は残す）。

## 出力

| パス | 内容 | コミット |
|---|---|---|
| `cases/<root>__<ID>__<preset>.json` | `evaluate_candidates.py` の record 形式。`metrics` = 生成時の 640 代理、`raw_metrics` = 原寸を 640 格子へ縮小した指標、`raw_star_ungated`、`raw_timing`（`t_total`/`t_decode` = CLI の elapsed 秒）。`raw_engine: "coreimage_full_area640"` | 可（数値のみ） |
| `measurements/<key>.json` | 全スケール × 全集合 × ROI の生測定、集合の adjustments（代理用 / 原寸用）、ROI 座標、renderer 統計、USM 等の `adjustments_applied.post`、再現性、エラー | 可 |
| `provenance.json` | commit / branch / `git status --porcelain` / rustc / macOS / CLI `--version` と実行ファイル SHA-256 / manifest SHA-256 / 設定 / 素材待ちケース / ケースエラー | 可 |
| `proxy_full_summary.{json,md}` | 下記の集計 | 可 |
| `app_data/<key>/coreimage/full/*.jpg` | 原寸現像の実画像（アプリと同じく EXIF 継承あり） | **不可** |

ソースの来歴は ARW と、星判定（`exif_hints`）・原寸 EXIF 継承（`<ID>.JPG`）が読むペア JPG の
ファイル名・SHA-256・バイト数だけを記録する。EXIF の値・GPS・画像は記録しない。絶対パスも残さない。

## 現像プロトコル

| スケール | 経路 | 設定 |
|---|---|---|
| `proxy640` | `candidates::Renderer`（本番の候補評価と同一。decode を集合間で共有） | preview / `max_edge` 640 / decoder 9 / `apple_default` / lens on / `jpeg_quality` 1.0 |
| `proxy640_qcli` | 同上（対照） | `jpeg_quality` を省略（CLI 既定 0.92 = 原寸と同じ） |
| `proxy1280` | `Renderer::new_with_edge`（計測専用） | `max_edge` 1280、他は proxy640 と同じ |
| `full_down640` / `full_down1280` | `coreimage::develop(DevelopMode::Full)`（`develop_full_coreimage` と同じ） | `max_edge` なし / `jpeg_quality` 省略 / adjustments は `normalize_adjustments` 済み。出力 8bit JPEG を同じ集合の代理と同寸へ縮小 |
| `full_native` | 同じ原寸 JPEG | ROI だけを原寸のまま切り出す |

原寸はケースごとの `app_data/<key>/`（`config.json` の `source_root` = RAW root）に隔離し、キャッシュ上限の
prune が計測中の出力を消さないようにした。各原寸 JPEG は現像直後に読み、縮小と ROI 切り出しをしてから次へ進む
（24MP 全体に `image_metrics` を掛けない。全体画像は 8bit で 1 枚だけ保持）。

**縮小**: 面積平均（出力画素が覆う入力区間 `[o·s, (o+1)·s)` の被覆率で重み付け。cv2.INTER_AREA と同じ定義）。
8bit sRGB の符号化値 /255 のまま、線形化しない。CI 内部の preview 縮小（作業色空間・フィルタ非公開）とは
別物なので、`proxy640` と `full_down640` の natural 同士の差にはこの縮小法の差も含まれる。

## 測る集合

- **候補**: その時点の本番経路 `command::generate` が返した候補（natural が先頭）。seed / budget は明示して記録。
- **掃引**（natural の adjustments から 1 変数だけ変える）:
  - sharpness ∈ {0.8, 1.0, 1.47, 1.6}。契約の下限は 0.8（`normalize_adjustments` が 0 を 0.8 に丸める）ので 0 は測れない。
    < 1 は decoder 内シャープ（decode 鍵）、> 1 は後段 USM の増分。
  - noise_reduction ∈ {off, light, full}（契約の語彙。"standard" は無い）。nightscape は natural が full なので
    `sweep_nr_full` は natural と同一 = 決定性の確認になる。

## 指標の定義

- **全体指標**: 各スケールで、そのスケールの natural を `baseline`、natural の輝度 25% 分位以下を暗部マスクにして
  `image_metrics`（`Evaluator` と同じ）。星ゲートは候補生成と同じ（natural = 星判定の結果、それ以外は `stars` のみ）。
  `star_ungated` は `star_measurements(.., evaluate_gate=false)`。
- **ROI**: natural の 640 代理から正規化座標で決定的に選び、全スケール・全集合に同じ座標を使う。一辺は長辺の 12.5%
  （640 で 80px、6000 で 750px）。`center` / `detail`（平均勾配最大）/ `flat_dark`（輝度下位 25% のタイルで勾配最小、
  白飛び < 1%）/ `flat_mid`（平均輝度 0.2–0.8 で勾配最小）。ROI 内は ROI の natural を基準に `image_metrics`（星ゲートなし）と:
  - `grad_mean`: |∂x|+|∂y| の平均（画素単位）
  - `band_px`: 輝度 − Gaussian(σ=1.1px) の標準偏差（`image_metrics` の残差と同じ画素帯域）
  - `band_matched`: σ = 1.1 × 長辺 / 640。640 代理の 1.1px と同じ「画面上の周波数」帯域
  ROI の絶対値はスケール間で比べず、natural 比 Δ の向きと相関だけを比べる。星指標は面積比・絶対面積の閾値を持つため
  ROI では計算しない（全体 640 格子のものを主に使う）。

## 集計（`proxy_full_summary`）

- **全体** `a__vs__b`（proxy640↔full_down640、proxy1280↔full_down1280、proxy640↔proxy1280、
  proxy640↔proxy640_qcli、full_down640↔full_down1280）× 指標: 候補の絶対値の n / bias(b−a) / MAE /
  写真内スピアマン平均、natural 比 Δ の bias / Pearson / Spearman / 向き不一致（両方 |Δ| > 1e-4 で符号が逆）/
  片側のみ変化（一方だけ |Δ| > 1e-4）。
- **ROI** proxy640 / proxy1280 / full_down640 ↔ full_native: ROI × 指標 × 集合種別（候補 / 掃引）の Δ の比較。
- **掃引**: 掃引集合 × スケールの natural 比 Δ のケース平均（全体の detail / noise_flat / halo / source_deviation、
  ROI の band_px / band_matched / noise_flat / halo）。
- `proxy640_rerender_max_abs_diff_vs_generation`: 640 再現像の指標と、候補生成時の指標の最大差（再現性）。

## 既知の交絡（結果を読むときの注意）

1. **USM の核自体がスケールで違う**。`captureSharpenBase(longEdge:)` は decode 後の長辺から σ / gain を決める
   （smoke では 640: σ 0.6・gain 0.0075、6000: σ 0.8・gain 0.0105 @ sharpness 1.02）。sharpness > 1 の応答差は
   解像度だけでなく核の違いも含む。各描画の `applied.usm` を `measurements/*.json` に残してある。
2. **JPEG 品質**: 代理は 1.0、原寸は CLI 既定 0.92。`proxy640_qcli` で寄与を分ける。
3. **縮小法**: 上記のとおり面積平均（sRGB 符号化値）。CI の preview 縮小とは別。
4. **正規化**: 原寸は `normalize_adjustments` を通す（アプリと同じ）。代理は生成時の値のまま。差があれば
   `sets[].normalization_changed` が true（JSON の数値表記差でも true になる）。
5. 原寸の EXIF 継承は `<ID>.JPG`（大文字）だけを読む。`paired_jpg[].used_for_full_exif_inherit` と
   `extra.exif_inherited` を参照。

## smoke（参考。結果ではない）

2026-10-23、DSC07402:nightscape 1 件、budget 32、CLI 0.8.0（f3e24b0 からの再ビルド）、macOS 27.0.1。68 秒、
エラー 0、640 再現像は生成時指標と完全一致。全体 detail の natural 比 Δ（sharpness 0.8）が proxy640 −0.0002 に対し
full_down640 −0.0053、proxy1280 −0.0071 で、#57 コメントの「640 では decoder sharpness < 1 が見えない」を再現した。
数値の結論は 11 件の本計測で出す。

## 未計測

- 昼夜ミックス 13 件（manifest `image_work` root）は素材待ち。届いたら `GEOFRAME_PROXY_MEASURE_IMAGE_WORK_ROOT` を
  付けて同じコマンドで測る。

## 本計測結果

（親が 11 件の実測後に `proxy_full_summary.md` から転記する）
