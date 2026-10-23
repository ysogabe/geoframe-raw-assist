# ADR 0002: Core Image（CIRAWFilter）エンジンの採否

- ステータス: 承認（2026-10-20、オーナーの目視判断による。当初の提案から決定を改訂。経緯は末尾の「決定の経緯」）
- 関連: Issue #65、[ADR 0001](0001-coreimage-raw-poc.md)（CLI の設計）、
  [比較レポート](../reports/coreimage-engine-comparison.md)、[パラメータ対応表](../development/coreimage-parameter-mapping.md)

## 背景

Issue #65 では、Swift / Core Image の CLI を Tauri から子プロセスとして直接呼ぶ現像経路を PoC として作り
（PR1: CLI、PR2: Tauri 統合とフラグ付き比較 UI、PR3: 比較ハーネス）、
カメラ JPEG・rawpy（現行エンジン）・Core Image を α6400 の ARW 21 カットで比較した。
この ADR はその結果から採否を決める。

## 判断材料（比較レポートの要約）

| 観点 | rawpy（現行） | Core Image | 評価 |
|---|---|---|---|
| 処理時間 preview / full（中央値） | 3.71 s / 4.49 s | 0.77 s / 1.60 s（レンズ補正 ON で 1.30 s / 2.15 s） | Core Image が 2〜5 倍速い |
| ピーク RSS | 1.9〜2.6 GB | 約 225 MB（レンズ補正 ON で約 290 MB） | Core Image が約 1/8 |
| 明るさ（カメラ JPEG との輝度差） | −0.23〜+0.28（auto_bright でカットごとに振れる） | −0.08〜+0.02 | Core Image がカメラに安定して近い |
| ノイズ（高 ISO） | 輝度・色ノイズが目立つ | 少ない | Core Image 優位 |
| 解像感（ピント甘さ。比較レポート §9） | 原寸でエッジ幅がカメラ比 1.16 倍（シャープ OFF だと 1.35 倍）。preview は 1.05 倍 | 設定次第。neutral v9 は 1.41〜1.44 倍で最も甘い（細部が水彩・蝋状）。neutral v8 は 1.29〜1.32 倍で rawpy シャープ OFF 並み。apple_default は 0.84 倍でカメラに最も近い | 下記参照 |
| ハイライト（逆光） | 白飛び率が高く、境界にマゼンタの偽色が出るカットあり | rawpy よりは少ないがカメラより多い | どちらも課題 |
| 色 | 薄明の空が濃い青寄り | 彩度高め、薄明でマゼンタ寄り | 好みの範囲。要調整 |
| レンズ補正 | lensfun（暗シーン判定時は周辺減光補正をスキップ） | 内蔵（カメラの補正と幾何が近い）。中身は非公開 | Core Image が簡便 |
| 調整パラメータ | 現行 Adjustments すべてに対応 | 露出・WB(K/tint) のみ直接。他は後段 CIFilter での再実装が必要 | rawpy 優位 |
| 依存・配布 | Python + .venv + localhost サーバ | OS 標準のフレームワークのみ（macOS 14 以上） | Core Image 優位（macOS 限定） |
| 夜景・星空（比較レポート §10、11 カット・1 セッション） | 空ノイズがカメラ比 1.72、色ノイズが多い | 星の残存率 ≥ 0.991、空ノイズ 1.05、空の輝度差 −0.013〜+0.016 | Core Image 優位（このサンプルの範囲で） |
| 目視（オーナー、等倍クロップ） | 偽色・色ノイズが多い | 好ましい（`apple_default` v9 lens ON） | Core Image 優位（下記） |

### オーナーの目視評価（2026-10-20）

比較レポート §9 の等倍クロップシート（`work/engine-comparison/sharpness1/sheets/`。日中・薄明の屋外と室内のカットのみで、
夜景・星空は含まない）をオーナーが確認し、**rawpy は偽色・色ノイズが多く、Core Image の方が好ましい**と判断した。
基本路線とする設定は **CI `apple_default`・decoder 9・レンズ補正 ON**（比較レポートのバリアント名 `ci_apple_v9_l1`）。
これは数値指標ではなく目視による評価だが、§9 の数値（エッジ幅比 preview 0.95 / 原寸 0.84、ノイズ比 preview 1.02 / 原寸 0.81。
rawpy 現行は 1.05 / 1.16、1.55 / 2.20）とも整合する。

