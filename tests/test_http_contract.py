from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from app.http.server import (
    ApplicationContext,
    Handler,
    ThreadingHTTPServer,
)
from app.repositories.post_repository import PostRepository
from app.services.admin_service import AdminService
from app.services.auth_service import AdminAuthService
from app.services.search_service import SearchService
from app.services.template_service import TemplateService


class FakeAdminCrawlService:
    def preview(self, source, query, pages):
        return {
            "preview_id": "preview-1",
            "source": source,
            "query": query,
            "calls": pages,
            "candidates": [],
            "quota": {},
        }

    def create_job(self, preview_id, selected_ids, strategy):
        return {"id": "job-1", "status": "queued", "items": []}

    def get_job(self, job_id):
        return {"id": job_id, "status": "completed", "items": []}


class HTTPContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db_path = root / "posts.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            create table posts(
                id text primary key,
                content text,
                category_name text,
                l2_category text,
                user_name text,
                show_user_id text,
                real_user_id text,
                create_time text,
                comment_count integer,
                star_count integer,
                trace_count integer
            );
            create table comments(
                row_key text primary key,
                comment_id text,
                post_id text,
                parent_comment_id text,
                detail text,
                show_user_name text,
                show_user_id text,
                real_user_id text,
                reply_show_user_name text,
                reply_show_user_id text,
                is_publisher integer,
                create_time text
            );
            """
        )
        conn.execute(
            "insert into posts values (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "100",
                "食堂今天开门",
                "日常",
                "gender_view",
                "某同学",
                "u1",
                "123",
                "2026-06-11 10:00:00",
                2,
                2,
                3,
            ),
        )
        conn.execute(
            "insert into comments values (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "100:c1",
                "c1",
                "100",
                "",
                "十一点关门",
                "某同学1",
                "u2",
                "0",
                "",
                "",
                2,
                "2026-06-11 10:01:00",
            ),
        )
        conn.execute(
            "insert into comments values (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "100:c2",
                "c2",
                "100",
                "",
                "楼主补充",
                "随机匿名名",
                "u3",
                "0",
                "",
                "",
                1,
                "2026-06-11 10:02:00",
            ),
        )
        conn.execute(
            "insert into comments values (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "100:c3",
                "c3",
                "100",
                "",
                "实名评论",
                "某同学3",
                "u5",
                "456",
                "",
                "",
                2,
                "2026-06-11 10:04:00",
            ),
        )
        conn.commit()
        conn.close()

        templates = Path(__file__).resolve().parents[1] / "app" / "templates"
        Handler.context = ApplicationContext(
            posts_db=str(self.db_path),
            admin_password="test",
            posts=PostRepository(self.db_path),
            search=SearchService(self.db_path),
            admin=AdminService(self.db_path),
            admin_crawl=FakeAdminCrawlService(),
            auth=AdminAuthService(),
            templates=TemplateService(templates),
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def get_json(self, path):
        try:
            response = urlopen(self.base + path, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            self.assertEqual(response.headers.get_content_charset(), "utf-8")
            return response.status, json.loads(response.read())

    def test_health_and_search_contract(self):
        self.assertEqual(self.get_json("/healthz"), (200, {"ok": True}))
        status, payload = self.get_json(
            f"/api/search?q={quote('食堂')}&limit=10"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["results"][0]["id"], "100")
        self.assertEqual(payload["results"][0]["identity_status"], "real")
        self.assertNotIn("show_user_id", payload["results"][0])

        status, cursor = self.get_json(
            f"/api/search?q={quote('食')}&limit=10&cursor=1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(cursor["pagination_mode"], "cursor")
        self.assertEqual(cursor["candidate_total"], 1)
        self.assertTrue(cursor["total_exact"])

    def test_public_sort_modes_work_for_numbered_and_cursor_search(self):
        conn = sqlite3.connect(self.db_path)
        conn.executemany(
            "insert into posts values (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "200", "食堂二", "日常", "gender_view", "乙", "u6", "0",
                    "2026-06-12 10:00:00", 8, 1, 0,
                ),
                (
                    "300", "食堂三", "日常", "gender_view", "丙", "u7", "0",
                    "2026-06-13 10:00:00", 0, 10, 0,
                ),
            ],
        )
        conn.commit()
        conn.close()

        cases = [
            ("/api/search?sort=stars&limit=10", ["300", "100", "200"]),
            (
                "/api/search?sort=comments&limit=10&cursor=1",
                ["200", "100", "300"],
            ),
            (
                f"/api/search?q={quote('食')}&scope=content&sort=score&limit=10&cursor=1",
                ["200", "300", "100"],
            ),
        ]
        for path, expected_ids in cases:
            with self.subTest(path=path):
                status, payload = self.get_json(path)
                self.assertEqual(status, 200)
                self.assertEqual(
                    [item["id"] for item in payload["results"]], expected_ids
                )

    def test_public_can_search_comment_text_without_admin_metadata(self):
        status, payload = self.get_json(
            f"/api/search?q={quote('十一点关门')}&scope=all&limit=10"
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in payload["results"]], ["100"])
        self.assertNotIn("show_user_id", payload["results"][0])

        status, body_only = self.get_json(
            f"/api/search?q={quote('十一点关门')}&scope=content&limit=10"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body_only["results"], [])

    def test_public_search_keeps_text_scope_with_admin_session(self):
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        with opener.open(self.base + "/admin", timeout=5) as response:
            token = re.search(r'name="csrf_token" value="([^"]+)"', response.read().decode()).group(1)
        with opener.open(Request(self.base + "/admin", data=urlencode(
            {"password": "test", "csrf_token": token}).encode(), method="POST"), timeout=5):
            pass
        for cursor in (0, 1):
            for query, scope, expected in (
                ("食堂", "content", ["100"]),
                ("十一点关门", "content", []),
                ("十一点关门", "all", ["100"]),
                ("123", "content", []),
            ):
                with self.subTest(cursor=cursor, query=query, scope=scope):
                    params = urlencode({"q": query, "scope": scope, "cursor": cursor})
                    with opener.open(self.base + "/api/search?" + params, timeout=5) as response:
                        payload = json.load(response)
                    self.assertEqual([p["id"] for p in payload["results"]], expected)
        params = urlencode({"q": "123", "admin_required": 1, "admin_fields": "body,uid"})
        with opener.open(self.base + "/api/search?" + params, timeout=5) as response:
            self.assertEqual([p["id"] for p in json.load(response)["results"]], ["100"])
    def test_public_access_policy_enforces_view_search_and_deleted_settings(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "alter table posts add column source_state text not null default 'available'"
        )
        conn.execute(
            "insert into posts "
            "(id,content,category_name,l2_category,user_name,show_user_id,real_user_id,"
            "create_time,comment_count,star_count,trace_count,source_state) "
            "values (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "200", "已删除帖子正文", "已删分类", "", "删除者", "u200", "0",
                "2026-06-12 10:00:00", 1, 0, 0, "deleted_or_unavailable",
            ),
        )
        conn.execute(
            "insert into comments values (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "200:c1", "c1", "200", "", "删除帖评论", "评论者", "u201", "0",
                "", "", 2, "2026-06-12 10:01:00",
            ),
        )
        conn.commit()
        conn.close()

        policy = Handler.context.public_access
        policy.save(
            {
                "body_view": False,
                "body_search": True,
                "comments_view": False,
                "comments_search": True,
                "deleted_records": False,
            }
        )
        status, hidden_body = self.get_json(
            f"/api/search?q={quote('食堂')}&scope=content&limit=10"
        )
        self.assertEqual(status, 200)
        self.assertEqual(hidden_body["results"][0]["content"], "")
        self.assertEqual(hidden_body["results"][0]["comments"], 0)
        with urlopen(self.base + "/api/search?q=" + quote("食堂"), timeout=5) as response:
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        status, hidden_deleted = self.get_json(
            f"/api/search?q={quote('已删除帖子正文')}&source_state=deleted&limit=10"
        )
        self.assertEqual(status, 200)
        self.assertEqual(hidden_deleted["results"], [])
        status, comments_denied = self.get_json("/api/comments?id=100")
        self.assertEqual(status, 403)
        self.assertFalse(comments_denied["ok"])

        policy.save(
            {
                "body_view": True,
                "body_search": False,
                "comments_view": True,
                "comments_search": True,
                "deleted_records": False,
            }
        )
        _, body_search_denied = self.get_json(
            f"/api/search?q={quote('食堂')}&scope=content&limit=10"
        )
        self.assertEqual(body_search_denied["results"], [])
        for cursor in ("0", "1"):
            _, comment_search = self.get_json(
                f"/api/search?q={quote('十一点关门')}&scope=content&limit=10&cursor={cursor}"
            )
            self.assertEqual([item["id"] for item in comment_search["results"]], ["100"])

        policy.save(
            {
                "body_view": True,
                "body_search": True,
                "comments_view": True,
                "comments_search": True,
                "deleted_records": True,
            }
        )
        status, deleted_visible = self.get_json(
            f"/api/search?q={quote('已删除帖子正文')}&source_state=deleted&limit=10"
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in deleted_visible["results"]], ["200"])
        self.assertEqual(
            deleted_visible["results"][0]["source_state"],
            "deleted_or_unavailable",
        )
        _, deleted_comments = self.get_json("/api/comments?id=200")
        self.assertEqual(deleted_comments["source_state"], "deleted_or_unavailable")
        self.assertEqual(deleted_comments["comment_list"][0]["detail"], "删除帖评论")
        _, categories = self.get_json("/api/categories?min_count=1")
        self.assertIn("已删分类", categories["categories"])

    def test_admin_search_and_comments_omit_inferred_gender_metadata(self):
        search = SearchService(self.db_path)
        result = search.search("", "time", 1, 10, admin=True)
        self.assertEqual([item["id"] for item in result["results"]], ["100"])
        for key in (
            "gender",
            "female_probability",
            "female_likely",
            "gender_classification",
        ):
            self.assertNotIn(key, result["results"][0])

        comments = search.comments("100", admin=True)
        self.assertIsNotNone(comments)
        for key in ("gender_sort", "gender_method"):
            self.assertNotIn(key, comments)
        for key in (
            "gender",
            "female_probability",
            "female_likely",
            "gender_classification",
        ):
            self.assertNotIn(key, comments["comment_list"][0])

    def test_public_search_ignores_topic_filter(self):
        status, payload = self.get_json(
            f"/api/search?q={quote('食堂')}&l2=fwb&limit=10"
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in payload["results"]], ["100"])

    def test_calendar_date_range_includes_end_date_for_numbered_and_cursor_search(self):
        conn = sqlite3.connect(self.db_path)
        conn.executemany(
            "insert into posts values (?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("101", "当天最后一秒", "日常", "", "甲", "u6", "0", "2026-06-11 23:59:59", 0, 0, 0),
                ("102", "次日零点", "日常", "", "乙", "u7", "0", "2026-06-12 00:00:00", 0, 0, 0),
            ],
        )
        conn.commit()
        conn.close()

        expected = {"100", "101"}
        for cursor in ("0", "1"):
            status, payload = self.get_json(
                "/api/search?from=2026-06-11&to=2026-06-11&limit=10&cursor=" + cursor
            )
            self.assertEqual(status, 200)
            self.assertEqual({item["id"] for item in payload["results"]}, expected)

    def test_invalid_calendar_date_range_returns_400(self):
        status, payload = self.get_json(
            "/api/search?from=2026-06-12&to=2026-06-11"
        )
        self.assertEqual(status, 400)
        self.assertIn("开始日期不能晚于结束日期", payload["error"])

        status, payload = self.get_json("/api/search?from=not-a-date")
        self.assertEqual(status, 400)
        self.assertIn("日期格式无效", payload["error"])

    def test_comments_and_categories_contract(self):
        _, categories = self.get_json("/api/categories")
        self.assertEqual(categories, {"categories": []})
        _, comments = self.get_json("/api/comments?id=100")
        self.assertEqual(comments["post_id"], "100")
        self.assertEqual(comments["comment_list"][0]["detail"], "十一点关门")
        self.assertEqual(comments["comment_list"][0]["identity_status"], "anonymous")
        self.assertNotIn("real_user_id", comments["comment_list"][0])
        self.assertEqual(comments["comment_list"][1]["show_user_name"], "某同学")

        _, real_post = self.get_json(f"/api/search?q={quote('食堂')}&limit=10")
        self.assertEqual(real_post["results"][0]["identity_status"], "real")
        _, real_comments = self.get_json("/api/comments?id=100")
        self.assertEqual(real_comments["comment_list"][2]["identity_status"], "real")

    def test_admin_required_api_returns_401_without_session(self):
        status, payload = self.get_json(
            f"/api/search?q={quote('食堂')}&admin_required=1"
        )
        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])
        self.assertIn("管理员登录已失效", payload["error"])

        status, payload = self.get_json("/api/comments?id=100&admin_required=1")
        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])
        self.assertIn("管理员登录已失效", payload["error"])
        status, payload = self.get_json("/api/admin/crawl-status")
        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])
        status, payload = self.get_json("/api/admin/public-access")
        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])

    def test_main_page_contract(self):
        with urlopen(self.base + "/", timeout=5) as response:
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            content = response.read().decode("utf-8")
        self.assertIn("RUC", content)
        self.assertNotIn("__SHARED_UI_", content)
        self.assertEqual(content.count("function updateThemeButton()"), 1)
        self.assertNotIn("女性概率", content)
        self.assertNotIn("gender-method", content)
        self.assertNotIn('id="filter-l2"', content)
        self.assertNotIn("主题:", content)
        self.assertIn('id="scope-content"', content)
        self.assertIn('id="scope-comments"', content)
        self.assertIn('id="scope-all"', content)
        self.assertIn('id="filter-source-state"', content)
        self.assertNotIn("__PUBLIC_ACCESS_JSON__", content)
        self.assertIn('id="date-range-panel"', content)
        self.assertIn('type="date" id="date-from"', content)
        self.assertIn('id="date-filter" data-preset="1d"', content)

    def test_admin_login_contract(self):
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        with opener.open(self.base + "/admin", timeout=5) as response:
            login = response.read().decode("utf-8")
        token = re.search(r'name="csrf_token" value="([^"]+)"', login)
        self.assertIsNotNone(token)
        request = Request(
            self.base + "/admin",
            data=urlencode(
                {"password": "test", "csrf_token": token.group(1)}
            ).encode(),
            method="POST",
        )
        with opener.open(request, timeout=5) as response:
            dashboard = response.read().decode("utf-8")
        self.assertIn("SQLite", dashboard)
        self.assertNotIn("__SHARED_UI_", dashboard)
        self.assertEqual(dashboard.count("function updateThemeButton()"), 1)
        self.assertNotIn('id="live-source"', dashboard)
        self.assertNotIn("function loadUpstreamPreview", dashboard)
        self.assertIn('href="/admin/reports"', dashboard)
        self.assertIn('href="/admin/crawl"', dashboard)
        self.assertIn('href="/admin/ai-search"', dashboard)
        self.assertIn('href="/admin/ai-search/bench"', dashboard)
        self.assertIn('保留管理员登录">公开搜索</a>', dashboard)
        self.assertIn('注销管理员登录">退出登录</a>', dashboard)
        self.assertIn('id="date-range-panel"', dashboard)
        self.assertIn('type="date" id="date-to"', dashboard)
        self.assertIn('id="date-filter" data-preset="1d"', dashboard)
        self.assertIn('<span id="date-filter-summary">今天</span>', dashboard)
        self.assertNotIn("__ADMIN_CSRF_TOKEN__", dashboard)
        with opener.open(self.base + "/api/admin/public-access", timeout=5) as response:
            access_data = json.loads(response.read())
        self.assertEqual(
            access_data["settings"],
            {
                "body_view": True,
                "body_search": True,
                "comments_view": True,
                "comments_search": True,
                "deleted_records": False,
            },
        )
        save_request = Request(
            self.base + "/api/admin/public-access",
            data=json.dumps({"comments_search": False}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": access_data["csrf_token"],
            },
            method="POST",
        )
        with opener.open(save_request, timeout=5) as response:
            saved_access = json.loads(response.read())
        self.assertTrue(saved_access["ok"])
        self.assertFalse(saved_access["settings"]["comments_search"])
        self.assertIn('id="public-access-section"', dashboard)
        with opener.open(self.base + "/admin/crawl", timeout=5) as response:
            crawl_page = response.read().decode("utf-8")
        self.assertIn('id="live-source"', crawl_page)
        self.assertIn('type="number" min="1" step="1"', crawl_page)
        self.assertNotIn('id="q"', crawl_page)
        self.assertNotIn("doSearch()", crawl_page)
        self.assertNotIn("__ADMIN_", crawl_page)
        with opener.open(self.base + "/admin/reports", timeout=5) as response:
            reports_page = response.read().decode("utf-8")
        self.assertIn('id="pending-chart"', reports_page)
        self.assertIn('type="datetime-local"', reports_page)
        self.assertNotIn("__ADMIN_", reports_page)
        with opener.open(self.base + "/api/admin/crawler-report?from=2026-11-02&to=2026-11-04", timeout=5) as response:
            self.assertTrue(json.loads(response.read())["ok"])
        with opener.open(self.base + "/api/admin/crawl-status", timeout=5) as response:
            crawler_status = json.loads(response.read())
        self.assertTrue(crawler_status["ok"])
        self.assertEqual(crawler_status["posts"]["total"], 1)

        # Opening the public page preserves the authenticated administrator
        # session; the separate logout action clears it in the browser.
        with opener.open(self.base + "/", timeout=5) as response:
            self.assertEqual(response.status, 200)
        with opener.open(self.base + "/api/search?q=" + quote("食堂") + "&admin_required=1", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("results", json.loads(response.read()))
        with opener.open(self.base + "/admin?logout=1", timeout=5) as response:
            self.assertIn('name="password"', response.read().decode("utf-8"))
        with self.assertRaises(HTTPError) as error:
            opener.open(self.base + "/api/search?q=test&admin_required=1", timeout=5)
        self.assertEqual(error.exception.code, 401)

    def test_crawler_report_requires_admin_and_rejects_reversed_dates(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base + "/api/admin/crawler-report", timeout=5)
        self.assertEqual(error.exception.code, 401)
        with urlopen(self.base + "/admin/reports", timeout=5) as response:
            self.assertIn('name="password"', response.read().decode("utf-8"))
        token = self.server.RequestHandlerClass.context.auth.create_session()
        request = Request(self.base + "/api/admin/crawler-report?from=2026-11-04&to=2026-11-02", headers={"Cookie": "admin_token=" + token})
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=5)
        self.assertEqual(error.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
