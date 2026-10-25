# 計画の実施記録（2026-10-24）

実施範囲は[棚卸し後の計画](issue-audit-2026-10-24.md)と
[原寸ROIの計画](../design/coreimage-candidate-native-roi-plan.md)。
オーナー承認の下、親は管理・統合・QA、通常実装はSonnet、難しい実装はSol/Opus、軽い調査はHaiku/Lunaへ分担する。
重大な判断が必要になった場合はFableへ相談する。素材待ちを検証済みにせず、PRは明示依頼なしにマージしない。

## 進捗

- [x] #5: 現行の表示結果・撮影ID・採用対象を共通化し、合成パラメータの案内を訂正する。
- [x] #32/#3: 入力署名と解析revisionを分離し、同名差替えと同一秒の再解析・遅い応答を検証する。
- [x] #57 A: 凍結画像がゼロでもCI基準を作れるようにし、実4画像で作成・照合・再比較する。
- [x] #141①: 単発CLI/batchと任意UI計測の基準を取得。Swiftライブラリ/FFIはFable助言と測定から保留判断。
- [x] #57 B: 最終候補の原寸ROI診断を明示ONで追加し、既存11枚で検収する。
- [x] #3/#141②/#28: ビューアの正しさと利用可能なデータでの閲覧負荷を検証する。
- [x] セキュリティ・自己レビューと対応、PR登録、最終結果を記録する。
- [ ] 最終release bundleのウィンドウ表示を配布形で検収する（下記の具体的な未確認）。

### 統合中の検証

- #5 F1は`48be080`で統合済み。親のworktreeで145テスト、型検査、lint、Viteビルドが成功。
  Tauri画面の確認とレビューは引き続き未実施。
- #32/#3は`f16f0cf`で統合し、親でフロント149テスト・型検査・lint・ビルド成功。
  macOSの大小文字を区別しないFSで壊れるテスト素材を`ed79d01`で修正し、Rust154テスト成功（9 ignored）。
  フォルダ走査失敗とstat失敗はcache miss・`SOURCE_SCAN_FAILED`へ倒す。
- #57 Aは`c9b1654`で統合。親で241テスト成功（旧経路11 skip）。実4画像は再描画で完全一致し、
  worktree外の保存とSMBマウントBackupへの全12ファイル照合を完了。
  [実行記録](../reports/coreimage-ci-baseline-bootstrap-2026-10-24.md)。
- #57 Bは`be899fd`で統合。採否・閾値は変更しない。
  統合後フロント152テスト・型検査・lint・ビルド、Rust164テスト（10 ignored）、Swift146テスト（6 skip）成功。
  実11枚のOFF/ON逐次検収は成功。44候補のID・調整・proxy・採否が不変、原寸描画43回、全件complete。
  前回アーカイブとの176候補ROI・2,288数値も完全一致。追加時間中央値10.67秒。
  コンパイル済みバイナリでも再検収し、資源値を別記録。
  [報告](../reports/coreimage-native-roi-validation-2026-10-24.md)。実画面とレビューは未了。
- #141の比較条件はFableの助言を受領。プレビューは2400px JPEG、同一の調整列を使い、
  decode段の変更と後段だけの変更を分ける。Rustキャッシュhitも別集計にする。
  BackupはSMBなので同じ素材のローカルコピーとも比較する。
  まず単発CLI/batchとボタン→画像表示の各段階を測定し、輸送層の改善余地が小さければFFIは見送る。
  必要なら同じJSON行契約の最小C ABI shimをbenchからだけ使う。正式採用は決めていない。
- #5はDraft PR #142。実画面確認待ち、セキュリティ指摘0件。
  自己レビューでは採用済み旧合成とcurrentプレビューのマーク・閲覧導線、reducerの新規採用条件、
  旧recipeのnull調整値ガードを修正対象とした。Sonnetが別worktreeで対応中。
- #57 A/#32の追加セキュリティレビュー（`046aa65...ed79d01`）は指摘0件。
- #57 AはDraft PR #143として登録し自己レビュー中。#32のスタックはpush済み、PR作成はレビュー枠待ち。
- #141のCLI/batch計測基盤と任意UI計測hookはSonnet、#3ビューアの面別失敗とdrag状態は別のSonnetが担当中。
  一覧ベンチの入口と素材容量はLunaが読み取り調査済み。実100枚と合成1000/5000の範囲を分ける。
- #3ビューア修正は`7bad735`で統合。面ごとの失敗・旧画像応答の破棄・写真送りのdrag解放を25テスト追加。
  統合後フロント192件・型検査・lint・Vite build成功。実DOM/WKWebViewは未確認。

