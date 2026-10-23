# Core Image 現像の回帰確認手順（OS 更新時）

Issue #75。[ADR 0002](../adr/0002-coreimage-engine-decision.md) で現像エンジンの基本設定を
Core Image（CIRAWFilter）の **`apple_default`・decoder 9・レンズ補正 ON** に決めた。
`apple_default` はデコーダ内のシャープ・NR などを OS が決める値のまま使うため、**macOS を更新すると画が変わりうる**。
この手順で、更新のたびにベースラインとの差を確かめる。

## 1. ベースライン（2026-10-20 確定）

| 項目 | 値 |
|---|---|
| macOS | 27.0.1（Apple M3）。対応 OS は最新の macOS のみ（古い macOS の個別検証はしない） |
| CLI | `geoframe-ci-develop` 0.1.0（`native/coreimage-cli`）。0.2.0（#79、個別上書きキーと tiff16 の sRGB 出力を追加）、0.3.0（#73、`adjustments` と応答の `adjustments_applied` を追加）、0.4.0（#92、診断モード `diagnostics` と `settings.gamut_mapping` / `baseline_exposure` を追加）、0.5.0（#82、`--batch` を追加）でも、既定リクエスト（`adjustments` / `diagnostics` 無し）の `applied` は同一（差分 0 件を確認済み） |
| リクエスト | `"decoder_version": "9"`, `"settings": {"profile": "apple_default", "lens_correction": true}` |
| 実効パラメータ | [`coreimage-baseline/applied-params-macos-27.0.1.json`](../coreimage-baseline/applied-params-macos-27.0.1.json)（α6400 夜間・長秒 11 カット、ISO 3200 / 6400） |
| 画質指標 | [比較レポート §9.5](../reports/coreimage-engine-comparison.md) の `CI apple_default v9 lens ON`（下表に再掲） |

実効パラメータ（CLI 応答の `applied`）の要点（ISO ごとに OS が決める値）:

| ISO | sharpness_amount | luminance_noise_reduction_amount | color_noise_reduction_amount |
|---|---|---|---|
| 3200 | 1.0 | 0.241 | 0 |
| 6400 | 0.806〜0.826 | 0.357 | 0 |

画質指標（カメラ JPEG 比。§9.5 から再掲。カット中央値）:

| モード | edge 幅比 | ノイズ比 | lap 比 | HF 比 |
|---|---|---|---|---|
| preview（長辺 2400px、29 カット） | 0.95 | 1.02 | 1.05 | 1.28 |
| full（原寸、12 カット） | 0.84 | 0.81 | 1.06 | 0.94 |

処理時間・メモリの目安（macOS 27.0.1 / M3、ISO 6400 の 1 カット）: preview 約 1.0 秒・RSS 約 170 MB、
full 約 2.0 秒・RSS 約 270 MB。

## 2. いつ行うか

- macOS のメジャー / マイナー更新の後（セキュリティ更新だけのときは 3.1 のみでよい）。
- Xcode / Swift の更新で CLI を作り直した後（3.1 のみ）。
- CLI のリクエスト・既定値を変えた PR（3.1 と 3.2）。`adjustments` の写像・後段・NR 表を変えた場合は 3.4 も行う。
- 旧エンジン（rawpy）の基準と比べたいとき（3.6。rawpy 自体は回さない）。

## 3. 手順

### 3.1 実効パラメータの差分（数分）

OS が決める値（シャープ・NR・色温度の推定など）が変わったかを最初に見る。

```sh
npm run build:coreimage
scripts/coreimage-regression-applied.sh <ベースラインと同じ ARW のフォルダ>
# 例: scripts/coreimage-regression-applied.sh ~/works/60-misc/40-raw-assistant/work/raw
```

スクリプトは各 ARW を基本設定で preview 現像し（出力は `work/regression/<macOS のバージョン>/`、gitignore 済み）、
CLI 応答の `applied` をベースライン JSON の同じ撮影 ID と比べて、違う項目を 1 行ずつ出す
（`orientation_handling` と `scale_factor` は比べない）。差分が 0 件なら終了コード 0。
別のベースラインと比べるときは第 2 引数に JSON を渡す。

- **全項目が一致** → 3.2 は省略してよい（OS が同じ値を選んでおり、デコーダ本体の変更だけが残るリスク）。
  ただしメジャー更新のときは 3.2 も行う。
- **差がある** → 3.2 を行い、差分を下記 4. の記録に残す。

