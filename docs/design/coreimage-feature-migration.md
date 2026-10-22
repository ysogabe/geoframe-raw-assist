# rawpy 取りやめに伴う 4 機能（プリセット / PSO / ブラケット合成 / 星景）の移行設計

- 対象リポジトリ: geoframe-raw-assist（ブランチ `chore/74-coreimage-baseline` 時点のコードを読んで作成）
- 前提: ADR 0002（リポジトリ内 `docs/adr/0002-coreimage-engine-decision.md`）で
  「Core Image（CIRAWFilter、`apple_default`、decoder 9、レンズ補正 ON）を基本路線とし、移行完了後に rawpy を取りやめる」
  ことが決定済み。対応 OS は最新の macOS のみ。
- 関連: #72（プレビューの Core Image 化）、#73（Adjustments の Core Image 対応）、#70（夜景・星空・長秒比較）、
  #71（逆光ハイライト比較）、#77（decoder 9 neutral の平滑化・白点調査）、#74 / #75 / #76。

- 作成: 2026-10-20（設計は Fable、確認はメイン）。この設計から立てた Issue は下表。

| Issue | 内容 |
|---|---|
| #79 | Swift CLI: `tiff16` の sRGB 出力と、`apple_default` 下のパラメータ個別上書き（評価 PR に含める） |
| #80 | プリセット定義と Adjustments の解決を Rust（Tauri）へ移す（値は変えない） |
| #81 | プリセット値の Core Image 向け再調整と `highlight_recovery` の置き換え |
| #82 | Swift CLI のバッチモード |
| #83 | ブラケット合成を Swift CLI の合成モードとして実装 |
| #84 | アプリのブラケット合成を Core Image 経路へ切り替え |
| #85 | PSO の代理レンダを Core Image CLI に切り替え（足場） |
| #86 | PSO の画像指標・星判定・Pareto 選択の Rust 移植 |
| #87 | PSO の探索ループの Rust 移植と Tauri コマンド化 |
| #88 | 暗いシーンの固定倍率デコード（#50 経路）の扱い |
| #89 | （将来・任意）シーン参照（リニア）出力モード |
| #90 | rawpy の取りやめ |

本文中の「CLI バッチモード (C)」などの括弧付き記号は設計時の仮番号で、上表の Issue とは対応しない。

## 表記

- 【確認】: コード・ドキュメントを読んで確認した事実（ファイルパスと関数名を併記）。
- 【推測】: 設計上の見立て・未検証の想定。評価で確かめる必要があるもの。

---

## 0. 要点（先に結論）

| 機能 | 推奨移行先 | 一言の理由 |
|---|---|---|
| プリセット | Rust（Tauri）側に定義表と `resolve_adjustments` 相当を移し、**値は #73 の対応表確定後に再調整**する | 表そのものに rawpy 依存は無いが、全パラメータの「意味」が rawpy の処理段に束縛されている。`highlight_recovery` は CI に対応物が無く、置き換えレバーを決めないと救済系 3 プリセットが機能しない |
| PSO（現像候補自動生成） | **最後に移行**。Swift CLI に「バッチ/プロキシ現像モード」（1 プロセスで複数リクエスト、Adjustments 全量、長辺 640px 出力）を足し、探索ループと指標は Rust に移植する | 設計上の要（代理＝本番の後段関数を共有）を守るには、後段を CLI に移した後でなければ移植できない。移植前の中間期は Python を残す |
| ブラケット合成 | Swift CLI に「複数フレーム入力 → 位置合わせ → Exposure Fusion（Mertens）」を実装（Core Image + 自前 CIKernel/vImage）。入力は `apple_default` の **sRGB tiff16（新規）** で問題なし | Mertens は LDR の表示参照画像を前提にした手法で、現行も sRGB ガンマ後の値を入れている。リニア・NR 前のデータは不要。CI にはフレーム単位の自動明るさ（auto_bright）が無いため、露出差の保存は構造的に満たされる（#70 で検証） |
| 星景 | 専用モジュールは存在しない。(a) 暗所判定（Issue #50 の auto_bright 回避）は CI では不要になる、(b) `stars` スタイル・星判定は PSO と一緒に移植、(c) **CLI に `apple_default` 下の NR 個別上書き**を追加し #70 で星の残り方を確認 | rawpy の auto_bright が無い CI では #50 の問題自体が消える（要検証）。残る論点は `apple_default` の NR が星を消すかどうか。「スタック用のリニア出力」は現状の機能に無いため必須にしない |

**「rawpy を取りやめる」≠「Python を取りやめる」。** ADR 0002 の判断材料は「Python + .venv + localhost サーバ」を配布コストとして数えているが、
決定文は rawpy の取りやめまで。本設計では両方の読み方で成り立つよう、「rawpy だけ外す中間案」と「Python も外す最終形」を各機能で分けて示す。

---

## 1. 現状の全体像（rawpy 依存の所在）

【確認】`requirements-raw.txt`: numpy / Pillow / rawpy 0.27.0 / lensfunpy / opencv-python-headless 5.0 / google-auth 系。

【確認】rawpy を import するモジュール:

| モジュール | 用途 | rawpy 以外の重い依存 |
|---|---|---|
| `local_raw/engine.py` | `develop_raw`: `rawpy.imread` → `postprocess(**_postprocess_params)` → lensfun → 後段 | cv2（リサイズ・USM）、numpy、PIL、lensfunpy（`lens.py` 経由） |
| `local_raw/bracket.py` | `_decode_frame`: フレームごとに `rawpy.imread` → `postprocess(**_merge_postprocess_params)` | cv2（AlignMTB / MergeMertens）、numpy、PIL、lensfunpy |
| `local_raw/development_candidates.py` | `open_raw_half_decoder`: `rawpy.imread` → `postprocess(half_size=True)`（関数内 import） | numpy、PIL、cv2（任意。無ければ純 numpy フォールバック） |
| `local_raw/server.py` | `import rawpy` は `/api/health` の `rawpy_version` と起動ログのため（`handle_health`） | — |
| `local_raw/exif.py` | バージョン表記のみ（`SOFTWARE_TAG`）。未導入でも動く | — |

【確認】rawpy を使わないモジュール: `presets.py`（純 Python）、`analysis.py`（カメラ JPEG を PIL/cv2 で解析）、`export.py`、`photos_import.py`、`drive_upload.py`、`config.py`、`source_files.py`、`paths.py`。
これらは本設計の対象外だが、「Python を取りやめる」読み方をした場合に別途移行先が要る（本設計では触れない）。

【確認】Core Image 経路の現状:

- Swift CLI `native/coreimage-cli`（`Sources/CoreImageDevelop/Develop.swift` の `develop()`、`Request.swift` の `RequestParser`）。
  stdin JSON 1 件 → `CIRAWFilter` → JPEG（sRGB、ICC 埋め込み）または tiff16（**Display P3**）。1 プロセス 1 リクエスト。
  `settings` は `exposure_ev` / `neutral_temperature` / `neutral_tint` / `lens_correction` / `profile` のみ（未知キーはエラー）。
  `applyProfile()` は `neutral` のときだけ sharpness / contrast / detail / NR / moire / EDR / localToneMap を 0 にし、`apple_default` は既定値のまま。
  作業空間は extended linear sRGB（RGBAh）。
- Rust `src-tauri/src/coreimage.rs`: `develop()` が CLI を子プロセスで呼ぶ。固定値 `PROFILE="neutral"`、`DECODER_VERSION="9"`、`PREVIEW_MAX_EDGE=2400`、`FORMAT="jpeg"`。
  同時実行 1 本（`DevelopSlot`）、60 秒ウォッチドッグ、出力は `<app_data_dir>/coreimage/`。`GEOFRAME_COREIMAGE_EXPERIMENT=1` のときだけ有効。
  （この項は #72/#73 以前の状況。#72/#73 で既定が `apple_default` の Core Image プレビューに変わり、実験フラグは廃止、`GEOFRAME_PREVIEW_ENGINE=rawpy` で切り戻す形になった。）
- パラメータ対応表 `docs/coreimage-parameter-mapping.md`: `exposure`・`wb_temp` は「近似（換算必要）」、`shadow_lift` / `brightness` / `contrast` / `saturation` は「後段 CIFilter で再現可（sRGB ガンマ空間で演算が必要）」、
  `sharpness` / `noise_reduction` は「近似」、**`highlight_recovery` は「対応なし」**。

---

## 2. 共通論点

### 2.1 中間案「RAW デコードだけ Core Image、後段は Python」は成り立つか

【確認】現行の処理は 2 段に分かれている（`docs/coreimage-parameter-mapping.md` 原則、`engine.develop_raw`）:
デコード段（rawpy `postprocess` + lensfun）と、表示参照の後段（`engine.apply_post_decode_chain` = `_apply_shadow_lift_array` → `_resize_array` → `_apply_tone_array`）。
後段は numpy/cv2 のみで rawpy に依存しない。

したがって「CLI が tiff16 を書き、Python が読んで後段を掛ける」中間案は**技術的には成り立つ**。成立条件:

1. CLI の tiff16 を sRGB で出せること（現状は Display P3。後段の式は sRGB ガンマ空間の 0..1 を前提にしている）。→ CLI 拡張 (A)。
2. 後段の入力が「0..1 にクリップ済みの表示参照」であること。`apple_default` の JPEG/tiff16 出力はこれを満たす（【確認】`Develop.swift` は出力変換で sRGB/P3 に変換し 8/16bit 整数に落とす）。
3. `exposure` / `wb_temp` / `highlight_recovery` / `noise_reduction` はデコード段のパラメータなので、Python 側では再現できない。CLI に渡すか、意味を変えて吸収するか（#73 の範囲）。

欠点: rawpy の wheel は外れるが、numpy / cv2 / Pillow / lensfunpy（lensfun は CI のレンズ補正に置き換わるので外せる）/ `.venv` / localhost サーバはそのまま残る。
ADR 0002 が Core Image 優位とした「依存・配布」の利点はほとんど得られない。また 1 枚につき「CLI 子プロセス → 16bit TIFF 書き出し（原寸 6000×4000 で約 144MB）→ Python 読み込み」が挟まり、
ADR 0002 の速度・メモリ優位（CI 0.77〜2.15 s、RSS 約 290MB）も削られる【推測】。

結論: **中間案は移行期間の足場としてのみ使う**（例えば #73 の検証で「CI 後段と numpy 後段の結果比較」に使う、PSO 移植までの間 PSO が CI 現像の代理を評価するのに使う）。
最終形としては採らない。

### 2.2 後段処理（shadow_lift / brightness / contrast / saturation / USM）の置き場所

> **実装済み（#72/#73、2026-10-21）**: プレビューの Core Image 化と Adjustments 9 キーの写像が完了した。後段は下記の前提どおり Swift CLI 内の Metal カーネル（`geoframe_tone` / `geoframe_usm`）で、1 回のレンダに含めた（CLI 0.3.0。`highlight_recovery` は no-op で代替は #81）。写像は [設計書](coreimage-preview-adjustments.md) §3.3、較正結果は [比較レポート §16](../coreimage-engine-comparison.md)。原寸・書き出しはまだ rawpy 経由。以下の本文は設計時の記述で、§1 の「現状」も #72/#73 以前のもの。

4 機能のうちプリセット・PSO・ブラケット合成はすべて「Adjustments の後段」に依存している（【確認】`bracket.merge_bracket` も合成結果に `_apply_shadow_lift_array` / `_apply_tone_array` を 1 回掛ける。PSO は `render_proxy` → `engine.apply_post_decode_chain`）。
後段の置き場所は #73 で決まるが、本設計は次を前提にする:

- **後段は Swift CLI 内の CIFilter 連鎖として実装し、1 回のレンダで JPEG まで出す**（#73）。sRGB ガンマ空間での演算は `CILinearToSRGBToneCurve` → 演算 → `CISRGBToneCurveToLinear` で挟む（対応表の原則どおり）。
  輝度限定 USM（`_apply_tone_array` の Rec.709 輝度の高周波のみ加算、ISO 依存コアリング）は標準 CIFilter に等価物が無いため自前 `CIKernel`（Metal）になる【確認: 対応表「厳密には自前 CIKernel」】。
- CLI は Adjustments 全量（現行 9 キー相当。ただし `highlight_recovery` は #71 の結果で置き換えレバーを決める）を `settings` で受け、`applied` に実際の値を返す。

この前提が崩れる（例: 後段を Rust 側で画素処理する）場合でも、本設計の「どこに何を置くか」は変わらず「後段の実体がある場所に合わせる」だけで済む。

### 2.3 Swift CLI に追加すべき機能（複数の機能が依存するもの）

