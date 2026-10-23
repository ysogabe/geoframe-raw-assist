# Core Image merge mode implementation report

## Scope

PR-B adds Swift CLI `mode: "merge"` on top of the PR-A `MergeCore` at base `8c861cd`. CLI version is bumped from 0.7.0 to 0.8.0. The request contract follows `docs/design/bracket-merge-coreimage.md` §§2, 5 and 6.1, with the adopted defaults: Mertens weights (1,1,0), central pivot, common crop, shift-limit fallback, 2400-edge preview when requested, and UInt16 RGB retained frames.

The CLI emits JPEG only and does not copy EXIF/GPS. `exposure` and `highlight_recovery` are forced to zero in every decoder stage and reported in `adjustments_applied.ignored`. `settings.exposure_ev` must be omitted or null; the existing adjustment contract rejects any non-null explicit setting, including zero, alongside `adjustments`. White balance, NR, and decoder sharpness are applied while decoding all frames. The remaining post adjustments run once on the fused image.

## Resource smoke

Machine: Apple M3, macOS 27.0.1, release CLI, decoder 9. Source: existing `work/raw/DSC07415.arw` (6000×4000) linked nine times under `/private/tmp`; source bytes and metadata were untouched. All nine frames are the same exposure, so this validates the resource path only and says nothing about bracket image quality.

| Measure | Result |
|---|---:|
| Wall clock (`/usr/bin/time -l`) | 57.63 s |
| CLI elapsed total | 57.35 s |
| Decode | 14.59 s |
| AlignMTB and crop | 16.55 s |
| Mertens fusion | 25.69 s |
| Post chain / encode / write | 0.32 s |
| Maximum resident set size | 3,521,740,800 bytes (3.28 GiB) |
| Peak memory footprint | 3,656,552,432 bytes (3.41 GiB) |
| Output | 5999×3999 JPEG, common-cropped after detected −1/−1 shifts |

This first-round smoke used the initial 17 bytes/pixel/frame estimate (3.67 GB for 9×24 MP). The final estimate below includes a fixed workspace and is 3.984 GB for the same inputs, within the 4 GB ceiling. The measured peak footprint was close to that estimate. The 4 GB ceiling is also capped by `physicalMemory - 1.5 GB`; larger requests fail before RAW decoding with an `INVALID_REQUEST` containing estimated and safe-budget byte counts. Actual AEB and tripod long-exposure RAWs were not available, so the photographic comparison remains open and Issue #83 must not be closed on this report.

## Verification

- `swift test`: 140 tests passed, 9 RAW-dependent tests skipped, 0 failures.
- `swift build -c release`: passed.
- Nine-frame original-resolution identical-image resource smoke: succeeded as described above.
- Two-frame single and one-line `--batch` smoke: normalized metadata matched, and JPEG SHA-256 matched (`ee696a7c401b66ff...`); batch sequence and stage timings were excluded from the normalized metadata comparison as designed.
- Two-frame real decode with `exposure=2` / `highlight_recovery=100` versus both zero: JPEG SHA-256 was identical (`ee696a7c401b66ff...`); the response lists both ignored keys and reports effective exposure 0.
- `git diff --check`: passed.

## API difference from the frozen proposal

The response adds `sequence` only for batch, consistent with current CLI convention; timing fields vary by invocation, so byte-for-byte JSON equality between single and batch responses is not a contract. Both paths call `RequestParser.parseMerge` and the same `merge()` implementation. `elapsed_ms.align` and `elapsed_ms.fuse` report separate MergeCore stages; `post_encode` includes the post chain, render, and atomic JPEG write.

## 2回目レビューでの追加検証と修正（2026-10-23）

### 色変換の往復

合成後段で本番と同じ `mergePostChainInput` → `PostChain.apply` → sRGB `CIContext.render` を通すテストを追加した。sRGBグレーランプ（0、0.01、0.05、0.18、0.5、0.75、1）とRGBパッチを、lift 0 / brightness・contrast・saturation 1 / USM gain 0 で入力値へ戻す。旧グラフを同じテスト内で再現すると、sRGBタグ付き0.5を明示的に `CISRGBToneCurveToLinear` へ通した後段出力は約0.214となり、二重decodeで暗くなることを数値で確認する。原因はタグ付きsRGB CIImageが色管理でlinear working spaceへ変換されているところへ、明示decodeをもう一度掛けていたこと。merge入力生成を共通関数へ切り出し、余分なtone curve filterを外した。単発 `develop()` と `PostChain` の実装は変更していない。

