# Core Image の小サイズレンダ（PSO の代理画像）の速度と画質（Issue #102）

PSO（#85）は 1 枚につき数十回、長辺 640px の代理画像を作る。#82 の計測では `--batch` でも 1 件 1.36〜1.47 秒かかっていた
（`native/coreimage-cli/README.md`「バッチモード」）。この文書は、その時間の内訳を段階ごとに分けて測り、速くする方法を
画質の差とあわせて比べた結果をまとめる。結論は §0、#85 への推奨は §5。

## 0. 結論

| 項目 | 結果 |
|---|---|
| #82 の 1.4 秒の正体 | **約 0.9 秒は SMB 上の ARW の読み込み**（`/Volumes/Backup` は smbfs）。同じファイルをローカルに置くかメモリから渡すと 1 件 **0.50〜0.52 秒**（§2.3） |
| 0.5 秒の内訳 | CIContext のレンダ（RAW デコード〜読み出し）**0.41 秒**、`CIRAWFilter(imageURL:)` 0.07 秒、設定 0.014 秒、エンコード 0.004 秒（§2.1） |
| `scaleFactor` | 0.5 以下では時間が変わらない（120px でも 0.50 秒）。小さくしても速くならない（§2.2） |
| draft mode | `scaleFactor` 1.0 のときだけ速くなる（1.34 → 0.55 秒）。640px では速さは同じで、画は少し柔らかくなる（ΔE 0.30、ラプラシアン分散 0.7〜0.85 倍）。**使わない**（§2.2・§3.2） |
| レンズ補正・NR/シャープ・後段カーネル | どれも ±10 ms 以内で、計測のばらつきに埋もれる（§2.1） |
| 埋め込みプレビュー | 取り出しは 17 ms（ImageIO）/ 82 ms（`CIRAWFilter.previewImage`）。ただし CI 既定と ΔE 4.4、彩度が 28〜45% 低く、**調整に反応しない**。PSO の代理には使えない（§2.4・§3.3） |
| 1 回デコード → 後段だけ繰り返す | 後段 1 回 **7.2 ms**（中央値。デコード込みの 0.49 秒の約 1/70）。本番 `develop()` との差は ΔE 0.02（16bit で最大 34/65535）（§2.5・§3.1） |
| exposure を後段で近似 | 線形ゲイン 2^EV は ±0.25 EV で ΔE 1.0、±1 EV で 2.9〜4.7（不十分）。**隣り合う格子点の 2 回のデコードの幾何平均**で中間（±0.25）が ΔE 0.15〜0.20、**チャンネル別 1D LUT**（他の画像で作成）で ±0.25 が 0.10、±1 が 0.19〜0.40（§3.5） |
| sharpness ≤ 1（デコード段） | **640px では効かない**（0.8 と 1.0 の出力が画素単位で一致）。1280px 以上では効く（§3.4） |
| noise_reduction（デコード段） | 640px では ΔE 0.2 以下。平坦部ノイズは ISO 3200 で off +13% / full −13%（§3.4） |
| #85 への推奨 | ARW を 1 回メモリに読み、(noise_reduction, exposure 格子点) ごとに 1 回だけデコードして RGBAh でキャッシュし、後段（shadow_lift / brightness / contrast / saturation / sharpness）と exposure の補間だけを毎回回す。1 枚 50 評価で **約 73 秒（SMB・現行案）→ 2〜6 秒**の見込み（§5） |

## 1. 方法

### 1.1 環境

| 項目 | 値 |
|---|---|
| マシン | Apple M3（8 コア）、メモリ 24 GB |
| OS | macOS 27.0.1（26A434） |
| ツールチェーン | Apple Swift 6.4（swiftlang-6.4.0.34.1）、Xcode SDK の CoreImage ヘッダ |
| CLI | 0.5.0（このブランチの計測フック入り）。`decoder_version "9"`、`apple_default`、レンズ補正 ON |
| 計測日 | 2026-10-21 |

### 1.2 サンプル

`/Volumes/Backup/100MSDCF/*.ARW` から ISO 帯・シーンが違う 7 枚を選んだ（過去の評価で使った DSC04430・DSC04553 は外した）。
計測ではローカルにコピーした `work/small-decode/arw/`（gitignore 済み）を読み、元のファイルは読むだけ（`cmp` でコピーの一致を確認）。
SMB からの読み込みの影響は §2.3 で別に測った。

| 撮影 ID | ISO | 露光 | EV100 | シーン |
|---|---|---|---|---|
| DSC04780 | 100 | 1/640 s | 14.6 | 日中の屋外（逆光気味、人物と馬） |
| DSC04547 | 100 | 1.6 s | 3.7 | 薄明の山道（三脚、長秒） |
| DSC04491 | 250 | 1/30 s | 7.9 | 夜明けの空（明るい雲と暗い前景） |
| DSC04683 | 1000 | 1/125 s | 6.6 | 日中の木陰（黒い犬） |
| DSC04624 | 640 | 1/125 s | 5.3 | 室内（猫） |
| DSC04627 | 1600 | 1/125 s | 3.9 | 室内・暗め（猫） |
| DSC04638 | 3200 | 1/125 s | 2.9 | 室内・暗い（猫） |