| ID | 追加機能 | 依存する機能 | 規模【推測】 |
|---|---|---|---|
| (A) | `tiff16` の色空間指定（`output_color_space: "srgb" \| "display_p3"`）。既定は互換のため P3 のまま | ブラケット合成（Python 中間案・評価）、#70/#71 の指標計算 | 小（`OutputColor.space(for:)` に分岐追加） |
| (B) | `apple_default` 下での NR・シャープ個別上書き（`settings.luminance_noise_reduction` / `color_noise_reduction` / `sharpness_amount` / `detail_amount` を number \| null で受け、null は既定値） | 星景（星を消さない NR）、#70、#77、PSO の `noise_reduction` 探索 | 小（`applyProfile` の `.appleDefault` 分岐に追加、`RequestParser.settingsKeys` 拡張、README 更新） |
| (C) | **バッチモード**: stdin から JSON Lines で複数リクエストを受け、1 プロセスで順に処理（`CIRAWFilter` インスタンスと `CIContext` を再利用）。応答も 1 行ずつ | PSO（代理を数十回レンダ）、ブラケット合成（フレーム複数） | 中（main.swift のループ化、1 リクエスト失敗時に継続する契約） |
| (D) | **合成モード**: `inputs: [...]`（2〜9 本）を受け、各フレームを同一 settings で現像 → 位置合わせ → Mertens 合成 → 後段 → JPEG | ブラケット合成 | 大（§3.3） |
| (E) | （任意・将来）**シーン参照出力**: boost / NR / sharp を 0 にし、extended linear sRGB のまま 16bit/half TIFF に書く | 星景のスタック機能を将来作る場合のみ。現状機能に依存先は無い | 小〜中。**本移行の必須項目にはしない** |
| (F) | Adjustments 後段（§2.2） | プリセット・PSO・ブラケット合成 | #73 の本体 |

---

## 3. 機能別の設計

### 3.1 プリセット

#### 現状

【確認】

- 定義: `local_raw/presets.py` の `PRESETS`（11 種。`neutral_recovery` / `landscape_clear` / `nightscape` / `sunset_glow` / `indoor_warm` / `portrait_soft` / `monochrome_street` / `highlight_rescue` / `shadow_rescue` / `backlit_rescue` / `vivid_travel`）、
  `ADJUSTMENT_RANGES`（値域）、`DEFAULT_PRESET = "neutral_recovery"`。契約は `docs/API_CONTRACT.md` §2 / §5。
- 解決: `presets.resolve_adjustments(preset_name, overrides)` がプリセット既定値に上書きをマージし、未知キーを拒否、数値を値域へクランプ、float に正規化。`engine.develop_raw` / `bracket.merge_bracket` / `development_candidates.generate_development_candidates` の 3 箇所から呼ばれる。
- 配信: `presets.preset_infos()` → `server.handle_health`（`GET /api/health` の `presets`）→ フロント `src/lib/types.ts` の `PresetInfo` / `HealthResponse` → `DevelopControls.tsx`（プリセット選択とスライダー初期値）、`App.tsx` の `activePreset(state)`。
  フロントは `PresetName = string` で名前の一覧を持たず、`/api/health` から動的に取る（types.ts のコメント）。
- 依存ライブラリ: 無し（純 Python）。
- 値の意味は rawpy の処理段に束縛されている: `exposure` は auto_bright 正規化後の `bright = 2**ev`（暗所は `5.0 × 2**ev`）、`wb_temp` はカメラ WB の R/B ゲイン比 ±0.3%/単位、
  `highlight_recovery` は 0 / <75 / ≥75 で rawpy `HighlightMode` を 3 段選択、`noise_reduction` は FBDD の 3 段（`engine._postprocess_params`、`_fbdd_mode_for`）。
  後段 5 項目は `engine._apply_shadow_lift_array` / `_apply_tone_array` / `_capture_sharpen_params` の式。

#### 選択肢

| 案 | 内容 | 利点 | 欠点・リスク | 工数【推測】 |
|---|---|---|---|---|
| P1 | 定義表と `resolve_adjustments` 相当を **Rust（Tauri）に移す**。`get_presets` コマンドで `PresetInfo[]` を返す | Python 無しで UI が動く。値域クランプ・未知キー拒否を Rust で一元化し CLI に渡す前に検証できる | 表を 2 箇所（Python / Rust）に持つ期間が生じる。契約（API_CONTRACT §5）の置き場所を変える必要 | 小 |
| P2 | 定義表を **JSON リソース**としてアプリに同梱し、Rust / Python 双方がそれを読む | 移行期間中の二重定義を避けられる。値の再調整が JSON 編集だけで済む | 契約の「name とキー構成は凍結」を JSON スキーマで守る仕組みが要る。既存テスト（`tests/test_raw_engine.py` 等）がモジュール定数を参照していれば読み替えが要る | 小〜中 |
| P3 | **TypeScript 側**に表を持つ | 配信経路が最短 | 値域クランプ等をフロントに持つのは契約と逆（現状「サーバ側で reject」）。CLI/Rust 側の検証が二重になる | 小 |
| P4 | Python を残す（`/api/health` だけ使う） | 変更ゼロ | Python を取りやめる最終形と矛盾 | 0 |

#### 推奨: P1（Rust に移す）。移行期間は P2 的に「JSON を正とし Python の `PRESETS` もそこから読む」としてもよい

理由: プリセット値の検証（クランプ・未知キー拒否）は「CLI に渡す前の最後の関門」として Rust 側に置くのが自然で、PSO（§3.2）とブラケット合成（§3.3）の Rust 側実装も同じ検証を使う。

**ただし値（数値）はこの段階では移さない。** 次の順で進める:

1. **構造の移行**（名前・ラベル・説明・キー構成）を Rust に移す。値は現行のまま置く。
2. **#73 で各 Adjustments の CI 側の意味が確定**したら、11 プリセットの値を「現行 rawpy 出力と見た目が揃う」よう再調整する。
   特に `exposure`（基準が auto_bright 正規化後 → 固定の baselineExposure に変わる）と `wb_temp`（R/B 比 → K/tint 換算）は同じ数値で同じ結果にならない（【確認】対応表）。
3. **`highlight_recovery` の扱いを決める**（#71 の結果待ち）。CIRAWFilter にクリップ再構成方式を選ぶ API は無い（【確認】対応表「対応なし」）。候補は
   (i) `extendedDynamicRangeAmount` / `boostAmount` / `boostShadowAmount` の組み合わせを「ハイライト保持」レバーとして再定義する、(ii) 後段 `CIHighlightShadowAdjust.highlightAmount`、(iii) パラメータを廃止し救済系プリセットは `exposure` を下げ `shadow_lift` を上げる構成に書き換える。
   `highlight_rescue`（90）/ `backlit_rescue`（80）/ `sunset_glow`（70）はこの決定無しには同じ機能を持てない。
4. `noise_reduction`（off / light / full）は CI の連続量に対応表が無い（【確認】対応表「3 段と連続量の対応表は未定」）。CLI 拡張 (B) で個別上書きできるようにしたうえで、ISO 帯ごとに 3 段 → (luminance, color) の表を #73 で決める。
   `nightscape`（full）/ `shadow_rescue`（full）の見え方が変わる。

`apple_default` 出力を入力にして問題ないか: プリセットは「パラメータの集合」なので入力画像の問題は無い。問題は上記のとおり「同じ数値が同じ意味を持たない」点に尽きる。

