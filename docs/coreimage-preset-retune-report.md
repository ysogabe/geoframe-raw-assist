# プリセット再調整と `highlight_recovery` の写像 — 計測レポート（Issue #81）

- 設計: [設計書](design/coreimage-preset-retune.md)（§7 手順・§7.4 合否・§8 シート、§11 オーナー判断 2026-10-22）。
- 実施: 2026-10-22、macOS 27.0.1 / Apple M3、CLI 0.6.0、Python 3.13 + rawpy 0.27.0。
- **状況: 実装は完了。§7.3 の計測（10 + 8 カット）と §8 のシートは未完。** サンプルのボリューム（`/Volumes/Backup/100MSDCF`）が
  未マウントで、#92 の 10 カット・日中 3 カット・室内 2 カットを読めなかった。手元にある夜景 3 カット（ローカルの評価用コピー）で
  ハーネスのスモークと条件 8・9 だけを回した。**反復 0 / 3 回。プリセット値は設計書 §7.2 の初期案のまま。**
- 位置情報は書かない。シート・出力は `work/preset-retune/`（gitignored）に置き、画像はコミットしない。

## 1. 完了条件（§7.4）の状況

| # | 条件 | 対象 | 状況 |
|---|---|---|---|
| 1 | hr の寄与: `clip8_any(P) < clip8_any(P_hr0)` | 救済系 3 × 空 10 カット | **未実施**（ボリューム未マウント） |
| 2 | hr の寄与: `|mid_p50(P) − mid_p50(P_hr0)|` ≤ 0.03（9/10） | 同 | **未実施** |
| 3 | hr の寄与: `fg_p01(P) − fg_p01(P_hr0)` ≥ −0.02（9/10） | 同 | **未実施** |
| 4 | 偽色: `magenta_rate` ≤ 0.01（preview） | 同 | **未実施** |
| 5 | `d_base` 比: 白飛びが減り中間調 ±0.03（8/10） | 同 | **未実施** |
| 6 | 個性の方向（§7.3 の宣言） | 全プリセット × 日中 3 + 代表 | **一部**（夜景 3 カットのみ。§2.2） |
| 7 | ノイズ: `flat_noise(P) / flat_noise(d_base)` ≤ 1.5 | 実効 lift ≥ 40 × ISO ≥ 1600 | **一部**（夜景 3 カット 12 / 12 合格。室内 2 カットは未実施） |
| 8 | 回帰: identity の後段オラクル・既定リクエストの `applied` 差分 0 | `--adjustments` | **一部合格**（夜景 2 カットで全チェック合格。昼 6 カットは未実施） |
| 9 | 等価性（§2.2）: `(0, 0, hr 60)` ≡ `(−0.7, 40, hr 0)`、hr 90 ≡ `(−1.0, 55)` | 1 カット | **合格**（DSC07415。tiff16 sRGB の max abs diff 0、`applied` 一致。Swift のスモークでも JPEG のバイト一致） |
| 10 | 目視（§8 の 6 組） | オーナー | **未実施**（夜景の組だけシートあり） |

## 2. スモーク（夜景 3 カット、preview 2400px）

- コマンド: `--source-root <夜景のローカルコピー> --captures DSC07415 DSC07407 DSC07402 --presets`。
  出力 `work/preset-retune/smoke-night/`（`index.md`・`metrics.csv`・`checks.csv`・`summary.json`・`tables.md`・`sheets/` 33 枚）。エラー 0 件。
- 3 カットは空の 10 カットに入らないので条件 1〜5 は数えない（n/a）。以下は**参考値**で、合否の判断には使わない。

### 2.1 バリアント別（3 カットの中央値）

