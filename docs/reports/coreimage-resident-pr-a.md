# #141 B1 stateless resident — PR-A

PR-A は Rust executor seam、resident owner、mock 契約テスト、ignored 実 CLI 比較ハーネスまで。
Tauri command / IPC / UI / Cargo feature / 既定経路は変更しない。実素材・GUI の最終 QA と git 操作は親担当。

## 実装境界

- `develop()` と `develop_to_output_in_slot()` は `SingleExecutor` を明示する薄い wrapper。
  request 組立、出力 path/存在/寸法検証、result、EXIF、cache、provenance、prune は共通のまま。
  preview の 2400 JPEG / 60 秒、native ROI の単発 CLI / 180 秒は維持。
- resident は `cfg(test)` のみ。`ResidentOwner` が `Mutex<State>` 内の `Option<BatchSession>`、
  last-used / request count / sticky identity error を所有する。stateless 2400 JPEG preview に限定し、
  `op` / `handle` がある入力は送信前に拒否する。成功応答の `sequence` は共通来歴処理の前に除去。
- owner は生成時の `&DevelopSlot` を lifetime で borrow し、slot の移動・破棄と address 再利用を防ぐ。
  要求・identity 検査・pid 観測はその slot の `&SlotGuard` 必須。異なる origin の要求は
  owner mutex 待ち前に `INVALID_SLOT` で拒否する（pid は None、invalidate は無操作）。順序は slot → owner mutex。
  resident の共通 develop 経路は cache hit を含め、version/hash memo 参照より先に枠を取得して identity を検査する。
  Single の cache hit は従来どおり枠不要。PR-B の全呼出元は同じアプリ全体 `DEVELOP_SLOT` を使うこと。
- 送信 / timeout / 不正応答 / 処理中 exit は自動 retry しない。`sent` 未加算を未実行の証拠にしない。
  エラーを一度返し evict、次の独立要求で lazy start。要求前 `try_wait` で死亡を確認できた子だけ透過置換する。
  liveness 検査自体の I/O エラーは evict してエラーを返す。
  構造化 `ok:false` は code/message を維持し同じ session を継続できる。
  test-only の `reply_valid` は、不正な応答形と、たまたま同じ error code を持つ構造化 CLI エラーを区別する。
- canonical path + len/mtime/inode/device/ctime を固定する。変更・stat 不能は
  `SIDECAR_IDENTITY_CHANGED`（アプリ再起動要求）で evict し、close 後も sticky。
  owner 新規作成時にも一度だけ実 hash と既存 `sidecar_sha256` memo を照合するため、
  owner を作り直して古い cache key で新 binary を使うことはできない。memo 自体は変更しない。
  ディスクの同一性検査とプロセス起動は原子的ではないため、稼働中の binary 更新を支援する設計ではない。
- poison 回収では旧 session を破棄。`close()` は try_lock（処理中は BUSY）、`reap_idle()` も try_lock。
  Drop/close は既存 BatchSession の stdin EOF → 最大 500ms の猶予 → kill/wait を利用する。
  OS の子回収 wait 自体は同期処理であり、厳密なリアルタイム上限を保証するものではない。
  idle TTL / request count は任意の試験ノブ（既定 None）、製品閾値ではない。reaper thread は追加しない。
- 実行枠は「同時に現像する 1 本」。idle resident と別用途の単発 CLI が同時に存在することは許される。
  常駐 pid 数を実行中ジョブ数と混同しない。

## Mock と実 CLI ハーネス

`coreimage::resident` の通常テストは `executor=single/resident, binary=mock, host=cargo_harness`。
同一 pid、送信前 op/handle 拒否、来歴 key/value parity、cache hit、2RAW/フォルダ切替、idle resident と Single の共存、canonical path 変更、timeout の一度きり実行と旧 pid 回収、
実 pipe write 失敗（子は stdin を閉じたまま生存）、idle 死亡、処理中 exit、sequence/JSON/応答形不正、
構造化 CLI error、BUSY 上限、長い要求中の reap/close 拒否、TTL/count、stamp 差替え、poison、EOF/Drop/close を検証する。
繰返しの各 Single 前に preview cache を除去し `!single.cache_hit` を検査する。mock の bytes 先頭が
期待 folder-ID であることを両 executor で確認し、独立した cache-hit 検査も残す。
実行中の BUSY/reap と idle resident/Single 共存試験は release ファイルの明示 barrier で保持する。
mock 側は 15 秒 deadline で失敗するため、試験側の失敗で release されなくても永久待ちしない。
既定 Policy の共存試験における `!reap_idle()` は期限無効の確認であり、busy owner 拒否の証拠ではない。
wrong-slot 負例は実行中でも送信・bound slot の待ち追加なしに即拒否し、正しい slot の同一 session が継続することを検査する。
mock は非 JPEG のため実 CLI gate は適用しない。pure validator の正負例は別の synthetic JSON を使う。
mock は E2E・実画質・実性能の証拠に含めない。