### 3.2 画質指標と目視（十数分）

比較ハーネスの解像感モードを、ベースラインと同じカットで再実行する。

```sh
.venv/bin/python scripts/compare_engines.py --source-root <ARW のフォルダ> \
  --sharpness --captures <ベースラインと同じ撮影 ID> \
  --full --full-ids <full を回す撮影 ID> \
  --out work/regression/$(sw_vers -productVersion)/sharpness
```

- 撮影 ID とコマンドは [比較レポート §9.2](../reports/coreimage-engine-comparison.md) と同じにする。
- `sharpness_summary.json` の `by_mode` から `ci_apple_v9_l1` の値を取り、1. の表と比べる。
- `sheets/` の等倍クロップ（`detail1` / `detail2` / `corner`）を、前回の同じファイルと並べて目視する。
  特に見る点: 細部の水彩状の平滑化、白い点状のノイズ、偽色、星（夜景カット）の消失。
- 比較ハーネスの既定は **Core Image のみ**（カメラ JPEG と CI のバリアント）で、rawpy の列は作らない。
  Python 環境は評価用の `requirements-dev.txt`（NumPy / Pillow / OpenCV）だけでよく、rawpy・`local_raw/` は不要。
  §9 の表にある rawpy の列は過去の記録で、再生成はしない（旧エンジンとの比較が要るときは 3.6 の凍結基準を使う）。
- 2026-10-23 より前の記録（4. の表・比較レポートの数値）は rawpy 列を含むハーネスで取ったもの。CI の列はそのまま比べてよい。

### 3.3 合否の目安

| 指標 | 合格の目安（ベースラインからの変化） |
|---|---|
| `applied` の値 | 一致。違うときは 3.2 の結果で判断する |
| edge 幅比（preview / full） | ±0.05 以内 |
| ノイズ比（preview / full） | ±0.15 以内 |
| 輝度（カメラ JPEG との平均輝度差） | ±0.03 以内 |
| 目視 | 平滑化・白点ノイズ・偽色・星の消失が新たに出ていない |
| 処理時間・メモリ | 1.5 倍以内 |

目安を外れたら、すぐに不合格とはしない。まず次を判断し、Issue を立てる。

- 画が良くなった方向（エッジが細く、ノイズが減った）の変化か。
- 後段処理（Adjustments のシャープ・NR）で吸収できる変化か。

### 3.4 Adjustments の写像の確認（CLI 0.3.0 以降。数分〜十数分）

`adjustments`（9 キー）の写像は `apple_default` の既定値基準の相対値で、OS が既定値を変えると効き方（NR full の量、sharpness、WB の効き）がずれうる。
3.1 で `applied` に差があった場合、または写像・後段を変えた場合に、`--adjustments` モードのハーネスを回す。

```sh
.venv/bin/python scripts/compare_engines.py --source-root <ARW のフォルダ> \
  --captures <撮影 ID> --adjustments \
  --out work/regression/$(sw_vers -productVersion)/adjustments
```

- 撮影 ID・手順・閾値は [比較レポート §16](../reports/coreimage-engine-comparison.md)（昼 6 + 星空 3 カットで全チェック合格が基準。閾値は `scripts/compare_engines.py` の `ADJ_*`）。
- 後段オラクル（tiff16 sRGB、ΔE76 中央値・チャンネル差 p99）、方向・単調、NR full の星の残存率・空の高周波、白点（`impulse_vs_full` ≤ 10）、処理時間が見える。
- 不合格は、写像の定数（`MIRED_PER_UNIT`、NR の `fullLuminanceCap` など）の較正か、指標の見直しかを §16.3 の分類（目安が非現実的 / 定数の較正 / 実装のバグ / rawpy 側の基準差）で判断する。
- **注意（2026-10-23〜）**: `--adjustments` の実行は rawpy の掃引と Python の後段オラクルを使う**旧経路（アーカイブ）**で、
  `--legacy-rawpy` の明示が必要になった（`--dry-run` によるリクエストの契約検査だけは不要）。Python 本体（`local_raw/`）の削除後は、
  削除前のコミットを checkout し、その時点の `requirements-raw.txt` の環境を用意して `--legacy-rawpy` を付けて回す。
  通常の OS 更新では 3.1・3.2・3.5 と 3.6 の凍結基準との比較を先に行い、写像そのものを変えたときにだけこの旧経路を使う。
