"""SQLite writer for the future DB-first crawler pipeline.

The store targets the slim production schema by default. It also tolerates the
older full schema by filling posts.comments_json when that column exists.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from app.domain.search import bigram_tokens


def safe_int(value, default=0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def comment_time(item: dict) -> str:
    return str(item.get("create_time") or item.get("show_create_time") or item.get("update_time") or "")


def comment_row(
    post_id: str,
    parent_id: str,
    comment_id: str,
    item: dict,
    updated_at: str,
    row_key: str,
) -> dict:
    return {
        "row_key": row_key,
        "comment_id": comment_id,
        "post_id": post_id,
        "parent_comment_id": parent_id,
        "detail": str(item.get("detail") or ""),
        "show_user_name": str(item.get("show_user_name") or ""),
        "show_user_id": str(item.get("show_user_id") or ""),
        "real_user_id": str(item.get("real_user_id") or "0"),
        "reply_show_user_name": str(item.get("reply_show_user_name") or ""),
        "reply_show_user_id": str(item.get("reply_show_user_id") or ""),
        "is_publisher": safe_int(item.get("is_publisher")),
        "create_time": comment_time(item),
        "updated_at": updated_at,
    }


def flatten_comments(
    post_id: str,
    comments: Iterable[dict],
    updated_at: str,
    *,
    parent_id: str = "",
    parent_key: str | None = None,
) -> list[dict]:
    rows: list[dict] = []
    base_key = parent_key or post_id
    for idx, item in enumerate(comments or []):
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or f"{base_key}-c-{idx}")
        row_key = f"{base_key}:{cid}"
        rows.append(comment_row(post_id, parent_id, cid, item, updated_at, row_key))
        replies = item.get("reply_comment_list") or []
        if isinstance(replies, list):
            rows.extend(
                flatten_comments(
                    post_id,
                    replies,
                    updated_at,
                    parent_id=cid,
                    parent_key=row_key,
                )
            )
    return rows


class SQLitePostStore:
    def __init__(self, db_path: str | Path, bigram_path: str | Path | None = None):
        self.db_path = Path(db_path)
        configured_bigram = (
            bigram_path
            if bigram_path is not None
            else os.environ.get("BIGRAM_DB_PATH")
            or os.environ.get("BIGRAM_DB", "")
        )
        self.bigram_path = Path(configured_bigram).resolve() if configured_bigram else None
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma journal_mode=wal")
        self.conn.execute("pragma synchronous=normal")
        self.conn.execute("pragma foreign_keys=off")
        self.conn.execute("pragma mmap_size=0")
        self.conn.execute("pragma cache_size=-2000")
        self.conn.execute("pragma temp_store=file")
        self._has_bigram_index = False
        if self.bigram_path:
            if not self.bigram_path.exists():
                raise FileNotFoundError(f"bigram index not found: {self.bigram_path}")
            self.conn.execute("attach database ? as bigram", (str(self.bigram_path),))
            meta = self.conn.execute(
                "select value from bigram.index_meta where key='schema_version'"
            ).fetchone()
            if meta is None or meta[0] != "bigram-v1":
                raise RuntimeError(f"unsupported bigram index: {self.bigram_path}")
            self.conn.execute("pragma bigram.journal_mode=wal")
            self.conn.execute("pragma bigram.synchronous=normal")
            self._has_bigram_index = True
        self._post_columns = self._columns("posts") if self._table_exists("posts") else set()
        self._comment_columns = self._columns("comments") if self._table_exists("comments") else set()
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
                real_user_id text not null,
                create_time text not null,
                comment_count integer not null,
                star_count integer not null,
                trace_count integer not null,
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
                updated_at text not null
            );

            create table if not exists crawl_state (
                key text primary key,
                value text not null,
                updated_at text not null
            );

            create index if not exists idx_posts_create_time on posts(create_time);
            create index if not exists idx_posts_stars on posts(star_count desc, id desc);
            create index if not exists idx_posts_category on posts(category_name);
            create index if not exists idx_posts_show_user_id on posts(show_user_id);
            create index if not exists idx_posts_real_user_id on posts(real_user_id);
            create index if not exists idx_posts_user_name_lower on posts(lower(user_name));
            create index if not exists idx_comments_post_id on comments(post_id);
            create index if not exists idx_comments_create_time on comments(create_time);
            create index if not exists idx_comments_post_time on comments(post_id, create_time, row_key);
            create index if not exists idx_comments_show_user_id on comments(show_user_id);
            create index if not exists idx_comments_real_user_id on comments(real_user_id);
            create index if not exists idx_comments_reply_show_user_id on comments(reply_show_user_id);
            create index if not exists idx_comments_show_user_name_lower on comments(lower(show_user_name));
            create index if not exists idx_comments_reply_user_name_lower on comments(lower(reply_show_user_name));
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
        self._comment_columns = self._columns("comments")
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
            "real_user_id": str(post.get("real_user_id") or "0"),
            "create_time": str(post.get("create_time") or post.get("time") or ""),
            "comment_count": safe_int(post.get("comment_count", post.get("comments", 0))),
            "star_count": safe_int(post.get("star_count", post.get("stars", 0))),
            "trace_count": safe_int(post.get("trace_count", post.get("trace", 0))),
            "updated_at": updated_at,
        }
        values.update(
            {
                key: value
                for key, value in {
                    "show_user_head": str(post.get("show_user_head") or ""),
                    "views": safe_int(post.get("views")),
                    "hot": safe_int(post.get("hot")),
                }.items()
                if key in self._post_columns
            }
        )

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
        self.refresh_bigram_index(post_id, values["content"], comments, commit=False)
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
            columns = [
                col
                for col in [
                    "row_key",
                    "comment_id",
                    "post_id",
                    "parent_comment_id",
                    "detail",
                    "show_user_name",
                    "show_user_id",
                    "real_user_id",
                    "reply_show_user_name",
                    "reply_show_user_id",
                    "is_publisher",
                    "create_time",
                    "updated_at",
                ]
                if col in self._comment_columns
            ]
            placeholders = ",".join("?" for _ in columns)
            self.conn.executemany(
                f"insert into comments({','.join(columns)}) values ({placeholders})",
                ([row[col] for col in columns] for row in rows),
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
            bodies = [
                row["detail"]
                for row in flatten_comments(post_id, comments, now_text())
                if row["detail"]
            ]
        self.conn.executemany(
            "insert into search_index(post_id, kind, body) values (?,?,?)",
            ((post_id, "comment", body) for body in bodies),
        )
        if commit:
            self.conn.commit()

    def refresh_bigram_index(
        self,
        post_id: str,
        content: str | None = None,
        comments: list[dict] | None = None,
        commit: bool = True,
    ) -> None:
        if not self._has_bigram_index:
            return

        row_ids = self.conn.execute(
            "select row_id from bigram.search_rows where post_id=?",
            (post_id,),
        ).fetchall()
        if row_ids:
            self.conn.executemany(
                "delete from bigram.search_bigram where rowid=?",
                ((row[0],) for row in row_ids),
            )
            self.conn.execute("delete from bigram.search_rows where post_id=?", (post_id,))

        if content is None:
            row = self.conn.execute("select content from posts where id=?", (post_id,)).fetchone()
            content = row[0] if row else ""
        if comments is None:
            rows = self.conn.execute(
                "select detail from comments where post_id=? and detail != ''",
                (post_id,),
            ).fetchall()
            comment_bodies = [row[0] for row in rows]
        else:
            comment_bodies = [
                row["detail"]
                for row in flatten_comments(post_id, comments, now_text())
                if row["detail"]
            ]

        bodies = []
        if content:
            bodies.append(("post", content))
        bodies.extend(("comment", body) for body in comment_bodies if body)
        for kind, body in bodies:
            cursor = self.conn.execute(
                "insert into bigram.search_rows(post_id, kind) values (?,?)",
                (post_id, kind),
            )
            self.conn.execute(
                "insert into bigram.search_bigram(rowid, tokens) values (?,?)",
                (cursor.lastrowid, bigram_tokens(body)),
            )
        if commit:
            self.conn.commit()


    def get_post_counts(self, post_id: str) -> int | None:
        row = self.conn.execute(
            "select comment_count from posts where id=?",
            (str(post_id),),
        ).fetchone()
        if row is None:
            return None
        return safe_int(row[0])

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
