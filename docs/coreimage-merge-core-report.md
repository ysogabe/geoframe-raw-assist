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
* 50 dBを受け入れ基準にしたまま再計測しました。group 0/1/2のPSNRは46.1857/46.0719/45.3137 dBで、3組とも未達です。しきい値は下げていません。
* 差分の段階比較では、ImageIO入力RGB8をFloat32へ変換した画素はPNG RGB8値と完全一致し、AlignMTBの検出シフトも3組すべてでOpenCVと一致しました。OpenCV Mertensのfloat出力を既存PNGへ8bit量子化した誤差は58.93〜59.03 dBです。したがって不足はdecode、MTB、最終8bit化ではなく、Mertensの重み付きpyramid合成段にあります。saturation単独のSwift出力はOpenCV 5.0.0と142 dB前後で一致し、contrast map単体もRGB係数・4近傍Laplacianで一致します。weighted Laplacianを全レベルで合成する段階の差をまだ特定できていません。
* 未達時の画素誤差は group 0/1/2 で MAE 0.003521/0.003794/0.003860、p95絶対誤差 0.006327/0.006503/0.006374、最大絶対誤差 0.117979/0.035219/0.254760（0...1 Float）。次の修正候補は、OpenCV 5.0.0 `modules/photo/src/merge.cpp` の正規化済みweight pyramid・各level weighted LaplacianをSwift側でも個別にダンプし、同じ画素で差が初めて現れる段を固定fixtureにすることです。既存のpyrUp/pyrDown oracleだけでは合成段の一致を保証できません。
* SwiftPM全体は135テストを実行し、9件skip、123件pass、基準PSNRの3 assertionがfailしました。PSNR未達を完了扱いしていません。
* 境界安全性: 0...1外の負値・1e30を拒否し、画素数乗算overflowを無効画像として拒否します。common cropが空のときは1pxに偽装せず全画面・無補正へfallbackし、検出shiftと適用shiftを別々に返します。
* 実写RAWブラケットはありません。PR-AはRAWデコード、CoreImage呼び出し、CLI `merge` mode、Rust/UI変更を含みません。

## PR-B向けメモ

呼び出し側の選択順でフレームを渡してください。MergeCoreはsortしません。pivotは中央で、PR-Bがファイル名やEXIF継承に使う先頭フレームとは独立です。応答metadataは検出shift `shiftsXY` を診断用に、画像生成の再現には `appliedShiftsXY` を使います。本番では `edge: "crop"` を指定してください。入力はFloat32で渡すことも、16bit量子化してUInt16で保持することもできます。合成核はUInt16入力をframeごとにFloat32へ展開し、pyramid scratchもframeごとに処理して全フレーム分を同時保持しません。
