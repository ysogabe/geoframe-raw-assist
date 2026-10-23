# CoreImage MergeCore（PR-A）

## 範囲とAPI

`MergeCore` は `CoreImageDevelop` に追加した純Swift CPU実装です。CoreImageとImageIOをimportしません。PR-Bは現像後のsRGBガンマRGBを `MergeRGBImage(width:height:pixels:)` に渡して呼び出します。

```swift
let result = try MergeCore.merge(images, weights: .init(), edge: "crop")
```

`pixels` は0...1のFloat32 interleaved RGB配列です。フル解像度フレームを圧縮保持する呼び出し元向けに、0...65535の `uint16RGB` initializerもあります。`MergeResult` は合成画像、各フレームのシフト（x,y順）、中央pivot（`count / 2`）、共通crop、pyramid段数、位置合わせ状態と理由を返します。`edge: "keep"` は比較用にゼロ埋め領域を残します。枚数、寸法、配列長、非有限値、オプションの不正値は `MergeCore.MergeError` をthrowします。

既定weightはcontrast=1、saturation=1、exposure=0です。2〜9枚の同寸画像を受け、pyramidと合成計算はFloat32で行い、出力を0...1へclipします。MTBの比較対象が無い場合、またはシフトが `maxShiftRatio` を超える場合は無補正入力へfallbackします。

## 検証

* SwiftPMのtest targetにOpenCV 5.0.0で生成した小さな `pyrDown` と奇数・偶数 `pyrUp` のoracle配列を追加しました。7×5の奇数出力はOpenCVの `dstsize` 位相を、6×4の偶数出力は零挿入filterを確認します。
* MTBは既知の1px移動と中央pivot、その他に無地入力、crop範囲、有限出力、寸法、1/2/10枚境界を確認します。
* コミット済み合成ブラケット3組は45 dBの下限を通過します。group 0/1/2のPSNRは46.19/46.07/45.31 dBで、設計目標の50 dBには未達です。一致性の主張に使う前に差分の追加調査が必要です。入力と参照PNGはどちらもImageIOでsRGBデコードして測定しました。
* 実写RAWブラケットはありません。PR-AはRAWデコード、CoreImage呼び出し、CLI `merge` mode、Rust/UI変更を含みません。

## PR-B向けメモ

呼び出し側の選択順でフレームを渡してください。MergeCoreはsortしません。pivotは中央で、PR-Bがファイル名やEXIF継承に使う先頭フレームとは独立です。応答metadataには返却されたcropとshiftsを使います。本番では `edge: "crop"` を指定してください。入力はFloat32で渡すことも、16bit量子化してUInt16で保持することもできます。合成核はUInt16入力をframeごとにFloat32へ展開し、pyramid scratchもframeごとに処理して全フレーム分を同時保持しません。