### メモリ見積もり

旧17 bytes/px/frame式は、保持フレームに依存しないMertensの作業領域もframe数倍していた。新式は `pixels × (6 × frame数 + 112)` bytes。6 bytes/frameは保持UInt16 RGB配列。固定112 bytes/pxはソース上のfusion peakにあるshifted RGB（12）、重み計算3面（12）と出力（4）、正規化重みと重みpyramid（約8）、画像pyramid（約16）、Laplacian upscale scratch（12）、累積pyramid（約16）、再構成出力（12）、RGBAデコード/書き出し時の追加領域を足して余裕を持たせた予約枠で、frame数に依存させない。2/3/9枚・巨大入力・overflow・preview切り上げの境界テストを加えた。24 MP×9枚の見積もりは3.984 GBとなり、4 GB ceiling内に残る。RAW metadataからの事前判定に加え、CIRAWFilterの実extentで寸法積/配列要素数/row bytesのoverflowと予算を配列確保前に確認する。

### 契約と再現方法

merge requestでは `settings.exposure_ev` は値が0でも、非nullで明示された時点で拒否される。既存のsettings/adjustments排他検証のキー指定契約に従い、validationを緩めずREADMEの「0以外拒否」を明確化して0指定拒否テストを加えた。`exposure` と `highlight_recovery` は引き続きadjustments内の0固定で無視される。

実画像で単発と `--batch` を再現するスクリプトは `native/coreimage-cli/scripts/smoke-merge-single-batch.sh`。GPSを含む実ファイルはリポジトリへ置かず、`GEOFRAME_TEST_MERGE_ARWS` に2〜9件の絶対ARWパスをコロン区切りで渡す。previewの単発/バッチ出力JPEGをbyte比較し、応答の寸法とignoredキーを検証する。既存9枚同一画像の資源測定は画素一致の資源スモークであり、実AEB/三脚長秒ブラケットの画質試験は未実施のまま。実写画質を確認するまでIssue #83は閉じない。

今回のrelease resource smokeは、`DSC07415.ARW` のハードリンク2件、decoder 9、max_edge 2400、M3/macOS 27.0.1で実施した。単発/バッチJPEGはbyte一致し、両応答に ignored exposure / highlight_recovery が記録された。単一実行の壁時計は7.30秒、最大RSSは647,233,536 bytes。metadata 6000×4000からのpreview見積もりは2400×1600×(6×2+112) = 952,320,000 bytes（約0.89 GiB）で、実測RSSはこの予算内だった。元画像は同一露出の複製なので、これもブラケット画質評価ではない。

### decoder versionの契約修正（2026-10-23）

merge requestはdecoder version 9に固定した。省略時は従来どおり最新decoder（現在9）を選び、明示した `"9"` は許可し、`"8"`、数値・booleanなどの不正型、未知versionは `INVALID_REQUEST` で拒否する。共通request parserの8/9対応は保ち、mergeだけ許可値を9に絞る。mergeの単発と `--batch` はどちらも `RequestParser.parseMerge` を通ることをコードで確認し、parser境界とbatch応答の回帰テストを追加した。`swift test --filter 'CoreImageDevelopTests\.(MergeRequestTests|BatchTests|RequestParserTests)'` は30件成功、実RAW依存2件skip、失敗0件。非mergeのdecoder 8受理テストも成功した。`git diff --check` 成功。

## 親側の最終再検証

2026-10-23、修正後に親側でSwift全144件（9件skip、失敗0件）を確認した。release同梱用CLI 0.8.0のビルドとgit diff --checkも成功。手動境界レビューでは信頼度7以上の新規リスクは0件だった。

### 親側の最終版・原寸2枚の資源検証

CLI 0.8.0の色変換修正後に、既存DSC07415.ARWを一時フォルダへ別名コピーした2枚（6000×4000、max_edge指定なし）を合成した。CLI totalは12.718秒、maximum RSSは2,435,121,152 bytes、peak footprintは2,497,317,840 bytes。新式の事前見積もり2,976,000,000 bytes内で成功した。出力5999×3999、ignored exposure/highlight_recoveryも確認。一時コピーと生成物は検証後に回収し、元RAWは変更していない。同一露出画像の複製による資源確認であり、実AEBの画質検証ではない。
