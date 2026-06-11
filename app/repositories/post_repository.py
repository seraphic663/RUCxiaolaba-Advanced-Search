"""General post read operations outside full-text search."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.repositories.connections import connect_readonly


class PostRepository:
    def __init__(self, posts_db: str | Path):
        self.posts_db = Path(posts_db)

    def overview(self) -> dict:
        if not self.posts_db.exists():
            return {
                "total": 0,
                "earliest": "?",
                "latest": "?",
                "crawl_time": "?",
            }
        with connect_readonly(self.posts_db) as conn:
            row = conn.execute(
                """
                select count(*) as total,
                       min(nullif(create_time, '')) as earliest,
                       max(nullif(create_time, '')) as latest
                from posts
                """
            ).fetchone()
        return {
            "total": row["total"] if row else 0,
            "earliest": row["earliest"] or "?",
            "latest": row["latest"] or "?",
            "crawl_time": datetime.fromtimestamp(
                self.posts_db.stat().st_mtime
            ).strftime("%Y-%m-%d %H:%M"),
        }
