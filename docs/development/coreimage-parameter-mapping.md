# 現行 Adjustments と Core Image（CIRAWFilter）パラメータの対応

Issue #65 PR1 時点の整理（#72/#73 での決定は末尾の「#73 での決定」節に追記した。この表の「PoC での扱い」列は PoC 時点の記述で、現在の扱いではない）。現行エンジンは `local_raw/engine.py`（ENGINE_VERSION 17 系）、
Adjustments の型と値域は `src/lib/types.ts`。Core Image 側は `CIRAWFilter`（macOS 27, α6400, decoder 8/9）と
後段に挿せる標準 `CIFilter`。CLI の決定事項は [ADR 0001](../adr/0001-coreimage-raw-poc.md)。

## 原則

- **名前が似ていても意味が違うものは対応付けない。** 「対応」は同じ量を同じ段で操作するもの、
  「近似」は目的は同じだが量・段・曲線が違うもの（違いを明記）、「対応なし」は置き換え先が無いもの。
- 現行エンジンの処理段は 2 種類ある:
  - **デコード段（シーン参照）**: rawpy `postprocess` の引数（`bright` / `user_wb` / `highlight_mode` / `fbdd_noise_reduction`）と lensfun 補正。
  - **後段（表示参照, sRGB ガンマ後の 0..1 float）**: shadow_lift LUT → リサイズ → Brightness → Contrast → Color(saturation) → 輝度 USM。
- CIRAWFilter のパラメータはすべてデコーダ内部（シーン線形〜トーンカーブの間）で効く。
  現行の「後段」に相当する処理を Core Image で再現するには、`outputImage` の後に CIFilter を挿し、
  **sRGB ガンマ空間で演算する**必要がある（Core Image の作業空間は線形なので、
  `CILinearToSRGBToneCurve` → 演算 → `CISRGBToneCurveToLinear` で挟むか、作業空間を sRGB にした CIContext を使う）。
- **PoC（PR1）の CLI はどの Adjustments も受け付けない。** 受け付けるのは `exposure_ev` / `neutral_temperature` /
  `neutral_tint` / `lens_correction` / `profile` のみで、これらも Adjustments の値をそのまま流用してはいけない（下表）。

## 対応表