### 解像感（ピント甘さ）についての要点

- **Core Image に替えるだけではピント甘さは解決しない。** シャープ 0 同士の素の解像は rawpy（AHD）≒ CI decoder 8 ＞ CI decoder 9。
  CI v8 に現行と同じ撮影時シャープを後から掛けると rawpy 現行とほぼ同じ見え方になる。差を決めているのはエンジンではなく
  シャープと NR の掛け方。
- **decoder 9 の neutral は使わない。** 原寸で最も甘く、レンズ補正 ON の preview では高 ISO の暗部に白い点状ノイズが出る
  （PR3 で「レンズ補正 ON で粒状ノイズが戻る」と見えたのはこの白点で、ディテールが戻ったわけではなかった）。
- **apple_default（デコーダ内のシャープ・NR 既定）はカメラ JPEG に最も近いキレ**（エッジ幅で rawpy 現行より preview 29/29、
  原寸 12/12 カットで鋭い）。ただし内部パラメータは非公開で OS 更新で変わりうる。局所コントラストが強めで硬い質感・白い粒が出る箇所もある。
- **現行 rawpy の残課題は原寸のシャープ強度不足**（カメラ比 16% 太い）。rawpy の撮影時シャープを原寸で強める調整は、
  エンジン置き換えよりはるかに小さい変更で効く可能性があり、先に試す価値がある。
  FullSizeViewer が 1600px プレビューを引き伸ばす問題はエンジンと独立した UI の課題。

## 決定

**Core Image（`apple_default`・decoder 9・レンズ補正 ON）を現像エンジンの基本路線とし、rawpy から段階的に移行する。
移行の条件（下記）を満たすまでは、本番の現像は rawpy のまま維持する。**

1. 基本設定は CIRAWFilter の `apple_default` profile（デコーダ内のシャープ・NR は既定値）、decoder 9、レンズ補正 ON とする。
   neutral profile（特に decoder 9 の neutral）は原寸で甘く、preview で白点ノイズが出るため本番の基本設定には使わない
   （白点ノイズは `apple_default` では出ないことを比較レポート §9.6 で確認済み）。
2. 移行は「プレビュー（一覧・比較用の軽量現像）→ 原寸・書き出し」の順に進める。プレビューは調整パラメータが不要で、
   速度・メモリの差が最も効くため先に置き換える。原寸・書き出しは Adjustments の対応が揃ってから置き換える。
3. 移行が終わるまでは、プリセット・Adjustments・書き出し・PSO・ブラケット合成・星景は rawpy で行う。
   Core Image 経路は当面 `GEOFRAME_COREIMAGE_EXPERIMENT=1` のフラグ付きのまま残し、段階ごとに既定へ切り替える。
4. `apple_default` の内部パラメータは非公開で OS 更新で変わりうるため、**OS 更新ごとに比較ハーネス
   （`scripts/compare_engines.py`、`--sharpness` を含む）を再実行して回帰を確認する**ことを常設の運用とする。
5. rawpy は移行完了後に取りやめる。そのため、rawpy の原寸の撮影時シャープ再調整（当初の提案でピント甘さ対策の
   第一手としていたもの）は行わない。
6. 対応 OS は最新の macOS で動作すればよいものとし、古い macOS（14 / 15）での個別の検証は行わない。

### 置き換えに進む条件

