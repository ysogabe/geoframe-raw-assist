# Core Image と rawpy のエンジン比較（Issue #65 PR3）

現行エンジン（rawpy/LibRaw, `local_raw/engine.py`）と Core Image CLI（`geoframe-ci-develop`,
[`native/coreimage-cli/README.md`](../native/coreimage-cli/README.md)）を、同じ α6400 ARW 21 カットで
比較した記録（§9 は解像感＝ピント甘さに絞った追加評価、29 カット。§10〜§15 は ADR 0002 の移行条件の確認＝夜景・星空・長秒 #70、ブラケットの前提、白点 #77、ハイライト #71 の道具）。**採否の判断は ADR 0002 で行う**。この文書は事実・所見・判断材料（論点）までに留める。

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

## 10. 夜景・星空・長秒の評価（Issue #70、設計書 §4.1）

### 10.1 目的

ADR 0002「置き換えに進む条件」のうち最重要とされた「夜景・星空・長秒のサンプルで破綻（ノイズの塗りつぶし、星の消失、色かぶり）が
無い。`apple_default` の NR が星を消さないこと」を、実データで確かめる。あわせて設計書 §4.1 の観点（空の明るさ＝Issue #50 の再発確認、
空のノイズ・色ノイズ、白点ノイズ、周辺減光補正の四隅への影響）を同じ出力で測る。Issue #79 の CLI 拡張（settings の個別上書き）を使い、
「`apple_default` のトーン・色のまま NR だけ 0」のバリアントも並べる。

### 10.2 手順（再現コマンド）

```sh
npm run build:coreimage            # CLI 0.2.0（#79 の output_color_space / settings 上書き入り）
RAW=/Users/yoshio/works/60-misc/40-raw-assistant/work/raw   # 星空 11 組（ARW は小文字 .arw、JPG は .JPG）
.venv/bin/python scripts/compare_engines.py --source-root $RAW \
  --captures DSC07402 DSC07404 DSC07405 DSC07407 DSC07408 DSC07412 DSC07415 DSC07416 DSC07419 DSC07422 DSC07423 \
  --night --full --full-ids DSC07415 DSC07407 DSC07402 --night-scales DSC07415 DSC07407 \
  --out work/engine-comparison/night1
# #77 の原因切り分けバリアント（§12）を CI_SCALE_REQUESTS に足した後の掃引だけの再実行
.venv/bin/python scripts/compare_engines.py --source-root $RAW --captures DSC07415 DSC07407 \
  --night --night-scales DSC07415 DSC07407 --out work/engine-comparison/night1-scale
```

- 実行: 2026-10-20、§1 と同じマシン（macOS 27.0.1 / M3）。11 カット × preview（長辺 2400px）+ 3 カット × full（原寸）+ 2 カットの
  scale 掃引。night1 は 15.6 分、night1-scale は 6.0 分。エラー 0 件。1 カットにつき CI 9 バリアント + rawpy を 4 並列で現像し、
  preview では各 CI バリアントの原寸レンダも作る（§10.4 の `impulse_vs_full` の基準）。処理時間はこの節では比較しない。
- 出力（gitignore 対象の `work/engine-comparison/night1/`）: `night_metrics.csv`（カット × モード × バリアント）、`night_scale_metrics.csv`、
  `night_summary.json`、`night_tables.md`、`sheets/<ID>_<mode>_{sky,stars,corner}_g{1,2,3}.jpg`（160px パッチを最近傍 2 倍、
  4 バリアントずつ、ラベル付き、EXIF なし）。`rawpy/` 配下の rawpy 出力そのものは §3 と同じくペア JPG の EXIF を継承している。
- **サンプル**: `scripts/eval_cases/candidates_baseline.json` の `stars` セット 11 組。**1 セッション（2022-11-19 夜）、レンズ 2 本
  （Sony E 16mm F2.8、Sigma 30mm F1.4）、ISO 3200 × 6 / 6400 × 5**。10〜30 秒の三脚長秒が 10 枚（空だけのカットと地上の前景を含む
  カットが混在）、DSC07402 だけ 1/13 秒の手持ち夜景（ISO 3200。`classify_scene` では `night`、星空ではない）。
  空は薄明・光害で明るめ（カメラ JPEG の空の平均輝度 0.11〜0.44、中央値 0.28）。**暗い空（0.2 未満）は 4 カット**
  （DSC07402 / 07404 / 07405 / 07419）。rawpy は全カットで暗所判定（`dark_scene` = true、固定倍率・ビネット補正スキップ）。
- ハーネスの ARW / JPG 探索は `local_raw.source_files` 経由にし、拡張子の大文字小文字を問わなくした（この評価 PR で修正）。

### 10.3 バリアント

| variant | 内容 |
|---|---|
| camera | カメラ JPEG（基準）。preview は 2400px へ INTER_AREA 縮小、full は原寸 |
| rawpy | 現行エンジン（`ENGINE_VERSION` 17、既定プリセット、暗所判定あり） |
| ci_apple_v9_l1 | **ADR 0002 の基本路線**: `apple_default`、decoder 9、lens ON |
| ci_apple_v9_l1_nr0 | 同上 + `luminance_noise_reduction: 0, color_noise_reduction: 0`（#79。この ISO では color NR は元々 0 なので実質は輝度 NR 0.241 / 0.357 → 0） |
| ci_apple_v9_l1_nr0_sh0 | 同上 + `sharpness_amount: 0` |
| ci_apple_v9_l0 / ci_apple_v9_l0_nr0 | `apple_default` v9 で lens OFF（周辺減光補正の影響の把握用）/ + NR 0 |
| ci_apple_v8_l1 / ci_apple_v8_l1_nr0 | `apple_default` decoder 8、lens ON / + NR 0 |
| ci_v9_l1 / ci_v8_l1 | neutral（sharpness / detail / NR / moire / contrast / EDR = 0）decoder 9 / 8、lens ON |

