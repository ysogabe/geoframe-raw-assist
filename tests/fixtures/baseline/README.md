# Python 基準値の凍結（#107）

2026-10-23追補: 以下は削除前の生成記録を含む。
保存済み画像・JSONを読む評価経路と、旧実装による基準の再生成は区別する。
今後の順序は[評価経路独立化の計画](../../../docs/design/evaluation-before-python-removal.md)、
削除後も維持する実行方法は[評価経路の手順](../../../docs/development/evaluation-paths.md)を参照。
凍結JSON・合成画像・旧rawpy画像の内容は今回の評価整理では更新しない。

Python 廃止前の参照実装を固定する。数値の意味・許容差は
`docs/design/single-app-implementation-plan.md` §3.8 / §4 と
`docs/adr/0003-python-removal-single-app.md` に従う。
実機写真および派生画像はここには置かない。

## 再生成（削除前の参照点 `e838ecc` のcheckoutでのみ）

リポジトリルートで実行する（外部入力が必要）。

```sh
.venv/bin/python scripts/freeze_baseline.py --legacy-generate
.venv/bin/python scripts/freeze_baseline.py --audit-only
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

この環境では既定の `python3` が 3.15.0 で、依存に対応する wheel が
揃っていなかったため、Python 3.14 を指定して仮想環境を直接作成した。
`node_modules` は不要。

```sh
mkdir -p work/python314-bin
ln -sf /opt/homebrew/bin/python3.14 work/python314-bin/python3
/opt/homebrew/bin/python3.14 -m venv --clear .venv
PATH="$PWD/work/python314-bin:$PATH" .venv/bin/python scripts/freeze_baseline.py --legacy-generate
```

既存 `.venv` を作り直す `--clear` は環境を作り直す場合だけ使用する。
入力パスを変更する場合は `--source-root /absolute/path` を渡す。
出力先はこの checkout の `tests/fixtures/baseline/` と `work/baseline/` に固定。
`--audit-only` と追加テストは外部画像を必要としない。
基準更新は意図して行うこと（再生成は既存の基準 JSON を上書きする）。

## 外部入力

- `<repo>/work/raw/`
  の DSC07402 / 07404 / 07405 / 07407 / 07408 / 07412 / 07415 / 07416 /
  07419 / 07422 / 07423 の `.arw` と `.JPG`、11 組。読み取り専用。
- 元 RAW の有無を `rg --files <repo>/work -g '*.ARW' -g '*.arw'`
  で確認した。利用可能な元 RAW は上記 11 枚のみ。
  アーカイブにある過去の現像画像を元 RAW の代替として使っていない。
- `/Volumes/Backup/100MSDCF` は未マウントのため使わない。
  `candidates_baseline.json` の `image_work` に対応する昼夜混在の入力もない。
- `provenance.json` は入力ファイル名と sha256、依存バージョン、OS、生成秒数を記録。
  `analysis._source_signature` は元の絶対ルートとファイル名から計算されるため、
  別のルートへコピーすれば署名とサムネ接頭辞は変わる。移植時は同じルートで比較する。
  現行の Rust 解析は `_source_signature` の代わりに入力指紋 `_source_fingerprint`（サイズ・mtime を含む）と
  公開フィールド `source_id`・`analysis_revision` を出す（#32。docs/API_CONTRACT.md §3）。
  `analysis.json` は当時の形のまま凍結し、これらを後から書き足さない。

## 7 件の状態とファイル

| 項目 | ファイル・内容 | 状態 | 利用 Issue |
|---|---|---|---|
| 解析 | `analysis.json`: `build_analysis` の全 11 枚の EXIF 要約、採点、グループ、推薦と `_source_signature` | 完了 | #110 / #114 |
| EXIF 継承 | `exif_tags.json`: 全 11 枚の元 / `build_inherited_exif(6000, 4000)` の IFD0・Exif・GPS・Interop・IFD1 のタグ一覧 | 完了 | #108 |
| rawpy 基準画像 | `rawpy_images.json`: DSC07402 / DSC07423 の preview / full のファイル名、sha256、寸法 | 完了（代表 2 枚） | #108 / #90 |
| PSO 純関数 | `metrics.json` と `gradient.png` / `stars.png` / `checker.png` / `black.png` / `white.png`: `_image_metrics`、ゲート有無の `_star_measurements`、重複点・空集合を含む `pareto_front` | 完了 | #86 |
| 候補探索 | `candidates.json`: `scripts/eval_cases/candidates_baseline.json` を既存ローダーで読み、`generate_development_candidates` を既定 seed / budget で呼んだ adjustments・代理指標・除外理由 | 星空 11 ケース完了。`image_work` 13 ケースは入力不足で保留 | #87 |
| ブラケット | `bracket.json` と `bracket-*.png`: 合成 3 組の OpenCV MTB シフトと Mertens 出力 | 最小合成基準は完了。実写代表 3 組は元 RAW 不足で保留 | #83 |
| 書き出し | `manifest.json`: `export_batch` が実際に書いた camera_jpg / developed の現行 manifest。コピー実体は `work/` に保存 | 完了 | #111 |

`candidate_series.json` は最初の星空ケース DSC07402 の評価順を固定する。
`_Evaluator.evaluate` の呼び出しをラップし、返った指標、入力 adjustments、star_gate、
既定 seed / budget、最終候補集合を保存した。さらに `_metric_objectives` の入力
（丸め前の探索ベクトル・目標・基準・指標・安全策ペナルティ）と出力を記録する。探索ロジックや指標を再実装していない。
Rust 側の検証では記録済みの系列・指標入力を使い、numpy の乱数列の再現は要求しない。
候補 JSON の指標は Python 探索の代理指標であり、`evaluate_candidates.run_case` が
別途計測する実 RAW 現像後の `raw_metrics` は含まない。

合成 PNG はすべてスクリプト内でゼロから作る 96×96 RGB（メタデータなし）。
ブラケット入力は露出倍率 0.5 / 1 / 1.8 と既知の roll 移動の組み合わせ。
`mtb_shifts_xy` は `cv2.createAlignMTB(...).calculateShift` の出力であり、既知の
移動量と常に一致することを期待しない。実際の合成には既存の
`bracket._align_frames` と `bracket._merge_frames` をそのまま使用する。
画素は `bracket-*-merged.png`（RGB 8bit）だけに保存する。JSON の `reference` は
PNG の sha256・寸法・形式・PSNR 比較時のピーク値（255）。移植後はこの PNG を
基準として 8bit RGB で PSNR を計測する。`float32_stats_rgb` は量子化前の RGB 各チャンネルの
最小・最大・平均・標準偏差、`quantization_psnr_db` は Python の float32 出力と
PNG を 0..1 に戻した値の PSNR（ピーク値 1）で、8bit 量子化による誤差の目安。
float32 全画素は JSON に保存しない。
実写 3 組の保留は合成基準だけでは解消しない。

## 実機由来画像の置き場

`compare_engines.run_rawpy` を直接呼び、同スクリプトの rawpy ワーカーで生成する。
Core Image CLI のビルドや呼び出しは不要。

- `work/baseline/rawpy/preview/`: 長辺 1600 の基準 JPEG とレシピ JSON。
- `work/baseline/rawpy/full/`: 原寸の基準 JPEG とレシピ JSON。
- `work/baseline/thumbnails/`: Python 解析が作る WEBP。
- `work/baseline/export/baseline-107/`: 実際にコピーされた JPEG と元 manifest。

これらはすべて gitignore 対象。外部入力と同じく GPS を持ち得るのでコミットしない。
コミット対象の `rawpy_images.json` の path は checkout 相対。
sha256 は EXIF 込み JPEG のバイト列全体、寸法は保存 JPEG の width / height。
`rgb8_sha256` は JPEG を RGB 8bit へデコードした行優先バイト列のハッシュ。
既存 Python はプロセス起動時に生成する ICC プロファイルに作成時刻を含むため、
同じ環境での再実行でも JPEG 全体の sha256 は変わる。画素一致には `rgb8_sha256`
を使い、保存したファイルそのものの識別には sha256 を使う。
OS・依存・レンズ DB 等が変われば再生成した画像のハッシュが変わり得る。
画像を比較するときはこの checkout の `work/baseline/` の凍結画像を参照し、
移動する場合も JSON と対応付けてリポジトリ外に保管する。

## 正規化と GPS 非保存

- `analysis.json` の `generated_at`、manifest の `created_at` を
  `2000-01-01T00:00:00+00:00` に正規化する。撮影日時はそのまま。
- manifest 内の絶対パスはファイル名 / 末尾のフォルダ名に正規化する。
  レシピや manifest のその他の構造と数値は既存関数の出力を保持する。
- EXIF の IFD ポインター値は省略。GPS IFD は存在有無、タグ ID、Python 値の型、
  要素数だけ。GPS IFD のキーは `present` / `tag_ids` / `tags`、各タグは
  `type` / `count` に限定する。緯度・経度・時刻・測地系など GPS の値も
  そのハッシュも一切保存しない。狭い値域のハッシュは総当たりで復元され得るため。
  移植先との GPS 値の一致確認は、テスト実行時に手元の実機 JPEG と継承出力を読み、
  GPS IFD をメモリ内で直接比較する。値・ハッシュを fixture やログには残さない。
  この生成スクリプトも全 11 枚で `verify_gps_inheritance` により元画像と Python 継承出力を
  直接比較する。メタデータだけでは同じ型・要素数の値の変化を検出できないため。
  バイト / 文字列の count は長さ、tuple は要素数、スカラーは 1。
- GPS 以外のハッシュ対象は `jsonable` による正規化 JSON（キー順、空白なし、UTF-8）。
  バイトは hex、Rational は約分した numerator / denominator。
- GPS 以外も既知のカメラ技術タグの許可リストだけ値を出す。
  MakerNote、説明、コメント、私用タグなどは値をハッシュ化する。
  元画像の MakerNote はハッシュで存在を記録し、継承出力では消えることを検証する。
- IFD1 はタグの型・要素数・ハッシュだけ。サムネイルの画像データは保存しない。

```sh
.venv/bin/python scripts/freeze_baseline.py --audit-only
.venv/bin/python -m unittest discover -s tests -p 'test_freeze_baseline.py'
```

監査は GPS IFD が `present` / `tag_ids` / `tags` のみ、各 GPS タグが
`type` / `count` のみであることを確認する。ハッシュを含む他のキーは拒否する。
GPS 以外の私用 EXIF に `value` が無いことも検査する。他の全 JSON は位置情報のキーを再帰検査する。
PNG は EXIF とその他メタデータが空であることを確認する。
追加テストは GPS の平文・ハッシュ・余分なキーや入れ子の latitude を注入すると
監査が失敗すること、同じ型・要素数の GPS 値の変化を直接比較が検出することも確認する。
単なる座標文字列の grep だけに頼らず、保存時の EXIF 許可リストと構造検査で制限する。

## この凍結の実行記録

- Python 3.14.8、依存バージョン・OS は `provenance.json` 参照。
- 全生成 191.877 秒（約 3 分 12 秒）。解析 0.649 秒、EXIF 0.025 秒、
  純関数指標 0.035 秒、rawpy 4 画像 28.356 秒、候補 11 ケース 162.178 秒、
  合成 0.094 秒、manifest 0.064 秒。環境構築・テスト時間は含まない。
- 系列固定フィクスチャは 48 評価と 48 目的関数入力。
- DSC07402 preview の独立した再生成で RGB 画素は完全一致。
  ICC はヘッダーの作成時刻だけが変化し、それ以外のバイトは一致した。
- 保存済み rawpy 4 画像はファイル sha256 / RGB 画素 sha256 の両方が JSON と一致。

GPS 監査のコマンドと出力:

```text
$ .venv/bin/python scripts/freeze_baseline.py --audit-only
privacy audit: PASS (9 JSON, 102 GPS tags type/count-only, 17 metadata-free synthetic PNG)
```

検証結果:

```text
$ .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
Ran 867 tests in 82.725s
OK (skipped=1)

