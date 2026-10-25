# 常駐stateless CLIのRust経路・資源検証（2026-10-25）

PR-Aの`96fa77a`、Core Image CLI 0.8.0、macOS 27.0.1で、共通Rust現像経路の単発/residentを比較した。製品の既定は単発のままで、residentは`cfg(test)`専用。GUI/IPC/WebViewを含むE2Eではない。

## 条件と一致

DSC04430/DSC04880のローカルとSMB上のARWをSHA-256で照合し、同一であることを確認した。2400px JPEG、decoder 9、apple_default、レンズ補正ON、調整9キーを共通にした。全10系列48ステップで、local RAW0→local RAW1→SMB RAW0→SMB RAW1を繰り返す192組。両側とも完成JPEGキャッシュmissを要求した。

要求JSON（outputを含む）・キャッシュ鍵・来歴キー集合・JPEGバイト列・復号RGBは192組すべて一致し、RGB最大差は0。EOF close後にresidentの子が回収された。ハーネスのexit 0だけを証拠にせず、親が全192行と件数を独立にassertした。

## 実行時間

各行は1回の固定順走行から集計した要求単位のms（p50 / p95）。Rustでの入力解決・鍵・来歴・pruneを含む。単発→residentの順序、フォルダ設定更新、初回resident起動を含み、coldなOS/GPUキャッシュを統制した比較や純粋なIPC差ではない。前日のdebug GUI数値とは比較しない。

| 保管場所 | 系列 | n | 単発 ms | resident ms |
|---|---|---:|---:|---:|
| local_apfs | decode_changing | 46 | 1608.6 / 1930.0 | 1136.7 / 1380.3 |
| local_apfs | post_only | 50 | 1568.8 / 1764.7 | 1123.6 / 1342.1 |
| smb_backup | decode_changing | 46 | 2418.9 / 2724.4 | 1824.4 / 2142.0 |
| smb_backup | post_only | 50 | 2352.3 / 2578.2 | 1854.5 / 2114.9 |

## メモリと終了

192要求のresident境界RSSは前半/後半各96点。p50は134,784→138,768KiB、maxは185,728→186,352KiBだった。系列・入力と時間が交絡した短い走行で、長期メモリ上限やリーク非存在を確定しない。要求境界RSSは真のpeakではない。

別のCLI単体資源probeでは、各保管場所で新しいstateless batchを起動し、2RAW・後段/露出を交互に40要求ずつ送った。`/usr/bin/time -l`が記録した値は次のとおり。

| 場所 | 最大RSS B | peak memory footprint B | 要求数 |
|---|---:|---:|---:|
| local_apfs | 233,750,528 | 796,198,112 | 40 |
| smb_backup | 226,803,712 | 761,873,608 | 40 |

両走行ともEOF後のexit 0と子の不在を確認した。追加で実CLIの親プロセスを通常終了/SIGKILLしたところ、子はそれぞれ観測11.6ms/66.8msで不在だった。これは検出時点までの観測値で、正確な終了遅延や上限保証ではない。Rust ownerのkill/reap試験やQAアプリ終了とは別の、standalone probeとして記録する。

## 記録と次のゲート

[要求単位JSON](../coreimage-baseline/resident-2026-10-25/resident-comparison.json.gz)、[資源probe](../coreimage-baseline/resident-2026-10-25/resource-probe.json.gz)、[要約・CLI/入力ハッシュ](../coreimage-baseline/resident-2026-10-25/summary.json)。原本・出力画像・private provenance・絶対パス・GPS・source-path hashは公開しない。

- 親のRust全体は200 passed / 0 failed / 14 ignored。子の権限制限下のMetal/ps失敗と区別する。
- code-review後、不一致を非0にするgate、mockのcache miss/入力確認、並行試験のbarrier、ownerとslotの結び付けを補強した。親の追加20テストと保存済み192組の新gate再判定は成功。新入力/PID観測gateの実CLI走行は別の追加検証として進行中で、統合前に結果を追記する。
- PR-Bは専用QA ID・preview限定cargo featureでアプリへ配線し、同条件の単発/resident E2E、cache hitの枠待ち、異常時の次要求、他処理との競合、終了・署名を検収する。正式採用・製品既定の切替・B2・FFIは決めていない。

## レビュー修正後の追加検収

`de7578f`の実CLI192組でも、parity gateと新観測gateがすべて成功した。全要求でRust側のrequest.inputが期待した解決先と一致し、resident PIDを観測、2要求目から最後まで同じPIDを再利用し、EOFで回収した。これはCLI内部の入力使用の直接観測ではない。local/SMBは同じRAWのコピーで、異なるパスを使用したことをバイト一致で識別したとも扱わない。mockの異なるfolder-ID bytes検査を併用した。

この追加走行は処理進行を確かめる1秒のスタック採取を含むため、時間値を速度比較の根拠には使わない。[追加観測アーカイブ](../coreimage-baseline/resident-2026-10-25/final-observations.json.gz)は初回速度記録と分離した。レビューround2はmerge blockerなし。親の追加変更は入力定義の文言訂正と同じcanonical rootを拒否する1行のみで、既存Single/CLIリクエスト/描画は変えていない。