CLI 応答の `applied`（`coreimage/<mode>/*.json` に保存）で確認した `apple_default` v9 の実効値: ISO 3200 は sharpness 1.0・
輝度 NR 0.241、ISO 6400 は sharpness 0.806〜0.826・輝度 NR 0.357。**color NR・detail・contrast・moire・EDR はどちらの ISO でも 0**、
boost 1.0・boostShadow 0.9。したがってこのサンプルでは `apple_default` = neutral + sharpness + 輝度 NR であり、実際に
`ci_apple_v9_l1_nr0_sh0` と `ci_v9_l1` の指標は全カットで一致した（CSV で同値）。decoder 8 の `apple_default` は輝度 NR 0.676（ISO 3200）で
color NR も有効（色ノイズが NR 0 版の 1/13）。

### 10.4 指標の定義（`scripts/compare_engines.py` の純粋関数、`tests/test_compare_engines.py` で検証）

| 指標 | 定義 |
|---|---|
| point_count / point_density | `development_candidates._point_light_count` そのもの（gray − σ3 ガウス ≥ 0.04 の小連結成分で最大差 ≥ 0.08）。PSO の星判定と同じ検出。**高 ISO では粒状ノイズも拾う**（カメラ JPEG で 2400px あたり 1.4 万点）ので、絶対数ではなく比（point_ratio）と下の recall で読む |
| point_count_strong / point_strong_ratio | 同じ検出で最大差 ≥ 0.16 に絞った「強い星」の数 / カメラ比 |
| **star_recall** | カメラ JPEG の強い星（≥ 0.16）の成分のうち、バリアント側に点（≥ 0.08）が残っている割合。カメラの星成分をセル単位（6×8）の位相相関シフトで動かし、2px 膨らませた足跡で判定。**星の残り方の主指標** |
| star_count / star_halo / star_color_saturation | `development_candidates._star_measurements`（99.35 パーセンタイル閾値の点、周辺リングの輝度比、星ごとの白飛び割合）。star_count は画像ごとの相対閾値なので NR の有無で動きにくい |
| sky_mean_luma / sky_luma_diff | 空領域（カメラ JPEG の 32px タイルで、構造が小さい下位 25%。全バリアント同じタイル。星候補を 3px 膨らませて除外）の Rec.709 輝度平均 / カメラ JPEG との差 |
| sky_noise / sky_noise_ratio | 同じ空画素でのハイパス（原画 − σ1.5）標準偏差（8bit）/ カメラ比。flat_noise と同じ量 |
| sky_chroma_noise | 空画素での Lab a\*, b\* のハイパス標準偏差の二乗和平方根（色ノイズ） |
| sky_hf_ratio | 空画素のハイパス分散 / 全分散。NR の塗りつぶしで小さくなる（補助） |
| impulse_count | 3×3 メディアンより 24 階調以上明るい面積 ≤ 3px の孤立点のうち、周囲 1px がすべて暗く、カメラ JPEG の星候補（セル単位で位置合わせ・膨張）に触れないもの。**lens OFF のバリアントでは位置合わせの残差で星が漏れ込むので使わない** |
| **impulse_vs_full** | 同じ設定の原寸レンダを INTER_AREA で同寸に縮めたものを基準にし、「preview にあって原寸縮小には無い孤立点」の数。幾何が同一なので位置合わせに依らない。**白点（scaleFactor 経路の生む点）の主指標**（preview のみ） |
| center_noise / corner_noise / corner_noise_ratio | 短辺 15% の箱（中央 / 四隅の中央値）での flat_noise とその比 |
| corner_luma_ratio | 四隅の平均輝度 / 中央の平均輝度（周辺減光補正の効き） |
| camera_delta_e76 / camera_luma_diff / flat_noise / mean_luma 等 | §3 と同じ |

### 10.5 集計（preview 2400px、11 カット中央値。括弧は最小..最大）

**星の残り方・白点**:

| variant | star_recall | point_strong_ratio | star_halo | star_color_sat | impulse_vs_full |
|---|---|---|---|---|---|
| camera | 1.00 | 1.00 | 0.539 | 0.014 | — |
| rawpy | 0.991 (0.970..1.000) | 0.68 | 0.594 | 0.052 | —（impulse_count 3） |
| **ci_apple_v9_l1** | **1.000 (0.991..1.000)** | 1.11 | 0.494 | 0.064 | **1 (0..8)** |
| ci_apple_v9_l1_nr0 | 1.000 (0.992..1.000) | 1.13 | 0.494 | 0.065 | 1 (0..8) |
| ci_apple_v9_l1_nr0_sh0 (= ci_v9_l1) | 0.997 (0.990..1.000) | 0.82 | 0.588 | 0.043 | **5746 (123..34353)** |
| ci_apple_v8_l1 | 0.987 (0.960..1.000) | 0.59 | 0.582 | 0.043 | 0 (0..3) |
| ci_apple_v8_l1_nr0 | 0.989 | 0.62 | 0.582 | 0.051 | 0 |
| ci_v8_l1 | 0.989 (0.983..1.000) | 0.62 | 0.583 | 0.050 | 0 (0..1) |
| ci_apple_v9_l0 / _nr0 | 0.985 / 0.986（lens OFF は位置合わせの残差を含む） | 1.29 / 1.33 | 0.497 | 0.062 | 1 / 2 |

**空**:

