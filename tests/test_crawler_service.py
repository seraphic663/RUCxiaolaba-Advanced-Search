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


class RateLimitedClient(FakeClient):
    def article(self, post_id):
        return None, "rate_limited:今天刷的太久了，休息一下吧"


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

    def test_discover_latest_queues_missing_posts_until_old_pages(self):
        with SQLitePostStore(self.db) as store:
            store.upsert_post(
                {
                    "id": "100",
                    "content": "old",
                    "create_time": "2026-06-24 23:00:00",
                    "comment_count": 0,
                },
                [],
            )
        client = FakeClient(
            {
                1: [
                    {
                        "id": "103",
                        "detail": "post 103 stub",
                        "create_time": "2026-06-25 00:05:00",
                        "update_time": "2026-06-25 00:05:00",
                        "count_comment": 0,
                    }
                ],
                2: [
                    {
                        "id": "100",
                        "create_time": "2026-06-24 23:00:00",
                        "update_time": "2026-06-24 23:00:00",
                        "count_comment": 0,
                    }
                ],
                3: [
                    {
                        "id": "99",
                        "create_time": "2026-06-24 22:00:00",
                        "update_time": "2026-06-24 22:00:00",
                        "count_comment": 0,
                    }
                ],
            },
            {},
        )
        stats = self.service(client).discover_queue(
            command="discover-latest",
            endpoint="lists",
            since="2026-06-25 00:00:00",
            max_pages=10,
            old_page_threshold=2,
            stop_on_repeat=True,
            dry_run=False,
            write_stubs=True,
            min_delay=0,
            max_delay=0,
        )
        self.assertTrue(stats["old_page_stop"])
        with SQLitePostStore(self.db) as store:
            rows = store.conn.execute(
                "select post_id, source, priority, reason from crawler_queue"
            ).fetchall()
            post = store.conn.execute(
                "select id, content, crawl_status from posts where id='103'"
            ).fetchone()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["post_id"], "103")
        self.assertEqual(rows[0]["reason"], "new_post")
        self.assertEqual(post["crawl_status"], "list_only")
        self.assertIn("103", post["content"])

    def test_discover_requeues_list_only_post_when_queue_is_missing(self):
        with SQLitePostStore(self.db) as store:
            store.upsert_list_stub(
                {
                    "id": "150",
                    "detail": "old stub",
                    "create_time": "2026-06-25 00:05:00",
                    "update_time": "2026-06-25 00:05:00",
                    "count_comment": 0,
                },
                source="lists",
            )
        client = FakeClient(
            {
                1: [
                    {
                        "id": "150",
                        "detail": "newer stub",
                        "create_time": "2026-06-25 00:05:00",
                        "update_time": "2026-06-25 00:06:00",
                        "count_comment": 0,
                    }
                ],
                2: [],
            },
            {},
        )
        stats = self.service(client).discover_queue(
            command="discover-latest",
            endpoint="lists",
            since="2026-06-25 00:00:00",
            max_pages=2,
            old_page_threshold=2,
            stop_on_repeat=True,
            dry_run=False,
            write_stubs=True,
            min_delay=0,
            max_delay=0,
        )
        self.assertEqual(stats["queued"], 1)
        with SQLitePostStore(self.db) as store:
            queue = store.conn.execute(
                "select status, reason from crawler_queue where post_id='150'"
            ).fetchone()
            post = store.conn.execute(
                "select content, crawl_status, list_source from posts where id='150'"
            ).fetchone()
        self.assertEqual(queue["status"], "pending")
        self.assertEqual(queue["reason"], "new_post")
        self.assertEqual(post["crawl_status"], "list_only")
        self.assertEqual(post["list_source"], "lists")
        self.assertIn("newer stub", post["content"])

    def test_discover_active_stops_on_repeated_page_signature(self):
        with SQLitePostStore(self.db) as store:
            store.upsert_post(
                {
                    "id": "200",
                    "content": "existing",
                    "create_time": "2026-06-24 10:00:00",
                    "comment_count": 1,
                },
                [],
            )
        repeated = [
            {
                "id": "200",
                "create_time": "2026-06-24 10:00:00",
                "update_time": "2026-06-25 10:00:00",
                "count_comment": 2,
            }
        ]
        client = FakeClient({1: repeated, 2: repeated}, {})
        stats = self.service(client).discover_queue(
            command="discover-active",
            endpoint="lists2",
            since="2026-06-25 00:00:00",
            max_pages=10,
            old_page_threshold=2,
            stop_on_repeat=True,
            dry_run=False,
            write_stubs=True,
            min_delay=0,
            max_delay=0,
        )
        self.assertTrue(stats["repeat_stop"])
        with SQLitePostStore(self.db) as store:
            row = store.conn.execute(
                "select post_id, priority, reason from crawler_queue"
            ).fetchone()
        self.assertEqual(row["post_id"], "200")
        self.assertEqual(row["priority"], 0)
        self.assertEqual(row["reason"], "comment_changed")

    def test_trickle_fill_stops_on_rate_limit_and_keeps_pending(self):
        with SQLitePostStore(self.db) as store:
            store.enqueue_crawler_candidate(
                post_id="300",
                source="lists",
                priority=10,
                list_create_time="2026-06-25 00:00:00",
                list_update_time="2026-06-25 00:00:00",
                list_comment_count=0,
                db_comment_count=None,
                reason="new_post",
            )
        with self.assertRaisesRegex(RuntimeError, "rate_limited"):
            self.service(RateLimitedClient({}, {})).trickle_fill(
                limit=1,
                dry_run=False,
                min_delay=0,
                max_delay=0,
                stop_after_misses=3,
            )
        with SQLitePostStore(self.db) as store:
            row = store.conn.execute(
                "select status, attempts, last_error from crawler_queue where post_id='300'"
            ).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 1)
        self.assertIn("rate_limited", row["last_error"])

    def test_trickle_fill_skips_not_found_without_stopping(self):
        with SQLitePostStore(self.db) as store:
            for post_id in ("401", "402"):
                store.enqueue_crawler_candidate(
                    post_id=post_id,
                    source="lists",
                    priority=10,
                    list_create_time="2026-06-25 00:00:00",
                    list_update_time="2026-06-25 00:00:00",
                    list_comment_count=0,
                    db_comment_count=None,
                    reason="new_post",
                )
        client = FakeClient({}, {"402": detail("402")})
        stats = self.service(client).trickle_fill(
            limit=2,
            dry_run=False,
            min_delay=0,
            max_delay=0,
            stop_after_misses=1,
        )
        self.assertEqual(stats["written"], 1)
        with SQLitePostStore(self.db) as store:
            rows = {
                row["post_id"]: row["status"]
                for row in store.conn.execute(
                    "select post_id, status from crawler_queue"
                )
            }
        self.assertEqual(rows["401"], "skipped")
        self.assertEqual(rows["402"], "done")

    def test_plan_gaps_records_sparse_ranges(self):
        with SQLitePostStore(self.db) as store:
            store.upsert_post(
                {
                    "id": "1000",
                    "content": "anchor",
                    "create_time": "2026-06-25 00:00:00",
                    "comment_count": 0,
                },
                [],
            )
            store.upsert_post(
                {
                    "id": "1001",
                    "content": "near",
                    "create_time": "2026-06-25 00:01:00",
                    "comment_count": 0,
                },
                [],
            )
        stats = self.service(FakeClient({1: [{"id": "1010"}]}, {})).plan_gap_ranges(
            since="2026-06-25 00:00:00",
            start_id=1000,
            end_id=1010,
            chunk_size=5,
            density_threshold=0.8,
            dry_run=False,
        )
        self.assertEqual(stats["planned"], 3)
        with SQLitePostStore(self.db) as store:
            self.assertEqual(
                store.conn.execute("select count(*) from crawler_gap_ranges").fetchone()[0],
                3,
            )

    def test_probe_gaps_records_found_without_writing_post(self):
        with SQLitePostStore(self.db) as store:
            store.ensure_runtime_schema()
            store.conn.execute(
                """
                insert into crawler_gap_ranges values
                ('500-500', 500, 500, 'density_gap', 'pending', 0.0,
                 0, 0, 0, 0, 'now', 'now')
                """
            )
            store.conn.commit()
        stats = self.service(FakeClient({}, {"500": detail("500")})).probe_gap_ranges(
            range_limit=1,
            samples_per_range=1,
            enqueue_found=True,
            dry_run=False,
            min_delay=0,
            max_delay=0,
        )
        self.assertEqual(stats["found"], 1)
        with SQLitePostStore(self.db) as store:
            self.assertIsNone(
                store.conn.execute("select 1 from posts where id='500'").fetchone()
            )
            probe = store.conn.execute(
                "select status from crawler_id_probe where post_id='500'"
            ).fetchone()
            queue = store.conn.execute(
                "select priority, reason from crawler_queue where post_id='500'"
            ).fetchone()
        self.assertEqual(probe["status"], "found")
        self.assertEqual(queue["priority"], 15)
        self.assertEqual(queue["reason"], "id_probe_found")

    def test_gap_sampling_advances_after_existing_probes(self):
        first = CrawlerService.sample_ids(100, 109, 3)
        second = CrawlerService.sample_ids(
            100,
            109,
            3,
            offset=len(first),
            exclude={str(post_id) for post_id in first},
        )
        self.assertEqual(len(first), 3)
        self.assertEqual(len(second), 3)
        self.assertTrue(set(first).isdisjoint(second))


if __name__ == "__main__":
    unittest.main()
