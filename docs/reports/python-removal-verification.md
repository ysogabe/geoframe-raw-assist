# #90 / #117: Python本体削除の検証（2026-10-23）

現11枚のCore Image代理/原寸差を再現し、評価スクリプトを独立化してから本体を削除した。
実施順は[計画](../design/evaluation-before-python-removal.md)、測定値と#57の次の修正範囲は
[計測報告](coreimage-proxy-full-measurement.md)、CLIの使い方は[評価手順](../development/evaluation-paths.md)を参照。

## 復元点と削除範囲

- `d3068ec`: 計測ハーネスを固定。11枚・44候補・121設定、エラー0、640再現指標の最大差0。
- `e838ecc`: 独立評価・数値結果の保存後、旧本体を含む最後のコミット。旧rawpyでの再生成が必要な場合のcheckoutに使う。
- その後 `local_raw/`（旧サーバ・現像・解析・合成・取り込み・Driveを含む）、`requirements-raw.txt`、旧アプリ専用のテスト12本を削除。
- `requirements-dev.txt`にはNumPy / Pillow / OpenCVだけを残した。Pythonスクリプトは評価用で、アプリには不要。
- `tests/fixtures/baseline/`の凍結JSON・PNG、旧プリセットJSONは変更していない（READMEの手順だけを更新）。

## 親worktreeでの検証

| 確認 | 結果 |
|---|---|
| 削除前の独立評価テスト | 222件、成功（旧経路10件skip） |
| 本体を物理的に削除した後の全Pythonテスト | 初回222件、レビュー修正後231件実行、失敗0（旧経路11件skip）。10件の結合テストでは別ディレクトリにscripts/fixturesだけをコピーし、local_raw/rawpy/lensfunpyのimportを遮断して実行 |
| モジュールの存在確認 | `local_raw/`なし。評価用venvにlocal_raw/rawpy/lensfunpyのspecなし |
| Rust release全テスト | 152 passed / 9 ignored |
| Swift全テスト（本体削除前。Swiftコードの変更なし） | 146件、失敗0、9件skip |
| フロント | build / tsc --noEmit / lint / 132テスト成功 |
| Tauriアプリ | `env -u CI npm run tauri:build -- --bundles app`成功。バンドル内5ファイルにPython/local_raw/rawpy/lensfun資材なし、同梱CLI 0.8.0の起動成功 |
| 旧基準の照合・比較 | DSC07402 / DSC07423のpreview/full計4画像。ファイル・RGBハッシュ・寸法一致、実CLIの描画エラー0 |
| 削除後のOS回帰経路 | 上記の現在のCI出力を凍結し、再描画して比較。4画像ともΔE76=0、RGB MAE=0、完全一致画素率1.0 |
| 候補の集計 | 11ケース・44候補を独立Python集計でも確認。detail/noise/haloのMAE・biasはRust要約と丸め誤差内で一致。旧閾値の列は参考診断で、CIの合否はnull |
| evaluate_raw | 実CI preview出力2画像をカメラJPEGと比較できた |
| 凍結監査 | 9 JSON / GPS102タグはtype/countのみ / メタデータなし合成PNG17枚、成功 |
| PSO参照値・プリセット | `freeze_pso_parity.py --check`、`export_presets_fixture.py --check`成功 |
| セキュリティ | `security-review`スキルでf3e24b0からのコミット済み・staged・unstaged差分をレビュー。高・中確度の脆弱性0、数値アーカイブにGPS・絶対パスなし |

変更したRustファイルのrustfmtと`git diff --check`は成功。
リポジトリ全体の`cargo fmt --check`には、既存の未変更`analysis.rs`の整形差があるため、その整形はこの作業に含めていない。

実写の凍結画像はGitに含めない。旧基準は既存の`wt-107-baseline/work/baseline/`、今回のCI基準とマニフェストは
このworktreeの`work/issue57/frozen-comparison/`と`work/issue57/ci-baseline.json`。
OS更新後も画像と対応するマニフェストを一緒に保管・参照する。数値結果はGit内にも保存してある。

## 未完了として残すもの

- #57の本番変更: 最終候補の原寸ROI再評価と受け入れ判定。今回の計測・独立化とは別で、#57は閉じない。
- 昼夜混在13ケース、他レンズの四隅ノイズ、実ブラケットの画質検収は実素材待ち。
- #118の現行Tauri契約を最新mainから統合済み。評価用Pythonの独立化と凍結legacy値の保持を注記した。

## PR #138 自己レビュー後の確認

評価経路のレビュー指摘1〜7を修正した。現像済み画像の同一撮影ID・LABELの重複を拒否し、壊れたEXIFを読み飛ばす。
凍結比較は行番号ごとに出力を分け、照合レポートを書く前に凍結入力と出力先の重なりを拒否する。
旧経路のカメラJPEGも大小文字を区別せず解決し、EXIFの向きを反映する。
framesのない候補出力への一覧生成要求は警告し、削除済みPythonへの現役参照もコメントで訂正した。

- 親worktreeで全Pythonテストを再実行: 231件、失敗0、旧経路11件skip。
- 実CLIで既存CI凍結4画像を再比較: 4件ともΔE76=0、完全一致率1.0。
- 同じ撮影ID・modeの露出0/+1EVを別々に出力・凍結し、両方のハッシュを照合。
  その基準から再描画すると2件ともΔE76=0、完全一致率1.0。
- 重複集計、破損EXIF、複数設定の凍結・再比較、正常/壊れたマニフェストとverify-onlyを含む入力保護の回帰テストを追加。
- 任意の整理提案2件（プリセット読込補助とCLIごとのエラー報告の統合）は据え置き。
  値の正は同じJSONであり、CLIごとの報告内容も異なるため、今回の正しさの修正に統合は不要。
- ブランチ名の提案は、ユーザー指定の`codex/`規約を優先して据え置き。

レビュー後の変更は評価Python・テスト・文書とRust/Swiftのコメントだけ。本番現像処理・採否閾値は変えていない。
