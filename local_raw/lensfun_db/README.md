# lensfun 追加較正データ

`local_raw/lens.py` が `lensfunpy.Database(paths=sorted(glob("local_raw/lensfun_db/*.xml")))`
で読み込む追加DB。lensfunpy 同梱DB（lensfun 0.3.4）とマージされる。

**このディレクトリが存在しない場合・空の場合・XMLが壊れている場合でも、
現像は同梱DBのみで正常に動作する**（`lens.py` の多段フォールバック）。

## mil-sigma.xml

| 項目 | 値 |
|---|---|
| 取得元 | `https://raw.githubusercontent.com/lensfun/lensfun/master/data/db/mil-sigma.xml` |
| 取得日 | 2026-08-10 |
| upstream master SHA | `6804b5f581f31b10a90a06d811acb37d6877ef32` |
| ローカル改変 | `sed -e 's/<lensdatabase version="2">/<lensdatabase version="1">/'` の**1箇所のみ** |

### なぜ version 属性を書き換えたか

lensfun master のDBスキーマは **version 2**、一方 lensfunpy 1.18.0 が同梱する
lensfun は **0.3.4 = version 1 までしか解釈できない**。未改変のまま渡すと

```
[Lensfun] mil-sigma.xml:3:1: Database version is 2, but newest supported is only 1!
→ lensfunpy._lensfun.XMLFormatError
```

で `Database()` の構築自体が失敗する。

このファイルが実際に使用している較正モデルは `ptlens` / `poly3`（distortion）、
`poly3`（tca）、`pa`（vignetting）で、**すべて version 1 が解釈できるモデル**。
version 2 固有の要素は `<aspect-ratio>` が5箇所あるのみで、いずれも無関係な
Micro 4/3 レンズのエントリに付随しており、lensfun 0.3.4 は警告なしで読み飛ばす
（実測: パース時の warning/error はゼロ）。

**較正係数そのものは upstream の値を一切改変していない。**

### 目的のレンズ

本プロジェクトのサンプル 281 枚中 276 枚が下記レンズ。同梱DB（0.3.4）には存在しない。

```xml
<lens>
    <maker>Sigma</maker>
    <model>Sigma 10-18mm F2.8 DC DN | Contemporary 023</model>
    <mount>Sony E</mount>
    <cropfactor>1.534</cropfactor>
    <calibration>
        <!-- ptlens distortion @ focal 10 / 12 / 15 / 18。vignetting データは無い -->
    </calibration>
</lens>
```

EXIF の `LensModel` は `10-18mm F2.8 DC DN | Contemporary 023`（`Sigma ` プレフィックス無し）
なので、`find_lenses(camera, maker=None, lens=<EXIF値>)` で score 82 のヒットになる。

> このレンズには **distortion 較正しか無く vignetting 較正が無い**。
> したがって `lens_correction.flags` は `["distortion"]` になる（`vignetting` は入らない）。

## mil-sony.xml を置いていない理由

同じく master から取得したが、**version 1 では解釈できない v2 専用属性
`real-focal` が `<distortion>` に付いており**（`mil-sony.xml:694:109`）、
version 属性を書き換えても

```
[Lensfun] Unknown attribute `real-focal' for element <distortion>!
→ XMLFormatError
```

で失敗する。`Database(paths=[...])` は**1ファイルでも壊れていると全体が失敗する**ため、
同梱するとレンズ補正機能ごと巻き添えで死ぬ。

一方、このプロジェクトで必要な Sony レンズ `E 55-210mm F4.5-6.3 OSS`（サンプル5枚）は
**lensfunpy 同梱DBに既に存在する**（`Sony / E 55-210mm f/4.5-6.3 OSS`、score 85）ため、
mil-sony.xml は不要と判断して置いていない。

## 更新手順

```bash
curl -fsSL https://raw.githubusercontent.com/lensfun/lensfun/master/data/db/mil-sigma.xml \
  | sed -e 's/<lensdatabase version="2">/<lensdatabase version="1">/' \
  > local_raw/lensfun_db/mil-sigma.xml
.venv/bin/python -c "from local_raw.lens import lensfun_info; print(lensfun_info())"
```

lensfunpy が lensfun 0.4 系（DB version 2）を同梱するようになったら、
この `sed` は不要になり `mil-sony.xml` もそのまま追加できる。
