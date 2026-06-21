"""Contract checks for the committed synthetic quick-start databases."""

import sqlite3
import unittest
from pathlib import Path

from app.services.search_service import SearchService

ROOT = Path(__file__).resolve().parent.parent
POSTS_DB = ROOT / "demo" / "posts.db"
BIGRAM_DB = ROOT / "demo" / "bigram_index.db"


class DemoDataTests(unittest.TestCase):
    def test_demo_databases_are_valid_and_synthetic(self) -> None:
        self.assertTrue(POSTS_DB.exists())
        self.assertTrue(BIGRAM_DB.exists())
        with sqlite3.connect(POSTS_DB) as conn:
            self.assertEqual(conn.execute("pragma integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("select count(*) from posts").fetchone()[0], 12)
            self.assertEqual(conn.execute("select count(*) from comments").fetchone()[0], 20)
            marker = conn.execute(
                "select value from crawl_state where key='demo.synthetic'"
            ).fetchone()
            self.assertEqual(marker, ("true",))
            identities = conn.execute(
                """
                select count(*) from posts
                where show_user_id not in ('', '0') or real_user_id not in ('', '0')
                """
            ).fetchone()[0]
            self.assertEqual(identities, 0)
        with sqlite3.connect(BIGRAM_DB) as conn:
            self.assertEqual(conn.execute("pragma integrity_check").fetchone()[0], "ok")
            version = conn.execute(
                "select value from index_meta where key='schema_version'"
            ).fetchone()
            self.assertEqual(version, ("bigram-v1",))

    def test_demo_bigram_search(self) -> None:
        service = SearchService(POSTS_DB, BIGRAM_DB)
        result = service.search("食堂", "time", 1, 20, scope="all")
        self.assertEqual(result["search_backend"], "bigram")
        self.assertGreaterEqual(result["total"], 2)


if __name__ == "__main__":
    unittest.main()
