#!/usr/bin/env python3
"""
Non-invasive architecture switch demo.

This file does not call the real RUC Xiaolaba API and does not read/write the
project's production CSV files. It demonstrates one unified crawler entrypoint,
multiple crawl modes, and a swappable storage layer: CSV or SQLite.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Protocol


COLUMNS = [
    "id",
    "content",
    "category_name",
    "user_name",
    "show_user_id",
    "show_user_head",
    "real_user_id",
    "create_time",
    "comment_count",
    "star_count",
    "trace_count",
    "views",
    "hot",
    "comments_json",
    "updated_at",
]


@dataclass
class Post:
    id: str
    content: str
    category_name: str
    user_name: str
    show_user_id: str
    show_user_head: str
    real_user_id: str
    create_time: str
    comment_count: int
    star_count: int
    trace_count: int
    views: int
    hot: int
    comments_json: str
    updated_at: str


class Store(Protocol):
    def upsert_many(self, posts: Iterable[Post]) -> int:
        ...

    def get(self, post_id: str) -> Post | None:
        ...

    def ids(self) -> set[str]:
        ...

    def all_posts(self) -> list[Post]:
        ...


def normalize_detail(raw: dict) -> Post:
    """Convert upstream detail JSON into one canonical internal row."""
    comments = raw.get("comment_list", [])
    content = f"{raw.get('title') or ''} {raw.get('detail') or ''}".strip()
    return Post(
        id=str(raw["id"]),
        content=content,
        category_name=raw.get("category_name", ""),
        user_name=raw.get("show_user_name", ""),
        show_user_id=str(raw.get("show_user_id", "")),
        show_user_head=raw.get("show_user_head", ""),
        real_user_id=str(raw.get("real_user_id", "0")),
        create_time=raw.get("create_time", ""),
        comment_count=int(raw.get("count_comment", raw.get("comment_count", 0)) or 0),
        star_count=int(raw.get("count_star", raw.get("star_count", 0)) or 0),
        trace_count=int(raw.get("count_trace", raw.get("trace_count", 0)) or 0),
        views=int(raw.get("views", 0) or 0),
        hot=int(raw.get("hot", 0) or 0),
        comments_json=json.dumps(comments, ensure_ascii=False),
        updated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


class CsvStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, Post]:
        if not self.path.exists():
            return {}
        rows: dict[str, Post] = {}
        with self.path.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if not row.get("id"):
                    continue
                rows[row["id"]] = Post(
                    id=row["id"],
                    content=row.get("content", ""),
                    category_name=row.get("category_name", ""),
                    user_name=row.get("user_name", ""),
                    show_user_id=row.get("show_user_id", ""),
                    show_user_head=row.get("show_user_head", ""),
                    real_user_id=row.get("real_user_id", "0"),
                    create_time=row.get("create_time", ""),
                    comment_count=int(row.get("comment_count", 0) or 0),
                    star_count=int(row.get("star_count", 0) or 0),
                    trace_count=int(row.get("trace_count", 0) or 0),
                    views=int(row.get("views", 0) or 0),
                    hot=int(row.get("hot", 0) or 0),
                    comments_json=row.get("comments_json", "[]"),
                    updated_at=row.get("updated_at", ""),
                )
        return rows

    def _save(self, rows: dict[str, Post]) -> None:
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        ordered = sorted(rows.values(), key=lambda p: int(p.id), reverse=True)
        with tmp_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            for post in ordered:
                writer.writerow(asdict(post))
        os.replace(tmp_path, self.path)

    def upsert_many(self, posts: Iterable[Post]) -> int:
        rows = self._load()
        changed = 0
        for post in posts:
            old = rows.get(post.id)
            if old != post:
                rows[post.id] = post
                changed += 1
        if changed:
            self._save(rows)
        return changed

    def get(self, post_id: str) -> Post | None:
        return self._load().get(post_id)

    def ids(self) -> set[str]:
        return set(self._load())

    def all_posts(self) -> list[Post]:
        return sorted(self._load().values(), key=lambda p: int(p.id), reverse=True)


class SqliteStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
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
                    comments_json text not null,
                    updated_at text not null
                )
                """
            )

    def upsert_many(self, posts: Iterable[Post]) -> int:
        rows = [asdict(p) for p in posts]
        if not rows:
            return 0
        with self._connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                insert into posts values (
                    :id, :content, :category_name, :user_name,
                    :show_user_id, :show_user_head, :real_user_id,
                    :create_time, :comment_count, :star_count,
                    :trace_count, :views, :hot, :comments_json, :updated_at
                )
                on conflict(id) do update set
                    content=excluded.content,
                    category_name=excluded.category_name,
                    user_name=excluded.user_name,
                    show_user_id=excluded.show_user_id,
                    show_user_head=excluded.show_user_head,
                    real_user_id=excluded.real_user_id,
                    create_time=excluded.create_time,
                    comment_count=excluded.comment_count,
                    star_count=excluded.star_count,
                    trace_count=excluded.trace_count,
                    views=excluded.views,
                    hot=excluded.hot,
                    comments_json=excluded.comments_json,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
            return conn.total_changes - before

    def get(self, post_id: str) -> Post | None:
        with self._connect() as conn:
            row = conn.execute("select * from posts where id = ?", (post_id,)).fetchone()
        return Post(**dict(row)) if row else None

    def ids(self) -> set[str]:
        with self._connect() as conn:
            return {r["id"] for r in conn.execute("select id from posts")}

    def all_posts(self) -> list[Post]:
        with self._connect() as conn:
            rows = conn.execute("select * from posts order by cast(id as integer) desc").fetchall()
        return [Post(**dict(r)) for r in rows]


