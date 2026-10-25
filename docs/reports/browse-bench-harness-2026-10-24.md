# 閲覧ベンチ：一覧・グループ帯の描画計測基盤（Issue #141 ② / #28）

提案グループ一覧（`GroupList`）と操作画面上部のグループ帯（`GroupStrip`）が、グループ数・写真数に対してどこで重くなるかを
**初回 baseline として測る**ための独立エントリ。手順と解釈は本書、実100/451・合成1000/5000枚の数値は[初回計測結果](browse-bench-results-2026-10-24.md)を参照する。

- 対象外: 仮想化・メモ化などの**最適化（render だけ速くする変更は未実装）**。先に baseline を取り、律速が描画かどうかを見る。
  アプリ本体（`App.tsx`・`main.tsx`・プロダクトの `index.html`・`npm run build`）には何も配線していない。
- 実装: `browse-bench.html` / `vite.browse-bench.config.ts` / `src/bench/browse/*`。追加依存なし。

## 1. 再現コマンド

```bash
npm ci
# fixture(git 管理外)を repo 直下 bench-fixtures/ に置く。→ §2
# A) 本番ビルド相当(react-dom/profiling に差し替え。Profiler が効く)。通常はこちらで測る
npm run bench:browse:prod          # http://localhost:1431/browse-bench.html?...
# B) dev ビルド(React が dev。数字は大きく出る。切り分け用)
npm run bench:browse:dev           # http://localhost:1430/browse-bench.html?...
```

URL クエリ（どちらか必須。`dataset.kind` の宣言が無ければ読み込みを拒否する）:

| 入力 | 例 |
|---|---|
| manifest | `?manifest=smb451/manifest.json`（`bench-fixtures/smb451/manifest.json`。サムネは manifest と同じディレクトリ基準） |
| 合成（ページ内で決定論的に生成） | `?synthetic=1000&pool=64&thumbs=synthetic/thumbs&seed=1`（`bench-fixtures/synthetic/thumbs/`） |

合成の入力は厳密に検査する: `synthetic` は 1〜20000 の整数、`pool` は 1 以上の整数、`seed` は整数（`1e3`・空文字・NaN・小数・負のグループ数は拒否）、`thumbs` は相対ディレクトリ。
生成した manifest も manifest 経路と同じ検証を通す。写真数が 100000 を超える指定も拒否する。

`BENCH_THUMB_CACHE=max-age`（既定）/`no-cache`/`no-store` でサムネ配信ヘッダを変える。ページは起動時に**計測に使うサムネ 1 枚へ HEAD**を投げ、`x-bench-thumb-cache` を
`environment.thumb_cache_policy`（`thumb_cache_policy_source: "head_probe"`）に記録する（manifest 経路でも合成でも同じ）。取れなければ `thumb_cache_policy` は null、
`thumb_cache_policy_source: "unobservable"` と `thumb_cache_policy_note`（理由）が入り、**キャッシュ方針は不明**として読む。cold を測るならブラウザのキャッシュを空にして `no-store`、warm なら `max-age`。

ブラウザ自動化（Playwright 等）からの呼び出し:

```js
await page.goto("http://localhost:1431/browse-bench.html?manifest=smb451/manifest.json");
await page.evaluate(() => window.__browseBench.ready);            // manifest 検証 + 初回 mount 完了
const report = await page.evaluate(() => window.__browseBench.runAll({ steps: 40 }));
// 1 本だけ: window.__browseBench.run("select_group_strip", { steps: 100 })
// JSON: window.__browseBench.exportJson("報告者メモ") / .download() (ツールバーの「JSON 保存」ボタンも同じ)
```

`__browseBench`: `ready`・`error`・`scenarios()`・`defaults()`・`manifest()`・`run(id, opts)`・`runAll(opts)`・`report(notes)`・`clear()`・`exportJson(notes)`・`download(notes)`。
`opts`: `steps`(既定 40)・`mountRepeats`(5)・`scrollSpeed`(2400px/s)・`maxScrollMs`(15000)・`settleMs`(3000)。**同じ page で走らせた結果は累積**し、`clear()` で空にする。
`run`/`runAll` の**同時起動は拒否**する（実行中の `clear()` も拒否）。失敗したシナリオがあると報告は `run_status: "incomplete"`、`failures[]` に理由が残り、
`scenarios[]` には失敗前に完了した分だけが入る（`clear()` まで incomplete のまま。部分結果を成功した報告として読まない）。

