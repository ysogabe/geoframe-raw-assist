# Core Image と rawpy のエンジン比較（Issue #65 PR3）

現行エンジン（rawpy/LibRaw, `local_raw/engine.py`）と Core Image CLI（`geoframe-ci-develop`,
[`native/coreimage-cli/README.md`](../native/coreimage-cli/README.md)）を、同じ α6400 ARW 21 カットで
比較した記録（§9 は解像感＝ピント甘さに絞った追加評価、29 カット）。**採否の判断は PR4 で行う**。この文書は事実・所見・判断材料（論点）までに留める。

関連: [ADR 0001](adr/0001-coreimage-raw-poc.md)（CLI の設計判断）、
[パラメータ対応表](coreimage-parameter-mapping.md)。

## 1. 環境

| 項目 | 値 |
|---|---|
| マシン | Apple M3 / メモリ 24 GB |
| macOS | 27.0.1 |
| Core Image CLI | `geoframe-ci-develop` 0.1.0（`npm run build:coreimage` の release ビルド） |
| Core Image 設定 | `profile: neutral`, `decoder_version: "9"`（α6400 は "8"/"9" をサポート）, `lens_correction: false`（+ 比較用に `true`） |
| rawpy | rawpy 0.27.0 / LibRaw 0.22.1, `ENGINE_VERSION` 17, 既定プリセット `neutral_recovery`・既定 Adjustments |
| Python 側 | Python 3.14.6, Pillow 12.3.0, OpenCV 5.0.0 |
| 計測実行 | 2026-10-19、逐次実行（並列なし）。入力は外付けボリューム上の ARW/JPG |

## 2. サンプル構成（21 カット）

母集団: ARW+JPG ペア 451 カット。レンズ 3 本（Sigma 10-18mm F2.8 DC DN 262 / Sony E 55-210mm 122 /
Sigma 30mm F1.4 DC DN 67）。ISO は 100 が 326 枚、3200 が 33 枚。縦位置 13 枚。

`--select auto` のシーン分類（しきい値は `scripts/compare_engines.py` 冒頭の定数）:

| 種別 | 条件 | 母集団での件数 |
|---|---|---|
| 星空候補 `star` | 露光 2 秒以上、または EV100 <= 0 | **0** |
| 夜景 `night` | EV100 <= 6 かつ撮影時刻 18〜5 時台 | 43 |
| 暗所 `dark` | EV100 <= 6（夜間帯以外） | 32 |
| 高 ISO `high_iso` | ISO >= 3200 | 33 |
| 中 ISO `mid_iso` | ISO 800〜2500 | 33 |
| 逆光/ハイライト `backlit` | カメラ JPEG 1/8 縮小で輝度 >0.95 が 3% 以上 かつ <0.10 が 10% 以上 | 9 |
| 縦位置 `portrait` | Orientation 5〜8 | 13 |
| 昼景 `day` | 暗所でない かつ ISO <= 400 | 357 |

**星空は 0 件**。最長露光は 1.6 秒（ISO 100、夜明け前の屋外）。**本格的な夜景（街明かり・長秒）も無い**。
「night」に入ったのは、夜明け前（4〜5 時台）の屋外の薄明 3 カット（DSC04546 / DSC04501 / DSC04532、
DSC04532 は逆光枠で選定）と、夕方 19 時台の**室内**（人工光、ISO 2500〜3200）2 カット
（DSC04638 / DSC04625）。夜景・星空の評価は**未実施**として扱う（§8）。

選定は決定的（同じ入力なら同じ結果）で、連写の重複を避けるため「同じ分に撮ったカットは 1 枚」
「同じ日付+時のセッションからは 2 枚まで」（レンズ網羅の lens_day だけは上限を外して再探索）。
Sigma 30mm の低 ISO 昼景は連写分の重複で lens_day 枠から外れたが、30mm は暗所/高 ISO 枠で 9 カット入っている。

| ID | レンズ | 焦点距離 | ISO | 種別 | 選定理由 |
|---|---|---|---|---|---|
| DSC04638 | Sigma 30mm F1.4 | 30 | 3200 | night（室内） | EV100 2.9、1/125s、夕方の室内 |
| DSC04625 | Sigma 30mm F1.4 | 30 | 2500 | night（室内） | EV100 3.3、1/125s、夕方の室内 |
| DSC04546 | Sigma 10-18mm | 16.6 | 100 | night（薄明・屋外） | EV100 3.7、1.6s（母集団の最長露光） |
| DSC04501 | Sigma 10-18mm | 10 | 3200 | night（薄明・屋外） | EV100 4.0、1/25s |
| DSC04758 | Sigma 10-18mm | 18 | 3200 | dark（縦位置） | EV100 3.7、1/13s、木陰 |
| DSC04430 | Sigma 30mm F1.4 | 30 | 3200 | dark | EV100 4.9、1/500s、屋内 |
| DSC04433 | Sigma 30mm F1.4 | 30 | 3200 | dark | EV100 4.9、白飛び 10% を含む |
| DSC04453 | Sigma 30mm F1.4 | 30 | 3200 | high_iso | ISO 3200、1/500s |
| DSC04455 | Sigma 30mm F1.4 | 30 | 3200 | high_iso | ISO 3200、1/640s |
| DSC04463 | Sigma 30mm F1.4 | 30 | 3200 | high_iso | ISO 3200、1/640s |
| DSC04683 | Sigma 10-18mm | 18 | 1000 | mid_iso | ISO 1000、1/125s |
| DSC04459 | Sigma 30mm F1.4 | 30 | 800 | mid_iso | ISO 800、1/640s |
| DSC04532 | Sigma 10-18mm | 18 | 100 | backlit（朝日・薄明） | 白 10.4% / 暗部 32.4%、1/2s |
| DSC04473 | Sigma 10-18mm | 10 | 200 | backlit | 白 11.4% / 暗部 12.2%（曇天の空 + 林） |
| DSC04607 | Sigma 10-18mm | 18 | 100 | backlit | 白 10.4% / 暗部 11.3% |
| DSC04645 | Sigma 10-18mm | 18 | 125 | portrait | 縦位置（Orientation 8） |
| DSC04652 | Sigma 10-18mm | 17.4 | 100 | portrait | 縦位置（Orientation 8） |
| DSC04464 | Sigma 10-18mm | 16.6 | 250 | lens_day | 低 ISO 昼景 |
| DSC04755 | Sigma 10-18mm | 10 | 100 | lens_day | 低 ISO 昼景（曇天の空を含む） |
| DSC04759 | Sony E 55-210mm | 62 | 100 | lens_day | 低 ISO 昼景（晴天・強い明暗差） |
| DSC04880 | Sony E 55-210mm | 85 | 100 | lens_day | 低 ISO 昼景 |

