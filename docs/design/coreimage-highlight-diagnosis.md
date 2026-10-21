# #92 Core Image の白飛び原因を段階別に切り分ける — 診断モードと比較マトリクスの設計書

- 対象: geoframe-raw-assist、ブランチ `feat/92-coreimage-highlight-diagnosis`（PR #93 = #72/#73、CLI 0.3.0、コミット b75e432 の上に積む）。
- 前提: ADR 0002（基本路線 = Core Image `apple_default`・decoder 9・レンズ補正 ON）。本 Issue は**原因究明の限定 PoC**で、
  本番プリセットの再調整（#81）や rawpy 廃止（#90）は先行しない。UI は変更しない。Swift 側の現像処理に Python を持ち込まない。
- 関連: Issue #92（仮説 H1〜H5、タスク A〜D、DoD）、#71 / PR #91（比較レポート §13〜§15、`--highlight`）、#79（個別上書き・tiff16 sRGB）、
  #73（`adjustments` と Metal 後段）、#81（`highlight_recovery` の置き換え。**本 Issue の判定 A/B/C を待つ**）、#75（回帰確認）、#89（将来の線形出力モード）。
- 作成: 2026-10-21（設計のみ。リポジトリのコードは変更していない。`work/diag-probe/` の使い捨てスニペットで事前実測を行った）。

## 表記

- 【確認】: コード・文書を読んで確認した事実、または本設計のための事前実測（§2）で観測した事実（条件を併記）。
- 【推測】: 未検証の見立て。実装・計測で確かめる。
- 【決定】: この設計で決めたこと。オーナー判断が要るものは §13 に別掲。

---

## 0. 要点（先に結論）

| 論点 | 決定 | 根拠（§） |
|---|---|---|
| 観測の枠組み | 「センサー CFA（S0）→ 線形段（S1）→ トーン（S2）→ 色域マッピング（S3）→ 16bit 出力（S4）→ 8bit JPEG（S5）」の 6 段を、**公開 API で実際に観測できる組み合わせ**で切り出す。S1 = `boost 0 + gamut OFF` の float 読み出し、S2 = `boost 1 + gamut OFF`、S3 = 既定の float、S4/S5 = 既存 tiff16 / jpeg | §2.2〜2.4、§3 |
| 事前実測で分かった機構（1 カット・1200px） | 線形段は表示白（1.0）を超える値を最大 2.70 まで保持し、boost のトーンカーブは 1.0 超を傾き約 0.145 で圧縮して**階調を残す**。1.0 で切り落としているのは**色域マッピング（`isGamutMappingEnabled`）**。sRGB/P3・8/16bit はその後なので、この画像では追加の情報損失をほぼ生まない | §2.2〜2.4 |
| 青チャンネル飽和の由来（同上） | DSC04705 の CFA 飽和は G 20% / B 4% / R 0% だが、線形段では R/G/B とも 25〜38% が 1.0 超。表示の B クリップは**WB ゲイン（B ×1.70）× baselineExposure（×1.32）で表示白を超える**ことが主因で、センサー B の飽和ではない | §2.1、§2.3 |
| CLI の契約 | CIRAWFilter のプロパティ上書き（`gamut_mapping`・`baseline_exposure`）は #79 と同じ流儀で `settings` に追加。観測の制御（float ダンプ・統計・`linear_space_filter`・診断用後段フィルタ・書き出し省略）は新しい任意オブジェクト `diagnostics` に置く。**`diagnostics` が無いリクエストはコード経路ごと 0.3.0 と同一**。`applied` のキー集合は変えない（回帰スクリプトがキー集合の差も数えるため）。CLI は 0.4.0 | §4 |
| `baselineExposure` の上書き | **可能で安全、ただし `exposure` と冗長**（0 に上書き = `exposure −0.4` と統計が一致）。掃引軸にせず、スモークテストの整合確認 1 本に留める | §2.5 |
| RAW CFA 飽和 | rawpy（評価スクリプト側のみ）の `raw_image_visible` / `raw_colors_visible` / `black_level_per_channel` / `white_level` から CFA 色別の飽和率と 2×2 セル飽和マップを出す。閾値は `white_level − 量子化ステップ`（α6400 ARW は上端のステップ 32 → 16348）。表示 RGB のクリップとは別スコア | §5 |
| マトリクス | 固定基準から 1 軸ずつ: boost 4 点、exposure 4 点、gamut OFF、出力 3 種、段階読み出し 3 本、EDR 3 点（参考）、対照 2 本、タスク C 候補 6 本 ≒ **1 カットあたり CI 約 26 リクエスト**。10 カット preview + 3 カット full で **CI 約 290 リクエスト、所要 10〜15 分**（4 並列） | §6 |
| ROI と位置合わせ | カメラ JPEG を基準に 5 種の ROI（明るい空・雲の階調・クリップ境界・低輝度前景・中間調）。位置合わせは既存の位相相関（`roi_shift`）。**画素単位の CFA↔CI 照合は CI lens OFF の幾何で行う**（lens ON/OFF でクリップ率は同一と実測）。失敗は欠測 | §7 |
| 判定 | ROI ごとに「センサー飽和 / デコーダ・トーン / 8bit 量子化 / JPEG 符号化 / 検証不能 / 混合」（2026-10-21 改名、§10.1）を帰属規則（§10.1）で分類し、シーン判定 → A / B / C | §10 |
| 実装の分割 | T1 Swift CLI（診断モード + テスト）、T2 Python ハーネス（`--diagnosis`）、T3 レポート。T1 と T2 は §4 の JSON 契約を固定して並行。T3 は T1+T2 の計測後 | §11 |

---

## 1. 現状と前提

### 1.1 #72/#73 で解消済みの事項

Issue #92 本文「現状 3」（`src-tauri/src/coreimage.rs` の既定が `neutral`・lens OFF）は **#72/#73（PR #93）で解消済み**【確認】
（`native/coreimage-cli/README.md`「アプリからの使い方」: リクエストは `decoder_version: "9"`、`settings: {profile: "apple_default", lens_correction: true}` 固定、長辺 2400px）。
したがって本 Issue は「UI の画が悪い」の調査ではなく、**基本路線そのもの（`apple_default` v9 lens ON）のハイライトの素性**を調べる。

### 1.2 既にある道具（再利用し、重複開発しない）

| 道具 | 内容 | 本設計での使い方 |
|---|---|---|
| CLI 0.3.0 `settings` の個別上書き（#79） | `luminance_noise_reduction` / `color_noise_reduction` / `sharpness_amount` / `detail_amount` / `extended_dynamic_range` / `boost_amount` / `boost_shadow_amount`（`Request.swift: overrideKeys`、`Develop.swift: applyOverrides`） | boost / EDR の掃引はこれをそのまま使う |
| `settings.exposure_ev` | `CIRAWFilter.exposure`（EV、baselineExposure に加算） | exposure の掃引 |
| `format: "tiff16"` + `output_color_space: srgb / display_p3` | 16bit RGB TIFF | S4（16bit 出力）の観測 |
| `adjustments`（#73） | 9 キー → デコード段 + Metal 後段（`PostChain.swift`: clamp → sRGB ガンマ → `geoframe_tone` → USM → 線形へ） | タスク C の「露出を下げて `shadow_lift` / `brightness` で戻す」候補 |
| 応答 `applied` | filter から読み戻した実効値（`baseline_exposure`、`boost_amount`、`gamut_mapping_enabled` 等） | 実効値の記録（毎リクエスト） |
| `scripts/compare_engines.py --highlight` | `channel_clip_rates` / `clip_boundary_magenta_rate` / `image_metrics`、`develop_variants`（4 並列）、`load_variant_images` + `fit_to_target`、`roi_shift`（位相相関）、`best_box_by_mask`、シート生成 | 新モード `--diagnosis` はこれらの純粋関数と実行基盤を流用する。**`--highlight` 自体は変えない**（§13 の再現性を保つ） |
| `scripts/coreimage-regression-applied.sh` | 既定リクエストの `applied` をベースライン JSON と比較（キー集合の差も数える） | 「既定不変」の合否条件 |
| `tests/test_compare_engines.py`（90 件） | 純粋関数の単体テスト | 新しい純粋関数も同じファイルに足す |

