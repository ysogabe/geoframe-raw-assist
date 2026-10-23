# GeoFrame RAW Assist — 技術設計

v0.1.0 の内部構成の概要です。細部は、本文中のリンク先の既存文書（API 契約、ADR、設計書、計測レポート）が正です。ビルドと操作は [使い方](USAGE.md)、文書の一覧は [Docs](README.md) を参照してください。

## 1. 全体像

アプリは 3 つのツリーでできています。

| ツリー | 言語 | 役割 |
|---|---|---|
| `src/` | TypeScript（React + Vite + Tailwind） | 画面。状態は `src/lib/app-state.ts` の reducer に集め、Tauri コマンドの呼び出しは `src/lib/raw-api.ts`（解析・フォルダ・プリセット・書き出し・取り込み）と `src/lib/coreimage-api.ts`（エンジン情報・現像・合成）に閉じています。候補生成の呼び出しだけは `src/App.tsx` にあります |
| `src-tauri/` | Rust（Tauri 2） | 本体。解析、現像・合成の指示、キャッシュ、書き出し、EXIF 継承、候補探索、「写真」への登録 |
| `native/coreimage-cli/` | Swift | Core Image の現像 CLI `geoframe-ci-develop`。`bundle.externalBin` として `.app` に同梱 |

```
  画面 (React, WebView)
        │  invoke("develop_full_coreimage", {...})        ← Tauri コマンド 13 本 + geoframe:// スキーム
        ▼
  本体 (Rust)  ── 入力検証・キャッシュ判定・実行枠(DEVELOP_SLOT)
        │  子プロセスを起動
        ├─ 単発:  stdin に JSON 1 個  →  stdout の最終行に JSON 1 行
        └─ 候補生成: --batch（JSON Lines の常駐プロセス。op: decode / render / release）
        ▼
  現像 CLI (Swift, Core Image / CIRAWFilter)  →  JPEG を $APPDATA に書く
        ▲
        └─ Rust が出力に EXIF を後から付ける（本現像・本合成）
```

画面は `invoke` だけを使い、HTTP のサーバーは持ちません。Python のサーバーや rawpy を使う旧構成は廃止しました（[ADR 0003](adr/0003-python-removal-single-app.md)）。コマンドの名前・引数・戻り値・エラー code の正は [API 契約](API_CONTRACT.md) です。

## 2. データの保存先とキャッシュ

生成物はすべて OS のアプリデータフォルダ `$APPDATA`（macOS では `~/Library/Application Support/com.geoframe.rawassist/`）に置き、リポジトリや元の写真のフォルダには書きません。構成は [使い方](USAGE.md) の「操作の流れ」と [API 契約](API_CONTRACT.md) §4 にあります。

- 解析キャッシュ `analysis/<root_key>.json` とサムネイル `thumbnails/<root_key>-<ID>.jpg` は、`root_key`（現像元フォルダの解決済みパスの SHA-1 先頭 8 文字）でフォルダごとに分けます。撮影 ID はカメラの連番で、カードをまたぐと衝突するためです。
- 解析キャッシュは、版、現像元フォルダのパスとファイル名一覧の署名、サムネイルの存在が一致するときだけ有効です。ファイルの中身・mtime・サイズは見ません。
- 現像のキャッシュキーは、CLI への要求全量、CLI の版と実行ファイルの SHA-256、ARW のサイズと mtime などの SHA-256 先頭です。JPEG と来歴 JSON がそろっているときだけキャッシュヒットとし、書き込みは一時ファイルを経由する rename で行います。
- 各出力ディレクトリは、600 組または 1.5GB を超えると、古い順に 80% まで削除します。

## 3. 現像パイプライン

現像は Core Image の `CIRAWFilter` で行います。基本設定は `apple_default` profile・decoder 9・レンズ補正 ON です（[ADR 0002](adr/0002-coreimage-engine-decision.md)）。

- 調整値（9 キー: 露出・色温度・シャドウ持ち上げ・ハイライト回復・明るさ・コントラスト・彩度・シャープネス・ノイズ低減）は、現像では画面側でプリセットの値と合成し（ブラケット合成と候補生成では Rust 側で解決し）、Rust の `adjustments.rs` が範囲のクランプと検証をして CLI に渡します。CLI が、露出・色温度・ノイズ低減・デコーダのシャープを `CIRAWFilter` に、残りを Metal カーネルを含む後段チェーンに写像します。対応表は [パラメータ対応表](development/coreimage-parameter-mapping.md)、`highlight_recovery` の写像は [プリセット再調整の設計](design/coreimage-preset-retune.md) にあります。
- プレビューは長辺 2400px 固定（`max_edge`）、本現像は原寸です。プレビューは EXIF を書きません。
- 原寸現像と本合成は、カメラのペア JPG の EXIF APP1 をバイト列のままコピーし、Orientation を 1、寸法を出力寸法、Software を `GeoFrame RAW Assist (Core Image)` に書き換え、MakerNote と IFD1 へのリンクを落とします。**GPS IFD は、ペア JPG にあればそのまま継承します。アプリが新たに GPS を書くことはありません。** 継承できなくても現像は失敗させず、`exif_inherited: false` で返します（`src-tauri/src/exif_inherit.rs`、[API 契約](API_CONTRACT.md) §7.2）。
- CLI の実行は Rust の `DEVELOP_SLOT` が 1 本ずつに制限します。待てるのは 2 件までで、それを超えると `BUSY` を返します。プレビュー・本現像・合成・候補生成で共有します。キャッシュヒットは枠を使いません。

