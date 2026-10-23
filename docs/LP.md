# 紹介ページ(LP)の管理

GeoFrame RAW Assist の紹介ページは、このリポジトリでアプリと一緒に管理します。文書の一覧は [Docs](README.md)、アプリの機能とビルド方法は [README](../README.md) を参照してください。

- 公開先: https://ysogabe.github.io/geoframe-raw-assist/
- 本文: [site/index.html](../site/index.html)
- 見た目: `site/styles.css`
- アイコン: `site/favicon.svg`(`public/favicon.svg` と同じもの)
- 画像: `site/images/` の次の 4 枚(PNG、横長)
- 公開処理: [.github/workflows/pages.yml](../.github/workflows/pages.yml)

## 画像

| ファイル | 内容 | 撮影日と内容 |
| --- | --- | --- |
| `site/images/overview.png` | 解析後の一覧(撮影グループ、代表と推薦理由) | 2026-10-24 撮影。山の風景 52 組(登山道・夕焼け・湿原。人物なし)を解析した直後の一覧 |
| `site/images/develop.png` | 操作パネル(プリセットとスライダーでの現像) | 2026-10-24 撮影。夕焼けの DSC04486(類似 5 枚の代表)に「黒つぶれ救済」を選んだ状態 |
| `site/images/compare.png` | 現像の前後比較 | 2026-10-24 撮影。同じ写真のプレビュー現像の前後比較 |
| `site/images/candidates.png` | 現像候補の自動生成 | 2026-10-24 撮影。同じ写真で「自動候補を作る」を実行した結果(自然・前景を明るく など) |

`<img>` に `width` と `height` は付けず、CSS で `width: 100%; height: auto` にしています。画像はアプリのウィンドウ(1440×900)を Retina で撮り、幅 1600px に縮小した PNG です。写真は外付けディスクから選んで共有フォルダにコピーし、アプリの現像元フォルダをそこに切り替えて撮りました(撮影後に元の設定へ戻しています)。

## 内容を更新する

アプリの機能や画面を変えたら、LP の説明、画像、FAQ も見直します。

- 実装済みの機能と、今後の予定を分けて書きます。予定の機能(HDR、連写の選好学習、パノラマ、参照プリセットなど)は、LP の「できること」に入れません。
- 数値と条件は、リポジトリの文書(`README.md`、`docs/API_CONTRACT.md`、`docs/adr/`、`docs/reports/`)にあるものだけを使います。処理時間は、計測した環境と条件を添えます。
- 画像を追加・差し替えるときは、人物の顔、位置情報、個人情報、撮影した場所を特定できるものが写っていないか確認します。画面に出る写真は、公開してよいものだけを使います。
- 外部フォント、CDN、アクセス解析、外部の JavaScript は使いません。画像と CSS は `site/` の中に置き、相対パスで参照します。

### 画面画像の撮り方

アプリで、公開してよい写真のフォルダを開いてから撮ります。macOS では、次のどちらかでウィンドウを撮影できます。

- `screencapture -l <ウィンドウID> 出力.png`(ウィンドウ ID は取得ツールなどで調べます)
- ⌘⇧4 を押してから、スペースキーを押し、撮りたいウィンドウをクリックする

撮影後は、上の表の説明欄に、撮影日と内容を追記します。

## 手元で確認する

リポジトリのルートで次を実行し、http://127.0.0.1:18765/ を開きます。

```sh
python3 -m http.server 18765 --bind 127.0.0.1 --directory site
```

PC とスマートフォンの幅(1280px と 390px 程度)で、画像が読み込まれること、横にはみ出さないこと、ページ内リンクと FAQ が動くことを確認します。確認が終わったら、サーバーを止めてください。

## 公開する

リポジトリが Public になってから、GitHub のリポジトリ設定で、**Settings → Pages → Build and deployment → Source** を **GitHub Actions** にします。

`main` の `site/` または公開用ワークフローに変更が入ると、GitHub Actions が `site/` を GitHub Pages に公開します。Actions の「Deploy LP to GitHub Pages」から手動実行もできます。アプリのビルドは不要です。

公開後は、次のコマンドで、ページと CSS が HTTP 200 で返ることを確認します。

```sh
curl -I https://ysogabe.github.io/geoframe-raw-assist/
curl -I https://ysogabe.github.io/geoframe-raw-assist/styles.css
```
