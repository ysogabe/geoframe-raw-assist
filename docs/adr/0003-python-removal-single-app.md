# ADR 0003: Python を全廃し、現像エンジンを組み込んだ単一アプリにする

- ステータス: 承認（2026-10-22。Issue #105。オーナー判断は §6 で確定。実装の順序は [単一アプリの段階的実装プラン](../design/single-app-implementation-plan.md) が正）
- 関連: Issue #105、[ADR 0002](0002-coreimage-engine-decision.md)（Core Image 採用と rawpy 取りやめ）、
  [4 機能の移行設計](../design/coreimage-feature-migration.md)、[小サイズレンダの計測](../coreimage-small-render-performance.md)（#102）、
  `native/coreimage-cli/README.md`「バッチモード」（#82）、[単一アプリの段階的実装プラン](../design/single-app-implementation-plan.md)
- 表記: 【確認】はコードを読んで確かめた事実（ファイルと行を併記）。【推測】は見立て。【スパイク】は `work/spike-105/`（gitignore 済み、コミットしない）で動かして確かめたもの。

## 背景

ADR 0002 は rawpy の取りやめを決めたが、#90 は「Python 全体（analysis / export / photos_import / drive_upload / config と localhost サーバ）を
取りやめるかは範囲外」としている。オーナーの方針は「rawpy を外し、Python も廃止し、エンジンは単一アプリとして組み込みたい」。
この ADR は、それが実現できるか、どの形が妥当か、何を先に決めるべきかをまとめる。対象 OS は最新の macOS のみ（`tauri.conf.json` の
`minimumSystemVersion` は 14.0）。

### 現状【確認】

- `.app` は既に 1 つで、Swift CLI `geoframe-ci-develop` を `bundle.externalBin` として同梱している（`src-tauri/tauri.conf.json`）。
  Rust はこれを `Command::new` で起動する（`src-tauri/src/coreimage.rs:463` `run_sidecar`、`:614` `mod batch` = `--batch` 常駐セッション）。
- Python は `src-tauri/src/main.rs:92-860` が初回起動時に `.venv` を作り pip install し、`local_raw/server.py`（127.0.0.1:8788）を spawn する。
  Tauri コマンドは `setup_status` / `preview_engine` / `develop_preview_coreimage` / `get_presets` の 4 つだけ（`main.rs:893-900`）。
- フロントの `src/lib/raw-api.ts` は `getPresets` 以外すべて `fetch("http://127.0.0.1:8788/...")`。Core Image 経路は `src/lib/coreimage-api.ts`
  （`invoke("develop_preview_coreimage")`、出力は `convertFileSrc` で asset プロトコル配信、scope は `$APPDATA/coreimage/**`）。
  ブラウザモード（`isTauri() === false`）ではエンジンを常に `"rawpy"` にして HTTP に落ちる（`coreimage-api.ts:54-57`）。
- 移行済み: プレビュー現像（#72/#73）、プリセット定義と Adjustments の解決（#80/#81/#98）、CLI バッチモード（#82）。
- **原寸現像・書き出し JPEG の Core Image 化を扱う Issue は無い**（#73 はプレビューの写像で閉じている。ADR 0002 の「置き換えに進む条件」の
  Adjustments の行は原寸向けで未達のまま）。`develop_raw`（rawpy）が今も本現像の正である（`src/App.tsx:577` `developRaw`）。
- CLI の出力 JPEG は撮影 EXIF を一切書かない（`native/coreimage-cli/Sources/CoreImageDevelop/Develop.swift:698`「元 RAW の EXIF / GPS は引き継がない」）。
  EXIF 継承は Python `local_raw/exif.py` だけが持っている。

## 1. 結論

**実現できる。推奨形は「Swift sidecar CLI（`--batch` 常駐）を同梱したまま、Python の全モジュールを Rust（と CLI の新モード）に移し、
venv/pip の初回セットアップと localhost サーバを削除する」。** プロセス内リンク（§2 の案 B）は技術的に可能だと確かめたが（【スパイク】）、
現時点で得るものが計測上ほぼ無く、失うもの（クラッシュ隔離・メモリ隔離・テスト分離）がある。