## 3. 手順（再現コマンド）

```sh
npm run build:coreimage            # CLI をビルド（未ビルドなら）
.venv/bin/python scripts/compare_engines.py \
  --source-root /Volumes/Backup/100MSDCF \
  --select auto \
  --features-cache work/engine-comparison/features.json \
  --full --full-limit 0 \
  --out work/engine-comparison/run1
# 選定だけ確認: --dry-run。カット指定: --captures DSC04430 DSC04553
```

- 1 カットにつき 4 枚: camera（同名 JPG を正立化してリサイズのみ）/ rawpy / coreimage（neutral, lens off）/
  coreimage_lens（neutral, lens on）。preview（長辺 2400px）を全 21 カット、full（原寸）も全 21 カット。
  所要 7 分 21 秒（選定用 EXIF 読み取り約 3 分は別、キャッシュ済み）。
- **rawpy の分離**: このスクリプト自身を `--worker-rawpy` で別プロセス起動し（`/usr/bin/time -l` 越し）、
  ワーカー内で `config.source_root` を `--source-root` に、`engine.OUTPUT_ROOT` / `engine.PREVIEW_ROOT` を
  `--out/rawpy/<mode>/` に `mock.patch` してから `engine.develop_raw(..., force=True)` を呼ぶ
  （`scripts/evaluate_raw.py` と同じ方式）。アプリ設定・`outputs/` のキャッシュ・exports には書かない。
  preview は `max_edge=2400` を明示（省略すると engine 側で 1600px に読み替わるため）。full は既定 6000。
- Core Image CLI も 1 リクエスト 1 プロセスで `/usr/bin/time -l` 越しに呼ぶ。最初に計測外のウォームアップを 1 回。
- 指標は全エンジンを Core Image 出力の寸法へ INTER_AREA で揃えてから計算（rawpy の出力は 2400x1603 等で
  アスペクト差 0.2% 以内）。カメラ JPEG は EXIF Orientation を適用して正立化。
- 比較シートとクロップは **EXIF・ICC を付けずに保存**（確認済み: シートの EXIF は空）。
  ただし `work/.../rawpy/` 配下の rawpy 出力そのものは engine の仕様どおりペア JPG の EXIF（GPS を含みうる）を
  継承している。work/ は gitignore 済みで、リポジトリには入らない。

出力（すべて gitignore 対象の `work/engine-comparison/run1/`）:

- `selection.json`（選定理由・EXIF 要約。GPS は読まない）、`metrics.csv`（カット × モード × エンジン）、
  `summary.json`（環境・エンジン別/シーン別の中央値・範囲）、`run.log`
- `sheets/<ID>_preview_side.jpg`: 横並び（各パネル長辺 640px、ラベル付き）
- `sheets/<ID>_{preview,full}_crops.jpg`: 512px クロップ（中央 / 左上隅 / カメラ JPEG で Laplacian が最大の
  「detail」領域）。full はセンサー等倍、preview は 2400px 版の等倍
- 補助調査: `work/engine-comparison/probe/`（§6 の decoder / lens / profile 比較）

### 指標の定義

| 指標 | 定義 | 注意 |
|---|---|---|
| wall_s | `/usr/bin/time -l` で包んだプロセスの wall 秒 | rawpy はインタプリタ起動 + import（約 0.18 秒）を含む |
| engine_elapsed_s | rawpy は `develop_raw` の `elapsed_seconds`、CI は `elapsed_ms.total` | |
| maxrss / peak footprint | `/usr/bin/time -l` の maximum resident set size / peak memory footprint | rawpy ワーカーの常駐基準（import のみ）は RSS 73MB |
| 輝度 | Rec.709 係数、0..1。mean / p1 / p50 / p99 | |
| 白飛び | いずれかのチャンネルが 255 の画素率（clip_high_any） | |
| 黒つぶれ | 輝度 < 2/255 の画素率（clip_low） | |
| ΔE76 | OpenCV Lab（D65）でカメラ JPEG との平均 ΔE | **トーン差・幾何差（レンズ補正）も含む**（§5） |
| Laplacian 分散 | グレースケールの Laplacian 分散 | **ノイズでも上がる**。解像感の単独指標にしない |
| flat_noise | 32px タイルのうち構造が少ない下位 10% でのハイパス標準偏差の中央値（8bit） | 平坦部の簡易ノイズ |

## 4. 集計（21 カット、中央値 [最小..最大]）

### 処理時間・メモリ

| エンジン | モード | wall 秒 | エンジン内部 秒 | maxRSS MB | peak footprint MB |
|---|---|---|---|---|---|
| rawpy | preview 2400 | **3.71** [2.02..4.35] | 3.23 [1.67..3.92] | **1927** [1878..2447] | 1891 [1842..2411] |
| rawpy | full | **4.49** [2.90..6.02] | 4.10 [2.50..5.39] | **2607** [1978..3347] | 2731 [2534..3339] |
| Core Image | preview 2400 | **0.77** [0.69..0.90] | 0.74 [0.66..0.87] | **223** [222..228] | 552 [552..555] |
| Core Image | full | **1.60** [1.52..1.77] | 1.57 [1.49..1.75] | **229** [212..229] | 750 [567..766] |
| Core Image + lens | preview 2400 | 1.30 [1.14..1.49] | 1.26 [1.10..1.45] | 226 [224..230] | 585 [583..587] |
| Core Image + lens | full | 2.15 [2.01..2.37] | 2.12 [1.99..2.34] | 289 [276..289] | 916 [691..924] |

- Core Image は preview で rawpy の約 1/5、full で約 1/3 の時間。ウォームアップ（初回起動）は 1.6 秒。
- rawpy は preview でも full-size デモザイク（`half_size=False`）なので preview と full の差が小さい。
  ISO 800 以上で FBDD が掛かるカットが遅い側（約 3.7〜4.3 秒）、低 ISO は約 2.0〜2.8 秒。
- メモリ: rawpy プロセスは RSS 約 1.9〜2.6GB（16bit 全画素 + float32 作業配列）。Core Image は RSS 約 0.22〜0.29GB だが
  peak memory footprint は 0.55〜0.92GB（RSS に出ない GPU/IOSurface 系の割り当てを含むとみられる。内訳は未確認）。
  どちらの指標でも Core Image は rawpy の 1/3 以下。

### 画質指標（preview 2400px）

