# ブラケット合成（Exposure Fusion）の Core Image 化 — 実装設計書（#83 / #84）

- ステータス: 案（2026-10-22）として書いた設計の記録。§7.1 の推奨どおり実装・統合済み（[合成モード報告](../reports/coreimage-merge-mode-report.md)、[Rust 統合報告](../reports/coreimage-bracket-rust-report.md)）。本文は設計時点のまま残す
- 対象 Issue: #83（Swift CLI の合成モード）、#84（Rust `merge_bracket` コマンドとフロントの切り替え）
- 関連: [4 機能の移行設計](coreimage-feature-migration.md) §3.3（案 B1 を採る）、[単一アプリの段階的実装プラン](single-app-implementation-plan.md) §4 M2、[ADR 0003](../adr/0003-python-removal-single-app.md) §3.1・§6 決定 5（GPS は継承する）、Issue #8（黒帯）、#38（メモリ）、#5（枚数の境界）
- 参照実装: `local_raw/bracket.py`（Python は廃止済み。読むだけ）。基準値: `tests/fixtures/baseline/bracket.json`・`bracket-*.png`（合成画像 3 組。実写 3 組は保留）
- 表記: 【確認】はコードを読んで確かめた事実（ファイルと行）。【スパイク】は `work/spike-83/`（gitignore 済み。コミットしない）で OpenCV 5.0.0 を動かして確かめた事実。【推測】は見立て。

## 0. 要点

1. **合成の核（MTB 位置合わせ・ラプラシアンピラミッド・Mertens 重み）は Swift の CPU コード（Float32、Accelerate）で自前実装**し、Core Image は「各フレームの RAW 現像（`CIRAWFilter`）」と「合成結果への後段チェーン（#73 の `PostChain`）・JPEG 書き出し」にだけ使う。
   理由は 2 つ: (a) OpenCV の数値規約（5 タップ核・reflect-101 境界・ピラミッド段数・pivot）に合わせないと基準値（OpenCV 出力）と比較できない、(b) 重みの `+1e-12` と「重み和がほぼ 0 の画素」（基準値の合成画像で約 3%）は half float（CI の作業形式 RGBAh。最小正規化数 6e-5、非正規化でも 6e-8）で潰れて NaN になる【スパイク】。
2. **OpenCV の既定は well-exposedness（露出適正）の重みを使っていない**（`createMergeMertens()` = contrast 1, saturation 1, **exposure 0**）【スパイク】。現行の Python 出力も凍結した基準 PNG もこの重みで作られている。移行設計 §B1（`coreimage-feature-migration.md:270` の「σ 0.2」）は原論文の記述で、基準とは一致しない。**既定は基準と同じ (1, 1, 0) にする**（§7 判断 1）。
3. **AlignMTB の基準フレームは先頭ではなく中央（`n/2`）**【スパイク】。`bracket.py:291` の「基準フレームは `src[0]`」と Issue #83 本文は事実と違う。基準 PNG も中央 pivot で作られている。EXIF 継承元・ファイル名先頭は「ソート済み先頭 ID」のままで、位置合わせの基準とは別の規則。
4. #8 の黒帯は「シフトぶんをゼロ埋めする OpenCV の挙動」そのもの【スパイク】。Swift では**全フレーム共通の有効領域でクロップ**して出す（§1.4）。
5. CLI は `mode: "merge"` を 1 リクエストで受ける（単発でも `--batch` の 1 行でも同じ）。Rust は既存の `develop()` と同じ単発起動を使い、タイムアウトだけ別に持つ。CLI は EXIF を書かず、Rust が先頭フレームのペア JPG から `exif_inherit` で継承する（GPS IFD はバイトコピーで残る）。
6. 検証は 3 層: Swift 単体（基準 PNG と PSNR ≥ 50 dB 目標 / 45 dB 下限）、Rust 単体（検証・キー・出力先・export）、実写（CLI の sRGB tiff16 を OpenCV で合成した基準と PSNR ≥ 40 dB + 目視）。実写ブラケットの RAW は手元に無い（§5.3、§7 判断 6）。

## 1. 合成アルゴリズムの実装方式

### 1.1 現行（Python / OpenCV）の規約【確認・スパイク】

| 項目 | 現行 | 根拠 |
|---|---|---|
| 入力 | sRGB ガンマ後の 16bit → float32 0..1 → ×255 で OpenCV へ | `bracket.py:335-352 _merge_frames`、`:287-333 _align_frames` |
| 枚数 | 2〜9。重複拒否、昇順ソート | `bracket.py:113-114`、`_validate_capture_ids` |
| 寸法不一致 | 拒否（向き混在） | `bracket.py:257 _require_uniform_shapes` |
| 位置合わせ | `cv2.createAlignMTB(max_bits)`。`max_bits = clamp(floor(log2(max(2, 短辺×0.02))), 1, 6)` → 原寸（短辺 4000）で 6（±63 px）、プレビュー 1600 で 5（±31 px） | `bracket.py:141 ALIGN_MAX_SHIFT_RATIO`、`:272 _align_max_bits` |
| AlignMTB の中身 | 既定 `exclude_range 4`、`cut true`。**基準フレーム = `n/2`**（2 枚なら 2 枚目、3 枚なら 2 枚目、9 枚なら 5 枚目）。中央値は 256 ビンのヒストグラムで取るので入力は 0..255 スケール | 【スパイク】`mertens_check.py check_align`: `process()` で無変化のフレームは n=2,3→index 1、n=4→2、n=9→4。`getCut()` は True だが Python の in-place 呼び出しでは出力形状は変わらず、シフト量ぶんの行・列がゼロ（黒帯）になった |
| 位置合わせの失敗 | 例外・形状不一致・全面黒のときだけ `aligned=False` で無補正続行 | `bracket.py:287-333` |
| 合成 | `cv2.createMergeMertens()` 既定 = **(contrast 1, saturation 1, exposure 0)**。出力は 0..1 規約で範囲外あり → 有限性確認 → クリップ | 【スパイク】`getExposureWeight() == 0.0`。`(1,1,0)` 明示と既定の出力は PSNR 146 dB〜一致（inf）。`(1,1,1)` とは 23 dB しか一致しない。`bracket.py:352` |
| 後段 | 合成 → resize(max_edge) → shadow_lift → brightness/contrast/saturation/USM（1 回だけ）→ 8bit | `bracket.py` 冒頭 docstring 処理順 5〜6 |
| `exposure` / `highlight_recovery` | 合成では no-op | docstring「Adjustments の扱い」 |
| EXIF | ソート済み先頭フレームのペア JPG から継承。GPS IFD はそのまま残る | `bracket.py` 処理順 7、ADR 0003 §3.1 |
| 出力先 | 本合成 `outputs/merged/`、プレビュー `outputs/merged_preview/`（ディレクトリで分ける） | `bracket.py` の `MERGED_PREVIEW_ROOT` コメント |

