# 評価経路（Python 本体の削除前後で使う評価スクリプト）

旧Python本体と独立評価の両方を含む最後の参照点は `e838ecc`。
旧基準の再生成が必要な場合だけ、そのコミットを別checkoutで使用する。
今回の11枚のCore Image実測・決めた修正範囲は[計測報告](../reports/coreimage-proxy-full-measurement.md)を参照。

#57 / #90 / #117。実施順と検収条件は `docs/design/evaluation-before-python-removal.md`。
この文書は `scripts/` の評価スクリプトの**使い方**（CLI モード・前提・削除後の扱い）をまとめる。

## 原則

- 既定の評価経路はアプリ本体（`local_raw/`）にも `rawpy` / `lensfunpy` にも依存しない。必要な純関数・
  凍結済みプリセット表・撮影 ID の解決と EXIF 要約は `scripts/evaluation/` に最小限だけ持つ。
- 旧エンジン（rawpy）で基準や候補を**作り直す**経路はアーカイブで、明示フラグ（`--legacy-rawpy` /
  `--legacy-generate` / `--real-images` / `--write` / `--check-source`）を付けたときだけ動く。フラグが無いか、
  `local_raw/` が無ければ、前提を説明して終了コード非 0 で止まる（黙って縮退しない）。
- 凍結物（画像・JSON）の欠落・改変・寸法違いは成功扱いにしない。
- 旧 Python の閾値（`CLIP_WORSEN_LIMIT` 等）は Core Image の合否基準にしない。集計では「参考診断」と明記する。

## 環境

```sh
python3 -m venv .venv-eval
.venv-eval/bin/pip install -r requirements-dev.txt   # numpy / Pillow / opencv-python-headless のみ
```

Core Image を描画するモードは CLI（`npm run build:coreimage` →
`native/coreimage-cli/.build/release/geoframe-ci-develop`、`--cli` で変更可）が要る。

`scripts/evaluation/` の構成:

| モジュール | 中身 |
|---|---|
| `sources.py` | `DSC#####` の ARW / JPG 解決（拡張子の大小を問わない）、EXIF 要約（GPS は読まない）、EV100 |
| `presets.py` | 凍結プリセット表の読み込み。旧値は `tests/fixtures/presets_legacy_rawpy.json`、CI 値は `src-tauri/tests/fixtures/presets.json` |
| `candidate_metrics.py` | 旧 `development_candidates` の星判定・指標・安全策・スコア・理由の写し（値もロジックも同じ。凍結 JSON と完全一致をテスト） |
| `frozen.py` | 凍結マニフェスト（`rawpy_images.json` 形式）の照合: ファイル sha256・RGB 8bit の sha256・寸法・欠落 |
| `legacy.py` | 旧経路の opt-in ゲートと、前提が無いときのエラー文 |

## スクリプトとモード

### `scripts/compare_engines.py`