## 2. 入力 manifest

型と検証は `src/bench/browse/manifest.ts`。**写真・個人 EXIF・GPS・絶対パスは入れない**（未知キーは検証で拒否）。サムネは同一オリジンの
ローカルサーバーが配る**生成済み webp/jpg（最大辺 640px 想定）の相対 URL** だけ。原本 JPG/ARW・個人設定は読まない。

```jsonc
{
  "schema": "geoframe.browse-bench.manifest/1",
  "dataset": {
    "id": "smb451-g100", "kind": "real_metadata",          // "real_metadata" | "synthetic" の宣言が必須
    "group_count": 100, "photo_count": 312,                 // 実数と一致しなければ拒否
    "real": { "source_label": "smb-451pairs", "source_group_count": 451, "selection": "first_n" }
    // synthetic の場合は "synthetic": { "generator": "...", "seed": 1, "thumb_pool_size": 64 }
  },
  "photos": [{ "capture_id": "DSC02703", "captured_at": "2026-..T10:42:00", "thumb": "thumbs/DSC02703.webp", "width": 640, "height": 427 }],
  "groups": [{ "id": 1, "kind": "burst", "title": "10:42 連写 3枚", "start_at": "..", "end_at": "..",
               "capture_ids": ["DSC02703"], "recommended_capture_id": "DSC02703", "confidence": 0.8,
               "raw_level": "recommended", "raw_label": "RAW現像 推奨", "reason_text": "..." }]
}
```

- `analysisToManifest(analysis, { id, sourceLabel, selection, thumbUrl? })` が解析結果 → manifest の純関数（最小項目だけ写し、サムネは `thumbs/<ファイル名>` へ）。
  サムネのコピー先（`bench-fixtures/<dataset>/thumbs/`）に置く作業は fixture 側（親）の責務。
- データセットの区別: **実 451 pairs 由来は `real_metadata`、1000/5000 グループの水増しは `synthetic`**。合成は JPG/メタデータだけで ARW のコピーは無い。
  合成のサムネは `pool` < 写真数なら `<thumbs>/pool-0..pool-(P-1).jpg` を使い回し、`pool` ≥ 写真数なら `<thumbs>/syn-1..syn-N.jpg` を 1 枚ずつ要求する。
- **グループ数と写真数は別物**(451 pairs が 451 グループとは限らない)。manifest の `dataset.group_count`/`photo_count` と `real.source_group_count`(抽出元の解析の実グループ数。`selection:"all"` なら `group_count` と一致が必須)に実値を入れ、報告の `representativeness.stats` と `dataset` ブロックにも別々に出る。
- 実素材は SMB 451 pairs。写真・個人 EXIF・サムネは git に追加しない（`/bench-fixtures/`・`/dist-bench/` は `.gitignore` 済み）。

## 3. 何を測る（測らない）か

ハーネスは現行の `GroupList` / `GroupStrip` と**実 reducer・実 selector**（`visibleGroups`・`heroCaptureOf`・`acceptedSummaryOf`・`developedGroupIds`）をそのまま使う。
strip 系シナリオ（`select_group_strip`・`accept_toggle_strip`・`scroll_strip`）は、観測の開始**前**に先頭グループを開いて帯を mount しておく（`profiler.strip.mounts` と `dom_before` に初回 mount は入らない）。
操作画面ではグループ帯の下に高さ 2400px の空き領域を置く（帯の sticky/コンパクト切替のため。`OperationPanel` は載せない）。

| シナリオ | 操作 | 見る値 |
|---|---|---|
| `mount_list` | 一覧を unmount → mount（`mountRepeats` 回） | 操作→最初のコミット、→2 回目 rAF、画像が静まるまで、Profiler(root/list)、DOM 数 |
| `scroll_list` | window を一定速度で最下端まで縦スクロール | rAF の gap（p50/p95/p99/最大、1.5 フレーム超・33ms 超・100ms 超の回数）、longtask、サムネ load |
| `select_group_strip` | 操作画面で帯のカードを順にクリック（実ハンドラ） | to_commit / to_frame、Profiler(strip/root) |
| `accept_toggle_list` | 一覧表示中に別グループの採用をトグル | 〃（`GroupList` は未メモ化なので全カード再描画の費用が出る） |
| `accept_toggle_strip` | 操作画面の帯の表示中に採用をトグル | 〃 |
| `return_to_list` | 操作画面 → 一覧へ戻る（`GroupList` を remount）を繰り返す | to_commit / to_frame、サムネが静まるまで |
| `scroll_strip` | 帯を横に最後まで scrollLeft で送る | rAF の gap、longtask |