## 4. 解析・グループ化・推薦

`src-tauri/src/analysis.rs` が、現像元フォルダ直下の `DSC#####.JPG` / `.ARW` を走査し、バックグラウンド（最大 6 ワーカー）で JPEG だけを解析します。ARW は解析では読みません。

- 採点は JPEG の 1/8 スケールのデコードに対し、ラプラシアンの分散（シャープさ）、白飛びの割合（いずれかの色が 250 以上）、黒つぶれの割合（全色が 5 以下）、平均輝度を計算します。サムネイル（長辺 640px）だけ EXIF の向きを適用します。
- グループ化は、撮影時刻順に並べ、時刻の隙間が 60 秒を超える、レンズが変わる、焦点距離の比が 1.5 倍を超える、のいずれかで切ります。種類は、2 枚未満が単写、露出補正が 2 種類以上かつ幅 0.6EV 以上ならブラケット（連写より優先）、時刻の隙間の中央値が 1.5 秒以下なら連写、それ以外は類似です。
- 代表は、シャープさの順位（重み 0.6）と白飛びの少なさの順位（重み 0.4）の合計で選びます。次点との差が小さいと、確信度が 0.6 未満になり、目視を勧める理由が付きます。
- RAW 現像の推薦は、夜景（ISO 1600 以上かつ暗い）や白飛びの割合などの閾値で 4 段階（`strong` / `recommended` / `maybe` / `none`）に分けます。理由は日本語の文で付けます。
- 進捗はイベントではなく、画面が `get_analysis` を呼び続けて取ります。

採点の移植と旧 Python の基準との一致は [解析の採点・推薦の検証](reports/analysis-scoring-validation.md) にあります。

## 5. ブラケット合成

露出を変えた 2〜9 枚を、Core Image CLI の合成モード（`mode: "merge"`）の 1 リクエストで 1 枚にします（[CLI の README](../native/coreimage-cli/README.md) の「合成モード」、[実装報告](reports/coreimage-merge-mode-report.md)）。

- 各フレームを RAW からデコードしたあと、MTB（Median Threshold Bitmap）で位置合わせし、Mertens の Exposure Fusion（コントラストと彩度で重み付けしたピラミッド融合。露出の重みは 0）で 1 枚にします。HDR とトーンマップは採用していません。
- 位置合わせの基準は、昇順に並べた列の中央です。ファイル名の先頭 ID と EXIF の継承元は、昇順の先頭のフレームです。ずれが大きすぎる、または共通部分が空になるときは、シフトなしで合成して `aligned: false` にします。
- フレームごとの露出の正規化はしません（露出差が消えるため）。`exposure` と `highlight_recovery` は全フレームで 0 に固定し、色温度・ノイズ低減・デコーダのシャープは各フレームに、残りの後段は合成結果に 1 回だけかけます。
- 実行前に、画素数から作業メモリを見積もり、予算を超えるリクエストを拒否します。9 枚のハードリンクで作った同一画像の原寸（24MP）では、57.6 秒、最大 RSS 3.28 GiB でした（Apple M3 / macOS 27.0.1、資源の確認であって画質の確認ではありません）。
- Rust 側は `src-tauri/src/bracket.rs`。出力先は本合成 `coreimage/merged/`、プレビュー `coreimage/merged_preview/` とディレクトリで分け、書き出しは本合成だけを受け付けます。
- 実際の AEB や三脚での長秒ブラケットの画質は、未確認です。

## 6. 現像候補の自動生成

`generate_development_candidates`（`src-tauri/src/candidates/`）は、1 枚の RAW について、スタイル別の調整値を多目的の PSO（粒子群最適化）で探します。