| モード | 既定 / 旧経路 | 内容 |
|---|---|---|
| `--frozen-manifest M --frozen-root R --frozen-verify-only` | 既定 | 凍結物の照合だけ（CLI・現像元不要）。`<out>/frozen_verification.json` |
| `--frozen-manifest M --frozen-root R --source-root RAW` | 既定 | 照合後、同じ撮影 ID・mode を現在の CI で描画し、凍結画像と同寸で比較（ΔE76・RGB MAE・輝度差・完全一致画素率・各画像の指標）。`frozen_comparison.{json,csv}`、`sheets/<id>_<mode>_<NN>_frozen.jpg`（`NN` はマニフェストの行番号。CI 出力は `coreimage/<mode>/<id>_<mode>_frozen<NN>*.jpg`。同じ撮影 ID・mode の行が複数あっても上書きしない） |
| 上 + `--frozen-capture FILE` | 既定 | 今回の CI 出力を同じ形式のマニフェストとして書く（凍結ルートは `--out`）。**描画エラーが 1 件でもあれば書かない**（既存の完全な基準を部分結果で置き換えない）。`--frozen-manifest`（比較）と一緒にだけ使える: 単独、`--frozen-verify-only` との併用、`--frozen-root` / `--frozen-preset` だけの指定は黙って無視せずエラーにする |
| `--frozen-bootstrap-ci --source-root RAW --captures ID... --out ROOT` | 既定 | **凍結画像がゼロでも**現在の CI 出力から基準を新規作成する（下の「CI 基準の新規作成」）。`ROOT/ci_frozen.json` と来歴を書く |
| `--captures` / `--select auto`（無指定） | 既定 | camera / coreimage / coreimage_lens の比較 |
| `--sharpness` / `--night` / `--highlight` / `--presets` / `--diagnosis` | 既定 | CI 系の列だけで実行。rawpy 列・rawpy 後段仕上げ（`*_usm`）は `skipped: legacy_rawpy`、`--diagnosis` の CFA 飽和（センサー段の帰属）は計測しない |
| `--adjustments --dry-run` | 既定 | リクエストの契約検査だけ |
| `--legacy-rawpy` を付けた上記 | 旧経路 | rawpy 列・`*_usm`・CFA 計測を加える |
| `--bracket` / `--adjustments`（実行） | 旧経路のみ | rawpy 現像・旧 `bracket` / 後段オラクルを使うため `--legacy-rawpy` 必須 |

凍結マニフェストの行に `request`（profile / lens / decoder_version / adjustments）があればそれで描画する。
無い行（#107 の rawpy 基準）は `apple_default`・lens ON・decoder 9・`--frozen-preset`（既定 `neutral_recovery`）の
CI プリセット値で描画する。差の合否閾値は持たない。失敗（終了コード 1）は照合失敗と描画失敗だけ。
凍結入力を上書きしうる出力先は、照合レポートを書く前に（`--frozen-verify-only` でも）拒否する: `--out` が
`--frozen-root` と同じ・その中・それを含む、`--out` が `--frozen-manifest` を含む、`--frozen-capture` が
`--frozen-manifest` と同じ・`--frozen-root` の中。照合後も、凍結画像が `--out` の中にある・`--frozen-capture` と
同じなら拒否する。

### `scripts/evaluate_candidates.py`

| モード | 既定 / 旧経路 | 内容 |
|---|---|---|
| `--aggregate-only --out DIR [--contact-sheets]` | 既定 | `DIR/cases/*.json` を集計して `summary.{json,md}`。`--contact-sheets` は `DIR/frames/<key>__jpg.jpg`・`<key>__<style>.jpg` がある場合だけ一覧を作る（Rust ハーネスは `frames/` を書かないので、その出力では警告して集計だけ） |
| `--case` / `--cases-file`（生成） | 旧経路のみ | 旧 rawpy 候補生成 + 実RAW現像。`--legacy-rawpy` 必須 |

Core Image 候補の `cases/*.json` は Rust の候補ハーネスが書く（このスクリプトは候補を生成しない）。
1 ケースの形式は旧経路と共通で、`candidates[].metrics` = CI 640px 代理の指標、`candidates[].raw_metrics` =
同じ adjustments の CI 原寸を 640px へ縮小した指標、`proxy.source` = `coreimage_proxy`。
`key` が無ければファイル名、`gen_elapsed` が無ければ `elapsed_ms.total` を使う。
`summary.acceptance` に判定列の位置づけを出す: `broken_candidates` / `style_goals` / `near_natural` は
`inherited_python_threshold_diagnostic`（旧 Python の閾値・狙いの参考診断）、`authoritative_ci_acceptance` は
`null`（この集計は CI の合否を出さない）。

### `scripts/evaluate_raw.py`