OpenCV の `MergeMertens::process` の数値規約（numpy 再実装で確かめたもの。OpenCV のソースは `ghq` に無く、`work/spike-83/` の cv2 5.0.0 の挙動から確定した）【スパイク】:

- 画像は `/255` して 0..1。グレーは `COLOR_RGB2GRAY`（0.299 R + 0.587 G + 0.114 B。RGB 順のまま）。
- contrast = |Laplacian 3×3（中心 −4、四近傍 +1）|、境界 reflect-101。saturation = `sqrt(Σ_c (c − mean)²)`（÷3 の有無は正規化で消えるので無関係）。well-exposedness = `Π_c exp(−(c − 0.5)² / 0.08)`（σ 0.2）だが**既定では指数 0 で効かない**。
- `weight = contrast^wcon · saturation^wsat · wellexp^wexp + 1e-12`、画素ごとに全フレームの和で正規化。
- ピラミッド段数 `maxlevel = floor(log2(min(w, h)))`（96 → 6、4000 → 11）。ガウスピラミッドは `pyrDown`（5 タップ `[1 4 6 4 1]/16`、reflect-101、偶数位置を間引き）。ラプラシアンは `img_pyr[l] −= pyrUp(img_pyr[l+1], size of l)`。重みはガウスピラミッド。各段で `Σ_frames lap · weight` を累積し、最上段から `pyrUp` して足し戻す。
- 一致度: numpy 再実装 vs cv2 既定 = 基準 3 組で 55.6〜62.5 dB、合成 1500×1000 で 77 dB（偶数寸法）。残差は**重み和が ≈1e-10 の画素**（Laplacian も彩度も 0 のフラット部。基準画像 9216 画素のうち 296 画素）で、1e-12 のタイブレークが float 誤差で揺れるため。実写ではノイズがあり事実上起きない。
- 奇数寸法の `pyrUp(dstsize)` は自前の零挿入実装と一致しなかった（601×403 で 51.8 dB、偶数では 1e-7 で一致）。**要確認**: OpenCV の奇数 `dstsize` の位相規則。実装 PR で cv2 のオラクル配列（小さな奇数寸法）を spike venv から生成して Swift のテストフィクスチャにする（§5.1）。基準 PNG（96 → 48 → 24 → 12 → 6 → 3）は奇数段 3×3 を含むので、そこでも検証できる。

### 1.2 選択肢と採用

| 案 | 内容 | 判定 |
|---|---|---|
| (a) Core Image だけで組む（`CIGaussianBlur` / `CILanczosScaleTransform` / 自前 `CIKernel`） | 移行設計 §B1 の当初案 | **不採用**。`CIGaussianBlur`・Lanczos は 5 タップ核と一致しない。作業形式 RGBAh では `1e-12` の重みが 0 になり、重み和 0 の画素で NaN。基準との PSNR 比較が成立しない |
| (b) Vision `VNTranslationalImageRegistrationRequest` で位置合わせ | 移行設計 §B1 の代替 | **不採用**（少なくとも初版では）。露出差のある画像での挙動が非公開で、OpenCV のシフトと比較できない。MTB は数十行で書ける |
| (c) **Swift CPU（Float32、Accelerate `vDSP` / `vImage`）で MTB と Mertens を自前実装**。各フレームは `CIRAWFilter` で現像し sRGB 8/16bit ではなく **Float32 の sRGB ガンマ値**として取り出す。合成結果を `CIImage` に戻して `PostChain.apply`（`PostChain.swift:54`）→ JPEG | **採用**。OpenCV の規約を 1 対 1 で写せ、GPU 非依存で `swift test` が決定的。後段・書き出しは #73 / N-A の実装を共有 |
| (d) Rust 側で合成（移行設計 B2） | — | 不採用（後段の二重実装、tiff16 中間 1.3 GB。移行設計の結論どおり） |

`vImage` の畳み込み（`vImageConvolve_PlanarF`）の境界フラグは複製（`kvImageEdgeExtend`）で reflect-101 が無い。5 タップ分離核は自前のループ（`simd` / `vDSP_vsmul` + `vDSP_vadd`）で書き、境界 2 px だけ reflect-101 のインデックスで埋める方が確実【推測】。Laplacian 3×3 も同様。計算量は原寸 1 フレームあたり数 GMAC で、M3 で 1 フレーム 1 秒前後【推測。実装 PR で計測】。

### 1.3 パラメータの合わせ方（確定値）