$ .venv/bin/python -m unittest discover -s tests -p 'test_freeze_baseline.py'
Ran 5 tests in 0.348s
OK
```


## QA 修正後の部分再生成

```sh
.venv/bin/python scripts/freeze_baseline.py --sections exif bracket
.venv/bin/python scripts/freeze_baseline.py --audit-only
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
du -sh tests/fixtures/baseline
```

`--sections` は EXIF / ブラケットのみを更新し、全生成の `provenance.json` は保持する。
GPS のハッシュを削除し、ブラケット全画素配列を PNG と数値要約に置き換えた。
`bracket.json` は 3,316,023 → 3,918 bytes、`exif_tags.json` は 217,518 → 208,338 bytes。
`candidate_series.json`（136,483 bytes）は再生検証に必要な系列を保つため変更していない。

QA 修正後の検証結果:

```text
$ .venv/bin/python scripts/freeze_baseline.py --sections exif bracket
exif: 0.137s
bracket: 0.222s
privacy audit: PASS (9 JSON, 102 GPS tags type/count-only, 17 metadata-free synthetic PNG)

$ .venv/bin/python scripts/freeze_baseline.py --audit-only
privacy audit: PASS (9 JSON, 102 GPS tags type/count-only, 17 metadata-free synthetic PNG)

$ .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
Ran 869 tests in 30.629s
OK (skipped=1)

$ du -sh tests/fixtures/baseline
568K tests/fixtures/baseline
```