class FakeApiClient:
    """Small deterministic API fixture that mirrors the real crawler shape."""

    def __init__(self):
        self._details = {
            "1004": self._post("1004", "最新帖子", "新增内容", comments=1, views=51),
            "1003": self._post("1003", "活动", "今晚讲座", comments=2, views=42),
            "1002": self._post("1002", "问答", "食堂几点关门", comments=1, views=30),
            "1001": self._post("1001", "求助", "丢了校园卡", comments=0, views=12),
        }

    def _post(self, post_id: str, category: str, detail: str, comments: int, views: int) -> dict:
        return {
            "id": post_id,
            "title": "",
            "detail": detail,
            "category_name": category,
            "show_user_name": "某同学",
            "show_user_id": f"demo-{post_id}",
            "show_user_head": "https://example.invalid/avatar.png",
            "real_user_id": "0",
            "create_time": f"2026-06-01 10:{int(post_id) % 60:02d}:00",
            "count_comment": comments,
            "count_star": int(post_id) % 7,
            "count_trace": 0,
            "views": views,
            "hot": views + comments * 10,
            "comment_list": [
                {
                    "id": f"c-{post_id}-{i}",
                    "detail": f"demo comment {i}",
                    "show_user_name": "评论同学",
                    "show_user_id": f"commenter-{i}",
                    "real_user_id": "0",
                    "reply_comment_list": [],
                }
                for i in range(comments)
            ],
        }

    def detail(self, post_id: str) -> dict | None:
        return self._details.get(str(post_id))

    def latest_list(self) -> list[dict]:
        return [
            {"id": "1004", "comment_count": 1},
            {"id": "1003", "comment_count": 2},
            {"id": "1002", "comment_count": 1},
        ]


class UnifiedCrawler:
    def __init__(self, api: FakeApiClient, store: Store):
        self.api = api
        self.store = store

    def full_scan(self, start_id: int, end_id: int) -> int:
        posts = []
        for post_id in range(start_id, end_id - 1, -1):
            raw = self.api.detail(str(post_id))
            if raw:
                posts.append(normalize_detail(raw))
        return self.store.upsert_many(posts)

    def incremental(self) -> int:
        known = self.store.ids()
        changed = []
        for item in self.api.latest_list():
            post_id = str(item["id"])
            current = self.store.get(post_id)
            if current is None or current.comment_count != int(item.get("comment_count", 0)):
                raw = self.api.detail(post_id)
                if raw:
                    changed.append(normalize_detail(raw))
            elif post_id in known:
                continue
        return self.store.upsert_many(changed)

    def detail_fill(self, ids: Iterable[str]) -> int:
        posts = []
        for post_id in ids:
            if self.store.get(post_id):
                continue
            raw = self.api.detail(post_id)
            if raw:
                posts.append(normalize_detail(raw))
        return self.store.upsert_many(posts)

    def verify(self) -> tuple[int, list[str]]:
        errors = []
        posts = self.store.all_posts()
        for post in posts:
            try:
                json.loads(post.comments_json)
            except json.JSONDecodeError:
                errors.append(f"post {post.id}: bad comments_json")
            if not post.id.isdigit():
                errors.append(f"post {post.id}: non-numeric id")
        return len(posts), errors

    def export_csv(self, out_path: Path) -> int:
        out = CsvStore(out_path)
        return out.upsert_many(self.store.all_posts())

    def import_csv(self, csv_path: Path, limit: int | None = None) -> int:
        rows = load_posts_csv(csv_path, limit=limit)
        return self.store.upsert_many(rows)