### 1.3 公開 API で観測できること・できないこと【確認】

- 観測できる: `CIRAWFilter.outputImage`（向き適用済み、作業空間 extended linear sRGB・half float）を任意の `CGColorSpace` / `CIFormat` で描画した値。
  プロパティの読み戻し（`applied`）。`linearSpaceFilter` に挿した CIFilter の**入力に対する出力**（直接は読めないが、定数置換で応答関数を標本化できる。§2.4）。
- 観測できない: センサーの生モザイク（CFA）、デモザイク・NR・シャープ・ハイライト再構成の中間画像、トーンカーブの式、色域マッピングの方式、
  `linearSpaceFilter` の入力画像そのもの。**CFA が飽和した画素を CI がどう埋めたかは見えない**（線形読み出しに値はあるが、それが再構成値か実測値かは区別できない）。
- `outputImage` の作業空間は extended linear sRGB なので、1.0 超・0 未満の値は **float（RGBAf / RGBAh）で描画すれば clamp されずに読める**（§2.2 で確認）。

---

## 2. 事前実測（設計のための probe。最終結果ではない）

スニペット `work/diag-probe/probe{,2,3,4}.swift`（`swiftc -O`、CIContext は CLI と同じ extended linear sRGB / RGBAh / cacheIntermediates false）と
`work/diag-probe/cfa_*.py`（rawpy 0.27.0 / LibRaw 0.22.1、`.venv` は `t3-8cafe7c8` のもの）。macOS 27.0.1 / M3。
**CI 側は DSC04705（昼の青空と雲、ISO 100、bright_frac 40%）1 カットを scaleFactor 0.2（1200×800）で見ただけ**なので、10 カットでの再確認は T2 の仕事。
CFA 側は 10 カットすべて。元 ARW は読むだけで、画像は書いていない。

### 2.1 CFA 飽和（rawpy、10 カット）【確認】

`white_level` 16380、`black_level` 512（全 CFA）、`camera_white_level_per_channel` 15360、`raw_pattern` RGBG。上端の値は 32 刻み
（…16340, 16372, 16404。DSC04705 の G は 16372 に 121 万画素が集中し、16404 は 105 画素）。閾値 `white_level − 32 = 16348` で:

| ID | R | G | B | 2×2 セルのいずれか | 備考（§13 のカメラ JPEG clip_any） |
|---|---|---|---|---|---|
| DSC04473 | 0.00% | 0.00% | 0.00% | 0.00% | 曇天の空抜け。CI では空が白く抜ける（§13.6） |
| DSC04607 | 0.00% | 0.14% | 0.00% | 0.16% | 明るい曇り空。CI では空がほぼ白一色（§13.6） |
| DSC04532 | 0.77% | 1.81% | 0.21% | 1.91% | 夜明けの空 |
| DSC04705 | 0.00% | 20.00% | 4.14% | 20.12% | 青空と白い雲。G が広く飽和 |
| DSC04579 | 0.00% | 1.64% | 0.00% | 1.70% | 朝の明るい空 |
| DSC04604 | 0.07% | 1.65% | 0.28% | 1.70% | 朝の空 |
| DSC04754 | 0.11% | 7.56% | 3.25% | 7.59% | 曇天の空を含む風景 |
| DSC04432 | 1.69% | 9.30% | 2.85% | 9.55% | 室内の窓（ISO 2000） |
| DSC04746 | 0.00% | 0.17% | 0.00% | 0.18% | 空抜けの前景 |
| DSC04462 | 5.69% | 9.23% | 7.81% | 9.25% | 明るい屋外 |

読み: CI `apple_default` の preview clip_any 中央値 13.0%（§13.5）に対し、センサー側が飽和しているのは中央値 1.8% 程度。
**DSC04473 / DSC04607 / DSC04746 はセンサーがほぼ飽和していないのに CI は空を白く飛ばしている**ので、これらは H1（センサー）ではなく
H2/H3（デコーダ内の処理）側に帰属する見込み【推測: ROI 単位の照合は未実施】。DSC04705 は H1 と H2/H3 の**混合**になる見込み。
`camera_white_level` 15360 を閾値にすると飽和率は上がる（保守的）。両方の列を出す（§5）。

### 2.2 float 読み出しと段階の切り出し（CI、DSC04705、1200px）【確認】

`outputImage` を RGBAf・extended linear sRGB で描画した統計（R/G/B）:

| 設定 | p50 | p99.9 | max | 1.0 超の割合 | 解釈 |
|---|---|---|---|---|---|
| 既定（boost 1・gamut ON） | 0.050 / 0.054 / 0.064 | 1.000 / 1.000 / 1.0075 | 1.000 / 1.000 / 1.010 | 0.25% / 0.06% / **36.6%** | S3。R/G はちょうど 1.0 で頭打ち、B だけ 1.01 まで僅かに超える |
| boost 1・**gamut OFF** | 同上 | 1.2215（3ch 同値） | 1.2465（3ch 同値） | 24.7% / 32.8% / 38.5% | S2。1.0〜1.25 に 247 段階の値があり**階調が残っている** |
| **boost 0・gamut OFF** | 0.038 / 0.040 / 0.045 | 2.528（同値） | **2.6997**（同値） | 24.7% / 32.7% / 38.3% | S1（線形読み出し）。表示白の 2.7 倍まで保持 |
| boost 0・gamut ON | — | 1.000 / 1.000 / 1.029 | 1.000 / 1.000 / 1.034 | 0.8% / 0.5% / 36.7% | gamut ON が 1.0 で切る |
| exposure −0.5・gamut OFF | — | 1.114 | 1.1315 | 14.3% / 17.1% / 25.4% | |
| exposure −1（gamut ON） | 0.020 / 0.021 / 0.025 | — | 1.000 / 1.000 / 1.001 | 0.25% / 0.06% / 5.7% | §13 の −1.0EV で clip 2.2% と整合 |
| EDR 1.0（gamut ON） | — | — | 2.70（同値） | 37% / 40% / 43% | EDR は 1.0 超をそのまま通す（SDR では悪化） |
| lens OFF（他は既定） | — | — | 1.000 / 1.000 / 1.010 | 0.17% / 0.00% / 36.5% | レンズ補正はハイライトに影響しない |

- 色域マッピング ON と OFF で、OFF 側が [0,1] に収まる画素（61%）の差は平均 0.00016、最大 0.026、0.8 超の画素でも平均 0.0018。
  → **この画像では `isGamutMappingEnabled` は実質「1.0 への clamp」**で、範囲内の画素の色はほぼ変えない。
- 既定出力を linear Display P3 で描画すると B の 1.0 超は 1.4% になるが、これは clamp が既に終わった値を広い色域へ写しただけで、
  **P3 が白飛びを減らすのではない**。sRGB / P3 の比較は同じ表示空間へ変換した後で行う（§8）。
- 既定の sRGB（ガンマ）float 描画は、新しい filter インスタンスで行えば妥当な値（p50 0.248 ≒ sRGB(0.05)）。**同じ filter インスタンスで
  `outputImage` を 2 回以上参照すると extent が半分ずつ縮む**（1200×800 → 600×400 → 300×200。レンダ有無に関係なし）ことを確認した。
  現行 CLI は 1 回しか参照していない（`Develop.swift: guard let decoded = filter.outputImage`）ので影響は無いが、**診断モードでも参照は 1 回に限定し、
  取得した `CIImage` を使い回す**（§4.6、スモークテストで extent を検査）。原因は非公開【推測: scaleFactor の再適用】。

### 2.3 青チャンネルの由来【確認（1 カット）】

線形読み出し（S1）で「いずれかのチャンネルが 1.0 超」38.3%、「3 チャンネルとも 1.0 超」24.7%、「B だけ 1.0 超」5.6%。
CFA 飽和は G 20% / B 4%。カメラ WB は R ×2.74 / B ×1.70（`camera_whitebalance` 2808/1024、1740/1024）、baselineExposure 0.4（×1.32）。
青空では B が最大チャンネルなので、**センサー B が白の 45% を超えると表示白を超える**（0.45 × 1.70 × 1.32 ≈ 1.0）。
表示で B が先に飛ぶのはこの算術で、センサー B の飽和ではない。G の 20% は本物のセンサー飽和で、その画素の色は CI が埋めている（観測不能）。

