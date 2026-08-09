# Wave 0 スパイク結果

実施日: 2026-08-10 / 実施環境: macOS (darwin 25.5.0, arm64), `.venv` Python 3.14.2

対象プラン: `.claude/plans/vectorized-snacking-falcon-agent-ad862d8a7d0ff47d3.md` §b / §c

インストール済みバージョン（`.venv/bin/python -m pip list` 実測）:

| パッケージ | バージョン | 備考 |
|---|---|---|
| numpy | 2.5.1 | **変化なし**（lensfunpy/opencv 追加でも降格されず。rawpy の ABI 安全） |
| Pillow | 12.3.0 | |
| rawpy | 0.27.0 | |
| lensfunpy | 1.18.0 | `cp314-macosx_14_0_arm64` wheel、ソースビルド無しで導入 |
| opencv-python-headless | 5.0.0.93 | `cp37-abi3-macosx_13_0_arm64` wheel。`cv2.__version__` は `"5.0.0"`（末尾 `.93` は含まれない） |

---

## スパイク1: lensfun DB ヒット確認（プラン §c）

### 結論

**同梱DBだけでは不十分。追加XMLが必要。** ただし追加XMLのロード機構は実証済みで、Wave 1-A はフォールバック設計をそのまま実装してよい。

| 対象 | 同梱DB（lensfunpy 1.18.0 / lensfun 0.3.4） | 判定 |
|---|---|---|
| カメラ `Sony` / `ILCE-6400` | **ヒット**（crop_factor = 1.534000039100647） | OK |
| レンズ `E 55-210mm F4.5-6.3 OSS`（EXIF実測・大文字F） | **ヒット** → `Sony / E 55-210mm f/4.5-6.3 OSS`（小文字 f/ 表記へあいまい一致） | OK |
| レンズ `10-18mm F2.8 DC DN \| Contemporary 023` | **ミス**（strict / loose_search=True の両方、maker 有無の全4通りで 0 件） | **NG** |

同梱DB規模: cameras=948 / lenses=1304 / mounts=49。`10-18` を含むレンズは Canon EF-S 10-18 と Sony E 10-18mm f/4 OSS のみで、Sigma 10-18mm F2.8 DC DN は**存在しない**。

### 重要: EXIF の LensModel には**メーカー名が入らない**

プラン §0 は「`Sigma 10-18mm F2.8 DC DN | Contemporary 023` が EXIF実測文字列と完全一致」としているが、**実測は `Sigma ` プレフィックス無し**だった（sample/100MSDCF 全281枚を Pillow で走査）:

| EXIF `LensModel` (0xA434) 実測値 | 枚数 |
|---|---|
| `10-18mm F2.8 DC DN \| Contemporary 023` | 276 |
| `E 55-210mm F4.5-6.3 OSS` | 5 |

→ `find_lenses(cam, maker, lens)` の **`maker` は EXIF から取れない**（ボディは SONY、レンズメーカーは記録されない）。
→ **`maker=None` で呼ぶこと**。`maker='Sigma'` を渡しても同梱DBには無いので変わらないが、追加XML投入後は `maker=None` で正しくヒットする（下記実証）。

### API シグネチャ（実物確認済み。Cython クラスのため `inspect.signature` は不可、`__doc__` で確認）

```
lensfunpy.Database(paths=None, xml=None, load_common=True, load_bundled=True)
    paths : iterable of str  -- XML ファイルパス群
    xml   : str              -- XML 文字列を直接ロード
Database.find_cameras(maker=None, model=None, loose_search=False) -> list[Camera]
Database.find_lenses(camera, maker=None, lens=None, loose_search=False) -> list[Lens]
```

### db_version の取得方法

`lensfunpy.lensfun_version` → **`(0, 3, 4, 0)` のタプル**（lensfun 本体のバージョン）。
DBスナップショット自体のバージョン番号を返すAPIは存在しない。
→ `HealthResponse.lensfun.db_version` には `"0.3.4.0"`（`".".join(map(str, lensfunpy.lensfun_version))`）を入れる。§d では optional なので、この扱いを API_CONTRACT.md に明記済み。

### 追加XMLロードの実証（動いたコード断片）

`paths=` / `xml=` の両方で、**同梱DBにマージされる形で**追加ロードされることを確認した
（lenses 1304 → 1305、カメラ検索・Sony 55-210 検索は引き続き成功）:

