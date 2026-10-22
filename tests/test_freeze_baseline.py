"""Baseline contracts that do not require the external Sony photographs."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, TiffImagePlugin

from scripts import freeze_baseline as freeze


class BaselineTest(unittest.TestCase):
    def test_metrics_match_frozen_png_inputs(self):
        fixture = json.loads((freeze.FIXTURES / 'metrics.json').read_text())
        for record in fixture['images']:
            with self.subTest(input=record['input']):
                with Image.open(freeze.FIXTURES / record['input']) as image:
                    baseline = freeze.dc._normalise_rgb(image)
                transformed = np.clip(baseline * np.float32(1.2), 0, 1)
                for gate in (False, True):
                    actual = freeze.dc._image_metrics(baseline, transformed, record['exif'], gate)
                    expected = record['metrics'][str(gate)]
                    self.assertEqual(actual.keys(), expected.keys())
                    for key in actual:
                        if isinstance(actual[key], (int, float)):
                            self.assertAlmostEqual(actual[key], expected[key], delta=1e-6, msg=key)
                        else:
                            self.assertEqual(actual[key], expected[key])
                self.assertEqual(freeze.dc._star_measurements(baseline, record['exif']), record['star_measurements'])
        for record in fixture['pareto']:
            self.assertEqual(freeze.dc.pareto_front(record['points']), record['indices'])

    def test_objective_series_can_replay_without_raw_or_rng(self):
        fixture = json.loads((freeze.FIXTURES / 'candidate_series.json').read_text())
        for item in fixture['objective_inputs']:
            actual = freeze.dc._metric_objectives(
                item['metrics'], item['style'], item['vector'], item['target'],
                item['base_vector'], item['star_reference'], item['guard_penalty'])
            np.testing.assert_allclose(actual, item['objectives_before_safety_penalty'],
                                       atol=1e-12, rtol=1e-12)
            self.assertEqual(freeze.dc._safe_vector(item['vector']), item['safe_vector'])

    def test_gps_values_compared_in_memory_but_never_saved_or_hashed(self):
        data = Image.Exif()
        data[34853] = {2: tuple(TiffImagePlugin.IFDRational(v, 1) for v in (1, 2, 3)),
                       29: '2000:01:01'}
        source = Image.Exif()
        source.load(data.tobytes())
        inherited = Image.Exif()
        inherited.load(data.tobytes())
        freeze.verify_gps_inheritance(source, inherited)
        gps = freeze.exif_inventory(source)['GPS']
        self.assertEqual(set(gps), {'present', 'tag_ids', 'tags'})
        for tag in gps['tags'].values():
            self.assertEqual(set(tag), {'type', 'count'})
        # Same metadata, different value: inventory cannot detect it; direct
        # comparison must fail without printing the private values.
        inherited.get_ifd(34853)[2] = (4, 5, 6)
        self.assertEqual(gps, freeze.exif_inventory(inherited)['GPS'])
        with self.assertRaisesRegex(AssertionError, '^GPS inheritance changed$'):
            freeze.verify_gps_inheritance(source, inherited)

    def test_bracket_summary_matches_png_reference(self):
        path = freeze.FIXTURES / 'bracket.json'
        self.assertLess(path.stat().st_size, 200 * 1024)
        for group in json.loads(path.read_text())['groups']:
            self.assertNotIn('merged_float32', group)
            frames = []
            for name in group['inputs']:
                with Image.open(freeze.FIXTURES / name) as image:
                    frames.append(np.array(image, dtype='float32') / 255.)
            aligned, ok = freeze.bracket._align_frames(frames)
            merged = freeze.bracket._merge_frames(aligned)
            self.assertEqual(ok, group['aligned'])
            output = freeze.FIXTURES / group['output']
            self.assertEqual(freeze.sha(output), group['reference']['sha256'])
            with Image.open(output) as image:
                reference = np.array(image, dtype='float32') / 255.
                self.assertEqual(list(image.size), group['reference']['dimensions'])
            np.testing.assert_allclose(merged, reference, atol=0.5 / 255. + 1e-6, rtol=0)
            for key, actual in (('min', merged.min(axis=(0, 1))),
                                ('max', merged.max(axis=(0, 1)))):
                np.testing.assert_allclose(actual, group['float32_stats_rgb'][key], atol=1e-6)
            self.assertAlmostEqual(freeze.cv2.PSNR(merged, reference, R=1.),
                                   group['quantization_psnr_db'], delta=1e-4)

    def test_private_exif_is_always_digest_only(self):
        for tag, value in ((2, (1.2, 3.4, 5.6)), (18, 'private-location'),
                           (29, 'private-time'), (37500, b'private-maker-note')):
            record = freeze.describe_tag(tag, value, private=True)
            self.assertEqual(set(record), {'type', 'count', 'sha256'})
            self.assertEqual(len(record['sha256']), 64)
        self.assertNotIn('value', freeze.describe_tag(270, 'private-location'))
        first = freeze.describe_tag(2, TiffImagePlugin.IFDRational(6, 4), True)
        second = freeze.describe_tag(2, TiffImagePlugin.IFDRational(3, 2), True)
        self.assertEqual(first, second)

    def test_privacy_audit_rejects_location_and_plain_gps(self):
        freeze.WORK.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=freeze.WORK) as directory:
            path = Path(directory)
            with patch.object(freeze, 'FIXTURES', path):
                for payload in ({'nested': [{'latitude': 1.2}]}, {'GPS': {'1': 'private'}}):
                    freeze.dump(path / 'analysis.json', payload)
                    with self.assertRaises(ValueError):
                        freeze.audit_privacy()
                (path / 'analysis.json').unlink()
                original = json.loads((freeze.ROOT / 'tests/fixtures/baseline/exif_tags.json').read_text())[:1]
                for field in ('sha256', 'value', 'unexpected'):
                    payload = copy.deepcopy(original)
                    gps = payload[0]['source']['GPS']
                    next(iter(gps['tags'].values()))[field] = 'must-not-be-stored'
                    freeze.dump(path / 'exif_tags.json', payload)
                    with self.assertRaises(AssertionError):
                        freeze.audit_privacy()
                payload = copy.deepcopy(original)
                payload[0]['source']['GPS']['sha256'] = 'must-not-be-stored'
                freeze.dump(path / 'exif_tags.json', payload)
                with self.assertRaises(AssertionError):
                    freeze.audit_privacy()

    def test_committed_fixture_privacy(self):
        freeze.audit_privacy()
        for path in freeze.FIXTURES.iterdir():
            self.assertIn(path.suffix, ('.md', '.json', '.png'))
            if path.suffix == '.png':
                self.assertLess(path.stat().st_size, 32 * 1024)


if __name__ == '__main__':
    unittest.main()