| エンジン | mean 輝度 | p1 | p99 | 白飛び any | 黒つぶれ | Laplacian | flat_noise | ΔE76 vs camera | 輝度差 vs camera |
|---|---|---|---|---|---|---|---|---|---|
| camera JPEG | 0.355 [0.20..0.52] | 0.047 | 0.890 | 0.005% | 0.00003% | 149 [15..1507] | 0.89 [0.27..2.12] | — | — |
| rawpy | 0.330 [0.21..0.55] | 0.034 | 0.934 | **1.23%** [0..24.9] | 0.001% [0..10.6] | 125 [10..541] | **1.66** [0.34..3.12] | 9.2 [4.8..30.7] | −0.005 [**−0.234..+0.283**] |
| Core Image | 0.362 [0.16..0.53] | 0.019 | 0.901 | 0.06% [0..14.7] | 0.10% [0..8.4] | **45** [2..815] | **0.37** [0.22..1.59] | 7.7 [3.5..15.8] | +0.009 [−0.080..+0.019] |
| Core Image + lens | 0.374 [0.16..0.54] | 0.027 | 0.906 | 0.44% [0..15.3] | 0.11% [0..4.4] | 176 [26..958] | 1.80 [0.32..7.63] | **5.4** [3.3..10.1] | +0.016 [−0.075..+0.033] |

full（原寸）でも傾向は同じ（ΔE 中央値: rawpy 10.3 / CI 7.9 / CI+lens 5.3。flat_noise: rawpy 2.72 / CI 0.26 /
CI+lens 0.69 / camera 1.12。Laplacian: rawpy 146 / CI 7.4 / CI+lens 36 / camera 106）。

### シーン別（preview、中央値）

| シーン（カット数） | エンジン | mean 輝度 | 白飛び | 黒つぶれ | flat_noise | ΔE76 |
|---|---|---|---|---|---|---|
| 夜間・暗所（7） | camera | 0.284 | 0.00% | 0.00% | 1.60 | — |
| | rawpy | 0.279 | 0.55% | 0.00% | 2.36 | 7.5 |
| | Core Image | 0.290 | 0.01% | 0.01% | 0.37 | 6.0 |
| | Core Image + lens | 0.303 | 0.03% | 0.01% | 2.71 | 6.0 |
| 高 ISO 3200（3） | camera | 0.368 | 0.00% | 0.00% | 1.06 | — |
| | rawpy | 0.357 | 0.12% | 0.00% | 2.32 | 7.7 |
| | Core Image | 0.387 | 0.01% | 0.00% | 0.22 | 3.8 |
| | Core Image + lens | 0.401 | 0.06% | 0.00% | 1.85 | 4.4 |
| 逆光（3） | camera | 0.386 | 1.74% | 0.03% | 0.30 | — |
| | rawpy | 0.275 | 1.74% | 2.09% | 0.49 | 13.4 |
| | Core Image | 0.405 | 8.62% | 1.05% | 0.32 | 11.7 |
| | Core Image + lens | 0.408 | 9.92% | 0.58% | 0.38 | 7.3 |
| 昼景・縦位置（6） | camera | 0.328 | 0.11% | 0.00% | 0.69 | — |
| | rawpy | 0.324 | 1.31% | 0.02% | 0.81 | 19.2 |
| | Core Image | 0.332 | 0.53% | 2.50% | 0.36 | 11.0 |
| | Core Image + lens | 0.336 | 0.57% | 1.20% | 1.42 | 4.9 |

（中 ISO は 2 カットのみなので表から省略。値は `summary.json` の `summary_by_category`）

## 5. 指標の読み方の注意

- **ΔE はデコード品質ではない**。カメラ JPEG は本体でレンズ補正（歪曲・周辺減光）が掛かっているため、
  レンズ補正なしの Core Image は幾何がずれ、ΔE が膨らむ（昼景で CI 11.0 → CI+lens 4.9）。rawpy は lensfun で
  補正済み（21 カット全てで `lens_applied=true`）だが、ΔE の大半は §6 のトーン差（auto_bright）による。
- **Laplacian 分散はノイズで上がる**。CI+lens の Laplacian が camera より高いのは解像感ではなく粒状ノイズ（目視で確認）。
- 黒つぶれ率は、レンズ補正なしの Core Image では周辺減光の残る四隅の影響を受ける（10-18mm で顕著）。

## 6. 所見

### 6.1 明るさ・トーン

- **Core Image はカメラ JPEG の明るさにほぼ追従する**（輝度差の範囲 −0.08..+0.02）。ADR §4 の
  baselineExposure + boost の結果。
- **rawpy は auto_bright の影響でカットにより大きく振れる**（−0.23..+0.28）。明るい空を含むカットを暗く
  （DSC04755: −0.23、DSC04473/04607/04645: −0.10〜−0.12）、強い日向と日陰が混ざるカットを明るく
  （DSC04759: +0.28、DSC04880: +0.19、DSC04464: +0.16）。目視でも DSC04759 は rawpy だけ日陰が明るく
  日向が白飛びし、DSC04755 は rawpy だけ空が重い灰色。rawpy が暗シーン判定（EV100 <= 6、auto_bright を使わず固定倍率）
  したカット（11 カット）はカメラとの輝度差が小さく（−0.03..+0.02）、大きな振れは auto_bright 側の
  10 カットに集中している（−0.23..+0.28）。
- 夜明け前の薄明（DSC04501）: カメラは寒色寄りの淡い空、rawpy は空が濃い青、Core Image は全体に
  マゼンタ寄り（石段・岩がピンク）。どちらもカメラ WB（As Shot）を使っているが色の出方が違う。

### 6.2 ハイライト・暗部

- 逆光（DSC04532、朝日）: 白飛び率 camera 3.3% / rawpy 24.9% / CI 14.7%。rawpy は太陽周辺の雲の階調が広く
  飛ぶ。CI は rawpy より残るがカメラ JPEG（DRO 等のトーン圧縮）ほどではない。CI の neutral は
  ハイライト復元（EDR 等）を 0 にしているため、この差は設定で動く可能性がある（未検証）。
- DSC04758（ISO 3200、木漏れ日）: rawpy は白飛び部の境界に**マゼンタの偽色ドット**が出る（full クロップで明瞭）。
  Core Image では出ない。
- 暗部: Core Image は p1 がカメラより低く（0.019 vs 0.047）、シャドウが締まる（shadowBias 3.0）。
  DSC04758 の暗い幹では camera / rawpy より CI の方が沈むが、幹の質感は CI の方が残っている。

### 6.3 解像感・ノイズ

