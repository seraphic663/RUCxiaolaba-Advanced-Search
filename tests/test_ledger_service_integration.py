from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crawler.id_ledger import mark_detail_finished, record_list_page, set_ledger_state
from crawler.service import CrawlerService
from storage.post_writer import SQLitePostStore


class FakeLedgerServiceClient:
    def __init__(self, pages, details):
        self.pages = pages
        self.details = details

    def list_page(self, endpoint, page):
        return {"list": self.pages.get(page, [])}, None

    def article(self, post_id):
        return self.details.get(str(post_id), (None, "not_found"))


def detail_payload(post_id: str):
    return (
        {
            "community_id": "4",
            "title": f"post {post_id}",
            "detail": "body",
            "show_user_name": "user",
            "create_time": "2026-08-12 00:00:00",
            "count_comment": 0,
            "comment_list": [],
        },
        None,
    )


class LedgerServiceIntegrationTest(unittest.TestCase):
    def test_new_lists2_event_reopens_one_existing_queue_row(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "posts.db"
            with SQLitePostStore(db_path) as store:
                store.init_schema()
                post, comments = CrawlerService(
                    db_path=db_path,
                    cookie="test",
                    lock_timeout=2,
                ).fetch_detail_with_error(
                    FakeLedgerServiceClient({}, {"1": detail_payload("1")}),
                    "1",
                )[0]
                store.upsert_post(post, comments)
                article = {
                    "id": "1",
                    "create_time": "2026-08-12 00:00:00",
                    "update_time": "2026-08-12 01:00:00",
                    "count_comment": 0,
                }
                record_list_page(
                    store.conn,
                    run_id="lists",
                    endpoint="lists",
                    page=1,
                    articles=[article],
                )
                mark_detail_finished(store.conn, "1", status="succeeded")
                record_list_page(
                    store.conn,
                    run_id="lists2-baseline",
                    endpoint="lists2",
                    page=1,
                    articles=[article],
                    baseline=True,
                )
                set_ledger_state(store.conn, "lists2_baseline_ready", "1")
                store.conn.commit()

            service = CrawlerService(db_path=db_path, cookie="test", lock_timeout=2)
            service.client = lambda: FakeLedgerServiceClient(
                {
                    1: [
                        {
                            "id": "1",
                            "create_time": "2026-08-12 00:00:00",
                            "update_time": "2026-08-12 02:00:00",
                            "count_comment": 0,
                        }
                    ],
                    2: [],
                },
                {},
            )
            stats = service.discover_queue(
                command="discover-active",
                endpoint="lists2",
                since="2026-08-12 00:00:00",
                max_pages=2,
                old_page_threshold=2,
                min_pages=1,
                no_action_page_threshold=1,
                min_delay=0,
                max_delay=0,
            )
            self.assertEqual(stats["ledger_actionable"], 1)
            with SQLitePostStore(db_path) as store:
                queue = store.conn.execute(
                    "select priority,reason,status from crawler_queue where post_id='1'"
                ).fetchone()
                self.assertEqual(dict(queue), {"priority": 0, "reason": "active_event", "status": "pending"})
                events = store.conn.execute(
                    "select count(*) from list2_observation_log where post_id='1'"
                ).fetchone()[0]
            self.assertEqual(events, 2)


if __name__ == "__main__":
    unittest.main()
