from __future__ import annotations

import sqlite3
import unittest

from crawler.id_ledger import (
    ensure_ledger_schema,
    mark_detail_finished,
    mark_detail_started,
    record_list_page,
    set_ledger_state,
)


class IdLedgerTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_ledger_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def article(post_id: str, *, update: str = "2026-08-12 01:00:00", comments: int = 0):
        return {
            "id": post_id,
            "create_time": "2026-08-12 00:00:00",
            "update_time": update,
            "count_comment": comments,
            "count_star": 3,
            "count_trace": 1,
        }

    def test_rolling_back_page_also_rolls_back_events_order_and_checkpoint(self):
        self.conn.commit()
        articles = [self.article(str(i)) for i in range(20)]

        def record():
            record_list_page(self.conn, run_id="batch", endpoint="lists2", page=1, articles=articles)
            set_ledger_state(self.conn, "test_page_checkpoint", "1")

        record()
        self.conn.rollback()
        for table in ("post_id_ledger", "list2_observation_log", "ledger_state"):
            self.assertEqual(self.conn.execute(f"select count(*) from {table}").fetchone()[0], 0)
        record()
        orders = self.conn.execute("select discovery_order from post_id_ledger order by discovery_order")
        self.assertEqual([r[0] for r in orders], list(range(1, 21)))

    def test_first_written_detail_yield_belongs_to_latest_event_only(self):
        for count in (1, 2):
            record_list_page(self.conn, run_id="monitor", endpoint="lists2", page=count,
                             articles=[self.article("21", comments=count)])
        mark_detail_finished(self.conn, "21", status="blocked")
        mark_detail_finished(self.conn, "21", status="partial", added_comment_rows=0)
        mark_detail_finished(self.conn, "21", status="succeeded", added_comment_rows=3)
        record_list_page(self.conn, run_id="monitor-2", endpoint="lists2", page=12,
                         articles=[self.article("21", comments=5)])
        mark_detail_finished(self.conn, "21", status="succeeded", added_comment_rows=2)
        events = self.conn.execute(
            "select page, first_detail_new_comment_rows from list2_observation_log order by observation_id"
        )
        self.assertEqual([tuple(r) for r in events], [(1, None), (2, 0), (12, 2)])

    def test_list1_records_first_source_page_rank_and_stable_replay(self):
        articles = [self.article("10"), self.article("9", update="2026-08-12 00:30:00")]
        first = record_list_page(
            self.conn,
            run_id="lists-1",
            endpoint="lists",
            page=3,
            articles=articles,
        )
        self.assertEqual(first["new_ids"], 2)
        self.assertEqual(first["fresh_signals"], 2)
        self.assertEqual(first["actionable"], 2)
        self.assertEqual(first["source_create_time_min"], "2026-08-12 00:00:00")
        self.assertEqual(first["source_create_time_max"], "2026-08-12 00:00:00")
        row = self.conn.execute(
            """
            select first_seen_source,first_seen_page,first_seen_rank,
                   detail_status,needs_detail
            from post_id_ledger where post_id='9'
            """
        ).fetchone()
        self.assertEqual(
            dict(row),
            {
                "first_seen_source": "lists",
                "first_seen_page": 3,
                "first_seen_rank": 41,
                "detail_status": "queued",
                "needs_detail": 1,
            },
        )

        replay = record_list_page(
            self.conn,
            run_id="lists-2",
            endpoint="lists",
            page=3,
            articles=articles,
        )
        self.assertEqual(replay["new_ids"], 0)
        self.assertEqual(replay["fresh_signals"], 0)
        self.assertEqual(replay["actionable"], 0)
        self.assertTrue(replay["stable"])

    def test_lists2_baseline_then_new_event_is_actionable(self):
        article = self.article("20")
        record_list_page(
            self.conn,
            run_id="lists-1",
            endpoint="lists",
            page=1,
            articles=[article],
        )
        mark_detail_started(self.conn, "20")
        mark_detail_finished(
            self.conn,
            "20",
            status="succeeded",
            comment_count=0,
        )
        baseline = record_list_page(
            self.conn,
            run_id="lists2-baseline",
            endpoint="lists2",
            page=1,
            articles=[article],
            baseline=True,
        )
        self.assertEqual(baseline["actionable"], 0)
        self.assertEqual(baseline["fresh_signals"], 0)
        self.assertTrue(baseline["stable"])
        set_ledger_state(self.conn, "lists2_baseline_ready", "1")

        changed = record_list_page(
            self.conn,
            run_id="lists2-monitor",
            endpoint="lists2",
            page=1,
            articles=[self.article("20", update="2026-08-12 02:00:00")],
        )
        self.assertEqual(changed["actionable"], 1)
        self.assertEqual(changed["fresh_signals"], 1)
        self.assertEqual(changed["actionable_ids"], ["20"])
        self.assertFalse(changed["stable"])
        row = self.conn.execute(
            "select needs_detail,detail_status from post_id_ledger where post_id='20'"
        ).fetchone()
        self.assertEqual(dict(row), {"needs_detail": 1, "detail_status": "queued"})
        event = self.conn.execute(
            """
            select detail_attempted,detail_result
            from list2_observation_log where post_id='20'
            order by observation_id desc limit 1
            """
        ).fetchone()
        self.assertEqual(dict(event), {"detail_attempted": 0, "detail_result": ""})


if __name__ == "__main__":
    unittest.main()
