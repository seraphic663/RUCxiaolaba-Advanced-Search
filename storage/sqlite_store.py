"""SQLite writer for the future DB-first crawler pipeline.

The store targets the slim production schema by default. It also tolerates the
older full schema by filling posts.comments_json when that column exists.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable


def safe_int(value, default=0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def comment_time(item: dict) -> str:
    return str(item.get("create_time") or item.get("show_create_time") or item.get("update_time") or "")


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


def flatten_comments(post_id: str, comments: Iterable[dict], updated_at: str) -> list[tuple]:
    rows: list[tuple] = []
    for idx, item in enumerate(comments or []):
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or f"{post_id}-c-{idx}")
        rows.append(comment_row(post_id, "", cid, item, updated_at, f"{post_id}:{cid}"))
        replies = item.get("reply_comment_list") or []
        if isinstance(replies, list):
            for ridx, reply in enumerate(replies):
                if not isinstance(reply, dict):
                    continue
                rid = str(reply.get("id") or f"{cid}-r-{ridx}")
                rows.append(comment_row(post_id, cid, rid, reply, updated_at, f"{post_id}:{cid}:{rid}"))
    return rows


class SQLitePostStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma journal_mode=wal")
        self.conn.execute("pragma synchronous=normal")
        self.conn.execute("pragma foreign_keys=off")
        self._post_columns = self._columns("posts") if self._table_exists("posts") else set()
        self._has_search_index = self._table_exists("search_index")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "SQLitePostStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "select 1 from sqlite_master where type='table' and name=?",
            (name,),
        ).fetchone()
        return row is not None

    def _columns(self, table: str) -> set[str]:
        return {row[1] for row in self.conn.execute(f"pragma table_info({table})")}

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            create table if not exists posts (
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

            create table if not exists comments (
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

            create table if not exists crawl_state (
                key text primary key,
                value text not null,
                updated_at text not null
            );

            create index if not exists idx_posts_create_time on posts(create_time);
            create index if not exists idx_posts_hot on posts(hot desc, id desc);
            create index if not exists idx_posts_views on posts(views desc, id desc);
            create index if not exists idx_posts_stars on posts(star_count desc, id desc);
            create index if not exists idx_posts_category on posts(category_name);
            create index if not exists idx_comments_post_id on comments(post_id);
            create index if not exists idx_comments_create_time on comments(create_time);
            """
        )
        if not self._table_exists("search_index"):
            self.conn.execute(
                """
                create virtual table search_index using fts5(
                    post_id unindexed,
                    kind unindexed,
                    body,
                    tokenize='trigram'
                )
                """
            )
        self.conn.commit()
        self._post_columns = self._columns("posts")
        self._has_search_index = True

    def upsert_post(self, post: dict, comments: list[dict] | None = None, commit: bool = True) -> None:
        updated_at = now_text()
        post_id = str(post.get("id") or "")
        if not post_id:
            raise ValueError("post id is required")

        values = {
            "id": post_id,
            "content": str(post.get("content") or ""),
            "category_name": str(post.get("category_name") or post.get("category") or ""),
            "user_name": str(post.get("user_name") or post.get("user") or ""),
            "show_user_id": str(post.get("show_user_id") or ""),
            "show_user_head": str(post.get("show_user_head") or ""),
            "real_user_id": str(post.get("real_user_id") or "0"),
            "create_time": str(post.get("create_time") or post.get("time") or ""),
            "comment_count": safe_int(post.get("comment_count", post.get("comments", 0))),
            "star_count": safe_int(post.get("star_count", post.get("stars", 0))),
            "trace_count": safe_int(post.get("trace_count", post.get("trace", 0))),
            "views": safe_int(post.get("views")),
            "hot": safe_int(post.get("hot")),
            "updated_at": updated_at,
        }
        if "comments_json" in self._post_columns:
            values["comments_json"] = json.dumps(comments or [], ensure_ascii=False, separators=(",", ":"))

        columns = [col for col in values if col in self._post_columns]
        placeholders = ",".join("?" for _ in columns)
        update_sql = ",".join(f"{col}=excluded.{col}" for col in columns if col != "id")
        self.conn.execute(
            f"insert into posts({','.join(columns)}) values ({placeholders}) "
            f"on conflict(id) do update set {update_sql}",
            [values[col] for col in columns],
        )
        if comments is not None:
            self.replace_comments(
                post_id,
                comments,
                updated_at=updated_at,
                comment_count=values["comment_count"],
                commit=False,
            )
        self.refresh_search_index(post_id, values["content"], comments, commit=False)
        if commit:
            self.conn.commit()

    def replace_comments(
        self,
        post_id: str,
        comments: list[dict],
        updated_at: str | None = None,
        comment_count: int | None = None,
        commit: bool = True,
    ) -> None:
        updated_at = updated_at or now_text()
        rows = flatten_comments(post_id, comments, updated_at)
        self.conn.execute("delete from comments where post_id=?", (post_id,))
        if rows:
            self.conn.executemany(
                "insert into comments values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        if comment_count is None:
            comment_count = len(rows)
        self.conn.execute(
            "update posts set comment_count=?, updated_at=? where id=?",
            (comment_count, updated_at, post_id),
        )
        if commit:
            self.conn.commit()

    def refresh_search_index(self, post_id: str, content: str | None = None, comments: list[dict] | None = None, commit: bool = True) -> None:
        if not self._has_search_index:
            return
        self.conn.execute("delete from search_index where post_id=?", (post_id,))
        if content is None:
            row = self.conn.execute("select content from posts where id=?", (post_id,)).fetchone()
            content = row[0] if row else ""
        if content:
            self.conn.execute(
                "insert into search_index(post_id, kind, body) values (?,?,?)",
                (post_id, "post", content),
            )
        if comments is None:
            rows = self.conn.execute("select detail from comments where post_id=? and detail != ''", (post_id,)).fetchall()
            bodies = [row[0] for row in rows]
        else:
            bodies = [row[4] for row in flatten_comments(post_id, comments, now_text()) if row[4]]
        self.conn.executemany(
            "insert into search_index(post_id, kind, body) values (?,?,?)",
            ((post_id, "comment", body) for body in bodies),
        )
        if commit:
            self.conn.commit()


    def get_post_counts(self, post_id: str) -> tuple[int, int] | None:
        row = self.conn.execute(
            "select comment_count, views from posts where id=?",
            (str(post_id),),
        ).fetchone()
        if row is None:
            return None
        return safe_int(row[0]), safe_int(row[1])

    def set_state(self, key: str, value: str, commit: bool = True) -> None:
        self.conn.execute(
            "insert into crawl_state values (?,?,?) on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now_text()),
        )
        if commit:
            self.conn.commit()

    def latest_post_id(self) -> str | None:
        row = self.conn.execute("select id from posts order by create_time desc, id desc limit 1").fetchone()
        return row[0] if row else None