#### 評価で確かめること（#73 と #71 に渡す）

- 11 プリセットそれぞれで、現行 rawpy 出力と CI（再調整後）出力の輝度平均・p1/p99・ΔE76 を比較ハーネス（`scripts/compare_engines.py` の `image_metrics`）で測り、ΔE76 中央値が現行 rawpy vs カメラ JPEG の差（比較レポート §4）を超えないこと【推測: 合否の目安。#75 で正式に決める】。
- 救済系 3 プリセットについて、#71 の逆光セットで白飛び率（`clip_high_any`）が現行 rawpy 以下であること。

### 3.2 PSO（現像候補自動生成）

#### PSO とは何か【確認】

`local_raw/development_candidates.py`。**粒子群最適化（Particle Swarm Optimization）による多目的探索で、「現像候補（Adjustments の提案）」を最大 5 つ返す**機能。
`POST /api/development-candidates`（`server.handle_development_candidates`）→ フロント `raw-api.ts: generateDevelopmentCandidates` → `App.tsx: runDevelopmentCandidates` → `DevelopmentCandidates.tsx` にカード表示。
「適用」で `candidates/apply`（`src/lib/app-state.ts`）がその候補の Adjustments をスライダーの上書き値（`state.overrides`）にする。**候補を選んでも現像は実行されない**（docstring）。

処理の流れ:

1. `_resolve_source_paths` で ARW とペア JPG を解決、`read_capture_exif` で EXIF。
2. `open_raw_half_decoder`: `rawpy.imread` を 1 回開き、`engine._postprocess_params(...)` に `half_size=True` を足して `postprocess`。
   キー `(highlight カテゴリ, noise_reduction, exposure)` ごとにデコードし `DecodeCache`（LRU 24 件、長辺 1280px）に保持。暗所判定（`engine.detect_dark_scene`）も engine と同じ。
3. `render_proxy` → `engine.apply_post_decode_chain(decoded, adjustments, 640, iso=...)`。**本番と同じ後段関数**を使う（docstring: 以前の独自トーン式の代理は本番を予測できず作り直した）。レンズ補正は省略。
4. `_image_metrics`: 白飛び / 黒つぶれ / 平坦部ノイズ / ディテール / ハロー / 色クリップ / トーンレンジ / 基準との差 / 暗部平均 / 星指標。
5. `_optimise_style`: スタイル（`stars` / `foreground` / `low_noise` / `social`。`natural` はプリセット値そのまま）ごとに PSO。
   探索次元は `SEARCH_KEYS` の 7（exposure / shadow_lift / brightness / contrast / saturation / sharpness / highlight_class）。粒子数 `min(8, max(4, budget//2))`、`budget` 既定 48（32..512）。
   `pareto_front` で非劣解を取り、`_utility` でリーダー選択、`_guard_failure` で基準（natural）より白飛び・黒つぶれが 10pt 以上悪化した候補を除外。
   exposure は 0.5EV 格子にスナップ（`EXPOSURE_GRID`。デコード回数を抑えるため）。`highlight_class` は 3 カテゴリ（Clip / Blend / Reconstruct）。`noise_reduction` はスタイルごとに `_choose_noise_reduction` で固定。
   `_SHARPNESS_SEARCH_MAX = 1.47` は engine の USM 定数（`SHARPEN_GAIN_MAX` / `SHARPEN_GAIN_LARGE` / `SHARPEN_SLIDER_SLOPE`）から導出される（assert あり）。
6. 星判定 `detect_star_scene` / `_star_measurements`（§3.4）。

依存: numpy、PIL、cv2（`connectedComponentsWithStats` / `GaussianBlur`。無い環境向けの純 numpy フォールバックあり）、rawpy（engine 経由）。
評価スクリプト `scripts/evaluate_candidates.py`（代理と本番現像の指標のずれを集計）、ケース定義 `scripts/eval_cases/candidates_baseline.json`（昼夜ミックス 13 ケース + 星空 11 ケース。星空の実体 `work/raw` はこの worktree には無い）。
テスト `tests/test_development_candidates.py`（`decoder_factory` で合成デコーダを注入）。

#### 移行の難所

- **代理と本番の一致が設計の要。** 後段を CLI に移す（#73）と、Python の numpy 後段は本番ではなくなる。PSO を Python に残したまま後段だけ CLI に移すと「代理が本番を予測しない」状態に戻る。
- CI の `exposure` はデコーダ内（トーンカーブの前）で効くため、後段の乗算では模擬できない。exposure を変えるごとにレンダが要るのは rawpy と同じ。
- `highlight_class` は CI に対応物が無く探索次元から消える（#71 で決める代替レバーが入るかもしれない）。
- `noise_reduction` は連続量 2 つ（luminance / color）になる。探索次元に入れるか、スタイルごとの固定表にするかを決める。
- 1 回のレンダコスト【推測】: CI preview 2400px が 0.77〜1.30 s（比較レポート）。640px の代理なら `scaleFactor` が小さく速いはずだが未計測。`budget` 48 を 4 スタイルに配分すると 1 スタイル 12 評価。
  exposure 格子 9 値 × NR 固定 1 で最悪 9 レンダ/スタイル、全体で数十レンダ。1 プロセス 1 リクエストのままだと起動オーバーヘッドが支配的になるため、バッチモード (C) が必須。

#### 選択肢

| 案 | 内容 | 利点 | 欠点・リスク | 工数【推測】 |
|---|---|---|---|---|
| S1 | **Rust に PSO・指標・星判定を移植**し、レンダは CLI バッチモード (C) で `apple_default` + Adjustments（#73 後段込み）の 640px 出力を得る | 代理 = 本番の CLI と同じコードパス。Python 不要。Tauri 側で同時実行制御・キャッシュを一元化 | 移植量が大きい（約 1,100 行の Python。numpy 相当の画像統計を Rust で書く。`image` crate 等で足りる）。CLI 往復のオーバーヘッド | 大 |
| S2 | **Swift CLI 内に PSO を実装**（`candidates` モード） | レンダと評価が同一プロセス。`CIImage` のまま指標を取れる（`CIAreaHistogram` 等） | 探索ロジックと画像統計を Swift で書く。CLI が「現像器」から「最適化器」に膨らむ | 大 |
| S3 | **Python に PSO を残し**、レンダだけ CLI（中間案） | 移植不要。後段が CLI に移っても代理が本番と一致する | Python/.venv/サーバが残る。CLI 子プロセス → 16bit TIFF → Python 読み込みが数十回 | 中 |
| S4 | **機能を縮小**（PSO を廃止し、スタイルごとの固定オフセット `_style_target` を候補として返す） | 実装が小さい | 「画像を見て選ぶ」価値が消える。`rejected` の安全策も失う | 小 |

#### 推奨: S3 を**移行期間の足場**にし、最終形は S1