ignored `resident_real_cli_comparison` は `binary=real_cli, host=cargo_harness`。
共通 `develop` を使い、同じ JSON（output 含む）・調整・2400 JPEG の Single/resident を順次比較する。
既存 transport_bench の全 10 系列（後段 / decode）と RGB 比較・統計補助を再利用する。
各 step は folder A/RAW0 → A/RAW1 → B/RAW0 → B/RAW1。owner は全系列・全フォルダを跨いで維持。
report を保存した後に pure validator で受入を判定する。全 10 系列の計 48 steps × rounds
（rounds は 4 以上）の正確な件数・系列/step/round/folder/capture 対応、request/cache key/provenance keys、
bytes 一致、比較可能・同寸・正の寸法・長辺 2400、decoded RGB 完全一致（max_abs_diff=0）と EOF 子回収を必須とする。
false・unknown・missing・0 件・件数不足・余剰・比較不能は失敗する。bytes gate は B1 の同一 JSON に対する
exact equality 条件であり、新しい品質閾値ではない。速度/メモリ閾値は作らない。
新規 report は両 executor の Rust が解決した request.input が期待 canonical root + 明示 ID の解決先と一致する boolean、
resident pid の観測成否と前回との一致 boolean（先頭だけ null）を記録し、別の strict gate で必須検証する。
生 path・生 pid は公開しない。
同一順序で Single → resident を測るため OS/GPU cache の cold/warm を統制した比較ではない。

OUT は既存 parent 下の **未存在ディレクトリ** 必須。canonical parent を検査し、source の内側・祖先・symlink 越しの重なりを拒否する。
公開 `resident-comparison.json` は paths / GPS / source-path hash / request/provenance 実値を含めない。
比較用画像と private app_data は成功時に削除する。中断時は OUT 内に private provenance が残り得るので OUT 全体を公開しない。
RSS は child と cargo の **request 境界 ps RSS (KiB)**。resident の前半/後半統計を記録するが、真 peak RSS/footprint は null・未測定と明記する。
Single child は境界時点ですでに回収済みなので child boundary RSS は null。

## コマンド

すべて自身 WT で実行し、親の target/node_modules は使わない。

```sh
export CARGO_TARGET_DIR="$PWD/src-tauri/target"
npm run build:coreimage
cargo test --manifest-path src-tauri/Cargo.toml coreimage::resident --no-fail-fast
cargo test --manifest-path src-tauri/Cargo.toml --no-fail-fast
rustfmt --edition 2021 --config skip_children=true --check \
  src-tauri/src/coreimage.rs src-tauri/src/coreimage/resident.rs \
  src-tauri/src/coreimage/resident/tests.rs src-tauri/src/coreimage/resident/harness.rs
cargo fmt --manifest-path src-tauri/Cargo.toml -- --check
```

この実装環境では通常 npm build は shared module cache / SwiftPM sandbox で失敗。
WT 内 cache と native build（deprecated、環境回避用）で実 CLI を build できた。
Swiftbuild は link 後の dsymutil が sandbox で失敗したため、成功した native 成果物を配置し `cmp` で一致確認した。

```sh
CLANG_MODULE_CACHE_PATH="$PWD/native/coreimage-cli/.build/clang-cache" \
SWIFTPM_MODULECACHE_OVERRIDE="$PWD/native/coreimage-cli/.build/swift-cache" \
swift build --disable-sandbox --build-system native \
  --cache-path "$PWD/native/coreimage-cli/.build/cache" --manifest-cache local \
  -debug-info-format none -c release --package-path native/coreimage-cli
cp native/coreimage-cli/.build/release/geoframe-ci-develop \
  src-tauri/binaries/geoframe-ci-develop-aarch64-apple-darwin
```