規模の見立て（§3 の合計）: Rust 新規 約 4,300〜5,600 行（Drive は初版から外すので §3 の F は含めない）+ Swift 約 1,000〜1,500 行（合成モード・decode キャッシュ op）。
Python 約 7,400 行（`local_raw/`、`wc -l`）と Rust 約 770 行（`main.rs` のセットアップ）を削る。クリティカルパスは「原寸現像・書き出しの CI 化 + EXIF 継承」（新規 Issue A）で、
これが無いと他をいくら移しても本現像が Python に残る。

進め方（§6 決定 6）: **全機能を移してから Python を外すのではなく、まず「Python 無しで起動から書き出しまで成立する最小のアプリ」（M0）を作り、そこへ機能を足す。**
段階の定義・Issue の切り方・検証手順は実装プランに書く。本 ADR の §5 は方針の要約に留める。

## 2. 「単一アプリ」の解釈と Swift エンジンの組み込み方式

オーナーの意図は「ユーザーが Python / venv / pip を目にしない 1 つの `.app`」と解釈する。これは **sidecar のままでも満たす**（今も同梱 1 バイナリ）。
したがって論点は「Python を消すこと」で、Swift をプロセス内に入れるかは独立した設計判断。

### 2.1 比較

| 観点 | (A) sidecar CLI（現行。`--batch` 常駐） | (B) プロセス内リンク（Swift static lib を `@_cdecl` C ABI で Rust にリンク） |
|---|---|---|
| クラッシュ隔離 | `CIRAWFilter` の異常終了・Metal の失敗は子プロセスで止まり、アプリは `SIDECAR_*` エラーで続行（`coreimage.rs` の `BatchSession` は timeout で kill、再起動できる） | デコーダのクラッシュはアプリ本体の終了。復旧はアプリ再起動 |
| メモリ隔離 | 原寸 full のバッチでピーク RSS 約 343 MiB・footprint 約 1.09 GiB は子プロセス側（README「計測」）。終了で確実に解放 | 同じ量がアプリプロセスに乗る。WebView と同居 |
| IPC コスト【確認】 | プロセス起動 + 初期化 約 0.2 秒/件は `--batch` で消える。残る 1 件のコストはレンダ 0.41 秒 + `CIRAWFilter` 生成 0.07 秒 + エンコード 0.004 秒（#102 §2.1）。JSON Lines + ファイル経由の往復は数 ms【推測】 | IPC は無くなるが、支配項（レンダ 0.41 秒）は同じ。得は 1 件あたり数 ms〜数十 ms |
| PSO の代理レンダ | #102 §5.3 の「decode してキャッシュ → 後段だけ繰り返す（7.2 ms）」op を `--batch` に足せば、1 枚 50 評価で約 3〜6 秒（#102 §5.2）。プロセス境界は律速でない | 後段 1 回 7 ms が IPC 無しになるが、差は 50 評価で数百 ms 以下【推測】 |
| ビルド | `npm run build:coreimage`（`swift build` → `src-tauri/binaries/` へコピー）。tauri-build が `externalBin` の存在をコンパイル時に検査する | `build.rs` から `swift build` を呼び `rustc-link-search` / `rustc-link-lib=static` + Swift ランタイムの検索パス + `clang_rt.osx`（【スパイク】§2.2）。`cargo build` が Xcode 必須になる（今も実質そう） |
| codesign / notarization【未確認】 | Tauri が externalBin にも署名する経路だが、`tauri.conf.json` に `signingIdentity` / entitlements の設定は無く、**Developer ID 署名と notarization は未実施**。#74 で確認したのは「ビルドした .app の同梱 CLI が動く」ことまで | 単一 Mach-O なので署名対象は減るが、同じく未確認。Metal カーネルは base64 埋め込み（`Generated/KernelLibrary.swift`）でリソースバンドル無し【確認: `Bundle.module` の参照無し】なので static lib 化の障害は無い |
| テスト | `swift test`（CLI の単体 + スモーク）と `cargo test`（偽 CLI の `batch_mock` で `BatchSession` を検証）が分離 | Swift 側の単体テストは同じ。Rust 側は本物の CIContext が要る（CI では Metal の無い環境で落ちうる）か、C ABI の層にモックが要る |
| Swift 6 concurrency | `Package.swift` は tools 5.9・言語モード 5（同期処理のみ）。CLI はリクエストを直列に処理し、`CIContext` 1 つを使い回す（`Batch.swift`） | Rust の任意スレッドから呼ばれる。`CIContext` はスレッド安全だが `CIRAWFilter` は明記が無い【推測】。専用スレッド（または actor）で直列化が要る |
| CIContext の寿命 | プロセス寿命 = セッション寿命。1 件ごとに `clearCaches()`（しないと 30 件で 1.3 → 2.3 秒/件に伸びる。README） | `Unmanaged.passRetained` で Rust が所有し `release` を呼ぶ（【スパイク】）。`clearCaches()` の呼び出し規律を Rust 側に持つ |
| 切り戻し | 既存。CLI だけ差し替えて検証できる（`--version` の SHA-256 をキャッシュキーに含めている） | ビルド全体に絡む |