| variant | sky_luma_diff | sky_noise_ratio | sky_chroma_noise | sky_hf_ratio | corner_noise_ratio | corner_luma_ratio | ΔE76 |
|---|---|---|---|---|---|---|---|
| camera | 0 | 1.00 | 0.33 | 0.035 | 0.84 | 0.625 | — |
| rawpy | +0.015 (−0.024..+0.029) | 1.72 (1.19..2.88) | 6.05 | 0.166 | 1.08 | 0.694 | 8.56 |
| **ci_apple_v9_l1** | **+0.013 (−0.013..+0.016)** | **1.05 (0.89..1.24)** | 0.43 | 0.048 | 0.66 | 0.635 | 3.59 |
| ci_apple_v9_l1_nr0 | +0.013 (−0.012..+0.016) | 1.36 (1.22..1.40) | 0.44 | 0.059 | 0.71 | 0.636 | 3.66 |
| ci_apple_v9_l1_nr0_sh0 (= ci_v9_l1) | +0.029 | 2.47 (1.25..4.55) | 0.59 | 0.169 | 1.06 | 0.650 | 4.71 |
| ci_apple_v9_l0 / _nr0 | +0.012 / +0.013 | 1.44 / 1.70 | 0.60 / 0.60 | 0.070 / 0.100 | 0.74 / 0.76 | 0.616 / 0.618 | 4.56 / 4.64 |
| ci_apple_v8_l1 | +0.011 | 0.79 | 0.34 | 0.032 | 0.74 | 0.633 | 3.20 |
| ci_apple_v8_l1_nr0 / ci_v8_l1 | +0.010 | 1.59 | 4.51 | 0.110 | 0.95 | 0.631 | 7.30 |

ISO 帯別（`ci_apple_v9_l1` / rawpy）: star_recall ISO 3200 1.000 / 0.978、6400 0.995 / 0.993。sky_noise_ratio 3200 1.20 / 1.31、
6400 0.97 / 1.82。sky_chroma_noise 3200 1.01 / 5.22、6400 0.41 / 7.70。

**full（原寸、3 カット: DSC07415 ISO 3200 10s / DSC07407 ISO 6400 30s / DSC07402 手持ち）**:

| variant | star_recall | sky_noise_ratio | sky_chroma_noise | impulse_count | ΔE76 |
|---|---|---|---|---|---|
| camera | 1.00 | 1.00 | 0.21 | 0 | — |
| rawpy | 0.980 (0.979..0.984) | 1.81 (1.47..2.05) | 4.19 | 4 (2..33) | 9.33 |
| ci_apple_v9_l1 | 0.989 (0.965..0.998) | 1.16 (1.11..1.27) | 0.44 | 8 (0..41) | 3.95 |
| ci_apple_v9_l1_nr0 | 0.989 (0.972..0.998) | 1.41 (1.33..1.96) | 0.46 | 9 (1..50) | 4.02 |
| ci_v9_l1 (= apple NR0 sharp0) | 0.973 | 0.78 | 0.39 | 0 | 3.93 |
| ci_apple_v8_l1 | 0.989 | 1.16 | 0.33 | 6 | 3.82 |

**周辺減光補正（lens ON / OFF の差、`apple_default` v9、preview 11 カット）**: corner_luma_ratio は lens ON 0.635 / OFF 0.616
（カット別の差は +0.002〜+0.020）、カメラ JPEG 0.625。corner_noise_ratio は ON 0.66 / OFF 0.74。**CI のレンズ補正は四隅をほとんど
明るくしておらず（差 3% 未満）、四隅のノイズも増やしていない**（設計書の目安「中央の 1.5 倍以内」を満たす）。rawpy はビネット補正を
スキップしているのに corner_luma_ratio 0.694 と最も明るいが、これは暗所の固定倍率とトーンの差で、補正の効きではない【推測】。

### 10.6 目視所見（2 倍拡大パッチ、EXIF なし）

見たシート（それ以外は見ていない）: `night1/sheets/` の DSC07402_preview_sky_g1、DSC07402_full_stars_g1、DSC07404_preview_stars_g1、
DSC07404_preview_sky_g2、DSC07407_preview_{sky_g1,corner_g1,sky_g3,corner_g2}、DSC07415_preview_stars_g2、DSC07415_full_{stars_g1,sky_g1,sky_g2}、
および同じ設定の試走 `night-smoke2/sheets/` の DSC07415_preview_{sky_g1,corner_g2}。ブラインド比較ではない。

- **星（DSC07415 preview / full、ISO 3200 30mm）**: `ci_apple_v9_l1` はカメラ JPEG より星が小さく締まり、青白〜橙の星の色が残る。
  NR 0 との見た目の差は小さく、NR 0 の方が粒子がやや細かく見える程度。rawpy は全面に赤・青の粗い色ノイズが乗り、星の色はノイズに
  紛れる。原寸ではカメラ JPEG が強い NR で空が筆跡状に塗られ星が白い塊になるのに対し、CI `apple_default` は細かい粒子と色付きの
  星が残る。decoder 8 の `apple_default`（輝度 NR 0.68）は原寸でカメラ JPEG に近い塗り方になる。
- **空（DSC07407 preview、ISO 6400 30s 16mm、明るい空）**: `ci_apple_v9_l1` はカメラ JPEG と同程度に滑らかで微光星も残る。
  NR 0 は細かい粒子が見えるがブロッチ（塗りつぶしの島）は無い。rawpy は輝度・色ノイズともに明らかに多い。§9.6 で neutral v9 の
  原寸に見た「水彩・蝋状」の塗りつぶしは、`apple_default` の preview / full では見つからなかった（数値でも sky_hf_ratio 0.048 で
  カメラ 0.035 より高く、neutral v9 full の 0.008 のような低下は無い）。
- **白点**: `ci_v9_l1`（= apple NR 0 + sharp 0）の preview は、DSC07404 / 07407 の暗い空・隅に**白い点が全面に散る**（DSC07404 で
  3 万点超）。`ci_apple_v9_l1` と NR 0 だけの変種には見えない。§12 で切り分ける。