親が実素材比較を実施する際（自身は未実施）:

```sh
export CARGO_TARGET_DIR="$PWD/src-tauri/target"
export GEOFRAME_RESIDENT_SIDECAR="$PWD/src-tauri/binaries/geoframe-ci-develop-aarch64-apple-darwin"
export GEOFRAME_RESIDENT_SOURCE_ROOT_A='<local explicit source directory>'
export GEOFRAME_RESIDENT_SOURCE_ROOT_B='<SMB explicit source directory>'
export GEOFRAME_RESIDENT_CAPTURE_A=DSC04430
export GEOFRAME_RESIDENT_CAPTURE_B=DSC04880
export GEOFRAME_RESIDENT_OUT='<existing scratch parent>/<new run directory>'
export GEOFRAME_RESIDENT_ROUNDS=4
cargo test --release --manifest-path src-tauri/Cargo.toml \
  coreimage::resident::harness::resident_real_cli_comparison -- --ignored --exact --test-threads=1
```

既存 JSON の parity だけを再検証する入口（RAW 再現像・CLI 起動なし）:

```sh
export CARGO_TARGET_DIR="$PWD/src-tauri/target"
export GEOFRAME_RESIDENT_REPORT='<existing resident-comparison.json>'
export GEOFRAME_RESIDENT_ROUNDS=4
cargo test --manifest-path src-tauri/Cargo.toml \
  coreimage::resident::harness::resident_saved_report_parity -- --ignored --exact --nocapture
```

この入口は旧 schema-1 の一致判定専用で、input/PID 観測を検証したことにはしない。
既存 192 pairs に新 field がなければ観測証拠は未取得であり、`validate_observations` は失敗する。
新規実 CLI run は parity と新観測の両 gate を要求する。新観測の実 CLI 確認は親の小 smoke / 再計測担当。

## Review fix の検証範囲

M1 の pure validator 正負例 2 件、input/PID 観測の strict gate 1 件、R2 wrong-slot 負例 1 件を追加。
JSON 再検証入口 1 件は ignored とし、通常の対象テスト数と実 CLI 測定の件数を分ける。
review fix の対象テストは **20 passed / 0 failed / 2 ignored**（元の 16 件 + 通常追加 4 件、ignored は実 CLI と JSON 入口）。
通常 `cargo check`、変更 4 Rust ファイルの `rustfmt --check`、`git diff --check` は成功。
全体 `cargo fmt --check` は下記の既存 4 ファイルの整形差分で失敗し、当該 4 ファイルは base `a94df3c` と同一。
既存計測の request/cache key/provenance keys/bytes/RGB と子回収の証拠は JSON parity 再検証に利用できる。
owner の slot binding、barrier による同期、追加 input/PID 観測の実 CLI 動作は旧計測では検証されない。
この fix では実 CLI 本計測・全体 cargo test・GUI は実行しない。旧全体結果の訂正と親の数値追記は親担当。

## 既知制約 / PR-B での評価

- R1: resident の cache hit も identity 用に slot を取得するため待ち/BUSY になり得る。設計は維持し、
  PR-B で試験上の cache-hit 待ち/BUSY を明示して評価する。AtomicBool / lockless identity は範囲外。
- R5: 送信後の identity 変化時に JPEG だけ残り得る。provenance がなく cache として配信されず次回上書きされる。
  現状は試験 scratch の既知制約として扱う。
- T2/T3: full の cache-hit resident bypass と二重 load は、PR-B の入口を preview 限定にする前提で今回は範囲外。
  Single の挙動は変更しない。重複 ok 検査、poison reaper 戻り値、固定測定順序も今回変更しない。

## 実装担当の初回検証（履歴・親の最終結果と区別）

- 最終対象テスト: `cargo test ... coreimage::resident --no-fail-fast` — **16 passed / 0 failed / 1 ignored**。
  `mock` ハーネスの 192 pairs と追加の idle resident / Single 共存・canonical path 負例を含む。
- 全体テスト: `cargo test ... --no-fail-fast` — **194 passed / 4 failed / 14 ignored**。
  全体走行後に resident の試験 fixture と追加 2 テストのみを変更し、上記対象テストで再検証。
  製品コード・既存テスト・依存は全体走行時から変更なし。全体 passing とは扱わない。
