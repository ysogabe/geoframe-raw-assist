# ADR 0001: Core Image (CIRAWFilter) による Sony ARW 現像 PoC

- ステータス: 提案（PoC / Issue #65 PR1）
- 日付: 2026-10-19
- 対象: `native/coreimage-cli/`（Swift CLI `geoframe-ci-develop`）
- 検証環境: macOS 27.0.1 (26A434) / Apple M3 / 24 GB / Xcode Swift 6.4 / 入力は外付け `/Volumes/Backup/100MSDCF/`（α6400 ARW 451 枚）

## 背景

現行の現像エンジン（`local_raw/engine.py`）は rawpy(LibRaw) + lensfun + 自前トーン処理で、
シャープ・縮小・NR の組み合わせでピントが甘く見える問題を調査してきた。macOS 標準の
Core Image RAW（`CIRAWFilter`）は α6400 を公式にサポートしており、写真.app / プレビュー.app と同じ
デコーダを使える。本 PoC は「同じ ARW を Core Image で現像したらどう見えるか」を
現行エンジンと**比較できる形**で出力するための CLI を用意する。アプリ（Tauri / React / Python）への
組み込みは本 ADR の範囲外（後続 PR）。

> 注: 本文中の `/tmp/ci-poc/...` はこの検証セッションの一時出力（再起動で消える）。恒久的な記録は本文の表で、
> 出力は `native/coreimage-cli/README.md` のリクエスト例で再現できる。

## 決定

### 1. CLI 契約

- 実行ファイル `geoframe-ci-develop`（SwiftPM、`swift build -c release`）。
- リクエストは **stdin に JSON 1 個**（引数にパスを並べるとシェル/Rust 側のエスケープ問題が出るため）。
  応答は **stdout に JSON 1 行**、ログは stderr。stdout に JSON 以外は出さない。
- `--version` / `--capabilities [probe.ARW]` は引数で受ける（入力 JSON 不要）。
- 失敗時は `{"ok":false,"error":{"code","message"}}` と非 0 終了（処理エラー 1、引数エラー 2）。
- 未知キーは `INVALID_REQUEST` で拒否する（タイプミスで既定値のまま黙って現像されるのを防ぐ）。
- 入力は読むだけ。出力は **output と同じディレクトリ**の一時ファイル `.<name>.<UUID>.tmp` に書き、
  `rename(2)` で置き換える。失敗時は一時ファイルを消し、既存 output は壊さない。
  input と同一実体（同一パス・シンボリックリンク・ハードリンク）への出力は拒否する。
- 詳細なフィールド・エラーコードは `native/coreimage-cli/README.md`。

### 2. 現像順序

```
CIRAWFilter(imageURL:)                      // EXIF/RAW メタデータ読み込み
 → decoderVersion を最初に設定              // ★設定すると他パラメータが既定値へ戻る（後述）
 → scaleFactor = min(1, max_edge / 長辺)    // full は 1.0。preview と full の差はこれだけ
 → isDraftModeEnabled = false               // 常に OFF
 → profile（neutral なら下記の値を設定）
 → exposure / neutralTemperature / neutralTint / isLensCorrectionEnabled
 → outputImage（向き適用済み）
 → CIContext（作業空間 extended linear sRGB, RGBAh）でレンダリング
 → 出力空間へ変換（JPEG: sRGB 8bit / TIFF: Display P3 16bit）
 → ImageIO でエンコード（＋JPEG は sRGB ICC を APP2 に挿入）→ 一時ファイル → rename
```

`decoderVersion` の順序は実測で決めた: `sharpnessAmount = 0` を設定した後に `decoderVersion` を
設定すると `sharpnessAmount` が 1.0 に戻った。そのため CLI は decoderVersion を最初に設定し、
応答の `applied` は**全パラメータを設定し終えた後に filter から読み戻した値**を返す。

