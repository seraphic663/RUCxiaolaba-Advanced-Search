from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.search_service import SearchService
from crawler.service import CrawlerService
from storage.post_writer import SQLitePostStore


class ListOnlyClient:
    def __init__(self, article: dict):
        self.article_data = article

    def list_page(self, endpoint, page):
        return ({"list": [self.article_data]} if page == 1 else {"list": []}), None

    def article(self, post_id):
        return None, "not_found"


class DeletedPostVisibilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "posts.db"
        with SQLitePostStore(self.db) as store:
            store.init_schema()

    def tearDown(self):
        self.temp.cleanup()

    def test_known_list_post_becomes_admin_only_tombstone(self):
        with SQLitePostStore(self.db) as store:
            store.enqueue_crawler_candidate(
                post_id="701",
                source="lists",
                priority=10,
                list_create_time="2026-08-07 10:00:00",
                list_update_time="2026-08-07 10:05:00",
                list_comment_count=6,
                db_comment_count=None,
                reason="new_post",
            )
            store.mark_crawler_queue_item(
                "701",
                status="skipped",
                last_error="not_found",
                increment_attempts=True,
            )
            self.assertTrue(store.mark_post_source_unavailable("701", "not_found"))

        search = SearchService(self.db)
        public = search.search("", "time", 1, 20)
        admin = search.search("", "time", 1, 20, admin=True)

        self.assertEqual(public["results"], [])
        self.assertEqual(admin["results"][0]["id"], "701")
        self.assertEqual(admin["results"][0]["source_state"], "deleted_or_unavailable")
        self.assertEqual(admin["results"][0]["comments"], 6)
        self.assertEqual(admin["results"][0]["archived_comment_rows"], 0)
        self.assertEqual(
            search.search("", "time", 1, 20, admin=True, source_state="available")[
                "results"
            ],
            [],
        )
        self.assertEqual(
            search.search("", "time", 1, 20, admin=True, source_state="deleted")[
                "results"
            ][0]["id"],
            "701",
        )
        self.assertIsNone(search.comments("701", admin=False))
        self.assertEqual(search.comments("701", admin=True)["comment_list"], [])

    def test_deleted_full_post_keeps_archived_content_and_comment_rows(self):
        with SQLitePostStore(self.db) as store:
            store.upsert_post(
                {
                    "id": "702",
                    "content": "已经保存的正文",
                    "create_time": "2026-08-07 11:00:00",
                    "comment_count": 1,
                    "star_count": 2,
                },
                [{"id": "c1", "detail": "已经保存的评论"}],
            )
            store.enqueue_crawler_candidate(
                post_id="702",
                source="lists2",
                priority=0,
                list_create_time="2026-08-07 11:00:00",
                list_update_time="2026-08-07 12:00:00",
                list_comment_count=3,
                db_comment_count=1,
                reason="comment_changed",
            )
            self.assertTrue(store.mark_post_source_unavailable("702", "not_found"))

        admin = SearchService(self.db).search(
            "已经保存",
            "time",
            1,
            20,
            admin=True,
            admin_fields={"body"},
        )
        self.assertEqual(admin["results"][0]["comments"], 3)
        self.assertEqual(admin["results"][0]["archived_comment_rows"], 1)
        self.assertEqual(admin["results"][0]["crawl_status"], "full")

    def test_list_observation_refreshes_metrics_without_detail_and_restores_state(self):
        with SQLitePostStore(self.db) as store:
            store.upsert_post(
                {
                    "id": "703",
                    "content": "完整帖子",
                    "create_time": "2026-08-07 13:00:00",
                    "comment_count": 2,
                    "star_count": 1,
                },
                [],
            )
            store.mark_post_source_unavailable("703", "not_found")

        article = {
            "id": "703",
            "detail": "列表摘要",
            "create_time": "2026-08-07 13:00:00",
            "update_time": "2026-08-07 14:00:00",
            "count_comment": 1,
            "count_star": 7,
            "count_trace": 4,
        }
        service = CrawlerService(db_path=self.db, cookie="test", lock_timeout=2)
        service.client = lambda: ListOnlyClient(article)
        stats = service.discover_queue(
            command="discover-active",
            endpoint="lists2",
            since="2026-08-07 00:00:00",
            max_pages=2,
            old_page_threshold=2,
            stop_on_repeat=True,
            dry_run=False,
            write_stubs=True,
            min_delay=0,
            max_delay=0,
        )

        self.assertEqual(stats["comment_changed"], 0)
        with SQLitePostStore(self.db) as store:
            row = store.conn.execute(
                """
                select crawl_status,comment_count,star_count,trace_count,source_state
                from posts where id='703'
                """
            ).fetchone()
        self.assertEqual(
            dict(row),
            {
                "crawl_status": "full",
                "comment_count": 1,
                "star_count": 7,
                "trace_count": 4,
                "source_state": "available",
            },
        )

    def test_historical_not_found_queue_rows_are_backfilled_without_source_calls(self):
        with SQLitePostStore(self.db) as store:
            store.enqueue_crawler_candidate(
                post_id="704",
                source="lists,lists2",
                priority=20,
                list_create_time="2026-08-06 09:00:00",
                list_update_time="2026-08-06 09:30:00",
                list_comment_count=8,
                db_comment_count=None,
                reason="active_missing|new_post",
            )
            store.mark_crawler_queue_item(
                "704",
                status="skipped",
                last_error="not_found",
                increment_attempts=True,
            )
            store.conn.execute(
                "delete from crawl_state where key='crawler_not_found_posts_v1'"
            )
            result = store.migrate_crawler_not_found_posts()
            row = store.conn.execute(
                "select source_state,comment_count,crawl_status from posts where id='704'"
            ).fetchone()

        self.assertEqual(result["inserted_tombstones"], 1)
        self.assertEqual(
            dict(row),
            {
                "source_state": "deleted_or_unavailable",
                "comment_count": 8,
                "crawl_status": "list_only",
            },
        )


if __name__ == "__main__":
    unittest.main()