- 全体の失敗（いずれも未変更の既存テスト。baseline の別走行は未実施）:
  - `batch_session_decode_cache_with_built_sidecar_and_real_arw`: Metal kernels / unsupported system の `INTERNAL_ERROR`。
  - `develops_real_arw_when_available`: Core Image render の `RENDER_FAILED`。
  - `fake_cli_contract_all_methods_labels_and_rgb`: request 境界 RSS が取得できず assertion。
  - `rss_probe_reports_only_for_a_successful_probe_run`: `/usr/bin/time -l` probe が成功せず measured=false。
    この実行環境では `ps` が `operation not permitted`、`/usr/bin/time -l /usr/bin/true` も
    `sysctl kern.clockrate: Operation not permitted` で exit 1。実 CLI の build 成功を render 成功とは扱わない。
  全体テストが既存の実 RAW 条件付きテストを自動実行したが、追加 ignored 比較ハーネス・実性能本計測は実行していない。
- `cargo check --manifest-path src-tauri/Cargo.toml` — 成功、警告なし（resident 未配線の通常ビルド）。
- 変更した 4 Rust ファイルの上記 `rustfmt ... --check` と `git diff --check` — 成功。
- 全体 `cargo fmt -- ... --check` — 失敗。差分は base `a94df3c` と同一の未変更 4 ファイル
  (`analysis.rs`, `candidates/mod.rs`, `coreimage/transport_bench.rs`, `coreimage/transport_bench/lossless.rs`) のみ。
  本 PR では既存の整形差分を混ぜない。
実 RAW の比較・実性能・長時間 memory trend・真 peak・GUI/IPC/WebView・SMB 実走行は未実施。
非 macOS は未検証（既存 coreimage 実装と同じ Unix/macOS 前提）。frontend は未変更のため再テストなし。

## PR-B の E2E 条件（親所有）

preview 限定 Cargo feature で owner を QA アプリへ配線し、`cfg(test)` の owner と必要な BatchSession 内部 API の cfg を拡張する。
通常ビルド・full/merge/candidates/nativeROI は Single を維持する。public IPC 契約は維持する。
local/SMB の指定 2RAW、フォルダ切替、後段/decode の各系列で、実アプリの手動 preview を確認する。
IPC/表示までの wall、出力 bytes/寸法/来歴 key、cache hit、BUSY、エラー後の次要求、アプリ終了時 EOF/子回収、
idle 常駐と他処理 CLI 共存時の実行枠を観測する。機能受入と配布/署名/終了動作の受入を分け、未観測項目を passing としない。
EOF の実 CLI 回収はこの ignored ハーネスとアプリ終了の両方で確認する。

## 親による検収（2026-10-25）

- 初回HEAD `96fa77a`でRust全体 **200 passed / 0 failed / 14 ignored**。上記の子環境の4失敗は親の実環境ではすべて成功した。
- レビュー修正後、親が対象 **20 passed / 0 failed / 2 ignored**、変更Rustのfmt/diff検査を確認。保存済み実CLI192組を新pure gateで再照合し成功した（画像再生成なし）。
- 最初の実CLI192組はrequest/cache key/provenance keys/bytes/RGBすべて一致、EOF回収成功。新input/PID観測はこの初回データに存在せず、現在の観測gateの実走行で別途検証する。
- 単独の実CLI資源・親終了probeは完了。数値は[実測報告](coreimage-resident-validation-2026-10-25.md)へ記録する。アプリE2EはPR-Bの担当で、今回の検収に含めない。

### 再レビューの補足

`input_matches_expected`はRustが保存したrequest.inputと期待した解決先の比較であり、CLI内部の入力使用を直接観測していない。実測のlocal/SMBは同じRAWのコピーなので、バイト一致だけでどちらのパスを使用したかを区別する証拠にもしない。mockの異なるfolder-ID bytesで切替を別に検証する。以後のハーネスはcanonical rootが同じなら開始前に拒否する。

- 追加の実CLI192組でparity/入力解決/PID観測の全gateが成功し、EOF回収も成功した。速度比較には使わず、[実測報告](coreimage-resident-validation-2026-10-25.md)に区別して記録した。