- （注: 解像感はシャープ条件を揃えた §9 で再評価した。原寸ではディテール ROI の Laplacian は v9 lens OFF / ON とも 0.20 で、v9 は lens ON でも平滑化される。下記の全画像 Laplacian の差（v9 lens off 1.0 / on 2.4）は主に平坦部ノイズの差（flat_noise 0.18 vs 0.64）で、ディテールの回復ではない。preview の「粒状ノイズ」は decoder 9 + lens ON に出る白点ノイズ。§9.6・§9.7 参照）
- **ISO 3200（DSC04453, DSC04638 等）**: 解像感（毛・まつ毛の描写）はカメラ JPEG が最も高い（カメラ内シャープ）。
  rawpy と Core Image neutral は同程度の細部を描くが、**rawpy は輝度 + 色ノイズ（赤/青の斑点）が目立ち**、
  Core Image neutral はノイズがかなり少ない（flat_noise 0.22〜0.37 vs rawpy 2.3）。
- **Core Image neutral（lens off）は NR=0 設定なのにノイズが非常に少なく、平坦部が「塗った」ように滑らか**。
  DSC04430（ISO 3200）の 2400px クロップでは毛並みが水彩風に潰れ、full でも Laplacian が camera の約 1/10。
  ISO 100 の DSC04553 でも同傾向（full の Laplacian: camera 9.0 / CI 1.0）。
- **lens_correction を ON にするとこの滑らかさが消え、粒状ノイズが現れる**（flat_noise 0.37 → 1.80）。
  DSC04430 / DSC04638 では CI+lens の方が「ザラつくが細かい」。補助調査（DSC04553 full、
  `work/engine-comparison/probe/`）: Laplacian は v9/lens off 1.0、v9/lens on 2.4、**v8/lens off 6.3**、
  v8/lens on 5.4、v9 apple_default（シャープ・NR 既定）lens off 4.4。**decoder 9 かつ lens off の組み合わせだけが
  特に滑らか**。CLI の `applied`（フィルタから読み戻した値）は lens off / on で `lens_correction_enabled` 以外
  同一で、`luminance_noise_reduction_amount` / `color_noise_reduction_amount` / `detail_amount` /
  `sharpness_amount` / `moire_reduction_amount` はいずれも 0（`work/engine-comparison/probe/applied_{false,true}.json`）。
  つまり CLI のパラメータ設定順の問題（ADR §4 の decoderVersion によるリセットのような）ではなく、
  フィルタ内部の処理経路の差とみられる。原因（v9 デコーダ内部の処理経路の違いか）は API から判別できず未解明。目視では
  DSC04553 の看板文字の差は小さい（v9 lens off がわずかに甘い程度）。
- 低 ISO 昼景（DSC04880 等）: 細部の描写は 3 者とも大差なく、カメラ JPEG がシャープ分だけ締まって見える。

### 6.4 色

- 彩度（max−min の平均）: camera 0.065 < rawpy 0.082 < Core Image 0.103（preview 中央値）。Core Image は
  緑（植生）とマゼンタ寄りの色が強く出る。rawpy も camera より鮮やか。
- 高 ISO の室内（DSC04453）では三者の色味は近い。差が大きいのは薄明・夕景（§6.1）。

### 6.5 レンズ補正

- 10-18mm @10mm（DSC04501, DSC04473）では CI+lens で樽型歪曲が補正され画角が変わる（ADR §6 の 2.6% 縮小と整合）。
  カメラ JPEG との ΔE は CI+lens が最も小さく、**カメラ本体の補正と CI の補正は幾何的に近い**。
- 30mm では幾何変化はほぼ無いが、上記のとおりノイズの見え方が変わる。
- rawpy の `lens_applied=true` は全 21 カットだが、暗シーン判定の 11 カットでは engine が**周辺減光補正だけを
  スキップ**する（`force_skip_vignetting`。歪曲・倍率色収差は補正）。全カットで同じ補正が掛かっているわけではない。
- rawpy（lensfun）と CI レンズ補正の幾何・周辺減光の直接比較は未実施（ΔE には他の差が混ざる）。

## 7. Core Image の長所 / 短所（判断材料）

長所:

- 速い（preview 0.77 秒 / full 1.6 秒、rawpy の 1/5〜1/3）。メモリも RSS で rawpy の約 1/9、footprint でも 1/3 以下。
- 明るさがカメラ JPEG に安定して追従し、rawpy の auto_bright のような大きな振れが無い。
- 高 ISO で色ノイズが出ず、rawpy の白飛び境界のマゼンタ偽色も出ない。
- カメラ本体と近い幾何のレンズ補正が OS 側で使える（3 本とも `lens_correction_supported`）。

短所・リスク:

- decoder 9 + lens off の neutral は、設定上 NR=0 でも平坦部が強く平滑化される（ディテールの「塗り」）。
  lens on では消えるので、どの組み合わせを「既定」にするかで絵が大きく変わる。挙動が非公開で、OS 更新で変わりうる。
- 逆光の白飛びは rawpy より少ないがカメラ JPEG より多い。ハイライト処理の調整余地は未検証。
- 薄明でマゼンタ寄り・植生の彩度が高めなど、カメラ JPEG と色の出方が違う。
- macOS 専用（Windows/Linux では使えない）。古い macOS での decoder 9 可否は未確認（ADR）。

PR4 で決めるべき論点:

1. 既定の decoder_version / lens_correction の組み合わせ（v9 lens off の平滑化をどう扱うか。v8 や lens on を基準にするか）。
2. rawpy の auto_bright による露出の振れを課題として扱うか（Core Image 採用の動機になるか、rawpy 側で直すか）。
3. シャープ・NR を Core Image 側（sharpnessAmount / NR）で行うか、後段の自前処理に寄せるか。
4. ハイライト（逆光）と色（薄明のマゼンタ寄り）を許容範囲とみなすか。
5. 非 macOS 環境のフォールバック（rawpy を残すか）。

## 8. 未確認事項

- **星空・夜景（長秒・街明かり）**: サンプルに 0 件。夜間帯の 5 カットは薄明の屋外と室内のみ。
- decoder 9 + lens off の平滑化の原因と、ISO・decoder・profile ごとの網羅的な挙動。
- Core Image の peak memory footprint の内訳（GPU/IOSurface）と並列実行時のメモリ。
- rawpy（lensfun）と Core Image のレンズ補正の個別比較（歪曲・周辺減光・倍率色収差）。
- 中 ISO（800〜2500）は 2 カット、縦位置は 3 カット（DSC04758 含む）と少ない。
- Core Image の `apple_default` profile・EDR / ハイライト関連パラメータを使った場合の比較。
- 指標はすべて簡易指標で、知覚的な画質評価（SSIM 等、ブラインド目視）は行っていない。
- 目視は 11 カット（DSC04430, DSC04453, DSC04473, DSC04501, DSC04532, DSC04546, DSC04553, DSC04638,
  DSC04755, DSC04758, DSC04759 の横並び/クロップシートの一部）で、全 21 カットの全シートは見ていない。

