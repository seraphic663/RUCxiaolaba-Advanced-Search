from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

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
            6,
        )
        self.assertEqual(
            int(active[active.index("--max-pages") + 1]),
            18,
        )
        self.assertEqual(int(latest[latest.index("--min-pages") + 1]), 1)
        self.assertEqual(int(active[active.index("--min-pages") + 1]), 3)
        self.assertIn("--active-coverage", active)
        for args in (latest, active):
            self.assertEqual(
                int(args[args.index("--no-action-page-threshold") + 1]),
                1,
            )

    def test_list_jobs_keep_early_stops_throughout_the_day(self):
        tz = timezone(timedelta(hours=8))
        for hour in (0, 2, 3, 5, 6, 12, 23):
            at = datetime(2026, 8, 21, hour, 0, tzinfo=tz)
            for job, maximum, minimum in (("discover_new", 6, 1), ("discover_active", 18, 3)):
                with self.subTest(hour=hour, job=job):
                    args = scheduler.monitoring_list_args(job, at=at)
                    self.assertEqual(int(args[args.index("--max-pages") + 1]), maximum)
                    self.assertEqual(int(args[args.index("--min-pages") + 1]), minimum)
                    self.assertEqual(int(args[args.index("--no-action-page-threshold") + 1]), 1)
                    self.assertEqual(int(args[args.index("--old-page-threshold") + 1]), scheduler.LIST_OLD_PAGE_THRESHOLD)

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

    def test_hourly_list1_and_three_hour_list2_are_staggered(self):
        self.assertEqual(scheduler.NEW_DISCOVER_INTERVAL, 3600)
        self.assertEqual(scheduler.ACTIVE_DISCOVER_INTERVAL, 10800)
        self.assertEqual(scheduler.ACTIVE_DISCOVER_OFFSET, 1800)

    def test_active_segments_keep_lane_date_cutoff_and_get_a_new_list1_deadline(self):
        active = scheduler.ActiveListRound("round123", 90.0, "old", "2026-11-24", "2026-11-21 01:00:00")
        with (
            patch.object(scheduler, "prepare_job", side_effect=lambda *_args, **_kwargs: (
                scheduler.job_args("discover_active"), ""
            )),
            patch.object(scheduler, "COOKIE_POOL_PATH", "pool.json"),
            patch.object(scheduler, "quota_counter_snapshot", return_value=("2026-11-24", 0)),
            patch.object(scheduler.subprocess, "run", side_effect=[
                Mock(returncode=scheduler.LIST1_DUE_EXIT, stderr=""),
                Mock(returncode=0, stderr=""),
            ]) as child,
            patch.object(scheduler, "record_failed_crawler_run") as failure,
        ):
            first = scheduler.run_job("discover_active", active_round=active, list1_due_at=100.0)
            resumed = scheduler.run_job("discover_active", active_round=active, list1_due_at=3700.0)
        self.assertTrue(first.succeeded)
        self.assertEqual(first.error_kind, "list1_due")
        self.assertTrue(resumed.succeeded)
        self.assertEqual(resumed.error_kind, "")
        for call, deadline in zip(child.call_args_list, ("100.0", "3700.0")):
            args = call.args[0]
            self.assertEqual(args[args.index("--since") + 1], active.since)
            self.assertEqual(args[args.index("--yield-at-monotonic") + 1], deadline)
            self.assertEqual(args[args.index("--active-round-id") + 1], active.round_id)
            self.assertEqual(call.kwargs["env"]["CRAWLER_COOKIE_POOL_LANE"], active.lane_id)
            self.assertEqual(call.kwargs["env"]["CRAWLER_AUTOMATIC_QUOTA_DATE"], active.task_date)
        failure.assert_not_called()

    def test_only_enabled_list1_child_gets_coverage_flag_and_error_is_recorded_once(self):
        with (
            patch.object(scheduler, "prepare_job", side_effect=lambda *_args, **_kwargs: (
                scheduler.job_args("discover_new"), ""
            )),
            patch.object(scheduler, "job_lane_id", return_value=""),
            patch.object(scheduler, "quota_counter_snapshot", return_value=("2026-11-24", 0)),
            patch.object(scheduler.subprocess, "run", side_effect=[
                Mock(returncode=0, stderr=""), Mock(returncode=scheduler.LIST_SCAN_ERROR_EXIT, stderr=""),
            ]) as child,
            patch.object(scheduler, "record_failed_crawler_run") as failure,
        ):
            scheduler.run_job("discover_new")
            monitored = scheduler.run_job("discover_new", list1_coverage=True)
        self.assertNotIn("--list1-coverage", child.call_args_list[0].args[0])
        self.assertIn("--list1-coverage", child.call_args_list[1].args[0])
        self.assertFalse(monitored.succeeded)
        self.assertEqual(monitored.error_kind, "list_scan_error")
        failure.assert_not_called()

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
        before = datetime(2026, 8, 21, 0, 59, tzinfo=tz)
        morning = datetime(2026, 8, 21, 1, 0, tzinfo=tz)
        daytime = datetime(2026, 8, 21, 12, 0, tzinfo=tz)
        evening = datetime(2026, 8, 21, 19, 0, tzinfo=tz)
        self.assertIsNone(scheduler.active_detail_window(before))
        self.assertEqual(scheduler.active_detail_window(morning)[0], "morning")
        self.assertEqual(scheduler.active_detail_window(morning)[3], 400)
        self.assertEqual(scheduler.active_detail_window(daytime)[0], "morning")
        self.assertEqual(scheduler.active_detail_window(evening)[0], "evening")
        self.assertEqual(scheduler.active_detail_window(evening)[3], 600)

        quota = {
            "cookie_lanes": {
                "new": {"detail_calls": 25},
                "old": {"detail_calls": 24},
            }
        }
        self.assertEqual(
            scheduler.detail_window_remaining(quota, "new", morning),
            375,
        )
        self.assertEqual(
            scheduler.detail_window_remaining(quota, "old", morning),
            376,
        )
        self.assertEqual(
            scheduler.detail_window_remaining(quota, "new", evening),
            475,
        )
        self.assertEqual(
            scheduler.next_detail_window_start(daytime),
            daytime.replace(hour=19, minute=0, second=0, microsecond=0),
        )
        self.assertEqual(
            scheduler.next_detail_window_start(before),
            morning.replace(second=0, microsecond=0),
        )

    def test_fixed_detail_window_ignores_legacy_release_gate_at_one(self):
        tz = timezone(timedelta(hours=8))
        morning = datetime(2026, 8, 21, 1, 0, tzinfo=tz)
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
                400,
            )

    def test_list_page_caps_ignore_removed_deep_scan_configuration(self):
        code = """
import json
from datetime import datetime
from jobs import scheduler as s
regular = s.monitoring_list_args('discover_new', at=datetime.fromisoformat('2026-10-27T12:00:00+08:00'))
active = s.monitoring_list_args('discover_active', at=datetime.fromisoformat('2026-10-27T12:00:00+08:00'))
night_new = s.monitoring_list_args('discover_new', at=datetime.fromisoformat('2026-10-27T05:00:00+08:00'))
night_active = s.monitoring_list_args('discover_active', at=datetime.fromisoformat('2026-10-27T05:00:00+08:00'))
def arg(args, name):
    return int(args[args.index(name)+1])
pages = [arg(a, '--max-pages') for a in (regular, active, night_new, night_active)]
print(json.dumps(pages + [arg(regular, '--no-action-page-threshold')]))
"""
        for configured in (0, 10, 20, 50):
            with self.subTest(configured=configured):
                env = os.environ.copy()
                env.update({
                    "CRAWLER_DISCOVER_LATEST_PAGES": "1",
                    "CRAWLER_DISCOVER_ACTIVE_PAGES": "1",
                    "CRAWLER_LIST_DEEP_SCAN_START": "03:00",
                    "CRAWLER_LIST_DEEP_SCAN_END": "06:00",
                })
                for name in (
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
                    [min(bounded, 6), min(bounded, 18), min(bounded, 6),
                     min(bounded, 18), 1],
                )

    def test_new_windows_top_up_persisted_counts_without_resetting_them(self):
        tz = timezone(timedelta(hours=8))
        quota = {
            "cookie_lanes": {
                "new": {"detail_calls": 250},
                "old": {"detail_calls": 250},
            }
        }
        before_evening = datetime(2026, 10, 27, 18, 59, tzinfo=tz)
        evening = datetime(2026, 10, 27, 19, 0, tzinfo=tz)
        midnight = datetime(2026, 10, 28, 0, 0, tzinfo=tz)
        for lane in ("new", "old"):
            self.assertEqual(
                scheduler.detail_window_remaining(quota, lane, before_evening), 150
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