- **隅（DSC07407 corner）**: `ci_apple_v9_l1` の隅はカメラ JPEG と同じ暗さ・ノイズ感。lens OFF はごく僅かに暗い程度で、
  目視では区別しにくい。rawpy の隅は色ノイズで埋まる。
- **暗い空（DSC07402、手持ち 1/13s）**: 空はほぼ黒で、`apple_default` はカメラ JPEG と同じく黒く沈んだまま（灰白色に浮かない）。
  rawpy はノイズの粒が見えるぶん明るい印象を受けるが、空の平均輝度は 3 者とも同程度で、数値ではむしろカメラより暗い
  （sky_luma_diff は rawpy −0.024 / CI −0.013）。
  このカットの「stars」パッチは前景の光（水面の反射）で、星ではない。
- **DSC07404 の「stars」パッチ**も前景の木と灯りで、星の判断には使えない（星候補の検出が明るい前景の点を拾ったため）。

### 10.7 結論（#70 の判断材料）

1. **`apple_default` v9 lens ON の NR は星を消していない。** カメラ JPEG の強い星の残存率は 11 カットすべてで 0.991 以上
   （中央値 1.000）、rawpy 現行（0.970..1.000）以上。強い星の数はカメラ比 1.11 倍、ハロー指標はカメラ・rawpy より低い。
   NR を 0 にしても残存率は変わらず（1.000）、「NR が消した星の数」は検出できる範囲では 0 だった。
2. **空の明るさは Issue #50 の再発に当たらない。** `apple_default` の空の輝度差はカメラ比 −0.013..+0.016（目安 ±0.05 の内側）で、
   rawpy（−0.024..+0.029）より狭い。暗い空のカットでも浮かず、沈まない。
3. **空のノイズは rawpy 現行より少なく、塗りつぶしも無い。** sky_noise_ratio 1.05（rawpy 1.72）、色ノイズは rawpy の 1/14。
   NR 0 にすると 1.36（それでも rawpy 以下）。
4. **白点は `apple_default` では実質出ない**（preview 2400px で impulse_vs_full 最大 8/枚。長辺 1600px では 1 カットで数十点出るが、同じ条件の neutral v9 の 1/25 以下。§12）。大量に出るのは NR 0 かつ sharpness 0（= neutral v9）の preview。
5. **周辺減光補正は四隅のノイズを増やさない**（lens ON/OFF で corner_noise_ratio 0.66 / 0.74）。そもそも CI の補正は四隅を
   ほとんど明るくしていない（3% 未満）。rawpy の「暗所でビネット補正をスキップ」に相当する措置は CI では要らない【推測: 他のレンズ
   （10-18mm 等）では未確認】。
6. **星の色の白飛び（star_color_saturation）は rawpy より僅かに多い**（0.064 vs 0.052）。`apple_default` のシャープと
   コントラストで明るい星の芯が飛びやすい。目視では星の色は残っているが、明るい星ほど白く見える傾向。

## 11. 長秒ブラケットの前提確認（設計書 §4.1、Issue #83 の前提）

### 11.1 手順

```sh
.venv/bin/python scripts/compare_engines.py --source-root $RAW --select auto --bracket --out work/engine-comparison/bracket1
```

`--bracket` は EXIF から「同一レンズ・焦点距離・向きで、600 秒以内に撮られ、|ΔEV| ≥ 0.3 の隣接カット」をペア候補にする
（`bracket_pairs`。`--bracket-pairs A,B` で明示も可）。各ペアを camera JPEG / rawpy（`bracket._decode_frame`、`no_auto_bright`、
レンズ補正なし）/ CI `apple_default` v9 lens ON の **sRGB tiff16**（#79 の `output_color_space: "srgb"`）で 2400px に揃え、
(a) 露出比 `exposure_ratio_stops`（両フレームで最大チャンネルが 8..240 の中央 60% 領域、sRGB を外してリニア輝度平均の比を log2）、
(b) 幾何 `corner_shift_consistency`（中央 + 四隅の位相相関シフト。カメラ JPEG のシフトとの差 `max_patch_deviation_from_camera_px`）、
(c) `bracket._align_frames` + `_merge_frames`（アプリと同じ AlignMTB / Mertens）の合成シートを出す。所要 25 秒。

### 11.2 結果

**三脚ブラケット（同一構図の露出違い連続カット）はサンプルに無かった。** 候補 3 ペアのうち:

| pair | ΔEV（EXIF） | 構図 | 露出比（stop）camera / rawpy / CI | 幾何（カメラとの差）CI / rawpy |
|---|---|---|---|---|
| DSC07405→07407（10s→30s） | +1.585 | **別構図**（相関が取れるパッチ 0） | 2.95 / 2.46 / 2.78（別の内容を比べているので無効） | n/a |
| DSC07412→07415（ISO 6400→3200） | −1.000 | **別構図**（パッチ 0〜1） | −1.05 / −0.93 / −1.03（参考値） | n/a |
| DSC07415→07416（f/1.4→f/1.6） | −0.385 | 同じ空だが**約 39px（2400px 時）動いている**。5 パッチすべて相関あり | −0.23 / −0.18 / −0.21 | **0.92 px / 6.74 px** |

- **露出差の保存**: DSC07415→07416 で CI は −0.21 stop、カメラ JPEG −0.23、rawpy `no_auto_bright` −0.18。EXIF の −0.385 からは
  3 者とも同じ方向に 0.16〜0.20 stop ずれている（絞り 1/3 段の実際の露光差が EXIF どおりでない、または両フレームの間の空の明るさの
  変化によるもの【推測】）。**CI の露出比はカメラ JPEG と rawpy `no_auto_bright` の間に収まり、`apple_default` の処理が露出差を
  潰していない。** 設計書の目安「2^ΔEV の ±0.3 stop 以内」はカメラ JPEG 自身が満たさないため、「カメラ JPEG・rawpy と 0.05 stop
  以内で一致」と読み替えて満たしている。別構図の 2 ペアでも CI はカメラ JPEG と 0.03〜0.17 stop 差（参考）。
