"""Issue #108 手動検証。GPS はメモリ内で比較し、件数だけを出力する。

手順（リポジトリ直下、Pillow 必須）:
  python3 scripts/check_exif_inherit.py --prepare
  cargo test --manifest-path src-tauri/Cargo.toml real_camera_app1 -- --ignored --nocapture
  python3 scripts/check_exif_inherit.py --baseline /path/to/tests/fixtures/baseline/exif_tags.json

実機入力は読み取り専用。派生 JPEG は gitignore 対象の work/exif-108/rust/ のみ。
自動テストからは実行しない。GPS 値や GPS を含む差分を表示・保存しない。
"""
import argparse
import hashlib
import json
import struct
from fractions import Fraction
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work/exif-108/rust"
SOURCE = Path("/Users/yoshio/works/60-misc/40-raw-assistant/work/raw")
IFDS = {34665, 34853, 40965}
PUBLIC_TAGS = {256, 257, 258, 259, 262, 271, 272, 274, 277, 282, 283, 296,
               305, 306, 513, 514, 529, 530, 531, 33434, 33437, 34850, 34855,
               36864, 36867, 36868, 37121, 37122, 37377, 37378, 37379, 37380,
               37381, 37383, 37384, 37385, 37386, 40960, 40961, 40962, 40963,
               41486, 41487, 41488, 41728, 41729, 41985, 41986, 41987, 41988,
               41989, 41990, 41991, 41992, 41993, 41994, 42034, 42035, 42036}


def raw_ifds(path):
    with Image.open(path) as image:
        tiff = image.info["exif"][6:]
    endian = "<" if tiff[:2] == b"II" else ">"

    def u32(p):
        return struct.unpack_from(endian + "I", tiff, p)[0]

    def read(p):
        n = struct.unpack_from(endian + "H", tiff, p)[0]
        tags = {}
        for i in range(n):
            q = p + 2 + i * 12
            tag, kind, count = struct.unpack_from(endian + "HHI", tiff, q)
            size = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1,
                    8: 2, 9: 4, 10: 8, 11: 4, 12: 8}[kind] * count
            offset = q + 8 if size <= 4 else u32(q + 8)
            tags[tag] = (kind, count, tiff[offset:offset + size])
        return tags, u32(p + 2 + n * 12), tiff[p:p + 2 + n * 12 + 4]

    zero, next_ifd, _ = read(u32(4))
    exif = read(struct.unpack(endian + "I", zero[34665][2])[0])[0]
    gps = read(struct.unpack(endian + "I", zero[34853][2])[0])
    interop = read(struct.unpack(endian + "I", exif[40965][2])[0])[0] if 40965 in exif else {}
    return zero, exif, gps, interop, next_ifd


def jsonable(value):
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if hasattr(value, "numerator") and hasattr(value, "denominator") and not isinstance(value, int):
        fraction = Fraction(value.numerator, value.denominator)
        return {"numerator": fraction.numerator, "denominator": fraction.denominator}
    if isinstance(value, (list, tuple)):
        return [jsonable(x) for x in value]
    return value


def describe(tag, value, gps=False):
    result = {"type": type(value).__name__,
              "count": len(value) if isinstance(value, (bytes, str, list, tuple)) else 1}
    if gps:
        return result
    if tag in PUBLIC_TAGS:
        result["value"] = jsonable(value)
    else:
        encoded = json.dumps(jsonable(value), sort_keys=True, ensure_ascii=False,
                             allow_nan=False, separators=(",", ":")).encode()
        result["sha256"] = hashlib.sha256(encoded).hexdigest()
    return result


def inventory(path):
    with Image.open(path) as image:
        exif = image.getexif()
        def tags(items, gps=False):
            return {str(k): describe(k, v, gps) for k, v in items.items() if k not in IFDS}
        result = {"IFD0": tags(exif)}
        for tag, name in [(34665, "Exif"), (34853, "GPS"), (40965, "Interop")]:
            sub = exif.get_ifd(tag)
            result[name] = {"present": bool(sub), "tags": tags(sub, name == "GPS")}
            result[name]["tag_ids"] = sorted(sub)
        return result


def compare(baseline_path):
    baseline = {r["input"]: r for r in json.loads(baseline_path.read_text())}
    paths = sorted(SOURCE.glob("DSC074*.JPG"))
    checks = {name: 0 for name in ["GPS IFD 全タグ・実体バイト一致", "撮影情報一致",
              "UserComment 保持", "InteropIFD 保持", "PrintImageMatching 保持",
              "YCbCrPositioning 保持", "MakerNote 参照なし", "Orientation 1", "寸法 80x40",
              "IFD1 参照なし", "Python 基準 GPS 構造一致", "Python 基準 撮影・付加情報一致"]}
    for path in paths:
        output = WORK / path.name
        a, b = raw_ifds(path), raw_ifds(output)
        checks["GPS IFD 全タグ・実体バイト一致"] += a[2] == b[2]
        shoot0 = [0x10f, 0x110, 0x132]
        shoot_exif = [0x829a, 0x829d, 0x8827, 0x9003, 0x9004, 0x9010, 0x9011, 0x9012, 0x920a, 0xa434]
        checks["撮影情報一致"] += all(x.get(k) == y.get(k) for keys, x, y in [(shoot0, a[0], b[0]), (shoot_exif, a[1], b[1])] for k in keys)
        for label, index, tag in [("UserComment 保持", 1, 0x9286), ("PrintImageMatching 保持", 0, 0xc4a5), ("YCbCrPositioning 保持", 0, 0x213)]:
            checks[label] += tag in b[index] and a[index].get(tag) == b[index][tag]
        checks["InteropIFD 保持"] += bool(a[3]) and a[3] == b[3]
        checks["MakerNote 参照なし"] += 0x927c not in b[1]
        checks["IFD1 参照なし"] += b[4] == 0
        inv = inventory(output)
        checks["Orientation 1"] += inv["IFD0"]["274"]["value"] == 1
        checks["寸法 80x40"] += all(inv["Exif"]["tags"][str(k)]["value"] == v for k, v in [(40962, 80), (40963, 40)])
        expected = baseline[path.name]["inherited"]
        checks["Python 基準 GPS 構造一致"] += inv["GPS"] == expected["GPS"]
        # Software はエンジン名、寸法は手動合成の実寸に変更。IFD ポインタは比較対象外。
        same = True
        for section, ignored in [("IFD0", {"305"}), ("Exif", {"40962", "40963"}), ("Interop", set())]:
            x = inv[section] if section == "IFD0" else inv[section]["tags"]
            y = expected[section] if section == "IFD0" else expected[section]["tags"]
            same &= {k: v for k, v in x.items() if k not in ignored} == {k: v for k, v in y.items() if k not in ignored}
        checks["Python 基準 撮影・付加情報一致"] += same
    print(f"対象: {len(paths)} 件")
    print("| 比較項目 | 一致・要件達成 | 不一致 |")
    print("|---|---:|---:|")
    for label, count in checks.items():
        print(f"| {label} | {count} | {len(paths) - count} |")
    return len(paths) == 11 and all(n == 11 for n in checks.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--baseline", type=Path, default=ROOT / "tests/fixtures/baseline/exif_tags.json")
    args = parser.parse_args()
    if args.prepare:
        WORK.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (80, 40), (40, 80, 120)).save(WORK / "synthetic.jpg")
    else:
        raise SystemExit(0 if compare(args.baseline) else 1)


if __name__ == "__main__":
    main()