縮小は CIRAWFilter の `scaleFactor`（デコーダ内部での縮小）に任せ、後段のリサイズは行わない。
これにより preview と full で処理経路が一致する（現行エンジンの AREA 縮小とは異なる点に注意）。

### 3. 色管理

| 段 | 方針 |
|---|---|
| RAW → シーン線形 | CIRAWFilter 内部（カメラマトリクス + ホワイトバランス + baselineExposure + トーンカーブ）。 |
| 作業空間 | `CIContext` の `workingColorSpace = extendedLinearSRGB`, `workingFormat = RGBAh`（half float, 線形, 負値・1超を保持）。 |
| JPEG 出力 | `CGColorSpace.sRGB`（IEC61966-2.1, sRGB トーン応答 ≈ γ2.2）, 8bit。`isGamutMappingEnabled = true`（既定）で sRGB 外の色を圧縮。 |
| TIFF16 出力 | `CGColorSpace.displayP3`（sRGB と同じトーン応答、広色域）, 16bit/ch 整数, アルファ無し 3ch。 |
| ICC | JPEG: sRGB IEC61966-2.1（3144 byte）を APP2 `ICC_PROFILE` で埋め込む。TIFF: ImageIO が Display P3 ICC（536 byte）を埋め込む。 |

**JPEG の ICC は手で入れている。** ImageIO は sRGB の JPEG に ICC を書かず EXIF `ColorSpace=1`
だけを書く（名前付き sRGB・ICC データから作った sRGB・`CIContext.jpegRepresentation` の
3 通りすべてで ICC 0 byte を確認）。`sips` は EXIF から「sRGB IEC61966-2.1」と表示するが、
PIL などは ICC 無しと判定するため、Apple の sRGB プロファイル（`copyICCData()`）を APP2 に挿入した。

TIFF16 を extended linear sRGB にしなかった理由: 16bit **整数**バッファでは負値・1超を保持できず、
線形だと暗部の量子化が粗くなる。half float TIFF は読めるツールが限られる。比較用中間ファイルとしては
ガンマ付きの広色域（Display P3）が扱いやすい。

### 4. 中立プロファイル（`profile: "neutral"`、既定）

**定義: 局所的・装飾的な処理（シャープ・ディテール・NR・モアレ除去・コントラスト・ローカルトーン・EDR）を
すべて 0 にし、表示用トーンカーブ（boostAmount / boostShadowAmount / baselineExposure / shadowBias）は
CIRAWFilter 既定のまま残す。レンズ補正は明示指定時のみ ON。**

| パラメータ | neutral | apple_default | 理由 |
|---|---|---|---|
| sharpnessAmount | **0** | 既定 (1.0) | 比較時にデコーダのシャープを混ぜない |
| detailAmount | **0** | 既定 (0) | 同上 |
| luminanceNoiseReductionAmount | **0** | 既定（ISO/decoder 依存: v8 ISO3200=0.676, v9 ISO3200=0.241, v9 ISO100=0.01） | NR はディテールを溶かす。既定値が decoder・ISO で大きく変わる |
| colorNoiseReductionAmount | **0** | 既定（v8=0.5, v9=0） | 同上 |
| moireReductionAmount | **0** | 既定（v8 ISO100=0.5, それ以外 0） | 同上 |
| contrastAmount | **0** | 既定 (0) | トーンは boost に一本化 |
| localToneMapAmount | **0**（サポート時のみ） | 既定 | α6400 では未サポート |
| extendedDynamicRangeAmount | **0** | 既定 (0) | SDR 出力 |
| boostAmount | 既定 **1.0** | 既定 | 下記実測 |
| boostShadowAmount | 既定 **0.9** | 既定 | 下記実測 |
| baselineExposure | 既定（ファイル値 **0.4**） | 既定 | カメラ側の露出オフセット。0 にすると約 0.4EV 暗くなる |
| shadowBias | 既定 **3.0** | 既定 | 黒点。0 にすると黒が浮く |
| isGamutMappingEnabled | true | true | sRGB 外色のクリップ回避 |
| isLensCorrectionEnabled | **settings に従う（既定 false）** | settings に従う（既定 false） | CIRAWFilter 既定は ON だが CLI では明示指定のみ |
| isDraftModeEnabled | false | false | 常に OFF |

