# geoframe-ci-develop — Core Image RAW 現像 PoC CLI

Sony α6400 の ARW を macOS 標準の `CIRAWFilter`（Core Image RAW）で現像し、JPEG / 16bit TIFF に書き出す
単体 CLI。現行の rawpy エンジン（`local_raw/engine.py`）との比較材料を作るための PoC（Issue #65）。
設計判断の根拠は [`docs/adr/0001-coreimage-raw-poc.md`](../../docs/adr/0001-coreimage-raw-poc.md)、
現行 Adjustments との対応は [`docs/coreimage-parameter-mapping.md`](../../docs/coreimage-parameter-mapping.md)。

## 必要環境

- macOS 13 以上（検証は macOS 27.0.1 / Apple M3）
- Xcode（Swift 5.9 以上。検証は Swift 6.4。言語モードは Swift 5）

## ビルド・テスト

```sh
cd native/coreimage-cli
swift build -c release          # → .build/release/geoframe-ci-develop
swift test                      # 単体テスト + スモークテスト
```

後段の Metal カーネル（`Kernels/GeoFrameKernels.metal`）は SwiftPM ではビルドしない。リポジトリルートで
`scripts/build-coreimage-kernels.sh` を実行すると `xcrun metal -fcikernel` → `xcrun metallib -cikernel` で metallib を作り、
base64 にして `Sources/CoreImageDevelop/Generated/KernelLibrary.swift` に埋め込む（生成物はコミット対象）。
再生成は生成物が `.metal` より古いとき（生成物に記録した `.metal` の SHA-256 が現在と違うとき）だけで、最新なら
Metal toolchain が無い機でも何もせず成功する。`npm run build:coreimage` は先にこのスクリプトを呼ぶ。
`.metal` を変えたのに生成し忘れると `PostChainTests.testEmbeddedLibraryMatchesKernelSource` が落ちる。
toolchain が無い機で再生成が要るときは `xcodebuild -downloadComponent MetalToolchain` で入れる。

スモークテストは `/Volumes/Backup/100MSDCF/DSC04430.ARW`（横位置）/ `DSC04553.ARW`（縦位置, Orientation=8）が
読めるときだけ走り、無ければ skip される。個別上書き（下記）のスモークは、上記か星空サンプル
（`~/works/60-misc/40-raw-assistant/work/raw/DSC07415.arw`）か環境変数 `GEOFRAME_CI_SMOKE_ARW` の ARW のうち
最初に読めるものを使う。診断モード（0.4.0）のスモークのうち線形読み出し・`baseline_exposure`・`linear_space_filter` の確認は
明るい空のカット `/Volumes/Backup/100MSDCF/DSC04705.ARW`（または環境変数 `GEOFRAME_CI_DIAG_ARW`）が読めるときだけ走る。
テストの出力は一時ディレクトリに書き、入力 ARW には書き込まない。
ビルド成果物 `.build/` は `.gitignore` 済み。

## 使い方

```sh
B=.build/release/geoframe-ci-develop

$B --version
# {"cli_version":"0.4.0","engine":"coreimage","macos_version":"27.0.1","ok":true}

$B --capabilities                                   # OS が α6400 をサポートするか
$B --capabilities /Volumes/Backup/100MSDCF/DSC04430.ARW   # ＋そのファイルで使える decoderVersion

echo '{"input":"/Volumes/Backup/100MSDCF/DSC04430.ARW","output":"/tmp/ci-poc/DSC04430.jpg","mode":"preview","decoder_version":"9"}' | $B
```

`--capabilities` の応答例:

```json
{"adjustments":{"ignored":["highlight_recovery"],"post_kernel":"metal","supported":true},"camera_model_names":["Sony Alpha ILCE-6400","Sony Alpha ILCE-6400A"],"cli_version":"0.4.0","diagnostics":{"post_filters":["CIHighlightShadowAdjust","CIToneCurve","CIExposureAdjust","CIColorClamp"],"supported":true},"engine":"coreimage","ilce6400_supported":true,"macos_version":"27.0.1","ok":true,"output_formats":{"jpeg":"8-bit JPEG, sRGB IEC61966-2.1 ICC embedded","tiff16":"16-bit RGB TIFF, Display P3 ICC embedded (default) or sRGB with output_color_space \"srgb\""},"probe_input":"/Volumes/Backup/100MSDCF/DSC04430.ARW","profiles":["neutral","apple_default"],"request_decoder_versions":["8","9"],"supported_decoder_versions":["8","9"]}
```

`supportedDecoderVersions` は CIRAWFilter のファイル単位 API なので、probe ファイルを渡したときだけ
`supported_decoder_versions` が埋まる（渡さなければキー自体が出ない）。
`diagnostics` は下記「診断モード」（0.4.0）の対応状況と `post_filters` の許可リスト。

