import unittest
from nandwash.fsmap import encode_dev, is_free


class FsmapHelpers(unittest.TestCase):
    def test_encode_dev_matches_kernel(self):
        self.assertEqual(encode_dev(8, 1), 2049)            # /dev/sda1, seen live in GETFSMAP output
        self.assertEqual(encode_dev(259, 300), (300 & 0xFF) | (259 << 8) | ((300 & ~0xFF) << 12))

    def test_is_free_needs_one_whole_extent(self):
        ext = [(0, 10), (20, 10), (100, 1000)]
        self.assertTrue(is_free(ext, 0, 10))
        self.assertTrue(is_free(ext, 100, 1000))
        self.assertFalse(is_free(ext, 5, 10))               # runs past the end of the extent
        self.assertFalse(is_free(ext, 10, 10))              # sits in the hole between extents
        self.assertFalse(is_free(ext, 95, 10))              # straddles a hole and an extent
        self.assertFalse(is_free(ext, 1100, 1))             # past everything
        self.assertFalse(is_free([], 0, 1))


if __name__ == "__main__":
    unittest.main()
