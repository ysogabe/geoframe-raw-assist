# Core Image merge mode implementation report

## Scope

PR-B adds Swift CLI `mode: "merge"` on top of the PR-A `MergeCore` at base `8c861cd`. CLI version is bumped from 0.7.0 to 0.8.0. The request contract follows `docs/design/bracket-merge-coreimage.md` §§2, 5 and 6.1, with the adopted defaults: Mertens weights (1,1,0), central pivot, common crop, shift-limit fallback, 2400-edge preview when requested, and UInt16 RGB retained frames.

The CLI emits JPEG only and does not copy EXIF/GPS. `exposure` and `highlight_recovery` are forced to zero in every decoder stage and reported in `adjustments_applied.ignored`. `settings.exposure_ev` is required to be zero because the current adjustment contract rejects a non-null exposure setting alongside `adjustments`; accepting a non-zero value and silently overriding it would be misleading. White balance, NR, and decoder sharpness are applied while decoding all frames. The remaining post adjustments run once on the fused image.

## Resource smoke

Machine: Apple M3, macOS 27.0.1, release CLI, decoder 9. Source: existing `work/raw/DSC07415.arw` (6000×4000) linked nine times under `/private/tmp`; source bytes and metadata were untouched. All nine frames are the same exposure, so this validates the resource path only and says nothing about bracket image quality.

| Measure | Result |
|---|---:|
| Wall clock (`/usr/bin/time -l`) | 57.63 s |
| CLI elapsed total | 57.35 s |
| Decode | 14.59 s |
| AlignMTB and crop | 16.55 s |
| Mertens fusion | 25.69 s |
| Post chain / encode / write | 0.32 s |
| Maximum resident set size | 3,521,740,800 bytes (3.28 GiB) |
| Peak memory footprint | 3,656,552,432 bytes (3.41 GiB) |
| Output | 5999×3999 JPEG, common-cropped after detected −1/−1 shifts |

The input-size estimate for 9×24 MP is 3.67 GB at 17 bytes per pixel across all frames, within the implementation's 4 GB working-set ceiling. The measured peak footprint was close to that estimate. The 4 GB ceiling is also capped by `physicalMemory - 1.5 GB`; larger requests fail before RAW decoding with an `INVALID_REQUEST` containing estimated and safe-budget byte counts. Actual AEB and tripod long-exposure RAWs were not available, so the photographic comparison remains open and Issue #83 must not be closed on this report.

## Verification

- `swift test`: 140 tests passed, 9 RAW-dependent tests skipped, 0 failures.
- `swift build -c release`: passed.
- Nine-frame original-resolution identical-image resource smoke: succeeded as described above.
- Two-frame single and one-line `--batch` smoke: normalized metadata matched, and JPEG SHA-256 matched (`ee696a7c401b66ff...`); batch sequence and stage timings were excluded from the normalized metadata comparison as designed.
- Two-frame real decode with `exposure=2` / `highlight_recovery=100` versus both zero: JPEG SHA-256 was identical (`ee696a7c401b66ff...`); the response lists both ignored keys and reports effective exposure 0.
- `git diff --check`: passed.

## API difference from the frozen proposal

The response adds `sequence` only for batch, consistent with current CLI convention; timing fields vary by invocation, so byte-for-byte JSON equality between single and batch responses is not a contract. Both paths call `RequestParser.parseMerge` and the same `merge()` implementation. `elapsed_ms.align` and `elapsed_ms.fuse` report separate MergeCore stages; `post_encode` includes the post chain, render, and atomic JPEG write.