| 項目 | 値 | 由来 |
|---|---|---|
| 重み指数 (wcon, wsat, wexp) | **(1, 1, 0)** 既定。リクエストで (1, 1, 1) も選べる | 【スパイク】。§7 判断 1 |
| well-exposedness の σ | 0.2（`/0.08`）。wexp > 0 のときだけ使う | 【スパイク】(1,1,1) で numpy と cv2 が 56 dB 一致 |
| 重みの ε | 1e-12（Float32） | 【スパイク】 |
| ピラミッド段数 | `floor(log2(min(w, h)))` | 【スパイク】 |
| 核・境界 | 5 タップ `[1 4 6 4 1]/16`、reflect-101。`pyrUp` は零挿入 + 同核 ×4 | 【スパイク】偶数で 1e-7 一致。奇数 `dstsize` は要確認（§1.1） |
| グレー係数 | 0.299 / 0.587 / 0.114（RGB 順） | 【スパイク】BGR 順にすると数 dB 落ちる |
| MTB `max_bits` | `clamp(floor(log2(max(2, 短辺 × 0.02))), 1, 6)` | `bracket.py:272` |
| MTB 段数 | `min(floor(log2(max(w, h))) − 1, max_bits − 1)`、各段 ±1 の 9 近傍探索、`exclude_range 4`（中央値 ±4 を除外ビットマップ） | OpenCV の既定（`getExcludeRange() == 4`【スパイク】。段数の式は OpenCV の実装からの記憶で**要確認**。基準 PNG の `mtb_shifts_xy` は 96×96 で `max_bits 1` → 1 段 ±1 の値になっており整合する） |
| 基準フレーム | **中央 `n/2`**（OpenCV と同じ。§7 判断 2） | 【スパイク】 |
| 出力のクリップ | 有限性確認 → 0..1 クリップ | `bracket.py:335` |

### 1.4 黒帯（#8）の扱い

- 全フレームのシフト `(dx_i, dy_i)` から共通有効領域 `x ∈ [max(0, max dx), w + min(0, min dx))`、`y` も同様を求め、**全フレームを同じ矩形でクロップしてから合成**する（OpenCV の `cut=true` が意図していた動作。Python 経路ではクロップが効いていなかった【スパイク】）。
- 出力寸法は原寸より最大 2 × 63 px 小さくなる。応答に `crop` 矩形と `shifts_xy` を載せ、レシピに残す。
- シフト 0 ならクロップ無しで原寸のまま（三脚撮影の既定の形）。
- 代替の「縁を複製／反射で埋める」は寸法を保つが縁に捏造画素が入るので採らない（§7 判断 3）。
- 探索上限ガード: いずれかの `|shift|` が `短辺 × ALIGN_MAX_SHIFT_RATIO` を超えたら誤検出とみなし、**位置合わせ無し（`aligned: false`, `reason: "shift_exceeds_ratio"`）で続行**する。OpenCV にはこのガードは無く挙動変更になる（`ALIGN_MAX_SHIFT_RATIO` の本来の意図「誤検出の被害を有界にする」を満たす。§7 判断 4）。

### 1.5 プレビューと本合成の処理順

- 本合成: 原寸デコード（`scaleFactor 1`）→ MTB → クロップ → Mertens → 後段 → JPEG。
- プレビュー: `max_edge` 縮小デコード（`scaleFactor < 1`、現行アプリの `PREVIEW_MAX_EDGE 2400`。`coreimage.rs:53`）→ 同じ流れ。**Python（原寸で合成 → resize）とは順序が逆**になるが、#38 のコメント「プレビューは `max_edge` の縮小デコードにする」どおりで、プレビュー限定の差として受け入れる。MTB の探索幅は縮小後の短辺で決まる（1600 で ±31 px）。
- `exposure` / `highlight_recovery` はデコード段で **0 に固定**（`HighlightRecoveryMapping.resolve` の ΔEV・Δlift も 0 になる）。`Adjustments.ignoredKeys`（`Adjustments.swift:46`。現在は空）に `["exposure", "highlight_recovery"]` を載せて応答 `adjustments_applied.ignored` に出す。`wb_temp` / `noise_reduction` / `sharpness < 1` のデコード段写像と `settings` の個別上書きは全フレーム同一に掛ける。
- `noise_reduction` の `light`（= apple_default 既定）はフレームごとの filter から読み戻すので、Auto ISO のブラケットではフレーム間で NR 量が違いうる（現行 FBDD と同じ既知の制約。移行設計 §3.3）。
- 後段の USM の ISO は**ソート済み先頭フレーム**の EXIF（Python と同じ）。長辺は合成結果（クロップ後）の長辺。

## 2. CLI の I/F（CLI 0.7.0）

### 2.1 リクエスト

既存の単発リクエスト（`Request.swift:75 DevelopRequest`）と同じ 1 個の JSON。`mode: "merge"` のとき `input` の代わりに `inputs` を取る。

```json
{
  "mode": "merge",
  "inputs": ["/abs/DSC04510.ARW", "/abs/DSC04513.ARW", "/abs/DSC04514.ARW"],
  "output": "/abs/app-data/coreimage/merged/DSC04510-merged3-neutral_recovery-<hash10>.jpg",
  "max_edge": 2400,
  "format": "jpeg",
  "jpeg_quality": 0.92,
  "decoder_version": "9",
  "settings": { "profile": "apple_default", "lens_correction": true },
  "adjustments": { "exposure": 0, "wb_temp": 0, "shadow_lift": 15, "highlight_recovery": 60, "brightness": 1,
                   "contrast": 1.03, "saturation": 1.02, "sharpness": 1.04, "noise_reduction": "light" },
  "merge": { "align": true, "max_shift_ratio": 0.02, "edge": "crop", "weights": { "contrast": 1, "saturation": 1, "exposure": 0 } }
}
```

規則（違反は `INVALID_REQUEST`）:

- `inputs` は絶対パスの `.ARW` 2〜9 本。重複（`standardizedFileURL` 一致）は拒否。順序は呼び出し側（Rust）がソート済みで渡し、CLI は並べ替えない。
- `max_edge` は省略可。省略 = 原寸（本合成）。指定 = プレビュー（`RequestParser.maxEdgeRange` と同じ値域）。`mode: "merge"` では `preview`/`full` の区別は `max_edge` の有無だけ。
- `adjustments` は 9 キー全量必須（既存規則）。`exposure` / `highlight_recovery` は受けるが無視し、`ignored` に記録する。
- `merge` は省略可。省略時は上の既定値。`edge` は `"crop"` | `"keep"`（keep = ゼロ埋めのまま。基準との比較・調査用）。`weights` は各 0..3。
- `format` は `jpeg` のみ（`tiff16` は後続で必要なら足す）。`diagnostics` は merge では不可。
- 全フレームのデコード後 extent（向き適用後）が一致しなければ `INVALID_REQUEST`（`"frames differ in size or orientation"`）。

