#!/usr/bin/env bash
# Core Image CLI の後段 Metal カーネル（native/coreimage-cli/Kernels/GeoFrameKernels.metal）を
# metallib にコンパイルし、base64 で Swift ソース（Sources/CoreImageDevelop/Generated/KernelLibrary.swift）へ
# 埋め込む（Issue #73、設計書 docs/design/coreimage-preview-adjustments.md §3.2）。
#
# 生成物はコミット対象。再生成するのは「生成物が無い」か「生成物が .metal より古い」ときだけ。
# 古さは mtime ではなく内容で判定する: 生成物の先頭に記録した .metal の SHA-256 と、現在の .metal の
# SHA-256 が違えば古い。git checkout で .metal の mtime だけが新しくなった場合は中身が同じなので
# 再生成せず、生成物の mtime を揃えるだけにする（metallib はビルドごとにバイト列が変わりうるため、
# 無用な差分を作らない。toolchain の無い機でも checkout 直後のビルドが通る）。
#
# Metal toolchain（xcrun metal）が無い機では、生成物が最新なら何もせず成功する。再生成が必要なのに
# toolchain が無ければ、`xcodebuild -downloadComponent MetalToolchain` を案内して失敗する。
#
# 使い方: scripts/build-coreimage-kernels.sh [--force]
set -euo pipefail

cd "$(dirname "$0")/.."

SRC="native/coreimage-cli/Kernels/GeoFrameKernels.metal"
OUT="native/coreimage-cli/Sources/CoreImageDevelop/Generated/KernelLibrary.swift"
FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

[[ -f "$SRC" ]] || { echo "build-coreimage-kernels: $SRC がありません" >&2; exit 1; }

SRC_SHA="$(shasum -a 256 "$SRC" | awk '{print $1}')"

if [[ $FORCE -eq 0 && -f "$OUT" ]]; then
  RECORDED_SHA="$(sed -n 's/^\/\/ source-sha256: //p' "$OUT" | head -n 1)"
  if [[ "$RECORDED_SHA" == "$SRC_SHA" ]]; then
    # 内容は同じ。mtime だけ新しければ揃える（次回の判定を軽くするため）。
    if [[ "$SRC" -nt "$OUT" ]]; then
      touch "$OUT"
    fi
    echo "build-coreimage-kernels: 生成物は最新です（${OUT}）"
    exit 0
  fi
fi

if ! xcrun -sdk macosx -f metal >/dev/null 2>&1 || ! xcrun -sdk macosx -f metallib >/dev/null 2>&1; then
  echo "build-coreimage-kernels: Metal toolchain がありません。'xcodebuild -downloadComponent MetalToolchain' で入れてから再実行してください" >&2
  exit 1
fi

TMP="$(mktemp -d -t geoframe-kernels)"
trap 'command rm -rf "$TMP"' EXIT

xcrun -sdk macosx metal -fcikernel -mmacosx-version-min=13.0 -c "$SRC" -o "$TMP/GeoFrameKernels.air"
xcrun -sdk macosx metallib -cikernel "$TMP/GeoFrameKernels.air" -o "$TMP/GeoFrameKernels.metallib"

B64="$(base64 < "$TMP/GeoFrameKernels.metallib" | tr -d '\n')"
SIZE="$(wc -c < "$TMP/GeoFrameKernels.metallib" | tr -d ' ')"

mkdir -p "$(dirname "$OUT")"
{
  echo "// 自動生成ファイル。手で編集しないこと。"
  echo "// 生成: scripts/build-coreimage-kernels.sh（元: Kernels/GeoFrameKernels.metal）"
  echo "// source-sha256: $SRC_SHA"
  echo "// metallib-bytes: $SIZE"
  echo ""
  echo "import Foundation"
  echo ""
  echo "enum KernelLibrary {"
  echo "    /// Kernels/GeoFrameKernels.metal の SHA-256（生成時）。"
  echo "    static let sourceSHA256 = \"$SRC_SHA\""
  echo "    /// \`xcrun metallib -cikernel\` の出力（base64）。"
  echo "    static let metallibBase64 = \"$B64\""
  echo "    static var data: Data? { Data(base64Encoded: metallibBase64) }"
  echo "}"
} > "$TMP/KernelLibrary.swift"

command cp -f "$TMP/KernelLibrary.swift" "$OUT"
if ! cmp -s "$TMP/KernelLibrary.swift" "$OUT"; then
  echo "build-coreimage-kernels: $OUT への書き込みに失敗しました" >&2
  exit 1
fi
echo "build-coreimage-kernels: ${OUT}（metallib $SIZE bytes）"
