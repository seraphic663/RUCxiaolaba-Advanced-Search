import json
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.admin_crawl_service import AdminCrawlError, AdminCrawlService
from crawler.client import MiniProgramClient
from crawler.manual_quota import ManualQuota
from crawler.normalizer import normalize_detail
from storage.post_writer import SQLitePostStore
from storage.symbol_index import SYMBOL_SCHEMA


class FakeQuota:
    def __init__(self, detail_remaining=10):
        self.calls = []
        self.detail_remaining = detail_remaining

    def reserve(self, kind, manual_kind, count=1):
        self.calls.append((kind, manual_kind, count))
        return {}

    def pause_for_rate_limit(self, detail):
        raise AssertionError(detail)

    def status(self):
        return {
            "preview_used": 1,
            "preview_allowed": 20,
            "detail_used": len([call for call in self.calls if call[1] == "detail"]),
            "detail_allowed": 10,
            "detail_remaining": self.detail_remaining,
        }


class FakeClient:
    def search(self, keyword, page):
        return {
            "list": [
                {
                    "id": "901",
                    "title": keyword,
                    "detail": "候选正文",
                    "count_comment": 1,
                    "create_time": "2026-07-11 12:00:00",
                }
            ]
        }, None
    def list_page(self, endpoint, page):
        return self.search(endpoint, page)

    def article(self, post_id):
        return {
            "community_id": 4,
            "title": "立即保存",
            "detail": "完整正文",
            "category_name": "测试",
            "show_user_name": "测试用户",
            "show_user_id": "u901",
            "real_user_id": "r901",
            "create_time": "2026-07-11 12:00:00",
            "count_comment": 1,
            "count_star": 2,
            "count_trace": 3,
            "comment_list": [
                {
                    "id": "c901",
                    "detail": "完整评论",
                    "show_user_name": "评论者",
                    "show_user_id": "u902",
                }
            ],
        }, None


class EmptyCommentClient(FakeClient):
    def article(self, post_id):
        data, error = super().article(post_id)
        data["comment_list"] = []
        return data, error


def wait_for_job(service, job_id):
    deadline = time.time() + 5
    while time.time() < deadline:
        job = service.get_job(job_id)
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.02)
    raise AssertionError("admin crawl job did not finish")


def test_preview_then_immediate_crawl_saves_selected_post():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        posts_db = root / "posts.db"
        with SQLitePostStore(posts_db) as store:
            store.init_schema()
        quota = FakeQuota()
        service = AdminCrawlService(
            posts_db,
            config_path=root / "config.txt",
            control_db=root / "control.db",
            quota=quota,
            client_factory=FakeClient,
            min_delay=0,
            max_delay=0,
        )

        preview = service.preview("search", "立即", 1)
        assert preview["candidates"][0]["id"] == "901"
        assert preview["candidates"][0]["local_status"] == "missing"

        job = service.create_job(preview["preview_id"], ["901"], "smart")
        job = wait_for_job(service, job["id"])
        service.wait_for_idle()
        assert job["status"] == "completed"
        assert job["written"] == 1
        assert job["items"][0]["status"] == "succeeded"

        with SQLitePostStore(posts_db) as store:
            post = store.conn.execute(
                "select content,crawl_status from posts where id='901'"
            ).fetchone()
            comments = store.conn.execute(
                "select detail from comments where post_id='901'"
            ).fetchall()
        assert post["content"] == "立即保存 完整正文"
        assert post["crawl_status"] == "full"
        assert [row["detail"] for row in comments] == ["完整评论"]
        assert quota.calls == [
            ("new_list", "preview", 1),
            ("detail", "detail", 1),
        ]


def test_queue_strategy_does_not_call_detail_api():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        posts_db = root / "posts.db"
        with SQLitePostStore(posts_db) as store:
            store.init_schema()
        quota = FakeQuota()
        service = AdminCrawlService(
            posts_db,
            config_path=root / "config.txt",
            control_db=root / "control.db",
            quota=quota,
            client_factory=FakeClient,
            min_delay=0,
            max_delay=0,
        )
        preview = service.preview("lists", "", 1)
        job = service.create_job(preview["preview_id"], ["901"], "queue")
        job = wait_for_job(service, job["id"])
        service.wait_for_idle()
        assert job["written"] == 0
        with SQLitePostStore(posts_db) as store:
            row = store.conn.execute(
                "select priority,reason from crawler_queue where post_id='901'"
            ).fetchone()
        assert row["priority"] == -10
        assert row["reason"] == "admin_selected"
        assert quota.calls == [("new_list", "preview", 1)]