報告 JSON（`geoframe.browse-bench.report/1`）は: `environment`（UA・DPR・viewport・React ビルド種別・Tauri か・longtask 対応・サムネ配信ヘッダ）、`boot`（下記）、`run_status`・`failures`、
`representativeness`、`scenarios[]`（`params`・`operations`・`to_commit_ms`・`to_frame_ms`・`frame_gaps`・`profiler`・`dom_before/after`・`images`・`long_tasks`・`warnings`・`raw`）、`limitations`。

- `boot`: `to_first_commit_ms` / `to_first_frame_ms` は **React の render 開始（`createRoot().render()` の直前）** を起点にし、manifest の fetch・検証・Analysis 変換・HEAD 確認は含まない。
  `pre_render_ms`（エントリ評価開始 → render 開始、上記を含む）と、fetch/検証を含む全体値 `from_entry_to_first_commit_ms` / `from_entry_to_first_frame_ms` を別名で残す。`mount_actual_ms` は root Profiler の mount の actualDuration。
- `to_commit_ms`: 操作開始 → 最初のコミット（React Profiler の commitTime）。`to_frame_ms`: 操作開始 → その後 2 回目の `requestAnimationFrame`（**paint の代理観測**で、実際の合成完了ではない）。
- 画像: `<img>` の load/error と Resource Timing（`transferSize===0 && decodedBodySize>0` はキャッシュ由来と**推定**するだけ）。**画像デコード時間は直接測っていない**。

### ブラウザ自動化が観測する層と、未計測のもの

観測する: WebView/ブラウザ内の React 描画（Profiler）・DOM 数・スクロール中のフレーム gap・longtask・サムネの HTTP 読み込み。

**未計測**: Rust 側の解析（`analysis.rs`）、**SMB の I/O（サムネ生成・原本読み出し）**、Tauri IPC、`asset://` プロトコル配信（ここでは同一オリジン HTTP）、
`OperationPanel`・キー入力・確認ダイアログ・Tauri 呼び出し、原寸ビューア、実際の paint/GPU 合成、画像デコード時間、サムネ生成コスト。

## 4. 限界（報告に必ず書く）

1. **サンプル数**: 操作回数・フレーム数が少ない p95/p99 は最大値に近い参考値。各 summary の `p95_reliable`（n≥20）/`p99_reliable`（n≥100）が false の値は裾の推定として読まない。
   既定 `steps=40`・`mountRepeats=5` では mount 系の p95 は常に参考値。`warnings` にも出る。
2. **WebView と Chromium の差**: Playwright の Chromium の値を WKWebView（Tauri の実アプリ）の値として扱わない（JS エンジン・レイアウト・デコード・lazy 読み込みしきい値・GPU 合成が違う）。
   実アプリ相当が要るなら WebView 内で同じ `__browseBench` を呼び直す。`environment.user_agent` で測った場所を必ず残す。`longtask` は WebKit では取れない（null）。
3. **合成データの非代表性**: 1000/5000 の合成は単純複製に近いメタデータ・使い回しのサムネ・乱数のグループ構成。サムネキャッシュのヒット率と画像デコード負荷が実運用より軽く出る。
   `representativeness.stats` の `unique_thumb_ratio`（1 未満＝画像を使い回している）・`duplicate_group_signature_ratio`・`group_size` を併記し、**実メタデータと数値を混ぜて比較しない**。
   画像同一性（同じ画像が多数の `<img>` に出る）のため、デコード結果の共有も実運用より有利に働きうる。
