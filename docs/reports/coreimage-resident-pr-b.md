# #141 B1 resident — PR-B（QA 専用 feature 配線）

PR-B は PR-A の resident owner を、**preview 限定の Cargo feature `resident-preview-qa`** と専用 QA ID の
Tauri bundle にだけ配線する。製品の既定は切り替えない。普通の feature なしビルドは従来どおりすべて Single で、
resident のコードはバイナリに含まれない。B2（decode/render）・FFI・デバウンス・auto-preview・UI 追加・Swift 変更はない。
IPC の引数と結果の形は変えない。実アプリ E2E・署名・GUI・実 RAW・SMB は親の QA 担当で、本書のテストは mock のみ。

## 配線

| 項目 | 内容 |
|---|---|
| feature | `resident-preview-qa = ["tauri/devtools"]`（default は空）。 |
| 有効化条件 | 専用 ID `com.geoframe.rawassist.qaroadmap` のみ。`main` が `tauri::generate_context!()` を一度だけ作り、feature 時は `Builder::build`/`run` の**前**に `context.config().identifier` を検査する。不一致は stderr に `RESIDENT_QA_WRONG_APP_ID: <message>` を出して `exit(1)` し、ウィンドウ／WebView は作られない（明示的な起動前拒否）。`setup` にも防御的な同じ検査を残し、そちらは code 付きの文字列でエラーを返す（通常は到達しない）。なお Tauri の `setup` はウィンドウ作成後に走るため、setup だけでは「ウィンドウ前の拒否」にならない。環境変数での切替はない。通常ビルドは従来の `.run(context)` のまま。 |
| ルーティング | `develop_command`（feature 時は `develop_for_command`）の **`DevelopMode::Preview` だけ** `develop_with_executor` + resident。Full / merge / candidates / native ROI は `develop` / 独自 session のまま。Full の cache bypass が resident を通る問題は、この入口の mode 分岐で防ぐ。 |
| 共通処理 | 入力検証・正規化、request、cache key、出力検証、EXIF、prune は `develop_with_executor` のまま複製しない。 |
| owner | `ResidentPreview`（managed state `QaApp` の中）が最初の preview で遅延生成。sidecar パスを固定し、`static DEVELOP_SLOT` を borrow（別 slot は作らない）。別パスの呼び出しは `SIDECAR_IDENTITY_CHANGED`。 |
| 寿命 | `QaApp` が owner と idle reaper を持つ。`RunEvent::Exit` で `shutdown`（下記）。通常ビルドは従来の `.run` のまま。 |
| 来歴 | resident が実行したときだけ `resident_qa: {schema:1, executor:"resident_batch", owner_pid}` を来歴 JSON に追加。 |
| devtools | feature に限り `tauri/devtools`。UI コントロールや自動計測は追加していない。 |

### 寿命の保証範囲

- **reaper**: 5 秒間隔のスレッドが `reap_idle` だけを呼ぶ。slot は取らず、owner は `try_lock` なので、処理中の要求を kill も待ちもしない。
  ただし owner の手前の `pinned` mutex は blocking lock で、初回 preview の sidecar hash 計算中は `ResidentPreview::pinned` が保持するため、
  reaper（と終了処理）はその間だけ**有限時間**待つ。「即座・完全 nonblocking」とは保証しない。
  idle TTL / 要求回数超過の idle 子だけを閉じる（stdin EOF → 最大 500ms 猶予 → kill/wait の既存 `BatchSession::Drop`）。
- **終了処理**: `Exit` で reaper を止め、owner に `closing`（sticky）を立ててから `try_lock` で close を最大 3 秒ポーリングする。
  以後の**resident preview** の要求は `RESIDENT_CLOSED` で、resident の子は新たに起動されない（この保証は resident preview の範囲。
  Full / 合成 / 候補は従来の単発 CLI のままで、本 PR は終了処理でそれらを止めない）。idle なら EOF で閉じる。
  **処理中の要求は kill しない**。3 秒内に終わらなければ `StillBusy` をログに出して諦め、子は親プロセス終了による stdin EOF に任せる。
  - 完走する子なら、現像が終わって stdin を読みに戻ったところで EOF を見て終了する。
  - **hang した子は stdin を読まず、親のタイムアウトも親プロセスごと消えるため、回収は保証されない**（既存の Single 経路にもある制約）。
    子 kill の追加など仕様変更は本 PR ではしていない。
  - 子が確実に消えること（以前の standalone probe では約 12〜67ms で不在を観測）はこの PR のテストでは証明しておらず、親が実アプリで観測する。
  異常終了（SIGKILL など）はそもそも Exit フックが走らない。
