from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from crawler.cookie_pool import CookieLaneSpec
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

    def test_regular_list_jobs_use_effective_signal_caps(self):
        latest = scheduler.job_args("discover_new")
        active = scheduler.job_args("discover_active")
        self.assertEqual(
            int(latest[latest.index("--max-pages") + 1]),
            20,
        )
        self.assertEqual(
            int(active[active.index("--max-pages") + 1]),
            10,
        )
        self.assertEqual(int(latest[latest.index("--min-pages") + 1]), 1)
        self.assertEqual(int(active[active.index("--min-pages") + 1]), 2)
        for args in (latest, active):
            self.assertEqual(
                int(args[args.index("--no-action-page-threshold") + 1]),
                1,
            )

    def test_deep_list_window_reads_the_full_bounded_page_range(self):
        tz = timezone(timedelta(hours=8))
        deep = datetime(2026, 8, 21, 5, 0, tzinfo=tz)
        with patch.object(scheduler, "beijing_now", return_value=deep):
            latest = scheduler.monitoring_list_args("discover_new")
            active = scheduler.monitoring_list_args("discover_active")
        for args in (latest, active):
            self.assertEqual(int(args[args.index("--max-pages") + 1]), 10)
            self.assertEqual(int(args[args.index("--min-pages") + 1]), 10)
            self.assertEqual(
                int(args[args.index("--no-action-page-threshold") + 1]),
                0,
            )

    def test_monitoring_list_jobs_use_rolling_cutoffs_and_stop_after_old_page(self):
        tz = timezone(timedelta(hours=8))
        at = datetime(2026, 8, 21, 12, 0, tzinfo=tz)
        latest = scheduler.monitoring_list_args("discover_new", at=at)
        active = scheduler.monitoring_list_args("discover_active", at=at)

        self.assertEqual(
            latest[latest.index("--since") + 1],
            (
                at - timedelta(hours=scheduler.TRICKLE_NEW_LOOKBACK_HOURS)
            ).strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.assertEqual(
            active[active.index("--since") + 1],
            (
                at - timedelta(hours=scheduler.TRICKLE_FRESH_COVERAGE_HOURS)
            ).strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.assertEqual(
            int(latest[latest.index("--old-page-threshold") + 1]),
            scheduler.LIST_OLD_PAGE_THRESHOLD,
        )
        self.assertEqual(
            int(active[active.index("--old-page-threshold") + 1]),
            scheduler.LIST_OLD_PAGE_THRESHOLD,
        )

    def test_list1_and_list2_share_hourly_cadence(self):
        self.assertEqual(scheduler.NEW_DISCOVER_INTERVAL, 3600)
        self.assertEqual(scheduler.ACTIVE_DISCOVER_INTERVAL, 3600)
        self.assertEqual(scheduler.ACTIVE_DISCOVER_OFFSET, 1800)
        self.assertFalse(
            scheduler.list_deep_scan_active(
                datetime(2026, 8, 21, 2, 59, tzinfo=timezone(timedelta(hours=8)))
            )
        )
        self.assertTrue(
            scheduler.list_deep_scan_active(
                datetime(2026, 8, 21, 3, 0, tzinfo=timezone(timedelta(hours=8)))
            )
        )

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

    def test_legacy_id_release_profile_is_shared_for_compatibility(self):
        tz = timezone(timedelta(hours=8))
        night = datetime(2026, 8, 21, 4, 30, tzinfo=tz)
        self.assertEqual(
            scheduler.detail_quota_release_fraction(night, lane_id="new"),
            0.75,
        )
        self.assertEqual(
            scheduler.detail_quota_release_fraction(night, lane_id="old"),
            0.75,
        )

    def test_fixed_detail_windows_top_up_actual_lane_counters(self):
        tz = timezone(timedelta(hours=8))
        before = datetime(2026, 8, 21, 1, 59, tzinfo=tz)
        morning = datetime(2026, 8, 21, 2, 0, tzinfo=tz)
        daytime = datetime(2026, 8, 21, 12, 0, tzinfo=tz)
        evening = datetime(2026, 8, 21, 20, 0, tzinfo=tz)
        self.assertIsNone(scheduler.active_detail_window(before))
        self.assertEqual(scheduler.active_detail_window(morning)[0], "morning")
        self.assertEqual(scheduler.active_detail_window(morning)[3], 300)
        self.assertEqual(scheduler.active_detail_window(daytime)[0], "morning")
        self.assertEqual(scheduler.active_detail_window(evening)[0], "evening")
        self.assertEqual(scheduler.active_detail_window(evening)[3], 500)

        quota = {
            "cookie_lanes": {
                "new": {"detail_calls": 25},
                "old": {"detail_calls": 24},
            }
        }
        self.assertEqual(
            scheduler.detail_window_remaining(quota, "new", morning),
            275,
        )
        self.assertEqual(
            scheduler.detail_window_remaining(quota, "old", morning),
            276,
        )
        self.assertEqual(
            scheduler.detail_window_remaining(quota, "new", evening),
            475,
        )
        self.assertEqual(
            scheduler.next_detail_window_start(daytime),
            daytime.replace(hour=20, minute=0, second=0, microsecond=0),
        )
        self.assertEqual(
            scheduler.next_detail_window_start(before),
            morning.replace(second=0, microsecond=0),
        )

    def test_fixed_detail_window_ignores_legacy_release_gate_at_two(self):
        tz = timezone(timedelta(hours=8))
        morning = datetime(2026, 8, 21, 2, 0, tzinfo=tz)
        specs = (
            CookieLaneSpec(
                "new",
                Path("new.txt"),
                {"new_list": 0, "active_list": 0, "detail": 500},
                task_types=("list_new", "list_active", "id_followup"),
            ),
            CookieLaneSpec(
                "old",
                Path("old.txt"),
                {"new_list": 0, "active_list": 0, "detail": 500},
                task_types=("list_new", "list_active", "id_followup"),
            ),
        )
        quota = {"cookie_lanes": {"new": {"detail_calls": 0}}}
        with (
            patch.object(scheduler, "cookie_pool_specs", return_value=specs),
            patch.object(scheduler, "beijing_now", return_value=morning),
        ):
            self.assertEqual(
                scheduler.remaining_budget("detail", quota, lane_id="new"),
                300,
            )

    def test_regular_list1_ceiling_is_independent_of_deep_scans_and_list2(self):
        code = """
import json
from datetime import datetime
from jobs import scheduler as s
regular = s.monitoring_list_args('discover_new', at=datetime.fromisoformat('2026-10-27T12:00:00+08:00'))
active = s.monitoring_list_args('discover_active', at=datetime.fromisoformat('2026-10-27T12:00:00+08:00'))
deep_new = s.monitoring_list_args('discover_new', at=datetime.fromisoformat('2026-10-27T05:00:00+08:00'))
deep_active = s.monitoring_list_args('discover_active', at=datetime.fromisoformat('2026-10-27T05:00:00+08:00'))
def arg(args, name):
    return int(args[args.index(name)+1])
pages = [arg(a, '--max-pages') for a in (regular, active, deep_new, deep_active)]
print(json.dumps(pages + [arg(regular, '--no-action-page-threshold')]))
"""
        for configured in (0, 10, 20, 50):
            with self.subTest(configured=configured):
                env = os.environ.copy()
                for name in (
                    "CRAWLER_DISCOVER_LATEST_PAGES",
                    "CRAWLER_DISCOVER_ACTIVE_PAGES",
                    "CRAWLER_DISCOVER_LATEST_REGULAR_PAGES",
                    "CRAWLER_DISCOVER_ACTIVE_REGULAR_PAGES",
                ):
                    env[name] = str(configured)
                result = subprocess.run(
                    [sys.executable, "-B", "-c", code], env=env,
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True, text=True, check=True,
                )
                bounded = max(1, configured)
                self.assertEqual(
                    json.loads(result.stdout),
                    [min(bounded, 20), min(bounded, 10), min(bounded, 10),
                     min(bounded, 10), 1],
                )

    def test_new_windows_top_up_persisted_counts_without_resetting_them(self):
        tz = timezone(timedelta(hours=8))
        quota = {
            "cookie_lanes": {
                "new": {"detail_calls": 250},
                "old": {"detail_calls": 250},
            }
        }
        before_evening = datetime(2026, 10, 27, 19, 59, tzinfo=tz)
        evening = datetime(2026, 10, 27, 20, 0, tzinfo=tz)
        midnight = datetime(2026, 10, 28, 0, 0, tzinfo=tz)
        for lane in ("new", "old"):
            self.assertEqual(
                scheduler.detail_window_remaining(quota, lane, before_evening), 50
            )
            self.assertEqual(
                scheduler.detail_window_remaining(quota, lane, evening), 250
            )
            quota["cookie_lanes"][lane]["detail_calls"] = 300
            self.assertEqual(
                scheduler.detail_window_remaining(quota, lane, evening), 200
            )
            self.assertEqual(
                scheduler.detail_window_remaining(quota, lane, midnight), 0
            )

    def test_equal_lanes_rotate_list_observations_by_same_kind_usage(self):
        specs = (
            CookieLaneSpec(
                "new",
                Path("new.txt"),
                {"new_list": 0, "active_list": 0, "detail": 500},
                task_types=("list_new", "list_active", "id_followup"),
            ),
            CookieLaneSpec(
                "old",
                Path("old.txt"),
                {"new_list": 0, "active_list": 0, "detail": 500},
                task_types=("list_new", "list_active", "id_followup"),
            ),
        )
        quota = {
            "cookie_lanes": {
                "new": {"new_list_calls": 3, "active_list_calls": 0},
                "old": {"new_list_calls": 0, "active_list_calls": 3},
            }
        }
        with (
            patch.object(scheduler, "cookie_pool_specs", return_value=specs),
            patch.object(scheduler, "load_quota", return_value=quota),
            patch.object(scheduler, "_lane_is_paused", return_value=False),
        ):
            self.assertEqual(scheduler.job_lane_id("discover_new"), "old")
            self.assertEqual(scheduler.job_lane_id("discover_active"), "new")


if __name__ == "__main__":
    unittest.main()
