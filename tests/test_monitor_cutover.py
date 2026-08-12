from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crawler.id_ledger import ledger_state, set_ledger_state
from jobs.scheduler import (
    enable_monitor_jobs,
    enable_remaining_monitor_jobs,
    prepare_monitor_cutover,
    run_job,
)
from storage.post_writer import SQLitePostStore


class MonitorCutoverTest(unittest.TestCase):
    def enqueue(self, store, post_id: str, priority: int) -> None:
        store.enqueue_crawler_candidate(
            post_id=post_id,
            source="lists" if priority > 0 else "lists2",
            priority=priority,
            list_create_time="2026-08-12 10:00:00",
            list_update_time="2026-08-12 10:00:00",
            list_comment_count=1,
            db_comment_count=0,
            reason="test",
        )

    def test_cutover_pauses_old_coverage_but_keeps_refresh_and_new_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "posts.db"
            with SQLitePostStore(db_path) as store:
                store.init_schema()
                self.enqueue(store, "old-coverage", 10)
                self.enqueue(store, "old-refresh", 0)
                self.enqueue(store, "old-urgent", -1)
                cutoff = store.conn.execute(
                    "select max(queue_order) from crawler_queue"
                ).fetchone()[0]
                set_ledger_state(store.conn, "monitor_old_coverage_paused", "1")
                set_ledger_state(store.conn, "monitor_queue_order_cutoff", cutoff)
                self.enqueue(store, "new-coverage", 10)

                rows = store.next_crawler_queue_items(10, refresh_limit=10)

            self.assertEqual(
                [row["post_id"] for row in rows],
                ["old-urgent", "old-refresh", "new-coverage"],
            )

    def test_prepare_monitor_cutover_persists_the_queue_boundary_once(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "posts.db"
            with SQLitePostStore(db_path) as store:
                store.init_schema()
                self.enqueue(store, "old-coverage", 10)

            with patch("jobs.scheduler.DB_PATH", str(db_path)):
                first = prepare_monitor_cutover()
                with SQLitePostStore(db_path) as store:
                    self.enqueue(store, "new-coverage", 10)
                second = prepare_monitor_cutover()

            self.assertEqual(first["queue_order_cutoff"], 1)
            self.assertEqual(second["queue_order_cutoff"], 1)
            with SQLitePostStore(db_path) as store:
                self.assertEqual(
                    ledger_state(store.conn, "monitor_old_coverage_paused"),
                    "1",
                )
                self.assertEqual(
                    ledger_state(store.conn, "monitor_queue_order_cutoff"),
                    "1",
                )

    def test_quota_window_skip_is_deferred_to_the_next_release(self):
        with patch(
            "jobs.scheduler.prepare_job",
            return_value=(
                None,
                "quota_window_locked_until=2026-08-13T11:00:00+08:00",
            ),
        ):
            result = run_job("discover_new")

        self.assertTrue(result.succeeded)
        self.assertEqual(result.error_kind, "quota_window_locked")
        self.assertGreater(result.deferred_until, 0)

    def test_list2_is_not_scheduled_until_list1_seed_request_finishes(self):
        next_run = {}
        intervals = {}
        enable_monitor_jobs(next_run, intervals, 100.0)
        self.assertEqual(set(next_run), {"discover_new"})

        enable_remaining_monitor_jobs(next_run, intervals, 200.0)
        self.assertEqual(
            set(next_run),
            {"trickle_fill", "discover_new", "discover_active"},
        )

        restarted = {}
        restarted_intervals = {}
        enable_remaining_monitor_jobs(restarted, restarted_intervals, 300.0)
        self.assertEqual(set(restarted), set(next_run))


if __name__ == "__main__":
    unittest.main()