### 2.4 `linearSpaceFilter` の挙動【確認（1 カット）】

- 恒等（`CIColorMatrix` scale 1）を挿すと統計は既定と完全一致。scale 0.5 は `exposure −1` と同じ統計（平均 0.3650 vs 0.3648）。
- **定数置換**（scale 0 + bias v）で出力が画面全体で一様（最大−最小 0）になり、応答関数を標本化できる（局所適応なし。`isLocalToneMapSupported` false）:

  | 線形 v | boost 1（gamut ON） | boost 1（gamut OFF） | boost 0（gamut OFF） | EDR 1 | EDR 2 |
  |---|---|---|---|---|---|
  | 0.05 | 0.0744 | — | 0.05 | — | — |
  | 0.18 | 0.3875 | — | 0.18 | — | — |
  | 0.5 | 0.8050 | 0.8050 | 0.5 | 0.8224 | 0.8883 |
  | 0.8 | 0.9573 | 0.9573 | 0.8 | 1.0790 | 1.2553 |
  | 1.0 | 1.0000 | 1.0000 | 1.0 | 1.2500 | 1.5000 |
  | 1.2 | 1.0000 | 1.0290 | 1.2 | 1.4210 | 1.7447 |
  | 1.5 | 1.0000 | 1.0725 | 1.5 | 1.6776 | 2.1117 |
  | 2.0 | 1.0000 | 1.1450 | 2.0 | 2.1052 | 2.7233 |

  boost 1 は中間調を約 2 倍持ち上げ（0.18 → 0.39）、1.0 を固定点にし、1.0 超は傾き約 0.145 で圧縮する。boost 0 は恒等（2.0 まで確認。実画像の 2.70 までは外挿）。
  gamut ON はこれを 1.0 で切る。EDR は 1.0 → 1.25（EDR 1）/ 1.5（EDR 2）に**押し上げる**（§13 の「EDR で白飛びが増える」の機構）。
- `exposure` / `baselineExposure` は `linearSpaceFilter` の**前**に掛かる（定数 0.5 + exposure −1 → 0.805 = f(0.5)。exposure が後なら f(0.25) = 0.56 になるはず）。
  つまり線形読み出し（S1）の 1.0 は「baselineExposure 込みの表示白」。
- 定数置換の標本化が実画像にも当てはまる（ヒストグラム適応でない）ことは、ハーネスで「S1 に標本化した曲線を掛けて S2 と比べる」検査で確かめる（§8.4）。合わなければ【推測】に格下げ。

### 2.5 `baselineExposure` の上書き【確認（1 カット）】

`filter.baselineExposure = 0` → 読み戻し 0.0（元 0.4）。統計は `exposure = −0.4` と**完全一致**（平均 / p50 / p99.9 / max / 1.0 超率すべて）。
→ 上書きは可能で安全だが `exposure` と冗長。掃引軸にはせず、T1 のスモークテストで「baseline 0 ≡ exposure −0.4」を 1 回確かめるだけにする【決定】。

### 2.6 コスト【確認】

| 処理 | 時間 | メモリ |
|---|---|---|
| RGBAf 描画 1200×800（2 回目以降） | 約 350 ms | — |
| RGBAf 描画 6000×4000（1 回目、デコード込み） | 1.3 s | バッファ 384 MB（RGBA）。RGB だけ書けば 288 MB |
| RGBA16 sRGB 描画 6000×4000 | 1.2 s | 192 MB |
| rawpy CFA 飽和（6024×4024、Python） | 約 1 s / カット | — |

---

## 3. 段階モデルと仮説の対応

| 段 | 内容 | 観測手段（公開 API） | 対応する仮説 |
|---|---|---|---|
| S0 センサー CFA | 各 CFA 画素が `white_level` に達しているか | rawpy `raw_image_visible`（§5） | H1 |
| S1 線形段 | デモザイク・NR・シャープ・レンズ補正・WB・baselineExposure・exposure・shadowBias を経た線形 RGB。CI のハイライト再構成を**含む** | `boost_amount 0` + `gamut_mapping false` の float 読み出し（恒等であることを §2.4 で確認） | H2（露出の余裕）、H5 の一部 |
| S2 トーン | boost カーブ（+ boostShadow / contrast）。1.0 超は圧縮されるが残る | `gamut_mapping false` の float 読み出し | H2、H4 |
| S3 色域マッピング | 1.0 への clamp（+ 範囲内はほぼ恒等） | 既定の float 読み出し | H3 |
| S4 16bit 出力 | sRGB / Display P3 への変換と 16bit 量子化 | 既存 `tiff16`（srgb / display_p3） | H3 |
| S5 8bit JPEG | 8bit 量子化・JPEG | 既存 `jpeg` | H3 |
| 後段 | `adjustments` の Metal 後段（先頭で [0,1] clamp） | 既存。タスク C で使う | H4 |

帰属の規則（§10.1）は「どの段で情報が消えたか」ではなく「**どの段のレバーで戻せるか**」で決める。S2 で 1.0 超の画素は、clamp したのが S3 でも、
戻すレバー（exposure / boost / トーン圧縮）が S1〜S2 側にあるので「デコーダ/トーン」に帰属する。

---

## 4. CLI 診断モードの契約（CLI 0.4.0）

### 4.1 不変条件【決定】

1. `diagnostics` キーが**無い**リクエストは、`RequestParser` の分岐以外 0.3.0 と同一のコード経路を通り、出力バイト列・応答のキー集合・`applied` の値が同一。
   合否: `scripts/coreimage-regression-applied.sh` の差分 0 件、スモークテスト「`diagnostics` 無し vs `diagnostics: {}` で tiff16 のバイト列が一致」。
2. `applied` のキー集合は変えない（回帰スクリプトは `set(now) | set(base)` でキーの差も数える）。新しい読み戻し値は応答の `diagnostics` ブロックへ。
3. `settings` に加える 2 キーは #79 の個別上書きと同じ規則（省略 / null = 既定のまま、値域外は `INVALID_REQUEST`）で、**既定リクエストに影響しない**。
4. 出力は本番キャッシュ（`<app_data_dir>/coreimage/`）と無関係（ハーネスは `work/` 配下に書く）。元 ARW は読むだけ。
5. CLI バージョンを 0.4.0 に上げる。Tauri のキャッシュ鍵は `--version` 応答を含むため、**初回だけプレビューが再現像される**（#73 と同じ。許容）。

### 4.2 `settings` の追加（CIRAWFilter プロパティの上書き。#79 と同じ流儀）

| キー | 型 / 値域 | 既定 | 書き先 | 読み戻し |
|---|---|---|---|---|
| `settings.gamut_mapping` | boolean | null（CIRAWFilter 既定 = true） | `isGamutMappingEnabled` | 既存 `applied.gamut_mapping_enabled` |
| `settings.baseline_exposure` | number −5..5 | null（ファイル値） | `baselineExposure` | 既存 `applied.baseline_exposure` |

`applyOverrides` の末尾に 2 行足すだけ。`adjustments` との同時指定も可（優先順位は既存どおり「明示の settings ＞ adjustments の写像 ＞ profile」）。
→ 「既存の個別上書きと重複させず、既にあるものを再利用する」要件に合わせ、`diagnostics` 側には置かない（§13 判断 1）。

### 4.3 `diagnostics`（トップレベル、任意。省略 / null = 通常モード）

