"""Cursor page search correctness tests."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.domain.search import SearchQuery
from app.repositories.search_repository import SearchRepository


class CursorSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "posts.db"
        conn = sqlite3.connect(self.db)
        conn.executescript(
            """
            create table posts(
                id text primary key, content text, category_name text,
                user_name text, show_user_id text, real_user_id text,
                create_time text, comment_count integer, star_count integer,
                trace_count integer
            );
            create table comments(
                post_id text, detail text, show_user_id text,
                real_user_id text, reply_show_user_id text,
                show_user_name text, reply_show_user_name text
            );
            """
        )
        rows = [
            ("1", "猫 一", "A", "甲", "u1", "0", "2026-01-01", 0, 3, 0),
            ("2", "普通 二", "A", "乙", "u2", "0", "2026-01-02", 1, 9, 0),
            ("3", "猫 三", "B", "丙", "u3", "0", "2026-01-03", 0, 5, 0),
            ("4", "普通 四", "A", "丁", "u4", "0", "2026-01-04", 1, 7, 0),
            ("5", "猫 五", "A", "戊", "u5", "0", "2026-01-05", 0, 1, 0),
        ]
        conn.executemany("insert into posts values (?,?,?,?,?,?,?,?,?,?)", rows)
        conn.executemany(
            "insert into comments values (?,?,?,?,?,?,?)",
            [
                ("2", "评论有猫", "cu2", "0", "", "评论者", ""),
                ("4", "评论也有猫", "cu4", "0", "", "另一人", ""),
            ],
        )
        conn.commit()
        conn.close()
        self.repository = SearchRepository(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cursor_pages_preserve_time_order(self) -> None:
        request = SearchQuery(text="猫", sort_by="time", limit=2, scope="content")
        first = self.repository.search_cursor(request)
        self.assertEqual(first["pagination_mode"], "cursor")
        self.assertEqual([row["id"] for row in first["results"]], ["5", "3"])
        self.assertTrue(first["has_more"])

        second = self.repository.search_cursor(
            SearchQuery(text="猫", sort_by="time", page=2, limit=2, scope="content"),
            scan_offset=first["next_offset"],
            matched_before=first["matched_so_far"],
        )
        self.assertEqual([row["id"] for row in second["results"]], ["1"])
        self.assertFalse(second["has_more"])
        self.assertEqual(second["total"], 3)

    def test_cursor_pages_preserve_star_order_and_comment_matches(self) -> None:
        request = SearchQuery(text="猫", sort_by="stars", limit=3, scope="all")
        first = self.repository.search_cursor(request)
        self.assertEqual([row["id"] for row in first["results"]], ["2", "4", "3"])
        self.assertEqual(first["candidate_total"], 5)
        self.assertEqual(first["scanned"], 3)

    def test_cursor_results_match_conventional_search(self) -> None:
        conventional = self.repository.search(
            SearchQuery(text="猫", sort_by="time", limit=100, scope="all")
        )
        found: list[str] = []
        offset = matched = 0
        page = 1
        while True:
            result = self.repository.search_cursor(
                SearchQuery(
                    text="猫",
                    sort_by="time",
                    page=page,
                    limit=2,
                    scope="all",
                ),
                scan_offset=offset,
                matched_before=matched,
            )
            found.extend(row["id"] for row in result["results"])
            if not result["has_more"]:
                break
            offset = result["next_offset"]
            matched = result["matched_so_far"]
            page += 1
        self.assertEqual(found, [row["id"] for row in conventional["results"]])

    def test_non_text_filter_defines_candidate_total(self) -> None:
        result = self.repository.search_cursor(
            SearchQuery(text="猫", category="A", limit=10)
        )
        self.assertEqual(result["candidate_total"], 4)
        self.assertEqual([row["id"] for row in result["results"]], ["5", "1"])

    def test_empty_query_cursor_skips_exact_total(self) -> None:
        first = self.repository.search_cursor(
            SearchQuery(text="", sort_by="time", limit=2)
        )
        self.assertEqual(first["pagination_mode"], "cursor")
        self.assertEqual(first["search_backend"], "cursor")
        self.assertIsNone(first["candidate_total"])
        self.assertFalse(first["total_exact"])
        self.assertEqual([row["id"] for row in first["results"]], ["5", "4"])

        second = self.repository.search_cursor(
            SearchQuery(text="", sort_by="time", page=2, limit=2),
            scan_offset=first["next_offset"],
            matched_before=first["matched_so_far"],
        )
        self.assertEqual([row["id"] for row in second["results"]], ["3", "2"])


if __name__ == "__main__":
    unittest.main()
