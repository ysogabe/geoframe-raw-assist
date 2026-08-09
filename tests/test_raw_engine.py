import unittest

from local_raw.engine import RawDevelopmentError, develop_raw


class RawEngineValidationTests(unittest.TestCase):
    def test_rejects_path_traversal(self):
        with self.assertRaises(RawDevelopmentError):
            develop_raw("../DSC02943", "nightscape")

    def test_rejects_unknown_preset(self):
        with self.assertRaises(RawDevelopmentError):
            develop_raw("DSC02943", "unknown")

    def test_rejects_out_of_range_size(self):
        with self.assertRaises(RawDevelopmentError):
            develop_raw("DSC02943", "nightscape", max_edge=800)


if __name__ == "__main__":
    unittest.main()