| 条件 | 状態 |
|---|---|
| 解像感が rawpy 現行以上（`apple_default` v9 lens ON） | **満たしている**（比較レポート §9：エッジ幅比 preview 0.95 / 原寸 0.84、rawpy 現行 1.05 / 1.16） |
| 夜景・星空・長秒のサンプルで破綻（ノイズの塗りつぶし、星の消失、色かぶり）が無い。`apple_default` の NR が星を消さないことを特に確認する | **満たしている（サンプルの範囲で）**（比較レポート §10・§14。2022-11 の 1 セッション、ISO 3200 / 6400、明るめの空。暗い空・微光星の多い星野、周辺減光の大きいレンズは未確認。#70） |
| ハイライト（逆光）の白飛び・偽色が rawpy 現行より悪くない | **満たしている（2026-10-21、#92 の判定 A。#81 の写像と救済系プリセットでの計測・目視を 2026-10-22 に完了）**（比較レポート §13・§17、[診断レポート](../reports/coreimage-highlight-diagnosis-report.md)。偽色は rawpy 現行より明確に少ない。白飛びは**線形段で表示白を超えた画素**が主因で（CFA 飽和は中央値 1.8%、CI の白飛び 13.0%、カメラ JPEG 3.0%）、`exposure` −0.7 + `shadow_lift` 40（brightness 1.0）で白飛び率 3.6%・中間調 CI 既定 ±0.03 になる。合否の基準は CI 既定比に較正し、目視もオーナーが合格と判断した（2026-10-21）。#81 でこの構成を `highlight_recovery` の写像（hr 60 ≡ C1'、hr 90 ≡ 強め）として CLI 0.6.0 に実装し、等価性を確かめた（2026-10-22）。救済系プリセットでの計測（空 10 カットの白飛び率の中央値 45% → 0〜3%、中間調 CI 既定 ±0.03）と目視の合格は [再調整レポート](../reports/coreimage-preset-retune-report.md)。太陽の直射は未確認。#71 / #92 / #81） |
| 使う範囲の Adjustments（少なくとも exposure / wb_temp / shadow_lift / saturation / sharpness）について、後段処理を含めて現行と同等の結果になる対応が実装・検証されている（原寸・書き出しの置き換えの条件。プレビューには不要） | 未着手 |
| 最新の macOS で decoder 9 と `apple_default` が使える（decoder 9 は必須の依存になった。古い macOS の個別検証は不要） | **満たしている**（2026-10-20、macOS 27.0.1。CLI 単体と `tauri:build` した .app の同梱 CLI。#74） |
| OS 更新時の回帰比較の手順が文書化されている | **満たしている**（[回帰確認手順](../development/coreimage-regression-check.md)とベースライン。#75） |

### 評価で分かった注意点（2026-10-20、比較レポート §12・§13）

- decoder 9 の白点ノイズは「輝度 NR 0 かつ sharpness 0」の縮小 preview でだけ出る。`apple_default` は既定でどちらも 0 でないため出ず、NR だけ 0 にしても出ない。
  星景などで NR を切る場合は sharpness を 0 にしないこと。
- `apple_default` は明るい空（主に青チャンネル）を白飛びさせやすい。`extended_dynamic_range` を上げると白飛びはむしろ増え、`boost` / `boost_shadow_amount` はほぼ効かない。白飛びを減らすのは `exposure` を下げることで、中間調は `shadow_lift` などで戻す（#81）。
- `src-tauri/src/coreimage.rs` の試験経路の既定は `neutral`（白点が出る組み合わせ）のままなので、#72 で `apple_default` に切り替える。

## 追補（2026-10-21）: 段階 1「プレビューの CI 化と調整の写像」完了（#72/#73）

- 決定 2・3 の段階 1 を実装した。Tauri のプレビューは既定で Core Image（`apple_default`・decoder 9・レンズ補正 ON）になり、
  Adjustments の 9 キーを CLI 0.3.0 に写像した（`highlight_recovery` だけは no-op で、代替は #81）。
  設計は [設計書](../design/coreimage-preview-adjustments.md)、較正結果は [比較レポート §16](../reports/coreimage-engine-comparison.md)、写像は [対応表](../development/coreimage-parameter-mapping.md) の「#73 での決定」。
