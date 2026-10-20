# Core Image 現像の回帰確認手順（OS 更新時）

Issue #75。[ADR 0002](adr/0002-coreimage-engine-decision.md) で現像エンジンの基本設定を
Core Image（CIRAWFilter）の **`apple_default`・decoder 9・レンズ補正 ON** に決めた。
`apple_default` はデコーダ内のシャープ・NR などを OS が決める値のまま使うため、**macOS を更新すると画が変わりうる**。
この手順で、更新のたびにベースラインとの差を確かめる。

## 1. ベースライン（2026-10-20 確定）

| 項目 | 値 |
|---|---|
| macOS | 27.0.1（Apple M3）。対応 OS は最新の macOS のみ（古い macOS の個別検証はしない） |
| CLI | `geoframe-ci-develop` 0.1.0（`native/coreimage-cli`）。0.2.0（#79、個別上書きキーと tiff16 の sRGB 出力を追加）でも既定リクエストの `applied` は同一（差分 0 件を確認済み） |
| リクエスト | `"decoder_version": "9"`, `"settings": {"profile": "apple_default", "lens_correction": true}` |
| 実効パラメータ | [`coreimage-baseline/applied-params-macos-27.0.1.json`](coreimage-baseline/applied-params-macos-27.0.1.json)（α6400 夜間・長秒 11 カット、ISO 3200 / 6400） |
| 画質指標 | [比較レポート §9.5](coreimage-engine-comparison.md) の `CI apple_default v9 lens ON`（下表に再掲） |

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
- CLI のリクエスト・既定値を変えた PR（3.1 と 3.2）。

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

- 撮影 ID とコマンドは [比較レポート §9.2](coreimage-engine-comparison.md) と同じにする。
- `sharpness_summary.json` の `by_mode` から `ci_apple_v9_l1` の値を取り、1. の表と比べる。
- `sheets/` の等倍クロップ（`detail1` / `detail2` / `corner`）を、前回の同じファイルと並べて目視する。
  特に見る点: 細部の水彩状の平滑化、白い点状のノイズ、偽色、星（夜景カット）の消失。
- 注意: 現在の比較ハーネスは rawpy の現像も同時に行う（`.venv` が必要）。rawpy を取りやめた後は
  Core Image のみで回すモードが必要になる（ADR 0002 の移行作業で対応する）。

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

## 4. 記録

結果は、この文書の末尾の「回帰確認の記録」に 1 行ずつ追記する。実効パラメータに差があった場合は、
新しい OS のバージョンで `coreimage-baseline/applied-params-macos-<version>.json` を追加し、合格ならそれを新しいベースラインにする。

| 日付 | macOS | CLI | 3.1 applied | 3.2 指標 | 目視 | 判定 | 備考 |
|---|---|---|---|---|---|---|---|
| 2026-10-20 | 27.0.1 | 0.1.0 | ベースライン作成 | §9.5 の値 | §9.6 | ベースライン | Issue #74 / #75 |