### 2.2 スパイク（案 B の最小実証）【スパイク】

`work/spike-105/swiftlib`（SwiftPM、`.library(type: .static)`、macOS 14）に `@_cdecl` で 4 関数（`gf_ci_context_create` / `_release` /
`gf_ci_probe_render` / `gf_ci_metal_device_name`）を公開し、`work/spike-105/rustbin` の Rust bin から `extern "C"` で呼んだ。所要約 20 分。

- **動いた。** `CIContext` 生成 33 ms、4×4 の合成 `CIImage` を RGBA8 へ Metal レンダ: 初回 10 ms、2 回目以降 0.5 ms、検算値は毎回一致。
  実行ファイルは 471 KB。`otool -L` では CoreImage / Metal / `/usr/lib/swift/libswift*.dylib`（OS 同梱）を動的参照し、Swift ランタイムの同梱は不要。
- 詰まった点: **swift-rs 1.0.7 の `SwiftLinker` はそのままでは使えない。** 成果物を `<build-path>/arm64-apple-macosx/release/` に探すが、
  Swift 6.4（Xcode 27.1）の swiftpm は `<build-path>/release/` に書く（`ld: library 'GeoFrameCI' not found`）。`build.rs` を自前で書き、
  `swift build --build-path` → `rustc-link-search` + `rustc-link-lib=static=GeoFrameCI` + 検索パス 3 つ（toolchain `usr/lib/swift/macosx`、
  SDK `usr/lib/swift`、`/usr/lib/swift`）+ `clang_rt.osx` で通った。`-framework CoreImage` 等は Swift オブジェクトの autolink（LC_LINKER_OPTION）で解決され、明示不要。
- 未確認: ARW のデコード（画像に触れないため試していない）、`cargo test` を Metal の無い CI で回す方法、Tauri アプリ本体へのリンク（spike は単体 bin）。

### 2.3 判断

**(A) を採る。(B) は「可能と確認済み・保留」として記録する。** 理由は、支配項がレンダ時間でプロセス境界ではないこと（#102）、
PSO の高速化は `--batch` の op 追加（#102 §5.3）で達成できること、RAW デコーダのクラッシュをアプリから隔離できることの 3 点。
(B) を検討し直す条件: 後段だけを数百回/秒で回す対話的な調整 UI など、7 ms の後段を IPC が上回る用途が出たとき。

## 3. モジュールごとの移行表

規模は Rust の新規行数の目安（テスト別）。「移す先」の Rust は `src-tauri/src/` の Tauri コマンド。

