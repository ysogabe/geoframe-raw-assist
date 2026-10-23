# CoreImage MergeCore（PR-A）

## 範囲とAPI

`MergeCore` は `CoreImageDevelop` に追加した純Swift CPU実装です。CoreImageとImageIOをimportしません。PR-Bは現像後のsRGBガンマRGBを `MergeRGBImage(width:height:pixels:)` に渡して呼び出します。

```swift
let result = try MergeCore.merge(images, weights: .init(), edge: "crop")
```

`pixels` は0...1のFloat32 interleaved RGB配列です。フル解像度フレームを圧縮保持する呼び出し元向けに、0...65535の `uint16RGB` initializerもあります。`MergeResult` は合成画像、各フレームの検出シフト（x,y順）、実適用シフト、中央pivot（`count / 2`）、共通crop、pyramid段数、位置合わせ状態と理由を返します。fallback時は検出値を `shiftsXY` に残し、`appliedShiftsXY` は全0です。`edge: "keep"` は比較用にゼロ埋め領域を残します。枚数、寸法、配列長、0...1外または非有限の値、オプションの不正値は `MergeCore.MergeError` をthrowします。

既定weightはcontrast=1、saturation=1、exposure=0です。2〜9枚の同寸画像を受け、pyramidと合成計算はFloat32で行い、出力を0...1へclipします。MTBの比較対象が無い場合、またはシフトが `maxShiftRatio` を超える場合は無補正入力へfallbackします。

## 検証

* SwiftPMのtest targetにOpenCV 5.0.0で生成した小さな `pyrDown` と奇数・偶数 `pyrUp` のoracle配列を追加しました。7×5の奇数出力はOpenCVの `dstsize` 位相を、6×4の偶数出力は零挿入filterを確認します。
* MTBは既知の1px移動と中央pivotを確認します。無地入力、crop範囲、有限出力、0...1範囲、寸法overflow、1/2/10枚境界、空crop fallbackと検出/適用shiftの分離もテストします。
* 50 dBを受け入れ基準にし、修正後のgroup 0/1/2 PSNRは56.9565/58.9308/55.2099 dBです。しきい値は下げていません。
* 差分の段階比較では、ImageIO入力RGB8をFloat32へ変換した画素はPNG RGB8値と完全一致し、AlignMTBの検出シフトも3組すべてでOpenCVと一致しました。旧PSNR不足の原因はMertens重み計算前の入力スケールでした。baseline生成側は正規化RGBを255倍してOpenCVへ渡しますが、Swiftは0...1のままcontrast/saturationを計算していました。contrast×saturationに加える固定epsilon 1e-12との比が変わり、低コントラスト点で正規化weightが別物になります。
* OpenCV 5.0.0の4近傍Laplacianと同じ入力フレームを使って調べたgroup 0の(x=56,y=71)では、0...1スケールの正規化weightは[0.090929, 0.909043, 0.000027]、baselineどおり255倍した場合は[1.0, 3.09e-9, 3.09e-9]でした。weight mapのゼロ値率は約10%あり、単なる絶対誤差の一致では見落とす差でした。修正ではpyramidとcontrast/saturation weightを255倍スケールで計算し、出力を255で割って0...1へ戻します。exposure weightだけは公開入力契約に合わせて0...1へ戻して計算します。
* 修正後の固定baseline PSNRはgroup 0/1/2で56.9565/58.9308/55.2099 dBです。50 dB基準を維持し、baseline PNGやprovenanceは再生成していません。既存のbaselineテストがこの96×96合成入力と出力を固定し、スケール修正を外すと3 assertionが再現します。
* SwiftPMのMergeCoreTestsは9件すべてpassです。フル135件は9件skip、45件failでした。この実行環境ではDecodeCacheTestsのキャッシュ領域書き込みとCoreImage/Metal kernel初期化が拒否され、SmokeTests等が失敗します。MergeCoreの失敗はありません。CoreImage側はunsupported system / Cannot initialize kernel libraryでした。
* 境界安全性: 0...1外の負値・1e30を拒否し、画素数乗算overflowを無効画像として拒否します。common cropが空のときは1pxに偽装せず全画面・無補正へfallbackし、検出shiftと適用shiftを別々に返します。
* 実写RAWブラケットはありません。PR-AはRAWデコード、CoreImage呼び出し、CLI `merge` mode、Rust/UI変更を含みません。

## PR-B向けメモ

呼び出し側の選択順でフレームを渡してください。MergeCoreはsortしません。pivotは中央で、PR-Bがファイル名やEXIF継承に使う先頭フレームとは独立です。応答metadataは検出shift `shiftsXY` を診断用に、画像生成の再現には `appliedShiftsXY` を使います。本番では `edge: "crop"` を指定してください。入力はFloat32で渡すことも、16bit量子化してUInt16で保持することもできます。合成核はUInt16入力をframeごとにFloat32へ展開し、pyramid scratchもframeごとに処理して全フレーム分を同時保持しません。

## 親側の再検証

2026-10-23、通常のSwiftキャッシュとCoreImage/Metalを利用できる権限で全テストを再実行した。
`swift test --package-path native/coreimage-cli`: 135件実行、9件skip、失敗0件。
MergeCoreの基準3組も50 dBの閾値を維持して成功した。前述の45件の失敗は制限環境での結果であり、親側では再現しなかった。