- このフォルダの ARW は ISO 3200 が上限で、**ISO 6400 は無い**。高 ISO の結論は ISO 3200 までのもの。屋外の夜景（星空）も含まない。
- 寸法はすべて 6000×4000、640px の出力は 640×427。

### 1.3 計測ハーネス

- `scripts/coreimage-small-decode/main.swift` … 計測本体。`build.sh` が `native/coreimage-cli/Sources/CoreImageDevelop` の全ソースと
  一緒に 1 モジュールとして `swiftc -O` でビルドする（`Package.swift` も本番のバイナリも変えない）。internal な `applyProfile` /
  `applyAdjustmentsDecoder` / `PostChain` をそのまま呼び、本番の `develop()` と同じ順でフィルタを組む。
- `scripts/coreimage-small-decode/analyze.py` … 集計（`timing` / `quality` / `exposure`）。ΔE は `scripts/compare_engines.py` の
  `delta_e76`（8bit sRGB にしてから OpenCV の Lab、D65）。
- 条件は `--batch` と同じ: `CIContext` はプロセスで 1 つ（`makeRenderContext()`: extended linear sRGB・RGBAh・中間キャッシュなし）、
  `CIRAWFilter` は毎回作る、1 回ごとに `clearCaches()` と `autoreleasepool`。
- 方式は 1 反復の中で交互に回した（熱やクロックの偏りが特定の方式に寄らないように）。timing はウォームアップ 2 回を捨てて 10 回 × 7 枚
  = 各方式 n=70、sweep と io はウォームアップ 1 回 + 5 回 × 7 枚 = n=35。表の値は全サンプルをまとめた中央値と p90（ms）。
- リクエストは #85 が使う形: `mode: preview`、`max_edge: 640`、`format: tiff16`、`output_color_space: srgb`、
  `settings: {profile: apple_default, lens_correction: true}`、`adjustments` は既定プリセット `neutral_recovery`
  （exposure 0、wb_temp 0、shadow_lift 15、brightness 1.0、contrast 1.03、saturation 1.02、sharpness 1.04、noise_reduction light）。

```sh
scripts/coreimage-small-decode/build.sh                     # → work/small-decode/bench
B=work/small-decode/bench; A=(work/small-decode/arw/*.ARW)
$B timing --out work/small-decode/res --warmup 2 --iters 10 $A
$B sweep  --out work/small-decode/res --warmup 1 --iters 5  $A
$B io     --out work/small-decode/res --local work/small-decode/arw --warmup 1 --iters 5 /Volumes/Backup/100MSDCF/<同じ 7 枚>.ARW
$B dump   --out work/small-decode/dump $A
python3 scripts/coreimage-small-decode/analyze.py timing   work/small-decode/res
python3 scripts/coreimage-small-decode/analyze.py quality  work/small-decode/dump
python3 scripts/coreimage-small-decode/analyze.py exposure work/small-decode/dump
```

### 1.4 段階の分け方と限界

Core Image は遅延評価で、`outputImage` を取った時点では画素を計算しない。RAW の展開・デモザイク・NR・シャープ・トーンカーブ・
レンズ補正・後段カーネルはすべて、最後の `CIContext.render` の中でまとめて走る。そこで:

- **時刻で分けられる段**: `CIRAWFilter(imageURL:)`（create）、decoderVersion〜adjustments の設定（configure）、`outputImage` と
  後段のグラフ組み立て（graph）、レンダ（render）、エンコードと書き込み（encode_write）。
- **時刻では分けられない段**（レンダの中）: 設定を 1 つだけ変えたフィルタを毎回作り直し、同じ寸法の RGBAh ビットマップへレンダして
  差を見る（レンズ補正 off/on、NR とシャープ 0、draft、後段あり）。差には GPU への投入と読み出しのばらつきも入るので、
  **10 ms 程度の差は区別できない**。
- デモザイクと RAW の展開を分ける API は無い。`scaleFactor` の掃引（§2.2）で「出力の画素数に比例する部分」と「固定費」に分けた。
- 計測フック `GEOFRAME_CI_TIMING=1`（このブランチで追加）: CLI が 1 件ごとに stderr へ
  `timing create=… configure=… graph=… render=… encode_write=… other=… total=…`（ms）を書く。stdout の応答 JSON・出力画像は
  フックの有無でバイト単位で変わらない（tiff16 で `cmp` 一致、応答は `elapsed_ms`・`output` 以外一致を確認）。

## 2. 速度

### 2.1 現行経路（640px）の段階別

