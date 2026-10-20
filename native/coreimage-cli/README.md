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

スモークテストは `/Volumes/Backup/100MSDCF/DSC04430.ARW`（横位置）/ `DSC04553.ARW`（縦位置, Orientation=8）が
読めるときだけ走り、無ければ skip される。テストの出力は一時ディレクトリに書き、入力 ARW には書き込まない。
ビルド成果物 `.build/` は `.gitignore` 済み。

## 使い方

```sh
B=.build/release/geoframe-ci-develop

$B --version
# {"cli_version":"0.1.0","engine":"coreimage","macos_version":"27.0.1","ok":true}

$B --capabilities                                   # OS が α6400 をサポートするか
$B --capabilities /Volumes/Backup/100MSDCF/DSC04430.ARW   # ＋そのファイルで使える decoderVersion

echo '{"input":"/Volumes/Backup/100MSDCF/DSC04430.ARW","output":"/tmp/ci-poc/DSC04430.jpg","mode":"preview","decoder_version":"9"}' | $B
```

`--capabilities` の応答例:

```json
{"camera_model_names":["Sony Alpha ILCE-6400","Sony Alpha ILCE-6400A"],"cli_version":"0.1.0","engine":"coreimage","ilce6400_supported":true,"macos_version":"27.0.1","ok":true,"output_formats":{"jpeg":"8-bit JPEG, sRGB IEC61966-2.1 ICC embedded","tiff16":"16-bit RGB TIFF, Display P3 ICC embedded"},"probe_input":"/Volumes/Backup/100MSDCF/DSC04430.ARW","profiles":["neutral","apple_default"],"request_decoder_versions":["8","9"],"supported_decoder_versions":["8","9"]}
```

`supportedDecoderVersions` は CIRAWFilter のファイル単位 API なので、probe ファイルを渡したときだけ
`supported_decoder_versions` が埋まる（渡さなければキー自体が出ない）。

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
| `decoder_version` | `"8"` \| `"9"` | | そのファイルの最新 | **アプリからは明示指定を推奨**（省略値は OS 更新で変わりうる） |
| `settings.exposure_ev` | number -5..5 | | 0 | `CIRAWFilter.exposure`（EV、baselineExposure に加算） |
| `settings.neutral_temperature` | number 2000..50000 \| null | | null（= 撮影時 WB） | `neutralTemperature`（K） |
| `settings.neutral_tint` | number -150..150 \| null | | null | `neutralTint` |
| `settings.lens_correction` | boolean | | false | `isLensCorrectionEnabled`。CIRAWFilter 既定は ON だが、CLI では明示時のみ ON |
| `settings.profile` | `"neutral"` \| `"apple_default"` | | `"neutral"` | 下記 |

`null` を渡したキーは省略と同じ扱い。

### profile

- `neutral`（既定）: sharpness / detail / luminance・color NR / moire / contrast / localToneMap / EDR を 0。
  表示用トーンカーブ（boostAmount 1.0、boostShadowAmount 0.9、baselineExposure ファイル値、shadowBias 3.0）は
  既定のまま。根拠は ADR §4。
- `apple_default`: CIRAWFilter 既定値のまま（NR・シャープは ISO と decoderVersion で変わる）。
  `lens_correction` だけは settings に従う。

draft mode は常に OFF。

## レスポンス（stdout に JSON 1 行）

stdout には JSON 1 行だけを書く。ログは stderr。キーはソート済み。

### 成功（終了コード 0）

```json
{"applied":{"baseline_exposure":0.4,"bit_depth":8,"boost_amount":1,"boost_shadow_amount":0.9,"color_noise_reduction_amount":0,"color_space":"sRGB IEC61966-2.1","contrast_amount":0,"decoder_version":"9","detail_amount":0,"draft_mode_enabled":false,"exposure":0,"extended_dynamic_range_amount":0,"gamut_mapping_enabled":true,"lens_correction_enabled":false,"lens_correction_supported":true,"local_tone_map_amount":0,"local_tone_map_supported":false,"luminance_noise_reduction_amount":0,"moire_reduction_amount":0,"neutral_temperature":6414.951,"neutral_tint":9.99706,"orientation":8,"orientation_handling":"CIRAWFilter applies the source EXIF orientation (8) to outputImage; pixels are written upright and the output Orientation tag is 1 (no second rotation)","profile":"neutral","scale_factor":0.4,"shadow_bias":3,"sharpness_amount":0,"working_color_space":"extended linear sRGB (RGBAh)"},"cli_version":"0.1.0","decoder_version":"9","elapsed_ms":{"decode":350.7,"render_encode":891.8,"total":1242.5},"engine":"coreimage","format":"jpeg","height":2400,"input":"/Volumes/Backup/100MSDCF/DSC04553.ARW","macos_version":"27.0.1","mode":"preview","ok":true,"output":"/tmp/ci-poc/out/DSC04553_preview_neutral.jpg","source":{"camera_model":"ILCE-6400","iso":100,"lens_model":"10-18mm F2.8 DC DN | Contemporary 023","native_height":4000,"native_width":6000,"orientation":8},"width":1600}
```

- `applied`: 全パラメータを設定し終えた後に **filter から読み戻した値**（Float は最短表記に丸め）。
  `decoderVersion` を後から設定すると他の値が既定に戻るため、CLI は decoderVersion を最初に設定する。
- `width` / `height`: 出力画像（向き適用後）。`source.native_width/height` は回転前のセンサー寸法。
- `elapsed_ms.decode`: フィルタ生成〜`outputImage` 取得。Core Image は遅延評価なので、実際の
  デモザイクは `render_encode`（レンダリング＋エンコード＋rename）に含まれる。

### 失敗（終了コード 1、引数エラーのみ 2）

```json
{"error":{"code":"INPUT_NOT_FOUND","message":"input does not exist: /tmp/none.ARW"},"ok":false}
```

| code | 条件 |
|---|---|
| `INVALID_REQUEST` | JSON でない / オブジェクトでない / 未知キー / 型・値域違反 / 相対パス / 入力が `.ARW` でない / 出力拡張子が format と不一致 / `max_edge` を full で指定 / `jpeg_quality` を tiff16 で指定 / input と output が同一実体（同一パス・シンボリックリンク・ハードリンク） / input や output がディレクトリ / 未知のコマンドライン引数（終了コード 2） |
| `INPUT_NOT_FOUND` | input が存在しない |
| `INPUT_NOT_READABLE` | input が読めない（権限） |
| `OUTPUT_DIR_NOT_FOUND` | output の親ディレクトリが無い |
| `UNSUPPORTED_RAW` | CIRAWFilter が開けない・寸法が取れない（壊れた／非 RAW ファイル） |
| `UNSUPPORTED_DECODER_VERSION` | 指定 `decoder_version` がそのファイルで使えない |
| `RENDER_FAILED` | `outputImage` が無い / レンダリング失敗 |
| `WRITE_FAILED` | エンコード・一時ファイル書き込み・rename の失敗 |
| `INTERNAL_ERROR` | 想定外の例外 |

## 出力

| format | 画素 | 色空間 / ICC | メタデータ |
|---|---|---|---|
| `jpeg` | 8bit RGB | sRGB IEC61966-2.1。ICC（3144 byte）を APP2 に埋め込み | Software、Orientation=1、ImageIO 既定の最小 EXIF |
| `tiff16` | 16bit/ch RGB（アルファ無し） | Display P3 ICC 埋め込み | Software、Orientation=1 |

- 作業空間は extended linear sRGB（half float）。
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