- 順序: #73（CLI に Adjustments 後段）→ 中間案 S3 で PSO の代理を CLI レンダに切り替え（`decoder_factory` の差し替えで済む構造になっている【確認: `generate_development_candidates(decoder_factory=...)`】）→ `scripts/evaluate_candidates.py` で代理と本番のずれを再計測 → S1 で Rust へ移植。
- S1 の分割: (1) 星判定・画像指標（`_image_metrics` / `_star_measurements` / `pareto_front`）を Rust の純関数として移植し、Python の出力と数値一致テストを作る。(2) 探索ループ（`_optimise_style`）を移植。乱数は決定的（`seed` → `_seed_for_style` の SHA-256）なので、同じ seed で同じ粒子列になることをテストできる。(3) `POST /api/development-candidates` を Tauri コマンド `generate_development_candidates` に置き換え、応答型 `DevelopmentCandidatesResponse` は変えない。
- 探索空間の変更: `highlight_class` を外す（#71 の結果で代替レバーが決まれば入れ直す）。`noise_reduction` は (B) の連続量 2 つをスタイルごとの固定表にする（探索しない。現行と同じ方針）。`_SHARPNESS_SEARCH_MAX` は #73 で決まる USM 定数から導出する。
- `apple_default` 出力を入力にして問題ないか: PSO の代理は現行も「表示参照の sRGB、デモザイク済み」を入力にしており、デコーダのシャープ・NR が掛かっていること自体は問題にならない。
  ただし**指標の絶対値は変わる**（`noise_flat`・`detail`・`halo`・`star_count` のしきい値は rawpy 出力で調整されている。例: `_candidate_style_ids` の `noise >= 0.045`、`_image_metrics` の `detail` 係数）。
  `apple_default` は NR 既定で ISO 3200 の輝度 NR が v9 で 0.241（対応表）なので、`low_noise` スタイルの発動条件や `noise_flat` の優位が変わる【推測】。移植時に `evaluate_candidates.py` 相当で再調整が必要。

### 3.3 ブラケット合成

#### 現状【確認】

`local_raw/bracket.py`（契約 `docs/API_CONTRACT.md` §1 `POST /api/merge-bracket`・§4）。`server.handle_merge_bracket` → フロント `raw-api.ts: mergeBracket` → `App.tsx: runMerge`（`BracketMerge.tsx` でフレーム選択、2〜9 枚）。結果は `ExportPanel` で `variant: "merged"` として書き出し対象（`export.py: _validate_merged_filename`、`MERGED_ROOT`）。

処理順（docstring「品質を決めるので変更しないこと」）:

1. `_decode_frame` → `_merge_postprocess_params`: **`no_auto_bright=True`**、`bright` / `exp_shift` / `highlight_mode` を渡さない（露出差を保つ）。`wb_temp` と FBDD（`noise_reduction`）は全フレーム同一に適用。AHD / sRGB / 16bit、`half_size=False`。
2. レンズ補正（lensfun、`_apply_group_lens_corrections`）: **全フレームに掛けるか 1 枚も掛けないか**（歪曲補正済みと未補正が混ざると位置合わせで取れないゴーストになる）。焦点距離・絞りがフレーム間で違えば全フレーム無補正。
3. 位置合わせ `_align_frames`: `cv2.createAlignMTB(max_bits)`（`_align_max_bits` で短辺の 2% に収める）。入力は float32×255。失敗しても合成は続行（`aligned=False`）。
4. 合成 `_merge_frames`: `cv2.createMergeMertens().process(...)`（Exposure Fusion。HDR + トーンマップは不採用）。有限性確認後 0..1 にクリップ。
5. `_resize_array(max_edge)` → `_apply_shadow_lift_array` → `_apply_tone_array`（engine と共有。**engine は lift → resize の順、bracket は resize → lift の順**で異なる）。`exposure` / `highlight_recovery` は no-op。
6. 保存: EXIF は先頭フレーム（ソート後）から継承（`_resolve_save_kwargs`）、sRGB ICC。本合成 `outputs/merged/`、プレビュー `outputs/merged_preview/`（原寸かプレビューかをディレクトリで区別する契約）。
7. キャッシュキー `_merge_params_hash`: ソート済み capture_ids、adjustments、preset、max_edge、preview、lens_applicable、ALIGN_VERSION、source_root、各ライブラリ版（rawpy / Pillow / OpenCV）。レシピ JSON に `aligned`・`lens_correction`・`frames[]` を残す。

依存: rawpy、cv2（AlignMTB / MergeMertens / resize / GaussianBlur）、numpy、PIL、lensfunpy。テスト `tests/test_bracket.py`（`_decode_frame` を差し替えて合成段だけを検証）。

#### `apple_default` 出力を入力にして問題ないか

- **Mertens（Exposure Fusion）は LDR 画像の重み付き合成であり、入力はリニアでなくてよい。現行実装も sRGB ガンマ後の 0..1 を入れている**（【確認】`_decode_frame` は `output_color=sRGB` の 16bit を 0..1 にしただけ。リニア化していない）。
  コントラスト・彩度・露出適正度の重みは表示参照の値で計算するのが原法（Mertens 2007）どおり。したがって `apple_default` の sRGB 出力は**入力として適切**であり、リニア・NR 前のデータは必要ない。
- 必要なのは「フレーム間の露出差が保存されていること」。rawpy では auto_bright を切る必要があった（docstring「本モジュールの要」）。CI にはフレーム単位の自動明るさが無く `baselineExposure` はファイル固定値（【確認】対応表「自動明るさ: 対応なし」）なので、構造的に露出差は保たれる【推測: `apple_default` の内部処理（boost・NR）が ISO やシーンに適応する可能性は否定できないため #70 で検証】。
- NR・シャープが掛かっていることの影響【推測】: 各フレームに同じデコーダ NR が掛かるだけなので、合成の破綻要因にはならない。ただし NR の強さが ISO で変わるため Auto ISO のブラケットではフレーム間で NR 量が違いうる（現行の FBDD も ISO 800 境界で同じ問題を持つ。docstring の「既知の制約」）。
- レンズ補正: CI は内蔵でフレームごとに同じ補正が掛かる（同一レンズ・焦点距離なら）。lensfun の「全か無か」機構は不要になるが、**幾何がフレーム間で一致していることは #70 で確認する**（CI の補正は中身非公開）。
- 色空間: 合成の後段（shadow_lift 等）は sRGB 前提なので、tiff16 を使う経路では sRGB tiff16 (A) が要る。CLI 内で完結する案 (D) なら作業空間からの変換を 1 回にできる。

#### 選択肢

