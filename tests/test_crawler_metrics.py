from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobs import scheduler
from storage.crawler_metrics import current_crawler_pool_metrics, read_rows
from storage.post_writer import SQLitePostStore
from tools.operations.crawler_metrics import capture, render, validate


class CrawlerMetricsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "posts.db"
        self.metrics = Path(self.temp.name) / "crawler_history.csv"
        with SQLitePostStore(self.db) as store:
            store.init_schema()
            store.enqueue_crawler_candidate(
                post_id="id-1",
                source="lists",
                priority=10,
                list_create_time="",
                list_update_time="",
                list_comment_count=1,
                db_comment_count=0,
                reason="test",
                task_type="id_followup",
                commit=False,
            )
            store.enqueue_crawler_candidate(
                post_id="history-1",
                source="history",
                priority=40,
                list_create_time="",
                list_update_time="",
                list_comment_count=0,
                db_comment_count=0,
                reason="test",
                task_type="history_detail",
                commit=True,
            )

    def tearDown(self):
        self.temp.cleanup()

    def test_run_history_writes_standalone_exact_split(self):
        with patch.dict(os.environ, {"CRAWLER_METRICS_PATH": str(self.metrics)}):
            with SQLitePostStore(self.db) as store:
                store.record_crawler_run(
                    command="trickle-fill",
                    started_at="2026-10-10T00:00:00+08:00",
                    stats={
                        "source_calls": 2,
                        "selected": 2,
                        "written": 1,
                        "queue_inserted": 2,
                        "queue_delta_total": -1,
                        "comment_row_delta": 3,
                        "lane_id": "new",
                    },
                )

        rows = read_rows(self.metrics)
        id_rows = [row for row in rows if row["metric_name"] == "id_pending"]
        history_rows = [row for row in rows if row["metric_name"] == "history_pending"]
        self.assertEqual(len(id_rows), 1)
        self.assertEqual(id_rows[0]["value"], "1")
        self.assertEqual(history_rows[0]["value"], "1")
        raw = [row for row in rows if row["metric_name"] == "raw_stats"]
        self.assertEqual(json.loads(raw[0]["extra_json"])["lane_id"], "new")

    def test_capture_render_and_validate_are_source_backed(self):
        captured = capture(self.db, self.metrics)
        self.assertEqual(captured["method"], "exact")
        self.assertEqual(captured["id_pending"], 1)
        self.assertEqual(captured["history_pending"], 1)

        output = Path(self.temp.name) / "reports"
        rendered = render(self.metrics, output)
        self.assertGreater(rendered["rowCount"], 0)
        self.assertTrue((output / "latest.json").exists())
        self.assertTrue((output / "history.csv").exists())
        self.assertTrue((output / "latest.html").exists())
        self.assertTrue((output / "summary.md").exists())

        checked = validate(self.db, self.metrics)
        self.assertTrue(checked["ok"])
        self.assertEqual(checked["current"]["queue_pending_total"], 2)

    def test_current_metrics_keep_task_routes_separate(self):
        with SQLitePostStore(self.db) as store:
            metrics = current_crawler_pool_metrics(store.conn)

        self.assertEqual(metrics["queue_pending_total"], 2)
        self.assertEqual(metrics["id_pending"], 1)
        self.assertEqual(metrics["history_pending"], 1)
        self.assertEqual(metrics["queue_status"]["id_followup"]["pending"], 1)
        self.assertEqual(metrics["queue_status"]["history_detail"]["pending"], 1)

    def test_scheduler_heartbeat_writes_file_without_source_api(self):
        with (
            patch.object(scheduler, "DB_PATH", str(self.db)),
            patch.dict(os.environ, {"CRAWLER_METRICS_PATH": str(self.metrics)}),
            patch("builtins.print") as print_mock,
        ):
            self.assertTrue(
                scheduler.record_scheduler_metrics_snapshot(
                    state="idle",
                    job="trickle_fill",
                    detail="quota_window_locked",
                )
            )

        checked = validate(self.db, self.metrics)
        self.assertTrue(checked["ok"])
        self.assertEqual(checked["ledger"]["id_pending"], 1)
        logged = "\n".join(
            " ".join(str(argument) for argument in call.args)
            for call in print_mock.call_args_list
        )
        self.assertIn("[crawler-metrics]", logged)
        self.assertIn("id_ledger_total=", logged)
        self.assertIn("id_pending=1", logged)
        self.assertIn("history_pending=1", logged)


if __name__ == "__main__":
    unittest.main()