## 保留条件

昼夜13ケース、別レンズの原寸較正、実AEB/三脚ブラケットは素材待ち。
PhotoKit/ネイティブビューアは現在の方式の問題と利点を確認した場合だけPoCを行う。
CLI常駐化やFFIの正式採用、新しい画質閾値を実測前に確定しない。

## QAと記録

各担当は独立worktreeを使用し、親が差分・テスト・ビルド・必要な実CLI/実画面を確認する。
元素材、既存凍結JSON、個人設定、未コミットの旧worktreeは変更しない。
実写はGitに含めず、基準画像とマニフェストは同じ保管単位で保持する。

## 追加の統合・レビュー記録

- #5 Draft [PR142](https://github.com/ysogabe/geoframe-raw-assist/pull/142): 採用済み旧合成の別閲覧/取消、current新規採用のreducerガード、legacy null、写真の暗黙合成fallbackを修正。recipe復元と、未編集の復元値に移動確認を出さない判定まで対応。PR headで184 tests/type/lint/build成功。
- #57 A Draft [PR143](https://github.com/ysogabe/geoframe-raw-assist/pull/143): full-limit黙殺と描画後の元ARW再照合失敗を修正。Python242件成功（旧経路11 skip）。新しい基準の実4画像再比較・SMBへの全ファイル照合済み。
- #32 Draft [PR144](https://github.com/ysogabe/geoframe-raw-assist/pull/144): 無効な解析がある場合の書き出しは再解析を促してコピー前に停止。未解析のcamera_jpg書き出しは維持。終了時の再指紋照合、ロック外stat、フォルダ変更の同期refガードを追加。
- #57 B Draft [PR145](https://github.com/ysogabe/geoframe-raw-assist/pull/145): PSO評価と原寸描画後のstaleチェック、OFF診断の非表示、non-UTF8 path互換を追加。進行中full単発の最大180秒中断不可は残る。
- #3 Draft [PR146](https://github.com/ysogabe/geoframe-raw-assist/pull/146): 面別失敗・写真送りdragの修正に加え、全体表示中のステージ縮小へ追従。面再選択は再読み込みしない旨を訂正。
- #141 Draft [PR147](https://github.com/ysogabe/geoframe-raw-assist/pull/147): 2400pxの単発/Rust/batchをローカルとSMBで同条件測定。実2枚×2系列の全要求でerror/timeout/restart0、statelessは単発と一致。可逆TIFF16対照は最大96/65535で、画質合否は未定義。
  [結果](../reports/coreimage-preview-transport-results-2026-10-24.md)。FFIはFable助言と残り輸送時間から現時点で保留。C ABI実測済みとは扱わない。
- 統合の自動検証: Rust183件/13 ignored、front264件、type/lint、product/benchビルド成功。Swiftは変更なしで既検証146件実行/6 skip。
- 専用IDのTauri QAアプリはdebug bundle作成成功。起動は自動承認レビューが操作時確認を求めたため一度保留し、ユーザーからQAアプリの明示起動許可を受領済み。実WKWebView確認/E2Eはこの後実施する。release bundleも作成成功。
- 閲覧ハーネスはproductから独立。実451枚の解析結果は113グループ、実100枚の抽出は25グループ。合成1000/5000枚と区別してChromiumで測定中。Rust解析/SMB I/O/Tauri全体や実paintの値へ転用しない。

チェックは機能実装と検収を分ける。原寸数値と基準の実検証は完了しているが、GUIを含む#5/#32/#3と全PRのready化は保留。

実451枚のfixture準備に使ったRust解析は451 photos /113 groupsで正常終了（ignored手動テスト1件pass）。初回解析166.94秒、続く直列decode診断を含む全体443.05秒。専用の性能比較ではなく、fixture生成時の参考記録とする。

T3内ブラウザはAX snapshotが2回15秒timeoutとなり、途中からrAFが約1秒へthrottleされたため、同走行の数値は採用しない。許可されたfallbackの専用Chromeセッションで4規模を再計測する。


## 修正後の検収（追加）