既存 `PathValidator`（`Request.swift`）を `inputs` 全件に回す。

### 2.2 レスポンス

```json
{
  "ok": true, "engine": "coreimage", "cli_version": "0.7.0", "macos_version": "27.0.1", "decoder_version": "9",
  "mode": "merge", "format": "jpeg", "inputs": ["...", "...", "..."], "output": "/abs/....jpg",
  "width": 5874, "height": 3916,
  "elapsed_ms": { "decode": 7210.4, "align": 820.1, "fuse": 2410.7, "post_encode": 1090.3, "total": 11531.5 },
  "merge": {
    "algorithm": "mtb+mertens", "merge_version": 1,
    "aligned": true, "reference_index": 1, "shifts_xy": [[-2, 1], [0, 0], [1, -1]],
    "max_bits": 6, "max_shift_px": 80,
    "crop": { "x": 1, "y": 1, "width": 5997, "height": 3998 },
    "pyramid_levels": 11, "weights": { "contrast": 1, "saturation": 1, "exposure": 0 },
    "edge": "crop"
  },
  "frames": [
    { "input": "...", "width": 6000, "height": 4000, "applied": { "...": "既存 Applied と同じ" }, "source": { "camera_model": "ILCE-6400", "lens_model": "...", "iso": 100, "native_width": 6000, "native_height": 4000, "orientation": 1 } }
  ],
  "adjustments_applied": { "defaults": {}, "post": {}, "highlight_recovery": {}, "ignored": ["exposure", "highlight_recovery"], "guards": [] },
  "sequence": 1
}
```

- `aligned: false` のとき `merge.reason` に `"shift_exceeds_ratio"` | `"align_failed"` を載せ、`shifts_xy` は検出値のまま残す（何が起きたか分かるように）。
- `frames[i].applied` は既存 `Applied`（`Develop.swift:17`）。`adjustments_applied.defaults` は先頭フレームの読み戻し値。
- 失敗応答は既存 `FailureResponse` と同じ（`error.code`）。

### 2.3 単発か batch か

- **単発（`run_sidecar`、`coreimage.rs:430`）で呼ぶ**。N フレームは 1 リクエストに入るので複数行を流す必要が無い。`--batch` でも 1 行として同じ JSON を受けられるようにする（`BatchProcessor.handle` は `RequestParser.parse` → `develop` の経路を `mode` で分岐するだけ）。
- `BatchSession`（`coreimage.rs:614-`）は現状テスト（`:2417`）からしか使われていない【確認】。本件では使わない。
- タイムアウト: 既存 `DEVELOP_TIMEOUT` 60 秒（`coreimage.rs:46`）は不足する。README の計測（`native/coreimage-cli/README.md:434-438`）では full 1 件 2.4 秒なので 9 枚のデコードだけで約 22 秒、融合・後段を足して 30 秒前後【推測】。Rust に `MERGE_TIMEOUT_FULL = 240 s`、`MERGE_TIMEOUT_PREVIEW = 120 s` を別に持つ。

### 2.4 デコード設定

Rust の `base_request`（`coreimage.rs:971`）と同じ固定値: `profile apple_default`、`lens_correction true`、`decoder_version "9"`、`format jpeg`。レンズ補正は CI 内蔵でフレームごとに同じ補正が掛かる前提（移行設計 §3.3「幾何一致は #70 で確認」。実写検証 §5.2 に観点として残す）。

### 2.5 CLI 内部の流れとメモリ（#38）

```
for each input (autoreleasepool):
  CIRAWFilter → decoderVersion → scaleFactor → profile → adjustments(decode 段, exposure/hr=0) → overrides → 白点ガード
  outputImage → extent 一致を確認 → context.render(RGBAf, sRGB, 0..1 clamp) → Float32 RGB バッファ（sRGB ガンマ値）
align: gray(0..255) → MTB → shifts → crop 矩形 → 全フレームをクロップ（ビュー）
fuse pass 1: 各フレームの weight(contrast·sat[·wellexp]) + ε を計算し weight_sum を累積（weight 自体は捨てる）
fuse pass 2: 各フレームで weight を再計算 → /weight_sum → ラプラシアン・重みピラミッド → res_pyr に加算 → 一時領域を解放
reconstruct → 有限性確認 → 0..1 クリップ → CIImage(bitmapData, RGBAf, colorSpace sRGB) → PostChain.apply → createCGImage(RGBA8, sRGB) → AtomicImageWriter（既存）
```

- 2 パス（weight_sum を先に作り、第 2 パスで重みを再計算）にするとフレーム以外の常駐は `weight_sum` 1 枚で済む。再計算は Laplacian と彩度だけなので安い。
- フレームの保持形式: Float32 RGB（12 byte/px）か UInt16 RGB（6 byte/px）。UInt16 なら Python と同じ精度（16bit sRGB）で半分の容量。**UInt16 RGB を推奨**（第 2 パスで Float32 に展開して使う）。
- 見積もり【推測。実装 PR で `/usr/bin/time -l` の maximum resident set size と peak footprint を計測し、README に残す。#38 の完了条件】:

| 構成 | 1 フレーム (UInt16 RGB) | N フレーム保持 | 常駐（weight_sum + res_pyr Float32 RGB ×4/3） | 作業（Float32 フレーム + 重み + 両ピラミッド） | CI デコードの footprint（1 件、逐次） | 合計の目安 |
|---|---|---|---|---|---|---|
| 原寸 6000×4000、N=3 | 144 MB | 0.43 GB | 0.10 + 0.38 GB | 0.29 + 0.10 + 0.38 + 0.13 ≈ 0.9 GB | 約 1.0 GB（README `:445-446`: full 1 件 RSS 276 MiB / footprint 1.01 GiB） | **≈ 2.8 GB** |
| 原寸、N=5 | 144 MB | 0.72 GB | 0.48 GB | 0.9 GB | 1.0 GB | **≈ 3.1 GB** |
| 原寸、N=9 | 144 MB | 1.30 GB | 0.48 GB | 0.9 GB | 1.0 GB | **≈ 3.7 GB** |
| プレビュー 2400×1600、N=9 | 23 MB | 0.21 GB | 0.08 GB | 0.15 GB | 約 0.3 GB | **≈ 0.7 GB** |

  CI のデコード footprint は 1 フレームずつ `autoreleasepool` で解放するので N に比例しない（README の 9 件バッチで RSS が 343 MiB にしか伸びなかった実測と整合）。フレーム保持を Float32 にすると N=9 原寸で +1.3 GB。
- 時間の目安【推測】: 原寸 9 枚でデコード 22 秒 + 融合 5〜10 秒。プレビュー 9 枚で 10 秒前後。

## 3. Rust 側（#84）

新規 `src-tauri/src/bracket.rs`（`main.rs:6-14` の `mod` 一覧と `invoke_handler`（`main.rs:21`）に追加）。`coreimage.rs` の非公開関数のうち使い回すもの（`run_sidecar`、`parse_response`、`cli_error`、`cli_version`、`canonical_json`、`prepare_output_dirs` 系、`prune_cache`、`cleanup_stale_temps`、`write_provenance`、`DEVELOP_SLOT`、`resolve_arw`、`read_source_root_cached`、`sidecar_path`）は `pub(crate)` に開ける。

### 3.1 コマンド

```rust
#[tauri::command]
pub async fn merge_bracket(app: AppHandle, capture_ids: Vec<String>, preset: String,
                           adjustments: Value, preview: bool, force: bool) -> Result<Value, CoreImageError>
```

- 戻り値は契約 `MergedResult`（`src/lib/types.ts:585`、`docs/API_CONTRACT.md:208`）そのもの。`output_url` は `full_developed_result`（`coreimage.rs:1802`）と同じ asset URL の組み立てで Rust が付ける（`params_hash` も同じく先頭 10 桁）。
- `adjustments` はフロントが解決した 9 キー全量（`develop_full_coreimage` と同じ）。Rust は `normalize_adjustments`（`adjustments.rs:79`）で最終検証。`preset` は `presets::get_presets()` で存在確認（`UNKNOWN_PRESET`）。
- `capture_ids` の検証（`cargo test`）: 2〜9（#5 D2: 9 枚受理・10 枚拒否・1 枚拒否の境界）、`is_valid_capture_id`（`coreimage.rs:236`）、重複拒否、**昇順ソート**（`BTreeSet`）。ソート済み先頭 ID が基準名・EXIF 継承元。全 ID を `resolve_arw` で同じ `source_root` 直下に解決。
- `max_edge` は受けない（プレビューは `PREVIEW_MAX_EDGE` 固定。Python の `max_edge` 引数は落とす。契約型の `max_edge?` は optional なので互換は保てる。§7 判断 5）。

### 3.2 出力先とレシピ

- `$APPDATA/coreimage/merged/`（本合成）と `$APPDATA/coreimage/merged_preview/`（プレビュー）。`prepare_output_dirs`（`coreimage.rs:1398`）を「サブディレクトリ名を取る形」に一般化して共用する。asset scope は `$APPDATA/coreimage/**`（`src-tauri/tauri.conf.json:23-28`）で既にサブディレクトリを含むので **`tauri.conf.json` の変更は不要**【確認】。
- ファイル名: `{先頭ID}-merged{N}-{preset}-{hash10}.jpg` と `.json`（契約 §4。`export.rs:117 developed()` の `{id}_` / `{id}-` 接頭辞規則とも整合）。
- レシピ（来歴）JSON = CLI 応答に Rust が足す: `request`（実際に送った JSON）、`kind: "exposure_fusion"`、`capture_ids`（ソート済み）、`preset: {name, label}`、`adjustments`（正規化後）、`preview`、`source_root`、`exif_inherited`、`params_hash`、`engine: {name: "coreimage mtb+mertens", cli_version, macos_version, merge_version}`。`frames[]` は CLI 応答のもの（`applied` / `source` を含む）。`lens_correction` は `full_developed_result` と同じ要約（`applied` は全フレームの `lens_correction_enabled` が true のとき true、混在なら `{applied:false, reason:"inconsistent_across_frames"}`）。

### 3.3 キャッシュキー

`cache_key`（`coreimage.rs:1036`）と同じ思想で別関数 `merge_cache_key`:

```
schema: 1
request: CLI リクエスト全量（output を除く。inputs はソート済み canonicalize 絶対パス、settings、正規化 adjustments、max_edge の有無、merge オプション）
preset: name
cli: --version 応答全文、cli_binary_sha256
inputs: [{size, mtime_ns}] を inputs と同順
rust_exif: {schema, source_sha256: 先頭フレームのペア JPG の sha256 | null, software}（本合成のみ。develop() と同じ）
```

SHA-256 先頭 16 桁。**位置合わせの結果（shifts / aligned）は含めない**（作業前にファイル名を決めるため。`bracket.py` の `ALIGN_VERSION` の考え方。版は `merge` オプションと CLI 版で代替）。ヒット判定は `load_cached`（`coreimage.rs:1434`）と同じ「画像と来歴が通常ファイルで来歴の `ok` が true」。`force: true` ならキャッシュを無視して再合成。上限管理は `prune_cache`（`coreimage.rs:1211`）を merged ディレクトリにも掛ける（原寸合成は 1 件 10 MB 前後なので既存上限 600 件 / 1.5 GB で足りる【推測】）。

### 3.4 EXIF 継承