```jsonc
"diagnostics": {
  "stats": true,                              // 全画面の float 統計を応答に載せる（既定 true。diagnostics を書いた時点で「統計が欲しい」とみなす）
  "stats_color_space": "linear_srgb",         // "linear_srgb"（extended linear sRGB、既定）| "linear_p3"（extended linear Display P3）
  "dump_float": {                             // 任意。clamp しない float 画像をファイルに書く
    "path": "/abs/dir/DSC04705_s1.f32",       // 絶対パス、拡張子 .f32、親ディレクトリ必須。output と同じく一時ファイル + rename
    "color_space": "linear_srgb"              // 同上の 2 択。既定 linear_srgb
  },
  "linear_space_filter": {                    // 任意。CIRAWFilter.linearSpaceFilter に CIColorMatrix を挿す
    "kind": "identity" | "scale" | "constant",
    "value": 0.5                              // scale: 係数 0.01..8、constant: 置換値 0..8。identity では省略
  },
  "post_filters": [                           // 任意。outputImage の直後（adjustments 後段の前）に挿す CIFilter（許可リスト制、§4.5）
    {"name": "CIHighlightShadowAdjust", "inputHighlightAmount": 0.6, "inputShadowAmount": 0.0, "inputRadius": 0.0},
    {"name": "CIToneCurve", "inputPoint0": [0, 0], "inputPoint1": [0.25, 0.3], "inputPoint2": [0.5, 0.6], "inputPoint3": [1.0, 0.9], "inputPoint4": [2.7, 1.0]}
  ],
  "skip_write": false                         // true なら output に画像を書かない（統計 / dump だけ）。output のパス検証は従来どおり行う
}
```

規則（違反は `INVALID_REQUEST`）:

- 未知キーは拒否。`diagnostics: {}` は「統計だけ」の最小指定。
- `dump_float.path` は `input` と同一実体不可、`output` と同一パス不可。書き込みは `AtomicImageWriter.writeAtomically` を使う。
- `linear_space_filter` と `post_filters` は `adjustments` と同時指定可。順序は固定: **`outputImage` → `post_filters` → `adjustments` の後段（先頭 clamp）→ レンダ**。
  `post_filters` を後段の前に置くのは、後段の `CIColorClamp(0..1)` で 1.0 超が消える前にハイライト圧縮を掛けるため。
- `skip_write: true` でも `format` / `output` の検証はそのまま（契約を増やさない）。応答 `output` は要求パスのまま、`diagnostics.written: false`。
- CLI の統計は**全画面のみ**【決定】。ROI 指定の統計は CLI に持たせない。ROI の抽出はカメラ JPEG（CLI は見ない）を基準に行い、位置合わせも必要なので、
  ROI 統計は Python ハーネスが dump から計算する（§7、§8）。CLI の統計は「dump 無しの軽い掃引」での 1.0 超率の確認用（§13 判断 10）。
- 統計は `outputImage`（`post_filters` / 後段を含めた**レンダ直前の画像**）を、`stats_color_space` の extended linear 空間・RGBAf で全画面描画して計算する。
  `adjustments` 指定時は後段の clamp の後の値になる（1.0 超は出ない）。線形読み出し（S1）が欲しいときは `adjustments` を付けない。

### 4.4 応答の追加ブロック `diagnostics`（`diagnostics` 指定時のみ出す。`applied` は不変）

```jsonc
"diagnostics": {
  "cli_diagnostics_version": 1,
  "written": true,
  "stats_color_space": "linear_srgb",
  "stats_source": "render_input",              // 統計を取った画像の位置（固定文字列。将来 "linear_stage" 等を増やす余地）
  "stats": {                                   // 全画面、チャンネル別
    "width": 2400, "height": 1600,
    "r": {"mean": 0.43, "p50": 0.050, "p99": 1.0, "p999": 1.0, "max": 1.0, "min": -0.0005, "frac_gt_1": 0.0025, "frac_ge_1": 0.031, "frac_lt_0": 0.0006},
    "g": {...}, "b": {...},
    "any_gt_1": 0.38, "all_gt_1": 0.25          // いずれか / すべてのチャンネルが 1.0 超
  },
  "dump_float": {"path": "...", "width": 2400, "height": 1600, "channels": 3, "dtype": "float32", "byte_order": "little", "layout": "interleaved_rgb_row_major", "color_space": "linear_srgb", "bytes": 46080000},
  "linear_space_filter": {"kind": "scale", "value": 0.5, "filter": "CIColorMatrix"},
  "post_filters": [{"name": "CIHighlightShadowAdjust", "applied_parameters": {...}}],
  "readback": {"gamut_mapping_enabled": true, "baseline_exposure": 0.4, "extent": [0, 0, 2400, 1600]}
}
```

- `frac_ge_1` は `>= 1.0 − 1/65535`（16bit で白になる画素）。`frac_gt_1` は厳密に 1.0 超（extended 範囲に出た値）。両方出す。
- dump は RGB 3 チャンネル float32 little-endian・行優先・アルファ無し（RGBAf を描画して A を落とす）。Python は `np.fromfile(p, "<f4").reshape(h, w, 3)`。
  サイズは 2400px preview で約 46 MB、full で 288 MB。float TIFF ではなく生ダンプにする理由: ImageIO の float TIFF の書き出し・読み込み互換を検証するコストを避け、
  メタデータは応答 JSON に持たせる（§13 判断 2）。
- `stats` の計算は CPU（Swift、`Accelerate` の `vDSP` でソート / 集計）。full で 24M 画素 × 3ch の percentile は部分ソートか 65536 ビンのヒストグラム近似にする
  （ビン幅 1/16384 なら p99.9 の誤差 ≤ 6e-5。実装の自由）。

### 4.5 `post_filters` の許可リスト【決定】

診断専用のため名前を固定し、数値 / 2 次元ベクトル（配列 2 要素）のパラメータだけ受ける。未知の名前・未知のパラメータ・数値以外は `INVALID_REQUEST`。

| name | 受けるパラメータ | 用途（タスク C） |
|---|---|---|
| `CIHighlightShadowAdjust` | `inputHighlightAmount` 0..1、`inputShadowAmount` −1..1、`inputRadius` 0..10 | 候補 C2 |
| `CIToneCurve` | `inputPoint0..4`（各 [x, y]、x **0..1** 単調増加、y 0..2） | 候補 C3（自前トーン圧縮）。入力は [0,1] に clamp される【確認】ので、1.0 超を扱うときは前に `CIExposureAdjust` を置く |
| `CIExposureAdjust` | `inputEV` −5..5 | 線形段での露出戻し（C3 の組み合わせ） |
| `CIColorClamp` | `inputMinComponents` / `inputMaxComponents`（各 [r, g, b, a]） | 段の切り分け用（clamp 位置の実験） |

いずれも作業空間（線形）で掛かる。`CIHighlightShadowAdjust` は線形入力を前提にした設計ではない可能性があり【推測】、効き方はハーネスで確かめる。
本番に入れる候補は、ここで試した結果を #81 で `adjustments` の写像として正式に実装する（診断モードの `post_filters` をアプリから使うことはしない）。

### 4.6 実装メモ（T1）

- `RequestParser`: `topLevelKeys` に `diagnostics`、`settingsKeys` に `gamut_mapping` / `baseline_exposure` を追加。`Diagnostics` 構造体と `parseDiagnostics` を新設（`Adjustments` と同じ厳格さ）。
- `Develop.swift: develop()`: `filter.outputImage` の参照は現状どおり**1 回**。`post_filters` は `decoded` に順に `applyingFilter`。統計 / dump は最終 `image` を RGBAf で `context.render(toBitmap:)`（`renderRGB16` と同じ流儀）。
  `skip_write` のときはレンダ・エンコードを飛ばし、`width` / `height` は `extent` から出す。
- 統計 / dump の描画と通常のレンダは**同じ `CIImage` を 2 回描画する**（float と 8/16bit）。レンダ時間は約 2 倍になるが診断専用なので許容。
- スモークテスト（ARW があるときだけ）: (a) `diagnostics` 無し vs `{}` で tiff16 バイト一致、(b) `gamut_mapping false` + `boost_amount 0` で `any_gt_1 > 0` かつ `max > 1.5`
  （線形読み出しの成立）、(c) `baseline_exposure 0` と `exposure_ev −0.4` の `stats` が一致、(d) `linear_space_filter identity` で `stats` が既定と一致、
  (e) 応答 `readback.extent` が `scaleFactor` から期待される寸法（`outputImage` の二重参照の検出）、(f) `skip_write` で output が作られない。
- 単体テスト（ARW 不要）: パーサの受理 / 拒否、`post_filters` の許可リスト、dump パスの検証、`frac_*` の計算（合成バッファ）。
- README に「診断モード」節と `--capabilities` の `diagnostics: {supported: true, post_filters: [...]}` を追加。