## アプリからの使い方（Issue #65 PR2、#72/#73 で既定に）

デスクトップアプリ（Tauri）は Python エンジン / `.venv` / localhost HTTP を経由せず、Rust
（`src-tauri/src/coreimage.rs`）からこの CLI を子プロセスとして直接呼ぶ。**プレビュー現像の既定の経路**
（#72/#73）で、「Core Image プレビュー」のバッジが出る。書き出し・原寸（本現像）は当面 rawpy のまま。

### ビルド順序

CLI は Tauri の `bundle.externalBin`（`src-tauri/binaries/geoframe-ci-develop-<target triple>`）として
同梱する。tauri-build はこのファイルの存在を **コンパイル時に** 検査するため、
`cargo build` / `cargo test` / `cargo clippy` を素で回す前に一度ビルドしておくこと。

```sh
npm run build:coreimage     # swift build -c release → src-tauri/binaries/ へコピー（.gitignore 済み）
cd src-tauri && cargo test  # ここで初めて通る
```

`npm run tauri:dev` / `tauri:build` は beforeDevCommand / beforeBuildCommand で
`npm run build:coreimage` を先に実行するので、追加の手順は要らない。

### 切り戻しフラグ

```sh
GEOFRAME_PREVIEW_ENGINE=rawpy npm run tauri:dev
open -n --env GEOFRAME_PREVIEW_ENGINE=rawpy "src-tauri/target/release/bundle/macos/GeoFrame RAW Assist.app"
```

値がちょうど `rawpy` のときだけ Python（rawpy）プレビューに切り戻す（Rust の `preview_engine()` が返し、フロントは起動時に 1 回読む）。
未設定・それ以外は Core Image。旧 `GEOFRAME_COREIMAGE_EXPERIMENT` は廃止した。ブラウザモード（`npm run dev:raw`）は Python プレビューのまま。
CLI の起動失敗などインフラ側のエラー時は、フロントが自動で Python プレビューにフォールバックする。

### 動作

- 現像元フォルダは Python と同じ `<app_data_dir>/config.json` の `source_root` を **読むだけ**
  （`<app_data_dir>` は macOS で `~/Library/Application Support/com.geoframe.rawassist`）。
- capture_id → ARW の解決は `local_raw/source_files.py` と同じ規則（拡張子の大文字小文字揺れ、
  正規表記 `.ARW` 優先、フォルダ外を指すシンボリックリンクは拒否）。
- リクエストは `mode: "preview"`, `max_edge: 2400`, `format: "jpeg"`, `decoder_version: "9"`,
  `settings: {profile: "apple_default", lens_correction: true}` 固定で、`adjustments` に解決済みの 9 キー全量を渡す
  （Rust `src-tauri/src/adjustments.rs` がクランプ・正規化）。
- 出力先は `<app_data_dir>/coreimage/<撮影ID>_<mode>_<ハッシュ16桁>.jpg`。ハッシュはリクエスト
  （出力パス以外）+ `--version` 応答 + ARW のパス・サイズ・mtime の SHA-256。同名ファイルと
  来歴 `<同名>.json`（CLI の応答 JSON そのまま）が揃っていれば再現像しない。
- sidecar は実行ファイルの隣（dev は `src-tauri/target/debug/`、.app は `Contents/MacOS/`）の
  `geoframe-ci-develop` を `std::process::Command` で起動し、60 秒で kill する。
- 表示は Tauri の asset プロトコル（scope `$APPDATA/coreimage/**`）。
- 元の ARW / JPG と、Python 側の現像キャッシュ・書き出し先には書き込まない。

## リクエスト（stdin に JSON 1 個）

パスはシェル・Rust 側のエスケープ問題を避けるため引数ではなく JSON で渡す。**未知のキーはエラー**。