## 9. 解像感（ピント甘さ）の評価

### 9.1 目的

「現像するとピントが甘くなる（解像感が落ちる）」という問題について、Core Image に替えると改善するのか・
悪化するのか・条件次第なのかを判断する材料を揃える。§6.3 の比較は「Core Image neutral（シャープ 0）」対
「rawpy（撮影時シャープ込み）」で条件が揃っていなかったため、ここでは**デコーダの素の解像**と
**現行と同じ仕上げを掛けた後の解像**を分けて、エッジ幅とノイズを対で測る。採否の判断はしない（ADR 0002 で扱う）。

### 9.2 手順（再現コマンド）

```sh
npm run build:coreimage
R1=($(python3 -c "import json;print(' '.join(r['capture_id'] for r in json.load(open('work/engine-comparison/run1/selection.json'))['selection']))"))
NEW=(DSC04670 DSC04651 DSC04643 DSC04472 DSC04465 DSC04872 DSC04716 DSC04697)
.venv/bin/python scripts/compare_engines.py --source-root /Volumes/Backup/100MSDCF \
  --sharpness --captures $R1 $NEW \
  --full --full-ids $NEW DSC04880 DSC04755 DSC04430 DSC04453 \
  --out work/engine-comparison/sharpness1
```

- 実行: 2026-10-19、§1 と同じマシン。29 カット × preview + 12 カット × full、10 バリアント。所要 4 分 51 秒
  （1 カット内の 7 プロセスを 4 並列。処理時間はこの節では比較しない）。エラー 0 件。
- 出力（gitignore 対象の `work/engine-comparison/sharpness1/`）: `sharpness_metrics.csv`（カット × モード × バリアント）、
  `sharpness_summary.json`（モード別・ISO 帯別の中央値）、`sharpness_tables.md`（同じ表の Markdown）、
  `sheets/<ID>_<mode>_{detail1,detail2,center,corner}.jpg`（等倍クロップ、10 バリアントを 2 段 × 5 枚、ラベル付き、EXIF なし）。
- **サンプル**: §2 の 21 カット全部 + 解像評価向けの追加 8 カット（下表）。追加分は選定用特徴量キャッシュから
  ISO≤400・8〜17 時・EV100≥9 かつ §2 の 21 カット以外を候補にし、カメラ JPEG（1/4 縮小）の 64px ブロック
  Laplacian 分散の 90 パーセンタイルが高い順に並べ、上位の縮小版と最もディテールの多い 400px 等倍を目視して
  ピント・ブレを確認、連写の重複を避けて選んだ。昼の低 ISO で使えるのは 10-18mm と 55-210mm だけだった
  （30mm F1.4 は夜・室内が中心）。

  | ID | ISO | シャッター | F | 焦点距離 | 被写体 |
  |---|---|---|---|---|---|
  | DSC04670 | 100 | 1/125 | 4.0 | 16.5mm | 木の枝・葉（空抜け）、木の構造物 |
  | DSC04651 | 100 | 1/125 | 3.5 | 17mm | 草・葉、文字の書かれた看板 |
  | DSC04643 | 100 | 1/125 | 7.1 | 10mm | 草むら、遠景の山、看板 |
  | DSC04472 | 100 | 1/640 | 4.0 | 16.3mm | 木の枝・幹（曇天の空抜け） |
  | DSC04465 | 160 | 1/30 | 5.6 | 10mm | 岩・石段・葉、文字の看板 |
  | DSC04872 | 100 | 1/640 | 5.0 | 85mm | 観客席（人物・格子柄・文字） |
  | DSC04716 | 100 | 1/80 | 10 | 10mm | 岩・草の地表 |
  | DSC04697 | 100 | 1/100 | 7.1 | 10mm | 地層の崖・草 |

- full（原寸 6000x4000）は追加 8 カット + 低 ISO の DSC04880, DSC04755 + ISO 3200 の DSC04430, DSC04453 の 12 カット。
  preview（長辺 2400px）は 29 カット全部。ISO 帯: preview は low（<800）18 / mid（800〜1600）2 / high（>1600）9、
  full は low 10 / high 2。

### 9.3 バリアント

| variant | 内容 |
|---|---|
| camera | カメラ JPEG（基準）。preview は 2400px へ INTER_AREA 縮小、full は原寸 |
| rawpy | 現行エンジン（`ENGINE_VERSION` 17、既定プリセット `neutral_recovery`、撮影時シャープ ON）。preview は `max_edge=2400` で engine 内の INTER_AREA 縮小後にシャープ |
| rawpy_nosharp | 同上で**撮影時シャープだけ OFF**。ワーカー内で `engine._capture_sharpen_params` を `mock.patch` し gain=0 を返す（σ・コアリング閾値は元の値。`_apply_tone_array` は gain=0 で USM を飛ばす）。sharpness は `ADJUSTMENT_RANGES` で 0.8 未満にできないため。engine は変更しない。出力先は `--out/rawpy_nosharp/` |
| ci_v9_l0 / ci_v9_l1 | Core Image neutral（sharpness/detail/NR/moire/contrast=0）, decoder 9, lens_correction OFF / ON |
| ci_v8_l0 / ci_v8_l1 | 同 decoder 8 |
| ci_apple_v9_l1 | Core Image `apple_default`（CIRAWFilter 既定のシャープ・NR）, decoder 9, lens ON |
| ci_v9_l1_usm / ci_v8_l0_usm | ci_v9_l1 / ci_v8_l0 の出力（JPEG q92）に、rawpy と同じ後段仕上げを Python で適用。`engine.resolve_adjustments(DEFAULT_PRESET)` の値で `engine._apply_tone_array` をそのまま呼ぶ（brightness 1.0 / contrast 1.03 / saturation 1.02 / sharpness 1.04 と撮影時シャープ。ISO は EXIF）。shadow_lift（リサイズ前段）は含めない |

- 縮小経路はエンジンごとのものをそのまま評価対象にする: rawpy は engine 内の INTER_AREA、Core Image は
  `scaleFactor`（デコーダ内縮小、`max_edge: 2400`）。full はどちらも原寸。寸法差（rawpy 2400x1603 / 6000x4008）は
  再標本化せず中央クロップで揃えた（全行で `fit` は exact か crop、resampled は 0 件）。