- CLI 0.6.0（#81）からは `adjustments_ignored_empty`（`ignored` が空）がチェックに入る。オラクル `or_preset` は CI 値（`src-tauri/tests/fixtures/presets.json`）の既定プリセットを hr 0 で使う。

### 3.5 プリセットと `highlight_recovery` の写像の確認（CLI 0.6.0 以降。数十分）

`highlight_recovery` の写像の節点（hr 60 / 90）は macOS 27.0.1 の `apple_default` で計測した C1' に結び付いている。OS が `apple_default` のトーンを変えると、
写像の効き（白飛びの減り・中間調）とプリセットの見え方がずれうる。3.1 で `applied` に差があった場合に `--presets` モードを回す。

```sh
.venv/bin/python scripts/compare_engines.py --source-root <ARW のフォルダ> \
  --captures DSC04473 DSC04607 DSC04532 DSC04705 DSC04579 DSC04604 DSC04754 DSC04432 DSC04746 DSC04462 \
             DSC04670 DSC04651 DSC04872 DSC04430 DSC04453 \
  --presets --full --out work/regression/$(sw_vers -productVersion)/presets
# 夜景 3 カットは別フォルダ: --captures DSC07415 DSC07407 DSC07402 --presets
```

- 見るもの: `index.md` の §2.2 の等価性（条件 9。tiff16 の max abs diff 0 と `applied` の一致）と、救済系 3 プリセットの条件 1〜5（hr 0 比で白飛びが減り、中間調 ±0.03）。
- 手順・閾値・合否の定義は [再調整の設計書](../design/coreimage-preset-retune.md) §7.4、結果の記録は [再調整レポート](../reports/coreimage-preset-retune-report.md)。
- `--presets` は既定の Core Image のみの経路で回る（rawpy の列は作らない）。

### 3.6 凍結基準との比較（rawpy 削除後の旧エンジン比較。数分〜）

rawpy（旧エンジン）の画は、Python 本体の削除前に**凍結した基準画像**（ハッシュ・寸法つきのマニフェスト）として残し、
現在の Core Image の出力と同じ寸法で比べる。凍結物の欠落・改変は失敗として止まる。

- `--frozen-manifest <マニフェスト>` と `--frozen-root <凍結物の基準ディレクトリ>` で、凍結物を照合してから現在の CI で描画して比べる。
- `--frozen-verify-only` は凍結物のハッシュ・寸法の照合だけを行う（CLI・現像元フォルダは不要。このとき `--source-root` は省略できる）。
- `--frozen-capture` は今回の CI 出力を凍結マニフェストとして書き出す（次の OS 更新で `--frozen-manifest` に渡す CI 側の基準になる）。

フラグの正確な意味・凍結物の置き場所・旧経路（`--legacy-rawpy`）との使い分けは、[評価手順](evaluation-paths.md)と
`scripts/compare_engines.py --help` を正とする（この節はそれらの要約で、食い違うときはそちらに従う）。

## 4. 記録

結果は、この文書の末尾の「回帰確認の記録」に 1 行ずつ追記する。実効パラメータに差があった場合は、
新しい OS のバージョンで `coreimage-baseline/applied-params-macos-<version>.json` を追加し、合格ならそれを新しいベースラインにする。

| 日付 | macOS | CLI | 3.1 applied | 3.2 指標 | 目視 | 判定 | 備考 |
|---|---|---|---|---|---|---|---|
| 2026-10-20 | 27.0.1 | 0.1.0 | ベースライン作成 | §9.5 の値 | §9.6 | ベースライン | Issue #74 / #75 |
| 2026-10-21 | 27.0.1 | 0.3.0 | 差分 0 件（`adjustments` 無し） | `--adjustments` で昼 6 + 星空 3 カット全合格（§16.5） | §16 | 合格 | Issue #72 / #73。`adjustments` 写像の較正後（NR full 上限 0.5） |
| 2026-10-21 | 27.0.1 | 0.4.0 | 差分 0 件（`diagnostics` 無し） | 対象外（診断モードの追加のみ。`diagnostics` 無し と `{}` で tiff16 バイト一致） | — | 合格 | Issue #92。診断モード（CLI 0.4.0）の追加 |
| 2026-10-21 | 27.0.1 | 0.5.0 | 差分 0 件 | 対象外（`--batch` の追加のみ。バッチと単発で tiff16 バイト一致・設定の持ち越し無しを `BatchTests` で確認） | — | 合格 | Issue #82。バッチモード（CLI 0.5.0）の追加 |
