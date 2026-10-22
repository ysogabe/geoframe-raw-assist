# プリセット再調整と `highlight_recovery` の写像 — 計測レポート（Issue #81）

- 設計: [設計書](design/coreimage-preset-retune.md)（§7 手順・§7.4 合否・§8 シート、§11 オーナー判断 2026-10-22）。
- 実施: 2026-10-22、macOS 27.0.1 / Apple M3、CLI 0.6.0、Python 3.13 + rawpy 0.27.0。
- **状況: 完了（反復 2 / 3 回）。条件 1〜5・7〜10 は合格、条件 6 は誤差レベルの不一致のみ（§2.3）。目視はオーナーが 2026-10-22 に合格と判断（案 C と `landscape_clear` の弱めた版は採らない）。**
  run1（初期案）→ run2（`sunset_glow` の `shadow_lift` 8 → 4）→ run3（確定値で 15 カットを再計測。ハーネスの判定修正を含む）。
- 位置情報は書かない。シート・出力は `work/preset-retune/`（gitignored）に置き、画像はコミットしない。

## 1. 完了条件（§7.4）の状況（run3 = 確定値）

| # | 条件 | 対象 | 結果 |
|---|---|---|---|
| 1 | hr の寄与: `clip8_any(P) < clip8_any(P_hr0)` | 救済系 3 × 空 10 カット | **合格**（3 プリセットとも 10 / 10） |
| 2 | hr の寄与: `|mid_p50(P) − mid_p50(P_hr0)|` ≤ 0.03（9/10） | 同 | **合格**（backlit・sunset 10 / 10、highlight 9 / 10。外れは DSC04432） |
| 3 | hr の寄与: `fg_p01(P) − fg_p01(P_hr0)` ≥ −0.02（9/10） | 同 | **合格**（10 / 10） |
| 4 | 偽色: `magenta_rate` ≤ 0.01（preview） | 同 | **合格**（highlight は 9 / 9 + n/a 1、他は 10 / 10） |
| 5 | `d_base` 比: 白飛びが減り中間調 ±0.03（8/10。backlit は中間調を問わない） | 同 | **合格**（backlit・sunset 10 / 10、highlight 9 / 10） |
| 6 | 個性の方向（§7.3 の宣言） | 全プリセット × 15 カット | **4 プリセットで誤差レベルの不一致**（§2.3）。値は動かさない |
| 7 | ノイズ: `flat_noise(P) / flat_noise(d_base)` ≤ 1.5 | 実効 lift ≥ 40 × ISO ≥ 1600 | **合格**（室内 3 カット。夜景 3 カットはスモークで 12 / 12） |
| 8 | 回帰: identity の後段オラクル・既定リクエストの `applied` 差分 0 | `--adjustments` | **合格**（昼・高 ISO・逆光 6 カットで不合格 0、夜景 2 カットもスモークで全合格） |
| 9 | 等価性（設計書 §2.2）: hr 60 ≡ `(−0.7, 40)`、hr 90 ≡ `(−1.0, 55)` | 1 カット | **合格**（DSC04473・DSC07415 とも max abs diff 0、`applied` 一致） |
| 10 | 目視（§8 の 6 組） | オーナー | **合格**（2026-10-22）: 夜景の組（DSC07415 nightscape / DSC07407 shadow_rescue）は新プリセット（CI）の列が CI 既定・rawpy より好ましい。救済系・夕景・室内高 ISO は新プリセットの値で確定（hr は highlight 90・sunset 60 のまま）。既定の組は案 C（hr 30）を採らず hr 0、`landscape_clear` は弱めた版にせず今の値（設計書 §11） |

## 2. 本計測（run1〜run3、preview 2400px + 救済系の原寸）

- カット: 空 10（DSC04473 / 04607 / 04532 / 04705 / 04579 / 04604 / 04754 / 04432 / 04746 / 04462）+ 日中 3（DSC04670 / 04651 / 04872）+ 室内 2（DSC04430 / 04453）。
  原寸は救済系 3 プリセット × DSC04473 / 04607 / 04705。ARW / JPG は `work/preset-retune/arw/` のローカルコピー（原本は変更しない）。
- 出力: `work/preset-retune/run1/`・`run2/`（空 10 カット、rawpy 列なし）・`run3/`（確定値。`index.md` に §8 の組とシート 174 枚）。条件 8 は `run1-adjustments/`。

