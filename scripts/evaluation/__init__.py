"""評価スクリプト専用の独立サポート（#57 / #90 / #117）。

アプリ本体（``local_raw``）にも ``rawpy`` / ``lensfunpy`` にも依存しない。ここに置くのは
評価に必要な最小限だけ:

- :mod:`.sources` … 撮影 ID のファイル解決と EXIF 要約（GPS は読まない）
- :mod:`.presets` … 凍結済みプリセット表（JSON）の読み込み
- :mod:`.candidate_metrics` … 候補評価の純関数（旧 ``development_candidates`` の指標・選定の写し）
- :mod:`.frozen` … 凍結画像・JSON の照合（ハッシュ・寸法、欠落は失敗）
- :mod:`.legacy` … 旧 rawpy / local_raw 経路（アーカイブ）の明示 opt-in ゲート

旧エンジンを実行する基準生成は :mod:`.legacy` を通した明示 opt-in だけで、既定経路は
このパッケージと numpy / Pillow / OpenCV だけで動く（docs/development/evaluation-paths.md）。
"""