def post_from_csv_row(row: dict) -> Post:
    return Post(
        id=str(row["id"]),
        content=row.get("content", ""),
        category_name=row.get("category_name", ""),
        user_name=row.get("user_name", ""),
        show_user_id=row.get("show_user_id", ""),
        show_user_head=row.get("show_user_head", ""),
        real_user_id=row.get("real_user_id", "0"),
        create_time=row.get("create_time", ""),
        comment_count=int(row.get("comment_count", 0) or 0),
        star_count=int(row.get("star_count", 0) or 0),
        trace_count=int(row.get("trace_count", 0) or 0),
        views=int(row.get("views", 0) or 0),
        hot=int(row.get("hot", 0) or 0),
        comments_json=row.get("comments_json", "[]"),
        updated_at=row.get("updated_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def load_posts_csv(path: Path, limit: int | None = None) -> list[Post]:
    if not path.exists():
        raise FileNotFoundError(path)
    posts = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("id") and row["id"].isdigit():
                posts.append(post_from_csv_row(row))
                if limit is not None and len(posts) >= limit:
                    break
    return posts


def make_store(kind: str, data_dir: Path) -> Store:
    if kind == "csv":
        return CsvStore(data_dir / "posts_final.demo.csv")
    if kind == "sqlite":
        return SqliteStore(data_dir / "posts.demo.db")
    raise ValueError(f"unsupported store: {kind}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unified crawler/storage architecture demo")
    parser.add_argument(
        "mode",
        choices=["full-scan", "incremental", "detail-fill", "import-csv", "verify", "export-csv"],
        help="demo crawl mode",
    )
    parser.add_argument("--store", choices=["csv", "sqlite"], default="sqlite")
    parser.add_argument("--data-dir", default="demo/runtime")
    parser.add_argument("--ids", nargs="*", default=["1001", "1002", "1003", "1004"])
    parser.add_argument("--start-id", type=int, default=1004)
    parser.add_argument("--end-id", type=int, default=1001)
    parser.add_argument("--csv-path", default="demo/runtime/posts_final.demo.csv")
    parser.add_argument("--export-path", default="demo/runtime/export.csv")
    parser.add_argument("--limit", type=int, default=0, help="max rows to import from CSV; 0 means no limit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir)
    store = make_store(args.store, data_dir)
    crawler = UnifiedCrawler(FakeApiClient(), store)

    if args.mode == "full-scan":
        changed = crawler.full_scan(args.start_id, args.end_id)
        print(f"full-scan changed={changed} store={args.store} dir={data_dir}")
    elif args.mode == "incremental":
        changed = crawler.incremental()
        print(f"incremental changed={changed} store={args.store} dir={data_dir}")
    elif args.mode == "detail-fill":
        changed = crawler.detail_fill(args.ids)
        print(f"detail-fill changed={changed} ids={','.join(args.ids)}")
    elif args.mode == "import-csv":
        limit = args.limit if args.limit > 0 else None
        changed = crawler.import_csv(Path(args.csv_path), limit=limit)
        limit_text = args.limit if args.limit > 0 else "all"
        print(f"import-csv changed={changed} path={args.csv_path} store={args.store} limit={limit_text}")
    elif args.mode == "verify":
        total, errors = crawler.verify()
        print(f"verify total={total} errors={len(errors)}")
        for error in errors:
            print(f"- {error}")
        return 1 if errors else 0
    elif args.mode == "export-csv":
        changed = crawler.export_csv(Path(args.export_path))
        print(f"export-csv changed={changed} path={args.export_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