| 方式 | total 中央値 | total p90 | create | configure | graph | render |
|---|---|---|---|---|---|---|
| `develop()`（#85 のリクエスト。tiff16 sRGB + adjustments） | **493.8** | 537.8 | （`elapsed_ms.decode` 78.5） | | | （`render_encode` 415.1） |
| `develop()`（#82 と同じ。jpeg・adjustments なし） | 519.2 | 568.7 | （85.7） | | | （432.3） |
| デコード（レンズ補正 off → RGBAh） | 511.1 | 600.8 | 73.6 | 14.2 | 9.7 | 420.4 |
| デコード（レンズ補正 on） | 498.9 | 555.3 | 72.5 | 14.2 | 0.7 | 412.2 |
| デコード（レンズ補正 on、NR 0・シャープ 0.1） | 511.6 | 564.2 | 73.3 | 14.3 | 9.8 | 413.6 |
| デコード（レンズ補正 on、draft） | 509.0 | 560.3 | 72.0 | 14.2 | 0.7 | 419.8 |
| デコード（レンズ補正 on）+ 後段カーネル | 505.6 | 580.7 | 74.2 | 14.2 | 0.8 | 417.2 |
| エンコード + 書き込み（tiff16、640×427） | 3.9 | 5.3 | | | | |
| エンコード + 書き込み（jpeg） | 3.1 | 4.5 | | | | |
| ARW 全体の読み込み（ローカル、`Data(contentsOf:)`） | 7.6 | 8.3 | | | | |

- 時間のほぼすべてはレンダ（約 0.41 秒）と `CIRAWFilter` の生成（約 0.07 秒）。エンコードは 4 ms で効かない。
- レンズ補正・NR とシャープ・draft・後段カーネルの有無は、差が計測のばらつき（p90 − 中央値 ≈ 50〜90 ms）より小さい。
  graph が 9.7 ms の行（レンズ補正 off、NR 0・シャープ 0）は `outputImage` の取得に約 9 ms 余計にかかっているが、原因は追っていない
  （total への影響はばらつきの範囲）。
  「NR 0・シャープ 0」は adjustments 指定時の白点ガードでシャープが 0.1 に床上げされる（計測にはほぼ影響しない）。
- サンプル間の差は小さく、ISO 3200 の DSC04638 だけが約 80 ms 遅い（`develop()` 536 ms、他は 467〜513 ms）。
- 1 件あたりの CPU 時間: 同じ 640px を `--batch` で 10 件流すと wall 5.30 秒、user 2.47 秒 + sys 0.83 秒（1 件 0.33 CPU 秒）。

### 2.2 `scaleFactor` と draft の掃引（レンズ補正 on、後段なし、RGBAh レンダ）

| scaleFactor（出力） | total 中央値 | p90 | render 中央値 |
|---|---|---|---|
| 1.0（6000×4000） | 1341.8 | 1461.1 | 1256.0 |
| 1.0 + draft（6000×4000） | 548.0 | 585.0 | 457.0 |
| 0.5（3000×2000） | 531.4 | 572.4 | 445.1 |
| 0.5 + draft | 529.2 | 593.8 | 446.1 |
| 0.25（1500×1000） | 513.0 | 588.4 | 422.7 |
| 0.107（640×427、本番の 640px） | 508.3 | 580.9 | 418.8 |
| 0.107 + draft | 507.5 | 588.5 | 422.0 |
| 0.05（300×200） | 511.0 | 600.5 | 422.9 |
| 0.02（120×80） | 502.7 | 577.6 | 419.2 |

- **0.5 以下では出力の大きさによらず約 0.5 秒**。この領域の時間はほぼ固定費（ファイルからの RAW の展開と、縮小前の段の処理）で、
  `scaleFactor` をさらに小さくしても速くならない。
- draft mode（`isDraftModeEnabled`、SDK ヘッダの説明は "can improve image decoding speed with minimal loss of quality"）は
  1.0 のときだけ効き、0.5 以下の時間と同じになる。0.5 以下ではもともと draft 相当の経路を通っていると読める【推測】。
  640px で draft を立てても速さは変わらず、画だけが変わる（§3.2）。
- `scaleFactor` は指定どおりの寸法を返す（0.107 → 640×427。丸めのずれは無い）。
- 調べた SDK の API（`xcrun --show-sdk-path` 配下の `CIRAWFilter.h`）: `draftModeEnabled`・`scaleFactor` のほかに低解像度向けの
  設定は無い。`previewImage`（§2.4）と `filterWithImageData:identifierHint:`（§2.3）を使った。

### 2.3 ARW の置き場所と読み方

同じ 7 枚を、SMB 上のパス・ローカルのコピー・メモリ上の `Data`（`CIRAWFilter(imageData:identifierHint: "com.sony.arw-raw-image")`）
から同じ条件でデコードした（レンズ補正 on、RGBAh レンダ）。

