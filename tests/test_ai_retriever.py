import sqlite3
import tempfile
import unittest
from pathlib import Path

from ai_retriever import retrieve_ai


class AIRetrieverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "posts.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            create table posts (
                id text primary key,
                content text,
                category_name text,
                user_name text,
                create_time text,
                comment_count integer,
                star_count integer
            );
            create table comments (
                post_id text,
                detail text,
                show_user_name text,
                create_time text,
                is_publisher integer
            );
            create virtual table search_index using fts5(
                post_id unindexed, kind unindexed, body, tokenize='trigram'
            );
            """
        )
        posts = [
            ("1", "\u5b66\u6821\u4eca\u5929\u4e0b\u96e8", "", "u1", "2026-06-07 12:00:00", 0, 0),
            ("2", "\u5b66\u6821\u6253\u5370\u5e97\u65e9\u4e0a\u4e03\u70b9\u5f00\u95e8", "", "u2", "2026-06-06 12:00:00", 1, 0),
            ("3", "\u516d\u4e00\u6d3b\u52a8\u7559\u8a00", "", "u3", "2026-06-05 12:00:00", 1, 0),
        ]
        conn.executemany("insert into posts values (?,?,?,?,?,?,?)", posts)
        conn.executemany(
            "insert into search_index values (?,?,?)",
            [(p[0], "post", p[1]) for p in posts],
        )
        conn.execute(
            "insert into comments values (?,?,?,?,?)",
            ("3", "\u62ff\u5230\u4e86\u516d\u4e00\u7cd6\u679c\uff0c\u8c22\u8c22\u7559\u8a00", "u4", "2026-06-07 10:00:00", 0),
        )
        conn.execute(
            "insert into search_index values (?,?,?)",
            ("3", "comment", "\u62ff\u5230\u4e86\u516d\u4e00\u7cd6\u679c\uff0c\u8c22\u8c22\u7559\u8a00"),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_multiple_keyword_hits_beat_generic_newer_post(self):
        rows = retrieve_ai(
            "\u5b66\u6821\u6253\u5370\u5e97\u51e0\u70b9\u5f00\u95e8", self.db_path, limit=3
        )
        self.assertEqual(rows[0]["post"]["id"], "2")

    def test_comment_matches_contribute_to_ranking_and_context(self):
        rows = retrieve_ai("\u516d\u4e00\u7cd6\u679c\u7559\u8a00", self.db_path, limit=3)
        self.assertEqual(rows[0]["post"]["id"], "3")
        self.assertTrue(rows[0]["matched_comments"])
        self.assertGreater(rows[0]["comment_match_count"], 0)
        self.assertIn("\u7559\u8a00", rows[0]["body_match_terms"])


if __name__ == "__main__":
    unittest.main()
