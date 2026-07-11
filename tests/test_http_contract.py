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
            "insert into posts values (?,?,?,?,?,?,?,?,?,?)",
            (
                "100",
                "食堂今天开门",
                "日常",
                "某同学",
                "u1",
                "0",
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
        self.assertNotIn("show_user_id", payload["results"][0])

        status, cursor = self.get_json(
            f"/api/search?q={quote('食')}&limit=10&cursor=1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(cursor["pagination_mode"], "cursor")
        self.assertEqual(cursor["candidate_total"], 1)
        self.assertTrue(cursor["total_exact"])

    def test_comments_and_categories_contract(self):
        _, categories = self.get_json("/api/categories")
        self.assertEqual(categories, {"categories": []})
        _, comments = self.get_json("/api/comments?id=100")
        self.assertEqual(comments["post_id"], "100")
        self.assertEqual(comments["comment_list"][0]["detail"], "十一点关门")
        self.assertNotIn("real_user_id", comments["comment_list"][0])
        self.assertEqual(comments["comment_list"][1]["show_user_name"], "某同学")

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

    def test_main_page_contract(self):
        with urlopen(self.base + "/", timeout=5) as response:
            content = response.read().decode("utf-8")
        self.assertIn("RUC", content)
        self.assertNotIn("__SHARED_UI_", content)
        self.assertEqual(content.count("function updateThemeButton()"), 1)

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
        self.assertIn("上游候选与人工现爬", dashboard)
        self.assertNotIn("__ADMIN_CSRF_TOKEN__", dashboard)


if __name__ == "__main__":
    unittest.main()