- 上の「置き換えに進む条件」の Adjustments の行は**原寸・書き出し向け**の条件で、引き続き未達（プレビューの検証は §16 で完了）。
- **プレビュー = Core Image、本現像・書き出し = rawpy の間は、見た目が一致しない場合がある**（明るさの基準、色、ノイズなど）。
  オーナーは、移行が完了するまでこの不一致を受容した（2026-10-20 判断）。UI には「Core Image プレビュー」のバッジと注記を出す。
- 切り戻し: 環境変数 `GEOFRAME_PREVIEW_ENGINE=rawpy`（値がちょうど `rawpy` のときだけ）で Python（rawpy）プレビューに戻せる。
  旧 `GEOFRAME_COREIMAGE_EXPERIMENT` と試験用 UI は廃止した。CLI の起動失敗などインフラ側のエラー時は、自動で Python プレビューにフォールバックする。
- 決定 3 の「`GEOFRAME_COREIMAGE_EXPERIMENT=1` のフラグ付き」と、注意点の「試験経路の既定は `neutral`」は、この追補で解消済み。

## 追補（2026-10-21）: ハイライト白飛びの段階別診断（#92）

- 比較レポート §13 の「白飛びはカメラ JPEG より多い。レバーは `exposure` だけ」を段階別に切り分けた（CLI 0.4.0 の診断モード、`scripts/compare_engines.py --diagnosis`、10 + 3 カット。
  [診断レポート](../reports/coreimage-highlight-diagnosis-report.md)、[比較レポート §17](../reports/coreimage-engine-comparison.md)）。
- **結論（事実）**: 白飛びは線形段（WB ゲイン × baselineExposure 0.4EV）で表示白 1.0 を超えた画素が、boost のトーン（1.0 固定点の大域的な曲線）と色域マッピング（1.0 への clamp）で白になるもの。
  センサー（CFA）飽和は白飛びの一部（中央値 1.8% vs 13.0%）で、空が主役のカットでは ROI の 57〜99% が「デコーダ/トーン」帰属（計測 3 回目）＝`exposure` を下げれば階調が戻る。トーン・色域・16bit・8bit の各段に新しい損失はほぼ無い。
  公開 API には白の手前にニーを入れるレバーが無く、EDR は逆効果、boost は 13.0 → 10.9% まで。
- **判定（設計書 §10.3）**: **A（CI 継続）。オーナー判断（2026-10-21）で確定**。「暗くしないハイライト保持」の候補 C1'（`exposure` −0.7 + `shadow_lift` 40 + brightness 1.0）は、
  合否の基準を CI 既定（`d_base`）比に較正すると preview 10/10 カットで自動判定を満たし（原寸 3 カットでは境界の偽色の閾値で 1 カット不合格）、目視もオーナーが「Core Image の候補で問題なし」と判断した。
  B（CI + 代替現像の併用）に該当するカットは無い。経緯: カメラ JPEG を基準に厳格に読むと C（未判定。C1' 単独で 5/10、いずれかの候補で 6/10）だった（カメラの DRO で中間調・暗部が CI 既定より明るいカットが半数）。
- **決定への反映**: 「置き換えに進む条件」のハイライトの行を「満たしている（条件: #81 の実装）」に更新した（設計書 §10.3 の A の接続）。基本路線（`apple_default` v9 lens ON）は変えない。Core Image を優先して進める。
  C1' は #81 に引き継いだ（https://github.com/ysogabe/geoframe-raw-assist/issues/81#issuecomment-6303983717）。#81 の完了条件を「CI 既定より白飛び率が下がり、中間調が CI 既定 ±0.03 以内」に改める案も伝えた（採否は #81）。
  #81 は C1' の構成を `highlight_recovery` の写像として実装する。`brightness` > 1 で戻す案と、gamut OFF + 自前トーン（C3）は採らない（前者は白側が戻る、後者は偽色）。
- 注意点（2026-10-21、診断レポート §4.3・§6）: `CIToneCurve` の制御点は sRGB ガンマ符号化した座標で、補間は 5 点の 3 次スプライン（平坦な区間で振動）。
  後段 `geoframe_tone` の `brightness` はガンマ空間の乗算で、1.1 でも線形 0.80 以上が白に戻る。色域マッピングは低彩度では 1.0 への clamp だが、高彩度では最大 0.2 動く。