| キー | 型 | 必須 | 既定 | 説明 |
|---|---|---|---|---|
| `input` | string | ✓ | — | 絶対パス。拡張子 `.ARW`（大文字小文字不問）。読むだけ |
| `output` | string | ✓ | — | 絶対パス。`jpeg` は `.jpg/.jpeg`、`tiff16` は `.tif/.tiff`。親ディレクトリが存在すること。既存なら置き換え |
| `mode` | `"preview"` \| `"full"` | ✓ | — | 処理は同一。差は `scaleFactor` だけ |
| `max_edge` | integer 64..20000 | | 2400 | preview のみ（full で指定するとエラー）。`scaleFactor = min(1, max_edge / 長辺)` |
| `format` | `"jpeg"` \| `"tiff16"` | | `"jpeg"` | |
| `jpeg_quality` | number 0..1 | | 0.92 | jpeg のみ（tiff16 で指定するとエラー） |
| `output_color_space` | `"srgb"` \| `"display_p3"` | | `"display_p3"` | tiff16 のみ（jpeg は常に sRGB なので指定するとエラー）。既定は互換のため P3（Issue #79） |
| `decoder_version` | `"8"` \| `"9"` | | そのファイルの最新 | **アプリからは明示指定を推奨**（省略値は OS 更新で変わりうる） |
| `settings.exposure_ev` | number -5..5 | | 0 | `CIRAWFilter.exposure`（EV、baselineExposure に加算） |
| `settings.neutral_temperature` | number 2000..50000 \| null | | null（= 撮影時 WB） | `neutralTemperature`（K） |
| `settings.neutral_tint` | number -150..150 \| null | | null | `neutralTint` |
| `settings.lens_correction` | boolean | | false | `isLensCorrectionEnabled`。CIRAWFilter 既定は ON だが、CLI では明示時のみ ON |
| `settings.profile` | `"neutral"` \| `"apple_default"` | | `"neutral"` | 下記 |
| `settings.luminance_noise_reduction` | number 0..1 \| null | | null | `luminanceNoiseReductionAmount` の個別上書き（下記） |
| `settings.color_noise_reduction` | number 0..1 \| null | | null | `colorNoiseReductionAmount` |
| `settings.sharpness_amount` | number 0..1 \| null | | null | `sharpnessAmount` |
| `settings.detail_amount` | number 0..3 \| null | | null | `detailAmount` |
| `settings.extended_dynamic_range` | number 0..2 \| null | | null | `extendedDynamicRangeAmount` |
| `settings.boost_amount` | number 0..1 \| null | | null | `boostAmount`（0 でトーンカーブ無し＝リニア） |
| `settings.boost_shadow_amount` | number 0..2 \| null | | null | `boostShadowAmount`（`boost_amount` が 0 なら無効） |
| `settings.gamut_mapping` | boolean \| null | | null（= CIRAWFilter 既定 true） | `isGamutMappingEnabled` の上書き（0.4.0、**診断専用**。下記「診断モード」） |
| `settings.baseline_exposure` | number -5..5 \| null | | null（= ファイル値） | `baselineExposure` の上書き（0.4.0、**診断専用**） |
| `adjustments` | object \| null | | null | アプリのスライダー 9 キー（0.3.0、Issue #73）。下記「adjustments」。省略時は 0.2.0 と同じ処理 |
| `diagnostics` | object \| null | | null | 診断モード（0.4.0、Issue #92）。**本番（アプリ）では使わない**。下記「診断モード」。省略時は 0.3.0 と同じ処理 |

`null` を渡したキーは省略と同じ扱い。

### settings の個別上書き（Issue #79）

`luminance_noise_reduction` 以下の 7 キーは、**profile を適用した後に、指定されたものだけ**を `CIRAWFilter` へ書く。
`apple_default` では OS が ISO ごとに決めた既定値を、`neutral` では 0 をそれぞれ置き換える。null / 省略は「profile の
既定値のまま」。値域は `CIRAWFilter.h` のコメントどおりで、外れると `INVALID_REQUEST`。

- 用途: 星景で「`apple_default` のトーン・色はそのまま、輝度 NR だけ 0」にする（#70 / #77）、ハイライト評価で
  EDR / boost を掃引する（#71）。
- **`apple_default` の既定値は OS 更新で変わりうる**（例: macOS 27.0.1 / decoder 9 / ISO 3200 では
  sharpness 1.0・luminance NR 0.241・color NR 0・EDR 0・boost 1.0・boostShadow 0.9）。評価や比較では必ず応答の
  `applied` を記録し、「上書きしていない値が何だったか」を残すこと。
- `applied` には読み戻した値が載る。実測では `extended_dynamic_range` に 2 を超える値を書くと 2.0 に丸められる
  （CLI の値域検査は 0..2 なので通常は起きない）。

### profile

- `neutral`（既定）: sharpness / detail / luminance・color NR / moire / contrast / localToneMap / EDR を 0。
  表示用トーンカーブ（boostAmount 1.0、boostShadowAmount 0.9、baselineExposure ファイル値、shadowBias 3.0）は
  既定のまま。根拠は ADR §4。
- `apple_default`: CIRAWFilter 既定値のまま（NR・シャープは ISO と decoderVersion で変わる）。
  `lens_correction` だけは settings に従う。
- どちらの profile でも、上記の個別上書きキーを指定したものだけがその後に置き換わる。

draft mode は常に OFF。

### adjustments（CLI 0.3.0、Issue #73）

アプリの Adjustments（`docs/API_CONTRACT.md` §2）をそのまま渡すと、CLI が CIRAWFilter の値と後段チェーンへ写像する。
設計は `docs/design/coreimage-preview-adjustments.md` §3・§4（契約は §4.1 / §4.2）。

