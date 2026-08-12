from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from crawler.service import CrawlerService
from storage.post_writer import SQLitePostStore


class BootstrapClient:
    def __init__(self, pages, quota_pages=()):
        self.pages = pages
        self.quota_pages = set(quota_pages)
        self.calls = []

    def list_page(self, endpoint, page):
        self.calls.append(page)
        if page in self.quota_pages:
            return None, "source_quota_budget_exhausted"
        return {"list": self.pages.get(page, [])}, None

    def article(self, post_id):
        return None, "not_found"


def article(post_id: int, comments: int = 0) -> dict:
    return {
        "id": str(post_id),
        "create_time": f"2026-08-12 00:{post_id:02d}:00",
        "update_time": f"2026-08-12 00:{post_id:02d}:00",
        "count_comment": comments,
    }


class BootstrapQueueTest(unittest.TestCase):
    def test_bootstrap_records_only_ledger_and_appends_detail_queue_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "posts.db"
            with SQLitePostStore(db_path) as store:
                store.init_schema()

            pages = {
                page: [article(page)]
                for page in range(1, 21)
            }
            service = CrawlerService(
                db_path=db_path,
                cookie="test",
                lock_timeout=2,
            )
            service.client = lambda: BootstrapClient(pages)
            stats = service.discover_queue(
                command="bootstrap-latest",
                endpoint="lists",
                since="1970-01-01 00:00:00",
                max_pages=20,
                old_page_threshold=99,
                min_pages=20,
                no_action_page_threshold=0,
                dry_run=False,
                write_stubs=False,
                bootstrap=True,
                min_delay=0,
                max_delay=0,
            )

            self.assertEqual(stats["pages"], 20)
            self.assertTrue(stats["bootstrap_complete"])
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                self.assertEqual(conn.execute("select count(*) from posts").fetchone()[0], 0)
                self.assertEqual(
                    conn.execute("select count(*) from post_id_ledger").fetchone()[0],
                    20,
                )
                first = conn.execute(
                    """
                    select post_id,first_seen_source,first_seen_page,
                           first_seen_at,discovery_order
                    from post_id_ledger order by discovery_order limit 1
                    """
                ).fetchone()
                self.assertEqual(first["post_id"], "1")
                self.assertEqual(first["first_seen_source"], "lists")
                self.assertEqual(first["first_seen_page"], 1)
                self.assertTrue(first["first_seen_at"])
                self.assertEqual(first["discovery_order"], 1)
                queue = conn.execute(
                    "select post_id from crawler_queue order by queue_order"
                ).fetchall()
                self.assertEqual([row[0] for row in queue], [str(i) for i in range(1, 21)])
                state = conn.execute(
                    "select value from ledger_state where key='lists_bootstrap_complete'"
                ).fetchone()
                self.assertEqual(state[0], "1")

            service.client = lambda: BootstrapClient({1: [article(21)], 2: []})
            follow_up = service.discover_queue(
                command="discover-latest",
                endpoint="lists",
                since="1970-01-01 00:00:00",
                max_pages=2,
                old_page_threshold=99,
                min_pages=1,
                no_action_page_threshold=0,
                dry_run=False,
                write_stubs=False,
                min_delay=0,
                max_delay=0,
            )
            self.assertEqual(follow_up["ledger_new_ids"], 1)
            with sqlite3.connect(db_path) as conn:
                order = conn.execute(
                    "select queue_order from crawler_queue where post_id='21'"
                ).fetchone()[0]
                self.assertEqual(order, 21)

    def test_bootstrap_resumes_after_quota_without_repeating_completed_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "posts.db"
            with SQLitePostStore(db_path) as store:
                store.init_schema()

            pages = {page: [article(page)] for page in range(1, 21)}
            first_client = BootstrapClient(pages, quota_pages={13})
            first_service = CrawlerService(
                db_path=db_path,
                cookie="test",
                lock_timeout=2,
            )
            first_service.client = lambda: first_client
            first = first_service.discover_queue(
                command="bootstrap-latest",
                endpoint="lists",
                since="1970-01-01 00:00:00",
                max_pages=20,
                old_page_threshold=99,
                min_pages=20,
                no_action_page_threshold=0,
                dry_run=False,
                write_stubs=False,
                bootstrap=True,
                min_delay=0,
                max_delay=0,
            )
            self.assertTrue(first["quota_stop"])
            self.assertEqual(first_client.calls, list(range(1, 14)))

            with sqlite3.connect(db_path) as conn:
                self.assertEqual(
                    conn.execute(
                        "select value from ledger_state where key='lists_bootstrap_next_page'"
                    ).fetchone()[0],
                    "13",
                )
                self.assertIsNone(
                    conn.execute(
                        "select value from ledger_state where key='lists_bootstrap_complete'"
                    ).fetchone()
                )

            second_client = BootstrapClient(pages)
            second_service = CrawlerService(
                db_path=db_path,
                cookie="test",
                lock_timeout=2,
            )
            second_service.client = lambda: second_client
            second = second_service.discover_queue(
                command="bootstrap-latest",
                endpoint="lists",
                since="1970-01-01 00:00:00",
                max_pages=20,
                old_page_threshold=99,
                min_pages=20,
                no_action_page_threshold=0,
                dry_run=False,
                write_stubs=False,
                bootstrap=True,
                min_delay=0,
                max_delay=0,
            )
            self.assertTrue(second["bootstrap_complete"])
            self.assertEqual(second_client.calls, list(range(13, 21)))
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(
                    conn.execute(
                        "select value from ledger_state where key='lists_bootstrap_next_page'"
                    ).fetchone()[0],
                    "21",
                )
                self.assertEqual(
                    conn.execute(
                        "select value from ledger_state where key='lists_bootstrap_complete'"
                    ).fetchone()[0],
                    "1",
                )


if __name__ == "__main__":
    unittest.main()
