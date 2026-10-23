#!/usr/bin/env bash
# Core Image 現像の回帰確認 3.1（docs/development/coreimage-regression-check.md、Issue #75）。
# ベースライン設定（apple_default・decoder 9・lens ON）で ARW を preview 現像し、CLI 応答の `applied`
# （OS が決めるシャープ・NR・色温度など）をベースライン JSON と比べる。差分が 0 件なら終了コード 0。
#
# 使い方: scripts/coreimage-regression-applied.sh <ARW のフォルダ> [ベースライン JSON]
# 出力は work/regression/<macOS のバージョン>/（gitignore 済み）。ARW は読むだけで書き換えない。
set -euo pipefail

cd "$(dirname "$0")/.."

SRC="${1:?ARW のフォルダを指定してください}"
BASELINE="${2:-docs/coreimage-baseline/applied-params-macos-27.0.1.json}"
CLI="src-tauri/binaries/geoframe-ci-develop-$(rustc -vV 2>/dev/null | sed -n 's/^host: //p')"
[[ -x "$CLI" ]] || CLI="src-tauri/binaries/geoframe-ci-develop-aarch64-apple-darwin"
[[ -x "$CLI" ]] || { echo "CLI がありません。先に npm run build:coreimage を実行してください" >&2; exit 1; }

OUT="$PWD/work/regression/$(sw_vers -productVersion)"
mkdir -p "$OUT"
"$CLI" --version

shopt -s nullglob nocaseglob
files=("$SRC"/*.arw)
shopt -u nocaseglob
[[ ${#files[@]} -gt 0 ]] || { echo "ARW がありません: $SRC" >&2; exit 1; }

for f in "${files[@]}"; do
  id="$(basename "${f%.*}")"
  printf '{"input":"%s","output":"%s","mode":"preview","decoder_version":"9","settings":{"profile":"apple_default","lens_correction":true}}' \
    "$f" "$OUT/$id.jpg" | "$CLI" > "$OUT/$id.json" 2>/dev/null || true
done

python3 - "$BASELINE" "$OUT" <<'PY'
import json, pathlib, sys

base = json.load(open(sys.argv[1]))
out = pathlib.Path(sys.argv[2])
skip = ("orientation_handling", "scale_factor")
diffs = 0
for c in base["captures"]:
    p = out / f'{c["capture_id"]}.json'
    res = json.load(open(p)) if p.exists() else {}
    if not res.get("ok"):
        print(f'{c["capture_id"]}: 現像できませんでした {res.get("error")}')
        diffs += 1
        continue
    now = {k: v for k, v in res["applied"].items() if k not in skip}
    for k in sorted(set(now) | set(c["applied"])):
        if now.get(k) != c["applied"].get(k):
            print(f'{c["capture_id"]} {k}: {c["applied"].get(k)} -> {now.get(k)}')
            diffs += 1
print(f"ベースライン {base['macos_version']} との差分: {diffs} 件（出力: {out}）")
sys.exit(1 if diffs else 0)
PY