```json
"settings": {"profile": "apple_default", "lens_correction": true},
"adjustments": {"exposure": 0.0, "wb_temp": 0.0, "shadow_lift": 15.0, "highlight_recovery": 60.0, "brightness": 1.0,
                "contrast": 1.03, "saturation": 1.02, "sharpness": 1.04, "noise_reduction": "light"}
```

規則（違反はすべて `INVALID_REQUEST`）:

- 9 キーすべて必須（null も欠落扱い）。未知キーは拒否。値域は契約どおりで、**CLI はクランプしない**（クランプは Rust / Python の責務）。
  bool は数値として受けない。`noise_reduction` は `"off"` / `"light"` / `"full"`。
- `settings.profile` が `apple_default` のときだけ可（省略時の `neutral` も不可。写像が apple_default の既定値基準のため）。
- `settings.exposure_ev` / `neutral_temperature` / `neutral_tint` との同時指定は、**値に関わらず**不可（null は省略扱いで可）。
- 個別上書き 7 キー（`luminance_noise_reduction` 等）は同時指定可。adjustments の写像の**後**に適用される（明示が勝つ）。
- 優先順位: 白点ガード ＞ 明示の `settings.*` 上書き ＞ adjustments の写像 ＞ profile。

処理順（`Develop.swift: develop()`）: decoderVersion → scaleFactor → profile → **既定値の読み戻し**（応答 `adjustments_applied.defaults`）
→ adjustments のデコード段 → 個別上書き → **白点ガード** → `outputImage` → **後段チェーン** → レンダ。

| キー | 写像 | 段 |
|---|---|---|
| `exposure` | `CIRAWFilter.exposure`（EV、1:1） | デコード |
| `wb_temp` (v) | `K0` = 読み戻した撮影時 WB の色温度。`mired = 1e6/K0 − v × 0.85`、`K = clamp(1e6/mired, 2000, 50000)` → `neutralTemperature`。tint は触らない。v = 0 は書かない。係数 0.85 はハーネスで確認済み（rawpy の ±60 と同じ大きさ。比較レポート §16）。`K0` が有限でない・2000..50000 の外なら写像を飛ばし `adjustments_applied.guards` に `"wb_k0_out_of_range"` を記録する | デコード |
| `shadow_lift` (v) | `x + (v/100)·2.0·x·(1−x)²` → [0,1] クリップ（rawpy `_apply_shadow_lift_array`） | 後段 |
| `highlight_recovery` | **受理するが効かせない**（no-op、`adjustments_applied.ignored`。代替は #81） | — |
| `brightness` / `contrast` / `saturation` | `x·b` → `(x−0.5)·c+0.5` → Rec.709 輝度 Y 基準 `Y + (x−Y)·s`（rawpy `_apply_tone_array`。途中はクリップしない） | 後段 |
| `sharpness` (s) | s < 1: `sharpnessAmount = 既定 × s`。s = 1: 既定のまま。s > 1: 既定のまま + 後段の輝度 USM（増分だけ: `gain = min(1.2, baseGain × (s−1) × 1.5) × ISO 減衰`）。σ・baseGain・コアリング・ISO 減衰は rawpy `_capture_sharpen_params` の定数をそのまま移植（長辺は出力 extent の長辺、ISO は EXIF。不明ならコアリング 0.5/255・減衰なし） | デコード + 後段 |
| `noise_reduction` | off: 輝度・色 NR とも 0。light: apple_default の既定のまま。full: `輝度 = max(既定, min(0.5, max(既定 + 0.3, 0.3)))`（既定が 0.5 を超える高 ISO では既定のまま）、`色 = max(既定, 0.3)`（上限 0.5 はハーネスの輝度 NR 掃引で確定。比較レポート §16） | デコード |

#### noise_reduction の表（apple_default v9・macOS 27.0.1）

| ISO 帯 | light（= 既定）輝度 / 色 | off | full |
|---|---|---|---|
| < 800 | ≈0.01 / 0 | 0 / 0 | ≈0.31 / 0.30 |
| 800〜1600 | 未計測 | 0 / 0 | min(0.5, 既定 + 0.3) / 0.30 |
| 3200 | 0.241 / 0 | 0 / 0 | 0.5 / 0.30 |
| 6400 | 0.357 / 0 | 0 / 0 | 0.5 / 0.30 |

既定値は OS 更新で変わりうる。実際の値は応答の `adjustments_applied.defaults` と `applied` を見ること。

#### 白点ガード