| Python モジュール / API | 移す先 | 置き換える依存【確認】 | 規模 | Issue |
|---|---|---|---|---|
| `engine.py`（1074 行）`/api/raw-develop`（原寸・書き出し JPEG） | **Rust + 既存 CLI**。`develop_full_coreimage` コマンド。`DevelopMode::Full` は定義済みだが未使用（`coreimage.rs:101-105`、`#[allow(dead_code)]`）で、`develop()` にそのまま渡せる | rawpy / cv2（`:583,:612` `resize INTER_AREA`、`:701` `GaussianBlur` = 後段 USM）/ PIL → CLI 0.6.0 が後段ごと持つ。残る仕事は **EXIF 継承・レシピ JSON・出力先・`DevelopedResult` 契約**、および **原寸での写像の較正**（#73 の 9 キー写像はプレビュー 1600px で較正したもの。デコード段の `sharpness ≤ 1` は 640px では効かず 1280px 以上で効く、NR も倍率で変わる — #102 §3.4。原寸は別に比較ハーネスの full 行で確かめる。#57 と同根） | Rust 600〜900 | **新規 A（クリティカルパス）** |
| （参考）`/api/core` `/api/event` `/api/merge` | **存在しない**。`server.py` に経路は無く、フロントの `@tauri-apps/api/core` / `@tauri-apps/api/event` の import と `app-state.ts:1255` の古いコメントが grep に当たっただけ【確認】 | — | — | — |
| `exif.py`（134 行） | Rust または Swift。§3.1 | PIL → §3.1 | 300〜500（+ テスト） | 新規 A に含める |
| `analysis.py`（860 行）`/api/analysis` `/api/analyze` `/thumbs/*` `/source-jpg/*` | Rust。解析は `spawn_blocking` + 進捗 event。グループ化・推薦（`:433-620`）は純関数 | cv2 は `cvtColor(RGB2GRAY)` と `Laplacian(CV_64F).var()` だけ（`:283-285`）。PIL は `Image.draft`（1/8 DCT 縮小デコード `:298-300`）、`exif_transpose`、`thumbnail(LANCZOS)`、WEBP q80（`:330-335`）。Rust: ラプラシアンは自前（数十行）、JPEG 縮小デコードは `turbojpeg`（libjpeg-turbo の scale）か `zune-jpeg` + 縮小、または Swift ImageIO（`kCGImageSourceThumbnailMaxPixelSize`）。WebP 書き出しは `image` crate だと lossless のみ → **サムネイルを JPEG にするか `webp` crate**（設計判断。URL は asset プロトコルになるので外部契約ではない） | 900〜1,200 | 新規 C |
| `development_candidates.py`（1171 行）`/api/development-candidates` | Rust（#86 純関数 → #87 ループ）+ CLI に decode キャッシュ op | numpy / PIL / cv2（`:312` `connectedComponentsWithStats` 8 連結、`:353,:497` `GaussianBlur`。純 numpy フォールバックあり） → #86 で自前実装。代理レンダは rawpy half_size（`:218-265`）→ CLI op（#102 §5.3） | Rust 1,200〜1,500、Swift 300 | #86、#87、新規 B（CLI op） |
| `bracket.py`（998 行）`/api/merge-bracket` | Swift CLI 合成モード + Rust コマンド | rawpy / cv2（`:308` `createAlignMTB`、`:352` `createMergeMertens`）→ Swift で MTB と Mertens を自前実装（設計書 §3.3） | Swift 700〜1,000、Rust 400 | #83、#84 |
| `export.py`（463 行）`/api/export` | Rust | 標準ライブラリのみ（`shutil.copy2`、sha256、manifest JSON）。**契約変更あり**: 複製元が `outputs/developed`（リポジトリ相対、契約 §4）から `$APPDATA/coreimage/...` に変わり、manifest の相対パスの基準も変わる | 400〜500 | 新規 D |
| `photos_import.py`（122 行）`/api/import-to-photos` | Rust（`Command::new("osascript")` 配列引数） | subprocess のみ。`scripts/photos_import.applescript` を `bundle.resources` に入れる。TCC の自動化許可が「ターミナル」ではなく **アプリ本体**に付く（`PERMISSION_HINT` の文言を変える） | 150 | 新規 D の後半（実装プラン N-D2、M1） |
| `drive_upload.py`（881 行）`/api/drive-upload` GET/POST | **初版から外す**（§6 決定 1）。UI・state・API 関数は M0 で削除、Python 本体は H で削除。将来 Rust で実装するなら: 外部 import は遅延（`:316-345` `InstalledAppFlow` = ループバック OAuth、`:513-518` `files().create` resumable）。Rust: `oauth2` + `reqwest`（multipart/resumable は自前）か、`tiny_http` のループバック受け口。`drive_client_secret.json` は `PROJECT_ROOT/local_raw/`（`:60`）にあり、**バンドル後はリポジトリルートが無い**ので置き場所を変える。#62 / #17 の名前一致の重複判定は移植しない方がよい | （将来）800〜1,000 | 新規 F（将来。#62 / #17 / #13 と統合） |
| `config.py` / `paths.py` / `source_files.py`（520 行）`/api/source-folder` GET/POST | Rust。`read_source_root`（`coreimage.rs:155`）が既に `app_data_dir/config.json` を読む。書き込み側（検証・`root_key_for`・TCC 判定）を足す。`CONFIG_VERSION 1` の形式を保ち既存インストールを引き継ぐ | 標準ライブラリのみ | 300〜400 | 新規 E |
| `presets.py`（182 行）`/api/health` | **移行済み**（#80/#98 `presets.rs`）。`/api/health` は廃止。`HealthResponse`（`src/lib/types.ts:106`、`engine: "rawpy/LibRaw"`）を消す | — | — | 新規 G（セットアップ削除）に含める |
| `lens.py`（463 行） | **廃止**（CI のレンズ補正に置き換わり済み） | lensfunpy | — | #90 |
| `server.py`（559 行） | **廃止**。CORS / Host 検査 / 静的配信は Tauri の asset プロトコル scope に置き換わる。`/source-jpg/` と `/thumbs/` のために **撮影フォルダとサムネイル置き場を scope に動的追加**する必要がある（現 scope は `$APPDATA/coreimage/**` のみ） | — | — | 新規 C / G |
| `main.rs:92-860`（venv / pip / spawn / signal cleanup） | **削除** | — | −770 | 新規 G |
| `scripts/*.py`（compare_engines / evaluate_*）、`tests/test_*.py` | 開発用。§6 | numpy / cv2 / PIL / rawpy | — | 新規 H |