**実測根拠**（decoderVersion 9、scaleFactor 0.4 → 2400px、lens_correction false、出力 sRGB 8bit、
輝度 = Rec.709 係数、0..255。カメラ JPEG は同名 .JPG（6000px）。v8 でもほぼ同値）:

DSC04430（横位置, Sigma 30mm F1.4, ISO 3200）

| 候補 | mean | p1 | p5 | p50 | p95 | p99 | std |
|---|---|---|---|---|---|---|---|
| カメラ JPEG | 96.7 | 16.6 | 21.1 | 89.8 | 208.2 | 219.4 | 58.9 |
| A: CIRAWFilter 既定 | 101.2 | 19.7 | 24.6 | 94.8 | 211.0 | 222.7 | 58.6 |
| **B: 局所処理のみ 0（採用）** | 101.2 | 20.4 | 24.3 | 94.7 | 211.1 | 222.9 | 58.7 |
| C: B + boost=0 | 79.2 | 22.4 | 25.8 | 73.1 | 167.7 | 182.4 | 42.1 |
| D: C + boostShadow=0 | 79.2 | 22.4 | 25.8 | 73.1 | 167.7 | 182.4 | 42.1 |
| E: B + boostShadow=0 | 96.5 | 7.6 | 11.3 | 92.4 | 211.1 | 222.9 | 63.0 |
| F: B + boost/boostShadow/baselineExposure/shadowBias=0 | 71.3 | 24.3 | 26.6 | 65.4 | 148.4 | 161.1 | 36.2 |
| G: C + shadowBias=0 | 81.8 | 28.8 | 31.6 | 75.2 | 168.2 | 182.6 | 40.7 |
| H: B + boost=0.5 | 91.2 | 21.5 | 25.1 | 84.7 | 191.2 | 204.2 | 51.1 |

DSC04553（縦位置, Sigma 10-18mm, ISO 100）

| 候補 | mean | p1 | p5 | p50 | p95 | p99 | std |
|---|---|---|---|---|---|---|---|
| カメラ JPEG | 99.8 | 13.1 | 21.5 | 83.9 | 179.0 | 186.6 | 62.7 |
| A: 既定 | 96.4 | 10.2 | 22.5 | 85.1 | 169.2 | 175.4 | 57.4 |
| **B: 採用** | 96.6 | 10.7 | 22.7 | 85.2 | 169.3 | 175.8 | 57.5 |
| C: boost=0 | 73.9 | 13.2 | 24.6 | 67.9 | 120.9 | 125.9 | 36.6 |
| E: boostShadow=0 | 91.2 | 2.1 | 9.4 | 81.7 | 169.3 | 175.8 | 62.7 |
| F: 全 0 | 66.9 | 18.1 | 25.8 | 60.9 | 107.4 | 112.1 | 31.3 |

読み取り:

- **boostAmount=0 は「線形寄りで暗く眠い」絵になる**。平均が 2 割強下がり、p99 が 182（DSC04430）/126（DSC04553）
  で白に届かない（ハイライトが灰色に寝る）。目視でも明確にフラット。boost は「好みの味付け」ではなく
  **シーン線形 → 表示用 sRGB のベーストーンカーブ**として働いているので中立の構成要素とみなす。
- **boostShadowAmount は boostAmount=0 のとき効果が無い**（C と D が完全一致）。boost=1 のとき 0 にすると
  p1 が 20→8 / 11→2 に沈み、DSC04553 では 0.17% の画素が真っ黒になる（E）。既定 0.9 がカメラ JPEG の
  暗部（p1 16.6 / 13.1）に最も近い。
- **baselineExposure / shadowBias を 0 にすると**さらに暗く・黒が浮く（F, G）。baselineExposure はカメラ
  プロファイルの露出オフセット（ファイル値）で、消すとカメラの測光意図から外れる。