decoder 9 は「輝度 NR 0 ∧ sharpness 0 ∧ scaleFactor < 1」のときだけ白点ノイズを出す。**adjustments 指定時に限り**、
個別上書きの後でこの組み合わせになっていたら `sharpnessAmount = 0.1` に床上げし、`adjustments_applied.guards` に
`"white_dot_sharpness_floor"` を記録する。通常のスライダー範囲では発動せず、`noise_reduction: "off"` +
`settings.sharpness_amount: 0` のような明示上書きでだけ発動する。adjustments 無しのリクエスト（評価用の neutral 等）には触れない。

#### 後段チェーン

`outputImage`（extended linear sRGB）→ `CIColorClamp(0..1)` → `CILinearToSRGBToneCurve` → Metal カーネル `geoframe_tone`
（lift → clip → brightness → contrast → saturation）→ [USM gain > 0] 輝度 Y を `CIColorMatrix` で RGB に複製 →
`clampedToExtent` → ガウスぼかし → `geoframe_usm`（`d = Y − Yblur` をコアリングして RGB に `d·gain` を足し [0,1] クリップ）、
[gain = 0] `CIColorClamp(0..1)` → `CISRGBToneCurveToLinear` → 既存と同じレンダ。

- ガウスぼかしは σ ≤ 約 1.06 で cv2.GaussianBlur と同じ離散核（`ksize = round(8σ+1) | 1`）を
  `CIConvolution9Horizontal` / `Vertical` で掛ける。`CIGaussianBlur` は inputRadius ≈ σ で階段エッジの幅は合うが、
  σ 0.6〜0.8 では高周波の応答が離散ガウスと違い、乱数パッチで最大 10/255 ずれたため（単体テストで確認）。それより大きい σ は `CIGaussianBlur`。
- 作業フォーマットは half float（RGBAh）。単体テストで numpy 式との差は tone 段 ≤ 1.5/255、USM 込み ≤ 3/255。
- 恒等の adjustments（exposure 0、wb_temp 0、shadow_lift 0、brightness・contrast・saturation・sharpness 1、light）は filter に何も書かず、
  後段もほぼ素通し（16bit sRGB TIFF で既定リクエストとの差 最大 0.23/255）。

### 診断モード（CLI 0.4.0、Issue #92）

**ハイライトの白飛びを段階別に切り分けるための観測専用の機能で、本番（アプリ・Tauri）からは使わない。**
比較ハーネス（`scripts/compare_engines.py --diagnosis`）が `work/` 配下へ書くためのもの。設計と根拠は
[`docs/design/coreimage-highlight-diagnosis.md`](../../docs/design/coreimage-highlight-diagnosis.md) §2・§4。

- `diagnostics` が無い（省略 / null）リクエストは、コード経路・出力のバイト列・応答のキー集合・`applied` が 0.3.0 と同一
  （0.3.0 の実バイナリと tiff16 / jpeg を突き合わせ、ソフトウェア名の版表記以外のバイト一致を確認済み）。
- 応答の `applied` のキー集合は変えない（回帰スクリプトがキー集合の差も数えるため）。診断の結果は応答のトップレベル `diagnostics` に載る。

#### settings の診断用キー

`settings.gamut_mapping`（`isGamutMappingEnabled`）と `settings.baseline_exposure`（`baselineExposure`）は、個別上書き（#79）と
同じ規則（省略 / null = 既定のまま、値域外は `INVALID_REQUEST`、profile・adjustments の写像の後に適用）。読み戻しは既存の
`applied.gamut_mapping_enabled` / `applied.baseline_exposure`。`adjustments` との同時指定も可。

- `gamut_mapping: false` + `boost_amount: 0` で、CI の線形段（S1）を表示白（1.0）で切らずに読み出せる。
  `gamut_mapping: false` だけならトーン後（S2）。既定（gamut ON）は 1.0 で clamp される（S3）。
- `baseline_exposure: 0` は `exposure_ev: −(ファイル値)` と同じ統計になる（冗長。スモークテストで確認）。
- **本番で使う予定は無い**（アプリのリクエストは常に省略）。

#### リクエスト `diagnostics`

```jsonc
"diagnostics": {
  "stats": true,                              // 全画面の float 統計（既定 true）
  "stats_color_space": "linear_srgb",         // "linear_srgb"（extended linear sRGB、既定）| "linear_p3"（extended linear Display P3）
  "dump_float": {"path": "/abs/work/x_s1.f32", "color_space": "linear_srgb"},   // 任意。clamp しない float 画像
  "linear_space_filter": {"kind": "scale", "value": 0.5},                        // 任意。identity | scale（0.01..8）| constant（0..8）
  "post_filters": [{"name": "CIExposureAdjust", "inputEV": -1.43}],              // 任意。許可リスト制
  "skip_write": false                         // true なら output に画像を書かない（output のパス検証は従来どおり）
}
```

規則（違反は `INVALID_REQUEST`）:

- 未知キーは拒否。`diagnostics: {}` は「統計だけ」の最小指定。
- `dump_float.path`: 絶対パス・拡張子 `.f32`・親ディレクトリが存在すること（無ければ `OUTPUT_DIR_NOT_FOUND`）。input / output と
  同一実体（シンボリックリンク・ハードリンク含む）は不可、既存ディレクトリも不可。output と同じく一時ファイル + `rename(2)` で書く。
- `linear_space_filter`: `CIRAWFilter.linearSpaceFilter` に `CIColorMatrix` を挿す。identity（×1）/ scale（×value）/ constant
  （RGB を value で置換。トーン応答の標本化用）。identity に `value` を付けるとエラー。`exposure` / `baselineExposure` はこの**前**に掛かる。
- `post_filters`（最大 16 個）: 下表の名前とパラメータだけ受ける。数値か配列（`[x, y]` / `[r, g, b, a]`）のみ。null は「CIFilter の既定のまま」。

  | name | パラメータ |
  |---|---|
  | `CIHighlightShadowAdjust` | `inputHighlightAmount` 0..1、`inputShadowAmount` −1..1、`inputRadius` 0..10 |
  | `CIToneCurve` | `inputPoint0`〜`inputPoint4`（各 `[x, y]`、x 0..1、y 0..2。指定しなかった点は既定値で補い、x が狭義単調増加であること）。入力は [0,1] に clamp されるので、1.0 超を扱うときは前に `CIExposureAdjust` を置く |
  | `CIExposureAdjust` | `inputEV` −5..5 |
  | `CIColorClamp` | `inputMinComponents` / `inputMaxComponents`（各 `[r, g, b, a]`、有限の数値） |

- 処理順は固定: `outputImage` → `post_filters` → `adjustments` の後段（先頭で [0,1] clamp）→ レンダ。`post_filters` は端を
  `clampedToExtent` で延長してから掛け、`outputImage` の extent に切り戻す（`inputRadius` で寸法が変わらない）。
- **`outputImage` は 1 回だけ参照する**。同じ filter で 2 回以上参照すると extent が半分ずつ縮む（macOS 27.0.1 で確認。
  `SmokeTests.testDoubleReferenceOfOutputImageShrinksExtent` がこの挙動を、`testReadbackExtentMatchesScaleFactor` が CLI が縮んでいないことを検査する）。

#### 応答 `diagnostics`（`diagnostics` 指定時だけ出る）

例（DSC04705、`max_edge` 1200、`boost_amount: 0` + `gamut_mapping: false`、`skip_write` + `dump_float`。数値は丸めて表示）:

```json
"diagnostics":{"cli_diagnostics_version":1,"dump_float":{"byte_order":"little","bytes":11520000,"channels":3,"color_space":"linear_srgb","dtype":"float32","height":800,"layout":"interleaved_rgb_row_major","path":"/abs/work/diag/s1.f32","width":1200},"readback":{"baseline_exposure":0.4,"extent":[0,0,1200,800],"gamut_mapping_enabled":false},"stats":{"all_gt_1":0.2473,"any_gt_1":0.3826,"b":{"frac_ge_1":0.3826,"frac_gt_1":0.3826,"frac_lt_0":0.0000073,"max":2.6997209,"mean":0.6774,"min":-0.022789825,"p50":0.045065697,"p99":2.3279364,"p999":2.5276783},"g":{...},"r":{...},"height":800,"width":1200},"stats_color_space":"linear_srgb","stats_source":"render_input","written":false}
```

- `stats`: レンダ直前の画像（`post_filters` / 後段を含む）を `stats_color_space` の extended linear 空間・RGBAf で全画面描画した
  チャンネル別統計。`adjustments` 指定時は後段の clamp の後の値になる（1.0 超は出ない）。`stats: false` ならキーごと出さない。
  - `mean` / `min` / `max` / `p50` / `p99` / `p999`（順位統計は近似せず厳密値。順位は `min(n−1, ⌊n·q⌋)`、p50 は `n/2` 番目）
  - `frac_gt_1`（> 1.0）/ `frac_ge_1`（≥ 1 − 1/65535 = 16bit で白）/ `frac_lt_0`（< 0）、`any_gt_1` / `all_gt_1`（いずれか / 3ch とも 1.0 超の画素）
  - CLI の統計は**全画面のみ**。ROI の統計は dump からハーネスが計算する。
- `dump_float`: RGB 3 チャンネル float32 little-endian・行優先・アルファ無し。Python は
  `np.fromfile(path, "<f4").reshape(height, width, 3)`。サイズは preview 2400px で約 46 MB、full で 288 MB。
  stats と同じ色空間なら描画は 1 回で共有する。