| モード | 既定 / 旧経路 | 内容 |
|---|---|---|
| `--developed LABEL=DIR`（繰り返し可） | 既定 | 先頭が撮影 ID `DSC#####` ちょうど（直後が数字でない）の現像済み画像をカメラ JPEG と比べる（LABEL ごとに集計）。**1 DIR に 1 撮影 ID 1 枚**、LABEL の重複も不可（どちらも平均が黙って偏るので失敗にする） |
| `--frozen-manifest M --frozen-root R` | 既定 | 凍結画像を照合してからカメラ JPEG と比べる（ラベル `frozen:<mode>`） |
| `--legacy-rawpy [--presets ...] [--preview]` | 旧経路 | 旧エンジンで各プリセットを現像して評価 |

エラー行（カメラ JPEG 欠落・読めない画像等）が 1 件でもあれば終了コード 1。

compare_engines の `coreimage/preview` 等は同じ撮影の `_preview` / `_preview_lens` / `_frozen<NN>` / `_e<edge>` を
並べて持つので、そのまま `--developed` に渡すと失敗する。評価したい 1 バリアントだけを別フォルダへ集める:

```sh
mkdir -p work/raw-evaluation/inputs/ci_lens
cp work/engine-comparison/<run>/coreimage/preview/DSC*_preview_lens.jpg work/raw-evaluation/inputs/ci_lens/
python scripts/evaluate_raw.py --source-root "$RAW" --developed ci_lens=work/raw-evaluation/inputs/ci_lens
```

### 凍結スクリプト

| コマンド | 既定 / 旧経路 | 内容 |
|---|---|---|
| `freeze_baseline.py --audit-only` | 既定 | `tests/fixtures/baseline/` の位置情報・私用 EXIF の監査 |
| `freeze_baseline.py --verify-frozen-root R` | 既定 | 監査 + `rawpy_images.json` の凍結画像 4 枚を照合 |
| `freeze_baseline.py --legacy-generate [--sections ...]` | 旧経路 | 旧 Python で基準を作り直す（基準 JSON を上書きする） |
| `freeze_pso_parity.py --check` | 既定 | `pso-selection.json` を凍結 JSON と写しの純関数で再計算して一致確認（書かない） |
| `freeze_pso_parity.py` | 既定 | 同じ再計算で書き出す（意図した更新のときだけ） |
| `freeze_pso_parity.py --real-images` | 旧経路 | 旧 half デコードの参照値 |
| `export_presets_fixture.py --check` | 既定 | 凍結 legacy 表と CI 表の構造（名前・ラベル・説明・並び・キー・値域・NR）と書式の検査 |
| `export_presets_fixture.py --check-source` / `--write` | 旧経路 | `local_raw/presets.py` との突き合わせ / 再生成 |

## CI 基準の新規作成（凍結画像がゼロのとき）

`--frozen-manifest` の経路は既存の凍結画像を照合してから比べるため、旧 worktree ごと画像を失った状態では
`--frozen-capture` を使えない。そこで**作成だけ**を行う明示モード `--frozen-bootstrap-ci` を持つ（旧 Python・rawpy・
lensfunpy は使わない）。#57 の実4画像（DSC07402 / DSC07423 の preview / full）の例:

```sh
CLI=native/coreimage-cli/.build/release/geoframe-ci-develop
RAW=<repo>/work/raw                      # 読み取り専用の現像元
FROZEN_ROOT=<worktree 掃除の対象外>/ci-baseline/macos-<version>   # 新規か空のディレクトリ。work/ かリポジトリ外
python scripts/compare_engines.py --frozen-bootstrap-ci --source-root "$RAW" --cli "$CLI" \
  --captures DSC07402 DSC07423 --out "$FROZEN_ROOT"
```

