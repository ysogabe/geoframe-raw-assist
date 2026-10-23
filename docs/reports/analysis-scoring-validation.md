# 解析の採点・推薦の Rust 移植（#114）

## 実装

`src-tauri/src/analysis.rs` に画像採点、順位、代表選択、信頼度、推薦理由、
RAW の 4 段階判定を移植した。`analysis_version: 3` は維持し、`scored: true` を返す。
`scored: false` またはフラグなしのキャッシュは再解析する。
Python は参照実装と手動比較にだけ使い、アプリの実行経路からは呼ばない。

- JPEG: `jpeg-decoder 0.3.2` に元寸法 `// 8`（最低 1）を要求する。
  採点は未回転の縮小 RGB、サムネイルだけ向きを補正する。
  6000×4000 は 750×500 になる。従来の長辺 640 基準と違い、小さい JPEG も
  Python と同じ 1/8 に縮小する。サムネイルはそこから長辺 640 以内へ縮小し、拡大しない。
- 輝度: uint8 RGB を OpenCV RGB2GRAY と同じ整数係数
  `(9798*R + 19235*G + 3735*B + 16384) >> 15` で uint8 相当に丸める。
- Laplacian: `ksize=1` の十字カーネル、`BORDER_REFLECT_101`。
  1 画素だけの軸はその画素を参照する。符号付き整数から float64 の母分散を計算する。
- clip 比: いずれかの RGB 成分が 250 以上、全成分が 5 以下の画素の割合。
- 推薦: シャープ順位 0.6、白飛びの少なさの順位 0.4。同点は良い方の順位を共有し、
  総合値も同点なら capture_id の昇順で選ぶ。推薦判定は丸め前の値を使う。
  出力だけ Python と同じ桁数へ十進 ties-to-even で丸める。

通常テストでは色付き合成画素・勾配・1 行・1 画素の母分散、clip 境界、順位の同点、
単写・僅差・バランス型の推薦、RAW しきい値・理由順、未採点キャッシュの無効化を検査する。
フロントは変更していない。`GroupList` / `OperationPanel` の条件をコードで確認し、
`scored === false` の未採点表示を保ち、true では点数・信頼度・推薦理由を表示する。
指定に従い dev サーバーと Tauri は起動しておらず、画面の目視検証はしていない。

## 実機比較の手順

Pillow のある開発用 Python と外部 JPEG が必要なため `#[ignore]` としている。
読み取り専用の元画像は変更しない。派生サムネイルと合成画像はテスト用一時フォルダへ作り、
成功時に削除する。Python は PIL draft の RGB 画素を標準出力で Rust に渡すだけで、
元画像のメタデータや解析 JSON を出力しない。

```sh
npm ci && npm run build:coreimage
GEOFRAME_ANALYSIS_DIR=<repo>/work/raw \
GEOFRAME_ANALYSIS_PYTHON=<baseline-checkout>/.venv/bin/python \
cargo test --manifest-path src-tauri/Cargo.toml analysis::tests::compare_scoring_baseline -- --ignored --nocapture
```

`GEOFRAME_ANALYSIS_PYTHON` は Pillow を入れた任意の Python に変更できる。
今回の参照環境は Pillow 12.3.0。基準は `tests/fixtures/baseline/analysis.json` を直接読む。
基準の「11 組」は JPG/ARW の 11 ペアであり、解析グループは 8 個（枚数 1,2,1,1,1,2,1,2）。

## 実測結果

手動テストは成功（1 passed、16.11 秒）。ただし **厳密な数値目標は未達**。
Issue #114 が認めるデコーダ差の例外として、推薦を一致させ、差の大きさを固定している。
テストは `strict numeric target met: false` を明示する。

11 枚における Python の丸め済み出力からの最大誤差:

- `sharpness_raw`: 絶対 0.499、相対 0.001220504（目標 0.001 以内は 10/11 枚）。
- `highlight_clip_ratio`: 絶対 0.00004（目標 0.0001 以内は 11/11 枚）。
- `shadow_clip_ratio`: 絶対 0.01772（目標 0.0001 以内は 5/11 枚）。
- `mean_luma`: 絶対 0.50。
- `sharpness_score` と `overall_score`: ともに絶対 0。

8 グループすべてで `recommended_capture_id`、`raw.level`、`raw.reason_codes` が一致（各 8/8）。
`confidence`、`recommendation_reasons`、`raw.reasons` の文言、および summary も完全一致。
Pillow がデコードした **同じ画素** を Rust で採点した場合、全 11 枚の 4 指標が
凍結 Python 出力の各桁まで一致した（比較誤差 1e-9 未満）。

デコーダの比較では全 11 枚とも 750×500 で寸法一致。
RGB 成分差の最大は 22、平均絶対差は 0.783918707、差のある成分は
7,300,401 / 12,375,000。採点式ではなく入力画素段階に差があることを確認した。

丸め差は、色差のない 8×8・輝度 127・品質 90 の合成グレースケール JPEG を
1×1 へ DCT 縮小しても Pillow=127 / jpeg-decoder=126 と再現する。
`jpeg-decoder 0.3.2` の `idct.rs::dequantize_and_idct_block_1x1` は
`(DC * quant + 128 * 8) / 8` で丸め加算なしの整数除算をする。
この 1 段の差でも、しきい値 5 付近の画素が多い画像では黒つぶれ比に差が出る。
また同ライブラリは全成分へ同じ DCT スケールを適用し、色差をアップサンプリングする。
実機の 4:2:2 画像にはこの処理と色変換の差も含まれると考えられるが、
RGB 差の全量を各要因へ個別分解したわけではない。

既存デコーダを維持し、画像固有の補正値や採点結果の補正は入れていない。
比較テストは同一 Pillow 画素での厳密一致と推薦一致に加え、実測誤差の回帰上限
（sharpness 相対 0.0013、highlight 0.0001、shadow 0.018、mean_luma 0.51）を検査する。
これは当初の厳密な数値許容差とは区別する。厳密目標へ到達するには、
libjpeg 系デコーダへの切替か、縮小 IDCT・色差処理の整合が別途必要になる。

## コマンド結果

- `npm ci && npm run build:coreimage`: 成功。
- `cargo test --manifest-path src-tauri/Cargo.toml`: 成功、118 passed / 4 ignored。
- `npx tsc --noEmit`: 成功。
- `npm test`: 成功、6 ファイル / 124 テスト。
- `npm run lint`: 成功。
- `npm run build`: 成功。
- 上記 `compare_scoring_baseline -- --ignored --nocapture`: 成功、1 passed。

変更ファイルは `src-tauri/src/analysis.rs` とこの文書だけ。
コミット・push・PR 作成・stash は行っていない。