def test_suspicious_empty_comments_do_not_replace_existing_data():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        posts_db = root / "posts.db"
        with SQLitePostStore(posts_db) as store:
            store.init_schema()
            payload, _ = FakeClient().article("901")
            normalized_post, normalized_comments = normalize_detail("901", payload)
            store.upsert_post(normalized_post, normalized_comments)
        service = AdminCrawlService(
            posts_db,
            config_path=root / "config.txt",
            control_db=root / "control.db",
            quota=FakeQuota(),
            client_factory=EmptyCommentClient,
            min_delay=0,
            max_delay=0,
        )
        preview = service.preview("lists", "", 1)
        job = service.create_job(preview["preview_id"], ["901"], "force")
        job = wait_for_job(service, job["id"])
        service.wait_for_idle()
        assert job["items"][0]["status"] == "failed"
        assert "已保留旧数据" in job["items"][0]["error"]
        with SQLitePostStore(posts_db) as store:
            rows = store.conn.execute(
                "select detail from comments where post_id='901'"
            ).fetchall()
        assert [row["detail"] for row in rows] == ["完整评论"]


def test_detail_upsert_incrementally_refreshes_symbol_sidecar():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        posts_db = root / "posts.db"
        symbol_db = root / "symbol.db"
        with closing(sqlite3.connect(symbol_db)) as conn:
            conn.executescript(SYMBOL_SCHEMA)
            conn.execute(
                "insert into index_meta(key,value) values ('schema_version','symbol-v1')"
            )
            conn.commit()
        with SQLitePostStore(posts_db, symbol_path=symbol_db) as store:
            store.init_schema()
            store.upsert_post(
                {
                    "id": "902",
                    "content": "♀找搭子",
                    "category_name": "测试",
                    "user_name": "测试",
                    "create_time": "2026-07-11 13:00:00",
                },
                [{"id": "c902", "detail": "😀回复"}],
            )
        with closing(sqlite3.connect(symbol_db)) as conn:
            rows = conn.execute(
                "select token,kind from symbol_rows where post_id='902' order by kind"
            ).fetchall()
        assert set(rows) == {("♀", "post"), ("😀", "comment")}


def test_immediate_batch_is_rejected_before_start_when_quota_is_insufficient():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        posts_db = root / "posts.db"
        with SQLitePostStore(posts_db) as store:
            store.init_schema()
        quota = FakeQuota(detail_remaining=0)
        service = AdminCrawlService(
            posts_db,
            config_path=root / "config.txt",
            control_db=root / "control.db",
            quota=quota,
            client_factory=FakeClient,
            min_delay=0,
            max_delay=0,
        )
        preview = service.preview("lists", "", 1)
        with pytest.raises(AdminCrawlError, match="当前只剩 0 次"):
            service.create_job(preview["preview_id"], ["901"], "force")


def test_manual_quota_is_extra_and_does_not_consume_main_counters():
    class FakeScheduler:
        @staticmethod
        def quota_date():
            return "2026-07-11"

        @staticmethod
        def beijing_now():
            return datetime(2026, 7, 11, 8, 0, tzinfo=timezone.utc)

        @staticmethod
        def quota_release_fraction():
            return 0.0

        @staticmethod
        def configured_source_budget():
            return 690

        @staticmethod
        def adaptive_source_budget():
            return 690

        @staticmethod
        def adaptive_scale():
            return 1.0

    with tempfile.TemporaryDirectory() as temp:
        posts_db = Path(temp) / "posts.db"
        quota = ManualQuota(posts_db)
        with patch.object(ManualQuota, "_scheduler", return_value=FakeScheduler):
            quota.reserve("new_list", "preview", 1)
            quota.reserve("detail", "detail", 1)
            status = quota.status()
        saved = json.loads(
            posts_db.with_name(".crawler_quota.json").read_text(encoding="utf-8")
        )
        assert saved["new_list_calls"] == 0
        assert saved["detail_calls"] == 0
        assert saved["admin_preview_calls"] == 1
        assert saved["admin_detail_calls"] == 1
        assert saved["configured_total_budget"] == 720
        assert status["preview_allowed"] == 20
        assert status["detail_allowed"] == 10


def test_upstream_search_uses_search_parameter_name():
    class FakeResponse:
        @staticmethod
        def json():
            return {"code": "0000", "data": {"list": []}}

    class FakeSession:
        def __init__(self):
            self.params = None

        def get(self, url, *, params, timeout, verify):
            self.params = params
            return FakeResponse()

    client = MiniProgramClient("cookie")
    session = FakeSession()
    client.session = session
    data, error = client.search("食堂", 1)
    assert error is None
    assert data == {"list": []}
    assert session.params["search"] == "食堂"
    assert "keyword" not in session.params