---

## 5. RAW CFA 飽和の測定（Python、評価スクリプト側のみ）

`scripts/compare_engines.py` に純粋関数 `cfa_saturation(raw)` と `cfa_saturation_map(raw)` を追加（rawpy はハーネスの既存依存。**アプリの現像経路には入れない**）。

1. `rawpy.imread(path)`（読むだけ）→ `raw.raw_image_visible`（6024×4024、uint16）、`raw.raw_colors_visible`（0..3 = R G B G）、`raw.black_level_per_channel`、
   `raw.white_level`、`raw.camera_white_level_per_channel`、`raw.sizes.crop_*`（6000×4000 の有効域 = CI / カメラ JPEG の寸法）。
2. 量子化ステップ: `white_level / 2` より大きい値の `np.unique` の最小差分（α6400 ARW は 32。他カメラ・非圧縮では 1 になりうる）。
   **閾値 `T_libraw = white_level − step`**（主）と **`T_camera = camera_white_level − step`**（保守的、副）。
3. 指標（全画面と ROI）: `cfa_sat_{r,g,b}`（G は 2 面の平均）、`cfa_sat_cell_any` / `cfa_sat_cell_all`（2×2 セル）、同じものを `T_camera` で `cfa_satc_*`。
   `cfa_headroom_{r,g,b}` = 各 CFA 色の p99.9 / (T − black)（飽和していないが近いか）。
4. 飽和マップ: 2×2 セル単位（3000×2000、`any` / 色別の 3 ビット）を有効域でクロップし、比較寸法へ**最近傍**で縮小（補間しない。縮小後は「セル内のいずれか」の OR）。
   向きは EXIF Orientation を適用して正立にする（`read_rgb_upright` と同じ規則）。
5. 位置合わせ: マップはレンズ補正無しの幾何なので **CI lens OFF の出力**と画素単位で照合する（§7.3）。
6. 黒補正は閾値側で扱う（値 ≥ T）。`raw_image` ではなく `raw_image_visible` を使い、マージン 12px は `crop_*` で落とす。
7. rawpy の現像結果（`rawpy` バリアント）とは独立した指標で、**「表示 RGB のクリップ」とは別スコア**として CSV の別列に置く。

テスト（`tests/test_compare_engines.py`）: 合成 CFA（既知の飽和セル配置）で率・マップ・ステップ検出・クロップ・向きを検証。

---

## 6. 比較マトリクス

### 6.1 固定基準（B）

`decoder_version "9"`、`profile apple_default`、`lens_correction true`、`exposure_ev 0`、`boost_amount` 既定（1.0）、`extended_dynamic_range` 既定（0）、
`gamut_mapping` 既定（true）、`format jpeg`、preview `max_edge 2400`。実効値は毎回 `applied` から CSV に写す（`ci_baseline_exposure` / `ci_boost` / `ci_edr` /
`ci_gamut` / `ci_sharpness` / `ci_lum_nr`）。環境（macOS、CLI `--version`、カメラ / レンズ / ISO）は `diagnosis_summary.json` に記録。

### 6.2 軸（B から 1 軸ずつ。同時に 2 軸以上は動かさない。タスク C だけ例外）

| 群 | バリアント | リクエスト | 数 |
|---|---|---|---|
| 基準 | `d_base` | B（jpeg） | 1 |
| 段階読み出し | `d_s1_linear` / `d_s2_tone` / `d_s3_default` | `diagnostics: {dump_float}` + `skip_write`、それぞれ `boost 0 + gamut false` / `gamut false` / B 相当。**いずれも `lens_correction false`**（CFA マップと同じ幾何で画素照合するため。§7.3。lens ON/OFF でクリップ率は同一: §2.2） | 3 |
| boost | `d_boost00` / `025` / `050` / `075` | `boost_amount` 0 / 0.25 / 0.5 / 0.75（1.0 = B。既存 0.6 / 0.8 は §13 と対応付け） | 4 |
| exposure | `d_ev-03` / `-07` / `-10` / `-15` | `exposure_ev` −0.3 / −0.7 / −1.0 / −1.5 | 4 |
| 色域 | `d_gamut_off` | `gamut_mapping false`（jpeg） | 1 |
| 出力 | `d_tiff_srgb` / `d_tiff_p3` | `tiff16` + `output_color_space` srgb / display_p3 | 2 |
| EDR（参考） | `d_edr05` / `10` / `20` | `extended_dynamic_range` 0.5 / 1 / 2 | 3 |
| 対照 | `d_neutral` / `d_lens_off` | `profile neutral`、`lens_correction false`（画素照合の幾何橋渡しも兼ねる） | 2 |
| タスク C（§9） | `c1_ev-07_lift` / `c1_ev-10_lift` / `c2_ev-07_hsa` / `c3_lin_curve` / `c3_lin_curve_b` / `c_best`（後述） | 露出 + 後段の組み合わせ | 6 |
| 外部 | `camera` / `rawpy` | 既存（rawpy は現行プリセット `neutral_recovery`） | rawpy 1 |

1 カット preview: CI 26 + rawpy 1 + CFA 1。**baselineExposure は軸にしない**（§2.5。T1 のスモークで確認済みとする）。
`boost_shadow_amount` は §13 で効かないことが分かっているので外す。

### 6.3 規模と所要時間【推測（既存実測からの見積り）】

| | 件数 | 1 件 | 小計 |
|---|---|---|---|
| preview（10 カット） | CI 26 × 10 = 260 | jpeg 約 1.3 s、dump 約 1.7 s（46 MB 書き込み込み） | 4 並列で約 2.5 分 |
| full（3 カット: DSC04473 / DSC04607 / DSC04705） | CI 12 × 3 = 36（B、段階読み出し 3、ev −0.7 / −1.0、boost 0、gamut OFF、tiff sRGB、C 候補 3） | jpeg 約 2.2 s、dump 約 3 s（288 MB） | 直列で約 2 分（dump は同時 1 本） |
| rawpy preview / full | 10 + 3 | 約 4 / 5 s | 約 1 分 |
| CFA（rawpy、原寸） | 10 | 約 1 s | 10 s |
| 指標 / シート（Python） | — | ROI 位置合わせ・指標・シート | 約 3〜5 分【推測】 |

合計 **10〜15 分**。ディスク: preview dump 46 MB × 3 × 10 = 1.4 GB、full dump 288 MB × 3 × 3 = 2.6 GB → **dump は指標を取ったら削除**（`--keep-dumps` で残せる）。
出力先 `work/engine-comparison/diagnosis1/`（gitignore 済み）。

### 6.4 比較の原則

- 同一 ARW・同一画角（`fit_to_target`、resampled は記録）・同一表示空間（P3 tiff は sRGB へ変換してから clip を数える。変換前の P3 の値は「P3 でのクリップ率」として別列）。
- 露出水準: 中間調 ROI の p50 を記録し、**同じ p50 での比較**は §8.3 の方法で行う。
- `applied` の実効値がリクエストと違う場合（OS が丸めた等）はその値を正とし、CSV に残す。

---

## 7. ROI と位置合わせ

### 7.1 ROI の定義（カメラ JPEG を基準に自動抽出。1 辺は長辺の 10% = preview 240px / full 600px。既存 `ROI_FRACTION`）

| ROI | 抽出規則（カメラ JPEG、Rec.709 輝度 L、彩度 S = max−min） | 目的 |
|---|---|---|
| `sky_bright` | L > 0.85 の画素が最も多い箱（`best_box_by_mask`）。無ければ欠測 | 白飛び率・チャンネル別クリップ |
| `cloud_grad` | 0.6 < L < 0.98 の画素が 70% 以上を占める箱のうち、σ2 ぼかし後のローカル標準偏差（階調の起伏）が最大のもの | 雲の階調幅 / 勾配が残るか |
| `clip_edge` | 既存のクリップ境界リング（`dilate_mask(clipped, 3) & ~clipped`）が最も多い箱 | 境界の偽色・階調の連続性 |
| `fg_dark` | L の p50 が最も低く、かつ Laplacian 平均が閾値以上（構造がある）箱 | 前景ノイズ・黒つぶれ（露出を下げた副作用） |
| `midtone` | 0.3 < L < 0.6 かつ S < 0.25 の画素が最も多い箱 | p50 を揃える基準（§8.3） |

