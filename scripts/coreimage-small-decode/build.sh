#!/usr/bin/env bash
# Issue #102 の計測ハーネスをビルドする（本番の CLI・Package.swift には触れない）。
# CoreImageDevelop の全ソース + main.swift を 1 モジュールとして swiftc -O でコンパイルし、
# internal な関数（applyProfile 等）を本番と同じ実装のまま使う。出力は work/small-decode/bench（gitignore 済み）。
set -euo pipefail
cd "$(dirname "$0")/../.."
SRC="native/coreimage-cli/Sources/CoreImageDevelop"
OUT="work/small-decode"
mkdir -p "$OUT"
swiftc -O -module-name SmallDecodeBench \
  "$SRC"/*.swift "$SRC"/Generated/*.swift \
  scripts/coreimage-small-decode/main.swift \
  -o "$OUT/bench"
echo "built $OUT/bench"