| Adjustments | 値域（types.ts） | 現行エンジンでの実装 | Core Image 側の候補 | 判定 | 違い・根拠 | PoC での扱い |
|---|---|---|---|---|---|---|
| `exposure` | EV -2..+2 | LibRaw `bright = 2**ev`（`exp_shift` は 1.0 固定）。通常シーンは **auto_bright（ヒストグラム正規化, thr 0.01）の後**に掛かる。暗いシーン（EV100 ≤ 6）は auto_bright を止め `bright = 5.0 × 2**ev` | `CIRAWFilter.exposure`（EV、`baselineExposure` に加算、シーン線形） | **近似** | 同じ「EV」でも基準が違う。現行は画像ごとに自動正規化された明るさが基準で、+1EV は「正規化後の 2 倍」。CI は固定の baselineExposure（α6400=0.4）が基準でトーンカーブの前に掛かる。auto_bright 相当の自動露出は CI に無い。同じ値で同じ明るさにはならない | CLI `settings.exposure_ev` が CIRAWFilter.exposure に直結。Adjustments.exposure を流すと明るさの基準がずれる（特に暗所・逆光） |
| `wb_temp` | -100..100（0 = カメラ WB） | カメラ WB 係数 `[R,G,B,G]` の R を `×(1+0.003·v)`、B を `×(1−0.003·v)`（`user_wb`） | `CIRAWFilter.neutralTemperature`（K）/ `neutralTint` | **近似**（値の変換が必要） | 現行は無次元の R/B ゲイン比（±30%）、CI は色温度 K と tint の絶対値。`wb_temp ≠ neutralTemperature`。換算は「カメラ WB の K（ファイル依存: DSC04430 ≈3625K, DSC04553 ≈6415K）を起点に R/B 比が同じになる K/tint を求める」必要があり、線形関係ではない（tint にも漏れる）。CI の `neutralTemperature` が「光源色温度」か「補正量」かの意味も未確認 | CLI は `neutral_temperature/tint` を null（撮影時 WB）既定で、K の生値のみ受ける。wb_temp からの換算は未実装 |
| `shadow_lift` | 0..100 | 表示参照の float に `y = x + s·(SHADOW_LIFT_GAIN/255)·x·(1−x)²`（s = v/100、ピーク x≈1/3、ハイライト不変）。リサイズ前 | `CIRAWFilter.shadowBias` / `boostShadowAmount`、後段 `CIHighlightShadowAdjust.shadowAmount`、`CIToneCurve` | **対応なし**（CIRAWFilter）／後段で**再現可** | `shadowBias` はシーン側の**黒点バイアス**（既定 3.0。0 で黒が浮く）で暗部持ち上げではない。`boostShadowAmount` は boost カーブの暗部の締まり（0 で黒が沈む、boost=0 では無効: ADR §4）で方向も意味も違う。`CIHighlightShadowAdjust` は局所適応型で現行の大域 LUT と別物。**同じ式を再現するなら sRGB ガンマ空間での `CIColorCubeWithColorSpace` / `CIToneCurve`（5 点近似）または自前 CIKernel** | 未対応（neutral では shadowBias/boostShadow を既定のまま触らない） |
| `highlight_recovery` | 0..100 | rawpy `highlight_mode` を離散選択: 0 → Clip、<75 → Blend、≥75 → ReconstructDefault | なし（`extendedDynamicRangeAmount` は EDR/HDR 出力用、`boostAmount` はトーンカーブ） | **対応なし** | CIRAWFilter はハイライト再構成の方式を選ぶ API を持たない（デコーダ内部で常に処理）。boost のショルダーで白飛びの見え方は変わるが、クリップしたチャンネルの再構成方式を変えるものではない。`CIHighlightShadowAdjust.highlightAmount` は表示参照の局所トーン圧縮で別物 | 未対応。比較時は現行側の highlight_recovery を記録しておく |
| `brightness` | 0.7..1.4 | 表示参照 float に乗算 `x × b`（PIL ImageEnhance.Brightness 相当） | 後段 `CIColorMatrix`（R/G/B ベクトルを b 倍）を sRGB ガンマ空間で | **後段で対応可**（CIRAWFilter には無い） | `CIColorControls.inputBrightness` は**加算オフセット**（x + b）で乗算ではないので使わない。`CIExposureAdjust` は線形空間の乗算で、ガンマ空間の乗算とは曲線が違う（近似にしかならない）。クリップ: 現行は最後に [0,1] へクリップ、CI は extended range のまま出力変換でクリップ | 未対応 |
| `contrast` | 0.8..1.4 | 表示参照 `(x − 0.5) × c + 0.5` | 後段 `CIColorMatrix`（scale c, bias 0.5(1−c)）を sRGB ガンマ空間で ／ `CIRAWFilter.contrastAmount` | 後段 CIColorMatrix は**対応可**、`contrastAmount` は**近似** | `contrastAmount` はデコーダ内部のトーンカーブ（boost と組み合わさる、曲線非公開、既定 0）で、現行の 0.5 ピボット線形とは別物。`CIColorControls.inputContrast` もピボット・空間が異なるため近似 | 未対応。neutral では `contrastAmount = 0` |
| `saturation` | 0.0..1.5（0 = モノクロ） | 表示参照で Rec.709 輝度 Y を基準に `Y + (x − Y) × s` | 後段 `CIColorMatrix`（Rec.709 係数で組んだ彩度行列）を sRGB ガンマ空間で ／ `CIColorControls.inputSaturation` | CIColorMatrix は**対応可**、CIColorControls は**近似** | CIColorControls の saturation は線形作業空間で効き、輝度係数も公開されていない。CIRAWFilter に彩度パラメータは無い | 未対応 |
| `sharpness` | 0.8..1.6（1.0 = カメラ JPEG 相当） | 出力サイズ後の**輝度 USM**: σ 0.6〜0.8 / gain 0.5〜0.7（出力長辺 2000〜4000px で補間）× スライダー倍率、ISO でコアリングと gain 減衰（`_capture_sharpen_params`） | `CIRAWFilter.sharpnessAmount`（デコーダ内, 既定 1.0）／`detailAmount`／後段 `CISharpenLuminance`・`CIUnsharpMask` | **近似** | `sharpnessAmount` はデモザイク段のシャープで、出力サイズ（scaleFactor）や ISO に対する振る舞い・曲線は非公開。現行の 1.0（カメラ JPEG 相当に較正済み）と CI の 1.0 は等価ではない。現行と同じ処理は「CI では sharpness 0 で現像 → 出力サイズで輝度 USM」（`CISharpenLuminance` は radius/sharpness の意味が USM と異なるため、厳密には自前 CIKernel か現行コードで後処理） | neutral は `sharpnessAmount = 0` / `detailAmount = 0`（比較時にデコーダのシャープを混ぜない）。apple_default は既定 1.0 |
| `noise_reduction` | `"off"` \| `"light"` \| `"full"` | LibRaw FBDD（デモザイク前）: off → Off、light → Light（ISO < 800 なら Off）、full → Full + median 1 pass | `CIRAWFilter.luminanceNoiseReductionAmount` / `colorNoiseReductionAmount`（0..1 連続）／後段 `CINoiseReduction` | **近似** | 方式が違う（FBDD はデモザイク前のバンディング/偽色抑制、CI は輝度・色差別の連続量）。CI の既定値は ISO と decoderVersion で大きく変わる（luminance: v8 ISO3200 = 0.676 / v9 ISO3200 = 0.241 / v9 ISO100 = 0.01、color: v8 = 0.5 / v9 = 0。ADR §4）。3 段の enum と連続量の対応表は未定 | neutral は両方 0（＋`moireReductionAmount = 0`）。apple_default は既定 |