- 評価は、ARW を Core Image で縮小デコードした長辺 640px の代理画像で行います。CLI は `--batch` の常駐プロセスで、同じ RAW を `(露出, ノイズ低減, デコーダのシャープ)` ごとに 1 回だけデコードして保持し（`op: decode`）、後段の調整だけを繰り返し適用します（`op: render`）。後段 1 回は 7.2 ms（中央値。macOS 27.0.1 / M3）で、デコードを含む 1 件（約 0.49 秒）の約 1/70 です（[小サイズレンダの計測](reports/coreimage-small-render-performance.md)）。
- 探索するのは 6 つの値（露出、シャドウ持ち上げ、明るさ、コントラスト、彩度、シャープネス）です。露出はプリセット値と ±0.5EV の 3 格子です。`highlight_recovery` と色温度は探索せず、プリセット値のままです。乱数は seed から決まり、同じ入力なら同じ結果になります。
- 白飛び・黒つぶれ・平坦部のノイズ・細部・ハロ・色飽和・階調の幅・基準との差などを評価し、Pareto の非劣解と、スタイルごとの重みで候補を選びます。基準より白飛びや黒つぶれが 10 ポイント以上悪化する候補などは除外し、理由を `rejected` に残します。
- スタイルは、自然（プリセット値そのもの）、SNS 向けが常に、星空・前景を明るく・低ノイズが EXIF や画像の特徴が合うときに加わります。
- 限界として、代理画像（640px）と原寸で、指標のずれの向きが一致しない場合があります。11 枚・44 候補の計測で再現され、最終候補を原寸の ROI で再評価する方向が決まりました（[計測](reports/coreimage-proxy-full-measurement.md)）。美的な正解を保証するものではありません。

## 7. 書き出しと「写真」への登録

- `export_selection`（`src-tauri/src/export.rs`）は、選んだ写真を `$APPDATA/export/<バッチ名>/` にバイト列のままコピーし（再エンコードしない）、書き出し内容とレシピの `manifest.json` を書きます。現像済みや合成の写真は、来歴 JSON を検証してから受け付け、プレビュー品質の画像は拒否します。書き出しはプロセス内で直列化し、一時ファイルを経由する rename で置きます。
- `import_to_photos`（`src-tauri/src/photos_import.rs`）は、書き出しフォルダ直下の JPEG を、`osascript` で `scripts/photos_import.applescript`（アプリのリソースとして同梱）を実行して「写真」のアルバムに取り込みます。引数は配列で渡し、文字列連結でスクリプトを組み立てません。
- 「写真」の操作には macOS のオートメーションの許可が要り、許可の対象はアプリ本体です。`Info.plist` の `NSAppleEventsUsageDescription` に用途を書いています。許可がないと「写真」側の失敗がエラーとして返ります。このエラーは `Ok` の中の `status: "error"` で返る点が、他のコマンドと違います。

## 8. テストの構成

| 種類 | 実行 | 押さえるもの |
|---|---|---|
| フロント（vitest） | `npm test` | `app-state.ts` の reducer、`raw-api` と `coreimage-api` の `invoke` の引数、候補の表示用変換、書式、グループ移動など。純粋関数で、node 環境で動きます |
| Rust | `cargo test --manifest-path src-tauri/Cargo.toml` | 入力検証、キャッシュキー、出力先の分離、解析の採点とグループ化、EXIF 継承（合成 JPG で GPS の保持と MakerNote の除去）、書き出しの検証、PSO の決定性と安全策、`--batch` セッション。プリセットはフィクスチャ（`src-tauri/tests/fixtures/presets.json`）との一致で守ります |
| Swift | `swift test --package-path native/coreimage-cli` | CLI の要求の検証、調整値の写像、後段チェーン、バッチと decode キャッシュ、合成のコア（位置合わせ・融合）とリクエスト |
| Python | `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` | 評価スクリプト（比較・候補の集計・凍結基準の照合）とプリセット表の整合。アプリ本体の検証ではありません |

実機の写真が要る確認（macOS 更新後の回帰、画面の手動確認）は、[使い方](USAGE.md) の「開発者向け」と [回帰確認手順](development/coreimage-regression-check.md) にあります。

## 9. 主な設計判断

| ADR | 要約 |
|---|---|
| [ADR 0001](adr/0001-coreimage-raw-poc.md) | Core Image（`CIRAWFilter`）で Sony ARW を現像する Swift CLI の PoC と、その契約（stdin に JSON 1 個、stdout に JSON 1 行） |
| [ADR 0002](adr/0002-coreimage-engine-decision.md) | 現像エンジンを rawpy から Core Image（`apple_default`・decoder 9・レンズ補正 ON）に切り替える決定。OS 標準だけで済み、速く省メモリな一方、内部パラメータが非公開で OS 更新で画が変わりうるため、回帰確認を運用に入れる |
| [ADR 0003](adr/0003-python-removal-single-app.md) | Python を全廃し、現像エンジンを組み込んだ単一の Tauri アプリにする決定。Google Drive への送信は初版から外し、GPS はカメラ JPG のものを消さずに継承し、ブラウザ単体では動かさない |

これらに至る比較や計測は、[Docs](README.md) の「計測・調査の記録」にまとめています。