| 案 | 内容 | 利点 | 欠点・リスク | 工数【推測】 |
|---|---|---|---|---|
| B1 | **Swift CLI に合成モード (D)**: フレームを `CIRAWFilter` で現像（同一 settings）→ 位置合わせ（Vision `VNTranslationalImageRegistrationRequest`、または MTB を自前実装）→ Mertens（ラプラシアンピラミッド。`CILanczosScaleTransform` / `CIGaussianBlur` と自前 CIKernel で組む）→ 後段（#73 と共有）→ JPEG | 依存は OS フレームワークのみ。16bit 中間ファイル不要。1 プロセスで完結しメモリ・時間が読める | Mertens の自前実装（OpenCV と同じ結果にはならない。重み・ピラミッド段数を合わせる）。Vision の位置合わせは MTB と性質が違う（MTB は露出差に強い。Vision は輝度差のある画像で失敗しうる【推測】→ 画像を正規化してから登録するか、MTB を自前実装） | 大 |
| B2 | **Rust 側で合成**: CLI (A) で各フレームを sRGB tiff16 に書き、Rust で読んで Mertens（自前実装、`image` crate）と MTB → 後段（Rust で画素処理） | Swift の負担が増えない。テストしやすい | 後段を Rust にも持つことになり #73 と二重実装。16bit TIFF 読み込み（原寸 9 枚で約 1.3GB の中間データ）。`opencv` crate は配布が重く避けたい | 大 |
| B3 | **Python に合成を残す**（CLI (A) の sRGB tiff16 を `_decode_frame` の代替で読む中間案） | `_decode_frame` の差し替えだけで動く（テストもその構造）。OpenCV の実績ある MTB / Mertens をそのまま使う | Python / cv2 / .venv / サーバが残る。中間 TIFF の書き出し・読み込みが追加 | 小 |
| B4 | **機能を廃止** | — | 分析が `exposure_bracket` を検出し推薦理由に出している（`analysis.raw_recommendation`）。書き出し `variant: "merged"` の契約も消える | 小（削除）だが機能後退 |

#### 推奨: B3 を足場にして B1 へ

- 足場 B3 で先に確認できること: `apple_default` フレームを Mertens に入れたときの見え方、露出差の保存、CI レンズ補正の幾何一致。これは #70 の長秒ブラケット評価と同じ作業なので、評価 PR の中で B3 相当のスクリプトを作る（`scripts/compare_engines.py` の拡張）のが効率的。
- B1 の実装分割: (1) CLI バッチモード (C) で N フレームを同一 settings で現像し `CIImage` を保持。(2) 位置合わせ: まず MTB（Median Threshold Bitmap。中央値閾値ビットマップと排他的論理和による平行移動探索）を Swift で実装。現行の `ALIGN_MAX_SHIFT_RATIO = 0.02` と同じ上限を持たせる。(3) Mertens: 重み（コントラスト = ラプラシアンの絶対値、彩度 = RGB の標準偏差、露出適正 = ガウス（σ 0.2、中心 0.5））、ピラミッド段数は OpenCV の実装に合わせる【推測: 重みの定義・σ・段数は原論文と OpenCV の実装に基づく値で、このリポジトリ内で確認したものではない。実装時に OpenCV のソースで確認する】。OpenCV の出力と PSNR を測り目視で差が無いことを確認。(4) 後段と保存は #73 の実装を共有。(5) Rust 側で `merge_bracket` コマンド、`outputs/merged` 相当のディレクトリ分離（本合成 / プレビュー）、レシピ JSON、EXIF 継承（CLI は EXIF を書かない契約なので、Rust で先頭フレームのペア JPG から継承するか、CLI に `inherit_exif_from` を足す。GPS IFD はカメラ JPG からそのまま継承し、アプリが新たに書き込むことはしない。2026-10-22 オーナー確定、ADR 0003）。
- 契約の維持: `MergedResult` の形（`aligned`、`lens_correction`、`exif_inherited`、`frames`、`params_hash`）、2〜9 枚、`_require_uniform_shapes`（向き混在の拒否）、ソート済み先頭 ID を基準フレーム・EXIF 継承元にする規則。

### 3.4 星景（星景向けの処理）

#### 現状【確認】: 星景専用のモジュールやスタック（複数枚加算）機能は存在しない。星景向けの処理は 3 箇所に分散している

1. **暗いシーンのデコード方式**（`local_raw/engine.py`、ENGINE_VERSION 17、Issue #50）: `detect_dark_scene` が EXIF（ペア JPG → RAW サムネイル）から `ev100` を計算し、`DARK_SCENE_MAX_EV100 = 6.0` 以下なら LibRaw の auto_bright をやめて固定倍率 `DARK_SCENE_BRIGHT_K = 5.0 × 2**exposure` にし、ビネット補正を強制スキップ（`apply_lens_correction(force_skip_vignetting=True)`）。
   目的は「auto_bright が黒い空まで灰白色に持ち上げる」「周辺減光補正が暗部ノイズと色ムラを増幅する」ことの回避。結果はレシピ `dark_scene` に残り、`params_hash` にも倍率が入る。
2. **現像候補の `stars` スタイルと星判定**（`development_candidates.py`）: `_star_measurements` / `detect_star_scene`（EXIF が主信号: 露光 ≥ 2 s かつ EV100 ≤ −2 → 点光源密度 ≥ 0.0008 で星空。大面積光源があれば除外。EXIF 不明時は暗背景率 ≥ 0.72 等の厳しい画像条件）。
   星指標 `star_count` / `star_halo` / `star_color_saturation`。`stars` スタイルは `_choose_noise_reduction` で「ノイズが少なければ NR off、高 ISO なら light」、目標オフセット `_style_target["stars"]`（exposure +0.08、shadow_lift +8、彩度・シャープ微減）、目的関数に `star_loss`（基準より星が減ると罰）。
   フロントは `StarSceneInfo` を `candidate-view.ts: summarizeStarScene` で表示。
3. **プリセット `nightscape`**（夜景ノイズ低減。NR full、wb_temp −3）。

比較ハーネス `scripts/compare_engines.py: classify_scene` に `star` カテゴリ（露光 ≥ 2 s または EV100 ≤ 0）があるが、比較レポート §2 では **0 件**。星空サンプル 11 組は `scripts/eval_cases/candidates_baseline.json` の `stars` ルートとして参照されている（実体はこの worktree に無い）。

依存: numpy、cv2（任意）、rawpy（engine 経由）。

#### 移行で何が変わるか

- **(1) 暗所判定は CI では不要になる見込み【推測】。** CI には auto_bright が無く、`baselineExposure` はファイル固定値。したがって「auto_bright で空が灰白色になる」問題は CI では起きないはず。
  ただし CI の表示用トーンカーブ（`boostAmount` 1.0 / `boostShadowAmount` 0.9 / `shadowBias` 3.0。【確認】CLI README）が暗部をどう扱うかは非公開で、「暗い空が浮く／沈む」方向の別の問題がありうる。→ #70 の最重要観点。
  ビネットの強制スキップも CI の `isLensCorrectionEnabled` は周辺減光を含むか・分離できるかが不明（【確認】対応表「中身は非公開」）。星空で周辺減光補正がノイズを増幅するかを #70 で見る。