- **レンズ補正の幾何一致**: 2 フレーム間の動き（中央 −39.4px, +6.3px、隅で 33〜50px）はカメラ JPEG・CI・rawpy の全部に出ており
  実際の動き（三脚の揺れ・星の日周運動）。**CI の 5 パッチのシフトはカメラ JPEG（カメラ内レンズ補正）と最大 0.92 px で一致**し、
  放射状の不一致は無い。rawpy の 6.7 px はこの経路で lensfun を掛けていないための歪曲差であり、rawpy の欠点ではない。
  設計書の目安「四隅と中央が 1 px 以内」は、CI の幾何がカメラ内補正と 1 px 以内で一致している、という形で満たしている。
- **Mertens 合成の目視**: `bracket1/sheets/bracket_DSC07415-DSC07416_{center,ci_apple_v9_l1_overview}.jpg` を見た。39 px の動きは
  AlignMTB の探索上限（短辺の 2% = 32 px）を超えており、CI・rawpy とも星が二重になり、合成全体に波状の干渉模様が出る。
  **これは入力が三脚ブラケットでないために出るもので、CI 出力の問題ではない。** 同一構図のブラケットでの合成品質は**未確認**。
  rawpy 合成は `no_auto_bright` のため全体が暗い（平均輝度 0.175、CI 0.339）。
- sRGB tiff16 → OpenCV Mertens の経路（設計書 B3 の足場）は動いた（`bracket_pair_eval`）。原寸ではなく 2400px で回している。

## 12. decoder 9 の白点ノイズの原因切り分け（Issue #77、設計書 §4.3）

### 12.1 手順

§10.2 の `--night-scales`。DSC07415（ISO 3200、30mm）と DSC07407（ISO 6400、16mm）について、preview 長辺 1200 / 1600 / 2400 /
3000 / 4000（scaleFactor 0.2〜0.67）で 10 バリアントを現像し、各バリアント自身の原寸レンダ縮小版を基準に `impulse_vs_full` を数える。
原寸（scaleFactor 1.0）は §10 の full 行。

### 12.2 結果（2 カット中央値の impulse_vs_full。full 列は `impulse_count`、3 カット中央値）

| variant | 1200 | 1600 | 2400 | 3000 | 4000 | full |
|---|---|---|---|---|---|---|
| ci_v9_l1（neutral: NR 0・sharp 0） | 28 | 1539 | 2978 | **21839** | 14068 | 0 |
| ci_v9_l0（同 lens OFF） | 1 | **7104** | 0 | 2 | **46764** | — |
| ci_v9_l1_sh01（neutral + sharpness 0.1） | 3 | 56 | 1 | 4 | 0 | — |
| ci_v9_l1_nr024（neutral + 輝度 NR 0.24） | 3 | 38 | 0 | 2 | 0 | — |
| ci_apple_v9_l1 | 3 | 34 | 1 | 14 | 0 | 8 |
| ci_apple_v9_l1_nr0 | 3 | 41 | 1 | 18 | 0 | 9 |
| ci_apple_v9_l1_sh0（apple + sharpness 0） | 3 | 38 | 0 | 2 | 0 | — |
| ci_apple_v9_l0（apple lens OFF） | 3 | 122 | 6 | 35 | 0 | — |
| ci_apple_v8_l1 | 7 | 21 | 2 | 4 | 1 | 6 |
| ci_v8_l1（neutral v8） | 2 | 14 | 0 | 0 | 1 | 1 |

カット別（preview 2400px、`ci_v9_l1`）: 123〜34,353 点/枚（ISO 6400 の 4 カットで 5,746〜34,353）。同じカットの `ci_apple_v9_l1` は 0〜8。

### 12.3 所見

1. **白点は decoder 9 で「輝度 NR 0 かつ sharpness 0」のときに、scaleFactor < 1 の preview でだけ出る。** 原寸（full）では
   同じ設定でも 0。sharpness を 0.1 にするか輝度 NR を 0.24 にするかのどちらか一方で消える（最大 56 → 実質 0）。
   `apple_default` で sharpness だけ 0 にしても出ない（輝度 NR が残るため）。
2. **lens ON だけの現象ではない。** lens OFF でも出るが、出る長辺が違う（lens ON は 1600〜4000 で連続、lens OFF は 1600 と 4000 で
   突出）。レンズ補正のリサンプルで実効の縮小比が変わるため、特定の縮小比で起きるデコーダ内の縮小処理の問題と見える【推測】。
   §9.6 の「lens ON で出る」という観察は 2400px に限った話だった。
3. **decoder 8 では出ない**（neutral v8 / apple v8 とも全長辺で ≤ 21）。
4. **`apple_default` で NR だけ 0 にしても白点は増えない**（表の値は 2 カット中央値で、長辺 1600px の 41 は `apple_default` 既定の
   34 と同程度。カット別の最大は 1600px の DSC07415 で既定 62 / NR 0 で 71 と、同条件の neutral v9（1539）の 1/25 以下）。したがって設計書 §4.3 の懸念「NR 0 にすると白点が出るなら星景で NR を切る案は使えない」は当たらず、**NR 0 は
   sharpness > 0 である限り安全**。
5. `src-tauri/src/coreimage.rs` の現在の既定（`PROFILE="neutral"`、decoder 9、preview 2400）は、まさに白点が出る組み合わせである。
   #72 で `apple_default` に切り替えるまでは、試験経路のプレビューに白点が出る。

## 13. 逆光・ハイライトの評価（Issue #71、設計書 §4.2）

### 13.1 目的

