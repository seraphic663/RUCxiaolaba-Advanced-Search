#!/usr/bin/env python3
"""Build a lossless slim SQLite DB from a full posts.db.

The slim DB removes posts.comments_json, but keeps comments.raw_json so original
comment API fields remain available without storing the same comments twice.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import time
from pathlib import Path

POST_COLUMNS = [
    "id", "content", "category_name", "user_name", "show_user_id",
    "show_user_head", "real_user_id", "create_time", "comment_count",
    "star_count", "trace_count", "views", "hot", "updated_at",
]

COMMENT_COLUMNS = [
    "row_key", "comment_id", "post_id", "parent_comment_id", "detail",
    "show_user_name", "show_user_id", "real_user_id", "reply_show_user_name",
    "reply_show_user_id", "is_publisher", "create_time", "raw_json", "updated_at",
]


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("pragma journal_mode=off")
    conn.execute("pragma synchronous=off")
    conn.execute("pragma temp_store=memory")
    conn.execute("pragma cache_size=-200000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table posts (
            id text primary key,
            content text not null,
            category_name text not null,
            user_name text not null,
            show_user_id text not null,
            show_user_head text not null,
            real_user_id text not null,
            create_time text not null,
            comment_count integer not null,
            star_count integer not null,
            trace_count integer not null,
            views integer not null,
            hot integer not null,
            updated_at text not null
        );

        create table comments (
            row_key text primary key,
            comment_id text not null,
            post_id text not null,
            parent_comment_id text not null,
            detail text not null,
            show_user_name text not null,
            show_user_id text not null,
            real_user_id text not null,
            reply_show_user_name text not null,
            reply_show_user_id text not null,
            is_publisher integer not null,
            create_time text not null,
            raw_json text not null,
            updated_at text not null
        );

        create table crawl_state (
            key text primary key,
            value text not null,
            updated_at text not null
        );
        """
    )


def create_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create index idx_posts_create_time on posts(create_time);
        create index idx_posts_hot on posts(hot desc, id desc);
        create index idx_posts_views on posts(views desc, id desc);
        create index idx_posts_stars on posts(star_count desc, id desc);
        create index idx_posts_category on posts(category_name);
        create index idx_comments_post_id on comments(post_id);
        create index idx_comments_create_time on comments(create_time);
        """
    )


def create_search_index(conn: sqlite3.Connection, batch_size: int) -> tuple[int, int]:
    conn.executescript(
        """
        create virtual table search_index using fts5(
            post_id unindexed,
            kind unindexed,
            body,
            tokenize='trigram'
        );
        """
    )
    insert_sql = "insert into search_index(post_id, kind, body) values (?,?,?)"
    post_rows = 0
    comment_rows = 0

    print("[slim] indexing post bodies...", flush=True)
    cur = conn.execute("select id, content from posts where content != ''")
    while True:
        rows = cur.fetchmany(batch_size)
        if not rows:
            break
        conn.executemany(insert_sql, ((pid, "post", body) for pid, body in rows))
        conn.commit()
        post_rows += len(rows)
        if post_rows % (batch_size * 10) == 0:
            print(f"[slim] indexed posts={post_rows:,}", flush=True)

    print("[slim] indexing comments...", flush=True)
    cur = conn.execute("select post_id, detail from comments where detail != ''")
    while True:
        rows = cur.fetchmany(batch_size)
        if not rows:
            break
        conn.executemany(insert_sql, ((pid, "comment", detail) for pid, detail in rows))
        conn.commit()
        comment_rows += len(rows)
        if comment_rows % (batch_size * 20) == 0:
            print(f"[slim] indexed comments={comment_rows:,}", flush=True)

    conn.execute("insert into search_index(search_index) values ('optimize')")
    conn.commit()
    return post_rows, comment_rows


def build_slim(source: Path, target: Path, batch_size: int) -> dict:
    if not source.exists():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()

    t0 = time.time()
    conn = connect(tmp)
    try:
        init_schema(conn)
        conn.execute("attach database ? as src", (str(source),))
        post_cols = ",".join(POST_COLUMNS)
        comment_cols = ",".join(COMMENT_COLUMNS)
        print("[slim] copying posts without comments_json...", flush=True)
        conn.execute(f"insert into posts({post_cols}) select {post_cols} from src.posts")
        print("[slim] copying comments with raw_json...", flush=True)
        conn.execute(f"insert into comments({comment_cols}) select {comment_cols} from src.comments")
        print("[slim] copying crawl_state...", flush=True)
        conn.execute("insert into crawl_state select * from src.crawl_state")
        conn.execute(
            "insert or replace into crawl_state values (?,?,datetime('now','localtime'))",
            ("slim_source", str(source)),
        )
        conn.commit()
        conn.execute("detach database src")

        print("[slim] creating indexes...", flush=True)
        create_indexes(conn)
        conn.commit()
        post_index_rows, comment_index_rows = create_search_index(conn, batch_size)
        stats = {
            "posts": conn.execute("select count(*) from posts").fetchone()[0],
            "comments": conn.execute("select count(*) from comments").fetchone()[0],
            "search_index_rows": conn.execute("select count(*) from search_index").fetchone()[0],
            "indexed_posts": post_index_rows,
            "indexed_comments": comment_index_rows,
        }
    finally:
        conn.close()

    if target.exists():
        backup = target.with_suffix(target.suffix + ".bak")
        if backup.exists():
            backup.unlink()
        target.replace(backup)
    os.replace(tmp, target)
    stats["size_bytes"] = target.stat().st_size
    stats["elapsed_sec"] = round(time.time() - t0, 2)
    return stats


def verify(path: Path, query: str) -> None:
    conn = sqlite3.connect(path)
    try:
        print(f"size={path.stat().st_size:,}")
        print("posts=", conn.execute("select count(*) from posts").fetchone()[0])
        print("comments=", conn.execute("select count(*) from comments").fetchone()[0])
        print("has_comments_json=", bool(conn.execute("select 1 from pragma_table_info('posts') where name='comments_json'").fetchone()))
        print("has_raw_json=", bool(conn.execute("select 1 from pragma_table_info('comments') where name='raw_json'").fetchone()))
        if query:
            total = conn.execute(
                "select count(distinct post_id) from search_index where body match ?",
                (query,),
            ).fetchone()[0]
            print(f"query={query!r} total={total}")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build lossless slim DB from full SQLite DB")
    parser.add_argument("--source", default="data/posts.db")
    parser.add_argument("--target", default="data/posts.slim.db")
    parser.add_argument("--batch-size", type=int, default=20000)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--verify-query", default="毕业照")
    args = parser.parse_args()

    source = Path(args.source)
    target = Path(args.target)
    if not args.verify_only:
        print("[done]", build_slim(source, target, args.batch_size))
    verify(target, args.verify_query)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