### 3.1 EXIF 継承と GPS【確認】

現行 `local_raw/exif.py:114-134` `build_inherited_exif` は、ペア JPG の `Image.getexif()` を読み、(1) `Orientation` を 1 に、(2) ExifIFD の `PixelX/YDimension` を出力寸法に、
(3) `MakerNote`(0x927C) を破棄、(4) `Software` を設定して `exif.tobytes()` で書き直す。**GPS IFD (0x8825) には触れておらず、カメラ JPG に GPS があればそのまま出力に継承される。**
根拠: `docs/spikes-wave0.md:125`（実機 Sony JPEG で「GPS IFD も保持される」）と、本 ADR 作成時の合成 JPEG での再現（Pillow 13.0.0。GPS IFD 5 タグが出力で一致、MakerNote は消え、Orientation 1）。
pin されている Pillow は 12.3.0（`requirements-raw.txt`）で版が違うが、実機の根拠は spikes-wave0 側にある。

つまり、このプロジェクトの「GPS・撮影地をアプリが**新たに**書き込まない」方針は守られており、同時に**カメラが書いた GPS は落とさず残す**のが現行挙動である。
一方、#84 の本文（「GPS・MakerNote は書かない」）と設計書 §B1（`coreimage-feature-migration.md:270`「GPS は書かない現行方針を維持」）は現行と食い違う。
**移植後の要件（§6 決定 5 で確定）: 「現行と同じ: カメラ JPG の EXIF を継承し、Orientation 1・寸法更新・MakerNote 破棄・Software 設定。GPS IFD はそのまま継承する（消さない）。アプリが新たに GPS を書くことはしない」。**
#84 本文と設計書 §B1 の文言はこの要件に合わせて直す（「後続」1 項）。

実装手段:

| 手段 | 評価 |
|---|---|
| Rust `kamadak-exif` | 読み込み専用。要約 EXIF（`read_capture_exif` 相当: ISO・露光・F・焦点距離・レンズ）の取得には十分 |
| Rust `little_exif` | 書き込み可。JPEG の APP1 を読み直して書くが、Sony の私用タグ・サブ IFD の保全は未確認【推測】 |
| Rust 自前: APP1 を**バイト列のままコピー**し、IFD0 の Orientation / Software、ExifIFD の PixelX/Y を**同じ長さで上書き**、MakerNote エントリをタグ数デクリメントで外す | 再シリアライズしないので GPS IFD・Sony 私用タグ・サムネイル IFD が壊れない。400 行程度。MakerNote の実体バイトは残る（参照されないだけ）【推測: 容量が増えるだけで害は無い】 |
| Swift ImageIO（`CGImageSourceCopyProperties` → 辞書を編集 → `CGImageDestinationAddImage(properties:)`） | 【スパイク】合成 JPEG で Orientation 1・PixelX/Y 更新・Software・**GPS 辞書の継承**を確認。ただし ImageIO は **MakerNote を書かない／読み出さない**（合成元に入れた MakerNote が読み出し側で見えなかった）ので「破棄」は自動で満たすが、辞書を経由する再シリアライズのため **Sony 私用タグやサムネイル IFD の保全は実機で未確認** |

推奨: CLI が既に `CGImageDestination` で書いている（`Develop.swift:698`）ので、**まず Swift ImageIO 案を実機 Sony JPEG で検証**（新規 A の最初のタスク。`tests/test_exif.py` 相当の合成テスト + 実機 round-trip + GPS 保持の確認）。
そのとき properties 辞書経由に加えて `CGImageDestinationAddImageAndMetadata` + `CGImageMetadata` のコピー経由も試し、APP1 の保全がどちらで広いかを見る（確認項目であって結論ではない）。
保全に欠けがあれば Rust 自前 APP1 コピーへ切り替える。CLI のリクエストに `inherit_exif_from`（ペア JPG のパス）を足す形が設計書 §B1 の案とも合う。

## 4. #85 の扱い

#85 は Python の `DecoderFactory` を CLI 経路に差し替える「Python を残す前提の足場」。Python を廃止するなら成果物はすべて捨てられる。

| 案 | 内容 | 評価 |
|---|---|---|
| 続ける | #85 → #86 → #87 | 移行期間中の代理と本番の一致は保てる。ただし Python 側のコード・テスト・評価スクリプトの改修（数百行）が #87 で全て消える。#102 §5.2 の「評価ごとに現像」は 25〜73 秒/枚で、一致の価値より遅さが目立つ |
| **閉じる（推奨）** | #85 を閉じ、#86（純関数。Python と数値一致）と新規 B（CLI の decode キャッシュ op）を並行、→ #87 | #85 の「やること」のうち価値が残るもの（探索空間から `highlight_class` を外す、NR の固定表、`_SHARPNESS_SEARCH_MAX` の導出、しきい値の見直し、所要時間の記録）は #87 の本文へ移す |

#87 の Swift レンダラの呼び方: **sidecar の `--batch` に #102 §5.3 の `op: decode / render` を足したもの**（新規 B）。`(noise_reduction, exposure 格子点)` ごとに 1 回デコードして RGBAh を保持、
評価ごとに後段（7.2 ms）だけ回す。1 枚 50 評価で約 3〜6 秒（#102 §5.2）。プロセス内（案 B）にしても支配項は変わらない（§2）。

数値一致の検証（Python を消す前に基準値を固定する）:

1. #86 のフィクスチャ（固定入力 PNG に対する `_image_metrics` / `_star_measurements` / `pareto_front` の JSON）は **Python が動く間に生成してリポジトリに置く**。これは #86 に既に書かれているが、順序制約として明記する（新規 Issue「基準値の凍結」）。
2. 探索ループは numpy `default_rng` と同じ乱数列を Rust で再現しない（#87 の方針どおり）。代わりに「同じ seed・同じ指標入力なら同じ候補が選ばれる」を**系列固定のフィクスチャ**で検証する。
3. `scripts/eval_cases/candidates_baseline.json` の Python 経路の出力（候補の adjustments と指標）を凍結し、Rust 実装の受け入れ基準に使う。
4. ADR 0002 決定 4 の OS 更新回帰（`scripts/compare_engines.py`）は、rawpy を実行せず「保存した基準画像」と比較する形に変える（#90 に記載済み）。