| 読み方 | total 中央値 | p90 | create | render |
|---|---|---|---|---|
| SMB 上のパス（`CIRAWFilter(imageURL:)`） | **1454.7** | 1533.3 | 158.0 | 1285.8 |
| ローカルのパス | 529.7 | 602.0 | 75.3 | 439.5 |
| メモリ上の `Data` | **521.4** | 577.0 | 78.7 | 427.0 |
| （参考）ARW 全体の読み込み: ローカル | 8.9 | 10.0 | | |
| （参考）ARW 全体の読み込み: SMB（各ファイル 1 回） | 527.4 | 561.5 | | |

- **#82 の 1.36〜1.47 秒は、SMB 上の ARW を毎回パスから読んでいたことが原因の大半**。CLI に同じファイルを 6 回続けて渡しても
  毎回 1.3 秒前後かかり（`GEOFRAME_CI_TIMING` で render 1.1〜1.3 秒）、ローカルのコピーでは 2 件目以降 0.47〜0.52 秒だった。
  `CIRAWFilter` の読み込みは SMB のクライアントキャッシュがほとんど効かない読み方をしている【推測】。
- メモリから渡した場合の出力は、パスから読んだ場合と**画素単位で一致**した（7 枚とも RGBAf の最大差 0）。`filter.properties` の
  ISO（USM のコアリングに使う）・レンズ名・`orientation`・`nativeSize`・撮影時 WB の読み戻しも一致した（7 枚 + 縦位置の
  DSC04553〈orientation 8〉で確認）。
- ARW を 1 回だけ読む（SMB で約 0.53 秒）→ 以後はメモリから作る、で 1 件 1.45 秒 → 0.52 秒（約 2.8 倍）になる。画は変わらない。

### 2.4 埋め込みプレビュー

| 方式 | total 中央値 | p90 | 出力 |
|---|---|---|---|
| ImageIO（`CGImageSourceCreateThumbnailAtIndex`、`ThumbnailMaxPixelSize 640`・`WithTransform`・`FromImageIfAbsent`） | **17.4** | 21.0 | 640×428 |
| `CIRAWFilter.previewImage`（1616×1080）→ Lanczos で 640 へ → RGBAh レンダ | 82.0 | 84.6 | 640×428 |

- α6400 の ARW の `CGImageSource` は画像 1 つ（6000×4000）として見え、サムネイル API は埋め込みの 1616×1080 プレビュー
  （`previewImage` と同じもの）から作る。`previewImage` は取り出すだけで約 76 ms かかった。
- 速さは十分だが、画が CI の現像と違い、調整にも反応しない（§3.3）。

### 2.5 1 回デコード → 後段だけ繰り返す

キャッシュは「デコード段まで（`outputImage`。レンズ補正込み、後段なし）を extended linear sRGB のビットマップへ 1 回レンダしたもの」。
2 回目以降は `CIImage(bitmapData:…)` に戻して後段チェーン（`PostChain.apply`、本番と同じ Metal カーネル）を掛け、
`renderRGB16`（本番の tiff16 と同じ読み出し）で 640×427 の 16bit sRGB の画素まで取る。後段のパラメータは反復ごとに 4 通りを回した。

| 方式 | total 中央値 | p90 |
|---|---|---|
| キャッシュ作成（デコード 1 回 + RGBAh への読み出し） | 479.7 | 552.5 |
| **後段のみ（RGBAh キャッシュ → 後段 → RGB16 sRGB）** | **7.2** | 8.9 |
| 後段のみ（RGBAf キャッシュ） | 11.3 | 12.8 |
| 後段のみ + exposure の線形ゲイン + tiff16 の書き出し | 8.4 | 9.7 |
| （比較）`develop()` 1 件 | 493.8 | 537.8 |

- 後段だけなら 1 回 7〜8 ms で、デコード込みの約 1/70（tiff16 の書き出し込みで約 1/60）。
- RGBAh（半精度）のキャッシュは 640×427 で約 2.2 MB。RGBAf は倍の大きさで遅く、本番との一致も RGBAh の方が良い（§3.1）。

## 3. 画質

ΔE は「平均 ΔE76 / 画素の p95 / 真値の輝度上位 10% の画素の平均」。デコード段の比較（§3.2〜3.6）は後段なしの
デコード出力（extended linear sRGB → [0,1] にクリップ → sRGB 8bit）どうしで比べた。

### 3.1 キャッシュ経路と本番 `develop()` の一致

| 比較（tiff16 sRGB → 8bit） | 平均 ΔE の中央値（7 枚） | 平均 ΔE の最大 | 16bit の最大差 |
|---|---|---|---|
| RGBAh キャッシュ + 後段（neutral_recovery） vs `develop()` | **0.02** | 0.03 | 33〜35 / 65535 |
| RGBAf キャッシュ + 後段（neutral_recovery） vs `develop()` | 0.04 | 0.06 | 65〜66 |
| RGBAh キャッシュ + 後段（強め: lift 25・brightness 1.2・contrast 1.25・saturation 1.3・sharpness 1.45） vs `develop()` | 0.03 | 0.05 | 67〜81 |
| RGBAf キャッシュ + 後段（強め） vs `develop()` | 0.06 | 0.08 | 74〜86 |