| variant | 実効 (EV, lift) | clip8_any | mid_p50 | fg_p01 | dark_mean_luma | mean_saturation | flat_noise | ΔE76 vs d_base |
|---|---|---|---|---|---|---|---|---|
| d_base | — | 0.0006 | 0.411 | 0.188 | 0.235 | 0.034 | 2.47 | 0 |
| neutral_recovery | (0, 15) | 0.0008 | 0.452 | 0.216 | 0.266 | 0.035 | 2.58 | 4.12 |
| neutral_recovery_hr30（案 C） | (−0.35, 35) | 0.0006 | 0.450 | 0.227 | 0.274 | 0.032 | 2.39 | 4.24 |
| highlight_rescue | (−1.0, 55) | 0.0002 | 0.410 | 0.208 | 0.252 | 0.029 | 2.10 | 2.27 |
| highlight_rescue_hr0 | (0, 0) | 0.0006 | 0.409 | 0.187 | 0.235 | 0.033 | 2.47 | 0.56 |
| highlight_rescue_hr100 | (−1.1, 60) | 0.0002 | 0.410 | 0.207 | 0.251 | 0.029 | 2.11 | 2.23 |
| backlit_rescue | (−0.7, 70) | 0.0003 | 0.499 | 0.269 | 0.318 | 0.032 | 2.13 | 8.98 |
| backlit_rescue_hr0 | (0, 30) | 0.0006 | 0.495 | 0.261 | 0.311 | 0.034 | 2.37 | 8.53 |
| sunset_glow | (−0.7, 48) | 0.0005 | 0.439 | 0.215 | 0.262 | 0.043 | 2.34 | 4.06 |
| sunset_glow_hr0 | (0, 8) | 0.0010 | 0.437 | 0.195 | 0.246 | 0.048 | 2.66 | 3.96 |
| sunset_glow_hr70 | (−0.8, 53) | 0.0004 | 0.438 | 0.215 | 0.262 | 0.043 | 2.32 | 4.01 |
| shadow_rescue | (0, 45) | 0.0006 | 0.538 | 0.301 | 0.349 | 0.033 | 1.86 | 12.64 |
| nightscape | (0, 15) | 0.0008 | 0.448 | 0.214 | 0.263 | 0.034 | 2.12 | 4.00 |
| indoor_warm | (+0.1, 18) | 0.0008 | 0.480 | 0.239 | 0.290 | 0.038 | 2.58 | 7.03 |
| landscape_clear | (0, 10) | 0.0013 | 0.432 | 0.182 | 0.239 | 0.038 | 2.82 | 2.43 |
| landscape_clear_weak | (0, 10) | 0.0010 | 0.434 | 0.194 | 0.247 | 0.036 | 2.69 | 2.57 |
| vivid_travel | (0, 10) | 0.0011 | 0.434 | 0.192 | 0.245 | 0.038 | 2.71 | 2.58 |
| monochrome_street | (0, 10) | 0.0003 | 0.435 | 0.188 | 0.242 | 0.000 | 2.74 | 4.54 |
| portrait_soft | (0, 12) | 0.0004 | 0.446 | 0.226 | 0.271 | 0.034 | 2.39 | 3.94 |

- 救済系 3 プリセットは hr 0 比で白飛びが減り、中間調の差は ≤ 0.004、`fg_p01` は +0.008〜+0.021（暗部は沈まない）。夜景は空の白飛びがほぼ無い（0.06%）ので、効きの大きさは空の 10 カットで確かめる必要がある。
- `magenta_rate`（境界の偽色）は星のカットで hr を上げると増える（DSC07415: `d_base` 0.208 → `highlight_rescue` 0.330、DSC07402: 0.005 → 0.011）。
  星の周りの白が減って帯が小さくなり、星自身の色が帯に入る割合が上がるため【推測】。条件 4 は空の 10 カットで数える（星のカットは対象外）が、夜景の点光源は目視で確かめる。

### 2.2 条件 6・7（夜景 3 カットのみ）

| # | プリセット | 合格 / 判定 | 不合格のカット |
|---|---|---|---|
| 6 | highlight_rescue / backlit_rescue / shadow_rescue / indoor_warm / landscape_clear / vivid_travel / monochrome_street | すべて合格 | — |
| 6 | nightscape | 2 / 3 | DSC07402（`dark_mean_luma` −0.007） |
| 6 | portrait_soft | 2 / 4（n/a 2） | DSC07402（`std_luma` +0.005、`edge_width_px` −0.05） |
| 6 | sunset_glow | 8 / 9 | DSC07402（`mean_saturation` −0.013） |
| 7 | 実効 lift ≥ 40 の 4 プリセット（ISO 3200 / 6400） | 12 / 12（比 0.56〜0.95） | — |

DSC07402 は薄明の街灯と海面のカットで、差はどれも小さい（宣言した向きと逆でも大きさは目視の範囲）。日中 3 カットで回してから判断する。

### 2.3 条件 8（`--adjustments`、夜景 2 カット）

`work/preset-retune/adj-regression/`。DSC07415 / DSC07402 でチェック 36 種 × 2 カット = 72 件のうち合格 70・不合格 0・n/a 2、エラー 0。後段オラクル（tone）は ΔE76 中央値 0・チャンネル差 p99 ≤ 0.0006、
`or_preset`（CI 値の `neutral_recovery`、hr 0 固定）も中央値 0。既定リクエストの `applied` はベースラインと差分 0、`adjustments_ignored_empty` 合格。

## 3. プリセット値（実装した値 = 設計書 §7.2 の初期案）

