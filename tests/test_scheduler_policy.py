from __future__ import annotations

import unittest
from datetime import datetime, timezone, timedelta

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

    def test_list1_and_list2_share_hourly_cadence(self):
        self.assertEqual(scheduler.NEW_DISCOVER_INTERVAL, 3600)
        self.assertEqual(scheduler.ACTIVE_DISCOVER_INTERVAL, 3600)
        self.assertEqual(scheduler.ACTIVE_DISCOVER_OFFSET, 1800)

    def test_new_detail_has_day_and_night_cadence(self):
        tz = timezone(timedelta(hours=8))
        self.assertEqual(
            scheduler.detail_trickle_interval(datetime(2026, 8, 21, 4, 30, tzinfo=tz)),
            scheduler.NIGHT_TRICKLE_INTERVAL,
        )
        self.assertEqual(
            scheduler.detail_trickle_interval(datetime(2026, 8, 21, 12, 0, tzinfo=tz)),
            scheduler.DAY_TRICKLE_INTERVAL,
        )

    def test_new_and_old_detail_use_independent_release_profiles(self):
        tz = timezone(timedelta(hours=8))
        night = datetime(2026, 8, 21, 4, 30, tzinfo=tz)
        self.assertEqual(
            scheduler.detail_quota_release_fraction(night, lane_id="new"),
            0.75,
        )
        self.assertEqual(
            scheduler.detail_quota_release_fraction(night, lane_id="old"),
            0.05,
        )


if __name__ == "__main__":
    unittest.main()
