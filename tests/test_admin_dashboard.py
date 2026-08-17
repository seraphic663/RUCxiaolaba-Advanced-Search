from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.services.admin_service import AdminService


class AdminDashboardTest(unittest.TestCase):
    def test_dashboard_returns_only_currently_used_total(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "posts.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                "create table posts("
                "id text primary key, content text, category_name text, "
                "user_name text, show_user_id text, create_time text, "
                "star_count integer, comment_count integer)"
            )
            conn.execute("create table comments(id text primary key)")
            conn.executemany(
                "insert into posts("
                "id, content, category_name, user_name, show_user_id, "
                "create_time, star_count, comment_count) "
                "values (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("1", "正文", "日常", "甲", "u1", "2026-01-01", 0, 0),
                    ("2", "正文", "日常", "甲", "u1", "2026-01-02", 0, 0),
                    ("3", "正文", "日常", "乙", "u2", "2026-01-03", 0, 0),
                ],
            )
            conn.commit()
            conn.close()

            self.assertEqual(AdminService(db_path).dashboard(), {"total": 3})


if __name__ == "__main__":
    unittest.main()
