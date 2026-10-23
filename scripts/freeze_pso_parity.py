"""#86: Python 純関数の追加参照値。凍結済み #107 baseline は変更しない。

選定の参照値（tests/fixtures/pso-selection.json）は凍結 JSON（candidate_series.json）と
評価用の純関数の写し（scripts/evaluation/candidate_metrics.py）だけで作る。アプリ本体
（local_raw）・rawpy・lensfunpy は使わない:
  python scripts/freeze_pso_parity.py --check   # 再計算して一致を確かめる（書かない。ずれたら終了コード 1）
  python scripts/freeze_pso_parity.py           # 書き出す（意図して更新するときだけ）

旧経路（アーカイブ、明示 opt-in）:
  python scripts/freeze_pso_parity.py --real-images
旧 Python の half デコードで既定の読み取り専用入力から work/pso-parity/ にメタデータなしの
float32 RGB と EXIF の露光・F値・ISO だけを保存する。GPS は取得・保存しない。
local_raw/ が残る checkout と requirements-raw.txt の環境が前提（docs/evaluation-paths.md）。
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from evaluation import candidate_metrics as cm  # noqa: E402
from evaluation import legacy  # noqa: E402

FIXTURES = ROOT / 'tests/fixtures/baseline'
STYLES = ('natural', 'stars', 'foreground', 'low_noise', 'social')


SELECTION = ROOT / 'tests/fixtures/pso-selection.json'


def render(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(value))


def selection_payload():
    series = json.loads((FIXTURES / 'candidate_series.json').read_text())
    base = series['expected']['candidates'][0]['metrics']
    rows = []
    for index, row in enumerate(series['evaluations']):
        m = row['metrics']
        for style in STYLES:
            rows.append({'evaluation': index, 'style': style,
                         'guard': cm._guard_failure(style, m, base),
                         'score': cm._score(m, style, base),
                         'reasons': cm._reasons(style, m, base, True)})
    # 全ての guard 分岐、境界の前後、理由の切り詰め・フォールバック。
    edges = []
    for style, changes in [
        ('natural', {'highlight_clip': 0.025}),
        ('natural', {'noise_flat': 0.0025}),
        ('natural', {'halo': 0.01875}),
        ('natural', {'color_saturation_clip': 0.0375}),
        ('natural', {'source_deviation': 0.125}),
        ('natural', {'highlight_clip': 0.1}),
        ('natural', {'highlight_clip': 0.099999}),
        ('natural', {'shadow_clip': 0.1}),
        ('low_noise', {'noise_flat': 0.000001}),
        ('low_noise', {'noise_flat': 0.0}),
        ('foreground', {'shadow_mean': 0.004999}),
        ('foreground', {'shadow_mean': 0.005}),
        ('stars', {'star_count': 5, 'star_halo': 1., 'star_color_saturation': 1.}),
        ('other', {'highlight_clip': 1., 'halo': 1.}),
        ('social', {'detail': 1., 'tonal_range': 1., 'shadow_clip': 0.}),
    ]:
        b = {key: 0 for key in base}
        m = {**b, **changes}
        edges.append({'style': style, 'metrics': m, 'base': b,
                      'guard': cm._guard_failure(style, m, b),
                      'score': cm._score(m, style, b),
                      'reasons': cm._reasons(style, m, b, True)})
    return {'series': rows, 'edges': edges}


def freeze_selection():
    dump(SELECTION, selection_payload())


def check_selection():
    """書かずに再計算して比べる。一致すれば 0、ずれていれば 1。"""
    if SELECTION.read_text() != render(selection_payload()):
        print(f'{SELECTION.relative_to(ROOT)} が再計算と一致しません', file=sys.stderr)
        return 1
    print('pso-selection: ok')
    return 0


def freeze_real():
    feature = '--real-images（旧 rawpy half デコード）'
    dc = legacy.import_module('local_raw.development_candidates', feature)
    engine = legacy.import_module('local_raw.engine', feature)
    read_capture_exif = legacy.import_module('local_raw.exif', feature).read_capture_exif
    resolve_adjustments = legacy.import_module('local_raw.presets', feature).resolve_adjustments
    source = Path(__file__).resolve().parents[1] / 'work' / 'raw'
    output = ROOT / 'work/pso-parity'
    output.mkdir(parents=True, exist_ok=True)
    cases = json.loads((FIXTURES / 'candidates.json').read_text())['records']
    records = []
    for row in cases:
        capture = row['case']['capture_id']
        arw = next(p for p in source.iterdir() if p.stem == capture and p.suffix.lower() == '.arw')
        jpg = next(p for p in source.iterdir() if p.stem == capture and p.suffix.lower() == '.jpg')
        # read_capture_exif は露光等だけの許可リストを返す。保存は更に 3 キーに絞る。
        capture_exif = read_capture_exif(jpg)
        exif = {k: capture_exif.get(k) for k in ('exposure_time', 'f_number', 'iso')}
        base = resolve_adjustments(row['case']['preset'])
        with dc.open_raw_half_decoder(arw, base, exif) as decode:
            iso = engine._coerce_iso(getattr(decode, 'iso', None)) or engine._coerce_iso(exif.get('iso'))
            evaluator = dc._Evaluator(dc.DecodeCache(decode), exif, base, star_gate=False, iso=iso)
            image = evaluator.image
            gray = np.sum(image * np.array([0.2126, 0.7152, 0.0722], dtype=np.float32), axis=2)
            mask = gray <= np.quantile(gray, dc.SHADOW_QUANTILE)
            image.astype('<f4').tofile(output / f'{capture}.f32')
            mask.astype('uint8').tofile(output / f'{capture}.mask')
            star = dc.detect_star_scene(image, exif)
            assert star['is_star_scene'] == row['result']['star_scene']['is_star_scene']
            records.append({'capture_id': capture, 'width': image.shape[1], 'height': image.shape[0],
                            'exif': exif, 'star': star,
                            'frozen_gate': row['result']['star_scene']['is_star_scene'],
                            'metrics': {str(gate): dc._image_metrics(image, image, exif, gate, mask)
                                        for gate in (False, True)}})
        print(capture, flush=True)
    dump(output / 'reference.json', records)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--check', action='store_true', help='再計算して一致を確かめる（書かない）')
    group.add_argument('--real-images', action='store_true', help='旧経路（アーカイブ）: 旧 Python の half デコード参照値')
    args = parser.parse_args()
    if args.check:
        raise SystemExit(check_selection())
    if args.real_images:
        try:
            freeze_real()
        except legacy.LegacyUnavailable as error:
            raise SystemExit(str(error)) from None
    else:
        freeze_selection()