## Adjustments 以外の処理

| 現行の処理 | 現行の実装 | Core Image 側 | 判定 | PoC での扱い |
|---|---|---|---|---|
| レンズ補正 | lensfun（`local_raw/lens.py`: 周辺減光・歪曲・TCA、失敗時は無補正で続行、広角の周辺減光は白飛び防止で抑制あり） | `CIRAWFilter.isLensCorrectionEnabled`（既定 ON、中身は非公開） | **近似** | CLI は `lens_correction` 明示時のみ ON。3 本とも実際に補正が掛かることを確認（ADR §6）。lensfun との数値比較は未実施 |
| 自動明るさ | LibRaw auto_bright（thr 0.01）、暗所は固定倍率 5.0 | なし（`baselineExposure` は固定のファイル値） | **対応なし** | 未対応。暗所・逆光で両エンジンの明るさが大きく食い違う可能性 |
| デモザイク | LibRaw AHD、half_size なし | デコーダ内部（`decoderVersion` 8/9 で変わる、方式非公開） | 対応なし（選択不可） | `decoder_version` を明示指定して固定 |
| 縮小 | フル解像度で現像 → OpenCV INTER_AREA（6000px 超は 2% 未満なら中央クロップ） | `CIRAWFilter.scaleFactor`（デコーダ内縮小） | 近似 | CLI は scaleFactor のみ（後段リサイズなし）。縮小アルゴリズムは非公開 |
| 出力色空間 | rawpy `output_color = sRGB`、16bit → float → 8bit JPEG（EXIF・ICC は元 JPEG から） | 作業空間 extended linear sRGB → sRGB（JPEG）/ Display P3（TIFF16） | 対応 | JPEG は sRGB ICC を埋め込み、GPS・撮影 EXIF は書かない |
| 表示用トーンカーブ | LibRaw の sRGB ガンマのみ（S 字カーブ無し、明るさは auto_bright 任せ） | `boostAmount` / `boostShadowAmount` / `shadowBias` / `baselineExposure` | **対応なし**（現行に相当物が無い） | neutral でも既定値のまま残す（0 にすると暗く眠い: ADR §4）。ここが両エンジンの「見た目の差」の最大要因になりうる |
| 向き | engine.py に明示処理なし（LibRaw 既定の user_flip に依存。未確認） | `CIRAWFilter` が EXIF Orientation を適用済みの `outputImage` を返す | 対応 | 出力 Orientation=1（二重回転しない） |

