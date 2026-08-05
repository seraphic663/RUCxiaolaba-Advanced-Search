import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from crawler.cli import build_parser
from jobs.scheduler import (
    classify_error,
    job_args,
    job_budget_kind,
    next_job_run,
    next_quota_release,
    parse_release_steps,
    planned_job_calls,
    quota_release_fraction,
    quota_source_calls,
    record_failed_crawler_run,
    run_job,
    remaining_budget,
    select_next_job,
)


class CLIContractTest(unittest.TestCase):
    def test_new_and_canonical_latest_commands_match(self):
        parser = build_parser()
        old = parser.parse_args(["new"])
        new = parser.parse_args(["sync-latest"])
        self.assertEqual(old.endpoint, new.endpoint)
        self.assertIs(old.func, new.func)

    def test_phase1_and_scan_id_range_are_both_supported(self):
        parser = build_parser()
        old = parser.parse_args(["phase1", "--start-id", "1", "--end-id", "2"])
        new = parser.parse_args(["scan-id-range", "--start-id", "1", "--end-id", "2"])
        self.assertIs(old.func, new.func)
        self.assertEqual((old.start_id, old.end_id), (1, 2))

    def test_scheduler_uses_valid_canonical_commands(self):
        parser = build_parser()
        for job_name in (
            "new",
            "refresh",
            "backfill",
            "phase1",
            "discover_new",
            "discover_active",
            "trickle_fill",
        ):
            parsed = parser.parse_args(
                [
                    *job_args(job_name),
                    "--db-path",
                    "data/posts.db",
                    "--config",
                    "data/config.txt",
                ]
            )
            self.assertTrue(callable(parsed.func))

    def test_scheduler_classifies_crawler_fuses(self):
        self.assertEqual(
            classify_error("[crawler] error: rate_limited:今天刷的太久了"),
            "rate_limited",
        )
        self.assertEqual(
            classify_error("[crawler] error: cookie_expired"),
            "cookie_expired",
        )
        self.assertEqual(classify_error("[crawler] error: not_found"), "")

    def test_scheduler_records_failed_child_process(self):
        child = Mock(returncode=1, stderr="[crawler] error: cookie_expired\n")
        with (
            patch(
                "jobs.scheduler.prepare_job",
                return_value=(["plan-gaps"], ""),
            ),
            patch("jobs.scheduler.subprocess.run", return_value=child),
            patch("jobs.scheduler.record_failed_crawler_run") as record,
        ):
            result = run_job("plan_gaps")
        self.assertFalse(result.succeeded)
        self.assertEqual(result.error_kind, "cookie_expired")
        self.assertEqual(result.returncode, 1)
        record.assert_called_once()
        call = record.call_args.kwargs
        self.assertEqual(call["name"], "plan_gaps")
        self.assertEqual(call["source_calls"], 0)
        self.assertEqual(call["error_kind"], "cookie_expired")

    def test_failed_child_history_is_durable(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "posts.db"
            record_failed_crawler_run(
                name="trickle_fill",
                started_at="2026-07-29T23:00:00+08:00",
                source_calls=3,
                returncode=1,
                error_kind="rate_limited",
                stderr="rate_limited:test",
                db_path=db_path,
            )
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    """
                    select command,source_calls,errors,rate_limited,stats_json
                    from crawler_run_history
                    """
                ).fetchone()
            finally:
                conn.close()
        self.assertEqual(row["command"], "trickle-fill")
        self.assertEqual(row["source_calls"], 3)
        self.assertEqual(row["errors"], 1)
        self.assertEqual(row["rate_limited"], 1)
        stats = json.loads(row["stats_json"])
        self.assertTrue(stats["scheduler_failed"])
        self.assertEqual(stats["error_kind"], "rate_limited")

    def test_scheduler_budgets_source_call_types(self):
        self.assertEqual(job_budget_kind("discover_new"), "new_list")
        self.assertEqual(job_budget_kind("discover_active"), "active_list")
        self.assertEqual(job_budget_kind("plan_gaps"), "")
        self.assertEqual(job_budget_kind("trickle_fill"), "detail")
        self.assertEqual(job_budget_kind("probe_gaps"), "probe")
        self.assertEqual(
            planned_job_calls("discover_new", ["discover-latest", "--max-pages", "7"]),
            7,
        )
        self.assertEqual(planned_job_calls("plan_gaps", ["plan-gaps"]), 0)
        trickle_args = job_args("trickle_fill")
        self.assertLessEqual(int(trickle_args[trickle_args.index("--limit") + 1]), 12)
        self.assertLessEqual(
            int(trickle_args[trickle_args.index("--refresh-limit") + 1]),
            5,
        )

    def test_scheduler_parses_quota_release_steps(self):
        self.assertEqual(
            parse_release_steps("11=0.2,14:0.35,17:30=0.5,bad,23=1.2"),
            [(660, 0.2), (840, 0.35), (1050, 0.5), (1380, 1.0)],
        )

    def test_scheduler_releases_quota_in_stairs(self):
        china = timezone(timedelta(hours=8))
        self.assertEqual(
            quota_release_fraction(datetime(2026, 7, 10, 10, 59, tzinfo=china)),
            0.0,
        )
        self.assertEqual(
            quota_release_fraction(datetime(2026, 7, 10, 11, 0, tzinfo=china)),
            0.2,
        )
        self.assertEqual(
            quota_release_fraction(datetime(2026, 7, 10, 17, 0, tzinfo=china)),
            0.5,
        )
        self.assertEqual(
            quota_release_fraction(datetime(2026, 7, 10, 20, 0, tzinfo=china)),
            0.7,
        )
        self.assertEqual(
            quota_release_fraction(datetime(2026, 7, 10, 21, 0, tzinfo=china)),
            0.85,
        )
        self.assertEqual(
            quota_release_fraction(datetime(2026, 7, 10, 22, 0, tzinfo=china)),
            1.0,
        )
        self.assertEqual(
            next_quota_release(datetime(2026, 7, 10, 10, 30, tzinfo=china)).hour,
            11,
        )
        self.assertEqual(
            next_quota_release(datetime(2026, 7, 10, 11, 30, tzinfo=china)).hour,
            14,
        )

    def test_scheduler_prioritizes_overdue_details_then_active_then_new(self):
        next_run = {
            "discover_new": 10.0,
            "discover_active": 20.0,
            "trickle_fill": 30.0,
        }
        self.assertEqual(select_next_job(next_run, 40.0), "trickle_fill")
        self.assertEqual(
            select_next_job(
                {"discover_new": 10.0, "discover_active": 20.0},
                40.0,
            ),
            "discover_active",
        )
        self.assertEqual(select_next_job(next_run, 5.0), "discover_new")

    def test_scheduler_interval_is_measured_start_to_start(self):
        self.assertEqual(next_job_run(100.0, 340.0, 600.0), 700.0)
        self.assertEqual(next_job_run(100.0, 800.0, 600.0), 801.0)

    def test_scheduler_main_detail_budget_is_independent_from_admin(self):
        with (
            patch("jobs.scheduler.quota_release_fraction", return_value=1.0),
            patch("jobs.scheduler.daily_budget", return_value=450),
            patch("jobs.scheduler.DAILY_ADMIN_DETAIL_BUDGET", 10),
        ):
            self.assertEqual(remaining_budget("detail", {"detail_calls": 0}), 450)
            self.assertEqual(
                remaining_budget(
                    "detail",
                    {"detail_calls": 10, "admin_detail_calls": 10},
                ),
                440,
            )
        self.assertEqual(
            quota_source_calls(
                {
                    "new_list_calls": 1,
                    "active_list_calls": 2,
                    "detail_calls": 3,
                    "probe_calls": 4,
                    "admin_preview_calls": 5,
                    "admin_detail_calls": 6,
                }
            ),
            21,
        )


if __name__ == "__main__":
    unittest.main()