- **B はカメラ JPEG と分布がほぼ一致**（mean 差 +4.5 / −3.2、p95 差 +2.9 / −9.7）。A と B の差は
  局所処理だけで、全体トーンは変わらない（=局所処理を外しても明るさの基準が動かない）。

以上から「表示用 sRGB として妥当な最小処理」= B を neutral と定義した。

比較用サンプル（目視確認用）: `/tmp/ci-poc/neutral9/compare_DSC04430.jpg`
（左から カメラ JPEG / A / B / C / F）。各候補 PNG は `/tmp/ci-poc/neutral9/<file>/` と
`/tmp/ci-poc/neutral/<file>/`（v8）。

### 5. decoderVersion 固定方針

- α6400 では `supportedDecoderVersions = ["8", "9"]`。リクエストで省略すると**そのファイルで使える最新**
  （この環境では "9"）を使い、応答の `decoder_version` に解決後の値を返す。
- **呼び出し側（アプリ）は必ず明示指定し、レシピに記録すること。** 省略値は OS 更新で黙って変わりうる。
- 実測で v8 と v9 は同じ設定でも出力が変わる（neutral, 2400px TIFF16 比較）:
  - DSC04430 (ISO 3200): 画素差 平均 10.4/255、p99 27.9、最大 73.1（平均輝度は 95.7 / 95.9 でほぼ同じ。
    デモザイク／ノイズパターンの違い）
  - DSC04553 (ISO 100): 平均 1.77/255、p99 4.9、最大 11.6
  - 既定 NR 値も v8/v9 で大きく違う（§4 の表）。neutral はこの差を NR=0 で消す。
- PoC の比較用には **"9"**（この OS の最新）を使う。古い macOS で "9" が使えるかは未確認。

### 6. レンズ補正方針

- CIRAWFilter の既定は ON だが、CLI では **`settings.lens_correction: true` のときだけ ON**（両 profile）。
  現行エンジンは lensfun で補正しており、二重補正・比較条件の混乱を避けるため。
- 3 本のレンズで ON/OFF を比較（neutral, decoder 8, preview 2400px TIFF16。画素差は 16bit を 0..255 換算、
  幾何は ORB 特徴点 + RANSAC の相似変換推定）:

| ファイル | レンズ | 焦点距離 | `lens_correction_supported` | 画素差 平均 / p99 / 最大 | 変化画素 (>1/255) | 推定スケール ON/OFF | 隅(左上10%)輝度 ON/OFF |
|---|---|---|---|---|---|---|---|
| DSC04465 | Sigma 10-18mm F2.8 DC DN | 10mm | true | 36.6 / 144.6 / 255 | 98.5% | **0.974**（+ 半径依存の残差 ≈10px@r0.3-0.6） | 46.1 / 44.2 |
| DSC04430 | Sigma 30mm F1.4 DC DN | 30mm | true | 12.2 / 37.8 / 107.5 | 93.9% | **0.9999**（残差 1-2px） | 51.3 / 50.5 |
| DSC04759 | Sony E 55-210mm F4.5-6.3 OSS | 62mm | true | 11.8 / 106.2 / 254.4 | 96.5% | **1.017**（残差 外周ほど大 ≈10px@r>0.8） | 13.7 / 12.2 |

  読み取り:
  - 3 本とも**実際に補正が掛かる**（出力寸法は同じ 2400x1600 のまま、内部で再サンプル＋クロップ）。
  - 10-18mm @10mm: 縮小方向 2.6% + 非相似な残差 → 樽型歪曲補正。隅が約 2/255 明るくなる（周辺減光補正）。
  - 30mm: 幾何はほぼ不変（スケール 0.9999）。画素差の大半は ISO 3200 のノイズが再サンプルで位置ずれした
    ことと、周辺の微小な減光補正（隅 +0.8/255）による。実質的な効果は小さい。
  - 55-210mm @62mm: 拡大方向 1.7% + 外周ほど大きい残差 → 糸巻き型歪曲補正。隅 +1.5/255。
  - 補正の中身（歪曲・周辺減光・倍率色収差のどれを掛けているか、DNG Opcode 相当か Apple 独自プロファイルか）は
    API から読めない。lensfun との数値比較は後続 PR。
