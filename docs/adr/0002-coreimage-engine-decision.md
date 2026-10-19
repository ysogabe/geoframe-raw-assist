# ADR 0002: Core Image（CIRAWFilter）エンジンの採否

- ステータス: 提案（オーナー承認待ち）
- 関連: Issue #65、[ADR 0001](0001-coreimage-raw-poc.md)（CLI の設計）、
  [比較レポート](../coreimage-engine-comparison.md)、[パラメータ対応表](../coreimage-parameter-mapping.md)

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
| 夜景・星空 | 未評価 | 未評価 | **サンプル 0 件で判断不能** |

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

## 決定（提案）

**現時点では現行エンジン（rawpy）を置き換えない。Core Image は「試験経路」として残し、
条件を満たしたら段階的に採用範囲を広げる。**

1. 本番の現像（preview / full、プリセット、Adjustments、書き出し）は引き続き rawpy で行う。
2. Core Image 経路（PR1・PR2）は `GEOFRAME_COREIMAGE_EXPERIMENT=1` のフラグ付きで残す。既定では表示しない。
3. 次の段階として、**プレビュー（一覧・比較用の軽量現像）を Core Image に置き換えること**を第一候補にする。
   速度・メモリの差が最も効き、調整パラメータが不要な用途であるため。
4. Core Image で使う設定は、decoder 9 の平滑化と白点ノイズの原因が分かるまで **decoder 8 を明示的に固定**して基準にする。
   見た目の仕上げは「neutral + 現行と同じ撮影時シャープ」か「apple_default」の 2 案を候補として並べ、
   後者を採るなら OS 更新ごとの回帰比較（比較ハーネスの再実行）を前提にする。
5. ピント甘さへの対策は、エンジン置き換えとは切り離して、まず **rawpy の原寸の撮影時シャープ強度の再調整**を行う。

### 置き換えに進む条件

- decoder 9 の平滑化・白点ノイズの原因が分かる、または decoder 8 / apple_default で解像感が rawpy 現行以上であることを再確認している。
- 夜景・星空・長秒のサンプルで、破綻（ノイズの塗りつぶし、星の消失、色かぶり）が無いことを確認している。
- 使う範囲の Adjustments（少なくとも exposure / wb_temp / shadow_lift / saturation / sharpness）について、
  後段処理を含めて現行と同等の結果になる対応が実装・検証されている。
- macOS 14 / 15 で decoder 9 が使えること、出力が一致することを確認している。

## 理由

- 速度とメモリの差は大きく、明るさとノイズの点でもカメラ JPEG に近い。採用の動機としては十分ある。
- 一方で、原因不明の平滑化があり、夜景・星空（このアプリの重要な被写体）は未評価で、
  Adjustments の大半が未対応である。今置き換えると、品質と機能の両方で後退する恐れがある。
- Python エンジンを残せば、既存の機能（プリセット、PSO、ブラケット合成、星景）に影響を与えずに検証を続けられる。

## 後続 Issue の案

採否が承認されたら、次を Issue として立てる。

1. decoder 9 の平滑化（neutral）と、レンズ補正 ON の preview での白点ノイズの原因調査（decoder 8 との比較、ほかのパラメータの影響）。
2. rawpy の原寸の撮影時シャープ強度の再調整（カメラ比 16% 太いエッジの解消。`engine.SHARPEN_*` を比較ハーネスの `--sharpness` で before/after 比較。ENGINE_VERSION 18）。
3. 夜景・星空・長秒のサンプルを追加して比較ハーネスを再実行する。
4. Adjustments から Core Image（CIRAWFilter と後段 CIFilter）への対応の実装（exposure / wb_temp / shadow_lift / saturation / sharpness から）。
5. プレビュー現像の Core Image 化の設計（キャッシュ、ブラウザモードでの代替、フラグの扱い）。
6. macOS 14 / 15 での動作確認と、`Package.swift` の対象バージョン（現状 macOS 13）をアプリの最低要件 14.0 に揃える。
7. `env -u CI npm run tauri:build` の DMG 作成（bundle_dmg.sh）の失敗の調査（#65 とは独立）。