- 非対称性: `*_usm` は JPEG q92 をデコードしてからシャープするので、rawpy（エンコード前にシャープ）より
  JPEG ノイズをわずかに増幅しうる。rawpy と Core Image の出力はどちらも q92。

### 9.4 指標の定義（すべて `scripts/compare_engines.py` の純粋関数、`tests/test_compare_engines.py` で検証）

| 指標 | 定義 |
|---|---|
| ROI | カメラ JPEG の輝度で、σ1 ぼかし後の \|Laplacian\| 平均が高い重ならない正方ブロック上位 3 個（detail1〜3）+ 中央 + 左上隅（端から 1/8 内側）。1 辺は出力長辺の 10%（preview 240px、full 600px で同じ画角） |
| 位置合わせ | ROI ごとに、カメラ JPEG とバリアントの輝度（±ROI/4 の余白付き）を正規化して Hann 窓付き位相相関（`cv2.phaseCorrelate`）で平行移動を推定。response < 0.08 またはずれ > ROI/4 の ROI はそのバリアントで除外（全 410 行中、除外 41 ROI。10mm の 5 カットでの lens OFF バリアント（樽型歪曲で位置が大きくずれる）と、ISO 3200 の 4 カットの preview の ci_v9_l1 / ci_v9_l1_usm（白点ノイズで相関が落ちる）。各行の除外数は `roi_excluded`）。スケール差（レンズ補正の autoscale 等、数 %）は補正しない |
| lap_ratio / ten_ratio | ROI 内の Laplacian 分散 / Tenengrad（Sobel ksize3 の gx²+gy² 平均）を、カメラ JPEG の同 ROI で割った比。detail は detail1〜3 の比の中央値。**ノイズでも上がる** |
| hf_ratio | ROI の Hann 窓付き 2D パワースペクトルで、0.25〜0.5 cycles/px（ナイキストの上半分）のエネルギー / 全エネルギー（DC 除く）。detail ROI の比の中央値。これもノイズで上がる |
| edge_width_px | カメラ JPEG の detail1〜3 と中央 ROI から、構造テンソルのコヒーレンス ≥0.8・コントラスト ≥20 階調・両側が平坦・点がエッジ中央（30〜70%）にあるものを 16px セルごとに 1 点（ROI あたり最大 24 点）選び、勾配方向（暗→明）の ±8px を 0.25px 刻み・接線方向 5 本平均で双一次補間したプロファイル（ESF）から 10–90% 立ち上がり幅を計測。プラトーは両端 2px の中央値（min/max ではない＝オーバーシュートで幅が縮んで見えない）。同じ点を各バリアントで ROI の位置合わせ分ずらして計測し、点ごとの中央値 |
| edge_width_ratio | 同じ点での（バリアントの幅 / カメラ JPEG の幅）の中央値。**主指標**（ノイズで小さくならない）。1 未満はカメラより急峻 |
| flat_noise | §3 と同じ（画像全体の平坦タイルのハイパス σ、8bit）。flat_noise_ratio はカメラ比 |

ガウスぼかしステップの 10–90% 幅 = 2.563σ をユニットテストで確認している。計測点数の中央値は preview 約 50〜60 点、
full 約 95 点 / カット。preview の幅は 2400px 画素単位、full は原寸画素単位なので、**モードをまたいで px を比べない**。

### 9.5 集計（カット中央値）

**preview（2400px）**、29 カット（ISO low 18 / mid 2 / high 9）:

| variant | edge 幅 px | edge 幅比 | lap 比 | Tenengrad 比 | HF 比 | flat_noise | ノイズ比 |
|---|---|---|---|---|---|---|---|
| camera | 2.76 | 1.00 | 1.00 | 1.00 | 1.00 | 0.89 | 1.00 |
| rawpy（現行） | 3.08 | **1.05** | 0.66 | 0.66 | 1.03 | 1.85 | 1.55 |
| rawpy シャープ OFF | 3.34 | 1.10 | 0.39 | 0.58 | 0.65 | 1.48 | 1.34 |
| CI v9 lens OFF | 3.31 | 1.08 | 0.39 | 0.75 | 0.54 | 0.37 | 0.49 |
| CI v9 lens ON | 3.09 | 1.15 | 0.43 | 0.69 | 0.44 | 1.80 | 1.56 |
| CI v8 lens OFF | 3.25 | 1.10 | 0.52 | 0.73 | 0.55 | 1.15 | 1.23 |
| CI v8 lens ON | 3.01 | 1.10 | 0.43 | 0.72 | 0.45 | 1.12 | 1.12 |
| CI apple_default v9 lens ON | 2.17 | **0.95** | 1.05 | 1.26 | 1.28 | 1.14 | 1.02 |
| CI v9 lens ON + 現行仕上げ | 3.06 | 1.07 | 0.67 | 0.87 | 0.66 | 2.19 | 1.94 |
| CI v8 lens OFF + 現行仕上げ | 3.11 | **1.03** | 0.81 | 0.95 | 0.86 | 1.67 | 1.62 |

**full（原寸 6000px）**、12 カット（ISO low 10 / high 2）:

| variant | edge 幅 px | edge 幅比 | lap 比 | Tenengrad 比 | HF 比 | flat_noise | ノイズ比 |
|---|---|---|---|---|---|---|---|
| camera | 1.62 | 1.00 | 1.00 | 1.00 | 1.00 | 0.70 | 1.00 |
| rawpy（現行） | 1.92 | **1.16** | 0.52 | 0.49 | 0.62 | 1.55 | 2.20 |
| rawpy シャープ OFF | 2.38 | 1.35 | 0.26 | 0.36 | 0.32 | 1.12 | 1.55 |
| CI v9 lens OFF | 2.58 | 1.41 | 0.20 | 0.42 | 0.17 | 0.18 | 0.32 |
| CI v9 lens ON | 2.60 | 1.44 | 0.20 | 0.45 | 0.17 | 0.64 | 0.71 |
| CI v8 lens OFF | 2.53 | 1.29 | 0.36 | 0.53 | 0.39 | 0.89 | 1.13 |
| CI v8 lens ON | 2.47 | 1.32 | 0.30 | 0.52 | 0.33 | 0.84 | 1.07 |
| CI apple_default v9 lens ON | 1.41 | **0.84** | 1.06 | 1.36 | 0.94 | 0.54 | 0.81 |
| CI v9 lens ON + 現行仕上げ | 2.12 | 1.24 | 0.37 | 0.61 | 0.34 | 0.82 | 0.99 |
| CI v8 lens OFF + 現行仕上げ | 2.14 | **1.16** | 0.86 | 0.76 | 0.79 | 1.30 | 1.64 |