### 2.1 反復の記録

| 回 | 変更 | 結果 |
|---|---|---|
| run1 | 初期案（設計書 §7.2） | 条件 5: backlit 0 / 10（中間調 +0.08〜0.09）、sunset 7 / 10（DSC04462 / 04705 / 04754 の中間調 +0.032〜0.036）。条件 6 の clip8 の方向が、`d_base` がほぼ白飛びしないカット（DSC04453 / 04872、差 −1e-5 程度）で不合格扱い |
| run2 | `sunset_glow` の `shadow_lift` 8 → 4（実効 lift 48 → 44） | sunset の条件 5 が 10 / 10。条件 1〜4 も 10 / 10 のまま。DSC04532 の雲の暖色・桃色は目視で保たれている |
| run3 | 確定値で 15 カットを再計測。ハーネスの判定を 2 点修正（下記） | §1 のとおり |

ハーネスの判定修正（`scripts/compare_engines.py`、設計書 §7.4 に追記）:

1. **条件 5 で `backlit_rescue` の中間調を問わない。** 被写体向けの lift 30（オーナー判断 #3 で承認済みの値）で中間調を上げる設計で、±0.03 の幅と矛盾する。hr 0 との比では中間調の差は ±0.01 以内（条件 2）なので、白飛び救済が中間調を動かしていないことは条件 2 で確かめている。lift を削って合わせることはしない。
2. **`d_base` の clip8 が 1% 未満のカットは条件 5 と条件 6 の clip8 の方向を n/a にする**（条件 1 と同じ規則）。救う白飛びが無いカットで、差 −1e-5 を不合格に数えていた。

### 2.2 空 10 カットの中央値（run3）

| variant | 実効 (EV, lift) | clip8_any | sky_bright clip8 | mid_p50 | fg_p01 | mean_saturation | flat_noise | ΔE76 vs d_base |
|---|---|---|---|---|---|---|---|---|
| d_base | — | 0.130 | 0.452 | 0.378 | 0.0098 | 0.102 | 0.322 | 0 |
| highlight_rescue | (−1.0, 55) | 0.022 | 0 | 0.383 | 0.0099 | 0.101 | 0.304 | 4.32 |
| highlight_rescue_hr100 | (−1.1, 60) | 0.018 | 0 | 0.381 | 0.0095 | 0.101 | 0.298 | 4.43 |
| backlit_rescue | (−0.7, 70) | 0.035 | 0.003 | 0.469 | 0.0144 | 0.118 | 0.288 | 7.58 |
| backlit_rescue_hr0 | (0, 30) | 0.129 | 0.450 | 0.464 | 0.0160 | 0.115 | 0.312 | 5.83 |
| sunset_glow | (−0.7, 44) | 0.068 | 0.034 | 0.395 | 0.0018 | 0.112 | 0.303 | 4.19 |
| sunset_glow_hr70 | (−0.8, 49) | 0.055 | 0.015 | 0.394 | 0.0014 | 0.112 | 0.302 | 4.43 |
| neutral_recovery | (0, 15) | 0.160 | 0.720 | 0.419 | 0.0035 | 0.110 | 0.335 | 3.11 |
| neutral_recovery_hr30（案 C） | (−0.35, 35) | 0.113 | 0.302 | 0.420 | 0.0039 | 0.112 | 0.307 | 3.60 |
| landscape_clear | (0, 10) | 0.195 | 0.971 | 0.397 | 0.0004 | 0.114 | 0.805 | 3.82 |
| landscape_clear_hr30（案 C） | (−0.35, 30) | 0.160 | 0.722 | 0.399 | 0.0004 | 0.117 | 0.341 | 2.83 |
| landscape_clear_weak | (0, 10) | 0.176 | 0.850 | 0.401 | 0.0013 | 0.112 | 0.380 | 2.76 |

- 救済系 3 種は空の白飛び（`sky_bright` の clip8）を 45% から 0〜3% に減らし、中間調は CI 既定とほぼ同じ（backlit は設計どおり +0.09）。
- `neutral_recovery` / `landscape_clear` は lift の分だけ中間調が上がり、白飛びも CI 既定より増える（空 0.45 → 0.72 / 0.97）。案 C（hr 30）は空の白飛びを 0.30 / 0.72 に戻す。採否はオーナーの目視で決める（設計書 §11 #5）。
- `landscape_clear` の `flat_noise` 中央値 0.805 は CI 既定の 2.5 倍（シャープ 1.15 の影響。弱め候補で 0.380）。目視で白い粒が気になれば弱め候補に替える。