- **(2) 星判定・`stars` スタイルは PSO と一体で移植**（§3.2 S1）。しきい値は `apple_default` 出力で再調整が要る【推測】（NR で微光星が消えると `star_count` の基準値そのものが下がる）。
- **(3) `nightscape` プリセット**は §3.1 の値再調整の一部。NR full → CI の (luminance, color) の対応を決める。
- **最大のリスクは `apple_default` の NR が星を消すこと**（ADR 0002「置き換えに進む条件」でも最重要）。対策として CLI 拡張 (B) を入れ、「`apple_default` のトーン・色はそのまま、NR だけ 0 または弱」という設定を評価・提供できるようにする。
  星空判定（EXIF 主信号。`detect_star_scene` のロジックは EXIF だけでも概ね決まる）を Rust に先行移植し、星空なら NR を弱める「自動星空モード」をプリセットとは別の軸で持つ案もある【推測: 要オーナー判断】。

#### 「星景のスタックにはリニア・NR 前のデータが望ましい」について

現状のアプリにスタック（複数枚の加算平均・比較明合成）は無く、星景処理の入力は 1 枚の表示参照画像で足りている。したがって**本移行ではシーン参照（リニア）出力を必須にしない**。
将来スタックを作る場合に備え、CLI 拡張 (E)（boost 0・NR 0・sharp 0・extended linear sRGB・16bit または half float TIFF）を任意項目として記録しておく。
その場合でも Exposure Fusion（§3.3）とは入力の種類が違うことに注意する（Mertens は表示参照、スタックはシーン参照）。

#### 選択肢（星景として独立に選ぶもの）

| 案 | 内容 | 利点 | 欠点 | 工数【推測】 |
|---|---|---|---|---|
| N1 | 暗所判定を**廃止**し、CI の既定に任せる | 単純。ENGINE_VERSION 17 の特殊経路が消える | CI の暗部挙動が悪ければ代替が無い | 小 |
| N2 | 暗所判定を **Rust に移し、星空なら CLI (B) で NR を弱める／lens_correction を切る**自動設定にする | #50 の目的（空の色・ノイズ）を CI でも守れる | 「自動で設定を変える」振る舞いはユーザーに見える形（レシピ・UI 表示）にする必要。しきい値の再調整 | 小〜中 |
| N3 | 星景専用のシーン参照出力 (E) とスタック機能を新設 | 本格的な星景処理 | 新機能であり移行の範囲外 | 大 |

推奨: **N1 を基本に、#70 の結果次第で N2**。N3 は別 Issue（将来）。

---

## 4. 評価 PR（#70 / #71 / #77）に含めるべき観点

評価は `scripts/compare_engines.py` を拡張して行う（既存の `image_metrics` / `flat_noise` / `delta_e76` / `edge_rise_width` と等倍クロップシートの流儀を踏襲）。
バリアントは ADR 0002 の基本路線 `ci_apple_v9_l1` を中心に、比較対象として rawpy 現行・カメラ JPEG・`neutral` v9 を並べる。CLI 拡張 (B) が入れば `apple_default + NR 0` も加える。

### 4.1 #70 夜景・星空・長秒（本設計からの追加観点）

**データ**: 星空 11 組（`candidates_baseline.json` の `stars` ルート）、長秒の三脚ブラケット（コード中のコメントに「露光 1〜10 s・EV100 +0.3〜+3.6」の実例がある。`development_candidates.py` 冒頭の定数コメント）、夜の手持ち高 ISO。
`classify_scene` の `star` / `night` / `dark` カテゴリで `--select auto` が拾うようにサンプルを `--source-root` に含める。

| 観点 | 指標・手順 | 合否の目安【推測】 |
|---|---|---|
| 星が消えないか | 各出力に `development_candidates.detect_star_scene` と同じ検出（局所コントラスト ≥ 0.04 の小連結成分）を掛け、`star_count` をカメラ JPEG・rawpy 現行・CI 各バリアントで比較。等倍クロップで微光星の有無を目視 | CI `apple_default` の `star_count` が rawpy 現行の 0.9 倍以上。NR 0 との差で「NR が消した星の数」を出す |
| 星のハロー・色 | `star_halo`（星周辺リングの平均輝度比）、`star_color_saturation`（星ごとの白飛び割合）。星の色（青白〜橙）が残っているか等倍目視 | rawpy 現行以下 |
| 空の明るさ（#50 の再発確認） | 空領域（星検出成分を除いた上位暗部）の平均輝度・p50 をカメラ JPEG と比較。「灰白色に浮く」「黒く沈む」の両方向を見る | 輝度差がカメラ比 ±0.05（比較レポートの明るさ指標と同じ尺度） |
| 空のノイズ・塗りつぶし | 空領域の `flat_noise`、色ノイズ（a*b* の標準偏差）、等倍クロップで「水彩・蝋状」の塗りつぶしの有無（§9.6 と同じ観点） | 塗りつぶしが無いこと（目視）。`flat_noise` は rawpy 現行以下 |
| 白点ノイズ（§9.6 の再確認） | `apple_default` v9 lens ON のプレビュー（2400px）で、暗部に孤立した 1〜2 px の高輝度点を数える（星と区別するため EXIF 上の星空で星候補を除いた領域で） | 0 件に近いこと |
| 周辺減光補正の影響 | lens ON / OFF の差分で、四隅の輝度ゲインとノイズ増加を測る（rawpy は暗所でビネット補正をスキップしている） | 四隅の `flat_noise` 増加が中央の 1.5 倍以内 |
| **長秒ブラケットの露出差保存**（ブラケット合成の前提） | 同一ブラケット内の各フレームの平均輝度（sRGB → リニア化後）の比が EXIF の露出差 2^ΔEV に沿うか。rawpy `no_auto_bright=True` の結果と並べる | リニア輝度比が 2^ΔEV の ±0.3 stop 以内（`apple_default` の適応処理が露出差を潰していないこと） |
| **CI レンズ補正の幾何一致**（ブラケット合成の前提） | ブラケット各フレームを CI lens ON で現像し、四隅と中央の小パッチで位相相関によるシフトを測る。AlignMTB のシフトと比較 | 四隅のシフトが中央のシフトと 1 px 以内で一致（放射状の不一致が無い） |
| 合成の目視 | 各フレームの CI sRGB tiff16 (A) を OpenCV Mertens に入れた結果を、rawpy 現行の合成と並べて等倍クロップで比較 | ゴースト・黒帯・ハロー無し |