- 各撮影 ID を **preview**（長辺 `--max-edge`、既定 1600）と **full** で描画する（出力順は撮影 ID 順 → preview, full）。
  リクエストは既定に頼らず全項目を明示する: `profile=apple_default`・レンズ補正 ON・decoder `9`・
  `--frozen-preset`（既定 `neutral_recovery`）の Adjustments **全9キー**（契約の値域も検査する）。
  Adjustments を無視する古い CLI（0.3.0 未満）は、完全な基準にならないので作成前に拒否する。
- 書くもの（すべて `--out` 配下、path は `--out` からの相対）:
  `ci_frozen.json`（`rawpy_images.json` と同じ形式: `capture_id` / `mode` / `path` / `sha256` / `rgb8_sha256` /
  `dimensions` / `request`、加えて `source` = ARW のファイル名・サイズ・sha256）、`ci_frozen_provenance.json`
  （OS 版・CPU 種別・CLI のファイル名と sha256・`--version` / `--capabilities`・応答の CLI/OS/decoder 版・スクリプトの sha256・
  現像元ハッシュ・全リクエスト）、`bootstrap_report.json`、`run.log`、`coreimage/<mode>/*.jpg` と CLI 応答 JSON。
  CLI 応答の絶対パスはファイル名・相対パスへ置き換える。EXIF・GPS・絶対パスはマニフェスト・来歴に書かない。
- **安全規則**: `--out` は存在しないか空のディレクトリだけ（中身があれば何も変えずに拒否）。現像元・CLI と重なる場所、
  Git 管理下（`work/` 以外のリポジトリ内）も拒否する。描画は同じ親の一時ディレクトリ
  （`.<out名>.bootstrap-<時刻>-<pid>`）へ行い、全画像の描画・現像元不変の確認・マニフェストの自己照合（ハッシュ・寸法）に
  成功したときだけ `--out` へ改名して公開する。1 件でも失敗すれば `--out` には何も作らず（終了コード 1）、
  診断用に一時ディレクトリ（`bootstrap_report.json` あり・マニフェストなし）を残す。不要なら手で削除する。
  `--captures` の重複・不正 ID・ARW 欠落は描画前に拒否する。
- **併用できないオプション**は黙って無視せずエラー: `--frozen-manifest` / `--frozen-root` / `--frozen-verify-only` /
  `--frozen-capture` / `--select` / `--full` / `--no-lens` / 各評価モード / `--dry-run` / `--legacy-rawpy` など。
  `--source-root` / `--captures` / `--out` は必須（既定の出力先は使わない）。
- 作成後は通常の経路で使う: `--frozen-manifest "$FROZEN_ROOT/ci_frozen.json" --frozen-root "$FROZEN_ROOT" --frozen-verify-only` で照合、
  `--source-root` と `--cli` を付けて別の `--out` へ再比較（preview の長辺は `request.max_edge` から再現。
  同一環境なら ΔE76 0・完全一致率 1.0 を期待する）。
- 保管: 基準画像は撮影元の EXIF（GPS を含みうる）を引き継ぐため Git に入れない。**画像とマニフェスト・来歴は同じ
  ディレクトリのまま**、worktree の掃除で消えない場所（リポジトリ外の `$FROZEN_ROOT`）に置き、バックアップも同じ単位で取る。
  この基準は「作成時点の CI」の開始点であり、削除済みの旧 rawpy / 前回 CI 画像との回帰比較ができたことにはならない。

## OS 更新時の回帰確認（手順）

```sh
CLI=native/coreimage-cli/.build/release/geoframe-ci-develop
RAW=<repo>/work/raw          # 11 組（読み取り専用）
FROZEN=<baseline-checkout>                 # rawpy_images.json の path は work/baseline/... から

# 1. 旧 rawpy 基準（#107）の照合と、現在の CI との比較。CI 出力を次回用に凍結する
python scripts/compare_engines.py --frozen-manifest tests/fixtures/baseline/rawpy_images.json \
  --frozen-root "$FROZEN" --source-root "$RAW" --cli "$CLI" \
  --out work/frozen/ci-macos-<version> --frozen-capture work/frozen/ci-macos-<version>/ci_frozen.json

# 2. OS 更新後: 前回の CI 出力を基準に比べる（同一なら ΔE76 0・完全一致率 1.0）
python scripts/compare_engines.py --frozen-manifest work/frozen/ci-macos-<version>/ci_frozen.json \
  --frozen-root work/frozen/ci-macos-<version> --source-root "$RAW" --cli "$CLI" --out work/frozen/after-update
```