## 5. 段階の方針（詳細は実装プラン）

Issue の順序・範囲・完了条件・並列化・検証手順は [単一アプリの段階的実装プラン](../design/single-app-implementation-plan.md) が正。ここでは方針だけを書く（2 か所に順序を書くと食い違うため、本節は更新しない）。

- **M0 を先に出す**（§6 決定 6）。M0 = 「Python 無しで起動 → フォルダ選択 → 解析（EXIF とグループ化だけの lite 版）→ プレビュー → 原寸現像 → JPEG 書き出し（EXIF 継承・GPS 保持）」。
  画面が `Analysis` 無しでは先へ進めない【確認: `src/App.tsx:825-835`】ので、解析を後回しにはできず、採点だけを後回しにする。
- M0 の中身: 基準値の凍結（0）→ 原寸現像 + EXIF 継承（A）‖ フォルダ設定（E）‖ 解析 lite と配信（C の前半）→ 書き出し（D の前半）→ Python 切り離し（G）。
  **G が終わった時点で `main.rs` の venv / pip / サーバ起動は無くなる。** `scripts/` 用の Python は開発者の手元に残る（§6 決定 4）。
- M0 で隠すもの: ブラケット合成（M2 #83/#84）、現像候補（M3 #86/B/#87、#88・#57 併走、#85 は閉じる）、写真取り込み（M1）。Drive は削除（決定 1）。
- M4 で Python 本体を消す（#90 + H）。`local_raw/bracket.py` と `development_candidates.py` は M2 / M3 の参照実装なので最後。契約文書（`API_CONTRACT.md`）の書き直しと CLAUDE.md の検証手順更新も M4。
- #89（リニア出力）は任意のまま。

## 6. オーナー判断（2026-10-22 確定）

| # | 事項 | 決定 | 影響 |
|---|---|---|---|
| 1 | Google Drive アップロード | **初版から外す。** 将来対応の Issue（新規 F）に回す | M0（G）で UI・state・API 関数・型を削除。Python 本体は H で削除。#62 / #17 / #13 は F に統合 |
| 2 | 「写真」アプリへの取り込み | **残す** | M1 で Rust（`osascript` 配列呼び出し）に移す。TCC の許可先がアプリ本体に変わるので初回の許可導線を用意する |
| 3 | ブラウザ（vite dev + Python サーバ）だけで動く形 | **捨てる。Tauri アプリだけにする** | `isTauri()` 分岐は死にコード化し G で整理。検証は `tauri dev`（WebView）と `.app`。CLAUDE.md の「画面表示の検証はブラウザ」を改める（実装プラン §6） |
| 4 | `scripts/` の Python 評価スクリプト（compare_engines / evaluate_*） | **開発用に残す** | `requirements-raw.txt` → `requirements-dev.txt`（H）。ユーザーには配布しない。M0 で Python 起動を外した後も開発者の手元の venv で動かす |
| 5 | EXIF の GPS | **消さない。カメラ JPG の GPS IFD をそのまま継承する現行の動作を保つ。アプリが新たに GPS を書くことはしない** | §3.1 の要件で確定。#84 本文と設計書 §B1（`coreimage-feature-migration.md:270`）の「GPS は書かない」は現行と食い違っているので直す |
| 6 | 進め方 | **最優先は「Python 無しの単一アプリとして動作させる」こと。初版に現行の全機能は要らない。まず動かし、その上で機能を足す** | §5 の M0 先行。詳細は実装プラン |

設計側で決めたこと（オーナー判断は不要と判断。異議があれば Issue で）:

| 事項 | 決定 | 理由 |
|---|---|---|
| サムネイルの形式 | WebP q80 → **JPEG** に変える | 内部形式で asset プロトコル配信になる。Rust の WebP lossy 書き出しは `webp` crate（libwebp 同梱）が要る |
| 書き出し先 | リポジトリ相対 `outputs/export/` → **`$APPDATA/export/<batch>/`**（M0）。ユーザー指定フォルダは任意の後続 | バンドル後にリポジトリルートが無い（`local_raw/export.py:27`） |
| 既定の現像元フォルダ | 無し（「フォルダ未設定」状態を新設） | `sample/100MSDCF` はこの checkout にもバンドルにも無い（`local_raw/config.py:32`） |

## 7. リスクと未確認事項

- **EXIF の実機保全**: ImageIO 経由で Sony 実機 JPEG の GPS IFD・私用タグ・サムネイル IFD が保たれるかは未確認（§3.1）。新規 A の最初に確かめ、欠ければ Rust の APP1 バイトコピーに切り替える。
- **原寸での写像の較正**: プレビューと原寸が両方 CI になっても、ADR 0002 の「原寸で後段処理を含めて現行と同等」の条件は自動では満たされない。デコード段の sharpness / NR は倍率で効き方が変わる（#102 §3.4、#57）ので、新規 A で原寸の較正と比較ハーネスの full 行を別に持つ。
- **Mertens / MTB の自前実装の一致**（#83）: OpenCV と完全一致はしない。基準画像は Wave 0 で凍結する。
- **PSO の数値一致**: 乱数列は再現しない方針なので、基準との比較は候補集合の指標で行う。しきい値の再調整（`apple_default` 向け）が Python 時代の基準と混ざらないよう、Wave 0 の凍結を先にする。
- **メモリ**: 原寸 full のバッチで footprint 約 1.09 GiB（README）。sidecar なので隔離されるが、解析（Rust）と現像（CLI）と WebView が同時に走る時間帯のピークは未計測。
- **TCC**: 撮影フォルダ（外付け / SMB）の読み取りと「写真」の自動化の許可先がアプリ本体になる。初回の許可導線を UI に用意する（`config.py` の `PERMISSION_MESSAGE` 相当）。
- **asset プロトコル scope の動的追加**: 撮影フォルダは実行時に切り替わる。Tauri 2 の `asset_protocol_scope().allow_directory` で足すが、SMB 上のフォルダでの挙動は未確認。
- **ビルド依存**: `cargo test` は今も externalBin の存在を要求する（README）。Python が消えても Xcode / Swift toolchain は必須のまま。(B) に進むなら swift-rs 1.0.7 はそのままでは使えない（§2.2）。
- **契約文書**: `docs/API_CONTRACT.md` は HTTP 契約で書かれている。Tauri コマンドの契約として書き直す範囲が大きい（新規 G / H で扱う）。
- **ブラウザモード廃止**の影響範囲は `raw-api.ts` と `coreimage-api.ts` の `isTauri()` 分岐に閉じているが、`App.tsx` の起動シーケンス（`setupState`、`/api/health` の待ち）に手が入る。
- 本 ADR の規模見積もりは行数ベースの目安であり、Issue 化の際に各 Issue で見直す。

## 後続（この ADR の採用時にメインが行うこと）

1. 実装プラン §2〜§4 の新規 Issue（N-00 / N-A / N-E / N-C1 / N-D1 / N-G1 / N-G2 / N-C2 / N-D2 / N-B / N-H / N-DOC / N-F）を立て、M0 のマイルストーンを作る。#85 を「#87 へ統合」で閉じる。#105 は Issue 群を立てたら閉じる。
2. #84 本文と設計書 §B1（`coreimage-feature-migration.md:270`）の GPS の文言を §3.1・§6 決定 5 に合わせて直す（「GPS IFD は継承する。MakerNote は破棄」）。
3. ADR 0002 の「置き換えに進む条件」の Adjustments の行に新規 A を紐づける。
4. Drive 関連の既存 Issue（#62 / #17 / #13）に「初版の対象外。N-F に統合」を書く。