4. **React ビルド**: dev ビルド（`bench:browse:dev`）は本番より遅い。通常は `bench:browse:prod`（`environment.react_build` = `production-profiling`）で測る。profiling ビルドは本番とほぼ同じだが完全には同一ではない。
5. 同期コミットの測定は Profiler の commitTime に依存する。Profiler が効かない本番ビルド（profiling に差し替えていない）では `to_commit_ms` が null になる。
6. 画像が『静まった』の判定は、load/error が 200ms 来ず、**かつ要求済みで未完了の `<img>` が 0** のとき（遅い要求の最中に quiet だけで抜けない）。lazy の未要求は
   `currentSrc` とビューポート ±3000px で除外する近似で、ブラウザの実際の先読み幅とは一致しない。`reset()`・setup・各シナリオの待ちが timeout したら `settled=false` として
   `warnings` に未完了枚数を残す（『前のシナリオの読み込みが混入しうる』も明記）。未完了を settled と見なして次へ進むことはしない。
7. 1 回目の `mount_list` はサムネが cold の可能性がある。`raw.to_frame_ms` の並び順で cold と warm を読み分ける（`warnings` に注意書きがある）。
8. **操作の種類**: 採用トグルと『戻る』は DOM のクリックではなく **reducer への直接 dispatch**（グループ選択と一覧カードを開く操作だけが実 `onClick`）。操作回数がグループ数を超えると同じグループを繰り返しトグルするため、
   採用の**追加と取消が混ざる**グループを開く/選ぶ操作でクリック対象（一覧カード・帯のカード）が DOM に無ければ**例外で失敗**する（黙って state だけ進めるフォールバックはしない）。
   （グループ数 < steps のとき `warnings` に出る。グループ数 ≥ steps ならすべて追加）。取消側の再描画コストは追加と同じとは限らない。
9. **Profiler の合計と操作別の値**: `profiler.*.commits/actual_total_ms` には観測窓の中の準備操作（`mount_list` の unmount、`return_to_list` の openGroup など）のコミットも入る。
   `to_commit_ms`/`to_frame_ms` は測定対象の操作 1 回ぶんだけ。両者を混ぜて『操作 1 回の費用』と読まない（strip 系の初回 mount は観測前の setup に移してある）。
10. **smooth scroll の重なり**: `GroupStrip` は選択変更のたびに選択カードへ smooth scroll する（`prefers-reduced-motion` でなければ）。`select_group_strip` では前の操作の smooth scroll が次の操作に重なる。
    本番の ←→ 連打と同じ挙動込みの値で、reduced motion には固定していない（`environment.prefers_reduced_motion` を確認）。
11. `acceptedSummaryOf` が 1 カードごとに `analysis.groups.find` する O(n²) の構造は現行コードのまま計測に含まれる（5000 グループでは顕著に出うる）。変更はしていない。

## 5. 報告のひな形（親が埋める）

| 項目 | 内容 |
|---|---|
| データセット（kind / id / グループ数 / 写真数） | |
| 計測環境（UA / DPR / viewport / React ビルド / サムネ配信ヘッダ） | |
| WebView か Chromium か | |
| 各シナリオ（n / p50 / p95 / 最大 / `*_reliable`） | |
| boot（初期 mount） | |
| DOM 数（elements / imgs / group_cards / strip_cards） | |
| 代表性（`unique_thumb_ratio` ほか）と解釈への影響 | |
| 未計測の層（Rust 解析・SMB・IPC・asset プロトコル）の想定寄与 | |

## 6. 検証済みの範囲

型（`tsc --noEmit`）・lint・`vitest`（manifest 検証/合成生成/変換、集計、fake driver によるシナリオ契約）・`npm run bench:browse:build`（profiling 差し替えが効いていることの確認）は
自 worktree で確認した。親がChromeのproduction-profilingで4規模×7シナリオを実行済み。画像errorは0。lazy settleのtimeout警告と代表性の制約は[初回計測結果](browse-bench-results-2026-10-24.md)に残す。WKWebViewはこの独立ハーネスでは未計測。


## 2026-10-25の更新

#28で採用集計を一括Map化し、カードpropsを安定化した。上記のO(n²)・旧selector名は初回実装時の記録であり、現行コードの説明ではない。[変更前後の検収結果](browse-adoption-optimization-2026-10-25.md)を参照。画像イベントの最大値集計は大量配列でも落ちないreduceへ修正し、計時境界は維持した。