`work/` は gitignore 対象（実写・GPS を持ちうる画像を Git に入れない）。凍結 CI マニフェストを長く残すなら
凍結画像と一緒にリポジトリ外へ保管する。

2026-10-23 の実行（macOS 27.0、上記 CLI、#107 の 4 枚）: 照合 PASS（4 件）。現在の CI と rawpy 基準の
ΔE76 平均は DSC07402 preview 7.27 / full 9.35、DSC07423 preview 8.52 / full 10.22（エンジン差であり合否ではない。
CI は 1600×1067 / 6000×4000、rawpy は 1600×1069 / 6000×4008 で、2% 以内なので中央クロップで同寸化）。
凍結した CI 出力での再実行は 4 件とも ΔE76 0・完全一致率 1.0。

## Python 本体の削除後

- 既定経路（上の「既定」）はそのまま使える。必要なのは `requirements-dev.txt` と凍結 JSON / 凍結画像だけ。
- 残すテスト: `tests/test_compare_engines.py`、`tests/test_evaluate_candidates.py`、`tests/test_evaluate_raw.py`、
  `tests/test_freeze_baseline.py`、`tests/test_presets_fixture.py`、`tests/test_evaluation_support.py`、
  `tests/test_evaluation_independence.py`。旧経路を検証するテストだけが `local_raw` / `rawpy` の有無を見て skip する。

  ```sh
  .venv-eval/bin/python -m unittest tests.test_compare_engines tests.test_evaluate_candidates \
    tests.test_evaluate_raw tests.test_freeze_baseline tests.test_presets_fixture \
    tests.test_evaluation_support tests.test_evaluation_independence
  ```

- `tests/test_evaluation_independence.py` は削除後の状態を一時ディレクトリに再現する: 評価スクリプトと
  `scripts/evaluation/`・凍結 JSON だけをコピーし（`local_raw/` は置かない）、`sitecustomize` で `local_raw` /
  `rawpy` / `lensfunpy` の import を遮断したサブプロセスで、凍結比較（照合・欠落/改変/寸法/画素の失敗・
  CI 出力の凍結と再生・部分結果で上書きしないこと、`--frozen-bootstrap-ci` の作成→照合→再比較と空/新規でない出力先・描画失敗・
  重複 ID・併用不可オプションの拒否）、既定比較（rawpy 列なし）、候補集計、evaluate_raw、
  凍結監査・選定参照値・プリセット表、旧経路の明示エラーを実際に回す。Core Image CLI は偽 CLI。
- 旧経路が必要になったら、削除前のコミットを別の worktree に出して、その checkout の
  `requirements-raw.txt` の環境で明示フラグを付けて実行する
  （例: `git worktree add ../geoframe-legacy <削除前のコミット>`）。

## 残る制約

- `--diagnosis` の CFA 飽和（センサー段の帰属）、`--bracket`、`--adjustments` の実行は rawpy を使う旧経路だけ。
  既定の `--diagnosis` は CFA を計測せず、該当の帰属は `unverifiable` に寄る。
- 凍結 rawpy 画像は代表 2 枚 × preview / full の 4 枚だけ。11 枚すべての旧原寸基準ではない。
- 昼夜混在の `image_work` 13 ケースは素材待ち（`scripts/eval_cases/candidates_baseline.json` の定義は保持）。
- `candidate_metrics.py` の写しは旧 Python の値・ロジックの凍結であり、CI 候補の受け入れ基準ではない。