### 2.3 条件 6 の不一致（run3）

いずれも差が小さく、値は動かさない（設計書 §7.5 の 3。目視で判断する）。

| プリセット | カット | 指標 | 差（d_base 比） | 見立て |
|---|---|---|---|---|
| nightscape | DSC04532 / 04651 / 04670 / 04705 / 04872（昼） | `dark_mean_luma` | −0.001〜−0.007 | 昼のカットで NR full と contrast 1.04 が lift 15 を打ち消す。夜景カットはオーナーが目視で承認 |
| sunset_glow | DSC04532 / 04604 | `rb_ln_neutral` | −0.014 | 白飛びから戻った空の中立域が寒色寄りに入る。DSC04532 の雲は目視で暖色・桃色 |
| sunset_glow | DSC04670 / 04754 | `mean_saturation` | −0.0002〜−0.0006 | 誤差 |
| landscape_clear | DSC04532 / 04705 | `mean_saturation` | −0.0006〜−0.0015 | 白飛びの多いカットで誤差 |
| portrait_soft | DSC04430 / 04432 / 04453（ISO 3200 の室内）/ DSC04872 | `edge_width_px` / `std_luma` | −0.05〜+0.002 | 高 ISO ではシャープ 0.96 の差がノイズに埋もれる |

## 3. スモーク（夜景 3 カット、preview 2400px）

- コマンド: `--source-root <夜景のローカルコピー> --captures DSC07415 DSC07407 DSC07402 --presets`。
  出力 `work/preset-retune/smoke-night/`（`index.md`・`metrics.csv`・`checks.csv`・`summary.json`・`tables.md`・`sheets/` 33 枚）。エラー 0 件。
- 3 カットは空の 10 カットに入らないので条件 1〜5 は数えない（n/a）。以下は**参考値**で、合否の判断には使わない。

### 3.1 バリアント別（3 カットの中央値）

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

### 3.2 条件 6・7（夜景 3 カットのみ）

| # | プリセット | 合格 / 判定 | 不合格のカット |
|---|---|---|---|
| 6 | highlight_rescue / backlit_rescue / shadow_rescue / indoor_warm / landscape_clear / vivid_travel / monochrome_street | すべて合格 | — |
| 6 | nightscape | 2 / 3 | DSC07402（`dark_mean_luma` −0.007） |
| 6 | portrait_soft | 2 / 4（n/a 2） | DSC07402（`std_luma` +0.005、`edge_width_px` −0.05） |
| 6 | sunset_glow | 8 / 9 | DSC07402（`mean_saturation` −0.013） |
| 7 | 実効 lift ≥ 40 の 4 プリセット（ISO 3200 / 6400） | 12 / 12（比 0.56〜0.95） | — |

DSC07402 は薄明の街灯と海面のカットで、差はどれも小さい（宣言した向きと逆でも大きさは目視の範囲）。日中 3 カットで回してから判断する。

### 3.3 条件 8（`--adjustments`、夜景 2 カット）

`work/preset-retune/adj-regression/`。DSC07415 / DSC07402 でチェック 36 種 × 2 カット = 72 件のうち合格 70・不合格 0・n/a 2、エラー 0。後段オラクル（tone）は ΔE76 中央値 0・チャンネル差 p99 ≤ 0.0006、
`or_preset`（CI 値の `neutral_recovery`、hr 0 固定）も中央値 0。既定リクエストの `applied` はベースラインと差分 0、`adjustments_ignored_empty` 合格。

## 4. プリセット値（確定値。設計書 §7.2 の初期案から `sunset_glow` の `shadow_lift` だけ 8 → 4）

