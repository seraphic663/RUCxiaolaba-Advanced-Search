"""SQLite writer for the future DB-first crawler pipeline.

The store targets the slim production schema by default. It also tolerates the
older full schema by filling posts.comments_json when that column exists.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from app.domain.search import bigram_tokens, symbol_tokens


def safe_int(value, default=0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def later_text(seconds: int) -> str:
    return (datetime.now() + timedelta(seconds=max(0, int(seconds)))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def comment_time(item: dict) -> str:
    return str(
        item.get("create_time") or item.get("show_create_time") or item.get("update_time") or ""
    )


def article_text(item: dict) -> str:
    return f"{item.get('title') or ''} {item.get('detail') or ''}".strip()


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
    def __init__(
        self,
        db_path: str | Path,
        bigram_path: str | Path | None = None,
        symbol_path: str | Path | None = None,
    ):
        self.db_path = Path(db_path)
        configured_bigram = (
            bigram_path
            if bigram_path is not None
            else os.environ.get("BIGRAM_DB_PATH") or os.environ.get("BIGRAM_DB", "")
        )
        self.bigram_path = Path(configured_bigram).resolve() if configured_bigram else None
        configured_symbol = (
            symbol_path
            if symbol_path is not None
            else os.environ.get("SYMBOL_INDEX_DB_PATH") or os.environ.get("SYMBOL_INDEX_DB", "")
        )
        self.symbol_path = Path(configured_symbol).resolve() if configured_symbol else None
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma journal_mode=wal")
        self.conn.execute("pragma synchronous=normal")
        self.conn.execute("pragma foreign_keys=off")
        self.conn.execute("pragma mmap_size=0")
        self.conn.execute("pragma cache_size=-2000")
        self.conn.execute("pragma temp_store=file")
        self._has_bigram_index = False
        self._has_symbol_index = False
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
        if self.symbol_path:
            if not self.symbol_path.exists():
                raise FileNotFoundError(f"symbol index not found: {self.symbol_path}")
            self.conn.execute("attach database ? as symbol", (str(self.symbol_path),))
            meta = self.conn.execute(
                "select value from symbol.index_meta where key='schema_version'"
            ).fetchone()
            if meta is None or meta[0] != "symbol-v1":
                raise RuntimeError(f"unsupported symbol index: {self.symbol_path}")
            self.conn.execute("pragma symbol.journal_mode=wal")
            self.conn.execute("pragma symbol.synchronous=normal")
            self._has_symbol_index = True
        self._post_columns = self._columns("posts") if self._table_exists("posts") else set()
        self._comment_columns = (
            self._columns("comments") if self._table_exists("comments") else set()
        )
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
                crawl_status text not null default 'full',
                list_update_time text not null default '',
                list_source text not null default '',
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

            create table if not exists crawler_queue (
                post_id text primary key,
                source text not null,
                priority integer not null,
                list_create_time text not null,
                list_update_time text not null,
                list_comment_count integer not null,
                db_comment_count integer,
                status text not null,
                reason text not null,
                attempts integer not null,
                last_error text not null,
                last_attempt_list_comment_count integer,
                last_attempt_list_update_time text not null default '',
                last_detail_comment_count integer,
                same_observation_attempts integer not null default 0,
                next_attempt_at text not null default '',
                created_at text not null,
                updated_at text not null
            );

            create table if not exists crawler_gap_ranges (
                range_id text primary key,
                start_id integer not null,
                end_id integer not null,
                reason text not null,
                status text not null,
                estimated_density real not null,
                sampled integer not null,
                found integer not null,
                missing integer not null,
                errors integer not null,
                created_at text not null,
                updated_at text not null
            );

            create table if not exists crawler_id_probe (
                post_id text primary key,
                range_id text not null,
                status text not null,
                create_time text not null,
                comment_count integer not null,
                last_error text not null,
                attempts integer not null,
                probed_at text not null
            );

            create table if not exists crawler_run_history (
                run_id integer primary key autoincrement,
                command text not null,
                started_at text not null,
                finished_at text not null,
                source_calls integer not null,
                seen integer not null,
                selected integer not null,
                written integer not null,
                queue_inserted integer not null,
                queue_reopened integer not null,
                queue_updated integer not null,
                completed_details integer not null,
                refreshed_details integer not null,
                new_comment_rows integer not null,
                comment_row_delta integer not null,
                misses integer not null,
                errors integer not null,
                rate_limited integer not null,
                stats_json text not null
            );

            create index if not exists idx_posts_create_time on posts(create_time);
            create index if not exists idx_posts_id_int on posts(cast(id as integer));
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
            create index if not exists idx_crawler_queue_status_priority on crawler_queue(status, priority, updated_at);
            create index if not exists idx_crawler_queue_due on crawler_queue(status, next_attempt_at, priority);
            create index if not exists idx_crawler_gap_status on crawler_gap_ranges(status, start_id);
            create index if not exists idx_crawler_probe_range on crawler_id_probe(range_id, status);
            create index if not exists idx_crawler_run_finished
                on crawler_run_history(finished_at, command);
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
        self.ensure_runtime_schema()
        self._post_columns = self._columns("posts")
        self._comment_columns = self._columns("comments")
        self._has_search_index = True

    def ensure_runtime_schema(self) -> None:
        if self._table_exists("posts"):
            columns = self._columns("posts")
            for name, ddl in {
                "crawl_status": "alter table posts add column crawl_status text not null default 'full'",
                "list_update_time": "alter table posts add column list_update_time text not null default ''",
                "list_source": "alter table posts add column list_source text not null default ''",
            }.items():
                if name not in columns:
                    self.conn.execute(ddl)
            self.conn.execute(
                "create index if not exists idx_posts_id_int on posts(cast(id as integer))"
            )
        self.ensure_crawler_queue(commit=False)
        self.ensure_gap_tables(commit=False)
        self.ensure_crawler_run_history(commit=False)
        observation_migration = self.migrate_crawler_queue_observations(commit=False)
        terminal_migration = self.migrate_crawler_queue_terminal_states(commit=False)
        comment_gap_migration = self.migrate_crawler_comment_row_gaps(commit=False)
        comment_gap_priority_migration = (
            self.migrate_crawler_comment_row_gap_priorities(commit=False)
        )
        self.conn.commit()
        if observation_migration:
            print(
                f"[queue] migrated observation state {observation_migration}",
                flush=True,
            )
        if terminal_migration:
            print(
                f"[queue] migrated terminal state {terminal_migration}",
                flush=True,
            )
        if comment_gap_migration:
            print(
                f"[queue] migrated comment row gaps {comment_gap_migration}",
                flush=True,
            )
        if comment_gap_priority_migration:
            print(
                "[queue] migrated comment row gap priorities "
                f"{comment_gap_priority_migration}",
                flush=True,
            )
        self._post_columns = self._columns("posts") if self._table_exists("posts") else set()
        self._comment_columns = (
            self._columns("comments") if self._table_exists("comments") else set()
        )

    def ensure_crawler_queue(self, commit: bool = True) -> None:
        self.conn.execute(
            """
            create table if not exists crawler_queue (
                post_id text primary key,
                source text not null,
                priority integer not null,
                list_create_time text not null,
                list_update_time text not null,
                list_comment_count integer not null,
                db_comment_count integer,
                status text not null,
                reason text not null,
                attempts integer not null,
                last_error text not null,
                last_attempt_list_comment_count integer,
                last_attempt_list_update_time text not null default '',
                last_detail_comment_count integer,
                same_observation_attempts integer not null default 0,
                next_attempt_at text not null default '',
                created_at text not null,
                updated_at text not null
            )
            """
        )
        columns = self._columns("crawler_queue")
        for name, ddl in {
            "last_attempt_list_comment_count": (
                "alter table crawler_queue "
                "add column last_attempt_list_comment_count integer"
            ),
            "last_attempt_list_update_time": (
                "alter table crawler_queue "
                "add column last_attempt_list_update_time text not null default ''"
            ),
            "last_detail_comment_count": (
                "alter table crawler_queue add column last_detail_comment_count integer"
            ),
            "same_observation_attempts": (
                "alter table crawler_queue "
                "add column same_observation_attempts integer not null default 0"
            ),
            "next_attempt_at": (
                "alter table crawler_queue "
                "add column next_attempt_at text not null default ''"
            ),
        }.items():
            if name not in columns:
                self.conn.execute(ddl)
        self.conn.execute(
            "create index if not exists idx_crawler_queue_status_priority "
            "on crawler_queue(status, priority, updated_at)"
        )
        self.conn.execute(
            "create index if not exists idx_crawler_queue_due "
            "on crawler_queue(status, next_attempt_at, priority)"
        )
        if commit:
            self.conn.commit()

    def migrate_crawler_queue_observations(self, commit: bool = True) -> dict:
        """One-time repair for queue rows created before observation tracking."""
        if not self._table_exists("posts") or not self._table_exists("crawler_queue"):
            return {}
        self.conn.execute(
            """
            create table if not exists crawl_state (
                key text primary key,
                value text not null,
                updated_at text not null
            )
            """
        )
        migration_key = "crawler_queue_observation_state_v1"
        if self.conn.execute(
            "select 1 from crawl_state where key=?",
            (migration_key,),
        ).fetchone():
            return {}
        now = now_text()
        seeded = self.conn.execute(
            """
            update crawler_queue
            set last_attempt_list_comment_count=list_comment_count,
                last_attempt_list_update_time=list_update_time,
                same_observation_attempts=case
                    when attempts >= 2 then 2
                    when attempts > 0 then attempts
                    else 0
                end
            where attempts > 0
              and last_attempt_list_comment_count is null
            """
        ).rowcount
        stale = self.conn.execute(
            """
            select count(*)
            from crawler_queue q
            join posts p on p.id=q.post_id
            where q.priority=0
              and q.attempts > 0
              and coalesce(p.crawl_status,'full')='full'
              and coalesce(q.list_comment_count,0) > coalesce(p.comment_count,0)
            """
        ).fetchone()[0]
        self.conn.execute(
            """
            update crawler_queue
            set status=case when attempts >= 2 then 'deferred' else 'pending' end,
                db_comment_count=(
                    select p.comment_count from posts p
                    where p.id=crawler_queue.post_id
                ),
                last_detail_comment_count=(
                    select p.comment_count from posts p
                    where p.id=crawler_queue.post_id
                ),
                next_attempt_at='',
                last_error='legacy_list_detail_gap',
                updated_at=?
            where priority=0
              and attempts > 0
              and exists(
                  select 1 from posts p
                  where p.id=crawler_queue.post_id
                    and coalesce(p.crawl_status,'full')='full'
                    and coalesce(crawler_queue.list_comment_count,0)
                        > coalesce(p.comment_count,0)
              )
            """,
            (now,),
        )
        deferred = self.conn.execute(
            """
            select count(*) from crawler_queue
            where status='deferred' and last_error='legacy_list_detail_gap'
            """
        ).fetchone()[0]
        result = {
            "seeded": max(0, int(seeded or 0)),
            "stale_gaps": safe_int(stale),
            "deferred": safe_int(deferred),
        }
        self.conn.execute(
            "insert into crawl_state(key,value,updated_at) values (?,?,?)",
            (migration_key, "1", now),
        )
        if commit:
            self.conn.commit()
        return result if any(result.values()) else {}

    def migrate_crawler_queue_terminal_states(self, commit: bool = True) -> dict:
        """One-time normalization of terminal states written by the old worker."""
        if not self._table_exists("crawler_queue"):
            return {}
        self.conn.execute(
            """
            create table if not exists crawl_state (
                key text primary key,
                value text not null,
                updated_at text not null
            )
            """
        )
        migration_key = "crawler_queue_terminal_state_v2"
        if self.conn.execute(
            "select 1 from crawl_state where key=?",
            (migration_key,),
        ).fetchone():
            return {}
        now = now_text()
        normalized = self.conn.execute(
            """
            update crawler_queue
            set status='skipped', updated_at=?
            where status='failed'
              and last_error in ('not_found','foreign_or_invalid')
            """,
            (now,),
        ).rowcount
        requeued = self.conn.execute(
            """
            update crawler_queue
            set status='pending', next_attempt_at='', updated_at=?
            where status='failed'
              and attempts < 3
              and last_error not like 'suspicious_payload:%'
            """,
            (now,),
        ).rowcount
        result = {
            "normalized_skipped": max(0, int(normalized or 0)),
            "requeued_transient": max(0, int(requeued or 0)),
        }
        self.conn.execute(
            "insert into crawl_state(key,value,updated_at) values (?,?,?)",
            (migration_key, "1", now),
        )
        if commit:
            self.conn.commit()
        return result if any(result.values()) else {}

    def migrate_crawler_comment_row_gaps(self, commit: bool = True) -> dict:
        """Queue full posts whose declared comments exceed saved comment rows."""
        if not all(
            self._table_exists(name)
            for name in ("posts", "comments", "crawler_queue")
        ):
            return {}
        self.conn.execute(
            """
            create table if not exists crawl_state (
                key text primary key,
                value text not null,
                updated_at text not null
            )
            """
        )
        migration_key = "crawler_comment_row_gap_v1"
        if self.conn.execute(
            "select 1 from crawl_state where key=?",
            (migration_key,),
        ).fetchone():
            return {}
        rows = self.conn.execute(
            """
            with saved as (
                select post_id, count(*) as actual_count
                from comments
                group by post_id
            )
            select p.id, p.create_time, p.comment_count,
                   p.list_update_time,
                   coalesce(saved.actual_count, 0) as actual_count
            from posts p
            left join saved on saved.post_id=p.id
            where coalesce(p.crawl_status, 'full')='full'
              and coalesce(p.comment_count, 0)
                  > coalesce(saved.actual_count, 0)
            order by cast(p.id as integer)
            """
        ).fetchall()
        now = now_text()
        inserted = requeued = 0
        for row in rows:
            existing = self.conn.execute(
                "select source,reason,status from crawler_queue where post_id=?",
                (str(row["id"]),),
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    """
                    insert into crawler_queue(
                        post_id, source, priority, list_create_time,
                        list_update_time, list_comment_count, db_comment_count,
                        status, reason, attempts, last_error,
                        last_attempt_list_comment_count,
                        last_attempt_list_update_time, last_detail_comment_count,
                        same_observation_attempts, next_attempt_at,
                        created_at, updated_at
                    ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(row["id"]),
                        "local_audit",
                        35,
                        str(row["create_time"] or ""),
                        str(row["list_update_time"] or row["create_time"] or ""),
                        safe_int(row["comment_count"]),
                        safe_int(row["actual_count"]),
                        "pending",
                        "comment_rows_incomplete",
                        0,
                        "",
                        None,
                        "",
                        safe_int(row["actual_count"]),
                        0,
                        "",
                        now,
                        now,
                    ),
                )
                inserted += 1
                continue
            sources = set(filter(None, str(existing["source"] or "").split(",")))
            sources.add("local_audit")
            reasons = set(filter(None, str(existing["reason"] or "").split("|")))
            reasons.add("comment_rows_incomplete")
            if str(existing["status"] or "") != "pending":
                requeued += 1
            self.conn.execute(
                """
                update crawler_queue
                set source=?, priority=min(priority, 35),
                    list_create_time=?, list_update_time=?,
                    list_comment_count=?, db_comment_count=?,
                    status='pending', reason=?, last_error='',
                    last_attempt_list_comment_count=null,
                    last_attempt_list_update_time='',
                    last_detail_comment_count=?,
                    same_observation_attempts=0, next_attempt_at='',
                    updated_at=?
                where post_id=?
                """,
                (
                    ",".join(sorted(sources)),
                    str(row["create_time"] or ""),
                    str(row["list_update_time"] or row["create_time"] or ""),
                    safe_int(row["comment_count"]),
                    safe_int(row["actual_count"]),
                    "|".join(sorted(reasons)),
                    safe_int(row["actual_count"]),
                    now,
                    str(row["id"]),
                ),
            )
        result = {
            "candidates": len(rows),
            "inserted": inserted,
            "requeued": requeued,
        }
        self.conn.execute(
            "insert into crawl_state(key,value,updated_at) values (?,?,?)",
            (migration_key, "1", now),
        )
        if commit:
            self.conn.commit()
        return result if any(result.values()) else {}

    def migrate_crawler_comment_row_gap_priorities(
        self,
        commit: bool = True,
    ) -> dict:
        """Move no-yield local audits behind known list/detail coverage."""
        if not self._table_exists("crawler_queue"):
            return {}
        migration_key = "crawler_comment_row_gap_priority_v2"
        if self.conn.execute(
            "select 1 from crawl_state where key=?",
            (migration_key,),
        ).fetchone():
            return {}
        now = now_text()
        rebalanced = self.conn.execute(
            """
            update crawler_queue
            set priority=case
                    when reason like '%comment_changed%' then 0
                    when reason like '%new_post%' then 10
                    when reason like '%active_missing%' then 20
                    else 35
                end,
                updated_at=?
            where status='pending'
              and priority=5
              and reason like '%comment_rows_incomplete%'
            """,
            (now,),
        ).rowcount
        result = {"rebalanced": max(0, int(rebalanced or 0))}
        self.conn.execute(
            "insert into crawl_state(key,value,updated_at) values (?,?,?)",
            (migration_key, "1", now),
        )
        if commit:
            self.conn.commit()
        return result if any(result.values()) else {}

    def ensure_crawler_run_history(self, commit: bool = True) -> None:
        self.conn.execute(
            """
            create table if not exists crawler_run_history (
                run_id integer primary key autoincrement,
                command text not null,
                started_at text not null,
                finished_at text not null,
                source_calls integer not null,
                seen integer not null,
                selected integer not null,
                written integer not null,
                queue_inserted integer not null,
                queue_reopened integer not null,
                queue_updated integer not null,
                completed_details integer not null,
                refreshed_details integer not null,
                new_comment_rows integer not null,
                comment_row_delta integer not null,
                misses integer not null,
                errors integer not null,
                rate_limited integer not null,
                stats_json text not null
            )
            """
        )
        self.conn.execute(
            """
            create index if not exists idx_crawler_run_finished
            on crawler_run_history(finished_at, command)
            """
        )
        if commit:
            self.conn.commit()

    def record_crawler_run(
        self,
        *,
        command: str,
        stats: dict,
        started_at: str,
        commit: bool = True,
    ) -> None:
        self.ensure_crawler_run_history(commit=False)
        self.conn.execute(
            """
            insert into crawler_run_history(
                command,started_at,finished_at,source_calls,seen,selected,
                written,queue_inserted,queue_reopened,queue_updated,
                completed_details,refreshed_details,new_comment_rows,
                comment_row_delta,misses,errors,rate_limited,stats_json
            ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(command),
                str(started_at),
                datetime.now(timezone(timedelta(hours=8))).isoformat(),
                safe_int(stats.get("source_calls")),
                safe_int(stats.get("seen", stats.get("sampled", 0))),
                safe_int(stats.get("selected", stats.get("queued", 0))),
                safe_int(stats.get("written", stats.get("saved", 0))),
                safe_int(stats.get("queue_inserted")),
                safe_int(stats.get("queue_reopened")),
                safe_int(stats.get("queue_updated")),
                safe_int(stats.get("completed_details")),
                safe_int(stats.get("refreshed_details")),
                safe_int(stats.get("new_comment_rows")),
                safe_int(stats.get("comment_row_delta")),
                safe_int(stats.get("misses", stats.get("missing", 0))),
                safe_int(stats.get("errors")),
                int(bool(stats.get("rate_limited"))),
                json.dumps(stats, ensure_ascii=False, sort_keys=True),
            ),
        )
        if commit:
            self.conn.commit()

    def ensure_gap_tables(self, commit: bool = True) -> None:
        self.conn.executescript(
            """
            create table if not exists crawler_gap_ranges (
                range_id text primary key,
                start_id integer not null,
                end_id integer not null,
                reason text not null,
                status text not null,
                estimated_density real not null,
                sampled integer not null,
                found integer not null,
                missing integer not null,
                errors integer not null,
                created_at text not null,
                updated_at text not null
            );

            create table if not exists crawler_id_probe (
                post_id text primary key,
                range_id text not null,
                status text not null,
                create_time text not null,
                comment_count integer not null,
                last_error text not null,
                attempts integer not null,
                probed_at text not null
            );

            create index if not exists idx_crawler_gap_status on crawler_gap_ranges(status, start_id);
            create index if not exists idx_crawler_probe_range on crawler_id_probe(range_id, status);
            """
        )
        if commit:
            self.conn.commit()

    def upsert_post(
        self, post: dict, comments: list[dict] | None = None, commit: bool = True
    ) -> None:
        updated_at = now_text()
        post_id = str(post.get("id") or "")
        if not post_id:
            raise ValueError("post id is required")

        existing_meta = {}
        if {"list_update_time", "list_source"}.issubset(self._post_columns):
            row = self.conn.execute(
                "select list_update_time, list_source from posts where id=?",
                (post_id,),
            ).fetchone()
            if row is not None:
                existing_meta = {
                    "list_update_time": str(row["list_update_time"] or ""),
                    "list_source": str(row["list_source"] or ""),
                }

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
                    "crawl_status": str(post.get("crawl_status") or "full"),
                    "list_update_time": str(
                        post.get("list_update_time")
                        or post.get("update_time")
                        or existing_meta.get("list_update_time")
                        or ""
                    ),
                    "list_source": str(
                        post.get("list_source") or existing_meta.get("list_source") or ""
                    ),
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
        self.refresh_symbol_index(post_id, values["content"], comments, commit=False)
        if commit:
            self.conn.commit()

    def upsert_list_stub(
        self,
        article: dict,
        *,
        source: str,
        commit: bool = True,
    ) -> bool:
        if "crawl_status" not in self._post_columns:
            self.ensure_runtime_schema()
        post_id = str(article.get("id") or "")
        if not post_id:
            return False
        content = article_text(article)
        create_time = str(article.get("create_time") or article.get("show_create_time") or "")
        list_update_time = str(article.get("update_time") or create_time)
        now = now_text()
        values = {
            "id": post_id,
            "content": content,
            "category_name": str(article.get("category_name") or ""),
            "user_name": str(article.get("show_user_name") or article.get("user_name") or ""),
            "show_user_id": str(article.get("show_user_id") or ""),
            "real_user_id": str(article.get("real_user_id") or "0"),
            "create_time": create_time,
            "comment_count": safe_int(
                article.get("comment_count", article.get("count_comment", 0))
            ),
            "star_count": safe_int(article.get("count_star", article.get("star_count", 0))),
            "trace_count": safe_int(article.get("count_trace", article.get("trace_count", 0))),
            "crawl_status": "list_only",
            "list_update_time": list_update_time,
            "list_source": source,
            "updated_at": now,
        }
        columns = [col for col in values if col in self._post_columns]
        existing = self.conn.execute(
            """
            select crawl_status, content, category_name, user_name,
                   show_user_id, real_user_id, create_time,
                   comment_count, star_count, trace_count,
                   list_update_time, list_source
            from posts where id=?
            """,
            (post_id,),
        ).fetchone()
        if existing is None:
            placeholders = ",".join("?" for _ in columns)
            self.conn.execute(
                f"insert into posts({','.join(columns)}) values ({placeholders})",
                [values[col] for col in columns],
            )
            self.refresh_search_index(post_id, content, [], commit=False)
            self.refresh_bigram_index(post_id, content, [], commit=False)
            changed = True
        else:
            status = str(existing["crawl_status"] or "full")
            if status == "full":
                metadata_changed = any(
                    (
                        safe_int(existing["comment_count"]) != values["comment_count"],
                        safe_int(existing["star_count"]) != values["star_count"],
                        safe_int(existing["trace_count"]) != values["trace_count"],
                        str(existing["list_update_time"] or "") != list_update_time,
                        str(existing["list_source"] or "") != source,
                    )
                )
                if metadata_changed:
                    self.conn.execute(
                        """
                        update posts
                        set comment_count=?, star_count=?, trace_count=?,
                            list_update_time=?, list_source=?, updated_at=?
                        where id=?
                        """,
                        (
                            values["comment_count"],
                            values["star_count"],
                            values["trace_count"],
                            list_update_time,
                            source,
                            now,
                            post_id,
                        ),
                    )
                changed = False
            else:
                content_changed = str(existing["content"] or "") != content
                metadata_changed = any(
                    (
                        content_changed,
                        str(existing["category_name"] or "") != values["category_name"],
                        str(existing["user_name"] or "") != values["user_name"],
                        str(existing["show_user_id"] or "") != values["show_user_id"],
                        str(existing["real_user_id"] or "") != values["real_user_id"],
                        str(existing["create_time"] or "") != create_time,
                        safe_int(existing["comment_count"]) != values["comment_count"],
                        safe_int(existing["star_count"]) != values["star_count"],
                        safe_int(existing["trace_count"]) != values["trace_count"],
                        str(existing["list_update_time"] or "") != list_update_time,
                        str(existing["list_source"] or "") != source,
                    )
                )
                if metadata_changed:
                    self.conn.execute(
                        """
                        update posts
                        set content=?, category_name=?, user_name=?,
                            show_user_id=?, real_user_id=?, create_time=?,
                            comment_count=?, star_count=?, trace_count=?,
                            crawl_status='list_only', list_update_time=?,
                            list_source=?, updated_at=?
                        where id=?
                        """,
                        (
                            content,
                            values["category_name"],
                            values["user_name"],
                            values["show_user_id"],
                            values["real_user_id"],
                            create_time,
                            values["comment_count"],
                            values["star_count"],
                            values["trace_count"],
                            list_update_time,
                            source,
                            now,
                            post_id,
                        ),
                    )
                    if content_changed:
                        self.refresh_search_index(post_id, content, [], commit=False)
                        self.refresh_bigram_index(post_id, content, [], commit=False)
                changed = metadata_changed
        if commit:
            self.conn.commit()
        return changed

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

    def refresh_search_index(
        self,
        post_id: str,
        content: str | None = None,
        comments: list[dict] | None = None,
        commit: bool = True,
    ) -> None:
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
            rows = self.conn.execute(
                "select detail from comments where post_id=? and detail != ''", (post_id,)
            ).fetchall()
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

    def refresh_symbol_index(
        self,
        post_id: str,
        content: str | None = None,
        comments: list[dict] | None = None,
        commit: bool = True,
    ) -> None:
        if not self._has_symbol_index:
            return
        self.conn.execute("delete from symbol.symbol_rows where post_id=?", (post_id,))
        if content is None:
            row = self.conn.execute("select content from posts where id=?", (post_id,)).fetchone()
            content = row[0] if row else ""
        if comments is None:
            rows = self.conn.execute(
                "select detail from comments where post_id=? and detail != ''",
                (post_id,),
            ).fetchall()
            bodies = [("comment", row[0]) for row in rows]
        else:
            bodies = [
                ("comment", row["detail"])
                for row in flatten_comments(post_id, comments, now_text())
                if row["detail"]
            ]
        if content:
            bodies.insert(0, ("post", content))
        inserts = []
        for kind, body in bodies:
            inserts.extend(
                (token, post_id, kind, position)
                for position, token in enumerate(symbol_tokens(body or ""))
            )
        if inserts:
            self.conn.executemany(
                "insert into symbol.symbol_rows(token,post_id,kind,position) values (?,?,?,?)",
                inserts,
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

    def get_post_crawl_snapshot(self, post_id: str) -> dict | None:
        columns = ["comment_count"]
        if "crawl_status" in self._post_columns:
            columns.append("crawl_status")
        row = self.conn.execute(
            f"select {','.join(columns)} from posts where id=?",
            (str(post_id),),
        ).fetchone()
        if row is None:
            return None
        return {
            "comment_count": safe_int(row["comment_count"]),
            "crawl_status": str(row["crawl_status"] or "full")
            if "crawl_status" in row.keys()
            else "full",
        }

    def post_exists(self, post_id: str) -> bool:
        row = self.conn.execute(
            "select 1 from posts where id=?",
            (str(post_id),),
        ).fetchone()
        return row is not None

    def enqueue_crawler_candidate(
        self,
        *,
        post_id: str,
        source: str,
        priority: int,
        list_create_time: str,
        list_update_time: str,
        list_comment_count: int,
        db_comment_count: int | None,
        reason: str,
        commit: bool = True,
    ) -> str:
        self.ensure_crawler_queue(commit=False)
        now = now_text()
        existing = self.conn.execute(
            """
            select source, priority, reason, status, list_create_time,
                   list_update_time, list_comment_count, db_comment_count,
                   last_attempt_list_comment_count,
                   last_attempt_list_update_time,
                   same_observation_attempts, next_attempt_at
            from crawler_queue where post_id=?
            """,
            (str(post_id),),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                """
                insert into crawler_queue(
                    post_id, source, priority, list_create_time,
                    list_update_time, list_comment_count, db_comment_count,
                    status, reason, attempts, last_error,
                    last_attempt_list_comment_count,
                    last_attempt_list_update_time, last_detail_comment_count,
                    same_observation_attempts, next_attempt_at,
                    created_at, updated_at
                ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(post_id),
                    source,
                    priority,
                    list_create_time,
                    list_update_time,
                    list_comment_count,
                    db_comment_count,
                    "pending",
                    reason,
                    0,
                    "",
                    None,
                    "",
                    None,
                    0,
                    "",
                    now,
                    now,
                ),
            )
            action = "inserted"
        else:
            sources = set(filter(None, str(existing["source"]).split(",")))
            sources.add(source)
            reasons = set(filter(None, str(existing["reason"]).split("|")))
            reasons.add(reason)
            old_status = str(existing["status"] or "")
            status = old_status
            old_list_count = safe_int(existing["list_comment_count"])
            attempted_count = existing["last_attempt_list_comment_count"]
            attempted_update = str(existing["last_attempt_list_update_time"] or "")
            snapshot = self.get_post_crawl_snapshot(str(post_id))
            is_full = bool(snapshot and snapshot["crawl_status"] == "full")
            current_db_count = (
                safe_int(snapshot["comment_count"])
                if snapshot is not None
                else safe_int(existing["db_comment_count"])
            )
            count_growth = (
                list_comment_count > old_list_count
                and list_comment_count > current_db_count
            )
            never_attempted = attempted_count is None and not attempted_update
            incomplete_changed = (
                not is_full
                and (
                    list_comment_count != safe_int(attempted_count)
                    or (
                        bool(list_update_time)
                        and list_update_time > attempted_update
                    )
                )
            )
            has_new_evidence = (
                reason == "admin_selected"
                or never_attempted
                or count_growth
                or incomplete_changed
            )
            if old_status in {"done", "skipped", "failed", "deferred"} and has_new_evidence:
                status = "pending"
            new_source = ",".join(sorted(sources))
            new_reason = "|".join(sorted(reasons))
            new_priority = min(safe_int(existing["priority"]), priority)
            new_create_time = (
                list_create_time if list_create_time else str(existing["list_create_time"] or "")
            )
            # For a full post, an update-time-only change is not detail evidence.
            # Keeping the previous timestamp also lets discovery count this as a
            # true no-op instead of extending list scans with metadata churn.
            if (
                is_full
                and list_comment_count == old_list_count
                and reason != "admin_selected"
            ):
                new_update_time = str(existing["list_update_time"] or "")
            else:
                new_update_time = (
                    list_update_time
                    if list_update_time
                    else str(existing["list_update_time"] or "")
                )
            unchanged = all(
                (
                    str(existing["source"] or "") == new_source,
                    safe_int(existing["priority"]) == new_priority,
                    str(existing["reason"] or "") == new_reason,
                    str(existing["status"] or "") == status,
                    str(existing["list_create_time"] or "") == new_create_time,
                    str(existing["list_update_time"] or "") == new_update_time,
                    safe_int(existing["list_comment_count"]) == list_comment_count,
                    (existing["db_comment_count"] is None and db_comment_count is None)
                    or safe_int(existing["db_comment_count"]) == safe_int(db_comment_count),
                )
            )
            if unchanged:
                if commit:
                    self.conn.commit()
                return "unchanged"
            reopened = old_status != "pending" and status == "pending"
            reset_observation = has_new_evidence and (
                attempted_count is None
                or list_comment_count != safe_int(attempted_count)
                or str(list_update_time or "") != attempted_update
            )
            self.conn.execute(
                """
                update crawler_queue
                set source=?, priority=min(priority, ?),
                    list_create_time=?,
                    list_update_time=?,
                    list_comment_count=?, db_comment_count=?,
                    status=?, reason=?,
                    same_observation_attempts=case when ? then 0
                        else same_observation_attempts end,
                    next_attempt_at=case when ? then '' else next_attempt_at end,
                    last_error=case when ? then '' else last_error end,
                    updated_at=?
                where post_id=?
                """,
                (
                    new_source,
                    priority,
                    new_create_time,
                    new_update_time,
                    list_comment_count,
                    db_comment_count,
                    status,
                    new_reason,
                    reset_observation,
                    reset_observation,
                    reset_observation,
                    now,
                    str(post_id),
                ),
            )
            action = "reopened" if reopened else "updated"
        if commit:
            self.conn.commit()
        return action

    def next_crawler_queue_items(
        self,
        limit: int,
        refresh_limit: int | None = None,
        fresh_coverage_after: str = "",
    ) -> list[sqlite3.Row]:
        self.ensure_crawler_queue()
        limit = max(1, int(limit))
        if refresh_limit is None:
            return self.conn.execute(
                """
            select * from crawler_queue
            where status='pending'
              and (next_attempt_at='' or next_attempt_at <= ?)
            order by
                priority asc,
                max(
                    0,
                    coalesce(list_comment_count, 0)
                    - coalesce(db_comment_count, 0)
                ) desc,
                coalesce(list_update_time, list_create_time, '') desc,
                coalesce(list_comment_count, 0) desc,
                updated_at asc,
                cast(post_id as integer) desc
            limit ?
            """,
                (now_text(), limit),
            ).fetchall()

        selected: list[sqlite3.Row] = []
        selected_ids: set[str] = set()

        def append_lane(
            where: str,
            lane_limit: int,
            where_params: tuple[object, ...] = (),
        ) -> None:
            if lane_limit <= 0 or len(selected) >= limit:
                return
            params: list[object] = [now_text()]
            params.extend(where_params)
            exclude = ""
            if selected_ids:
                placeholders = ",".join("?" for _ in selected_ids)
                exclude = f" and post_id not in ({placeholders})"
                params.extend(sorted(selected_ids))
            params.append(min(lane_limit, limit - len(selected)))
            rows = self.conn.execute(
                f"""
                select * from crawler_queue
                where status='pending'
                  and (next_attempt_at='' or next_attempt_at <= ?)
                  and ({where})
                  {exclude}
                order by
                    priority asc,
                    max(
                        0,
                        coalesce(list_comment_count, 0)
                        - coalesce(db_comment_count, 0)
                    ) desc,
                    coalesce(list_update_time, list_create_time, '') desc,
                    coalesce(list_comment_count, 0) desc,
                    updated_at asc,
                    cast(post_id as integer) desc
                limit ?
                """,
                params,
            ).fetchall()
            selected.extend(rows)
            selected_ids.update(str(row["post_id"]) for row in rows)

        # Explicit admin work stays urgent. Natural comment refreshes are capped
        # so they cannot starve never-fetched IDs from the same detail budget.
        append_lane("priority < 0", limit)
        append_lane("priority = 0", max(0, int(refresh_limit)))
        coverage_capacity = max(0, limit - len(selected))
        if fresh_coverage_after and coverage_capacity:
            fresh_limit = max(1, (coverage_capacity * 2 + 2) // 3)
            backlog_limit = max(0, coverage_capacity - fresh_limit)
            commented = "priority > 0 and priority < 40"
            append_lane(
                f"{commented} and list_create_time >= ?",
                fresh_limit,
                (fresh_coverage_after,),
            )
            append_lane(
                f"{commented} and (list_create_time < ? or list_create_time='')",
                backlog_limit,
                (fresh_coverage_after,),
            )
        append_lane("priority > 0", limit)
        append_lane("priority = 0", limit)
        return selected

    def mark_crawler_queue_item(
        self,
        post_id: str,
        *,
        status: str,
        last_error: str = "",
        increment_attempts: bool = True,
        record_observation: bool = False,
        next_attempt_at: str = "",
        commit: bool = True,
    ) -> None:
        self.ensure_crawler_queue(commit=False)
        attempts_sql = "attempts + 1" if increment_attempts else "attempts"
        observation_sql = ""
        if record_observation:
            observation_sql = """
                , last_attempt_list_comment_count=list_comment_count
                , last_attempt_list_update_time=list_update_time
                , same_observation_attempts=same_observation_attempts + 1
            """
        self.conn.execute(
            f"""
            update crawler_queue
            set status=?, last_error=?, attempts={attempts_sql},
                next_attempt_at=?, updated_at=?
                {observation_sql}
            where post_id=?
            """,
            (status, last_error, next_attempt_at, now_text(), str(post_id)),
        )
        if commit:
            self.conn.commit()

    def finish_crawler_queue_detail(
        self,
        post_id: str,
        *,
        detail_comment_count: int,
        retry_delay_seconds: int,
        max_same_observation_attempts: int,
        accept_detail_count: bool = False,
        commit: bool = True,
    ) -> str:
        """Record the exact list observation consumed by a detail response."""
        self.ensure_crawler_queue(commit=False)
        row, observation_attempts = self._crawler_queue_observation_attempt(post_id)
        if row is None:
            return "missing"
        list_count = safe_int(row["list_comment_count"])
        detail_count = safe_int(detail_comment_count)
        if accept_detail_count:
            # A local row-count audit has no newer list observation to defend.
            # Once the response passes payload validation, its current count is
            # authoritative even when comments were deleted upstream.
            list_count = detail_count
        if detail_count >= list_count:
            status = "done"
            next_attempt_at = ""
            last_error = ""
        elif observation_attempts < max(1, int(max_same_observation_attempts)):
            status = "pending"
            next_attempt_at = later_text(retry_delay_seconds)
            last_error = (
                f"list_detail_comment_gap:list={list_count},detail={detail_count},"
                f"retry={observation_attempts}"
            )
        else:
            status = "deferred"
            next_attempt_at = ""
            last_error = (
                f"list_detail_comment_gap:list={list_count},detail={detail_count},"
                f"deferred={observation_attempts}"
            )
        self.conn.execute(
            """
            update crawler_queue
            set status=?, list_comment_count=?, db_comment_count=?, last_error=?,
                attempts=attempts + 1,
                last_attempt_list_comment_count=?,
                last_attempt_list_update_time=list_update_time,
                last_detail_comment_count=?,
                same_observation_attempts=?,
                next_attempt_at=?, updated_at=?
            where post_id=?
            """,
            (
                status,
                list_count,
                detail_count,
                last_error,
                list_count,
                detail_count,
                observation_attempts,
                next_attempt_at,
                now_text(),
                str(post_id),
            ),
        )
        if commit:
            self.conn.commit()
        return status

    def defer_crawler_queue_failure(
        self,
        post_id: str,
        *,
        last_error: str,
        retry_delay_seconds: int,
        max_same_observation_attempts: int,
        commit: bool = True,
    ) -> str:
        self.ensure_crawler_queue(commit=False)
        row, observation_attempts = self._crawler_queue_observation_attempt(post_id)
        if row is None:
            return "missing"
        terminal = observation_attempts >= max(1, int(max_same_observation_attempts))
        status = "failed" if terminal else "pending"
        next_attempt_at = "" if terminal else later_text(retry_delay_seconds)
        self.conn.execute(
            """
            update crawler_queue
            set status=?, last_error=?, attempts=attempts + 1,
                last_attempt_list_comment_count=list_comment_count,
                last_attempt_list_update_time=list_update_time,
                same_observation_attempts=?, next_attempt_at=?, updated_at=?
            where post_id=?
            """,
            (
                status,
                last_error,
                observation_attempts,
                next_attempt_at,
                now_text(),
                str(post_id),
            ),
        )
        if commit:
            self.conn.commit()
        return status

    def _crawler_queue_observation_attempt(
        self,
        post_id: str,
    ) -> tuple[sqlite3.Row | None, int]:
        row = self.conn.execute(
            """
            select list_comment_count, list_update_time,
                   last_attempt_list_comment_count,
                   last_attempt_list_update_time,
                   same_observation_attempts
            from crawler_queue where post_id=?
            """,
            (str(post_id),),
        ).fetchone()
        if row is None:
            return None, 0
        same_observation = (
            row["last_attempt_list_comment_count"] is not None
            and safe_int(row["last_attempt_list_comment_count"])
            == safe_int(row["list_comment_count"])
            and str(row["last_attempt_list_update_time"] or "")
            == str(row["list_update_time"] or "")
        )
        attempts = (
            safe_int(row["same_observation_attempts"]) + 1
            if same_observation
            else 1
        )
        return row, attempts

    def set_state(self, key: str, value: str, commit: bool = True) -> None:
        self.conn.execute(
            "insert into crawl_state values (?,?,?) on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now_text()),
        )
        if commit:
            self.conn.commit()

    def latest_post_id(self) -> str | None:
        row = self.conn.execute(
            "select id from posts order by create_time desc, id desc limit 1"
        ).fetchone()
        return row[0] if row else None
