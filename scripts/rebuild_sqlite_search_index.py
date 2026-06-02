#!/usr/bin/env python3
"""Rebuild SQLite FTS search index for posts and comments."""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma synchronous=normal")
    conn.execute("pragma temp_store=memory")
    conn.execute("pragma cache_size=-200000")
    return conn


def rebuild(db_path: Path, batch_size: int = 10000) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(db_path)

    t0 = time.time()
    conn = connect(db_path)
    stats = {"post_rows": 0, "comment_rows": 0}
    try:
        conn.executescript(
            """
            drop table if exists search_index;
            create virtual table search_index using fts5(
                post_id unindexed,
                kind unindexed,
                body,
                tokenize='trigram'
            );
            """
        )
        conn.commit()

        insert_sql = "insert into search_index(post_id, kind, body) values (?,?,?)"

        print("[search-index] indexing post bodies...", flush=True)
        cur = conn.execute("select id, content from posts where content != ''")
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            conn.executemany(insert_sql, ((pid, "post", body) for pid, body in rows))
            conn.commit()
            stats["post_rows"] += len(rows)
            if stats["post_rows"] % (batch_size * 10) == 0:
                print(f"[search-index] posts={stats['post_rows']:,}", flush=True)

        print("[search-index] indexing comments...", flush=True)
        cur = conn.execute("select post_id, detail from comments where detail != ''")
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            conn.executemany(insert_sql, ((pid, "comment", detail) for pid, detail in rows))
            conn.commit()
            stats["comment_rows"] += len(rows)
            if stats["comment_rows"] % (batch_size * 20) == 0:
                print(f"[search-index] comments={stats['comment_rows']:,}", flush=True)

        conn.execute("insert or replace into crawl_state values (?,?,datetime('now','localtime'))", ("search_index", "fts5_trigram",))
        conn.commit()
        conn.execute("insert into search_index(search_index) values ('optimize')")
        conn.commit()
    finally:
        conn.close()

    stats["elapsed_sec"] = round(time.time() - t0, 2)
    return stats


def verify(db_path: Path, query: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        t0 = time.time()
        total = conn.execute(
            "select count(distinct post_id) from search_index where body match ?",
            (query,),
        ).fetchone()[0]
        sample = conn.execute(
            """
            select p.id, substr(p.content, 1, 80)
            from posts p
            where p.id in (select post_id from search_index where body match ?)
            order by cast(p.id as integer) desc
            limit 3
            """,
            (query,),
        ).fetchall()
        print(f"query={query!r} total={total:,} elapsed={time.time() - t0:.3f}s")
        for row in sample:
            print(f"  #{row[0]} {row[1]}")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild SQLite FTS search index")
    parser.add_argument("--db-path", default="data/posts.db")
    parser.add_argument("--batch-size", type=int, default=10000)
    parser.add_argument("--verify", default="???")
    args = parser.parse_args()

    db_path = Path(args.db_path)
    stats = rebuild(db_path, args.batch_size)
    print("[done]", stats)
    if args.verify:
        verify(db_path, args.verify)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
