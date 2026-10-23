#!/bin/bash
set -euo pipefail

if [[ -z "${GEOFRAME_TEST_MERGE_ARWS:-}" ]]; then
  echo "Set GEOFRAME_TEST_MERGE_ARWS to at least two absolute ARW paths separated by ':' (test images remain external)." >&2
  exit 2
fi
IFS=: read -r -a inputs <<< "$GEOFRAME_TEST_MERGE_ARWS"
if (( ${#inputs[@]} < 2 || ${#inputs[@]} > 9 )); then
  echo "GEOFRAME_TEST_MERGE_ARWS must contain 2...9 ARW paths." >&2
  exit 2
fi
for input in "${inputs[@]}"; do
  [[ "$input" = /* && -f "$input" && "${input##*.}" =~ ^([Aa][Rr][Ww])$ ]] || {
    echo "Expected an existing absolute .ARW file: $input" >&2; exit 2;
  }
done

root="$(cd "$(dirname "$0")/.." && pwd)"
swift build -c release --package-path "$root"
binary="$root/.build/release/geoframe-ci-develop"
tmp="$(mktemp -d "${TMPDIR:-/tmp}/merge-smoke.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT

python3 - "$tmp" "${inputs[@]}" <<'PY'
import json, pathlib, sys
tmp = pathlib.Path(sys.argv[1])
inputs = sys.argv[2:]
base = {
    "mode": "merge", "inputs": inputs, "max_edge": 2400,
    "decoder_version": "9",
    "settings": {"profile": "apple_default", "lens_correction": True},
    "adjustments": {
        "exposure": 0, "wb_temp": 0, "shadow_lift": 0,
        "highlight_recovery": 0, "brightness": 1, "contrast": 1,
        "saturation": 1, "sharpness": 1, "noise_reduction": "light",
    },
    "merge": {"align": True, "edge": "crop"},
}
for mode in ("single", "batch"):
    request = dict(base, output=str(tmp / (mode + ".jpg")))
    (tmp / (mode + ".json")).write_text(json.dumps(request, separators=(",", ":")))
PY

"$binary" < "$tmp/single.json" > "$tmp/single-response.json"
"$binary" --batch < "$tmp/batch.json" > "$tmp/batch-response.json"
cmp "$tmp/single.jpg" "$tmp/batch.jpg"
python3 - "$tmp/single-response.json" "$tmp/batch-response.json" <<'PY'
import json, sys
single, batch = [json.load(open(path)) for path in sys.argv[1:]]
assert single["ok"] and batch["ok"], (single, batch)
assert single["mode"] == batch["mode"] == "merge"
assert (single["width"], single["height"]) == (batch["width"], batch["height"])
assert set(single["adjustments_applied"]["ignored"]) == {"exposure", "highlight_recovery"}
assert set(batch["adjustments_applied"]["ignored"]) == {"exposure", "highlight_recovery"}
print(f"single/batch JPEG bytes match ({single['width']}x{single['height']}); exposure and highlight_recovery reported ignored")
PY
