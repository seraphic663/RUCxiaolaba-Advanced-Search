#!/usr/bin/env python3
"""Smoke test SQLitePostStore against a temporary DB."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storage.post_writer import SQLitePostStore  # noqa: E402


def main() -> int:
    path = Path("temp/sqlite_store_test.db")
    if path.exists():
        path.unlink()
    path.parent.mkdir(exist_ok=True)

    post = {
        "id": "9001",
        "content": "今天有人拍毕业照吗，可以免费拍",
        "category_name": "日常投稿",
        "user_name": "某同学test",
        "show_user_id": "u-1",
        "real_user_id": "0",
        "create_time": "2026-06-02 12:00:00",
        "comment_count": 9,
        "star_count": 2,
        "trace_count": 3,
    }
    comments = [
        {
            "id": "c1",
            "detail": "我可以拍毕业照",
            "show_user_name": "某同学A",
            "show_user_id": "u-2",
            "real_user_id": 0,
            "create_time": "2026-06-02 12:01:00",
            "reply_comment_list": [
                {
                    "id": "r1",
                    "detail": "私信你了",
                    "show_user_name": "某同学test",
                    "show_user_id": "u-1",
                    "real_user_id": 0,
                    "reply_show_user_name": "某同学A",
                    "create_time": "2026-06-02 12:02:00",
                    "reply_comment_list": [
                        {
                            "id": "r2",
                            "detail": "收到",
                            "show_user_name": "某同学A",
                            "show_user_id": "u-2",
                            "real_user_id": 0,
                            "reply_show_user_name": "某同学test",
                            "create_time": "2026-06-02 12:03:00",
                        }
                    ],
                }
            ],
        }
    ]

    with SQLitePostStore(path) as store:
        store.init_schema()
        store.upsert_post(post, comments)
        store.set_state("last_mode", "smoke-test")
        assert store.latest_post_id() == "9001"

    con = sqlite3.connect(path)
    try:
        assert con.execute("select count(*) from posts").fetchone()[0] == 1
        assert con.execute("select count(*) from comments").fetchone()[0] == 3
        assert con.execute("select comment_count from posts where id='9001'").fetchone()[0] == 9
        assert not con.execute("select 1 from pragma_table_info('comments') where name='reply_comment_list'").fetchone()
        parent = con.execute("select parent_comment_id from comments where comment_id='r2'").fetchone()[0]
        assert parent == "r1"
        assert not con.execute("select 1 from pragma_table_info('posts') where name='comments_json'").fetchone()
        total = con.execute(
            "select count(distinct post_id) from search_index where body match ?",
            ("毕业照",),
        ).fetchone()[0]
        assert total == 1
        print("sqlite_store smoke test passed", path, path.stat().st_size)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