| name | exposure | wb_temp | shadow_lift | hr | brightness | contrast | saturation | sharpness | NR | 実効 (EV, lift) |
|---|---|---|---|---|---|---|---|---|---|---|
| neutral_recovery | 0.0 | 0 | 15 | 0 | 1.0 | 1.03 | 1.02 | 1.04 | light | (0, 15) |
| landscape_clear | 0.0 | 0 | 10 | 0 | 1.0 | 1.10 | 1.08 | 1.15 | light | (0, 10) |
| nightscape | 0.0 | −3 | 15 | 0 | 1.0 | 1.04 | 1.0 | 1.02 | full | (0, 15) |
| sunset_glow | 0.0 | 12 | **4** | 60 | 1.0 | 1.05 | 1.07 | 1.03 | light | (−0.7, 44) |
| indoor_warm | 0.1 | 5 | 18 | 0 | 1.0 | 1.02 | 1.03 | 1.02 | light | (+0.1, 18) |
| portrait_soft | 0.0 | 3 | 12 | 0 | 1.0 | 0.98 | 1.0 | 0.96 | light | (0, 12) |
| monochrome_street | 0.0 | 0 | 10 | 0 | 1.0 | 1.08 | 0.0 | 1.04 | light | (0, 10) |
| highlight_rescue | 0.0 | 0 | 0 | 90 | 1.0 | 1.0 | 1.0 | 1.02 | light | (−1.0, 55) |
| shadow_rescue | 0.0 | 0 | 45 | 0 | 1.0 | 1.0 | 1.0 | 1.01 | full | (0, 45) |
| backlit_rescue | 0.0 | 0 | 30 | 60 | 1.0 | 1.0 | 1.02 | 1.02 | light | (−0.7, 70) |
| vivid_travel | 0.0 | 0 | 10 | 0 | 1.0 | 1.07 | 1.10 | 1.05 | light | (0, 10) |

シートに並べる追加の候補（値は変えない）: `neutral_recovery` / `landscape_clear` の hr 30（案 C）、`landscape_clear` の弱め（contrast 1.06 / saturation 1.05 / sharpness 1.08）、
`sunset_glow` の hr 70、`highlight_rescue` の hr 100。

## 5. 実行した手順（再計測するとき）

```sh
# ARW / JPG を work/preset-retune/arw/ にコピーしてから（原本は変更しない。SMB は 1 件 ≈1.4 秒）
A=work/preset-retune/arw
# 空の 10 カット + 日中 3 + 室内 2（原寸は救済系 3 カット）
.venv/bin/python scripts/compare_engines.py --source-root $A \
  --captures DSC04473 DSC04607 DSC04532 DSC04705 DSC04579 DSC04604 DSC04754 DSC04432 DSC04746 DSC04462 \
             DSC04670 DSC04651 DSC04872 DSC04430 DSC04453 \
  --presets --full --out work/preset-retune/run3
# 夜景 3 カット（別フォルダ）
.venv/bin/python scripts/compare_engines.py --source-root <夜景のフォルダ> \
  --captures DSC07415 DSC07407 DSC07402 --presets --out work/preset-retune/smoke-night
# 条件 8（比較レポート §16 と同じ 6 カット）
.venv/bin/python scripts/compare_engines.py --source-root $A \
  --captures DSC04670 DSC04651 DSC04872 DSC04430 DSC04453 DSC04473 --adjustments --out work/preset-retune/run1-adjustments
```

- 値を変えたら `src-tauri/src/presets.rs` を直し、`UPDATE_PRESETS_FIXTURE=1 cargo test write_presets_fixture -- --ignored` でフィクスチャを作り直す。

## 6. 既知の限界

- 夕景の太陽そのものを含むカットは無い。`sunset_glow` の hr 60 は薄明 1 カット（DSC04532）と空のカットで決める（設計書 §10 リスク 3）。
- hr 100 の節点（−1.1EV, +60）は外挿。空 10 カットでは hr 90 とほぼ同じ指標（clip8 中央値 0.018 / 0.022、§2.2）。
- 原寸の `magenta_rate` の閾値は未確定（#92 の申し送り）。原寸は数値を記録して目視のみ。
- 星のカットでは hr を上げると `magenta_rate` が上がる（§3.1）。夜景プリセットは hr 0 なので既定では影響しないが、救済系を夜景に当てたときは目視で確かめる。
- 条件 6 の不一致（§2.3）は差が誤差レベルのため値を動かしていない。方向の判定にはデッドバンド（`DIRECTION_EPS` = 1e-4）しか無く、意味のある大きさかどうかは目視で見る。
- `sunset_glow` は空の中立域で `rb_ln_neutral` がわずかに寒色側に出るカットがある（§2.3）。暖色の強さは WB +12 のまま。