| name | exposure | wb_temp | shadow_lift | hr | brightness | contrast | saturation | sharpness | NR | 実効 (EV, lift) |
|---|---|---|---|---|---|---|---|---|---|---|
| neutral_recovery | 0.0 | 0 | 15 | 0 | 1.0 | 1.03 | 1.02 | 1.04 | light | (0, 15) |
| landscape_clear | 0.0 | 0 | 10 | 0 | 1.0 | 1.10 | 1.08 | 1.15 | light | (0, 10) |
| nightscape | 0.0 | −3 | 15 | 0 | 1.0 | 1.04 | 1.0 | 1.02 | full | (0, 15) |
| sunset_glow | 0.0 | 12 | 8 | 60 | 1.0 | 1.05 | 1.07 | 1.03 | light | (−0.7, 48) |
| indoor_warm | 0.1 | 5 | 18 | 0 | 1.0 | 1.02 | 1.03 | 1.02 | light | (+0.1, 18) |
| portrait_soft | 0.0 | 3 | 12 | 0 | 1.0 | 0.98 | 1.0 | 0.96 | light | (0, 12) |
| monochrome_street | 0.0 | 0 | 10 | 0 | 1.0 | 1.08 | 0.0 | 1.04 | light | (0, 10) |
| highlight_rescue | 0.0 | 0 | 0 | 90 | 1.0 | 1.0 | 1.0 | 1.02 | light | (−1.0, 55) |
| shadow_rescue | 0.0 | 0 | 45 | 0 | 1.0 | 1.0 | 1.0 | 1.01 | full | (0, 45) |
| backlit_rescue | 0.0 | 0 | 30 | 60 | 1.0 | 1.0 | 1.02 | 1.02 | light | (−0.7, 70) |
| vivid_travel | 0.0 | 0 | 10 | 0 | 1.0 | 1.07 | 1.10 | 1.05 | light | (0, 10) |

シートに並べる追加の候補（値は変えない）: `neutral_recovery` / `landscape_clear` の hr 30（案 C）、`landscape_clear` の弱め（contrast 1.06 / saturation 1.05 / sharpness 1.08）、
`sunset_glow` の hr 70、`highlight_rescue` の hr 100。

## 4. 再開の手順（ボリュームをマウントしてから）

```sh
# 空の 10 カット + 日中 3 + 室内 2（原寸は救済系 3 カット）
.venv/bin/python scripts/compare_engines.py --source-root /Volumes/Backup/100MSDCF \
  --captures DSC04473 DSC04607 DSC04532 DSC04705 DSC04579 DSC04604 DSC04754 DSC04432 DSC04746 DSC04462 \
             DSC04670 DSC04651 DSC04872 DSC04430 DSC04453 \
  --presets --full --out work/preset-retune/run1
# 夜景 3 カット（別フォルダ）
.venv/bin/python scripts/compare_engines.py --source-root <夜景のフォルダ> \
  --captures DSC07415 DSC07407 DSC07402 --presets --out work/preset-retune/run1-night
# 条件 8（昼 6 + 星 3 カット、比較レポート §16 と同じ）
.venv/bin/python scripts/compare_engines.py --source-root /Volumes/Backup/100MSDCF --captures <§16 の 6 カット> --adjustments \
  --out work/preset-retune/run1-adjustments
```

- SMB が遅い（1 件 ≈1.4 秒）ときは ARW / JPG を `work/preset-retune/arw/` にコピーして `--source-root` に渡す（原本は変更しない）。
- `run1/index.md` に §8 のオーナーに見せる組とシートのパス、§7.4 の自動判定（`check_summary`）、等価性が出る。不合格・目視で違和感のあるプリセットだけ
  1〜2 キーを直して `run2` / `run3` を回す（最大 3 回。§7.5）。値を変えたら `src-tauri/src/presets.rs` を直し、
  `UPDATE_PRESETS_FIXTURE=1 cargo test write_presets_fixture -- --ignored` でフィクスチャを作り直す。

## 5. 既知の限界

- 夕景の太陽そのものを含むカットは無い。`sunset_glow` の hr 60 は薄明 1 カット（DSC04532）と空のカットで決める（設計書 §10 リスク 3）。
- hr 100 の節点（−1.1EV, +60）は外挿で、空のカットでは未計測（夜景 3 カットでは hr 90 とほぼ同じ指標）。
- 原寸の `magenta_rate` の閾値は未確定（#92 の申し送り）。原寸は数値を記録して目視のみ。
- 星のカットでは hr を上げると `magenta_rate` が上がる（§2.1）。夜景プリセットは hr 0 なので既定では影響しないが、救済系を夜景に当てたときは目視で確かめる。