各 ROI は重ならないように順に確保（`roi_boxes` と同じ流儀）。抽出結果（座標）は `rois.json` に保存し、シートに枠を描く。

### 7.2 位置合わせ（ROI 単位）

既存 `roi_shift`（±ROI/4 の余白、Hann 窓付き位相相関）をカメラ JPEG → 各バリアントで使う。`response < 0.08` またはずれ > ROI/4 は**その ROI × バリアントを欠測**
（NaN、`roi_excluded` に記録）。白一色の `sky_bright` は相関が落ちやすいので、**平行移動は `midtone` / `clip_edge` で推定した値を流用**し、`sky_bright` 自身の相関は検査にだけ使う。
スケール差（レンズ補正の autoscale）は補正しない（既存と同じ。10mm の lens OFF は欠測になりやすいことを承知で扱う）。

### 7.3 画素単位の照合（CFA マップ ↔ CI）

- CFA マップ（レンズ補正無しの幾何）と**CI lens OFF**（`d_lens_off`、同じ幾何、向き適用済み）を画素単位で照合する。CI lens ON と lens OFF のクリップ率は同一（§2.2）なので、
  クリップの帰属（§10.1）は lens OFF 側で数えても結論は変わらない【推測: 10 カットで lens ON/OFF の全画面 clip 差 ≤ 0.5 pt を確認する】。
- 寸法: CFA 2×2 セルマップ 3000×2000 → 最近傍で 2400×1600（preview）/ 6000×4000 へ（full は 2× 最近傍拡大）。CI lens OFF 出力との残差は位相相関で全画面 1 回推定して補正（`global_shift`）。
- 段階読み出し（S1〜S3 の dump）も **lens OFF** で要求する（§6.2）。これで CFA マップ・S1〜S3・`d_lens_off` の 8bit 出力が同じ幾何に揃い、§10.1 の帰属規則を
  1 つの画素集合 K（`d_lens_off` の 8bit でクリップした画素）に対して適用できる。lens ON（B、掃引、タスク C、シート）は ROI 単位の指標にだけ使う。
- lens OFF は周辺減光補正も外れるので四隅がわずかに暗い。§8.4 の lens ON/OFF 検査は全画面だけでなく **ROI ごと**（特に四隅寄りの `sky_bright`）の clip 差で確認する。
- カメラ JPEG ↔ CI lens ON は §11 で「カメラ内補正と 1 px 以内」とあるので ROI の位相相関で足りる。

---

## 8. 指標（何を測り、何を保証しないか）

### 8.1 クリップ・飽和（ROI と全画面、チャンネル別）

| 指標 | 定義 | 測るもの | 保証しないこと |
|---|---|---|---|
| `cfa_sat_{r,g,b}`、`cfa_sat_cell_any` | §5 | センサーが情報を持たない画素 | CI / rawpy がそこをどう埋めたか |
| `s1_gt1_{r,g,b,any,all}` | S1 dump で 1.0 超の割合 | 表示白を超える（露出 / トーンで戻せる余地のある）画素 | センサー飽和（CI の再構成値が含まれる） |
| `s2_gt1_*` | S2 dump で 1.0 超 | トーン後にも表示白を超える画素（= gamut 段で clamp される画素） | — |
| `s3_ge1_*` | S3 dump で ≥ 1 − 1/65535 | clamp 後の白 | — |
| `clip16_{r,g,b,any}`、`clip8_*` | tiff16 で 65535、jpeg で 255 | 出力での白 | — |
| `stage_loss_{s1s2,s2s3,s3s4,s4s5}` | 前段で [0,1) だった画素が当段で白になった割合 | 段ごとの新規損失 | 戻せるかどうか |
| `blue_only_clip` | B だけ白（R, G は白でない） | 青空の B 先行飽和 | — |

### 8.2 階調・色（ROI）

| 指標 | 定義 | 保証しないこと |
|---|---|---|
| `grad_levels` | `cloud_grad` 内の L（8bit / 16bit）の distinct 数と、L > 0.8 の画素に限った distinct 数 | 値が「正しい」こと（ノイズでも増える） |
| `grad_texture` | `cloud_grad` の σ1 ハイパスの標準偏差（L > 0.8 の画素のみ）。カメラ比 `grad_texture_ratio` | ノイズと模様の区別（`fg_dark` のノイズ指標と併読） |
| `grad_slope` | `cloud_grad` を横断する最大勾配方向の L のプロファイル（ESF と同じ採り方）の 10–90% 幅 | — |
| `sky_hue` / `sky_chroma` | `sky_bright` 非クリップ画素の Lab 色相角の中央値 / クロマ | 好ましさ |
| `magenta_rate` | 既存 `clip_boundary_magenta_rate` | — |
| `fg_noise`、`fg_p01` | `fg_dark` の `flat_noise` と L の p1 | — |
| `delta_e76_inrange` | S2 vs S3 で、S2 が [0,1] の画素だけの ΔE76（色域マッピングが色を動かすか） | — |

### 8.3 中間調 p50 を揃えた比較（DoD 3、タスク C の合否）

- `mid_p50` = `midtone` ROI の L の p50（8bit 出力、sRGB ガンマ空間）。カメラ JPEG との差 `mid_p50_diff`。
- 「同じ明るさ」の判定: `|mid_p50_diff| ≤ 0.03`【決定（§13 判断 5）】。rawpy 現行は全画面平均でカメラ比 −0.13（§13）なので通常この条件を満たさない。
  rawpy を比較に載せるときは **「rawpy と同じ p50 に合わせた CI（exposure 掃引の内挿）」**を別行で作る。
- 掃引からの内挿: exposure 掃引（0 / −0.3 / −0.7 / −1.0 / −1.5）の `mid_p50` と各指標を折れ線で内挿し、目標 p50 での指標を求める（近似。`interpolated: true` を列に残す）。
- 「白飛びだけ減らして全体が沈む設定」は、`mid_p50_diff < −0.03` または `fg_p01` がカメラ比 −0.02 未満なら**不合格**。

### 8.4 検査（指標そのものの妥当性）

- 定数置換で標本化したトーン曲線（§2.4）を S1 dump に掛けて S2 dump と比べ、`cloud_grad` / `midtone` の ΔL p99 ≤ 0.01 なら「曲線は大域的」と記録。超えれば局所 / 適応と記録し §2.4 を【推測】に落とす。
- S2 が [0,1] の画素の S2 vs S3 差（§2.2 の 0.00016）を 10 カットで再確認。
- lens ON/OFF の全画面 clip 差（§7.3 の前提）。

---

## 9. タスク C「暗くしないハイライト保持」の試験設計

B（§6）で `cloud_grad` の階調が S1/S2 に残る（`grad_levels`・`grad_texture_ratio` がカメラ比 ≥ 0.5）カットに限って実施。候補は**公開 API だけ**で組む。

| 候補 | 構成 | CLI の指定 | 狙い | 限界 |
|---|---|---|---|---|
| C1 | 露出を下げて既存後段で中間調を戻す | `adjustments: {exposure: −0.7 or −1.0, shadow_lift: 30..60, brightness: 1.05..1.2, 他は恒等}`（`settings.exposure_ev` は `adjustments` と排他なので使わない） | #81 の案 (iii) の実装可能性 | `geoframe_tone` の lift はピーク x≈1/3 でハイライト不変。brightness は乗算なので白側も持ち上がる → 組み合わせの最適点を掃引（lift 3 点 × brightness 3 点 = 9、うち 2 本をマトリクスに） |
| C2 | 露出を下げて `CIHighlightShadowAdjust` で戻す | `settings.exposure_ev −0.7` + `diagnostics.post_filters: [CIHighlightShadowAdjust shadow 0.3..0.7, highlight 0..0.5]` | 局所適応型の戻し | 線形空間で掛かる・式非公開・ハロー【推測】 |
| C3 | **線形読み出し + 自前トーン圧縮** | `boost_amount 0` + `gamut_mapping false` + `post_filters: [CIExposureAdjust −1.43（2.7 → 1.0 に収める）, CIToneCurve（x 0..1 のショルダー: boost 1 の標本（§2.4）を再現し、元の 1.0 超＝x 0.37 超を 1.0 へ漸近）]` | 「表示白 = 2.7」のヘッドルームを全部使うトーンマッピング | **`CIToneCurve` は入力を [0,1] に clamp し、x > 1 の制御点も正しく扱わない**【確認: 定数 1.5 / 2.0 / 2.7 が同じ出力、x=2.7 の制御点で 0.5 → 0.32】ので、先に `CIExposureAdjust` で 0..1 へ収める。`apple_default` の見た目から離れる（色域マッピングも外れる）。本番採用はオーナー判断（§13 判断 4） |
| C3b | C3 + `CIExposureAdjust +0.3` | 中間調の落ち込みの補正 | 同上 | 同上 |
| `c_best` | 上記のうち preview で最良のもの（`mid_p50_diff` 内で `clip8_any` 最小・`grad_texture_ratio` 最大）を full で再実行 | — | 目視シート用 | — |