**ISO 帯別**（edge 幅比 / ノイズ比。n は各行のカット数）:

| variant | preview low (18) | preview high (9) | full low (10) | full high (2) |
|---|---|---|---|---|
| rawpy（現行） | 1.05 / 1.31 | 1.04 / 1.85 | 1.16 / 2.16 | 1.11 / 4.54 |
| rawpy シャープ OFF | 1.15 / 1.10 | 1.07 / 1.72 | 1.35 / 1.52 | 1.16 / 3.93 |
| CI v9 lens OFF | 1.10 / 0.82 | 1.05 / 0.26 | 1.42 / 0.42 | 1.28 / 0.17 |
| CI v9 lens ON | 1.16 / 1.21 | 1.05 / 1.75 | 1.47 / 0.71 | 1.31 / 1.00 |
| CI v8 lens OFF | 1.12 / 1.09 | 1.03 / 1.84 | 1.35 / 1.12 | 1.23 / 1.49 |
| CI v8 lens ON | 1.14 / 1.04 | 1.04 / 1.56 | 1.36 / 1.04 | 1.21 / 1.39 |
| CI apple_default v9 lens ON | 0.94 / 0.95 | 0.99 / 1.05 | 0.83 / 0.78 | 1.00 / 1.28 |
| CI v9 lens ON + 現行仕上げ | 1.07 / 1.75 | 1.06 / 1.94 | 1.21 / 0.99 | 1.30 / 1.08 |
| CI v8 lens OFF + 現行仕上げ | 1.04 / 1.46 | 1.03 / 2.04 | 1.13 / 1.63 | 1.19 / 1.78 |

（mid 帯は preview 2 カットのみなので表から省略。`sharpness_tables.md` にある。）

**カット単位の対比較**（edge 幅比が rawpy 現行より小さいカット数 / ノイズが rawpy 現行より大きいカット数）:

| variant | preview low (18) | preview mid+high (11) | full low (10) | full high (2) |
|---|---|---|---|---|
| CI v9 lens OFF | 2 / 3 | 3 / 0 | 0 / 0 | 0 / 0 |
| CI v9 lens ON | 0 / 8 | 3 / 6 | 0 / 1 | 0 / 0 |
| CI v8 lens OFF | 2 / 5 | 6 / 5 | 1 / 2 | 0 / 0 |
| CI apple_default v9 lens ON | 18 / 6 | 11 / 1 | 10 / 2 | 2 / 0 |
| CI v9 lens ON + 現行仕上げ | 5 / 13 | 6 / 8 | 1 / 3 | 0 / 0 |
| CI v8 lens OFF + 現行仕上げ | 9 / 12 | 7 / 7 | 8 / 2 | 0 / 0 |

### 9.6 目視所見（等倍クロップ、11 カット）

`sheets/` の detail1（一部 center）を、さらに中央 128px を 3 倍（最近傍）に拡大して見た。対象: DSC04670 / DSC04643 /
DSC04716 / DSC04872 / DSC04880 / DSC04430 / DSC04453（full）、DSC04651 / DSC04433 / DSC04758 / DSC04464（preview）。

- **カメラ JPEG**: どのカットでも最もキレがある。細い枝・草の葉脈・格子柄・文字の輪郭が締まり、局所コントラストも高い。
  ISO 3200（DSC04430 / DSC04453）では強い NR で平坦部は滑らかだが、毛並み・地表の線は残っている。
- **rawpy 現行**: preview ではカメラにかなり近い（細部の描写はほぼ同等、ややコントラストが低い）。**full ではカメラより
  明確に甘い**（DSC04716 の草、DSC04670 の葉で輪郭がにじむ）。DSC04880 のコンクリートの縁の上端など高コントラストの
  境界に細い明るい縁（軽いハロー）が出る。ISO 3200 では赤・青の粗い色ノイズが全面に乗る（シャープ OFF でも同じで、
  シャープ由来ではなくデモザイク + NR なしの色ノイズ）。
- **rawpy シャープ OFF**: 現行より一段眠い。preview でも枝・草が柔らかくなり、「ピントが甘い」と言われる見え方に近い。
- **CI neutral decoder 9（lens OFF / ON とも）**: full では**最も甘い**。葉・草が水彩風に平らになり（DSC04643, DSC04670）、
  人物の顔・格子柄が蝋のように滑らかになる（DSC04872）。ISO 3200 では色ノイズは無いが毛並み・地表の細線まで塗り潰される
  （DSC04430, DSC04453）。full では lens OFF と ON の差は小さい（どちらも平滑化される）。
- **CI neutral decoder 9 + lens ON の preview**: 高 ISO の暗部に**白い点状のノイズ（塩粒状のインパルス）**が散る
  （DSC04433, DSC04758 で顕著）。lens OFF、decoder 8、apple_default、full ではこの点は出ない。§6.3 で「lens ON で
  粒状ノイズが現れる」と書いた現象の正体はおそらくこれで、**解像が回復しているわけではない**（full の detail Laplacian は
  lens OFF/ON とも 0.20）。preview の ci_v9_l1 の lap 比・ノイズが高 ISO で大きく出るのもこの点の寄与。
- **CI neutral decoder 8**: v9 より明らかに細部が残る（rawpy シャープ OFF と同程度の柔らかさ）。ISO 3200 では
  rawpy と同程度か細かい色ノイズが出る（NR 0 なので当然）。lens OFF / ON の差は小さい。
- **CI apple_default（v9, lens ON）**: カメラ JPEG に最も近いキレ。低 ISO では枝・草の輪郭がカメラ並みかそれ以上に立ち、
  エッジ幅もカメラより狭い。その分、葉の重なりで明暗が硬く「ガリッとした」質感になり、明るい点（白い粒）が目立つ箇所が
  ある（DSC04670）。ISO 3200 では色ノイズを抑えつつ毛並み・地表の線を残しており（DSC04430, DSC04453）、v9 neutral の
  塗り潰しとは別物。明確な白縁ハローは目視では見つからなかったが、局所コントラストは強め。
- **CI + 現行仕上げ**: v8 lens OFF + 仕上げは rawpy 現行とほぼ同じ見え方で、full ではやや締まって見える。
  v9 lens ON + 仕上げは v9 の平滑化の上にシャープが乗るだけで、細部は戻らない（輪郭だけ硬い「塗り絵」になる）。
  preview の高 ISO では上記の白点を増幅する。

