from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime
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


class TransientDetailClient(FakeClient):
    def article(self, post_id):
        return None, "network down"


class QuotaStoppedClient(FakeClient):
    def list_page(self, endpoint, page):
        return None, "source_quota_window_locked"

    def article(self, post_id):
        return None, "source_quota_window_locked"


def detail(post_id, comments=0):
    comment_list = [
        {
            "id": f"c{post_id}-{index}",
            "detail": f"comment {index}",
            "show_user_name": "commenter",
        }
        for index in range(comments)
    ]
    return (
        {
            "community_id": "4",
            "title": f"post {post_id}",
            "detail": "body",
            "show_user_name": "user",
            "create_time": "2026-06-11 10:00:00",
            "count_comment": comments,
            "comment_list": comment_list,
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
            self.assertEqual(conn.execute("select count(*) from posts").fetchone()[0], 1)
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
        with self.assertRaisesRegex(RuntimeError, "failed before reading any page"):
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

    def test_discover_accepts_iso_cutoff_from_railway_env(self):
        client = FakeClient(
            {
                1: [
                    {
                        "id": "104",
                        "detail": "iso cutoff stub",
                        "create_time": "2026-06-25 00:05:00",
                        "update_time": "2026-06-25 00:05:00",
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
            since="2026-06-25T00:00:00",
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
            row = store.conn.execute(
                "select post_id from crawler_queue where post_id='104'"
            ).fetchone()
        self.assertIsNotNone(row)

    def test_discover_stops_cleanly_when_request_quota_closes(self):
        stats = self.service(QuotaStoppedClient({}, {})).discover_queue(
            command="discover-latest",
            endpoint="lists",
            since="2026-06-25 00:00:00",
            max_pages=5,
            old_page_threshold=2,
            dry_run=False,
            write_stubs=True,
            min_delay=0,
            max_delay=0,
        )
        self.assertTrue(stats["quota_stop"])
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["pages"], 0)

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

    def test_list_stub_noops_when_existing_snapshot_is_unchanged(self):
        article = {
            "id": "151",
            "detail": "same stub",
            "create_time": "2026-06-25 00:05:00",
            "update_time": "2026-06-25 00:06:00",
            "count_comment": 2,
            "count_star": 3,
            "count_trace": 4,
        }
        with SQLitePostStore(self.db) as store:
            changed = store.upsert_list_stub(article, source="lists")
            self.assertTrue(changed)
            store.conn.execute("update posts set updated_at='sentinel' where id='151'")
            store.conn.commit()
            changed = store.upsert_list_stub(article, source="lists")
            row = store.conn.execute("select updated_at from posts where id='151'").fetchone()
        self.assertFalse(changed)
        self.assertEqual(row["updated_at"], "sentinel")

    def test_enqueue_crawler_candidate_noops_when_candidate_is_unchanged(self):
        with SQLitePostStore(self.db) as store:
            kwargs = {
                "post_id": "152",
                "source": "lists",
                "priority": 10,
                "list_create_time": "2026-06-25 00:05:00",
                "list_update_time": "2026-06-25 00:06:00",
                "list_comment_count": 2,
                "db_comment_count": None,
                "reason": "new_post",
            }
            store.enqueue_crawler_candidate(**kwargs)
            store.conn.execute("update crawler_queue set updated_at='sentinel' where post_id='152'")
            store.conn.commit()
            store.enqueue_crawler_candidate(**kwargs)
            row = store.conn.execute(
                "select updated_at from crawler_queue where post_id='152'"
            ).fetchone()
        self.assertEqual(row["updated_at"], "sentinel")

    def test_runtime_schema_migrates_legacy_comment_gap_only_once(self):
        with SQLitePostStore(self.db) as store:
            post, comments = self.service(
                FakeClient({}, {"153": detail("153", 1)})
            ).fetch_detail_with_error(FakeClient({}, {"153": detail("153", 1)}), "153")[0]
            store.upsert_post(post, comments)
            store.enqueue_crawler_candidate(
                post_id="153",
                source="lists2",
                priority=0,
                list_create_time="",
                list_update_time="2026-06-25 10:00:00",
                list_comment_count=3,
                db_comment_count=1,
                reason="comment_changed",
            )
            store.mark_crawler_queue_item("153", status="done")
            store.upsert_post(
                {
                    "id": "154",
                    "content": "old hot loop",
                    "create_time": "2026-06-25 09:00:00",
                    "comment_count": 1,
                },
                [{"id": "c1", "detail": "old"}],
            )
            store.enqueue_crawler_candidate(
                post_id="154",
                source="lists2",
                priority=0,
                list_create_time="",
                list_update_time="2026-06-25 10:00:00",
                list_comment_count=4,
                db_comment_count=1,
                reason="comment_changed",
            )
            store.conn.execute(
                "update crawler_queue set status='pending',attempts=620 where post_id='154'"
            )
            store.conn.execute(
                "delete from crawl_state where key='crawler_queue_observation_state_v1'"
            )
            store.conn.execute(
                """
                update crawler_queue
                set last_attempt_list_comment_count=null,
                    last_attempt_list_update_time='',
                    same_observation_attempts=0
                where post_id in ('153','154')
                """
            )
            store.conn.commit()
            migration = store.migrate_crawler_queue_observations()
            row = store.conn.execute(
                """
                select status,priority,last_attempt_list_comment_count
                from crawler_queue where post_id='153'
                """
            ).fetchone()
            store.mark_crawler_queue_item("153", status="done")
            store.ensure_runtime_schema()
            stable = store.conn.execute(
                "select status from crawler_queue where post_id='153'"
            ).fetchone()
            hot_loop = store.conn.execute(
                """
                select status,attempts,last_attempt_list_comment_count
                from crawler_queue where post_id='154'
                """
            ).fetchone()
        self.assertEqual(migration["stale_gaps"], 2)
        self.assertEqual(migration["deferred"], 1)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["priority"], 0)
        self.assertEqual(row["last_attempt_list_comment_count"], 3)
        self.assertEqual(stable["status"], "done")
        self.assertEqual(hot_loop["status"], "deferred")
        self.assertEqual(hot_loop["attempts"], 620)
        self.assertEqual(hot_loop["last_attempt_list_comment_count"], 4)

    def test_runtime_schema_normalizes_legacy_terminal_states(self):
        with SQLitePostStore(self.db) as store:
            for post_id, error in (
                ("155", "not_found"),
                ("156", "network timeout"),
                ("157", "suspicious_payload:empty_comments"),
            ):
                store.enqueue_crawler_candidate(
                    post_id=post_id,
                    source="lists",
                    priority=10,
                    list_create_time="2026-06-25 10:00:00",
                    list_update_time="2026-06-25 10:00:00",
                    list_comment_count=1,
                    db_comment_count=None,
                    reason="new_post",
                )
                store.mark_crawler_queue_item(
                    post_id,
                    status="failed",
                    last_error=error,
                    record_observation=True,
                )
            store.conn.execute(
                "delete from crawl_state where key='crawler_queue_terminal_state_v2'"
            )
            store.conn.commit()
            migration = store.migrate_crawler_queue_terminal_states()
            rows = {
                row["post_id"]: row["status"]
                for row in store.conn.execute(
                    "select post_id,status from crawler_queue where post_id in ('155','156','157')"
                )
            }
        self.assertEqual(migration["normalized_skipped"], 1)
        self.assertEqual(migration["requeued_transient"], 1)
        self.assertEqual(rows["155"], "skipped")
        self.assertEqual(rows["156"], "pending")
        self.assertEqual(rows["157"], "failed")

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

    def test_comment_growth_reopens_done_queue_item_and_refreshes_detail(self):
        with SQLitePostStore(self.db) as store:
            old_post = detail("205", 1)[0]
            normalized = self.service(FakeClient({}, {})).fetch_detail_with_error(
                FakeClient({}, {"205": (old_post, None)}), "205"
            )[0]
            store.upsert_post(*normalized)
            store.enqueue_crawler_candidate(
                post_id="205",
                source="lists",
                priority=10,
                list_create_time="2026-06-25 10:00:00",
                list_update_time="2026-06-25 10:00:00",
                list_comment_count=1,
                db_comment_count=None,
                reason="new_post",
            )
            store.mark_crawler_queue_item("205", status="done")

        article = {
            "id": "205",
            "create_time": "2026-06-25 10:00:00",
            "update_time": "2026-06-25 11:00:00",
            "count_comment": 2,
        }
        service = self.service(FakeClient({1: [article], 2: []}, {}))
        stats = service.discover_queue(
            command="discover-active",
            endpoint="lists2",
            since="2026-06-25 00:00:00",
            max_pages=2,
            old_page_threshold=2,
            min_pages=1,
            no_action_page_threshold=1,
            dry_run=False,
            write_stubs=True,
            min_delay=0,
            max_delay=0,
        )
        self.assertEqual(stats["queue_reopened"], 1)
        with SQLitePostStore(self.db) as store:
            row = store.conn.execute(
                "select status,priority,reason from crawler_queue where post_id='205'"
            ).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["priority"], 0)
        self.assertIn("comment_changed", row["reason"])

        refresh = self.service(FakeClient({}, {"205": detail("205", 2)}))
        result = refresh.trickle_fill(
            limit=1,
            dry_run=False,
            min_delay=0,
            max_delay=0,
            stop_after_misses=1,
        )
        self.assertEqual(result["refreshed_details"], 1)
        self.assertEqual(result["new_comment_rows"], 1)
        with SQLitePostStore(self.db) as store:
            self.assertEqual(store.get_post_counts("205"), 2)
            self.assertEqual(
                store.conn.execute("select count(*) from comments where post_id='205'").fetchone()[
                    0
                ],
                2,
            )

    def test_unchanged_pending_observations_trigger_no_action_stop(self):
        article = {
            "id": "206",
            "detail": "pending",
            "create_time": "2026-06-25 10:00:00",
            "update_time": "2026-06-25 10:00:00",
            "count_comment": 1,
        }
        with SQLitePostStore(self.db) as store:
            store.upsert_list_stub(article, source="lists")
            store.enqueue_crawler_candidate(
                post_id="206",
                source="lists",
                priority=10,
                list_create_time=article["create_time"],
                list_update_time=article["update_time"],
                list_comment_count=1,
                db_comment_count=1,
                reason="new_post",
            )
        stats = self.service(
            FakeClient({1: [article], 2: [article], 3: [article]}, {})
        ).discover_queue(
            command="discover-latest",
            endpoint="lists",
            since="2026-06-25 00:00:00",
            max_pages=3,
            old_page_threshold=3,
            stop_on_repeat=False,
            min_pages=2,
            no_action_page_threshold=2,
            dry_run=False,
            write_stubs=True,
            min_delay=0,
            max_delay=0,
        )
        self.assertTrue(stats["no_action_stop"])
        self.assertEqual(stats["queued"], 2)
        self.assertEqual(stats["queue_unchanged"], 2)
        self.assertEqual(stats["pages"], 2)

    def test_discover_active_ignores_lower_comment_count_snapshot(self):
        with SQLitePostStore(self.db) as store:
            store.upsert_post(
                {
                    "id": "210",
                    "content": "existing",
                    "create_time": "2026-06-24 10:00:00",
                    "comment_count": 3,
                },
                [],
            )
        client = FakeClient(
            {
                1: [
                    {
                        "id": "210",
                        "create_time": "2026-06-24 10:00:00",
                        "update_time": "2026-06-25 10:00:00",
                        "count_comment": 2,
                    }
                ],
                2: [],
            },
            {},
        )
        stats = self.service(client).discover_queue(
            command="discover-active",
            endpoint="lists2",
            since="2026-06-25 00:00:00",
            max_pages=2,
            old_page_threshold=2,
            stop_on_repeat=True,
            dry_run=False,
            write_stubs=True,
            min_delay=0,
            max_delay=0,
        )
        self.assertEqual(stats["queued"], 0)
        self.assertEqual(stats["comment_changed"], 0)
        with SQLitePostStore(self.db) as store:
            self.assertIsNone(store.conn.execute("select 1 from crawler_queue").fetchone())

    def test_discover_active_stops_after_consecutive_no_action_pages(self):
        with SQLitePostStore(self.db) as store:
            for post_id in ("220", "221", "222", "223", "224"):
                store.upsert_post(
                    {
                        "id": post_id,
                        "content": "existing",
                        "create_time": "2026-06-24 10:00:00",
                        "comment_count": 2,
                    },
                    [],
                )
        pages = {
            idx + 1: [
                {
                    "id": str(220 + idx),
                    "create_time": "2026-06-24 10:00:00",
                    "update_time": "2026-06-25 10:00:00",
                    "count_comment": 2,
                }
            ]
            for idx in range(5)
        }
        pages[6] = [
            {
                "id": "999",
                "create_time": "2026-06-25 10:00:00",
                "update_time": "2026-06-25 10:00:00",
                "count_comment": 9,
            }
        ]
        stats = self.service(FakeClient(pages, {})).discover_queue(
            command="discover-active",
            endpoint="lists2",
            since="2026-06-25 00:00:00",
            max_pages=10,
            old_page_threshold=2,
            stop_on_repeat=False,
            min_pages=3,
            no_action_page_threshold=3,
            dry_run=False,
            write_stubs=True,
            min_delay=0,
            max_delay=0,
        )
        self.assertTrue(stats["no_action_stop"])
        self.assertEqual(stats["pages"], 3)
        with SQLitePostStore(self.db) as store:
            self.assertIsNone(
                store.conn.execute("select 1 from crawler_queue where post_id='999'").fetchone()
            )

    def test_queue_prioritizes_new_comment_delta_before_plain_new_posts(self):
        with SQLitePostStore(self.db) as store:
            store.enqueue_crawler_candidate(
                post_id="601",
                source="lists",
                priority=40,
                list_create_time="2026-06-25 12:00:00",
                list_update_time="2026-06-25 12:00:00",
                list_comment_count=0,
                db_comment_count=None,
                reason="new_post",
            )
            store.enqueue_crawler_candidate(
                post_id="602",
                source="lists2",
                priority=0,
                list_create_time="2026-06-25 09:00:00",
                list_update_time="2026-06-25 13:00:00",
                list_comment_count=6,
                db_comment_count=2,
                reason="comment_changed",
            )
            store.enqueue_crawler_candidate(
                post_id="603",
                source="lists",
                priority=10,
                list_create_time="2026-06-25 11:00:00",
                list_update_time="2026-06-25 11:00:00",
                list_comment_count=3,
                db_comment_count=None,
                reason="new_post",
            )
            rows = store.next_crawler_queue_items(3)
        self.assertEqual([row["post_id"] for row in rows], ["602", "603", "601"])

    def test_queue_refresh_cap_reserves_detail_slots_for_unfetched_ids(self):
        with SQLitePostStore(self.db) as store:
            for index in range(6):
                store.enqueue_crawler_candidate(
                    post_id=str(610 + index),
                    source="lists2",
                    priority=0,
                    list_create_time="",
                    list_update_time=f"2026-06-25 13:0{index}:00",
                    list_comment_count=10,
                    db_comment_count=1,
                    reason="comment_changed",
                )
            for index in range(8):
                store.enqueue_crawler_candidate(
                    post_id=str(620 + index),
                    source="lists",
                    priority=10,
                    list_create_time=f"2026-06-25 12:0{index}:00",
                    list_update_time="",
                    list_comment_count=2,
                    db_comment_count=None,
                    reason="new_post",
                )
            rows = store.next_crawler_queue_items(12, refresh_limit=4)
        self.assertEqual(sum(row["priority"] == 0 for row in rows), 4)
        self.assertEqual(sum(row["priority"] > 0 for row in rows), 8)
        self.assertEqual(len({row["post_id"] for row in rows}), 12)

    def test_trickle_scales_refresh_cap_for_a_quota_reduced_batch(self):
        details = {}
        with SQLitePostStore(self.db) as store:
            for index in range(4):
                post_id = str(640 + index)
                details[post_id] = detail(post_id, 1)
                store.enqueue_crawler_candidate(
                    post_id=post_id,
                    source="lists2",
                    priority=0,
                    list_create_time="",
                    list_update_time=f"2026-06-25 13:0{index}:00",
                    list_comment_count=1,
                    db_comment_count=0,
                    reason="comment_changed",
                )
            for index in range(6):
                post_id = str(650 + index)
                details[post_id] = detail(post_id, 1)
                store.enqueue_crawler_candidate(
                    post_id=post_id,
                    source="lists",
                    priority=10,
                    list_create_time=f"2026-06-25 12:0{index}:00",
                    list_update_time="",
                    list_comment_count=1,
                    db_comment_count=None,
                    reason="new_post",
                )
            fresh_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for index in range(6):
                post_id = str(660 + index)
                details[post_id] = detail(post_id, 1)
                store.enqueue_crawler_candidate(
                    post_id=post_id,
                    source="lists",
                    priority=10,
                    list_create_time=fresh_time,
                    list_update_time=fresh_time,
                    list_comment_count=1,
                    db_comment_count=None,
                    reason="new_post",
                )
        stats = self.service(FakeClient({}, details)).trickle_fill(
            limit=8,
            refresh_limit=4,
            dry_run=False,
            min_delay=0,
            max_delay=0,
            stop_after_misses=2,
        )
        self.assertEqual(stats["refresh_limit"], 2)
        self.assertEqual(stats["selected_refresh"], 2)
        self.assertEqual(stats["selected_coverage"], 6)
        self.assertEqual(stats["selected_fresh_coverage"], 4)
        self.assertEqual(stats["selected_backlog_coverage"], 2)

    def test_list_detail_gap_gets_one_delayed_retry_then_defers_until_growth(self):
        with SQLitePostStore(self.db) as store:
            store.upsert_post(
                {
                    "id": "630",
                    "content": "existing",
                    "create_time": "2026-06-25 10:00:00",
                    "comment_count": 1,
                },
                [{"id": "c1", "detail": "one"}],
            )
            store.enqueue_crawler_candidate(
                post_id="630",
                source="lists2",
                priority=0,
                list_create_time="",
                list_update_time="2026-06-25 13:00:00",
                list_comment_count=3,
                db_comment_count=1,
                reason="comment_changed",
            )

        service = self.service(FakeClient({}, {"630": detail("630", 1)}))
        first = service.trickle_fill(
            limit=1,
            dry_run=False,
            min_delay=0,
            max_delay=0,
            stop_after_misses=1,
            observation_retry_delay=3600,
            max_observation_attempts=2,
        )
        self.assertEqual(first["retry_scheduled"], 1)
        with SQLitePostStore(self.db) as store:
            row = store.conn.execute(
                """
                select status,same_observation_attempts,next_attempt_at
                from crawler_queue where post_id='630'
                """
            ).fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["same_observation_attempts"], 1)
            self.assertTrue(row["next_attempt_at"])
            self.assertEqual(store.next_crawler_queue_items(1), [])
            store.conn.execute(
                "update crawler_queue set next_attempt_at='' where post_id='630'"
            )
            store.conn.commit()

        second = service.trickle_fill(
            limit=1,
            dry_run=False,
            min_delay=0,
            max_delay=0,
            stop_after_misses=1,
            observation_retry_delay=3600,
            max_observation_attempts=2,
        )
        self.assertEqual(second["deferred_observations"], 1)
        with SQLitePostStore(self.db) as store:
            row = store.conn.execute(
                "select status from crawler_queue where post_id='630'"
            ).fetchone()
            self.assertEqual(row["status"], "deferred")
            unchanged = store.enqueue_crawler_candidate(
                post_id="630",
                source="lists2",
                priority=0,
                list_create_time="",
                list_update_time="2026-06-25 13:00:00",
                list_comment_count=3,
                db_comment_count=1,
                reason="comment_changed",
            )
            self.assertEqual(unchanged, "unchanged")
            update_only = store.enqueue_crawler_candidate(
                post_id="630",
                source="lists2",
                priority=0,
                list_create_time="",
                list_update_time="2026-06-25 13:30:00",
                list_comment_count=3,
                db_comment_count=1,
                reason="comment_changed",
            )
            self.assertEqual(update_only, "unchanged")
            reopened = store.enqueue_crawler_candidate(
                post_id="630",
                source="lists2",
                priority=0,
                list_create_time="",
                list_update_time="2026-06-25 14:00:00",
                list_comment_count=4,
                db_comment_count=1,
                reason="comment_changed",
            )
            status = store.conn.execute(
                "select status from crawler_queue where post_id='630'"
            ).fetchone()["status"]
        self.assertEqual(reopened, "reopened")
        self.assertEqual(status, "pending")

    def test_new_observation_clears_a_pending_gap_delay(self):
        with SQLitePostStore(self.db) as store:
            store.upsert_post(
                {
                    "id": "631",
                    "content": "existing",
                    "create_time": "2026-06-25 10:00:00",
                    "comment_count": 1,
                },
                [{"id": "c1", "detail": "one"}],
            )
            store.enqueue_crawler_candidate(
                post_id="631",
                source="lists2",
                priority=0,
                list_create_time="",
                list_update_time="2026-06-25 13:00:00",
                list_comment_count=3,
                db_comment_count=1,
                reason="comment_changed",
            )
            store.finish_crawler_queue_detail(
                "631",
                detail_comment_count=1,
                retry_delay_seconds=3600,
                max_same_observation_attempts=2,
            )
            store.enqueue_crawler_candidate(
                post_id="631",
                source="lists2",
                priority=0,
                list_create_time="",
                list_update_time="2026-06-25 14:00:00",
                list_comment_count=4,
                db_comment_count=1,
                reason="comment_changed",
            )
            row = store.conn.execute(
                """
                select status,next_attempt_at,same_observation_attempts
                from crawler_queue where post_id='631'
                """
            ).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["next_attempt_at"], "")
        self.assertEqual(row["same_observation_attempts"], 0)

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

    def test_transient_detail_failure_retries_with_delay_then_stops(self):
        with SQLitePostStore(self.db) as store:
            store.enqueue_crawler_candidate(
                post_id="302",
                source="lists",
                priority=10,
                list_create_time="2026-06-25 00:00:00",
                list_update_time="2026-06-25 00:00:00",
                list_comment_count=1,
                db_comment_count=None,
                reason="new_post",
            )
        service = self.service(TransientDetailClient({}, {}))
        for attempt in range(1, 4):
            stats = service.trickle_fill(
                limit=1,
                dry_run=False,
                min_delay=0,
                max_delay=0,
                stop_after_misses=2,
                transient_retry_delay=3600,
                max_transient_attempts=3,
            )
            with SQLitePostStore(self.db) as store:
                row = store.conn.execute(
                    """
                    select status,attempts,next_attempt_at
                    from crawler_queue where post_id='302'
                    """
                ).fetchone()
                self.assertEqual(row["attempts"], attempt)
                if attempt < 3:
                    self.assertEqual(stats["transient_retries"], 1)
                    self.assertEqual(row["status"], "pending")
                    self.assertTrue(row["next_attempt_at"])
                    store.conn.execute(
                        "update crawler_queue set next_attempt_at='' where post_id='302'"
                    )
                    store.conn.commit()
                else:
                    self.assertEqual(stats["terminal_failures"], 1)
                    self.assertEqual(row["status"], "failed")
                    self.assertEqual(row["next_attempt_at"], "")

    def test_trickle_quota_stop_keeps_item_pending_without_attempt(self):
        with SQLitePostStore(self.db) as store:
            store.enqueue_crawler_candidate(
                post_id="301",
                source="lists",
                priority=10,
                list_create_time="",
                list_update_time="",
                list_comment_count=1,
                db_comment_count=None,
                reason="new_post",
            )
        stats = self.service(QuotaStoppedClient({}, {})).trickle_fill(
            limit=1,
            dry_run=False,
            min_delay=0,
            max_delay=0,
            stop_after_misses=1,
        )
        self.assertTrue(stats["quota_stop"])
        with SQLitePostStore(self.db) as store:
            row = store.conn.execute(
                "select status,attempts,last_error from crawler_queue where post_id='301'"
            ).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["last_error"], "")

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
                for row in store.conn.execute("select post_id, status from crawler_queue")
            }
        self.assertEqual(rows["401"], "skipped")
        self.assertEqual(rows["402"], "done")

    def test_trickle_rejects_empty_comment_payload_without_erasing_old_rows(self):
        with SQLitePostStore(self.db) as store:
            old = self.service(FakeClient({}, {"410": detail("410", 1)}))
            parsed, error = old.fetch_detail_with_error(
                FakeClient({}, {"410": detail("410", 1)}), "410"
            )
            self.assertIsNone(error)
            store.upsert_post(*parsed)
            store.enqueue_crawler_candidate(
                post_id="410",
                source="lists2",
                priority=0,
                list_create_time="",
                list_update_time="",
                list_comment_count=2,
                db_comment_count=1,
                reason="comment_changed",
            )
        suspicious = detail("410", 2)
        suspicious[0]["comment_list"] = []
        stats = self.service(FakeClient({}, {"410": suspicious})).trickle_fill(
            limit=1,
            dry_run=False,
            min_delay=0,
            max_delay=0,
            stop_after_misses=1,
        )
        self.assertEqual(stats["suspicious_payloads"], 1)
        with SQLitePostStore(self.db) as store:
            rows = store.conn.execute("select detail from comments where post_id='410'").fetchall()
            queue = store.conn.execute(
                "select status,last_error from crawler_queue where post_id='410'"
            ).fetchone()
        self.assertEqual([row["detail"] for row in rows], ["comment 0"])
        self.assertEqual(queue["status"], "failed")
        self.assertIn("suspicious_payload", queue["last_error"])

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
            since="2026-06-25T00:00:00",
            start_id=0,
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

    def test_plan_gaps_uses_saved_max_id_without_a_source_list_call(self):
        class NoSourceListClient(FakeClient):
            def list_page(self, endpoint, page):
                raise AssertionError("gap planning must not call the source")

        with SQLitePostStore(self.db) as store:
            for post_id in ("1000", "1005"):
                store.upsert_post(
                    {
                        "id": post_id,
                        "content": "saved id",
                        "create_time": "2026-06-25 00:00:00",
                        "comment_count": 0,
                    },
                    [],
                )
        stats = self.service(NoSourceListClient({}, {})).plan_gap_ranges(
            since="",
            start_id=1000,
            end_id=0,
            chunk_size=3,
            density_threshold=0.8,
            dry_run=False,
        )
        self.assertEqual(stats["end_id"], 1005)
        self.assertEqual(stats["planned"], 2)

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
            self.assertIsNone(store.conn.execute("select 1 from posts where id='500'").fetchone())
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