- 差は半精度の丸め（8bit で最大 0.13〜0.3 階調）だけで、見た目の差は無い。`develop()` 自体が RGBAh の作業形式なので、
  RGBAh のキャッシュの方が本番に近い。

### 3.2 draft mode（640px）

| 比較 | 平均 ΔE の中央値（範囲） | ラプラシアン分散の比 | 平坦部ノイズの比 |
|---|---|---|---|
| draft vs 通常 | 0.30（0.19〜0.78） | 0.69〜0.85 | 0.88〜0.94 |

- 640px では速さが変わらない（§2.2）のに、画が柔らかくなる。使う理由が無い。

### 3.3 埋め込みプレビュー vs CI 既定（`apple_default`・レンズ補正 on・後段なし）

CI の 640×427 に合わせて縮小して比べた（プレビューは 640×428）。

| 撮影 ID | ISO | ΔE（ImageIO） | 平均輝度 プレビュー / CI | CI の exposure −1 / +1 | 彩度 プレビュー / CI | 白飛び画素 プレビュー / CI |
|---|---|---|---|---|---|---|
| DSC04780 | 100 | 5.00 | 0.161 / 0.173 | 0.105 / 0.268 | 0.051 / 0.083 | 0 / 0 |
| DSC04547 | 100 | 6.11 | 0.217 / 0.232 | 0.145 / 0.356 | 0.082 / 0.122 | 0.33% / 1.39% |
| DSC04491 | 250 | 3.69 | 0.104 / 0.104 | 0.064 / 0.162 | 0.061 / 0.085 | 0.04% / 0.77% |
| DSC04683 | 1000 | 5.66 | 0.352 / 0.364 | 0.238 / 0.522 | 0.064 / 0.102 | 0.10% / 0.78% |
| DSC04624 | 640 | 4.43 | 0.399 / 0.419 | 0.271 / 0.594 | 0.076 / 0.108 | 0 / 0 |
| DSC04627 | 1600 | 2.84 | 0.305 / 0.318 | 0.204 / 0.457 | 0.022 / 0.040 | 0 / 0 |
| DSC04638 | 3200 | 4.21 | 0.286 / 0.304 | 0.192 / 0.450 | 0.047 / 0.079 | 0 / 0 |

- 中央値 ΔE 4.4（`previewImage` 経由も 4.4 でほぼ同じ）。明るさはほぼ同じだが、**彩度が 28〜45% 低く**、白飛びが少ない
  （カメラ側のトーン・DRO で処理済み）。シャープ感（ラプラシアン分散の比）は 0.41〜1.39 と画ごとにばらつく。
- 幾何はカメラ側でレンズ補正済み（CI のレンズ補正 off と比べると ΔE が大きくなる: 例 DSC04683 で 5.66 → 12.09）。
- **プレビューはカメラが一度だけ作った画なので、exposure・wb_temp・NR・sharpness のどれにも反応しない**。
  CI では exposure −1 / +1 で平均輝度が約 −35〜−40% / +40〜+55% 動くのに対し、プレビューは固定。PSO の代理に使うと、デコード段の探索次元を
  すべて失う。シーン判定（星空判定・スタイル候補の選別など、調整に依存しない判定）の入力に限れば候補になる。

### 3.4 デコード段の sharpness ≤ 1 と noise_reduction（640px）

| 比較（基準は neutral_recovery のデコード出力） | 平均 ΔE の中央値（最大） | ラプラシアン分散の比 | 平坦部ノイズの比 |
|---|---|---|---|
| sharpness 0.8（`sharpnessAmount` 1.0 → 0.8） | **0.00（0.00）** | 1.000 | 1.000 |
| sharpness 0.9 | 0.00（0.00） | 1.000 | 1.000 |
| noise_reduction off | 0.02（0.19） | 1.00〜1.18（ISO 3200 で 1.18） | 1.00〜1.13（ISO 3200 で 1.13） |
| noise_reduction full | 0.10（0.20） | 0.84〜0.99 | 0.87〜1.00（ISO 3200 で 0.87） |

- **`sharpnessAmount` は `scaleFactor` 0.107（640px）では効かない**。filter から読み戻すと 0.8 になっているのに、出力は画素単位で一致した。
  同じ画（DSC04780）を `max_edge` 1280 / 2400 / 3200 で比べると、1.0 と 0.8 で 16bit の平均差 58 / 112 / 110 と効いている。
  したがって 640px の代理では sharpness < 1 の違いが見えない（本番の 2400px では見える）。これは今の CLI 経路でも同じで、
  キャッシュ方式に固有の問題ではない。