合否（候補ごと・カットごと）: (1) `|mid_p50_diff| ≤ 0.03`、(2) `clip8_any`（`sky_bright`）≤ カメラ JPEG、(3) `grad_texture_ratio` ≥ 0.7、(4) `fg_p01` カメラ比 ≥ −0.02、
(5) `magenta_rate` ≤ 0.01、(6) 目視でハロー・階調の段差・色転びが無い（4 列シート: camera / rawpy / `d_base` / 候補。全景 + `cloud_grad` 等倍 + `fg_dark` 等倍）。
**RAW デコーダ内で失われた情報（S0 の飽和、S3 の clamp 後）は後段で戻せない**。C1/C2 は S3 の後なので、露出を下げて S3 を通過させた分しか戻せない。C3 だけが S1 から作る。

---

## 10. 判定ロジック

### 10.1 ROI（`sky_bright` と `cloud_grad`）の帰属規則【決定】

> **改訂（2026-10-21）**: [診断レポート](../coreimage-highlight-diagnosis-report.md) §3.3・§6.4・§8（オーナー判断で採用）に従い、
> 規則 3 / 4 の名称と K の取り方を改めた（ハーネスの計測 3 回目 `diagnosis2` で実装）。規則の順序と判定式は変えていない。
> 旧名: 規則 3「色域・SDR 出力」（`gamut_output`）→「8bit 量子化」（`quant8`）、規則 4「検証不能」→「JPEG 符号化」（`jpeg_encode`）+「検証不能」（`unverifiable`）。
> 改めた理由は、計測で規則 3 の中身が白の手前 1/255 の丸め、規則 4 の中身がほぼ JPEG の符号化で、色域・色空間の損失は観測されなかったため。

白の画素集合 K = S3（既定の float dump）が 8bit sRGB で 255 に丸まる画素（いずれかのチャンネルが ≥ 0.9955）。dump が無いときだけ `d_lens_off`（B と同じ設定で lens OFF）の 8bit 出力でクリップした画素に戻す
（改訂前は常に 8bit 出力から取っていたが、lens OFF の JPEG で白の手前の画素が 254 に落ちて白を数え損なった。レポート §6.4）。
K の画素ごとに次の順で 1 つに帰属する（CFA マップ・S1〜S3 の dump・K はすべて lens OFF 幾何。§7.3）:

1. **センサー飽和**: 2×2 セルのいずれかの CFA 色が飽和（`T_libraw`）。S1 に何が入っていても優先（CI の埋め方は観測不能）。
2. **デコーダ/トーン**: 1 でなく、S2（トーン後）で 1.0 超。戻すレバーは exposure / boost / 自前トーン（S1〜S2 側）。
3. **8bit 量子化（白の手前 1/255）**: 1, 2 でなく、S2 で [0,1] なのに S3 が 8bit sRGB で 255 に丸まる（旧「色域・SDR 出力」）。
4. 1〜3 のどれにも該当しない画素（K を S3 から取る場合は構造上 0。K を 8bit 出力から取る場合だけ現れる）:
   - **JPEG 符号化**: S3 ≥ 0.98（白の手前で止まっているのに 8bit 出力で白）。
   - **検証不能**: それ以外（S3 < 0.98）。位置合わせ失敗・S1〜S3 の dump 欠損で照合できないときは、K 全体を検証不能にする（S3 が分からないので JPEG 符号化とは言わない）。

ROI の分類 = K に占める最大の帰属（≥ 50%）。2 位が 30% 以上なら「**混合**」。K が ROI の 1% 未満なら「クリップなし」。
`T_camera` で数え直した場合に分類が変わるかを `sensor_threshold_sensitive` に記録する。

### 10.2 シーンの判定

| シーン判定 | 条件 |
|---|---|
| センサー飽和 | `sky_bright` と `cloud_grad` の両方が「センサー飽和」 |
| デコーダ/トーン | 少なくとも `cloud_grad` が「デコーダ/トーン」で、exposure −0.7 または −1.0 で `grad_texture_ratio` ≥ 0.5（階調が実際に戻る） |
| 8bit 量子化（旧「色域・SDR 出力」） | 「8bit 量子化」が最大 |
| 検証不能（API 境界） | 「JPEG 符号化」+「検証不能」（= 改訂前の「検証不能」）が最大、または階調が S1 に無く S0 も飽和していない（CI の再構成段で消えている = 公開 API の外） |
| 混合 | 上記以外 |

事前実測（§2.1〜2.3）からの見込み【推測】: DSC04705 / DSC04754 / DSC04432 / DSC04462 は「混合（センサー + トーン）」、DSC04473 / DSC04607 / DSC04746 / DSC04579 / DSC04604 は「デコーダ/トーン」、
「色域・SDR 出力」（現「8bit 量子化」）はこのデータではほぼ出ない。

### 10.3 最終判断 A / B / C【決定】

| 判断 | 条件 | 接続 |
|---|---|---|
| **A: CI 継続** | 「デコーダ/トーン」「混合」のカットの **8/10 以上**で、タスク C の候補のいずれかが §9 の合否 (1)〜(6) を満たす。センサー飽和のカットは「どのエンジンでも回復不能」として A の妨げにしない | #81 へ候補の構成（C1 の lift/brightness 値、または C3 の曲線）と合否指標を引き渡す。ADR 0002 の「ハイライト: 条件付き」を「満たしている（条件: #81 の実装）」へ |
| **B: CI + 代替現像の併用** | 「検証不能（API 境界）」が **2 カット以上**あり、かつ rawpy（または他の現像）が同じ ROI で `grad_texture_ratio` ≥ 0.7 を同じ p50 で達成する | 対象写真の判定条件（例: `s1_gt1_any` と `cfa_sat_cell_any` の差が閾値以上）と LibRaw / darktable フォールバックの設計を別 Issue に。ADR 0002 追補 |
| **C: 未判定** | A でも B でもない（候補が合否を満たさないが API 境界とも言えない、またはセンサー飽和のカットばかりで判断材料が足りない） | 不足データ（太陽・夕景・夜景点光源）と追加実験を Issue に明記。#81 は C1（exposure + lift）を暫定採用するか保留かをオーナーが決める |

いずれの場合も、#71 の「条件付き」判定の根拠を本レポートの数値に差し替え、`docs/coreimage-engine-comparison.md` に §17 として追記する。

---

## 11. 実装タスクの分割（並列委譲の粒度）

### 11.1 固定する契約（並行開始の前に確定）

- §4.2〜4.5 の JSON（`settings` の 2 キー、`diagnostics` の入出力、dump の形式、`post_filters` の許可リスト）。
- バリアント名（§6.2 の `d_*` / `c*`）と CSV 列名（§8 の指標名）。
- 出力ディレクトリ `work/engine-comparison/diagnosis1/`、成果 `diagnosis_metrics.csv` / `diagnosis_summary.json` / `diagnosis_tables.md` / `rois.json` / `sheets/`。

### 11.2 タスク

