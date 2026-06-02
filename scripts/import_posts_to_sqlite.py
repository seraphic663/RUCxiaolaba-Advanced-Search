#!/usr/bin/env python3
"""Build a production SQLite database from posts_final.csv.

This is an offline migration helper. It writes to a temporary database first and
atomically replaces the target only after a successful import.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

POST_COLUMNS = [
    "id", "content", "category_name", "user_name", "show_user_id",
    "show_user_head", "real_user_id", "create_time", "comment_count",
    "star_count", "trace_count", "views", "hot", "comments_json", "updated_at",
]


def safe_int(value, default=0):
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def now_text():
    return time.strftime("%Y-%m-%d %H:%M:%S")


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
        drop table if exists posts;
        drop table if exists comments;
        drop table if exists crawl_state;

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
            comments_json text not null,
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


def normalize_post(row: dict, updated_at: str) -> tuple:
    return (
        row.get("id", ""),
        row.get("content", ""),
        row.get("category_name", ""),
        row.get("user_name", ""),
        row.get("show_user_id", ""),
        row.get("show_user_head", ""),
        row.get("real_user_id", "0"),
        row.get("create_time", ""),
        safe_int(row.get("comment_count")),
        safe_int(row.get("star_count")),
        safe_int(row.get("trace_count")),
        safe_int(row.get("views")),
        safe_int(row.get("hot")),
        row.get("comments_json", "[]"),
        updated_at,
    )


def comment_time(item: dict) -> str:
    return str(item.get("create_time") or item.get("show_create_time") or item.get("update_time") or "")


def flatten_comments(post_id: str, comments_json: str, updated_at: str) -> tuple[list[tuple], bool]:
    try:
        comments = json.loads(comments_json or "[]")
    except Exception:
        return [], False
    if not isinstance(comments, list):
        return [], False

    rows = []
    for idx, c in enumerate(comments):
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or f"{post_id}-c-{idx}")
        rows.append(comment_row(post_id, "", cid, c, updated_at, f"{post_id}:{cid}"))
        replies = c.get("reply_comment_list") or []
        if isinstance(replies, list):
            for ridx, r in enumerate(replies):
                if not isinstance(r, dict):
                    continue
                rid = str(r.get("id") or f"{cid}-r-{ridx}")
                rows.append(comment_row(post_id, cid, rid, r, updated_at, f"{post_id}:{cid}:{rid}"))
    return rows, True


def comment_row(post_id: str, parent_id: str, comment_id: str, item: dict, updated_at: str, row_key: str) -> tuple:
    return (
        row_key,
        comment_id,
        post_id,
        parent_id,
        str(item.get("detail") or ""),
        str(item.get("show_user_name") or ""),
        str(item.get("show_user_id") or ""),
        str(item.get("real_user_id") or "0"),
        str(item.get("reply_show_user_name") or ""),
        str(item.get("reply_show_user_id") or ""),
        safe_int(item.get("is_publisher")),
        comment_time(item),
        json.dumps(item, ensure_ascii=False, separators=(",", ":")),
        updated_at,
    )


def import_csv(csv_path: Path, db_path: Path, limit: int, batch_size: int) -> dict:
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    updated_at = now_text()
    post_sql = "insert into posts values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    comment_sql = "insert or replace into comments values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"

    stats = {
        "posts": 0,
        "comments": 0,
        "skipped_rows": 0,
        "bad_comment_json": 0,
        "started_at": updated_at,
    }

    t0 = time.time()
    conn = connect(tmp_path)
    try:
        init_schema(conn)
        post_batch = []
        comment_batch = []

        csv.field_size_limit(10 ** 9)
        with csv_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                aid = row.get("id", "")
                if not aid or not aid.isdigit() or row.get(None):
                    stats["skipped_rows"] += 1
                    continue

                post_batch.append(normalize_post(row, updated_at))
                comments, ok = flatten_comments(aid, row.get("comments_json", "[]"), updated_at)
                if not ok:
                    stats["bad_comment_json"] += 1
                comment_batch.extend(comments)

                if len(post_batch) >= batch_size:
                    conn.executemany(post_sql, post_batch)
                    conn.executemany(comment_sql, comment_batch)
                    conn.commit()
                    stats["posts"] += len(post_batch)
                    stats["comments"] += len(comment_batch)
                    post_batch.clear()
                    comment_batch.clear()
                    if stats["posts"] % (batch_size * 10) == 0:
                        elapsed = max(time.time() - t0, 0.001)
                        rate = stats["posts"] / elapsed
                        print(f"[import] posts={stats['posts']:,} comments={stats['comments']:,} rate={rate:.0f}/s", flush=True)

                if limit and stats["posts"] + len(post_batch) >= limit:
                    break

        if post_batch:
            conn.executemany(post_sql, post_batch)
            conn.executemany(comment_sql, comment_batch)
            conn.commit()
            stats["posts"] += len(post_batch)
            stats["comments"] += len(comment_batch)

        print("[import] creating indexes...", flush=True)
        create_indexes(conn)
        conn.execute("insert into crawl_state values (?,?,?)", ("source_csv", str(csv_path), updated_at))
        conn.execute("insert into crawl_state values (?,?,?)", ("import_stats", json.dumps(stats, ensure_ascii=False), updated_at))
        conn.commit()
    finally:
        conn.close()

    if db_path.exists():
        backup = db_path.with_suffix(db_path.suffix + ".bak")
        if backup.exists():
            backup.unlink()
        db_path.replace(backup)
    os.replace(tmp_path, db_path)
    stats["elapsed_sec"] = round(time.time() - t0, 2)
    return stats


def verify(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        post_count = conn.execute("select count(*) from posts").fetchone()[0]
        comment_count = conn.execute("select count(*) from comments").fetchone()[0]
        minmax = conn.execute("select min(create_time), max(create_time) from posts").fetchone()
        latest = conn.execute("select id, create_time, substr(content,1,80) from posts order by cast(id as integer) desc limit 5").fetchall()
        print(f"posts={post_count:,}")
        print(f"comments={comment_count:,}")
        print(f"time_range={minmax[0]} ~ {minmax[1]}")
        print("latest:")
        for row in latest:
            print(f"  #{row[0]} {row[1]} {row[2]}")
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import posts_final.csv into SQLite")
    parser.add_argument("--csv-path", default="data/posts_final.csv")
    parser.add_argument("--db-path", default="data/posts.db")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument("--verify-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db_path)
    if args.verify_only:
        verify(db_path)
        return 0
    stats = import_csv(Path(args.csv_path), db_path, args.limit, args.batch_size)
    print("[done]", json.dumps(stats, ensure_ascii=False, indent=2))
    verify(db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