## 追補（2026-10-22）: `highlight_recovery` の写像とプリセットの再調整（#81）

- CLI 0.6.0 で `highlight_recovery` を区分線形の写像 `0 → (0, 0)`・`60 → (−0.7EV, +40)`・`90 → (−1.0EV, +55)`・`100 → (−1.1EV, +60)` に置き換え、
  `exposure` / `shadow_lift` に加算する（[設計書](../design/coreimage-preset-retune.md) §2・§3、[対応表「#81 での決定」](../development/coreimage-parameter-mapping.md)）。
  `{0, 0, hr 60}` と `{−0.7, 40, hr 0}` は同じ画（バイト一致）で、#92 で承認した C1' にそのまま結び付く。
- プリセット（Rust = プレビュー）は CI 既定（`d_base`）を基準に再調整した。非救済 8 プリセットは hr 0、hr > 0 は救済系 3 プリセットだけ（オーナー判断 2026-10-22、案 B）。
  書き出し（rawpy）は旧値のまま。プレビューと書き出しの値の差は受容（#90 で解消）。
- 「置き換えに進む条件」のハイライトの行の条件を「#81 の実装」から「救済系プリセットでの計測と目視」に改めた。計測（10 カット + シーン別 8 カット）と目視は
  [再調整レポート](../reports/coreimage-preset-retune-report.md) を参照。2026-10-22 に計測（条件 1〜9）と目視（条件 10）を終え、条件を外した。
  案 C（`neutral_recovery` / `landscape_clear` に hr 30）は採らない。

## 追補（2026-10-23）: 暗所判定（`dark_scene`）の廃止と移行状況（#88 → #117）

- **暗所判定は N1（廃止）を採用し、N2（星空なら NR 0）は採らない。** #88 から #117 に引き継いだ要件で、Core Image 経路には
  rawpy 時代の暗所判定（`dark_scene`：固定倍率・周辺減光補正のスキップ）に相当する処理を設けない。
- 根拠は [比較レポート §10.7](../reports/coreimage-engine-comparison.md) の 6 項目（#70。α6400 夜間・長秒 11 カット、1 セッション）で、すべてが N1 を支持する:
  1. `apple_default` v9 lens ON の NR は星を消していない（強い星の残存率 11/11 カットで ≥ 0.991、NR 0 でも変わらない）。
  2. 空の明るさはカメラ比 −0.013〜+0.016 で、Issue #50（暗いシーンが浮く）の再発に当たらない。
  3. 空のノイズは rawpy 現行より少なく（sky_noise_ratio 1.05 vs 1.72）、塗りつぶしも無い。
  4. 白点は `apple_default` では実質出ない（大量に出るのは NR 0 かつ sharpness 0 の preview。§12）。
  5. 周辺減光補正は四隅のノイズを増やさない（lens ON/OFF で corner_noise_ratio 0.66 / 0.74）。CI の補正は四隅をほとんど明るくしない。
  6. 星の色の白飛びは rawpy より僅かに多い（0.064 vs 0.052）が、暗所判定で扱う性質のものではない（シャープとコントラストによる）。
- N2 を採らない理由: NR 0 の利点は数値上小さく（星の残存率は変わらず、空ノイズは 1.05 → 1.36 に増える）、decoder 9 では
  sharpness を 0 にしないことが条件になる（§12）。既定の `apple_default` のままで足りる。
- `dark_scene` キーは Core Image の現像結果（`DevelopedResult`）・レシピに持たない（#108。`f3e24b0` 時点で `src-tauri/src` に `dark_scene` は無い）。
- **未確認（実素材待ち）**: 上の 5 は 2 本のレンズでの結果で、10-18mm など周辺減光の大きい他レンズの四隅ノイズは確認していない。
  素材が手に入った時点で `scripts/compare_engines.py --night` を再実行して確かめる。それまでこの点を「確認済み」とは扱わない。
  微光星の多い暗い空での NR の差も同様に未確認（§10.7・§15）。

### 移行状況（2026-10-23 時点）

- `f3e24b0` で、プレビュー・原寸（本現像）・書き出し・ブラケット合成・候補生成（PSO と代理デコード）・解析がすべて Core Image / Rust に移った
  （#108 / #83 / #84 / #87 ほか）。Python サーバ・rawpy・`GEOFRAME_PREVIEW_ENGINE` による切り戻しは無い（[ADR 0003](0003-python-removal-single-app.md)）。
  決定 3（移行が終わるまでは rawpy で行う）と、段階 1 の追補の切り戻し・フォールバックの記述は、この時点で役目を終えた。
- 「置き換えに進む条件」の Adjustments の行（原寸・書き出し向け）は、この追補では状態を更新しない。代理（640px）と原寸の見え方の計測は
  #57 で行い、[11枚の計測結果](../reports/coreimage-proxy-full-measurement.md)にまとめた。本番の原寸ROI再評価は次の修正範囲とする。
- rawpy 時代の閾値（候補生成の発動値・評価の目安など）は、Core Image 経路で採用したものではない。候補生成の評価で
  昼夜混在 13 ケース（`image_work`）は元 ARW が無く未比較のまま残っており、現行で比較済みの星空 11 ケースとは別に扱う
  （[PSO 移植の報告](../reports/coreimage-pso-loop-report.md)）。

## 理由

- 速度（2〜5 倍）とメモリ（約 1/8）の差が大きく、明るさ・ノイズ・解像感（`apple_default`）でカメラ JPEG に近い。
- オーナーの目視で、rawpy の偽色・色ノイズに対して Core Image の画が好ましいと判断された。
- 一方で、夜景・星空（このアプリの重要な被写体）は未評価で、Adjustments の大半が未対応である。
  一度に置き換えると品質と機能の両方で後退する恐れがあるため、条件付きで段階的に移行し、それまでは rawpy を残す。

## 決定の経緯

当初（提案時）は「rawpy を置き換えず、Core Image は試験経路として残す。Core Image を使う場合は decoder 8 を固定し、
『neutral + 現行と同じ撮影時シャープ』と『apple_default』の 2 案を並べる。ピント甘さはまず rawpy の原寸シャープ再調整で対処する」
としていた。2026-10-20 にオーナーが等倍クロップを目視し、`apple_default` v9 lens ON を基本路線とすることを決めたため、
上記の決定に改訂した。decoder 8 固定の理由（decoder 9 neutral の平滑化と白点ノイズ）は `apple_default` には当てはまらない。

## 後続 Issue の案

優先度順。2026-10-20 にオーナーの了承を得て Issue として立てた。

1. #70 夜景・星空・長秒のサンプルを追加して比較ハーネスを再実行する（`apple_default` v9 lens ON を中心に。星の残り方・NR の塗りつぶしを確認）。
2. #71 逆光・ハイライトの比較（`apple_default` の白飛び率・偽色、EDR / ハイライト関連パラメータの影響）。
3. #72 プレビュー現像の Core Image 化の設計と実装（`apple_default` v9 lens ON。キャッシュ、ブラウザモードでの代替、フラグから既定への切り替え）。
4. #73 Adjustments から Core Image（CIRAWFilter と後段 CIFilter）への対応の実装（exposure / wb_temp / shadow_lift / saturation / sharpness から。
   `apple_default` のシャープ・NR と Adjustments の sharpness の関係を含む）。
5. #74 最新の macOS での動作確認（decoder 9・`apple_default` が使えること。古い macOS の個別検証は不要）。
6. #75 OS 更新時の回帰比較の手順化（比較ハーネスの実行手順と合否の目安を文書化）。
7. #76 `env -u CI npm run tauri:build` の DMG 作成（bundle_dmg.sh）の失敗の調査（#65 とは独立）。
8. #77 （低）decoder 9 neutral の平滑化と、レンズ補正 ON の preview での白点ノイズの原因調査。