- CLI は EXIF を書かない（README「出力」）。本合成のみ、`develop()` の `inherit_output_exif`（`coreimage.rs:1597`）と同じ手順で、**ソート済み先頭フレーム**の `{id}.JPG`（同じフォルダ）を読み、`exif_inherit::inherit_exif(developed, source, out_w, out_h, EXIF_SOFTWARE)`（`exif_inherit.rs:366`）を呼ぶ。寸法は**クロップ後の出力寸法**（応答の `width` / `height`）。
- `exif_inherit` は APP1 をバイトコピーし Orientation 1・寸法・Software・MakerNote 参照だけを書き換える（`exif_inherit.rs:1-2`）ので、**GPS IFD はそのまま継承される**（ADR 0003 §6 決定 5。#84 本文の「GPS は書かない」は撤回済み）。
- 失敗（ペア JPG 無し・解析失敗）は `exif_inherited: false` で続行（Python と同じ）。プレビュー合成は継承しない（書き出し対象外）。

### 3.5 `export.rs` の `merged` variant

- `ExportItem` に `merged_filename: Option<String>`（`export.rs:21`）。`match item.variant`（`:223-235`）に `"merged"` を追加。
- 新関数 `merged(data, root, id, filename)`（`developed()` `:117` を雛形に）: 対象は `coreimage/merged/` だけ（`merged_preview/` にあれば「プレビュー品質の画像は書き出せません。」）、ファイル名は `{id}-merged{N}-` で始まり `.jpg`、レシピの `kind == "exposure_fusion"`、`preview == false`、`request.max_edge` 無し、`capture_ids[0] == id`、`request.inputs` の全件が `root` 直下、`request.output` がファイル実体と一致。manifest の `recipe` にレシピ全体を入れ、`source.arw` は先頭フレーム（Python と同じ。全フレームはレシピ内の `capture_ids` / `frames` で辿れる）。
- フロントの `runExport`（`src/App.tsx:456-462`）は既に `variant: "merged"` と `merged_filename` を送る【確認】。

### 3.6 同時実行

`DEVELOP_SLOT`（1 本 + 待ち 2 件）を共用する。合成中に現像を連打すると `BUSY` になるのは現行と同じ振る舞い（フロントは `busy` でボタンを止める）。

## 4. フロント: 隠した UI を戻す

PR #126（`d95b1c1`、マージ `e850e87`）で描画だけを外し、state・reducer・コンポーネントは残っている【確認】。戻す手順:

1. `src/lib/coreimage-api.ts` に `mergeBracketCoreImage(captureIds, adjustments, preset, mode): Promise<MergedResult>` → `invoke("merge_bracket", { captureIds, preset: preset.name, adjustments, preview: mode === "preview", force: false })`。Rust が契約形で返すので変換は不要。
2. `src/App.tsx`: `git show e850e87^2 -- src/App.tsx` の削除行から `runMerge`（差分 428〜470 行付近）、`mergeFrames` / `mergeBlock` / `mergePreviewResult` / `mergeFullResult` / `mergeBusy` / `groupMergeMode`（343〜356）、`mergeElapsed`（542〜543）、busy 判定の `state.mergeMode`（322〜326、396、559）、`OperationPanel` への props（605〜630）を戻す。`mergeBracket` の HTTP 呼び出しは `mergeBracketCoreImage` に置き換え、エラーは `coreImageErrorMessage`。現在のプレースホルダは `App.tsx:448-450`。
3. `src/components/OperationPanel.tsx`: 同じ差分の 64〜97（props 型）、136〜168（`mergedResult` の選択ロジック）、184〜204（`viewerGalleryFor`）、213〜258（合成タイル）、267〜284（ブラケットの案内）、306〜316（`<BracketMerge>`。差し込み位置は現在の `OperationPanel.tsx:311` のコメント）を戻す。
4. `src/components/BracketMerge.tsx`、`app-state.ts` の `merge/*` reducer（`:820-879`）、`mergeBlockReason`（`:1226`）、`groupMergeResultOf`、`mergedResultFor`（`:1030`）、`isMergedAccepted`（`:1093`）、`ExportPanel` の merged 行はそのまま使える【確認】。CSS の `.shot-merged`（`globals.css:289`）・`.bracket-hint`（`:594`）・`.bracket-*` は残っている【確認】（PR #126 が消した CSS は Drive / セットアップ画面のみ）。
5. `BracketMerge.tsx` の説明文「合成プレビューでも数秒、本合成は数十秒」は妥当のまま。`hasPreview` / `hasFull` の判定は従来どおり。
6. テスト: `app-state.test.ts` で PR #126 が消したのは health / Drive 関連で merge のテストは残っている【確認】。`coreimage-api` の呼び出し引数を検査する vitest を 1 本足す。`lens_correction.reason` の語彙（#10 から引き継ぎ）: Rust が出す `"coreimage_unsupported"` / `"inconsistent_across_frames"` が `LENS_REASON_LABELS` にあることを確かめる vitest を置く。

## 5. 検証

### 5.1 基準値（合成 3 組）との比較 — Swift 単体（`swift test`、ARW 不要）

- `tests/fixtures/baseline/bracket-{0,1,2}-{0,1,2}.png` を ImageIO で読み（96×96 RGB8）、`/255` → MTB（`max_bits 1`、中央 pivot、クロップ無し＝`np.roll` 入力なので黒帯は出ない）→ Mertens (1,1,0) → 0..1 クリップ → `bracket-{i}-merged.png` と 8bit RGB で PSNR。
- 合格: **PSNR ≥ 50 dB（目標）、≥ 45 dB（下限）**、`bracket.json` の `float32_stats_rgb`（mean / stddev）と 1e-3 以内。numpy 再実装は float 基準に対し 54〜62 dB だった【スパイク】。残差は重み和 ≈ 0 の退化画素（約 3%）の float タイブレーク由来で、**アルゴリズムを変えて「直す」対象ではない**（§1.1）。
- `mtb_shifts_xy` との比較: フィクスチャの値は `calculateShift(gray[0], gray[i])`（先頭基準・1 段）なので、同じ呼び方の単体関数で一致を確かめる（`process()` の中央 pivot とは別）。
- 追加フィクスチャ（実装 PR で `work/spike-83/` の cv2 から生成し `native/coreimage-cli/Tests/Fixtures/` にコミット。画像は合成のみ）: `pyrDown` / `pyrUp` の奇数寸法オラクル（例 7×5、13×9）、Laplacian の境界、MTB の `shiftMat`、黒帯クロップ（`np.roll` ではなくゼロ埋めシフトで作った 3 枚 → クロップ矩形と出力寸法）。
- 枚数の境界（1 / 2 / 9 / 10）、寸法不一致の拒否、`exposure` / `hr` の無視、`weights` の値域、`edge: keep` でゼロ埋め黒帯が残ることのテスト。
- 退化画素の扱い: 重み和が 0 に近い画素は OpenCV と同じく `1e-12` のタイブレークに任せる（NaN を出さないことだけ検査）。