## 未確認事項

- `neutralTemperature` / `neutralTint` の厳密な意味（光源 K か補正後の白点か）と、wb_temp（R/B ゲイン比）からの換算式。
- `contrastAmount` / `sharpnessAmount` / `detailAmount` の曲線・カーネル（非公開）と、出力サイズ・ISO 依存性。
- noise_reduction の 3 段と CI の連続量の対応値（ISO 別のテーブルが必要か）。
- CIRAWFilter のハイライト処理（クリップしたチャンネルの再構成）の方式。
- 現行 auto_bright 相当の自動露出を CI 側で用意するか（`CIAreaAverage` 等で計測 → exposure に反映）。
- 後段処理（brightness / contrast / saturation / shadow_lift / USM）を CIFilter で組む場合の色空間の扱いと、現行 numpy 実装とのビット一致の可否。

## #73 での決定（CLI 0.3.0、2026-10-21）

上の表の「判定」を受けて、9 キーを次のように写像した。実装は `native/coreimage-cli/Sources/CoreImageDevelop/Adjustments.swift`（デコード段の写像と後段チェーン）、Rust 側の正規化は `src-tauri/src/adjustments.rs`。
詳細は [設計書](../design/coreimage-preview-adjustments.md) §3.3、較正の経緯は [比較レポート §16](../reports/coreimage-engine-comparison.md)。
写像は `apple_default` の既定値を基準にした**相対値**で、`adjustments` は `profile: "apple_default"` のときだけ受ける。
CLI は `adjustments` の 9 キーをすべて必須とし、範囲外は `INVALID_REQUEST`（クランプしない。クランプは Rust の責務）。

| Adjustments | 決定した写像 | 段 | 応答（`adjustments_applied`） |
|---|---|---|---|
| `exposure` | `CIRAWFilter.exposure = ev`（基準は固定の baselineExposure。rawpy の auto_bright 基準とは違う。±1 EV は T5 で rawpy 比 0.8..1.25） | デコード | `applied.exposure` |
| `wb_temp` | **mired シフト**: `K0` = 撮影時 WB（profile 適用後の `neutralTemperature` の読み戻し）、`mired = 1e6/K0 − v × 0.85`（`MIRED_PER_UNIT`、T5 で較正し据え置き）、`K = clamp(1e6/mired, 2000, 50000)` を `neutralTemperature` に設定。tint は触らない。v>0（暖）で K 増。K0 が有限かつ 2000..50000 でなければ写像を飛ばし `guards: ["wb_k0_out_of_range"]` | デコード | `post.wb.{k0,mired_shift,mired_per_unit}`、guard |
| `shadow_lift` | `lift = v/100` を自前 Metal カーネル `geoframe_tone` で sRGB ガンマ空間に適用（rawpy と同じ式） | 後段 | `post.shadow_lift` |
| `brightness` / `contrast` / `saturation` | 同じ `geoframe_tone` で rawpy と同じ式（乗算 / 0.5 ピボット / Rec.709 輝度基準）を sRGB ガンマ空間で適用 | 後段 | `post.brightness` 等 |
| `sharpness` | **ハイブリッド**: s ≤ 1 は `sharpnessAmount = 既定 × s`（USM なし）。s > 1 は `sharpnessAmount` を既定のまま、輝度 USM（自前 Metal カーネル）で増分だけ足す（`sigma, baseGain, coring = captureSharpenParams(長辺, ISO)`、`gain = min(1.2, baseGain × (s−1) × 1.5) × isoMult`）。rawpy の撮影時シャープ（基準 gain）は掛けない（二重がけ回避） | デコード + 後段 | `sharpness_amount`、`post.usm.*` |
| `noise_reduction` | `off` = 輝度 0 / 色 0、`light` = 既定のまま、`full` = 輝度 `max(既定, min(0.5, max(既定+0.3, 0.3)))`（既定が 0.5 を超える高 ISO では既定のまま。full を light より弱くしない） / 色 `max(既定, 0.3)`（下表） | デコード | `luminance_noise_reduction_amount`、`color_noise_reduction_amount`、`post.noise_reduction` |
| `highlight_recovery` | 0.5.0 までは **no-op**（受理するが効かせない）。**#81（CLI 0.6.0）で `exposure` / `shadow_lift` への写像に置き換えた**（下の「#81 での決定」） | — | 0.5.0: `ignored: ["highlight_recovery"]`、0.6.0: `ignored: []` |

