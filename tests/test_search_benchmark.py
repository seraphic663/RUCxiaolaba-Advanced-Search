"""Tests for search benchmark result comparison."""

from __future__ import annotations

import unittest

from tools.benchmarks.benchmark_search_backends import (
    Timing,
    percentile_95,
)


class SearchBenchmarkTest(unittest.TestCase):
    def test_percentile_95_uses_nearest_rank(self) -> None:
        self.assertEqual(percentile_95([1, 2, 3, 4, 5]), 5)
        self.assertEqual(percentile_95([3]), 3)

    def test_timing_shape_accepts_samples(self) -> None:
        timing = Timing(10.0, 15.0, [9.0, 10.0, 15.0])
        self.assertEqual(timing.median_ms, 10.0)


if __name__ == "__main__":
    unittest.main()
