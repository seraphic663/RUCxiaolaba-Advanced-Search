"""Focused tests for the flexible Phase 1 ID scanner."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crawler_db  # noqa: E402
from storage.post_writer import SQLitePostStore  # noqa: E402


def args_for(db_path: Path, config_path: Path, **overrides) -> argparse.Namespace:
    values = {
        "db_path": str(db_path),
        "init_schema": False,
        "lock_timeout": 2,
        "config": str(config_path),
        "from_date": "2026-06-01",
        "to_date": "",
        "start_id": 0,
        "end_id": 902,
        "workers": 2,
        "chunk_size": 3,
        "restart": False,
        "dry_run": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class Phase1Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path = root / "posts.db"
        self.config_path = root / "config.txt"
        self.config_path.write_text("ys7_ysxy_session=test\n", encoding="utf-8")
        with SQLitePostStore(self.db_path) as store:
            store.init_schema()
            store.upsert_post(
                {
                    "id": "1000",
                    "content": "range anchor",
                    "create_time": "2026-06-01 00:00:00",
                },
                [],
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_date_range_writes_post_comments_search_and_checkpoint(self) -> None:
        def fake_api_get(_session, _path, params=None):
            post_id = str(params["id"])
            if post_id == "900":
                return {
                    "community_id": "4",
                    "title": "new post",
                    "detail": "body",
                    "create_time": "2026-06-01 01:00:00",
                    "count_comment": 1,
                    "comment_list": [{"id": "c1", "detail": "reply"}],
                }, None
            if post_id == "901":
                return {"community_id": "5"}, None
            return None, "not_found"

        with patch.object(crawler_db, "api_get", side_effect=fake_api_get):
            self.assertEqual(crawler_db.command_phase1(args_for(self.db_path, self.config_path)), 0)

        with SQLitePostStore(self.db_path) as store:
            self.assertEqual(store.conn.execute("select count(1) from posts").fetchone()[0], 2)
            self.assertEqual(store.conn.execute("select count(1) from comments").fetchone()[0], 1)
            self.assertEqual(
                store.conn.execute("select count(1) from search_index where post_id='900'").fetchone()[0],
                2,
            )
            state = json.loads(
                store.conn.execute(
                    "select value from crawl_state where key='crawler_db_phase1_900_902'"
                ).fetchone()[0]
            )
        self.assertTrue(state["complete"])
        self.assertEqual(state["next_id"], 903)
        self.assertEqual(state["new"], 1)
        self.assertEqual(state["foreign"], 1)
        self.assertEqual(state["missing"], 1)

    def test_error_keeps_checkpoint_at_chunk_start(self) -> None:
        def failing_api_get(_session, _path, params=None):
            if str(params["id"]) == "901":
                return None, "temporary failure"
            return None, "not_found"

        scan_args = args_for(
            self.db_path,
            self.config_path,
            from_date="",
            start_id=900,
            end_id=902,
        )
        with patch.object(crawler_db, "api_get", side_effect=failing_api_get):
            with self.assertRaisesRegex(RuntimeError, "retry from id 900"):
                crawler_db.command_phase1(scan_args)

        with SQLitePostStore(self.db_path) as store:
            state = json.loads(
                store.conn.execute(
                    "select value from crawl_state where key='crawler_db_phase1_900_902'"
                ).fetchone()[0]
            )
        self.assertFalse(state["complete"])
        self.assertEqual(state["next_id"], 900)
        self.assertEqual(state["errors"], 1)


if __name__ == "__main__":
    unittest.main()