ADR 0002「ハイライト（逆光）の白飛び・偽色が rawpy 現行より悪くない（`apple_default` では未評価）」を実データで確かめ、
`highlight_recovery` の置き換えレバー（#81）の候補＝`extendedDynamicRangeAmount` / `boostAmount` / `boostShadowAmount` / `exposure`
を #79 の個別上書きで掃引して、白飛び率・黒つぶれ率・輝度平均の表を残す。後段 `CIHighlightShadowAdjust.highlightAmount` は
CLI に入れていないため**未評価**（#72 / #73 の後段 CIFilter 連鎖と一緒に扱う）。

### 13.2 手順（再現コマンド）

```sh
.venv/bin/python scripts/compare_engines.py --source-root /Volumes/Backup/100MSDCF \
  --captures DSC04473 DSC04607 DSC04532 DSC04705 DSC04579 DSC04604 DSC04754 DSC04432 DSC04746 DSC04462 \
  --highlight --full --full-ids DSC04473 DSC04607 DSC04705 --out work/engine-comparison/highlight1
```

- 実行: 2026-10-20、§1 と同じマシン。10 カット × preview（2400px）+ 3 カット × full。所要約 7 分、エラー 0。
  出力は `work/engine-comparison/highlight1/`（`highlight_metrics.csv` / `highlight_summary.json` / `highlight_tables.md` /
  `sheets/<ID>_<mode>_clip_g{1..4}.jpg`（カメラ JPEG のクリップ境界が最も多い 200px 箱を 2 倍、4 バリアントずつ）/ `<ID>_<mode>_overview.jpg`）。
- **サンプルの選定**: §2 の逆光 2 カット（DSC04473 / DSC04607）に、選定用特徴量キャッシュ（`work/engine-comparison/features.json`、
  カメラ JPEG 1/8 縮小の輝度 >0.95 の割合 `bright_frac`）の上位から、同じ分の連写を避けて 8 カットを足した。
  いずれも Sigma 10-18mm か 30mm で、位置情報は使っていない。

  | ID | ISO | 選定理由（bright_frac / 暗部） | 内容（目視） |
  |---|---|---|---|
  | DSC04473 | 200 | §2 の逆光（白 11.4% / 暗部 12.2%） | 曇天の空抜けの木立・遊歩道 |
  | DSC04607 | 100 | §2 の逆光（白 10.4% / 暗部 11.3%） | 明るい曇り空と雲 |
  | DSC04532 | 100 | §2 の逆光・薄明（白 10.4% / 暗部 32.4%、0.4s） | 夜明けの空（橙〜桃色の雲） |
  | DSC04705 | 100 | bright_frac 最大 40.0% | 青空と白い雲（昼） |
  | DSC04579 | 100 | bright_frac 27.0%（6 時台） | 朝の明るい空 |
  | DSC04604 | 100 | bright_frac 19.7%（7 時台） | 朝の空 |
  | DSC04754 | 100 | bright_frac 16.7%、10mm | 曇天の空を含む風景 |
  | DSC04432 | 2000 | `backlit`（白 15.7% / 暗部 15.8%）、室内 | 窓からの強い光 |
  | DSC04746 | 100 | `backlit`（白 11.0% / 暗部 10.7%） | 空抜けの前景 |
  | DSC04462 | 400 | bright_frac 10.8%、30mm | 明るい屋外 |

### 13.3 バリアント

camera / rawpy（現行、既定プリセット `neutral_recovery` = `highlight_recovery` 60 → rawpy `HighlightMode.Blend`）/ `ci_v9_l1`（neutral）/
`ci_apple_v9_l1`（基本路線）と、`apple_default` v9 lens ON に #79 の上書きを 1 つずつ加えたもの: `extended_dynamic_range` 0.5 / 1.0 / 2.0、
`boost_amount` 0.6 / 0.8、`boost_shadow_amount` 0.5 / 1.3、`exposure_ev` −0.7 / −1.0 / −1.5、`boost_amount` 0.6 + `exposure_ev` −0.7。
`applied` で確認した `apple_default` の既定（ISO 100〜2000）は EDR 0・boost 1.0・boostShadow 0.9。

### 13.4 指標の定義

| 指標 | 定義 |
|---|---|
| clip_any / clip_r / clip_g / clip_b / clip_all | いずれか / 各チャンネル / 全チャンネルが 255 の画素率（`channel_clip_rates`） |
| magenta_rate | クリップ領域（いずれかのチャンネル 255）の外側 3px の帯で、R > G かつ B > G かつ min(R,B) − G ≥ 0.06 の画素率（`clip_boundary_magenta_rate`）。帯が無ければ NaN |
| clip_low / mean_luma / p99_luma / camera_luma_diff / camera_delta_e76 | §3 と同じ |

### 13.5 集計（preview 2400px、10 カット中央値。括弧は最小..最大）