### 9.7 結論（判断材料）

1. **Core Image に替えるとピント甘さが改善するかは、設定次第で逆向きになる。**
   - **neutral（シャープ 0）のままなら悪化する**。とくに decoder 9 は lens の有無にかかわらず原寸で最も甘い
     （エッジ幅比 1.41〜1.44、rawpy 現行は 1.16）。§6.3 の「v9 + lens OFF だけが特に滑らか」は全画像 Laplacian に
     基づく所見で、その差は主に平坦部ノイズ（原寸 flat_noise 0.18 vs 0.64）の差だった。ディテール ROI の解像は
     lens OFF / ON で同じ（Laplacian 比 0.20 / 0.20、エッジ幅比 1.41 / 1.44）で、v9 は lens ON でも平滑化される。
     v9 + lens ON の preview（scaleFactor 縮小）には高 ISO で白点ノイズも出る。
   - **apple_default（デコーダ内シャープ・NR 既定）なら改善する**。エッジ幅比は preview 0.95 / full 0.84 で、
     29/29（preview）・12/12（full）カットで rawpy 現行より狭く、原寸のノイズは rawpy の約 1/3（full の flat_noise 0.54 vs 1.55）。
     見た目もカメラ JPEG に最も近い。ただしパラメータは非公開で ISO・decoderVersion・OS で変わりうる（§7）、
     局所コントラストが強めで硬い質感になることがある。
   - **デコーダの素の解像（シャープ 0 同士）**: rawpy シャープ OFF（full 1.35）≒ CI v8（1.29〜1.32）＞ CI v9（1.41〜1.44）。
     つまり**素のデモザイク解像は rawpy（AHD）と CI decoder 8 が同等、decoder 9 が劣る**。
   - **同じ仕上げを掛けた比較**: CI v8 lens OFF + 現行仕上げは rawpy 現行と同等（full 1.16 vs 1.16、full low で 8/10 カット
     わずかに狭い）だが、ノイズも同程度に出る。CI v9 lens ON + 仕上げは rawpy 現行より甘い（1.24）。
     **エンジンを替えるだけでは改善せず、改善分はほぼ「シャープ（+NR）の掛け方」で決まる。**
2. **条件の整理**: 解像を保つ組み合わせは decoder 8（neutral + 後段シャープ）か decoder 9 の apple_default。
   decoder 9 の neutral は lens 設定によらず避けるべき候補。lens ON / OFF 自体の解像差は v8 では小さい（1.29 vs 1.32）。
3. **現行 rawpy の残課題との関係**（2026-09 調査の寄与順: シャープ実質ゼロ ＞ AREA 縮小 ＞ FBDD light ＞ remap）:
   - **撮影時シャープ**: 今回 rawpy シャープ OFF → 現行で edge 幅比が preview 1.10→1.05、full 1.35→1.16 に改善しており、
     既定化は効いている。ただし**原寸ではまだカメラ JPEG より 16% 太い**（preview は 5%）。原寸側の gain（0.7）が
     カメラに対して弱く、full の甘さが残る主因はここ。Core Image apple_default が原寸で勝つのも主にこの差。
   - **AREA 縮小**: preview では rawpy 現行（AREA 縮小 → シャープ）と CI（scaleFactor 縮小）で幅比に大差が無く
     （v8 neutral 1.10、rawpy シャープ OFF 1.10）、縮小経路そのものの差は小さい。CI の scaleFactor 縮小は v9 + lens ON で
     白点ノイズを生むので、縮小経路としてはむしろリスク。
   - **FBDD light**: 低 ISO（<800）は既に Off。高 ISO の rawpy は色ノイズが大きく（full high のノイズ比 4.5）、
     「NR が強くて甘い」ではなく「NR が無くてざらつく」状態。CI apple_default は高 ISO で解像とノイズの両立が最も良い。
   - **レンズ補正 remap**: CI v8 lens OFF / ON の差（full 1.29 vs 1.32）は小さく、Core Image のレンズ補正は解像をほとんど
     落とさない。rawpy の remap（CUBIC）の単独寄与は今回分離していない（rawpy はレンズ補正 ON の 1 条件のみ）。
   - **FullSizeViewer の引き伸ばし**: 1600px プレビューを拡大表示する限り、どのエンジンでも 100% 表示は甘い。
     エンジン選択とは独立の UI 課題で、今回の数値（2400px・原寸）はその手前の画質。
4. 判断に効く数値の要約: 「現行 rawpy の原寸の甘さ（幅比 1.16）」を Core Image で解消できるのは apple_default（0.84）のみで、
   neutral + 現行仕上げでは解消しない（v8 1.16 / v9 1.24）。rawpy 側で原寸のシャープ強度を上げる選択肢と比較すること
   （本節では rawpy のシャープ強度の掃引はしていない）。

### 9.8 この節の限界・未確認事項

- エッジ幅は自動検出した「強い直線エッジ」の 10–90% 幅で、テクスチャの解像（MTF）そのものではない。カメラ JPEG は
  撮影時シャープとトーンが強いので、比 1.0 は「カメラと同じ急峻さ」であって理想値ではない。
- オーバーシュート（ハロー）量は数値化していない（プラトーを外側中央値で取るので幅には効かない）。apple_default と
  rawpy 現行のハローは目視のみ。
- 位置合わせは ROI 単位の平行移動のみ。レンズ補正の有無によるスケール差（数 %）は補正していない（幅への影響は数 % 以内）。
  lens OFF の 10mm カットでは位置合わせ不能で除外された ROI がある（9.4）。
- full は 12 カットで、高 ISO は 2 カットのみ。mid ISO の full、星景・夜景（長秒）は無い。
- `*_usm` は q92 JPEG からの後処理で、rawpy 内でのシャープとは厳密には同一でない（9.3）。
- preview の基準（カメラ JPEG）はハーネスが INTER_AREA で 2400px に縮小したもの。preview の比 1.0 は「AREA 縮小したカメラ JPEG と同じ急峻さ」で、
  経路としては rawpy（AREA 縮小）に近く、Core Image の scaleFactor 縮小とは異なる。
- 隅（corner）ROI の比は周辺減光補正によるノイズ増幅の影響が大きく（rawpy full で 2.96 等）、解像の指標としては使っていない。
- Core Image apple_default の内部パラメータ（ISO 依存のシャープ・NR 量）は API から読めない値があり、OS 更新での変化は未確認。
- 目視は 11 カット・各 1〜2 ROI。ブラインド比較ではない。