| タスク | 所有ファイル | 内容 | 受け入れ条件 | 検証コマンド |
|---|---|---|---|---|
| **T1 Swift CLI 0.4.0** | `native/coreimage-cli/Sources/CoreImageDevelop/{Request,Develop}.swift`、新規 `Diagnostics.swift`、`Tests/CoreImageDevelopTests/{RequestParserTests,SmokeTests}.swift`、新規 `DiagnosticsTests.swift`、`native/coreimage-cli/README.md`、`cliVersion` | §4 の契約を実装。`settings.gamut_mapping` / `baseline_exposure`、`diagnostics`（stats / dump / linear_space_filter / post_filters / skip_write）、応答 `diagnostics`、`--capabilities` | `swift test` 全件合格。§4.6 のスモーク (a)〜(f)。`scripts/coreimage-regression-applied.sh <ARW フォルダ>` 差分 0 件。`applied` キー集合不変（スモークで既定応答のキー集合を 0.3.0 の固定リストと比較） | `cd native/coreimage-cli && swift build -c release && swift test`、`npm run build:coreimage && scripts/coreimage-regression-applied.sh ~/works/60-misc/40-raw-assistant/work/raw` |
| **T2 Python ハーネス `--diagnosis`** | `scripts/compare_engines.py`（新節「診断モード --diagnosis」。既存関数は変更しない。`coreimage_request` / `run_coreimage` に `diagnostics` / `extra_settings` 引数を**追加**するのみ）、`tests/test_compare_engines.py`（追記） | §5 CFA 飽和、§6 マトリクス実行、§7 ROI・位置合わせ、§8 指標、§9 候補、§10.1〜10.2 の分類、シート、CSV/JSON/MD 出力。dump は指標後に削除（`--keep-dumps`） | 純粋関数の単体テスト（CFA 合成、ROI 抽出、帰属規則、内挿、stage_loss）合格。`--highlight` の出力が従来と同一（既存テストと `highlight1` の再実行で CSV 差分なし）。T1 のビルド済み CLI で 1 カット（DSC04607）のドライランがエラー 0 | `.venv/bin/python -m pytest tests/test_compare_engines.py`、`.venv/bin/python scripts/compare_engines.py --source-root /Volumes/Backup/100MSDCF --captures DSC04607 --diagnosis --out work/engine-comparison/diag-smoke` |
| **T3 計測とレポート** | `docs/coreimage-engine-comparison.md`（§17 追記）、`docs/coreimage-highlight-diagnosis-report.md`（新規: 判定・根拠・再現条件・API 境界）、`docs/adr/0002-coreimage-engine-decision.md`（追補）、`docs/coreimage-baseline/`（必要なら）| 10 + 3 カットの本実行、目視シートの確認（オーナー）、§10 の判定、#81 / ADR への接続 | DoD 1〜6 を満たす記述。画像はコミットしない（CSV / JSON / MD のみ。位置情報・撮影地を書かない） | `.venv/bin/python scripts/compare_engines.py --source-root /Volumes/Backup/100MSDCF --captures DSC04473 DSC04607 DSC04532 DSC04705 DSC04579 DSC04604 DSC04754 DSC04432 DSC04746 DSC04462 --diagnosis --full --full-ids DSC04473 DSC04607 DSC04705 --out work/engine-comparison/diagnosis1` |

### 11.3 順序と依存

1. 契約（§4）確定 → T1 と T2 を**並行**（T2 は契約の JSON をモックして純粋関数とフローを先に作り、T1 完成後にドライラン）。worktree は `wt-92-t1-cli` / `wt-92-t2-harness`。
2. T1 完了 → 回帰スクリプト差分 0 をメインが確認 → T2 のドライラン → 本実行（T3、メインまたは委譲）。
3. T3 の判定 → #81 のコメントと ADR 追補。PR は 1 本（CLI + ハーネス + レポート）か、CLI を先に分けるか（§13 判断 7）。

---

## 12. リスク・限界

- CI 側の事前実測は 1 カット・1200px。トーン曲線の形・gamut の clamp 挙動が ISO やカメラ既定値で変わる可能性【推測】→ §8.4 の検査を全カットで回す。
- `outputImage` の二重参照で extent が縮む挙動（§2.2）は OS 依存の可能性。スモークで検出する。
- 線形読み出し（S1）は CI の再構成・NR・シャープを含む「CI の線形段」であって RAW 線形ではない。センサー飽和画素の値は信用しない（帰属規則 1 で先に除く）。
- `camera_white_level` 15360 と LibRaw の 16380 のどちらが真の飽和かは不明【推測】。両方で分類し、感度を記録する。
- サンプルは明るい空 10 カット。太陽そのもの・夕景の太陽・夜景の点光源は無い → これらのシーンは「データ不足」としてレポートに明記し、判定 A を全シーンへ一般化しない。
- 目視はブラインドでない。
- dump の 288 MB × full を複数同時に書かない（直列）。`work/` の空き容量を実行前に確認する（≥ 5 GB）。

---

## 13. オーナー判断が要る事項（推奨案付き）

| # | 事項 | 推奨 |
|---|---|---|
| 1 | `gamut_mapping` / `baseline_exposure` を `settings`（#79 流儀）に置くか `diagnostics` に閉じ込めるか | **`settings`**。CIRAWFilter のプロパティで読み戻しも既存。本番で使う予定が無いことは README に書く |
| 2 | float ダンプの形式（生 `.f32` + JSON メタ vs float TIFF） | **生 `.f32`**（検証コスト最小、Python で 1 行）。将来 #89 の線形出力モードで正式形式を決める |
| 3 | `post_filters`（許可リスト制の診断用後段）を CLI に入れるか、タスク C の候補を固定コードにするか | **許可リスト制**。候補の掃引を Swift 再ビルド無しで回せる。本番は #81 で正式な写像にする |
| 4 | C3（`boost 0 + gamut OFF + 自前トーン`）を本番の選択肢として検討してよいか（`apple_default` の見た目から離れる） | **検討対象に含める**（A の判定材料）。採用の可否は目視シートを見て決める |
| 5 | 「同じ明るさ」の許容幅 `mid_p50_diff` ±0.03、タスク C 合否の `grad_texture_ratio` ≥ 0.7 | この値で開始し、結果を見て較正（§16 の流儀） |
| 6 | センサー飽和の主閾値（LibRaw 16380 − 32 vs camera 15360 − 32） | **LibRaw 側を主**、camera 側を副列。分類が変わるカットは「混合」扱い |
| 7 | PR の分け方 | CLI 0.4.0（T1）を先に小さな PR にし、ハーネス + レポートを後続 PR にする（回帰確認を独立に通せる） |
| 8 | サンプル不足（太陽・夕景・夜景点光源）の扱い | 今回は追加撮影しない。レポートで「未検証シーン」として列挙し、判定の適用範囲を限定する |
| 9 | 成果物のコミット範囲 | CSV（指標）/ JSON（summary、rois）/ MD はコミット、画像・dump はしない。位置情報は一切書かない |
| 10 | CLI の統計を全画面のみとし、ROI 統計を Python 側に置く（指示の例では `diagnostics` に ROI 指定があった） | **全画面のみ**。ROI はカメラ JPEG 基準の抽出と位置合わせが要り、CLI に入れると契約が肥大する。dump があれば Python で同じ値が出せる |

---

## 14. 事実と推測の区別（まとめ）

- 【確認】: #72/#73 の既定（§1.1）、既存の道具（§1.2）、CFA 飽和 10 カット（§2.1）、S1/S2/S3 の切り出しと gamut の clamp（§2.2、1 カット）、青チャンネルの算術（§2.3、1 カット）、
  `linearSpaceFilter` の恒等 / 定数置換と exposure の順序（§2.4、1 カット）、`baselineExposure` の上書き可否と冗長性（§2.5、1 カット）、コスト（§2.6）、`outputImage` の二重参照で extent が縮む（§2.2）、`CIToneCurve` が入力を [0,1] に clamp する（§9、合成定数画像）。
- 【推測】: 10 カットでの再現性（§8.4 で検査）、各カットの帰属の見込み（§10.2）、`CIHighlightShadowAdjust` の線形空間での効き（§9）、所要時間（§6.3）、`camera_white_level` の意味（§12）、
  トーン曲線が大域的であること（§2.4 の検査で確定させる）。