| variant | clip_any | clip_b | clip_r | clip_all | magenta_rate | clip_low | camera_luma_diff | ΔE76 |
|---|---|---|---|---|---|---|---|---|
| camera | 3.0% (0.02..28.2) | 2.2% | 2.4% | 1.3% | 0 | 0 | — | — |
| rawpy（現行、highlight_recovery 60） | **1.8% (1.2..25.1)** | 1.6% | 1.5% | 1.2% | 0 (0..0.070) | 1.0% | **−0.128 (−0.248..+0.015)** | 14.2 |
| ci_v9_l1（neutral） | 13.4% | 11.5% | 7.1% | 3.0% | 0 | 0.1% | +0.013 | 5.9 |
| **ci_apple_v9_l1** | **13.0% (7.1..38.4)** | 11.0% | 6.3% | 1.7% | 0 (0..0.004) | 0.4% | **+0.008 (−0.066..+0.023)** | 5.7 |
| ci_apple_edr05 / edr10 / edr20 | 16.5% / 18.0% / 20.5% | 13.9 / 15.0 / 17.1% | 14.3 / 15.6 / 18.1% | 11.6 / 12.7 / 14.9% | 0 | 0.4% | +0.010 / +0.012 / +0.015 | 5.9 / 6.1 / 6.6 |
| ci_apple_boost06 / boost08 | 11.4% / 12.0% | 9.6 / 10.1% | 3.7 / 4.7% | 0.8 / 1.0% | 0 | 0.4% | −0.016 / −0.004 | 5.5 / 5.4 |
| ci_apple_bshadow05 / bshadow13 | 13.0% / 13.0% | 11.0% | 6.3% | 1.7% | 0 | 1.0 / 0.4% | 0.000 / +0.009 | 6.0 / 5.7 |
| ci_apple_ev-07 | 3.6% (0..18.7) | 0.8% | 2.6% | 0.4% | 0 | 1.0% | −0.067 (−0.131..−0.053) | 7.8 |
| ci_apple_ev-10 | 2.2% (0..10.5) | 0.4% | 1.8% | 0.2% | 0 | 1.4% | −0.098 | 11.0 |
| ci_apple_ev-15 | 0.05% (0..6.1) | 0.05% | 0.05% | 0.04% | 0 | 2.2% | −0.150 | 16.5 |
| ci_apple_boost06_ev-07 | 2.7% (0..15.9) | 0.6% | 2.0% | 0.2% | 0 | 0.8% | −0.090 | 10.1 |

カット単位（preview）で白飛び率が rawpy 以下 / カメラ JPEG 以下だったカット数: `ci_apple_v9_l1` **2 / 0**（10 中）、ev −0.7 で 8 / 8、
ev −1.0 で 8 / 9、ev −1.5 で 10 / 10、boost 0.6 + ev −0.7 で 8 / 9。

full（3 カット中央値）: camera clip_any 1.8%、rawpy 1.6%（luma −0.111）、`ci_apple_v9_l1` 9.6%（+0.002）、ev −0.7 0.1%（−0.069）、
ev −1.0 0.04%、boost 0.6 + ev −0.7 0.04%（−0.088）、EDR 1.0 15.0%。magenta_rate は full で rawpy 0.0001、`apple_default` 0.003、
ev −1.0 0.002（いずれも帯の 1% 未満）。

### 13.6 目視所見（2 倍拡大クロップ、EXIF なし）

見たシート: `highlight1/sheets/` の DSC04473_preview_{overview,clip_g1}、DSC04473_full_clip_g4、DSC04607_preview_{clip_g1,clip_g3}、
DSC04532_preview_clip_g1、DSC04705_preview_{clip_g1,clip_g4}、DSC04579_preview_clip_g2、DSC04432_preview_clip_g1（9 枚。ブラインドではない）。

- **rawpy 現行は全体が暗い**（カメラ比 −0.13、最大 −0.25）。`neutral_recovery` の `highlight_recovery` 60（Blend）で空の階調は残る
  （DSC04705 の青空と雲、DSC04607 の雲の筋）が、画面全体の明るさを下げて得ているもの。
- **`apple_default` はカメラ JPEG と同じ明るさだが、明るい空の青チャンネルが先に飽和する**（clip_b がカメラの 5 倍）。DSC04607 /
  DSC04579 の空は CI ではほぼ白一色になり、カメラ JPEG に残る薄い階調（雲の濃淡）が無い。DSC04473 の空抜けも CI は白く抜ける
  （カメラは薄い灰色が残る）。
- **偽色（マゼンタ）**: rawpy は DSC04532（夜明けの空）で地平線付近の白飛び境界に黄〜マゼンタの縁（magenta_rate 0.070）、DSC04432
  （室内の窓）で白飛び部に黄〜シアンの虹状の帯が出る。**`apple_default` ではどちらも出ない**（0.000〜0.004）。DSC04532 の桃色の雲の
  階調は CI がカメラ JPEG に最も近い。
- **EDR（0.5〜2.0）は白飛びを増やす**（全チャンネル同時のクリップが 1.7% → 12〜15%）。SDR 出力では EDR はハイライトを明るく押し上げる
  方向に働き、保持ではない。boost 0.6 / 0.8 と boostShadow は白飛び率をほとんど動かさない（boost 0.6 で 13.0 → 11.4%）。
- **`exposure` を下げると白飛びはカメラ JPEG 並み以下になる**（−0.7EV で 3.6%、−1.0EV で 2.2%）。DSC04705 の青空は −1.0EV で雲の形、
  −1.5EV で rawpy 並みの階調が戻る。ただし画面全体が暗くなる（−0.7EV でカメラ比 −0.067、−1.5EV で −0.150 ≒ rawpy 現行と同じ暗さ）。
  ハイライトだけを落とすレバーは今回の CLI の範囲には無い。

### 13.7 結論（#71 の判断材料）

1. **白飛び率は、既定のままの `apple_default` が rawpy 現行より明確に多い**（中央値 13.0% vs 1.8%、10 カット中 8 で多い）。
   ただし rawpy 現行は `highlight_recovery` 60 と auto_bright の組み合わせで画面全体がカメラ比 −0.13 暗く、「同じ明るさでの比較」ではない。
   `apple_default` はカメラ JPEG と同じ明るさ（+0.008）で、カメラ JPEG（3.0%）よりも白飛びが多い。差の主体は明るい空の青チャンネル。
2. **偽色は `apple_default` の方が少ない**（rawpy の白飛び境界のマゼンタ・虹状の帯は CI には出ない）。ADR 0002 の判断材料表の
   「境界にマゼンタの偽色が出るカットあり」は CI では解消する。