- 閲覧ベンチはレビューM1〜M3/L1〜L7を修正し、親のfront290件・type/lint・product/bench build成功。Chromeで実100/451・合成1000/5000枚の各7シナリオ完了、画像error0。[結果](../reports/browse-bench-results-2026-10-24.md)。合成のlazy待機timeoutを未確定として残し、#28の集計/再描画削減を次の比較PRとして計画。
- QAアプリを明示許可後に起動し、CI接続・未設定gate・専用5ペア→3グループの解析/一覧を確認。非既定contrast1.10で3枚本合成→採用→他グループ往復でrecipe/current維持、未編集復元値に破棄確認なし。
- 古い採用済み3枚本合成＋先頭を外した新2枚previewでは、SELECTED PHOTO/EXIF/表示leadはDSC04514で一致。新previewに✓は付かず、旧採用は別タイルで原寸確認と取消可能。長いタグは2行で収まり、未現像DSC04515を選ぶとその写真を表示し、暗黙に合成へ切り替わらない。
- 原寸ビューア: 比較面切替/100%/パン/写真送りのfit復帰、Inspectorのdockによる表示領域縮小5%→拡大16%のfit追従を確認。live DOMのafter srcだけを壊れたdata画像に変更し、その面のエラーと正常beforeへ戻したときの解消を実WKWebViewで確認（原本/成果物ファイルは未変更）。pointer保持中の写真送り・initial-afterの失敗順序はreducerの制御テストで検証し、手動race再現とは区別する。
- 候補の既定OFFではnot_run detailsなし。原寸ROI ONの実画面走行と任意プレビュー計測を続ける。


## 最終状態（2026-10-25）

- 原寸ROI ONは実画面で4描画完了、数値/座標/Δと「画質合格の判定なし」を確認。候補のスライダー反映→6000×4000本現像成功。debugの60.5秒はrelease11枚の性能値へ混ぜない。
- 任意プレビュー計測は8行を取得（ok5、image_not_observed3、invoke_error/decode_failed0）。成功5行のハンドラ→2rAFは995〜1047ms、p50 1037ms。1枚/少数/debug/ローカルの参考値で、p95やSMB/batch E2E比較の保証には使わない。[記録](../coreimage-baseline/preview-transport-2026-10-24/gui-preview-debug.json)。
- 同一5ペアを実画面で強制再解析するとsource_idは同じ、analysis_revisionは更新され、採用済みDSC04430も一覧に維持された。走査中変更/無効解析export/遅延応答の厳密な順序はRust/フロントの制御試験で検証し、手動再現とは区別する。
- 最終自動検証はRust184成功/13 ignored、front290成功、Python242実行/11 skip、Swift146実行/6 skip、type/lint/通常・bench build成功。以後は検収文書だけの変更で、無関係な再テストは重ねていない。
- 最終release QA bundleはsource `89a9a84`から作成成功（製品コードは`cb04d75`以後変更なし）、15MB。専用ID `com.geoframe.rawassist.qaroadmap`。bundleをadhoc署名し直し、`codesign --verify --deep --strict`成功。Python本体/ランタイムなし。main実行ファイルSHA-256 `139464cc22d08c42434b9c57f582dcb1c6db799d8c76c31d8c002465ab23e057`。
- **未確認**: releaseプロセスは起動したが、CUAが`cgWindowNotFound`でウィンドウを取得できず、Finder経由の代替操作も長い応答待ちになった。releaseの表示成功とはしない。debugでの機能GUI合格とは分離し、PR142〜148はDraftのまま、mainへのマージはしていない。OS側のウィンドウ/操作経路の問題かアプリ起動の問題かは未確定。
- mainのtrackedファイルはclean。既存未追跡のlocal_raw/とresume.txt、未マージの旧worktreeは保存し、削除・stash・移動していない。

## 次の作業順

1. release表示の未確認を切り分ける。正確なbundleを手動/操作ツールで前面起動し、既存debugとのbundle ID取り違え・ウィンドウ/デスクトップ状態とアプリ起動失敗を区別する。確認できるまで配布検収を完了にしない。
2. 上記を解消した後、レビュー済みPR142→143→144、その上の145/146/147/148をマージ候補とする。マージは新たな明示依頼を待つ。
3. #28は採用集計の一括計算とprops/callback安定化の小PRを先に計画し、同じ4 manifest/40操作で比較する。DOM/layoutが残る場合に仮想化を別PRで検討。#141の次はB1 stateless常駐のライフサイクル・枠・timeout復旧・E2Eの検証。FFI/全面SwiftUI化は先行しない。
4. #57 C/#46は昼夜13ケース・別レンズが届いてから較正・採否へ接続する。現11枚のproxy/原寸差を再現した事実を根拠とし、旧Python閾値を流用しない。#83は確認済み実AEB/三脚素材で画質検収。SMBで見つかったブラケット候補は確認済み実AEBとは扱わない。
5. #5の同lead再合成に伴う採用artifactの置換、#3の残るビューア/解析state再結合は既存Issueへ追跡を残す。#119/#89は要求が具体化するまで着手しない。