- `linear_space_filter`: `{kind, value, filter: "CIColorMatrix"}`（identity の `value` は null）。
- `post_filters`: `[{name, applied_parameters}]`。`applied_parameters` は許可リストのパラメータを filter から読み戻した値
  （指定しなかったものは CIFilter の既定値）。
- `readback`: `gamut_mapping_enabled` / `baseline_exposure`（filter の読み戻し）と `extent`（`outputImage` の `[x, y, w, h]`）。
- `written`: output に画像を書いたか（`skip_write: true` なら false。応答の `output` は要求パスのまま、`width` / `height` は extent から）。
- 統計 / dump の描画は通常のレンダと別に同じ画像をもう一度描くので、時間（`elapsed_ms.render_encode` に含まれる）は約 2 倍。
  full の float 描画はバッファ 384 MB（RGBAf）。

#### 例

```sh
# 線形読み出し（S1）の統計と float dump。画像は書かない（出力先は work/ 配下）
echo '{"input":"/Volumes/Backup/100MSDCF/DSC04705.ARW","output":"'$PWD'/work/diag/s1.jpg","mode":"preview","max_edge":1200,
  "decoder_version":"9","settings":{"profile":"apple_default","lens_correction":true,"boost_amount":0,"gamut_mapping":false},
  "diagnostics":{"skip_write":true,"dump_float":{"path":"'$PWD'/work/diag/s1.f32"}}}' | $B
```

## レスポンス（stdout に JSON 1 行）

stdout には JSON 1 行だけを書く。ログは stderr。キーはソート済み。

### 成功（終了コード 0）

```json
{"applied":{"baseline_exposure":0.4,"bit_depth":8,"boost_amount":1,"boost_shadow_amount":0.9,"color_noise_reduction_amount":0,"color_space":"sRGB IEC61966-2.1","contrast_amount":0,"decoder_version":"9","detail_amount":0,"draft_mode_enabled":false,"exposure":0,"extended_dynamic_range_amount":0,"gamut_mapping_enabled":true,"lens_correction_enabled":false,"lens_correction_supported":true,"local_tone_map_amount":0,"local_tone_map_supported":false,"luminance_noise_reduction_amount":0,"moire_reduction_amount":0,"neutral_temperature":6414.951,"neutral_tint":9.99706,"orientation":8,"orientation_handling":"CIRAWFilter applies the source EXIF orientation (8) to outputImage; pixels are written upright and the output Orientation tag is 1 (no second rotation)","profile":"neutral","scale_factor":0.4,"shadow_bias":3,"sharpness_amount":0,"working_color_space":"extended linear sRGB (RGBAh)"},"cli_version":"0.3.0","decoder_version":"9","elapsed_ms":{"decode":350.7,"render_encode":891.8,"total":1242.5},"engine":"coreimage","format":"jpeg","height":2400,"input":"/Volumes/Backup/100MSDCF/DSC04553.ARW","macos_version":"27.0.1","mode":"preview","ok":true,"output":"/tmp/ci-poc/out/DSC04553_preview_neutral.jpg","source":{"camera_model":"ILCE-6400","iso":100,"lens_model":"10-18mm F2.8 DC DN | Contemporary 023","native_height":4000,"native_width":6000,"orientation":8},"width":1600}
```

- `applied`: 全パラメータを設定し終えた後に **filter から読み戻した値**（Float は最短表記に丸め）。
  `decoderVersion` を後から設定すると他の値が既定に戻るため、CLI は decoderVersion を最初に設定する。
- `width` / `height`: 出力画像（向き適用後）。`source.native_width/height` は回転前のセンサー寸法。
- `adjustments_applied`: **`adjustments` を指定したときだけ**出る（省略時はキー自体が無く、応答は 0.2.0 と同じキー集合）。
  `applied` は adjustments の有無で変わらない形のまま（最終的に filter から読み戻した値）。
  - `defaults`: profile 適用直後・adjustments 適用前の読み戻し（`sharpness_amount` / `luminance_noise_reduction_amount` /
    `color_noise_reduction_amount` / `neutral_temperature` / `neutral_tint`）
  - `post`: 後段に実際に使った値。`shadow_lift`（0..1）/ `brightness` / `contrast` / `saturation` /
    `usm: {sigma, gain, coring, long_edge, iso}`（`iso` は不明なら null）/
    `wb: {k0, mired_shift, mired_per_unit}`（`mired_shift` はクランプ後の実効値 `1e6/K − 1e6/K0`。wb_temp > 0 で負）/ `noise_reduction`
  - `ignored`: 受理したが効かせなかったキー（`["highlight_recovery"]`）
  - `guards`: 発動したガード（例 `["white_dot_sharpness_floor"]`）

  例（DSC07415・ISO 3200、`wb_temp` 20・`sharpness` 1.3・`shadow_lift` 15）:

  ```json
  "adjustments_applied":{"defaults":{"color_noise_reduction_amount":0,"luminance_noise_reduction_amount":0.24097165,"neutral_temperature":4353.8257,"neutral_tint":8.204875,"sharpness_amount":1},"guards":[],"ignored":["highlight_recovery"],"post":{"brightness":1,"contrast":1.03,"noise_reduction":"light","saturation":1.02,"shadow_lift":0.15,"usm":{"coring":0.00392156862745098,"gain":0.12150000000000002,"iso":3200,"long_edge":2400,"sigma":0.64},"wb":{"k0":4353.8257,"mired_per_unit":0.85,"mired_shift":-17}}}
  ```
