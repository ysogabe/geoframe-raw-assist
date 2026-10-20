#!/usr/bin/env bash
# Core Image 試験現像 CLI（native/coreimage-cli）を release ビルドし、Tauri の externalBin
# （src-tauri/binaries/geoframe-ci-develop-<target triple>）として配置する（Issue #65）。
#
# tauri-build は externalBin の実体をコンパイル時に検査するので、`cargo build` / `cargo test` /
# `tauri dev` / `tauri build` の前にこのスクリプトが一度走っている必要がある
# （tauri.conf.json の beforeDevCommand / beforeBuildCommand から `npm run build:coreimage` で呼ぶ）。
set -euo pipefail

# どこから呼ばれてもリポジトリルート基準で動く。
cd "$(dirname "$0")/.."

PACKAGE_DIR="native/coreimage-cli"
DEST_DIR="src-tauri/binaries"
NAME="geoframe-ci-develop"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "build-coreimage-cli: Core Image CLI は macOS 専用です" >&2
  exit 1
fi

TRIPLE="$(rustc -vV | sed -n 's/^host: //p')"
if [[ -z "$TRIPLE" ]]; then
  echo "build-coreimage-cli: rustc -vV から host triple を取得できませんでした" >&2
  exit 1
fi

# 後段の Metal カーネル（Kernels/GeoFrameKernels.metal）→ metallib → Generated/KernelLibrary.swift。
# 生成物が .metal より古いときだけ再生成する（最新なら Metal toolchain が無くても素通り。Issue #73）。
bash scripts/build-coreimage-kernels.sh

swift build -c release --package-path "$PACKAGE_DIR"
BIN_DIR="$(swift build -c release --package-path "$PACKAGE_DIR" --show-bin-path)"
SRC="$BIN_DIR/$NAME"
DEST="$DEST_DIR/$NAME-$TRIPLE"

mkdir -p "$DEST_DIR"
# cp に -i エイリアスがあっても確認待ちで止まらないよう command 経由で強制上書きする。
command cp -f "$SRC" "$DEST"
chmod 755 "$DEST"

# コピーが本当に成功したかを内容で確かめる（黙って古いバイナリが残るのを防ぐ）。
if ! cmp -s "$SRC" "$DEST"; then
  echo "build-coreimage-cli: $DEST へのコピーに失敗しました" >&2
  exit 1
fi
echo "build-coreimage-cli: $DEST"
