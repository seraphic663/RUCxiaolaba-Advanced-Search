from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from crawler.service import CrawlerService
from storage.post_writer import SQLitePostStore


class FakeClient:
    def __init__(self, pages, details):
        self.pages = pages
        self.details = details

    def list_page(self, endpoint, page):
        return {"list": self.pages.get(page, [])}, None

    def article(self, post_id):
        return self.details.get(str(post_id), (None, "not_found"))


class FailingClient(FakeClient):
    def list_page(self, endpoint, page):
        return None, "network down"


def detail(post_id, comments=0):
    return (
        {
            "community_id": "4",
            "title": f"post {post_id}",
            "detail": "body",
            "show_user_name": "user",
            "create_time": "2026-06-11 10:00:00",
            "count_comment": comments,
            "comment_list": [],
        },
        None,
    )


class CrawlerServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "posts.db"
        with SQLitePostStore(self.db) as store:
            store.init_schema()

    def tearDown(self):
        self.temp.cleanup()

    def service(self, client):
        service = CrawlerService(
            db_path=self.db,
            cookie="test",
            lock_timeout=2,
        )
        service.client = lambda: client
        return service

    def test_page_scan_inserts_new_post(self):
        client = FakeClient(
            {1: [{"id": "101", "count_comment": 0}], 2: []},
            {"101": detail("101")},
        )
        stats = self.service(client).scan_pages(
            command="sync-latest",
            endpoint="lists",
            start_page=1,
            pages=10,
            min_pages=1,
            stop_unchanged=5,
            max_details=0,
            dry_run=False,
            min_delay=0,
            max_delay=0,
        )
        self.assertEqual(stats["new"], 1)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute("select count(*) from posts").fetchone()[0], 1
            )
        finally:
            conn.close()

    def test_page_scan_stops_after_unchanged_threshold(self):
        with SQLitePostStore(self.db) as store:
            store.upsert_post(
                {
                    "id": "101",
                    "content": "existing",
                    "create_time": "2026-06-11 10:00:00",
                    "comment_count": 0,
                },
                [],
            )
        client = FakeClient(
            {
                1: [{"id": "101", "count_comment": 0}],
                2: [{"id": "101", "count_comment": 0}],
                3: [{"id": "102", "count_comment": 0}],
            },
            {},
        )
        stats = self.service(client).scan_pages(
            command="sync-latest",
            endpoint="lists",
            start_page=1,
            pages=10,
            min_pages=1,
            stop_unchanged=1,
            max_details=0,
            dry_run=False,
            min_delay=0,
            max_delay=0,
        )
        self.assertEqual(stats["pages"], 1)
        self.assertEqual(stats["unchanged"], 1)
        self.assertEqual(stats["details"], 0)

    def test_page_scan_reports_total_network_failure(self):
        with self.assertRaisesRegex(
            RuntimeError, "failed before reading any page"
        ):
            self.service(FailingClient({}, {})).scan_pages(
                command="sync-latest",
                endpoint="lists",
                start_page=1,
                pages=2,
                min_pages=1,
                stop_unchanged=1,
                max_details=0,
                dry_run=True,
                min_delay=0,
                max_delay=0,
            )


if __name__ == "__main__":
    unittest.main()