- noise_reduction は 640px ではほとんど画を変えない（ΔE 0.2 以下）。高 ISO の平坦部ノイズの指標にだけ ±13% 程度出る。

### 3.5 exposure を後段で近似する

CIRAWFilter の `exposure` はトーンカーブの前（RAW の線形空間）に掛かるので、出力（トーンカーブ後）に 2^EV を掛けても一致しない。
代わりの方法を 3 つ測った（7 枚、後段なしのデコード出力どうし）。

| 近似（元 → 目標） | 平均 ΔE の中央値 | 平均 ΔE の最大 | 輝度上位10% の中央値 | 輝度上位10% の最大 |
|---|---|---|---|---|
| EV0 → +0.25: 何もしない | 3.51 | 4.50 | 4.60 | 5.11 |
| EV0 → +0.25: 線形ゲイン 2^0.25 | 1.11 | 1.38 | 1.47 | 2.87 |
| EV0 → +0.25: チャンネル別 1D LUT | **0.10** | 0.16 | 0.20 | 0.65 |
| EV0 → −0.25: 線形ゲイン | 1.00 | 1.26 | 1.20 | 2.66 |
| EV0 → −0.25: LUT | **0.08** | 0.14 | 0.17 | 0.63 |
| EV0 → +0.5: 線形ゲイン | 2.19 | 2.74 | 3.19 | 5.59 |
| EV0 → +0.5: LUT | 0.19 | 0.30 | 0.37 | 1.25 |
| EV0 → −0.5: 線形ゲイン | 1.71 | 2.22 | 2.23 | 4.98 |
| EV0 → −0.5: LUT | 0.15 | 0.25 | 0.28 | 1.22 |
| EV0 → +1: 線形ゲイン | 4.67 | 5.64 | 7.34 | 9.06 |
| EV0 → +1: LUT | 0.40 | 0.54 | 0.68 | 2.33 |
| EV0 → −1: 線形ゲイン | 2.85 | 3.72 | 4.36 | 8.51 |
| EV0 → −1: LUT | 0.19 | 0.40 | 0.47 | 2.23 |
| EV0 → +2: 線形ゲイン | 9.08 | 10.53 | 4.74 | 15.87 |
| EV0 → +2: LUT | 0.85 | 1.26 | 0.91 | 3.36 |
| EV−1 → EV0: LUT（低い側から上げる） | 0.24 | 0.42 | 0.46 | 1.95 |
| EV0 と EV+0.5 の 2 回のデコードから +0.25: 算術平均 | 0.52 | 0.57 | 0.37 | 0.72 |
| EV0 と EV+0.5 から +0.25: **幾何平均** | **0.20** | 0.33 | 0.48 | 0.49 |
| EV−0.5 と EV0 から −0.25: 幾何平均 | **0.15** | 0.25 | 0.49 | 0.50 |

- **線形ゲインは粗い**: ±0.25 EV でも平均 ΔE 1.0〜1.1（明部で最大 2.9）、±1 EV で 2.9〜4.7。
- **チャンネル別 1D LUT**: 出力の linear sRGB の各チャンネルについて「EV0 の値 → 目標 EV の値」をビンごとの中央値で表にしたもの。
  表は**評価する画像を除いた他の 6 枚**で作った（leave-one-out）ので、画像に依らない表で近似できることを示す。±0.25 で ΔE 0.1、
  ±1 でも 0.2〜0.4 と、デコードし直したものとほぼ見分けがつかない。弱点は明部で、もとの EV0 で飽和・ハイライト処理された
  画素は戻せない（輝度上位 10% の最大 2.2〜3.4。明るい空を含む DSC04491・DSC04683）。低い EV からのデコードを上げても明部は
  改善しなかった（EV−1 → 0 で上位 10% 最大 1.95）。表は decoder 9・macOS 27.0.1・`apple_default` の条件のもので、
  OS や decoder の版が変わったら作り直しが要る【推測】。
- **隣り合う格子点の幾何平均**: exposure を 0.5 EV 刻みでデコードしておけば、その間（最悪の真ん中 ±0.25）は 2 枚の幾何平均で
  ΔE 0.15〜0.20（明部の最大 0.5）。表を持たなくてよい。
- 後段（neutral_recovery）まで通しても線形ゲインの ΔE はほぼ同じ（±0.25 で 1.02〜1.12、±1 で 3.06〜4.59）。後段で差は縮まない。

### 3.6 wb_temp を後段で近似する（参考。PSO の探索次元ではない）

| 比較 | 平均 ΔE の中央値（範囲） |
|---|---|
| wb_temp ±20: 何もしない | 4.44〜4.50（1.99〜4.91） |
| wb_temp ±20: チャンネル別ゲイン（真値に最小二乗で合わせた、楽観的な下限） | 1.30〜1.32（0.86〜2.01） |
| wb_temp ±50: 何もしない | 10.73〜11.51（4.88〜12.71） |
| wb_temp ±50: チャンネル別ゲイン（同上） | 3.05〜3.40（1.98〜4.98） |