- `diagnostics`: **`diagnostics` を指定したときだけ**出る（上記「診断モード」）。省略時はキー自体が無い。
- `elapsed_ms.decode`: フィルタ生成〜`outputImage` 取得。Core Image は遅延評価なので、実際の
  デモザイクは `render_encode`（レンダリング＋エンコード＋rename）に含まれる。

### 失敗（終了コード 1、引数エラーのみ 2）

```json
{"error":{"code":"INPUT_NOT_FOUND","message":"input does not exist: /tmp/none.ARW"},"ok":false}
```

| code | 条件 |
|---|---|
| `INVALID_REQUEST` | JSON でない / オブジェクトでない / 未知キー / 型・値域違反 / `adjustments` の欠落キー・`apple_default` 以外との組み合わせ・`settings.exposure_ev` 等との同時指定 / `diagnostics` の未知キー・型・値域違反・許可リスト外の `post_filters`・`dump_float.path` が `.f32` でない / input と `dump_float.path` が同一実体 / 相対パス / 入力が `.ARW` でない / 出力拡張子が format と不一致 / `max_edge` を full で指定 / `jpeg_quality` を tiff16 で指定 / `output_color_space` を jpeg で指定 / input と output が同一実体（同一パス・シンボリックリンク・ハードリンク） / input や output がディレクトリ / 未知のコマンドライン引数（終了コード 2） |
| `INPUT_NOT_FOUND` | input が存在しない |
| `INPUT_NOT_READABLE` | input が読めない（権限） |
| `OUTPUT_DIR_NOT_FOUND` | output（または `diagnostics.dump_float.path`）の親ディレクトリが無い |
| `UNSUPPORTED_RAW` | CIRAWFilter が開けない・寸法が取れない（壊れた／非 RAW ファイル） |
| `UNSUPPORTED_DECODER_VERSION` | 指定 `decoder_version` がそのファイルで使えない |
| `RENDER_FAILED` | `outputImage` が無い / レンダリング失敗 |
| `WRITE_FAILED` | エンコード・一時ファイル書き込み・rename の失敗 |
| `INTERNAL_ERROR` | 想定外の例外 |

## 出力

| format | 画素 | 色空間 / ICC | メタデータ |
|---|---|---|---|
| `jpeg` | 8bit RGB | sRGB IEC61966-2.1。ICC（3144 byte）を APP2 に埋め込み | Software、Orientation=1、ImageIO 既定の最小 EXIF |
| `tiff16` | 16bit/ch RGB（アルファ無し） | 既定 Display P3 ICC 埋め込み。`output_color_space: "srgb"` で sRGB ICC | Software、Orientation=1 |

- 作業空間は extended linear sRGB（half float）。`adjustments` 指定時は後段チェーン（上記）を通してからレンダする。
- ImageIO は sRGB JPEG に ICC を書かない（EXIF ColorSpace=1 のみ）ため、CLI が Apple の sRGB プロファイルを
  APP2 `ICC_PROFILE` として挿入している。
- **向き**: CIRAWFilter が EXIF Orientation を適用済みの画像を返すので、画素は正立のまま書き、Orientation=1 とする
  （二重回転しない）。
- **GPS・撮影 EXIF は出力しない**（元 ARW に GPS があっても出力の GPS IFD は空）。

## 原本不変・原子的書き込み

- input は読むだけ。input と同一実体への出力は拒否する。
- 出力は **output と同じディレクトリ**の一時ファイル `.<output名>.<UUID>.tmp` に書き、`rename(2)` で置き換える。
  途中で失敗した場合は一時ファイルを削除し、既存の output は元のまま残る。
- 一時ファイルは output 側のディレクトリに作るので、入力ボリューム（例 `/Volumes/Backup`）には書き込まない
  （output を入力と同じディレクトリに指定した場合を除く）。

## 制限（PoC）

- 対象は ARW のみ（拡張子で判定）。
- 1 プロセス 1 リクエスト。並列実行時のメモリ使用量は未計測（full TIFF16 で 16bit バッファ約 192MB + CI 中間）。
- 未確認事項は ADR「未確認事項・後続」を参照。