```python
import glob, lensfunpy
paths = sorted(glob.glob("local_raw/lensfun_db/*.xml"))
db = lensfunpy.Database(paths=paths)          # load_bundled=True が既定 → 同梱DBと合成される
cam = db.find_cameras("Sony", "ILCE-6400")[0]
lens = db.find_lenses(cam, None, "10-18mm F2.8 DC DN | Contemporary 023")[0]
#   -> maker='Sigma'  model='10-18mm F2.8 DC DN | Contemporary 023'  score=85
```

`paths` が空リスト/存在しないディレクトリの場合は `lensfunpy.Database()` と等価に扱えばよい（同梱DBのみ）。

Modifier パイプライン（プラン §c の適用手順）も同スタックで**完走を確認**:

```python
mod = lensfunpy.Modifier(lens, cam.crop_factor, w, h)
mod.initialize(focal_length, aperture, 1000.0,
               flags=lensfunpy.ModifyFlags.VIGNETTING | lensfunpy.ModifyFlags.DISTORTION)
mod.apply_color_modification(img_float32)      # -> True、in-place
coords = mod.apply_geometry_distortion()       # -> ndarray (H, W, 2) float32
out = cv2.remap(img_uint8, coords, None, cv2.INTER_LANCZOS4)   # -> (H, W, 3) uint8
```

`ModifyFlags` の実メンバ: `ALL, DISTORTION, GEOMETRY, SCALE, TCA, VIGNETTING`。

### Wave 1-A への指示事項

1. **`local_raw/lensfun_db/*.xml` 方式を採用**。ディレクトリが空でも動く（同梱DBのみ）フォールバックを必ず実装する。
2. `find_lenses` は **`maker=None`** で呼ぶ（EXIF にレンズメーカー名が無いため）。ヒット0なら `loose_search=True` で再試行し、それでも0なら `lens_correction.applied=false, reason="lens_not_in_db"` で**現像は正常続行**。
3. **Sigma 10-18mm F2.8 DC DN の実キャリブレーションXMLは未取得**。sample の 276/281（98%）がこのレンズなので、XML を入れない限り実質すべての写真でレンズ補正は `applied=false` になる（＝MVPとしては動作するが補正効果は Sony 55-210 の5枚のみ）。
   取得元: `https://raw.githubusercontent.com/lensfun/lensfun/master/data/db/mil-sigma.xml`（Sony 分は `mil-sony.xml`）。
   **本スパイクではこのダウンロードを実行していない**（外部ファイルの取得はユーザー承認が必要な操作のため、プラン §h の「XML取得手順を Wave 1-A へ指示」に従い手順の確定に留めた）。
   ロード機構の実証には、上記スキーマの**プレースホルダ係数を書いた最小XML**をスクラッチ領域に作って使用した（リポジトリには残していない）。
4. **`apply_color_modification` の後は必ずクリップすること。** プレースホルダ係数での実測で、出力が `0.0 〜 5.7e12` まで発散した。ビネット補正は多項式による除算なので、係数や像高の条件次第で容易に範囲外へ出る。`np.clip(img, 0.0, 1.0)` を挟まないと uint8 変換で破綻する。

---

## スパイク2: Pillow Exif round-trip 検証（プラン §b）

### 結論

**Pillow 単独で要件を満たす。piexif は不要（導入しない）。** プラン §b の決定をそのまま採用してよい。

検証は2段構えで実施した。合成JPEGだけでは「Pillow が自分で書いたものを Pillow が読む」ことしか証明できないため、
**MakerNote / GPS IFD / サムネイルIFD を持つ実機 Sony JPEG** でも同じ検証を行った。

| ケース | 入力 | 判定 |
|---|---|---|
| A: 合成JPEG（Pillow生成、Orientation=8） | 120×80 → 3000×2000 | **PASS**（14項目全て） |
| B: 実機 Sony JPEG `DSC02710.JPG`（Orientation=8, 16MB, MakerNote/GPS 有り） | 6000×4000 → 6000×4000 | **PASS**（14項目全て） |
| B': 同上・寸法を実際に変更 | 6000×4000 → 4000×2667 | **PASS**（PixelX/Y が 4000/2667 に更新） |

検証した項目（read → mutate → `exif.tobytes()` → save → 再読込）:

- `Orientation`(0x0112) 8 → **1** に正規化される
- ExifIFD `PixelXDimension`(0xA002) / `PixelYDimension`(0xA003) が出力実寸に**更新される**
- `MakerNote`(0x927C) が **消える**（ExifIFD タグ数 41 → 40。他タグは巻き添えにならない）
- `Software`(305) が `"GeoFrame RAW Assist (rawpy 0.27.0)"` に**設定される**
- **保持される**: `DateTimeOriginal`(0x9003), `SubsecTimeOriginal`(0x9291), `ISOSpeedRatings`(0x8827),
  `LensModel`(0xA434), `FNumber`(0x829D), `ExposureTime`(0x829A), `FocalLength`(0x920A), `Make`, `Model`
- **GPS IFD (0x8825) も保持される**（実機ファイルで present → present）

→ 焦点だった **sub-IFD（ExifIFD）の round-trip は実機ファイルでも保持される**。

### 実証コード（`build_inherited_exif` の骨格そのもの）

```python
with Image.open(source_jpg) as im:
    exif = im.getexif()
    exif[0x0112] = 1                                   # Orientation 正規化（rawpy は正立出力）
    exif[0x0131] = "GeoFrame RAW Assist (rawpy 0.27.0)"  # Software
    ifd = exif.get_ifd(0x8769)                         # ExifIFD
    ifd[0xA002], ifd[0xA003] = out_w, out_h
    ifd.pop(0x927C, None)                              # MakerNote 破棄
    blob = exif.tobytes()
developed_image.save(dst, "JPEG", exif=blob, icc_profile=SRGB_ICC_BYTES, quality=92)
```

`exif.get_ifd(0x8769)` は**生きた参照**を返すため、`ifd` への代入/pop はそのまま `exif.tobytes()` に反映される
（`exif[0x8769] = ifd` の書き戻しは不要）。

### Wave 1-A への指示事項

- **piexif は requirements に追加しない。** Pillow のみで実装する（プラン §b どおり）。
- `tests/test_exif.py` は合成JPEGで十分だが、上記のとおり合成だけでは実機レイアウトを保証しないため、
  Wave 3 の目視チェック（§5-4）で実機経路を必ず確認すること。

---

## 副次的な実測所見（Wave 1-B / Wave 3 に影響）

sample/100MSDCF の全281枚を走査して判明した、プランの前提に関わる事実:

1. **`SubsecTimeOriginal` は 281枚すべてで存在しない**（`SubsecTime` / `SubsecTimeDigitized` も全て None）。
   ILCE-6400 はサブ秒を記録していない。→ **撮影時刻の分解能は1秒**。
   プラン §a-5 の連写判定「ギャップ中央値 ≤1.5s → `burst`」は、サブ秒が無い前提で設計し直す必要がある
   （同一秒を共有する写真は 281枚中 12枚のみ、最大クラスタは2枚）。
   プラン §a-2 の EXIF 抽出仕様は `SubsecTimeOriginal` を含むが、**常に None になる**ので
   ソートキーは `DateTimeOriginal` + ファイル名（連番）で安定化させること。
   代替として `OffsetTimeOriginal`(0x9011) = `'+09:00'` は全枚に存在する。

2. **`ExposureBiasValue` は 96.4% が非ゼロ**: `-0.3` が182枚、`+0.3` が89枚、`0.0` が10枚。
   このユーザーは常用で露出補正をかけており、**ブラケット撮影ではない**。
   → プラン §a-7 の RAW推薦ルール `maybe`: 「highlight_clip≥0.5% **or ExposureBias≠0**」は
   そのままだと**ほぼ全写真で発火して無意味**になる。
   → プラン §a-5 のグループ種別判定「ExposureBias ±0.3 パターン → `bracket`」も、
   このデータでは大量の誤検出を生む。**同一グループ内で複数の異なる bias 値が出現すること**を条件にする等、
   Wave 1-B で判定条件の見直しが必要（しきい値はモジュール定数なので Wave 3 で調整可能）。

3. **元JPEGに ICC プロファイルは付いていない**（281枚すべて）。
   → 現像時の sRGB ICC 付与は純粋な追加であり、元の色管理と衝突しない。

4. `Orientation` 分布は **1 が251枚 / 8 が30枚**（プラン §0 の「30枚」と一致）。
   `Make`=`SONY` / `Model`=`ILCE-6400` は281枚すべてで同一。