- OS の子回収 `wait` は同期処理で、厳密なリアルタイム上限は保証しない。静的 `Drop` には依存していない（`DEVELOP_SLOT` は static のまま）。
- 「1 active + idle resident 1」: 同時に現像するのは 1 本のまま、待機中の常駐子が別に 1 本存在しうる。プロセス総数の上限ではない。

### 未調整の実験ノブ（正式な上限ではない）

| ノブ | 値 | 備考 |
|---|---|---|
| idle TTL | 90 秒 | QA で idle 回収を観測しやすい値として仮置き。調整・根拠なし。 |
| 要求回数で再起動 | 64 回 | 同上。メモリ上限やリーク対策としての根拠はまだない。 |
| reaper 間隔 | 5 秒 | 同上。 |
| 終了時の busy 待ち | 3 秒 | 同上。 |

テストは小さい値の `Policy` を注入する（`ResidentPreview::new` / `QaApp::start`）。

## 来歴 evidence の読み方

- 実際に resident が実行した preview の来歴にだけ `resident_qa` が付く。`owner_pid` は **要求を送った時点の子の pid**
  （owner の state lock 内、送信前に取得）で、reaper が別 pid を誤記録する競合はない。
- キーが無い来歴は単発製（または PR-B 以前）。Single が作った cache を resident が返しても `resident_qa` は付かず、resident 実行を偽らない。
- `cache_hit: true` の応答は、生成時の来歴をそのまま返す。resident 製ならタグは残るが、**その pid が今も生きているとは限らない**
  （過去の実行の事実）。今回の要求が実行したかは `cache_hit` で判断する。
- request / cache key（`params_hash`）には混ぜていない。PR-A の Single / Resident 共通来歴 parity テストは plain owner を使うため影響を受けない。
- ログ（stderr）は reaper の回収と終了処理の結果だけで、path / GPS / capture ID は出さない。

## 既知のトレードオフ（維持）

resident の cache hit も CLI 同一性検査のために先に実行枠を取る。他の現像が走行中だと待ち（上限超過で `BUSY`）になる。
同じ理由で、不正入力（無効な capture_id など）も resident 入口の identity 用 slot を待つため、`BUSY` が検証エラーより先に出うる
（Single は入力検証が先で slot を取らない）。これも既知のトレードオフとして維持する。
通常ビルドの Single の hit は枠不要で、この差は mock テスト（`resident_cache_hit_takes_a_slot_but_single_does_not`）で固定した。
lockless identity への変更はしていない。親 QA で、候補/合成/原寸の最中に preview の cache hit が待たされる体感・`BUSY` を確認する。
CLI が差し替えられると `SIDECAR_IDENTITY_CHANGED` が sticky で、同一プロセス内では回復しない（アプリ再起動が必要）。
timeout / `BAD_RESPONSE` / `IO_FAILED` は PR-A と同じく自動再試行しない（変更なし）。

## ビルド

通常ビルドと確認:

```sh
export CARGO_TARGET_DIR="$PWD/src-tauri/target"
cargo check --manifest-path src-tauri/Cargo.toml                                   # 通常（resident なし）
cargo check --manifest-path src-tauri/Cargo.toml --features resident-preview-qa    # QA feature
cargo test  --manifest-path src-tauri/Cargo.toml --no-fail-fast
cargo test  --manifest-path src-tauri/Cargo.toml --features resident-preview-qa --no-fail-fast
```

QA リリース（親が実行。QA 用 config は ID `com.geoframe.rawassist.qaroadmap`、productName `GeoFrame Roadmap QA`、専用 appdata）:

```sh
env -u CI npm run tauri:build -- --bundles app --features resident-preview-qa --config <qa>/tauri-qa-config.json
```

通常 ID で feature 付きをビルド・起動すると、`main` が `Builder::build` の前に stderr へ `RESIDENT_QA_WRONG_APP_ID: ...` を出して `exit(1)` する
（ウィンドウは作られない。実 bundle での確認は親 QA 表 #1 で未実施）。
署名（adhoc strict）・配布は本 PR の範囲外。

### devtools の開き方（要親確認）