### 5.2 実写の目視確認

1. 手持ち AEB 5 枚と三脚長秒 3 枚の 2 セット（Issue #83 の完了条件）を `work/raw-bracket/`（gitignore）に置く。
2. 基準の生成（rawpy 不要）: CLI を `mode: full, format: tiff16, output_color_space: srgb, adjustments 恒等`（`exposure 0, hr 0`）で各フレームに掛け、`work/spike-83/` と同じ venv（`opencv-python-headless`）で `AlignMTB(6)` + `MergeMertens(1,1,0)` を回し PNG 保存。これを `scripts/bracket_reference.py`（開発者用・cv2 依存。`requirements-dev.txt`）として置く。
3. CLI `mode: merge`（`edge: keep` で寸法を揃える）の出力と、後段を恒等にした条件で PSNR ≥ 40 dB（Issue #83）、等倍クロップでゴースト・黒帯・ハロー・四隅のレンズ補正不一致（放射状のズレ）が無いこと。`shifts_xy` が基準の `calculateShift` と ±1 px で一致。
4. `.app` で: ブラケットグループを選ぶ → 合成プレビュー → 本合成 → 採用 → 書き出し → 書き出し JPEG の EXIF に `DateTimeOriginal` / `ISO` / `LensModel` / **GPS IFD**（元 JPG に GPS があるとき）が残り `MakerNote` が無く `Orientation = 1`、寸法がクロップ後の値。確認手段は `single-app-implementation-plan.md` §6.3 と同じ。
5. 9 枚原寸の `/usr/bin/time -l`（RSS・footprint・壁時計）を README「合成モード」に記録（#38）。

### 5.3 実写の基準が保留になっている件

- 凍結された実写基準は無い（`bracket.json: status "synthetic_only"`）。手元の 11 枚（`work/raw/DSC074xx`）のグループは `single` / `similar` のみで `exposure_bracket` が無い【確認: `tests/fixtures/baseline/analysis.json` の `kind`】。
- Python 環境は廃止済みだが、§5.2 の手順 2 は **rawpy を使わない**（CLI tiff16 → cv2）ので、ブラケットの RAW が用意できれば基準は作れる。作った基準 PNG は実写なので**コミットしない**（`work/` に置き、sha256 と PSNR だけを PR 本文と README に記録）。
- RAW が用意できるまでは §5.1（合成）と Swift / Rust の単体で PR をマージし、**実写確認を #83 のクローズ条件として残す**（§7 判断 6）。

## 6. PR の切り方と担当

CLI の契約（§2）をこの設計書で凍結し、PR-A と PR-C / PR-D を並列にする。1 PR 1〜3 日。

| PR | 内容 | 担当 / worktree | 依存 | 完了条件 |
|---|---|---|---|---|
| **PR-A** `feat/83-merge-core` | Swift: `MergeCore`（Float32 の MTB・ピラミッド・Mertens・クロップ）を `CoreImageDevelop` に純関数で追加。`Tests/Fixtures/` のオラクル生成と単体テスト | Swift 担当 1 | 無し | `swift test` 緑。基準 PNG 3 組 PSNR ≥ 50 dB。奇数寸法 pyrUp・MTB・クロップのテスト。CI/ImageIO 非依存 |
| **PR-B** `feat/83-merge-mode` | Swift: `mode: "merge"` のパース（`RequestParser`）、`develop()` から分離した `merge()`（N フレーム現像 → MergeCore → `PostChain.apply` → 書き出し）、`--batch` 対応、応答 `merge` / `frames`、README「合成モード」、CLI 0.7.0、`ignoredKeys`。実 ARW のスモーク（skip 可） | Swift 担当 1（PR-A の続き） | PR-A | `swift test` 緑。`mode: merge` 単発と `--batch` 1 行の応答がバイト一致。`exposure` / `hr` 無視。寸法不一致で `INVALID_REQUEST`。9 枚原寸の時間・RSS を README に記録（#38） |
| **PR-C** `feat/84-merge-bracket-rust` | Rust: `bracket.rs`（検証・ソート・キー・出力先・`run_sidecar`・EXIF 継承・レシピ・`MergedResult`）、`coreimage.rs` の共有関数を `pub(crate)` に、`main.rs` 登録、`cargo test`（capture_ids 境界 #5 D2・キー安定性・出力先分離・モック CLI での応答処理） | Rust 担当 | §2 の契約のみ（モック CLI でテスト。実 CLI は PR-B 後に結合） | `cargo test` 緑。モック CLI で merged / merged_preview に出力・来歴ができる。EXIF 継承テスト（GPS IFD の保持は `exif_inherit` の既存テストに委ねる） |
| **PR-D** `feat/84-export-merged` | Rust `export.rs` の `merged` variant と拒否テスト（preview ディレクトリ・接頭辞・`capture_ids[0]` 不一致・root 外） | Rust 担当（PR-C と別 worktree なら並列可。衝突は `ExportItem` のみ） | レシピ形式（§3.2） | `cargo test` 緑。manifest に `variant: "merged"` と `recipe.kind` |
| **PR-E** `feat/84-merge-ui` | フロント: §4 の復元、`coreimage-api.ts`、vitest（呼び出し引数・`LENS_REASON_LABELS`）。`npm test` / `tsc` / `lint` / `build` | フロント担当 | PR-C のコマンド名・戻り値（契約は `MergedResult` で既定） | ビルド緑。`tauri dev` で合成パネル・合成タイル・採用・書き出し行が出る（実合成は PR-B/C 結合後） |
| **PR-F** `docs/83-merge-verification` | 結合確認（`.app` で合成 → 採用 → 書き出し）、実写基準スクリプト `scripts/bracket_reference.py`、ADR 短文「Mertens を自前実装。OpenCV と完全一致はしない。既定重み (1,1,0)」、`coreimage-feature-migration.md:262-270` の σ 0.2 / 先頭基準 / GPS の記述修正、#83 / #84 本文の修正 | メイン | 全 PR | §5.2 の手順 3〜5。実写 RAW が無ければ #83 を「実写確認待ち」で残す |