- 真値に合わせたゲイン（実際には使えない、楽観的な値）でも ±20 で ΔE 1.3、±50 で 3 残る。WB はデコード段に残すべき。
  当てはめたゲインは画像間で近い（+20 で R ×1.05〜1.12、B ×0.89〜0.93）が、ゲインという形自体の限界で ΔE が下がらない。

## 4. Adjustments の分類と PSO の探索次元

`Develop.swift: applyAdjustmentsDecoder`・`PostChain`・設計書 `docs/design/coreimage-preview-adjustments.md` §3.3 と、
`local_raw/development_candidates.py` の `SEARCH_KEYS` を突き合わせた。

| Adjustments | 段 | CI での写像 | PSO（#85）での扱い | デコード 1 回で済むか |
|---|---|---|---|---|
| `shadow_lift` | 後段 | `geoframe_tone` | 探索する | **済む** |
| `brightness` | 後段 | `geoframe_tone` | 探索する | **済む** |
| `contrast` | 後段 | `geoframe_tone` | 探索する | **済む** |
| `saturation` | 後段 | `geoframe_tone` | 探索する | **済む** |
| `sharpness` > 1 | 後段 | 輝度 USM（増分） | 探索する（上限 1.47） | **済む** |
| `sharpness` ≤ 1 | デコード | `sharpnessAmount = 既定 × s` | 同上 | **済む**（640px では効かない。§3.4） |
| `exposure` | デコード | `CIRAWFilter.exposure` | 探索する | 格子点ごとに 1 回（間は幾何平均か LUT。§3.5） |
| `noise_reduction` | デコード | `luminance/colorNoiseReductionAmount` | スタイルごとに固定（探索しない） | 使う値ごとに 1 回 |
| `wb_temp` | デコード | `neutralTemperature` | 探索しない（プリセット値のまま） | プリセット値で 1 回 |
| `highlight_recovery` | — | 受理するが効かせない | `highlight_class` は #85 で探索から外す予定 | — |

- #85 の探索次元（`highlight_class` を外した 6 次元: exposure・shadow_lift・brightness・contrast・saturation・sharpness）のうち
  **5 次元はデコード 1 回で済み**、デコードし直しが要るのは exposure だけ。デコード回数は
  「使う noise_reduction の値の数 × 訪れた exposure の格子点の数」で決まる。
- exposure を 1D LUT で近似すれば、デコードは noise_reduction の値ごとに 1 回（1〜3 回）まで減らせる（明部の誤差を許容する場合）。

## 5. #85 への推奨

### 5.1 代理レンダの方式

1. **ARW を 1 回だけメモリに読む**。`CIRAWFilter(imageData:identifierHint:)` で作る（出力は画素単位で同じ。SMB では 1 件
   1.45 秒 → 0.52 秒）。これだけで今の「1 評価 = 1 現像」の案のままでも約 2.8 倍速くなる。
2. **(noise_reduction, exposure 格子点) ごとにデコードを 1 回だけ行い、RGBAh でキャッシュ**する。格子は rawpy 側の
   `EXPOSURE_GRID`（0.5 EV）と同じでよい。キャッシュ 1 枚 2.2 MB なので、rawpy の `DECODE_CACHE_LIMIT`（24）でも約 53 MB。
3. 評価ごとには、キャッシュに**後段チェーン**を掛けるだけ（7〜8 ms）。exposure が格子点の間なら、**隣り合う 2 つの格子点の
   幾何平均**を線形空間で取ってから後段に渡す（ΔE ≤ 0.33）。
4. 線形ゲインでの近似は使わない（±0.25 EV で ΔE 1.0）。LUT 方式はデコード回数をさらに減らせるが、OS・decoder の版ごとの表の
   管理が要るので、まず幾何平均で始め、必要になったら検討する。
5. 埋め込みプレビュー・draft mode は使わない（§3.2・§3.3）。

### 5.2 1 枚あたりの見込み時間（640px、50 評価）

PSO の既定予算 48 + 基準の評価 2 回 ≒ 50 評価として、§2 の中央値から見積もった。

| 方式 | 計算 | 見込み |
|---|---|---|
| 現行案（評価ごとに CLI で現像、ARW は SMB） | 50 × 1.45 秒 | 約 73 秒 |
| 現行案（ARW がローカル / メモリ） | 50 × 0.50 秒 | 約 25 秒 |
| 推奨（キャッシュ、デコード 4 回: NR 1 値 × 格子点 4） | 0.53（SMB 読み込み）+ 4 × 0.52 + 50 × 0.008 | **約 3 秒** |
| 推奨（デコード 10 回: NR 2 値 × 格子点 5） | 0.53 + 10 × 0.52 + 50 × 0.008 | 約 6 秒 |
| （参考）exposure も LUT にする（デコード = NR の値の数 1〜3 回） | 0.53 + 2 × 0.52 + 50 × 0.008 | 約 2 秒 |

