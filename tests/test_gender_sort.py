from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.domain.search import SearchQuery
from app.repositories.search_repository import SearchRepository


METHODS = ("combined", "rule", "context", "anchor", "pu", "thread_prior", "llm")


class GenderSortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.posts_db = root / "posts.db"
        self.gender_db = root / "gender.db"

        conn = sqlite3.connect(self.posts_db)
        conn.executescript(
            """
            create table posts(
                id text primary key, content text, category_name text,
                user_name text, show_user_id text, real_user_id text,
                create_time text, comment_count integer, star_count integer,
                trace_count integer
            );
            create table comments(
                row_key text primary key, comment_id text, post_id text,
                parent_comment_id text, detail text, show_user_name text,
                show_user_id text, real_user_id text,
                reply_show_user_name text, reply_show_user_id text,
                is_publisher integer, create_time text
            );
            """
        )
        conn.executemany(
            "insert into posts values (?,?,?,?,?,?,?,?,?,?)",
            [
                ("1", "one", "", "a", "", "", "2026-01-01", 0, 0, 0),
                ("2", "two", "", "b", "", "", "2026-01-02", 0, 0, 0),
                ("3", "three", "", "c", "", "", "2026-01-03", 0, 0, 0),
            ],
        )
        conn.executemany(
            "insert into comments values (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("3:c1", "c1", "3", "", "old", "u", "", "", "", "", 0, "2026-01-01"),
                ("3:c2", "c2", "3", "", "new", "u", "", "", "", "", 0, "2026-01-02"),
                ("3:c3", "c3", "3", "", "high", "u", "", "", "", "", 0, "2026-01-03"),
            ],
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(self.gender_db)
        columns = [
            "unit_id text primary key", "unit_type text", "post_id text", "row_key text"
        ]
        for method in METHODS:
            columns.extend(
                [
                    f"female_{method} real", f"male_{method} real",
                    f"unknown_{method} real",
                ]
            )
        columns.extend(["rule_label text", "rule_tier text", "llm_label text", "source text"])
        conn.execute(f"create table gender_scores({','.join(columns)})")

        def row(unit_id, unit_type, post_id, row_key, female, unknown=0.2):
            values = [unit_id, unit_type, post_id, row_key]
            for _ in METHODS:
                values.extend([female, 1 - female - unknown, unknown])
            values.extend(["unknown", "none", None, "test"])
            return values

        conn.executemany(
            "insert into gender_scores values (" + ",".join("?" for _ in range(4 + 3 * len(METHODS) + 4)) + ")",
            [
                row("post:1", "post", "1", None, 0.2),
                row("post:2", "post", "2", None, 0.8),
                row("post:3", "post", "3", None, 0.5),
                row("comment:3:c1", "comment", "3", "3:c1", 0.2),
                row("comment:3:c2", "comment", "3", "3:c2", 0.2),
                row("comment:3:c3", "comment", "3", "3:c3", 0.8),
            ],
        )
        conn.commit()
        conn.close()
        self.repository = SearchRepository(self.posts_db, gender_db=self.gender_db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_posts_sort_high_low_and_low_high(self) -> None:
        high = self.repository.search_cursor(
            SearchQuery(text="", sort_by="female_desc", limit=3, admin=True)
        )
        low = self.repository.search_cursor(
            SearchQuery(text="", sort_by="female_asc", limit=3, admin=True)
        )
        self.assertEqual([row["id"] for row in high["results"]], ["2", "3", "1"])
        self.assertEqual([row["id"] for row in low["results"]], ["1", "3", "2"])
        self.assertEqual(high["results"][0]["gender"]["female"], 0.8)

    def test_comment_score_ties_break_newest_first(self) -> None:
        result = self.repository.comments("3", admin=True, gender_sort="female_asc")
        self.assertEqual([row["comment_id"] for row in result["comment_list"]], ["c2", "c1", "c3"])
        result = self.repository.comments("3", admin=True, gender_sort="female_desc")
        self.assertEqual([row["comment_id"] for row in result["comment_list"]], ["c3", "c2", "c1"])

    def test_public_results_hide_gender_metadata_and_ignore_gender_sort(self) -> None:
        result = self.repository.search_cursor(
            SearchQuery(text="", sort_by="female_desc", limit=3)
        )
        self.assertEqual([row["id"] for row in result["results"]], ["3", "2", "1"])
        self.assertNotIn("gender", result["results"][0])
        self.assertNotIn("female_probability", result["results"][0])

        comments = self.repository.comments("3", gender_sort="female_desc")
        self.assertEqual(
            [row["comment_id"] for row in comments["comment_list"]],
            ["c1", "c2", "c3"],
        )
        self.assertNotIn("gender", comments["comment_list"][0])
        self.assertNotIn("gender_sort", comments)


if __name__ == "__main__":
    unittest.main()