並列: PR-A ‖ PR-C ‖ PR-E（PR-E は型だけで進められる）。PR-B は PR-A の後、PR-D は PR-C と同時か直後。各 worktree では `npm ci` / `swift build` / `cargo build` を先に済ませる。コミット・PR 作成はメインの責務（`orchestrator-qa`）。

## 7. リスク・未確認事項・オーナー判断

### 7.1 オーナーに判断してもらう事項（推奨を太字）

| # | 事項 | 選択肢 | 推奨と理由 |
|---|---|---|---|
| 1 | Mertens の重み | **(1, 1, 0) = OpenCV 既定・現行出力・基準 PNG と同じ** / (1, 1, 1) = 原論文（露出適正を使う） | **(1,1,0)**。基準との PSNR 比較が成立し、現行の見え方を保つ。(1,1,1) は見え方が変わり（基準に対し 23 dB）、基準 PNG の再生成（spike venv で可能）と実写での再評価が要る。リクエストの `weights` で後から切り替えられるようにしておく |
| 2 | 位置合わせの基準フレーム | **中央 `n/2`（OpenCV・基準 PNG と同じ）** / ソート済み先頭（Issue #83 本文・`bracket.py` 注記の記述） | **中央**。基準 PNG と一致させるため。先頭にする場合は基準 PNG の再生成と Issue 本文の修正が要る。EXIF・ファイル名の基準は先頭のまま（別の規則） |
| 3 | 黒帯（#8） | **共通領域でクロップ（寸法が最大 2×63 px 縮む）** / 縁を複製・反射で埋める（寸法を保つ） | **クロップ**。捏造画素を出さない。`edge: keep` を調査用に残す |
| 4 | シフト上限ガード | **上限超過は位置合わせ無しで続行（`aligned:false, reason`）** / OpenCV どおり無条件に採用 | **ガードあり**。`ALIGN_MAX_SHIFT_RATIO` の意図（誤検出の被害を有界に）に沿う。挙動変更なので明記 |
| 5 | プレビュー合成の `max_edge` | **2400（アプリの現像プレビューと同じ）** / 1600（Python の `PREVIEW_MAX_EDGE`） | **2400**。BeforeAfter で現像プレビューと同じ寸法で比べられる。メモリは 9 枚で 0.7 GB 程度 |
| 6 | 実写ブラケットの RAW | 手持ち AEB 5 枚・三脚長秒 3 枚を用意してもらう / 用意できるまで合成基準だけで進める | **用意を依頼**。無ければ PR-A〜E は単体で進め、#83 のクローズを実写確認待ちにする |
| 7 | フレーム保持形式 | **UInt16 RGB（Python と同精度、N=9 原寸で 1.3 GB）** / Float32 RGB（+1.3 GB） | **UInt16** |

### 7.2 要確認（実装 PR の中で確かめる）

- OpenCV `pyrUp` の奇数 `dstsize` の位相規則（§1.1。偶数は一致）。cv2 のオラクルで Swift を合わせる。
- `AlignMTB::calculateShift` の段数式 `min(floor(log2(max(w,h))) − 1, max_bits − 1)` と `exclude_range` の使い方（排他ビットマップ `|v − median| > 4`）。基準 PNG の `mtb_shifts_xy` と、`work/spike-83/` の cv2 の `calculateShift` をオラクルにする。
- `CIRAWFilter` の `extent` がフレーム間で 1 px 違う可能性（向き・レンズ補正）。違えば `INVALID_REQUEST` ではなく共通矩形へのクロップで吸収するかを実装時に決める【推測】。
- CI レンズ補正の幾何がフレーム間で一致すること（移行設計 §3.3 の #70 観点）。§5.2 の四隅確認で見る。
- 9 枚原寸の実測メモリ・時間（§2.5 は見積もり）。
- `PostChain.apply` に `CIImage(bitmapData:)` の sRGB タグ付き画像を渡したときの色変換（sRGB → extended linear → `CILinearToSRGBToneCurve` → カーネル → linear → sRGB レンダ）の往復誤差。単発現像と同じ RGBAh 経路なので 1.5/255 程度【推測。PostChainTests の既存値】。

### 7.3 リスク

- **精度**: 合成の核を half float に置くと破綻する（§0-1）。CPU Float32 を守る。
- **メモリ**: 原寸 9 枚で 3〜4 GB【推測】。足りなければフレーム保持を 8bit に落とす（Mertens は 8bit 入力でも成立。精度は落ちる）か、上限枚数を下げる。測ってから決める。
- **時間**: 原寸 9 枚で 30 秒前後【推測】。`BracketMerge.tsx` の文言「本合成は数十秒」の範囲。タイムアウトは §2.3。
- **基準の限界**: 合成 3 組は 96×96 で、クロップ・原寸ピラミッド段数・実写のノイズ特性を含まない。実写確認（§5.2）が無いまま閉じると、見え方の差（NR 由来のフレーム間差、レンズ補正の幾何）を見逃す。
- **契約の食い違い**: Issue #83 本文（先頭基準、σ 0.2 の露出適正）と移行設計 §B1 は本設計と異なる。PR-F で直す。`docs/API_CONTRACT.md` は N-DOC（M4）で Tauri コマンド契約に書き直す予定なので、本件では `MergedResult` の形を変えないことだけ守る。
