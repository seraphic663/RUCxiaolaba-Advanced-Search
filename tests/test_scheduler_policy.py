from __future__ import annotations

import unittest

from jobs import scheduler


class SchedulerPolicyTest(unittest.TestCase):
    def test_bootstrap_scans_exactly_twenty_list1_pages_without_stubs(self):
        args = scheduler.job_args("bootstrap_new")
        self.assertIn("--bootstrap", args)
        self.assertIn("--no-write-stubs", args)
        self.assertEqual(
            int(args[args.index("--max-pages") + 1]),
            20,
        )
        self.assertEqual(
            int(args[args.index("--min-pages") + 1]),
            20,
        )

    def test_list_jobs_use_low_rate_page_caps_and_two_page_floor(self):
        latest = scheduler.job_args("discover_new")
        active = scheduler.job_args("discover_active")
        self.assertEqual(
            int(latest[latest.index("--max-pages") + 1]),
            5,
        )
        self.assertEqual(
            int(active[active.index("--max-pages") + 1]),
            5,
        )
        for args in (latest, active):
            self.assertEqual(int(args[args.index("--min-pages") + 1]), 2)
            self.assertEqual(
                int(args[args.index("--no-action-page-threshold") + 1]),
                2,
            )

    def test_list1_and_list2_have_separate_default_cadence(self):
        self.assertEqual(scheduler.NEW_DISCOVER_INTERVAL, 3600)
        self.assertEqual(scheduler.ACTIVE_DISCOVER_INTERVAL, 1800)


if __name__ == "__main__":
    unittest.main()