- サンプル: `/tmp/ci-poc/lens/<file>_lc_{true,false}.tif`

### 7. 向きの扱い

- `CIRAWFilter.orientation` は EXIF Orientation（DSC04553 = 8）を読み、**`outputImage` は向き適用済み**
  （DSC04553 の extent = 4000x6000、`nativeSize` は回転前の 6000x4000）。
- CLI は追加の回転をせず、出力に Orientation=1 を書く（二重回転しない）。応答 `source.native_width/height` は
  回転前、`width/height` は出力（回転後）。
- DSC04553 の preview / full とも 1600x2400 / 4000x6000 で、目視で正立（山頂標柱が縦に立つ）を確認。
  スモークテスト `testPortraitIsWrittenUpright` でも検査。

### 8. メタデータ

出力には Software（`geoframe-ci-develop 0.1.0`）、Orientation=1、ImageIO が付ける最小の EXIF
（ColorSpace / PixelX/YDimension 等）のみ。元 RAW の EXIF・GPS は引き継がない
（DSC04553 は元 ARW に GPS あり → 出力の GPS IFD 空を確認）。

### 9. 処理時間（full, DSC04430, decoder 9, neutral, 6000x4000）

| 形式 | 1 回目 | 2 回目 | 3 回目 |
|---|---|---|---|
| JPEG (q 0.92) | 2230.6 ms | 2170.5 ms | 2219.2 ms |
| TIFF16 | 2314.5 ms | 2381.0 ms | 2338.6 ms |

内訳（JPEG 例）: `decode` ≈ 310-360 ms、`render_encode` ≈ 1830-1910 ms。Core Image は遅延評価なので
`decode` はフィルタ生成・パラメータ設定・`outputImage` 取得までで、**実際のデモザイクは `render_encode` に入る**。
preview (2400px) は約 1.1-1.4 s。外付けボリュームのコールドキャッシュ初回は `decode` が 4.2 s まで伸びた
（ファイル読み込み待ち）。プロセス起動時間は含まない。

## 検討した代替案

- **boostAmount=0 を中立とする**: §4 の実測で暗く・ハイライトが白に届かず不採用。
- **CIRAWFilter 既定値（apple_default）を中立とする**: シャープ・NR の既定値が ISO・decoderVersion で
  大きく変わり（luminanceNR 0.01〜0.68）、比較条件が揃わないため不採用。比較用に profile として残す。
- **CIContext.jpegRepresentation / writeJPEGRepresentation**: ICC が埋め込まれず、メタデータの制御もしづらいため
  不採用（ImageIO + 手動 APP2）。

## 未確認事項・後続

- 古い macOS（14/15）で decoder "9" の可否、出力の一致。アプリの最低 OS 要件との整合。
- レンズ補正の内訳（歪曲 / 周辺減光 / 倍率色収差）と lensfun 補正との差。
- 現行エンジン（rawpy）との画質比較（解像感・ノイズ・色）。本 PR は比較材料の出力まで。
- `neutralTemperature` / `neutralTint` の Kelvin・tint の意味（カメラ WB との関係、範囲）。
- ハイライト復元系（`extendedDynamicRangeAmount`、boost 下のハイライト圧縮）の評価。
- 並列実行時のメモリ（full TIFF16 で RGBA16 バッファ 192MB + CI 中間）。
- 2400px 以外の preview サイズでの `scaleFactor` 縮小品質（デコーダ内縮小のアルゴリズムは非公開）。
- ARW 以外（同 OS がサポートする他社 RAW）は拡張子チェックで意図的に拒否している。