`noise_reduction` の表（`apple_default` v9・macOS 27.0.1、**輝度の上限 0.5**。旧暫定は ISO 3200 = 0.541、6400 = 0.657 だったが、空の高周波がカメラ比 0.5 を割るため T5 で頭打ちにした）:

| ISO 帯 | light（= 既定）輝度 / 色 | off | full |
|---|---|---|---|
| < 800 | ≈0.01 / 0 | 0 / 0 | ≈0.31 / 0.30 |
| 800〜1600 | 未計測 | 0 / 0 | min(0.5, 既定 + 0.3) / 0.30 |
| 3200 | 0.241 / 0 | 0 / 0 | 0.5 / 0.30 |
| 6400 | 0.357 / 0 | 0 / 0 | 0.5 / 0.30 |

- 白点ガード: `adjustments` 指定時に限り、輝度 NR 0 かつ sharpness 0 になる組み合わせを `sharpnessAmount = max(…, 0.1)` に補正し `guards: ["white_dot_sharpness_floor"]` に記録（通常のスライダー範囲では発動しない）。
- 後段（shadow_lift / brightness / contrast / saturation / USM）は CLI 内の Metal カーネルで 1 回のレンダに含める。rawpy の numpy 実装との差はオラクル比較（tiff16 sRGB、ΔE76 中央値 0、チャンネル差 p99 ≤ 0.001）で確認済み。
- 原寸・書き出しは当面 rawpy のままなので、上の写像はプレビューにだけ効く。

## #81 での決定（CLI 0.6.0、2026-10-22）

`highlight_recovery`（hr）を、#92 で承認した C1'（`exposure` −0.7 + `shadow_lift` 40、強め −1.0 + 55）を節点にした区分線形の写像で置き換えた。
設計は [設計書](../design/coreimage-preset-retune.md) §2〜§4、実装は `Adjustments.swift: HighlightRecoveryMapping`（合成は `Develop.swift: develop()`）。

| hr | ΔEV | Δlift | 根拠 |
|---|---|---|---|
| 0 | 0 | 0 | 恒等 |
| 60 | −0.7 | 40 | C1'（診断レポート §5.1、10/10） |
| 90 | −1.0 | 55 | C1' 強め（9/10） |
| 100 | −1.1 | 60 | 外挿（#81 の空 10 カットで hr 90 とほぼ同じ指標: 白飛び 1.8% / 2.2%） |

| 項目 | 決定 |
|---|---|
| 合成 | **加算**: `exposure_eff = exposure + ΔEV`、`lift_eff = clamp(shadow_lift + Δlift, 0, 100)`。max / 置換は不感帯を作るので採らない |
| 置き場所 | CLI（`wb_temp` の mired 写像と同じく CI 固有の解釈は CLI に集約）。Rust は値域へのクランプだけ |
| 応答 | `adjustments_applied.highlight_recovery = {value, exposure_delta, shadow_lift_delta, effective_exposure, effective_shadow_lift, shadow_lift_clamped}`。`ignored` は空配列（フィールドは残す） |
| 等価性 | `{0, 0, hr 60}` ≡ `{−0.7, 40, hr 0}`、`{0, 0, hr 90}` ≡ `{−1.0, 55, hr 0}`（`applied`・`post.shadow_lift`・出力のバイト列が一致。Swift のスモークとハーネス `--presets` で確認） |
| プリセット | 非救済 8 プリセットは hr 0、hr > 0 は `highlight_rescue` / `backlit_rescue` / `sunset_glow` だけ（契約 §5 の CI 値） |
| rawpy | 変えない（hr は `HighlightMode` の選択のまま。プリセットも旧値） |
| キャッシュ | `CACHE_KEY_SCHEMA` は上げない（鍵に CLI 版と実行ファイルの SHA が入っている） |
