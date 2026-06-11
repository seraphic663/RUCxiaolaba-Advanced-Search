import unittest

from crawler.strategies.page_scan import PageScanProgress


class PageScanProgressTest(unittest.TestCase):
    def test_requires_both_minimum_pages_and_unchanged_threshold(self):
        progress = PageScanProgress()
        progress.page_read()
        progress.unchanged()
        progress.unchanged()
        self.assertFalse(progress.should_stop(min_pages=2, threshold=2))
        progress.page_read()
        self.assertTrue(progress.should_stop(min_pages=2, threshold=2))

    def test_change_resets_consecutive_counter(self):
        progress = PageScanProgress(pages=3, consecutive_unchanged=10)
        progress.changed()
        self.assertEqual(progress.consecutive_unchanged, 0)
        self.assertFalse(progress.should_stop(min_pages=1, threshold=1))


if __name__ == "__main__":
    unittest.main()
