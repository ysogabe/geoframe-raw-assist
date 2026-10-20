# ADR 0002: Core Image（CIRAWFilter）エンジンの採否

- ステータス: 承認（2026-10-20、オーナーの目視判断による。当初の提案から決定を改訂。経緯は末尾の「決定の経緯」）
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
5. rawpy の原寸の撮影時シャープ再調整（当初の提案でピント甘さ対策の第一手としていたもの）は、主対策から外す。
   移行完了までの暫定改善として必要なら行う（優先度は低）。

### 置き換えに進む条件

| 条件 | 状態 |
|---|---|
| 解像感が rawpy 現行以上（`apple_default` v9 lens ON） | **満たしている**（比較レポート §9：エッジ幅比 preview 0.95 / 原寸 0.84、rawpy 現行 1.05 / 1.16） |
| 夜景・星空・長秒のサンプルで破綻（ノイズの塗りつぶし、星の消失、色かぶり）が無い。`apple_default` の NR が星を消さないことを特に確認する | 未確認（最重要） |
| ハイライト（逆光）の白飛び・偽色が rawpy 現行より悪くない（`apple_default` では未評価。比較レポート §8） | 未確認 |
| 使う範囲の Adjustments（少なくとも exposure / wb_temp / shadow_lift / saturation / sharpness）について、後段処理を含めて現行と同等の結果になる対応が実装・検証されている（原寸・書き出しの置き換えの条件。プレビューには不要） | 未着手 |
| macOS 14 / 15 で decoder 9 と `apple_default` が使え、出力が同等である（decoder 9 は必須の依存になった） | 未確認 |
| OS 更新時の回帰比較の手順が文書化されている | 未着手 |

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

優先度順。オーナーの了承後に Issue として立てる。

1. 夜景・星空・長秒のサンプルを追加して比較ハーネスを再実行する（`apple_default` v9 lens ON を中心に。星の残り方・NR の塗りつぶしを確認）。
2. 逆光・ハイライトの比較（`apple_default` の白飛び率・偽色、EDR / ハイライト関連パラメータの影響）。
3. プレビュー現像の Core Image 化の設計と実装（`apple_default` v9 lens ON。キャッシュ、ブラウザモードでの代替、フラグから既定への切り替え）。
4. Adjustments から Core Image（CIRAWFilter と後段 CIFilter）への対応の実装（exposure / wb_temp / shadow_lift / saturation / sharpness から。
   `apple_default` のシャープ・NR と Adjustments の sharpness の関係を含む）。
5. macOS 14 / 15 での動作確認（decoder 9・`apple_default` の可否と出力差）と、`Package.swift` の対象バージョン（現状 macOS 13）をアプリの最低要件 14.0 に揃える。
6. OS 更新時の回帰比較の手順化（比較ハーネスの実行手順と合否の目安を文書化）。
7. `env -u CI npm run tauri:build` の DMG 作成（bundle_dmg.sh）の失敗の調査（#65 とは独立）。
8. （低）decoder 9 neutral の平滑化と、レンズ補正 ON の preview での白点ノイズの原因調査。
9. （低・任意）移行完了までの暫定として rawpy の原寸の撮影時シャープ強度の再調整（`engine.SHARPEN_*`、ENGINE_VERSION 18）。