`proxy.decodes`（デコード回数）と新設の `proxy.render_ms` に加えて、後段だけの時間（`proxy.post_ms` など）も分けて載せると、
どちらが効いているかを後から追える。

### 5.3 CLI に足すモードの案（提案。#116 で `decode` / `render` / `release` として実装済み）

> **実装との差（#116、CLI 0.7.0）**: 仕様は `native/coreimage-cli/README.md`「decode キャッシュ」。ハンドルは呼び出し側の `key` ではなく
> **CLI が返す ID**。`post` は `render`、`evict` は `release`（冪等）。2 つの格子点の幾何平均（`keys` / `weights`）は未実装（1 ハンドル = 1 デコード）。
> 保持は half float（RGBAh）のビットマップ、上限は 32 件 / 256 MiB の LRU。実測は decode 約 0.8 秒 + render 4〜6 ms（p50、書き込み込み）。
> 以下は元の提案（参考）。

`--batch` の 1 行に「操作」を足し、同じプロセスの中でデコード結果を使い回す。既存の 1 行 1 現像のリクエストはそのまま動かす
（`op` が無ければ従来どおり）。

```jsonc
// 1) デコードしてキャッシュする（画像は書かない）。key は呼び出し側が付ける。
{"op":"decode","key":"DSC04624/nr=light/ev=+0.5","input":"/abs/DSC04624.ARW","max_edge":640,"decoder_version":"9",
 "settings":{"profile":"apple_default","lens_correction":true},
 "decode_adjustments":{"exposure":0.5,"wb_temp":0,"sharpness":1.04,"noise_reduction":"light"}}
// → {"ok":true,"op":"decode","key":"…","width":640,"height":427,"elapsed_ms":{"decode":…},"sequence":1}

// 2) キャッシュに後段だけを掛けて書く。exposure が格子点の間なら 2 つの key と重みを渡す（線形空間の幾何平均）。
{"op":"post","keys":["DSC04624/nr=light/ev=+0.0","DSC04624/nr=light/ev=+0.5"],"weights":[0.5,0.5],
 "output":"/abs/work/p-17.tiff","format":"tiff16","output_color_space":"srgb",
 "adjustments":{"exposure":0.25,"wb_temp":0,"shadow_lift":20,"highlight_recovery":60,"brightness":1.05,
                "contrast":1.1,"saturation":1.08,"sharpness":1.2,"noise_reduction":"light"}}
// → 応答は単発と同じ形 + "cache":{"keys":[…],"post_ms":…}

// 3) 捨てる（省略時は LRU で上限件数を超えた分から捨てる）。
{"op":"evict","keys":["…"]}
```

- CLI の中では、入力ごとに ARW の `Data` を保持（`CIRAWFilter(imageData:identifierHint:)`）し、デコード結果は RGBAh の
  ビットマップで保持する（`CIImage` や `CIRAWFilter` は保持しない。#82 の「`outputImage` を 2 回参照すると extent が半分になる」
  問題に触れない）。
- `post` は `adjustments` のデコード段のキー（exposure・wb_temp・sharpness ≤ 1・noise_reduction）が key の作成時の値と
  食い違ったら（exposure は重みで説明できない値なら）`INVALID_REQUEST` にする。黙って近似しない。
- `adjustments_applied` は単発と同じ形で返し、`develop()` との差（ΔE 0.02 程度、§3.1）は README に明記する。
- Rust の `BatchSession`（`src-tauri/src/coreimage.rs`）からは、1 枚の PSO の間だけセッションを保ち、終わったら `evict` する。

### 5.4 注意点

- **640px の代理は sharpness < 1 を見分けられない**（§3.4）。本番の 2400px とずれる点で、今の CLI 経路でも同じ。
  デコードの時間は 0.5 以下の `scaleFactor` でほぼ一定（§2.2）なので、キャッシュを 1280px（`scaleFactor` 0.213、デコーダ内
  シャープが効く）で作り、後段の後で 640px に縮める、という選択肢もある。その場合は後段 USM の長辺の扱いと後段の時間
  （画素 4 倍）を別に測ること。
- exposure の近似（幾何平均・LUT）の誤差は明部に出る（明るい空など。§3.5）。PSO の白飛び指標が明部に敏感なら、
  格子点ちょうどの評価を優先する等の工夫が要る。
- 数値は ISO 3200 まで・7 枚・この 1 台での計測。ISO 6400 と星空は含まない（§1.2）。
- `/Volumes/Backup` のような SMB の現像元では、ARW の読み込みが支配的になる。PSO 以外（アプリのプレビュー現像、ブラケット）でも
  同じファイルを続けて現像するなら、メモリに読んでから渡す方が速い【推測: アプリの現像元が SMB の場合】。
