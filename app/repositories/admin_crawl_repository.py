"""Persistence for administrator upstream previews and live-crawl jobs."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class AdminCrawlRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma journal_mode=wal")
        conn.execute("pragma busy_timeout=10000")
        return conn

    @contextmanager
    def connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                create table if not exists admin_crawl_previews (
                    id text primary key,
                    source text not null,
                    query text not null,
                    pages integer not null,
                    candidates_json text not null,
                    created_at real not null,
                    expires_at real not null
                );
                create table if not exists admin_crawl_jobs (
                    id text primary key,
                    preview_id text not null,
                    strategy text not null,
                    status text not null,
                    total integer not null,
                    completed integer not null default 0,
                    written integer not null default 0,
                    skipped integer not null default 0,
                    failed integer not null default 0,
                    error text not null default '',
                    created_at real not null,
                    updated_at real not null
                );
                create table if not exists admin_crawl_items (
                    job_id text not null,
                    post_id text not null,
                    position integer not null,
                    source text not null,
                    upstream_comment_count integer not null,
                    status text not null,
                    result text not null default '',
                    error text not null default '',
                    primary key(job_id, post_id)
                );
                create index if not exists idx_admin_crawl_jobs_status
                on admin_crawl_jobs(status, created_at);
                """
            )

    def recover_jobs(self) -> None:
        with self.connection() as conn:
            conn.execute(
                "update admin_crawl_jobs set status='queued', "
                "error='服务重启后恢复', updated_at=? where status='running'",
                (time.time(),),
            )
            conn.execute(
                "update admin_crawl_items set status='queued', error='' "
                "where status in ('running','waiting_write')"
            )

    def save_preview(
        self,
        preview_id: str,
        source: str,
        query: str,
        pages: int,
        candidates: list[dict],
        *,
        ttl: int = 600,
    ) -> None:
        now = time.time()
        with self.connection() as conn:
            conn.execute("delete from admin_crawl_previews where expires_at < ?", (now,))
            conn.execute(
                "insert into admin_crawl_previews values (?,?,?,?,?,?,?)",
                (
                    preview_id,
                    source,
                    query,
                    pages,
                    json.dumps(candidates, ensure_ascii=False),
                    now,
                    now + ttl,
                ),
            )

    def get_preview(self, preview_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "select * from admin_crawl_previews where id=? and expires_at>=?",
                (preview_id, time.time()),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["candidates"] = json.loads(result.pop("candidates_json"))
        return result

    def create_job(
        self,
        job_id: str,
        preview_id: str,
        strategy: str,
        selected: list[str],
        candidates: dict[str, dict],
    ) -> str:
        now = time.time()
        with self.connection() as conn:
            conn.execute("begin immediate")
            active = conn.execute(
                "select id from admin_crawl_jobs where status in ('queued','running') "
                "order by created_at limit 1"
            ).fetchone()
            if active is not None:
                return str(active["id"])
            conn.execute(
                "insert into admin_crawl_jobs "
                "(id,preview_id,strategy,status,total,created_at,updated_at) "
                "values (?,?,?,'queued',?,?,?)",
                (job_id, preview_id, strategy, len(selected), now, now),
            )
            conn.executemany(
                "insert into admin_crawl_items "
                "(job_id,post_id,position,source,upstream_comment_count,status) "
                "values (?,?,?,?,?,'queued')",
                (
                    (
                        job_id,
                        post_id,
                        position,
                        candidates[post_id]["source"],
                        int(candidates[post_id]["comment_count"] or 0),
                    )
                    for position, post_id in enumerate(selected, 1)
                ),
            )
        return ""

    def next_queued_job_id(self) -> str:
        with self.connection() as conn:
            row = conn.execute(
                "select id from admin_crawl_jobs where status='queued' "
                "order by created_at limit 1"
            ).fetchone()
        return str(row["id"]) if row else ""

    def start_job(self, job_id: str) -> tuple[dict | None, list[dict]]:
        with self.connection() as conn:
            row = conn.execute(
                "select * from admin_crawl_jobs where id=?", (job_id,)
            ).fetchone()
            if row is None:
                return None, []
            conn.execute(
                "update admin_crawl_jobs set status='running', updated_at=? where id=?",
                (time.time(), job_id),
            )
            items = conn.execute(
                "select * from admin_crawl_items where job_id=? and status='queued' "
                "order by position",
                (job_id,),
            ).fetchall()
        return dict(row), [dict(item) for item in items]

    def update_item(self, job_id: str, post_id: str, **values) -> None:
        if not values:
            return
        with self.connection() as conn:
            assignments = ",".join(f"{key}=?" for key in values)
            conn.execute(
                f"update admin_crawl_items set {assignments} "
                "where job_id=? and post_id=?",
                [*values.values(), job_id, post_id],
            )

    def fail_job_initialization(self, job_id: str, error: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "update admin_crawl_items set status='failed', error=? "
                "where job_id=? and status='queued'",
                (error, job_id),
            )
            conn.execute(
                "update admin_crawl_jobs set status='failed', failed=total, "
                "completed=total, error=?, updated_at=? where id=?",
                (error, time.time(), job_id),
            )

    def finish_job(self, job_id: str, stop_error: str, *, queue_strategy: bool) -> None:
        with self.connection() as conn:
            if stop_error:
                conn.execute(
                    "update admin_crawl_items set status='failed', error=? "
                    "where job_id=? and status='queued'",
                    (f"未执行：{stop_error}", job_id),
                )
            counts = conn.execute(
                "select status,count(*) n from admin_crawl_items "
                "where job_id=? group by status",
                (job_id,),
            ).fetchall()
            summary = {row["status"]: row["n"] for row in counts}
            final_status = "failed" if stop_error else "completed"
            conn.execute(
                "update admin_crawl_jobs set status=?, completed=?, written=?, "
                "skipped=?, failed=?, error=?, updated_at=? where id=?",
                (
                    final_status,
                    sum(summary.values()),
                    0 if queue_strategy else summary.get("succeeded", 0),
                    summary.get("skipped", 0),
                    summary.get("failed", 0),
                    stop_error,
                    time.time(),
                    job_id,
                ),
            )

    def get_job(self, job_id: str) -> dict | None:
        with self.connection() as conn:
            job = conn.execute(
                "select * from admin_crawl_jobs where id=?", (job_id,)
            ).fetchone()
            if job is None:
                return None
            items = conn.execute(
                "select post_id,position,status,result,error from admin_crawl_items "
                "where job_id=? order by position",
                (job_id,),
            ).fetchall()
        payload = dict(job)
        payload["items"] = [dict(item) for item in items]
        return payload
