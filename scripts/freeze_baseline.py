#!/usr/bin/env python3
"""Freeze the existing Python implementation before its removal (Issue #107).

Real photographs and their derivatives are only written below work/baseline.
The repository fixtures contain numeric JSON and generated, metadata-free PNGs.

Retained after the Python app removal (#90 / #117), with no local_raw / rawpy / lensfunpy:

- ``--audit-only``: privacy audit of the committed fixtures.
- ``--verify-frozen-root DIR``: verify the frozen rawpy images listed in
  ``rawpy_images.json`` below DIR (sha256, RGB sha256, dimensions; missing or
  modified images fail).

Regeneration (full run or ``--sections``) re-runs the old Python engine and is an
explicit archival opt-in: ``--legacy-generate`` on a checkout that still has
``local_raw/`` and the requirements-raw.txt environment (docs/development/evaluation-paths.md).
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import platform
from fractions import Fraction
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2
import numpy as np
from PIL import Image, TiffImagePlugin

sys.path.insert(0, str(ROOT / 'scripts'))
from evaluation import frozen, legacy  # noqa: E402

# Old-engine modules, loaded only by load_legacy() for --legacy-generate.
analysis = bracket = config = dc = export = build_inherited_exif = compare = evaluate = None

DEFAULT_SOURCE = Path('/Users/yoshio/works/60-misc/40-raw-assistant/work/raw')
FIXTURES = ROOT / 'tests/fixtures/baseline'
WORK = ROOT / 'work/baseline'
# EXIF values outside this camera/technical allowlist are hashed too: arbitrary
# private tags, comments, image descriptions and serial numbers cannot leak.
PUBLIC_TAGS = {256, 257, 258, 259, 262, 271, 272, 274, 277, 282, 283, 296,
               305, 306, 513, 514, 529, 530, 531, 33434, 33437, 34850, 34855,
               36864, 36867, 36868, 37121, 37122, 37377, 37378, 37379, 37380,
               37381, 37383, 37384, 37385, 37386, 40960, 40961, 40962, 40963,
               41486, 41487, 41488, 41728, 41729, 41985, 41986, 41987, 41988,
               41989, 41990, 41991, 41992, 41993, 41994, 42034, 42035, 42036}
IFDS = {34665: 'Exif', 34853: 'GPS', 40965: 'Interop'}
FIXED_TIME = '2000-01-01T00:00:00+00:00'


def load_legacy():
    """Import the old Python engine for regeneration (archival, explicit opt-in only)."""
    global analysis, bracket, config, dc, export, build_inherited_exif, compare, evaluate
    feature = 'freeze_baseline regeneration'
    analysis = legacy.import_module('local_raw.analysis', feature)
    bracket = legacy.import_module('local_raw.bracket', feature)
    config = legacy.import_module('local_raw.config', feature)
    dc = legacy.import_module('local_raw.development_candidates', feature)
    export = legacy.import_module('local_raw.export', feature)
    build_inherited_exif = legacy.import_module('local_raw.exif', feature).build_inherited_exif
    from scripts import compare_engines as compare_module, evaluate_candidates as evaluate_module
    compare, evaluate = compare_module, evaluate_module


def jsonable(value):
    if isinstance(value, bytes):
        return {'bytes_hex': value.hex()}
    if isinstance(value, TiffImagePlugin.IFDRational):
        fraction = Fraction(value.numerator, value.denominator)
        return {'numerator': fraction.numerator, 'denominator': fraction.denominator}
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [jsonable(v) for v in value]
    return value


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jsonable(value), ensure_ascii=False, indent=2,
                               sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def describe_tag(tag, value, private=False, *, gps=False):
    result = {'type': type(value).__name__,
              'count': len(value) if isinstance(value, (bytes, str, list, tuple)) else 1}
    if gps:
        return result
    if private or tag not in PUBLIC_TAGS:
        encoded = json.dumps(jsonable(value), sort_keys=True, ensure_ascii=False,
                             allow_nan=False, separators=(',', ':')).encode()
        result['sha256'] = hashlib.sha256(encoded).hexdigest()
    else:
        result['value'] = jsonable(value)
    return result


def exif_inventory(exif):
    def tags(items, private=False, gps=False):
        return {str(tag): describe_tag(tag, value, private, gps=gps) for tag, value in sorted(items.items())
                if tag not in IFDS}
    result = {'IFD0': tags(exif)}
    def visit(parent, tag, name):
        items = parent.get_ifd(tag) if hasattr(parent, 'get_ifd') else {}
        result[name] = {'present': tag in parent, 'tag_ids': sorted(items),
                        'tags': tags(items, gps=(name == 'GPS'))}
        if name == 'Exif' and 40965 in items:
            interop = exif.get_ifd(40965)
            result['Interop'] = {'present': True, 'tag_ids': sorted(interop), 'tags': tags(interop)}
    visit(exif, 34665, 'Exif')
    visit(exif, 34853, 'GPS')
    # Thumbnail bytes are deliberately excluded; record only their digest.
    thumbnail = exif.get_ifd(1)
    result['IFD1'] = tags(thumbnail, private=True)
    return result


def freeze_analysis(source):
    result = analysis.build_analysis(source_root=source, thumbs_root=WORK / 'thumbnails')
    result['generated_at'] = FIXED_TIME
    result[analysis.SIGNATURE_KEY] = analysis._source_signature(source)
    dump(FIXTURES / 'analysis.json', result)
    return result


def verify_gps_inheritance(source_exif, inherited_exif):
    """Compare locally in memory; never serialize GPS values or their hashes."""
    assert (34853 in source_exif) == (34853 in inherited_exif), 'GPS IFD presence changed'
    assert source_exif.get_ifd(34853) == inherited_exif.get_ifd(34853), 'GPS inheritance changed'


def freeze_exif(source):
    records = []
    for path in sorted(source.glob('*.JPG')):
        with Image.open(path) as image:
            source_exif = image.getexif()
            before = exif_inventory(source_exif)
        output = Image.Exif()
        output.load(build_inherited_exif(path, 6000, 4000))
        after = exif_inventory(output)
        verify_gps_inheritance(source_exif, output)
        assert '37500' not in after['Exif']['tags'], 'MakerNote retained'
        records.append({'input': path.name, 'output_dimensions': [6000, 4000],
                        'source': before, 'inherited': after})
    dump(FIXTURES / 'exif_tags.json', records)


def synthetic_images():
    y, x = np.indices((96, 96))
    gradient = np.stack([x * 255 // 95, y * 255 // 95, (x + y) * 255 // 190], axis=2).astype('uint8')
    stars = np.full((96, 96, 3), 7, dtype='uint8')
    for yy in range(8, 90, 13):
        for xx in range(7, 90, 17):
            stars[yy-1:yy+2, xx-1:xx+2] = (70, 85, 100)
            stars[yy, xx] = (255, 230, 190)
    checker = np.repeat(((((x // 4 + y // 4) % 2) * 255).astype('uint8'))[..., None], 3, axis=2)
    return {'gradient': gradient, 'stars': stars, 'checker': checker,
            'black': np.zeros_like(gradient), 'white': np.full_like(gradient, 255)}


def freeze_metrics():
    records = []
    exif = {'iso': 3200, 'f_number': 2.8, 'exposure_time': 20.0}
    for name, rgb in synthetic_images().items():
        path = FIXTURES / f'{name}.png'
        Image.fromarray(rgb).save(path)
        with Image.open(path) as image:
            baseline = dc._normalise_rgb(image)
        transformed = np.clip(baseline * np.float32(1.2), 0, 1)
        records.append({'input': path.name, 'sha256': sha(path), 'exif': exif,
                        'transform': {'multiply_float32': 1.2, 'clip': [0, 1]},
                        'star_measurements': dc._star_measurements(baseline, exif),
                        'star_ungated': dc._star_measurements(baseline, exif, evaluate_gate=False),
                        'metrics': {str(gate): dc._image_metrics(baseline, transformed, exif, gate)
                                    for gate in (False, True)}})
    points = [[0., 1., 1.], [1., 0., 1.], [1., 1., 0.], [2., 2., 2.], [0., 1., 1.]]
    dump(FIXTURES / 'metrics.json', {'images': records,
         'pareto': [{'points': points, 'indices': dc.pareto_front(points)},
                    {'points': [], 'indices': dc.pareto_front([])}]})


def freeze_rawpy(source):
    ctx = compare.RunContext(source, WORK, ROOT / 'unused-cli', 1600, legacy_rawpy=True)
    rows = []
    for capture in ('DSC07402', 'DSC07423'):
        for mode in ('preview', 'full'):
            print(f'rawpy {capture} {mode}', flush=True)
            row = compare.run_rawpy(ctx, capture, mode)
            if 'error' in row:
                raise RuntimeError(row['error'])
            path = Path(row['output'])
            with Image.open(path) as image:
                size = list(image.size)
                pixel_sha256 = hashlib.sha256(np.asarray(image.convert('RGB')).tobytes()).hexdigest()
            rows.append({'capture_id': capture, 'mode': mode, 'path': str(path.relative_to(ROOT)),
                         'sha256': sha(path), 'rgb8_sha256': pixel_sha256,
                         'dimensions': size, 'wall_s': row['wall_s']})
    dump(FIXTURES / 'rawpy_images.json', rows)
    return rows


def freeze_candidates(source):
    cases = evaluate.load_cases_file(ROOT / 'scripts/eval_cases/candidates_baseline.json')
    records, pending, series, objectives = [], [], [], []
    original_objectives = dc._metric_objectives
    def objectives_logged(metrics, style, vector, target, base_vector, star_reference, guard_penalty=0.):
        result = original_objectives(metrics, style, vector, target, base_vector, star_reference, guard_penalty)
        objectives.append({"metrics": copy.deepcopy(metrics), "style": style,
                           "vector": np.asarray(vector).copy(), "target": np.asarray(target).copy(),
                           "base_vector": np.asarray(base_vector).copy(), "star_reference": star_reference,
                           "guard_penalty": guard_penalty, "safe_vector": dc._safe_vector(vector),
                           "objectives_before_safety_penalty": result})
        return result
    original = dc._Evaluator.evaluate
    def evaluate_logged(self, adjustments, star_gate):
        metrics = original(self, adjustments, star_gate)
        series.append({'adjustments': copy.deepcopy(adjustments), 'star_gate': star_gate,
                       'metrics': copy.deepcopy(metrics)})
        return metrics
    for case in cases:
        if case['root'] != 'stars' or evaluate.find_source_file(source, case['capture_id'], ('ARW',)) is None:
            pending.append(case)
            continue
        print(f'candidates {case["capture_id"]}', flush=True)
        with patch.object(config, 'source_root', return_value=source):
            if not records:
                with patch.object(dc._Evaluator, 'evaluate', evaluate_logged), \
                     patch.object(dc, '_metric_objectives', objectives_logged):
                    result = dc.generate_development_candidates(case['capture_id'], case['preset'])
            else:
                result = dc.generate_development_candidates(case['capture_id'], case['preset'])
        records.append({'case': case, 'result': result})
    dump(FIXTURES / 'candidates.json', {'records': records, 'pending_missing_root': pending})
    dump(FIXTURES / 'candidate_series.json', {'case': records[0]['case'],
         'seed': records[0]['result']['seed'], 'budget': records[0]['result']['budget'],
         'evaluations': series, 'objective_inputs': objectives, 'expected': records[0]['result']})


def freeze_bracket():
    # Synthetic photographs only: no real-image pixels enter repository PNGs.
    base = synthetic_images()['gradient']
    base[20:40, 30:50] = (240, 90, 30)
    groups = []
    for index, shifts in enumerate((((0, 0), (0, 0), (0, 0)),
                                    ((0, 0), (2, -1), (-1, 2)),
                                    ((0, 0), (-2, 1), (1, -2)))):
        frames, names = [], []
        for i, (exposure, shift) in enumerate(zip((0.5, 1., 1.8), shifts)):
            rgb = np.roll(np.clip(base.astype('float32') * exposure, 0, 255).astype('uint8'),
                          shift=(shift[1], shift[0]), axis=(0, 1))
            name = f'bracket-{index}-{i}.png'
            Image.fromarray(rgb).save(FIXTURES / name)
            names.append(name)
            frames.append(rgb.astype('float32') / 255.)
        aligner = cv2.createAlignMTB(bracket._align_max_bits(frames[0].shape))
        gray = [cv2.cvtColor((frame * 255).astype('uint8'), cv2.COLOR_RGB2GRAY) for frame in frames]
        mtb = [list(aligner.calculateShift(gray[0], frame)) for frame in gray]
        aligned, ok = bracket._align_frames(frames)
        merged = bracket._merge_frames(aligned)
        # Pixels live only in the PNG; retain numeric summaries for float output.
        output = f'bracket-{index}-merged.png'
        quantized = np.round(merged * 255).astype('uint8')
        Image.fromarray(quantized).save(FIXTURES / output)
        mean, stddev = cv2.meanStdDev(merged)
        groups.append({'inputs': names, 'known_roll_xy': shifts, 'mtb_shifts_xy': mtb,
                       'aligned': ok, 'output': output,
                       'reference': {'sha256': sha(FIXTURES / output),
                                     'dimensions': [merged.shape[1], merged.shape[0]],
                                     'channels': 'RGB', 'dtype': 'uint8', 'psnr_peak': 255},
                       'float32_stats_rgb': {
                           'min': merged.min(axis=(0, 1)), 'max': merged.max(axis=(0, 1)),
                           'mean': mean.ravel(), 'stddev': stddev.ravel()},
                       'quantization_psnr_db': cv2.PSNR(merged, quantized.astype('float32') / 255., R=1.)})
    dump(FIXTURES / 'bracket.json', {'status': 'synthetic_only',
         'pending': '実写ブラケット3組の元RAWが利用可能なwork配下にない', 'groups': groups})


def normalize_manifest(value):
    if isinstance(value, dict):
        return {k: FIXED_TIME if k == 'created_at' else normalize_manifest(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize_manifest(v) for v in value]
    if isinstance(value, str) and value.startswith('/'):
        return Path(value).name
    return value


def freeze_manifest(source, analysis_result, rawpy_rows):
    full = next(row for row in rawpy_rows if row['mode'] == 'full')
    developed = ROOT / full['path']
    with patch.object(config, 'source_root', return_value=source), \
         patch.object(export, 'EXPORT_ROOT', WORK / 'export'), \
         patch.object(export, 'OUTPUT_ROOT', developed.parent), \
         patch.object(export, '_load_analysis', return_value=analysis_result):
        result = export.export_batch('baseline-107', [
            {'capture_id': full['capture_id'], 'variant': 'camera_jpg'},
            {'capture_id': full['capture_id'], 'variant': 'developed', 'developed_filename': developed.name}])
    manifest = json.loads((ROOT / result['manifest_path']).read_text())
    dump(FIXTURES / 'manifest.json', normalize_manifest(manifest))


def audit_privacy():
    """Fail closed: GPS tags have only type/count; other JSON has no location keys.

    This is a structural audit, not a substring search for unknown coordinates.
    Unknown EXIF and private tags are hashed by describe_tag before writing.
    """
    forbidden = {'gps', 'latitude', 'longitude', 'altitude', 'location', 'geolocation',
                 'gpslatitude', 'gpslongitude', 'gpsaltitude', '撮影地', '緯度', '経度'}
    def visit(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.casefold() in forbidden:
                    raise ValueError(f'location key at {path}/{key}')
                visit(child, f'{path}/{key}')
        elif isinstance(value, list):
            for i, child in enumerate(value):
                visit(child, f'{path}/{i}')
    count = gps_count = 0
    for path in sorted(FIXTURES.glob('*.json')):
        payload = json.loads(path.read_text())
        if path.name == 'exif_tags.json':
            for record in payload:
                for kind in ('source', 'inherited'):
                    gps = record[kind]['GPS']
                    assert set(gps) == {'present', 'tag_ids', 'tags'}, 'unexpected GPS IFD field'
                    assert gps['tag_ids'] == sorted(int(key) for key in gps['tags']), 'GPS tag IDs mismatch'
                    for tag in gps['tags'].values():
                        assert set(tag) == {'type', 'count'}, 'GPS value or digest leaked'
                        gps_count += 1
                    for name, ifd in record[kind].items():
                        tags = ifd if name in ('IFD0', 'IFD1') else ifd['tags']
                        for key, tag in tags.items():
                            if name in ('GPS', 'IFD1') or int(key) not in PUBLIC_TAGS:
                                assert 'value' not in tag, 'private EXIF value leaked'
        else:
            visit(payload, path.name)
        count += 1
    pngs = list(FIXTURES.glob('*.png'))
    for path in pngs:
        with Image.open(path) as image:
            assert not image.info and not image.getexif(), f'metadata in {path.name}'
    print(f'privacy audit: PASS ({count} JSON, {gps_count} GPS tags type/count-only, {len(pngs)} metadata-free synthetic PNG)', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--audit-only', action='store_true')
    parser.add_argument('--verify-frozen-root', type=Path,
                        help='Verify the frozen rawpy images of rawpy_images.json below this root (no app needed).')
    parser.add_argument('--legacy-generate', action='store_true',
                        help='Archival: re-run the old Python engine to regenerate fixtures (overwrites baseline JSON).')
    parser.add_argument('--sections', nargs='+', choices=('exif', 'bracket'),
                        help='Regenerate only the selected independent sections; keep full-run provenance.')
    args = parser.parse_args(argv)
    if args.audit_only or args.verify_frozen_root:
        audit_privacy()
        if args.verify_frozen_root:
            try:
                images = frozen.verify_manifest(FIXTURES / 'rawpy_images.json', args.verify_frozen_root.resolve())
            except frozen.FrozenArtifactError as error:
                print(str(error), file=sys.stderr)
                raise SystemExit(1) from None
            print(f'frozen rawpy images: PASS ({len(images)} verified)', flush=True)
        return
    if not args.legacy_generate:
        parser.error('regeneration re-runs the old Python engine: ' + legacy.PREREQUISITE
                     + ' (pass --legacy-generate; use --audit-only / --verify-frozen-root otherwise)')
    try:
        load_legacy()
    except legacy.LegacyUnavailable as error:
        parser.error(str(error))
    source = args.source_root.resolve()
    if (not args.sections or 'exif' in args.sections) and not source.is_dir():
        parser.error(f'missing read-only source: {source}')
    FIXTURES.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timings = {}
    def run(name, function):
        start = time.monotonic()
        result = function()
        timings[name] = round(time.monotonic() - start, 3)
        print(f'{name}: {timings[name]}s', flush=True)
        return result
    if args.sections:
        for section in dict.fromkeys(args.sections):
            run(section, (lambda: freeze_exif(source)) if section == 'exif' else freeze_bracket)
        audit_privacy()
        return
    result = run('analysis', lambda: freeze_analysis(source))
    run('exif', lambda: freeze_exif(source))
    run('metrics', freeze_metrics)
    rows = run('rawpy', lambda: freeze_rawpy(source))
    run('candidates', lambda: freeze_candidates(source))
    run('bracket', freeze_bracket)
    run('manifest', lambda: freeze_manifest(source, result, rows))
    dump(FIXTURES / 'provenance.json', {
        'source_root': str(source), 'source_files': [
            {'name': path.name, 'sha256': sha(path)} for path in sorted(source.iterdir())
            if path.suffix.lower() in ('.arw', '.jpg')],
        'python': platform.python_version(), 'platform': platform.platform(),
        'packages': {name: importlib.metadata.version(name) for name in
                     ('numpy', 'Pillow', 'rawpy', 'lensfunpy', 'opencv-python-headless')},
        'opencv_threads': cv2.getNumThreads(), 'timings_seconds': timings,
        'total_seconds': round(time.monotonic() - started, 3)})
    audit_privacy()


if __name__ == '__main__':
    main()
