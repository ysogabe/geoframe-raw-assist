# 通常プレビューの輸送方式：実2枚のローカル/SMB計測（2026-10-24）

2400px JPEG・同じ調整列で単発CLIと既存batchを比較した。製品の実行方式は切り替えていない。
現像元はDSC04430/DSC04880の同一ARW/JPGペア（計79,209,984 B）。ローカルコピーとSMB原本をSHA-256照合した。
ローカルはAPFS、Backupはsmbfs。両方を直列に測定し、他の実Core Imageテストは同時に走らせていない。
マシン全体の無負荷やOS/GPU/SMBキャッシュのcoldは保証していない。

## 条件と時間

各撮影ID・系列・方式でcold 1回＋warm 3回、各5ステップ。
`decode_exposure`はRAW再デコードを伴い、`post_shadow_lift`は後段だけを変更する。
残り8系列、他の素材、連続ドラッグ、UI E2Eはこの表に含めない。
表は2枚のwarm要求をプールしたp50 / p95（ms）。通常の各行n=30、完成JPEGキャッシュhitはcold反復分もwarmラベルでn=40。
系列内・写真内に相関があり、母集団のp95や速度保証ではない。元データには各写真・要求とcoldも残している。

| 保管場所 | 系列 | 方式 | p50 / p95 ms |
|---|---|---|---:|
| local | decode_exposure | single_cli | 836.69 / 1006.38 |
| local | decode_exposure | rust_develop_miss | 805.64 / 874.64 |
| local | decode_exposure | rust_develop_cache_hit | 0.18 / 0.23 |
| local | decode_exposure | batch_stateless | 563.11 / 714.57 |
| local | decode_exposure | batch_decode_render | 613.22 / 715.26 |
| local | post_shadow_lift | single_cli | 811.77 / 924.29 |
| local | post_shadow_lift | rust_develop_miss | 790.68 / 915.64 |
| local | post_shadow_lift | rust_develop_cache_hit | 0.19 / 0.23 |
| local | post_shadow_lift | batch_stateless | 598.69 / 694.70 |
| local | post_shadow_lift | batch_decode_render | 31.67 / 677.33 |
| smb-backup | decode_exposure | single_cli | 1618.50 / 2015.51 |
| smb-backup | decode_exposure | rust_develop_miss | 1556.41 / 1786.78 |
| smb-backup | decode_exposure | rust_develop_cache_hit | 0.18 / 0.29 |
| smb-backup | decode_exposure | batch_stateless | 1472.15 / 1570.75 |
| smb-backup | decode_exposure | batch_decode_render | 1451.47 / 1661.47 |
| smb-backup | post_shadow_lift | single_cli | 1593.50 / 1783.63 |
| smb-backup | post_shadow_lift | rust_develop_miss | 1639.91 / 2076.44 |
| smb-backup | post_shadow_lift | rust_develop_cache_hit | 0.18 / 0.24 |
| smb-backup | post_shadow_lift | batch_stateless | 1388.12 / 1577.52 |
| smb-backup | post_shadow_lift | batch_decode_render | 32.55 / 1491.86 |

後段再利用のp95には各反復先頭のデコードが含まれる。約32msという中央値を、写真切替・露出変更やボタン→表示へ転用しない。
完成JPEGキャッシュhitはCLIを呼ばない別経路であり、輸送方式の改善とは分ける。

## 出力と失敗

全方式・全要求でエラー/timeout/再起動は0。
Rust miss/hitとbatch statelessは、各20条件の単発CLI基準とJPEG bytes/RGBが一致した。
decode/renderはJPEG RGBで非一致（最大11/255）。許容の合否は未定義。
ローカル/SMBの最終60出力ファイルも全てSHA-256一致した。

640px TIFF16で記録された半精度差と、今回の8bit JPEG差は別の単位である。
JPEG量子化の増幅か別経路差かを分けるため、2400px TIFF16の独立対照を追加した。結果は後記する。

## 判断と残り

Fableの助言を受け、FFIの実装は現時点で保留する。batch statelessの輸送上の残り時間は数msであり、
単発CLIとの差の多くはプロセス内のCore Image初期化・処理時間にある。単発のoverheadには25ms間隔の終了ポーリングも含まれ、spawnだけの値ではない。
FFIの性能実測・配布/署名・クラッシュ試験が済んだとは扱わない。

B1（常駐stateless）は画素一致を確認済み。E2E・常駐ライフサイクル・枠・timeout復旧を製品側で確認してから採用を判断する。
B2（decode再利用）はTIFF16対照後に品質条件を決め、保持ハンドルとメモリ上限を含め別に判断する。
FFI再開の条件は、画像受け渡し/エンコード/書込/WebView復号がE2Eの有意な割合で、JPEG往復を迂回する表示方式を検討する場合、
または常駐セッションの運用が成立しない場合とする。FFI/C ABIを比較済みの数値として表に埋めない。

[要求単位の数値アーカイブ](../coreimage-baseline/preview-transport-2026-10-24/)と
[手順・環境変数](coreimage-preview-transport-bench.md)を参照。
実写画像・EXIF・GPS・私的絶対パスはアーカイブに含めない。
