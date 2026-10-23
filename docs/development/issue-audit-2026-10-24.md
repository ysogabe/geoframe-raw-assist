# Issue棚卸しと作業順（2026-10-24）

対象は棚卸し開始時のOpen Issue全11件と、移行先の実装を確認するための旧#32。
コード基準はmain `d12c87e`（PR #140後）、進行中のPRは0件。
現行コード・マージ済みPR・検証報告を照合し、旧本文は各Issueの折り畳み内に保持した。
今回の変更はIssue管理と計画文書だけで、製品コードの修正はしていない。

## 状態の変更

| Issue | 棚卸し結果 | 根拠・残す追跡先 |
|---|---|---|
| [#84](https://github.com/ysogabe/geoframe-raw-assist/issues/84) | completedでクローズ | PR #136のRust/UI/export移行とGUI接続・書き出しの完了記録がある。実AEB画質は#83に残す |
| [#17](https://github.com/ysogabe/geoframe-raw-assist/issues/17) | #119へ統合してnot plannedでクローズ | 旧Drive実装は削除済み。再現像の内容更新はRustで復活させるときの要件へ移す。修正済み扱いではない |
| [#62](https://github.com/ysogabe/geoframe-raw-assist/issues/62) | #119へ統合してnot plannedでクローズ | 出所・冪等性・命名衝突・応答消失/検索反映遅延の要件を移す。appPropertiesだけで冪等性が保証されるとはしない |
| [#32](https://github.com/ysogabe/geoframe-raw-assist/issues/32) | 再オープン | #110への移管後もRustの解析署名にサイズ/mtimeが入っていない。原寸現像・合成のキャッシュは対象から外す |

これによりOpenは11件から9件になる。#90/#117/#118/#139は既にclosedで、再オープンしない。

## 残る9件

| Issue | 現在の作業対象 | 優先・依存 |
|---|---|---|
| [#5](https://github.com/ysogabe/geoframe-raw-assist/issues/5) | 古い本合成と現在プレビューの表示/採用先整合、合成パラメータ案内、フォルダ復帰/遅い解析応答 | 最優先の小修正。性能項目は後段 |
| [#32](https://github.com/ysogabe/geoframe-raw-assist/issues/32) | 同名ファイル差替え・修復による解析キャッシュ無効化 | 高。#3の解析revisionと同じPRで扱う |
| [#3](https://github.com/ysogabe/geoframe-raw-assist/issues/3) | 秒内再解析の識別、合成stateの読み口、原寸ビューアのfailure/パン/ズーム | 解析revisionは高。ビューアは別の小PR |
| [#57](https://github.com/ysogabe/geoframe-raw-assist/issues/57) | CI基準再確保→最終候補原寸ROI診断→素材追加後の較正・採否 | 高。解析/採用の正しさを先に確保。基準整備は独立に進められる |
| [#46](https://github.com/ysogabe/geoframe-raw-assist/issues/46) | 現行CIでlow_noise/social/starsと探索軸・guardを再評価 | #57診断後。星空11枚はあり、昼夜13ケースは素材待ち |
| [#28](https://github.com/ysogabe/geoframe-raw-assist/issues/28) | GroupStripの再レンダー・採用集計の負荷 | 中〜低。selector整合後に計測から開始。素材待ちなし |
| [#83](https://github.com/ysogabe/geoframe-raw-assist/issues/83) | 実AEB/三脚ブラケットの画質検収 | 実装済み・素材待ち。#57の完了には依存しない |
| [#119](https://github.com/ysogabe/geoframe-raw-assist/issues/119) | Drive復活時のOAuth・出所・内容更新・冪等性 | 将来。#17/#62/旧#13の有効要件を一本化 |
| [#89](https://github.com/ysogabe/geoframe-raw-assist/issues/89) | 星景スタック用scene-linear出力の設計 | 将来・任意。スタックの要望が出るまで着手しない |

## 確認できた問題と確認の強さ

### 現行reducerで再現した2件

`src/lib/app-state.ts`と`format.ts`をTypeScriptでES moduleへ変換して読み込み、
製品ロジックを変更せずに最小stateから実行した。GUIの再現試験ではない。

1. **#3 B1: 同一秒の再解析で採用先が古いgroupに残る。**
   - 解析A: group 1にDSC00001、採用もgroup 1。
   - 解析B: 同じ`generated_at`でgroup 2に同じ撮影IDを移す。
   - `analysis/status`後、解析はgroup 2、採用のgroup IDは1のまま。
   - 時刻を1秒進めた対照では採用がgroup 2へ再結合する。
   - Rustの時刻は[analysis.rs](../../src-tauri/src/analysis.rs)で秒精度、
     判定は[app-state.ts](../../src/lib/app-state.ts)の`generated_at !==`に依存する。

2. **#5 F1: 選択・新プレビューと写真対象が食い違う。**
   - 古い本合成: DSC00001/DSC00002。
   - 現在の選択と新プレビュー: DSC00002/DSC00003、表示は合成。
   - `mergeFramesOf`の先頭はDSC00002、`viewingCaptureOf`は古い本合成のDSC00001を返す。
   - Appの表示結果は選択・プリセット・調整を照合する一方、撮影ID selectorはrawな本合成を優先する。

### コードと既存記録で確認したもの

- **#32:** `analysis.rs::source_signature`はrootと名前だけをハッシュする。
  `read_cached_analysis`の追加条件は版・scored・サムネイルの存在で、元JPG/ARWのサイズ/mtimeを検証しない。
  今回、実写真のキャッシュを作り変える実験はしていない。
- **#5 A:** [BracketMerge.tsx](../../src/components/BracketMerge.tsx)の「現像パラメータをそのまま使う」は、
  実mergeのexposure/highlight_recovery no-opと一致しない。文言を直す際は実画面も確認する。
- **#3 C:** [FullSizeViewer.tsx](../../src/components/FullSizeViewer.tsx)の読み込み成功時にfailureを消す処理、
  パンのclamp、ドラッグ中ズーム時の基準更新が不足するコードは残る。GUI再現は未実施で、修正前に行う。
- **#28:** 200ms tick、inline callback、`acceptedSummaryOf`内の`groups.find`/`accepted.filter`は残る。
  全体はO(G×(G+A))。実際のrender時間・支障は今回測っていない。
- **#84:** [ブラケット報告](../reports/coreimage-bracket-rust-report.md)の最終GUI確認に、preview/full、採用、
  原寸ビューア、GUI exportとJPEG/manifestの一致が記録されている。実AEB画質の証拠とは区別する。

## 陳腐化・解消済みとして外したもの

- Python HTTPサーバのOrigin・複数タブ・Enter連打、旧config/exportテストの同期や重複、lensfun依存テストは廃止済み。
- 原寸現像/合成のサイズ・mtime等を含むキャッシュキー、merged exportの出所・指紋検証は実装済み。
- mergeの9枚受理はSwift/Rustに境界テストがある。
- #46の「代理はレンズ補正なし」は現行Rendererのlens ONと異なる。露出も既に3格子であり、旧Blendカテゴリ等の指摘は使わない。
- 旧rawpyでの狙い未達数・処理時間を現在のCIの不具合数や性能値として転用しない。

## 改訂した実施順とPRの単位

### 1. 写真対象の整合を直す（#5 F1・パラメータ案内）

表示画像、SELECTED PHOTO/EXIF、写真採用先を同じ有効な結果selectorへ寄せる。
古い本合成＋新プレビュー、フレーム/先頭/プリセット/調整変更を回帰テストする。
合成で無視されるパラメータの案内も訂正し、Tauri上で合成・採用の対象と文言を確認する。
大きなstate構造変更や性能最適化は混ぜない。

### 2. 解析の入力識別と再解析revisionを整える（#32・#3 B1）

- 署名は実入力の変更を検出するもの、revisionは解析結果の世代を識別するものとして別に定義する。
  時刻の桁を増やしただけ、または入力署名をrevisionと呼び替えただけでは完了にしない。
- JPG/ARWの実名・サイズ・高精度mtime等を含む署名とschema更新を行う。stat方式の検出限界を明記する。
- 応答・型・API契約に解析revisionとsource identityを整え、同一秒の強制再解析でも採用が再結合されるようにする。
- #5の「遅い解析statusがフォルダ切替を跨ぐ」可能性は、制御した応答順で再現して必要なguardを決める。
  現時点では実機での発生を断定しない。
- 同名ファイル修復・差替え、変更なしcache hit、同一時刻再解析、重複ポーリング、フォルダ切替の遅い応答を検収する。
  旧凍結JSONは履歴として保持し、現行契約への追加を過去データの書き換えで隠さない。

### 3. CI基準を再確保する（#57 PR A）

以前削除された実写基準がなくても、新しいCI基準を作成・保管できる経路を整える。
現在あるDSC07402/DSC07423のpreview/full計4枚で新規作成→照合→別出力先で再比較する。
この作業は1/2のコードと独立に進められるが、旧基準との比較成功とは扱わない。

### 4. 最終候補の原寸ROI診断を追加する（#57 PR B）

[詳細設計](../design/coreimage-candidate-native-roi-plan.md)のとおり、640探索後の最終候補だけを診断する。
実行枠の二重取得、フォルダ世代、欠測・失敗、原寸呼出し上限を検証する。
診断はまず明示ONとし、候補の採否・順位・閾値を変えず、既存11枚で数値と追加時間を検収する。

### 5. 素材待ちと独立作業を分ける

- 素材なしで進められる: #3のビューア再現/修正、#28の計測と必要な最適化、#46の現行星空11枚の診断。
- 昼夜13ケース・別レンズが届いたら: #57 PR Cと#46で較正し、本番の採否へ接続する。
- 実AEB/三脚素材が届いたら: #83の実画質検収。#57の完了を待つ必要はない。
- #119/#89は明示的に機能が必要になったときに設計する。

各実装PRで、変更した層のテスト・build・型検査・lintと必要なTauri実画面を検収する。
今回実施したのはコード照合・上記2件のreducer実行・Issue更新であり、全テストやアプリビルドの再実行はしていない。
