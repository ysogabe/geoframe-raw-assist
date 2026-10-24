# Core Image基準の再作成（2026-10-24）

旧凍結画像が残っていないため、`--frozen-bootstrap-ci`で現在のCI出力を新規基準として作成した。
削除済みの旧rawpy画像・前回CI画像との回帰比較ではない。

- 対象: DSC07402 / DSC07423のpreviewとfull、計4画像。
- 環境: macOS 27.0.1 / arm64、CLI 0.8.0、decoder 9。
- 条件: `apple_default`、lens ON、`neutral_recovery`全9調整値を明示。
  preview長辺1600（1600×1067）、fullは6000×4000。
- 全4画像のファイル/RGBハッシュ・寸法の自己照合に成功。
- 同じCLIで別出力先へ再描画し、全4画像でΔE76 0、RGB MAE 0、完全一致画素率1.0。
- 基準はworktree外の`geoframe-evaluation-baselines/ci-2026-10-24/`へ保存。
  外付けBackupの`GeoFrame-evaluation-baselines/ci-2026-10-24/`へ同じ単位で複製し、全12ファイルのSHA-256一致を確認。
  実写画像とマニフェストはGitに含めていない。

| 来歴 | SHA-256 |
|---|---|
| CLI | `d005a66b1e4a4435c67caae49593a19e2c3e3c441c54b3e2809126f4e85a2941` |
| `ci_frozen.json` | `88665d808e492e6ef20d322775aa95a6d08c973220ccc78455ec84f0af4242aa` |
| `ci_frozen_provenance.json` | `3f4b6ff2b9e6ce7e08566ffd226f57665635866ac6c0dc256ec6073cecc53328` |

統合後の独立Python評価テストは241件成功、旧経路のみ11件skip。
基準作成→照合→再比較、空でない保存先・入力との衝突・不正ID・描画失敗等を偽CLIでも検証した。
昼夜13ケースと別レンズ・実AEBの追加評価は素材待ちのまま。