3. **`highlight_recovery` の置き換えレバーとして使えるのは `exposure`（負方向）だけ**。EDR は逆効果、boost / boostShadow は効かない。
   `exposure` −0.7〜−1.0EV で白飛び率はカメラ JPEG・rawpy 現行以下になるが、全体が暗くなるので、`shadow_lift` / `brightness`
   （#73 の後段）で中間調を戻す組み合わせが要る。後段の `CIHighlightShadowAdjust.highlightAmount` は未評価で、これが入れば
   「明るさを保ったままハイライトだけ落とす」レバーになりうる【推測】。

## 14. ADR 0002 の移行条件への判定案と後続 Issue への示唆

| 条件 | 判定案 | 根拠 |
|---|---|---|
| 夜景・星空・長秒で破綻が無い。`apple_default` の NR が星を消さない | **満たしている（このサンプルの範囲で）** | §10.7: 星の残存率 ≥ 0.991（rawpy 以上）、空の輝度差 ±0.016、空ノイズ・色ノイズは rawpy 以下、塗りつぶし・白点なし（目視・数値）。限界は §15 |
| ハイライト（逆光）の白飛び・偽色が rawpy 現行より悪くない | **条件付き**: 偽色は rawpy より少ない（満たす）。白飛び率は既定の `apple_default` では rawpy 現行より多く（13.0% vs 1.8%、カメラ JPEG 3.0%）、`exposure` を −0.7〜−1.0EV 下げたときだけ rawpy 以下になる。「明るさを保ったまま白飛びを減らす」レバーは未評価（`CIHighlightShadowAdjust`） | §13.7 |

後続 Issue への示唆:

- **#81（`highlight_recovery` の置き換え）**: §13 の掃引で、**EDR は白飛びを増やし（逆効果）、boost / boostShadow はほぼ効かず、
  効くのは `exposure` の負方向だけ**。救済系プリセット（`highlight_rescue` 90 / `backlit_rescue` 80 / `sunset_glow` 70）は
  設計書 §3.1 の案 (iii)「`exposure` を下げ `shadow_lift` を上げる」構成が現実的で、案 (i)（EDR / boost の再定義）は採れない。
  案 (ii) の `CIHighlightShadowAdjust.highlightAmount` は未評価のまま（#72 / #73 で後段が CLI に入ったときに `--highlight` に足す）。
  rawpy 現行の白飛びの少なさは画面全体が −0.13 暗いことで得ているので、置き換え後のプリセット値は「同じ明るさで比べる」こと。
- **#83（ブラケット合成）**: §11 のとおり露出差の保存とレンズ補正の幾何一致（カメラ内補正と 1 px 以内）は満たす。合成品質は同一構図の
  サンプルが無く未確認。CI sRGB tiff16 → OpenCV Mertens の足場（B3）は `bracket_pair_eval` として動いており、原寸対応と
  実ブラケットでの確認が残る。
- **#88（暗所判定の扱い）**: このサンプルでは N1（廃止）で問題が見えない（空の輝度差はカメラ比 +0.013、ビネット補正は四隅のノイズを
  増やさない）。N2（星空なら NR 0）を採る場合、**decoder 9 では sharpness を 0 にしないこと**が条件（§12）。NR 0 の利点は数値上
  小さく（星の残存率は変わらず、空ノイズが 1.05 → 1.36 に増える）、目視でも差は僅かなので、既定の `apple_default` のままで足りる
  可能性が高い【推測: 微光星の多い暗い空での差は未確認】。
- **#72（プレビューの CI 化）**: Rust 側の `neutral` 既定は白点の出る組み合わせなので、`apple_default` への切り替えは必須。
- **#75（回帰確認）**: この PR で CLI を 0.2.0 にしたが既定リクエストは変えておらず、`scripts/coreimage-regression-applied.sh` は
  ベースライン 27.0.1 と差分 0 件だった（2026-10-20）。`--night` の指標（star_recall / sky_noise_ratio / impulse_vs_full）を
  回帰確認の星空側の項目に加えられる。

## 15. §10〜§13 の限界・未確認事項

- サンプルは 1 セッション・2 本のレンズ・ISO 3200/6400・明るめの空に偏る。暗い空（輝度 0.2 未満）は 4 カット、前景の無い純粋な
  星野は少なく、天の川の濃淡や微光星の多い暗所での「NR が微光星を消す」可能性はこのデータでは検出できない。
  10-18mm（周辺減光の大きいレンズ）での四隅の挙動は未確認。
- `point_count` / `star_count` は高 ISO の粒状ノイズも数えるため、星の絶対数としては読めない。星の残存率は「カメラ JPEG の強い星」を
  基準にしており、カメラ JPEG 自身の NR で消えた微光星は評価できない。
- `impulse_count` は lens OFF のバリアントで位置合わせの残差を含む（apple v9 l0 で impulse_count 747 に対し impulse_vs_full 6）。
  lens OFF の `star_recall`（0.985）も同じ理由で低めに出る。lens OFF は `impulse_vs_full` と `corner_*` だけで読むこと。
- 白点の scale 掃引は 2 カット、原因の切り分けは「どの設定で消えるか」までで、デコーダ内部の機構は分からない（非公開）。
  OS 更新で変わりうる（#75 の対象）。
- 露出差の保存は三脚ブラケットが無く、動いたペア 1 組と別構図 2 組での確認。合成品質は未確認。
- ハイライト（#71）は 10 カット（うち 8 は明るい空が主役。太陽そのもの・夜景の点光源・夕景の太陽は無い）。`CIHighlightShadowAdjust` は未評価。
  rawpy と `apple_default` の白飛び率は明るさが違う状態での比較で、同じ明るさに揃えた比較（CI の `exposure` −1.5EV が rawpy 現行と
  ほぼ同じ暗さになる）は exposure 行で代用している。
- 目視は §10 で 15 枚、§13 で 9 枚のシート（2 倍拡大パッチ）で、ブラインド比較ではない。