### 4.2 #71 逆光・ハイライト（本設計からの追加観点）

| 観点 | 指標・手順 | 本設計との関係 |
|---|---|---|
| 白飛び率 | `clip_high_any`（いずれかのチャンネル 255）と**チャンネル別**クリップ率 | `highlight_recovery` 廃止可否 |
| 境界の偽色 | クリップ領域の外側 3 px の帯で色相ヒストグラム（マゼンタ寄りの画素率）。rawpy の偽色ドットとの比較 | — |
| EDR / boost の掃引 | `apple_default` で `extendedDynamicRangeAmount`（0 / 0.5 / 1.0）、`boostAmount`（0.6 / 0.8 / 1.0）、`boostShadowAmount` を変えたときの白飛び率・中間調の変化。CLI に上書きキー（(B) と同じ流儀）が必要 | **`highlight_recovery` の置き換えレバー**を決める材料。救済系 3 プリセットの再設計に直結 |
| `CIHighlightShadowAdjust` | 後段で `highlightAmount` を掛けたときの白飛び率と局所コントラストの変化 | 置き換えレバーの候補 (ii) |
| プリセット `highlight_rescue` / `backlit_rescue` の再現 | 現行 rawpy のプリセット出力と、置き換えレバーで作った CI 出力の白飛び率・黒つぶれ率・輝度平均 | §3.1 の値再調整の合否 |

### 4.3 #77（decoder 9 neutral の平滑化・白点）に含めてほしい観点

- `apple_default` で NR だけを 0 にした設定（(B)）で白点が出るか。出るなら白点はデコーダ段のもので `apple_default` の NR が隠しているだけ、ということになり、**星景で NR を切る案（§3.4）は使えない**。出ないなら NR 0 は安全。
- 白点の発生が lens ON のときだけか（比較レポート §9.6 の観察）。周辺減光補正のゲインとの関係。
- decoder 8 の `apple_default` では出るか（decoder 9 必須の依存を外せるかの判断材料。ADR 0002 は decoder 9 を必須としている）。

---

## 5. 依存関係と実施順序

```
#70 星空・長秒評価 ──┐                     ┌─ プリセット値の再調整 ──┐
#71 ハイライト評価 ──┼→ #73 Adjustments ──┼─ ブラケット合成 (B1) ───┼→ rawpy 取りやめ
#77 白点調査 ────────┘      ↑               └─ PSO 移植 (S1) ─────────┘
                            │
#72 プレビュー CI 化 ───────┘（独立に先行可。Adjustments 不要）

CLI 小改修 (A)(B) ─→ #70/#71/#77 の評価に使う（最初に入れる）
CLI (C) バッチモード ─→ PSO・ブラケット合成の前提
プリセット構造の Rust 移行 ─→ #72 と並行可（値は動かさない）
```

| 順 | 作業 | 依存 | 備考 |
|---|---|---|---|
| 1 | CLI 拡張 (A) sRGB tiff16、(B) `apple_default` 下の NR / sharp / EDR / boost 上書き | なし | 評価 PR が使う。小さく先に入れる |
| 2 | #70 / #71 / #77 の評価（§4 の観点を含む） | 1 | ADR 0002「置き換えに進む条件」の充足確認 |
| 3 | #72 プレビュー CI 化（`apple_default` v9 lens ON へ既定変更、キャッシュ、フラグ→既定） | 2 の #70（星空で破綻が無いこと） | プリセット・Adjustments 不要 |
| 4 | プリセット構造の Rust 移行（値は現行のまま） | なし | 3 と並行可。`/api/health` の `presets` と二重提供する期間を許す |
| 5 | #73 Adjustments の CI 対応（CLI 後段 (F)、`exposure` / `wb_temp` 換算、`noise_reduction` 表、`highlight_recovery` の代替レバー） | 2 の #71 | 本移行の中核 |
| 6 | プリセット値の再調整 | 5 | 11 種を現行 rawpy 出力に揃える。契約 §5 の表を更新 |
| 7 | CLI (C) バッチモード | 5 | PSO・合成の前提 |
| 8 | ブラケット合成: 足場 B3（評価用）→ B1（CLI 合成モード (D) + Rust コマンド） | 5、7、2 の #70（露出差保存・幾何一致） | |
| 9 | PSO: 足場 S3 → S1（Rust 移植）。星判定・`stars` スタイルを含む | 5、7 | 最後。指標しきい値の再調整を含む |
| 10 | 暗所判定の廃止（N1）または Rust 移植（N2） | 2 の #70 | 9 と同時でよい |
| 11 | rawpy 取りやめ: `engine.py` / `bracket.py` / `development_candidates.py` のデコード経路削除、`requirements-raw.txt` から rawpy、`server.handle_health` の `engine` / `rawpy_version` | 3、6、8、9 | 「Python を取りやめる」まで進めるかは別判断（analysis / export / drive_upload 等は本設計の範囲外） |

#72 / #73 との関係の要約: **#72 は 4 機能に依存せず先行できる**。**#73 は 4 機能すべての前提**で、特に「後段をどこに置くか」「`highlight_recovery` の代替」「`noise_reduction` の対応表」の 3 点が 4 機能の設計を固定する。

---

## 6. リスク・未確認事項（評価と実装の前に認識しておくもの）

1. `apple_default` の内部パラメータは非公開で OS 更新で変わる（ADR 0002 決定 4）。PSO の指標しきい値・プリセット値・Mertens の重みは OS 更新ごとの回帰（#75）の対象に入れる。
2. `highlight_recovery` の代替が決まらないと救済系 3 プリセットと PSO の `highlight_class` が宙に浮く。#71 を先に終える。
3. Mertens の自前実装（B1）は OpenCV と一致しない。合否は目視と PSNR で決めるが、基準となる「OpenCV での合成結果」を評価 PR で保存しておく。
4. CLI のバッチモードでも 1 レンダ数百 ms なら PSO の体感時間は現行（half_size デコード数回）と桁が変わる可能性がある。`budget` の既定値や粒子数の見直しが要る【推測】。
5. 星空サンプルがリポジトリに無い（`work/raw` は worktree に無い）。評価 PR の担当者が実データを用意する必要がある。
6. CI のレンズ補正は周辺減光・歪曲・TCA を分離指定できない見込み【推測。確認できているのは公開 API が `isLensCorrectionEnabled` だけで中身が非公開であること（対応表）】。星空で周辺減光補正を切りたい場合は lens 全体を OFF にするしかない。
7. 本設計は「rawpy の取りやめ」を対象にしている。`analysis.py`（cv2 / PIL）、`export.py`、`photos_import.py`、`drive_upload.py`、`config.py` と localhost サーバの扱いは別途決める必要がある。