feature で `tauri/devtools` が有効になる。QA bundle で preview-timing を取るには、WebView のコンテキストメニュー
（右クリック →「要素を検証」）で devtools を開き、既存の手順を使う。Cmd+Option+I はメニュー項目が無いので保証しない
（[計測手順](coreimage-preview-transport-bench.md) §8）:

```js
localStorage.setItem("geoframe.previewTiming", "1");   // 次のクリックから ON
JSON.stringify(window.__geoframePreviewTimings)
localStorage.removeItem("geoframe.previewTiming");
```

署名済み QA bundle で実際に devtools が開くかは **未確認**（実装担当は起動していない）。

### stderr ログの取得

reaper の回収と終了処理の結果は stderr に `[resident-qa] ...` で出る（`exit cleanup: Closed / StillBusy / NotStarted`）。
Finder や `open` 起動では stderr が失われるので、項目 8・9 のログが要るときは
`<QA>.app/Contents/MacOS/geoframe-raw-assist` を端末から直接起動して取得する。

## テスト（mock CLI のみ）

`coreimage::resident::qa::tests` の 12 件。実アプリ・GUI・実 CLI・実終了・署名の証明ではない。

| 観点 | テスト |
|---|---|
| QA ID guard | `qa_identifier_guard_accepts_only_the_dedicated_id` |
| preview routing / Full は Single / 遅延生成・再利用 | `preview_is_lazy_and_reused_while_full_stays_single` |
| 来歴 evidence が cache key / request に入らない | `evidence_is_outside_request_and_cache_key` |
| cache hit の帰属（Single 製に付けない / resident 製は過去の事実） | `cache_hit_attribution_never_claims_a_resident_run` |
| resident cache hit の枠待ち | `resident_cache_hit_takes_a_slot_but_single_does_not` |
| reaper / EOF 回収 / 再起動 | `reap_idle_closes_with_eof_and_next_preview_starts_again`, `background_reaper_closes_idle_child_and_app_shutdown_joins_it` |
| busy を kill しない / shutdown 後は子を作らない | `busy_owner_is_neither_reaped_nor_killed_and_shutdown_blocks_new_children`, `shutdown_without_use_starts_nothing_and_refuses_later_previews` |
| 終了処理（EOF） | `app_shutdown_closes_the_idle_child_with_eof` |
| 既定ノブ | `default_knobs_are_the_documented_uncalibrated_qa_values` |
| identity 固定（sticky / 第二パス） | `sidecar_identity_change_and_second_path_stay_rejected` |

PR-A の wrong-slot / 差し替え / 再試行なし / timeout 等は既存テスト（`coreimage::resident::tests`）のまま維持。

## 親 QA チェック表（すべて未実施）

| # | 項目 | 結果 |
|---|---|---|
| 1 | 通常 ID + feature の誤起動が拒否される | 未 |
| 2 | QA release が adhoc strict 署名で起動・devtools が開く | 未 |
| 3 | local / SMB の 2 RAW で Single 版と resident 版の preview（同調整、cache miss）。画素・寸法一致、来歴に `resident_qa` | 未 |
| 4 | 連続 preview・ソース切替で `owner_pid` が同一 | 未 |
| 5 | preview-timing（status と origin→2rAF。n が小さいので p95 は主張しない。同一 URL の `image_not_observed` を成功にしない） | 未 |
| 6 | 候補 / 合成 / 原寸の最中の preview cache hit の枠待ち・`BUSY`、2 プロセス（実行 1 + idle 1）の確認 | 未 |
| 7 | エラー後の次要求（mock で先に。実アプリは所有 scratch の無効 path 等のみ。元写真は変更しない） | 未 |
| 8 | idle TTL（90 秒）での reaper 回収 | 未 |
| 9 | 通常終了で子が不在（EOF）、処理中終了、親の異常終了での子の不在 | 未 |
| 10 | 通常 ID の feature なしビルドは従来どおり Single（resident コードを含まない） | 未 |

機能受入と配布・署名の受入は分けて扱う。未観測の項目を passing と書かない。

## 親の初回検証

- feature付き全体216 passed / 0 failed / 15 ignored。default並行走行は既存mockハーネス1件が5秒timeout（215/1/15）したが、defaultをtest-threads=1で再走行すると216/0/15で成功した。最初の失敗ログも保存し、全走行成功とは書かない。
- 署名済みアプリの実動作は以下の検収で追記する。
