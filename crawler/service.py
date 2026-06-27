"""Crawler use cases: detail fill, page scans and complete ID scans."""

from __future__ import annotations

import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from crawler.client import MiniProgramClient
from crawler.lock import database_write_lock
from crawler.normalizer import normalize_detail
from crawler.strategies.page_scan import PageScanProgress
from storage.post_writer import SQLitePostStore, safe_int


class CrawlerService:
    def __init__(
        self,
        *,
        db_path: str | Path,
        cookie: str,
        lock_timeout: int,
        init_schema: bool = False,
        api_get_fn=None,
    ):
        self.db_path = Path(db_path)
        self.cookie = cookie
        self.lock_timeout = lock_timeout
        self.init_schema = init_schema
        self.api_get_fn = api_get_fn

    def client(self) -> MiniProgramClient:
        return MiniProgramClient(self.cookie)

    def _article(self, client: MiniProgramClient, post_id: str):
        if self.api_get_fn:
            return self.api_get_fn(
                client,
                "/article/article/info",
                {"community_id": 4, "id": str(post_id)},
            )
        return client.article(post_id)

    def _list_page(self, client: MiniProgramClient, endpoint: str, page: int):
        if self.api_get_fn:
            return self.api_get_fn(
                client,
                f"/article/article/{endpoint}",
                {"community_id": 4, "page": page},
            )
        return client.list_page(endpoint, page)

    def _latest_id(self, client: MiniProgramClient) -> int:
        if not self.api_get_fn:
            return client.latest_id()
        data, error = self._list_page(client, "lists", 1)
        if error:
            raise RuntimeError(f"cannot determine latest id: {error}")
        return max(
            (
                safe_int(item.get("id"))
                for item in (data or {}).get("list", [])
            ),
            default=0,
        )

    def fetch_detail(
        self,
        client: MiniProgramClient,
        post_id: str,
    ) -> tuple[dict, list[dict]] | None:
        data, error = self._article(client, post_id)
        if error or not data:
            return None
        return normalize_detail(str(post_id), data)

    @staticmethod
    def article_time(article: dict, key: str) -> str:
        return str(
            article.get(key)
            or article.get("create_time")
            or article.get("update_time")
            or ""
        )

    @staticmethod
    def page_signature(articles: list[dict]) -> str:
        return ",".join(str(item.get("id") or "") for item in articles)

    @staticmethod
    def is_rate_limited(error: str | None) -> bool:
        return bool(error and error.startswith("rate_limited:"))

    def fetch_detail_with_error(
        self,
        client: MiniProgramClient,
        post_id: str,
    ) -> tuple[tuple[dict, list[dict]] | None, str | None]:
        data, error = self._article(client, post_id)
        if error or not data:
            return None, error or "empty_detail"
        parsed = normalize_detail(str(post_id), data)
        if parsed is None:
            return None, "foreign_or_invalid"
        return parsed, None

    def discover_queue(
        self,
        *,
        command: str,
        endpoint: str,
        since: str,
        max_pages: int,
        old_page_threshold: int,
        stop_on_repeat: bool,
        dry_run: bool,
        min_delay: float,
        max_delay: float,
    ) -> dict:
        client = self.client()
        stats = {
            "endpoint": endpoint,
            "pages": 0,
            "seen": 0,
            "queued": 0,
            "existing": 0,
            "comment_changed": 0,
            "errors": 0,
            "repeat_stop": False,
            "old_page_stop": False,
        }
        seen_signatures: dict[str, int] = {}
        old_pages = 0
        with database_write_lock(self.db_path, self.lock_timeout):
            with SQLitePostStore(self.db_path) as store:
                if self.init_schema:
                    store.init_schema()
                else:
                    store.ensure_crawler_queue()
                for page in range(1, max_pages + 1):
                    time.sleep(random.uniform(min_delay, max_delay))
                    data, error = self._list_page(client, endpoint, page)
                    if error:
                        stats["errors"] += 1
                        print(f"[{command}] page={page} err={error}", flush=True)
                        if error == "cookie_expired" or self.is_rate_limited(error):
                            raise RuntimeError(error)
                        continue
                    articles = data.get("list", []) if data else []
                    if not articles:
                        print(f"[{command}] page={page} empty stop", flush=True)
                        break
                    signature = self.page_signature(articles)
                    if stop_on_repeat and signature in seen_signatures:
                        stats["repeat_stop"] = True
                        print(
                            f"[{command}] page={page} repeats page="
                            f"{seen_signatures[signature]} stop",
                            flush=True,
                        )
                        break
                    seen_signatures[signature] = page
                    stats["pages"] += 1
                    stats["seen"] += len(articles)
                    page_queued = page_existing = page_changed = 0
                    page_has_since = False
                    for article in articles:
                        post_id = str(article.get("id") or "")
                        if not post_id:
                            continue
                        create_time = self.article_time(article, "create_time")
                        update_time = self.article_time(article, "update_time")
                        comment_count = safe_int(
                            article.get(
                                "comment_count",
                                article.get("count_comment", 0),
                            )
                        )
                        db_comment_count = store.get_post_counts(post_id)
                        missing = db_comment_count is None
                        create_after_since = create_time >= since
                        update_after_since = update_time >= since
                        comment_changed = (
                            db_comment_count is not None
                            and db_comment_count != comment_count
                        )
                        if create_after_since or update_after_since:
                            page_has_since = True
                        reason = ""
                        priority = 99
                        if endpoint == "lists":
                            if missing and create_after_since:
                                reason = "new_post"
                                priority = 10
                        else:
                            if comment_changed:
                                reason = "comment_changed"
                                priority = 0
                                stats["comment_changed"] += 1
                                page_changed += 1
                            elif missing and create_after_since:
                                reason = "active_missing"
                                priority = 20
                            elif update_after_since:
                                reason = "active_updated"
                                priority = 30
                        if reason:
                            page_queued += 1
                            stats["queued"] += 1
                            if not dry_run:
                                store.enqueue_crawler_candidate(
                                    post_id=post_id,
                                    source=endpoint,
                                    priority=priority,
                                    list_create_time=create_time,
                                    list_update_time=update_time,
                                    list_comment_count=comment_count,
                                    db_comment_count=db_comment_count,
                                    reason=reason,
                                    commit=False,
                                )
                        else:
                            stats["existing"] += 1
                            page_existing += 1
                    if not dry_run:
                        store.conn.commit()
                    if endpoint == "lists" and not page_has_since:
                        old_pages += 1
                    else:
                        old_pages = 0
                    print(
                        f"[{command}:{endpoint}] page={page} "
                        f"articles={len(articles)} queued={page_queued} "
                        f"existing={page_existing} changed={page_changed} "
                        f"old_pages={old_pages}",
                        flush=True,
                    )
                    if endpoint == "lists" and old_pages >= old_page_threshold:
                        stats["old_page_stop"] = True
                        print(
                            f"[{command}] stop old_pages={old_pages}",
                            flush=True,
                        )
                        break
                if not dry_run:
                    store.set_state(
                        f"crawler_{command.replace('-', '_')}",
                        json.dumps(stats, ensure_ascii=False),
                        commit=True,
                    )
        print(
            f"[{command}] done {json.dumps(stats, ensure_ascii=False)} "
            f"dry_run={dry_run}",
            flush=True,
        )
        return stats

    def trickle_fill(
        self,
        *,
        limit: int,
        dry_run: bool,
        min_delay: float,
        max_delay: float,
        stop_after_misses: int,
    ) -> dict:
        client = self.client()
        stats = {
            "limit": limit,
            "selected": 0,
            "written": 0,
            "misses": 0,
            "rate_limited": False,
        }
        consecutive_misses = 0
        with database_write_lock(self.db_path, self.lock_timeout):
            with SQLitePostStore(self.db_path) as store:
                if self.init_schema:
                    store.init_schema()
                else:
                    store.ensure_crawler_queue()
                items = store.next_crawler_queue_items(limit)
                stats["selected"] = len(items)
                for item in items:
                    post_id = str(item["post_id"])
                    time.sleep(random.uniform(min_delay, max_delay))
                    parsed, error = self.fetch_detail_with_error(client, post_id)
                    if error:
                        stats["misses"] += 1
                        consecutive_misses += 1
                        status = "failed"
                        if self.is_rate_limited(error):
                            stats["rate_limited"] = True
                            status = "pending"
                        if not dry_run:
                            store.mark_crawler_queue_item(
                                post_id,
                                status=status,
                                last_error=error,
                                increment_attempts=True,
                                commit=False,
                            )
                            store.conn.commit()
                        print(
                            f"[trickle-fill] miss #{post_id} err={error}",
                            flush=True,
                        )
                        if stats["rate_limited"]:
                            raise RuntimeError(error)
                        if consecutive_misses >= stop_after_misses:
                            raise RuntimeError(
                                f"too many consecutive detail misses: "
                                f"{consecutive_misses}"
                            )
                        continue
                    consecutive_misses = 0
                    post, comments = parsed
                    if dry_run:
                        print(
                            f"[trickle-fill] dry #{post_id} "
                            f"c={post['comment_count']} {post['content'][:50]}",
                            flush=True,
                        )
                    else:
                        store.upsert_post(post, comments, commit=False)
                        store.mark_crawler_queue_item(
                            post_id,
                            status="done",
                            last_error="",
                            increment_attempts=True,
                            commit=False,
                        )
                        store.conn.commit()
                    stats["written"] += 1
                    print(
                        f"[trickle-fill] ok #{post_id} "
                        f"written={stats['written']}/{stats['selected']}",
                        flush=True,
                    )
                if not dry_run:
                    store.set_state(
                        "crawler_trickle_fill",
                        json.dumps(stats, ensure_ascii=False),
                        commit=True,
                    )
        print(
            f"[trickle-fill] done {json.dumps(stats, ensure_ascii=False)} "
            f"dry_run={dry_run}",
            flush=True,
        )
        return stats

    def fill_details(
        self,
        ids: list[str],
        *,
        dry_run: bool,
        batch_size: int,
        min_delay: float,
        max_delay: float,
    ) -> dict:
        if not ids:
            raise RuntimeError("no ids provided")
        client = self.client()
        stats = {"ids": ids, "written": 0, "misses": 0}
        with database_write_lock(self.db_path, self.lock_timeout):
            with SQLitePostStore(self.db_path) as store:
                if self.init_schema:
                    store.init_schema()
                for index, post_id in enumerate(ids, 1):
                    time.sleep(random.uniform(min_delay, max_delay))
                    parsed = self.fetch_detail(client, post_id)
                    if parsed is None:
                        stats["misses"] += 1
                        print(f"[fill-details] miss #{post_id}", flush=True)
                        continue
                    post, comments = parsed
                    if dry_run:
                        print(
                            f"[fill-details] dry #{post_id} "
                            f"c={post['comment_count']} {post['content'][:50]}",
                            flush=True,
                        )
                    else:
                        store.upsert_post(post, comments, commit=False)
                        stats["written"] += 1
                        if stats["written"] % batch_size == 0:
                            store.conn.commit()
                    if index % 20 == 0:
                        print(
                            f"[fill-details] progress {index}/{len(ids)} "
                            f"written={stats['written']} "
                            f"miss={stats['misses']}",
                            flush=True,
                        )
                if not dry_run:
                    store.set_state(
                        "crawler_fill_details",
                        json.dumps(stats, ensure_ascii=False),
                        commit=False,
                    )
                    store.conn.commit()
        print(
            f"[fill-details] done written={stats['written']} "
            f"misses={stats['misses']} dry_run={dry_run}"
        )
        return stats

    def scan_pages(
        self,
        *,
        command: str,
        endpoint: str,
        start_page: int,
        pages: int,
        min_pages: int,
        stop_unchanged: int,
        max_details: int,
        dry_run: bool,
        min_delay: float,
        max_delay: float,
    ) -> dict:
        client = self.client()
        stats = {
            "pages": 0,
            "seen": 0,
            "new": 0,
            "updated": 0,
            "unchanged": 0,
            "misses": 0,
            "details": 0,
            "errors": 0,
        }
        progress = PageScanProgress()
        limit_reached = False
        with database_write_lock(self.db_path, self.lock_timeout):
            with SQLitePostStore(self.db_path) as store:
                if self.init_schema:
                    store.init_schema()
                for page in range(start_page, start_page + pages):
                    if limit_reached:
                        break
                    time.sleep(random.uniform(min_delay, max_delay))
                    data, error = self._list_page(client, endpoint, page)
                    if error:
                        stats["errors"] += 1
                        print(
                            f"[{command}] page={page} err={error}", flush=True
                        )
                        if error == "cookie_expired":
                            raise RuntimeError(
                                "crawler authentication expired; update cookie"
                            )
                        continue
                    articles = data.get("list", []) if data else []
                    if not articles:
                        print(f"[{command}] page={page} empty stop", flush=True)
                        break
                    stats["pages"] += 1
                    progress.page_read()
                    page_new = page_updated = 0
                    for article in articles:
                        post_id = str(article.get("id") or "")
                        if not post_id:
                            continue
                        stats["seen"] += 1
                        comment_count = safe_int(
                            article.get(
                                "comment_count",
                                article.get("count_comment", 0),
                            )
                        )
                        existing = store.get_post_counts(post_id)
                        if existing is not None and existing == comment_count:
                            stats["unchanged"] += 1
                            progress.unchanged()
                            continue
                        if max_details and stats["details"] >= max_details:
                            limit_reached = True
                            break
                        parsed = self.fetch_detail(client, post_id)
                        if parsed is None:
                            stats["misses"] += 1
                            continue
                        post, comments = parsed
                        stats["details"] += 1
                        if not dry_run:
                            store.upsert_post(post, comments, commit=False)
                        if existing is None:
                            stats["new"] += 1
                            page_new += 1
                        else:
                            stats["updated"] += 1
                            page_updated += 1
                        progress.changed()
                    if not dry_run:
                        store.conn.commit()
                    print(
                        f"[{command}:{endpoint}] page={page} "
                        f"articles={len(articles)} new={page_new} "
                        f"updated={page_updated} "
                        f"unchanged_run={progress.consecutive_unchanged}",
                        flush=True,
                    )
                    if progress.should_stop(
                        min_pages=min_pages,
                        threshold=stop_unchanged,
                    ):
                        print(
                            f"[{command}] stop unchanged_run="
                            f"{progress.consecutive_unchanged}",
                            flush=True,
                        )
                        break
                if not dry_run:
                    store.set_state(
                        f"crawler_{command.replace('-', '_')}",
                        json.dumps(stats, ensure_ascii=False),
                        commit=True,
                    )
        if stats["pages"] == 0 and stats["errors"]:
            raise RuntimeError(
                f"{command} failed before reading any page "
                f"({stats['errors']} request error(s))"
            )
        print(
            f"[{command}] done {json.dumps(stats, ensure_ascii=False)} "
            f"dry_run={dry_run}"
        )
        return stats

    @staticmethod
    def parse_date(value: str, option: str) -> str:
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(
                f"{option} must use YYYY-MM-DD: {value}"
            ) from exc

    def scan_id_range(
        self,
        *,
        from_date: str,
        to_date: str,
        start_id: int,
        end_id: int,
        workers: int,
        chunk_size: int,
        restart: bool,
        dry_run: bool,
    ) -> dict:
        from_date = (
            self.parse_date(from_date, "--from-date") if from_date else ""
        )
        to_date = self.parse_date(to_date, "--to-date") if to_date else ""
        if from_date and to_date and from_date > to_date:
            raise ValueError("--to-date must not be earlier than --from-date")
        if not start_id and not from_date:
            raise ValueError("provide --start-id or --from-date")

        probe = self.client()
        with SQLitePostStore(self.db_path) as state_store:
            if self.init_schema:
                state_store.init_schema()
            resolved_start = safe_int(start_id)
            if resolved_start <= 0:
                row = state_store.conn.execute(
                    "select min(cast(id as integer)) from posts "
                    "where create_time >= ?",
                    (f"{from_date} 00:00:00",),
                ).fetchone()
                resolved_start = safe_int(row[0] if row else 0)
            if resolved_start <= 0:
                raise RuntimeError(f"cannot determine start id for {from_date}")
            if not start_id:
                resolved_start = max(1, resolved_start - 100)

            resolved_end = safe_int(end_id)
            if resolved_end <= 0:
                if to_date:
                    row = state_store.conn.execute(
                        "select max(cast(id as integer)) from posts "
                        "where create_time <= ?",
                        (f"{to_date} 23:59:59",),
                    ).fetchone()
                    resolved_end = safe_int(row[0] if row else 0)
                    if resolved_end <= 0:
                        raise RuntimeError(
                            f"cannot determine end id for {to_date}"
                        )
                    resolved_end += 100
                else:
                    resolved_end = self._latest_id(probe) + 100
            if resolved_end < resolved_start:
                raise ValueError(
                    f"end id {resolved_end} is earlier than "
                    f"start id {resolved_start}"
                )

            # Keep the historical key so in-progress production scans resume
            # across the architecture migration.
            state_key = f"crawler_db_phase1_{resolved_start}_{resolved_end}"
            row = state_store.conn.execute(
                "select value from crawl_state where key=?", (state_key,)
            ).fetchone()
            saved = json.loads(row[0]) if row and not restart else {}
            if saved.get("complete"):
                print(
                    f"[scan-id-range] already complete "
                    f"range={resolved_start}..{resolved_end}",
                    flush=True,
                )
                return saved
            next_id = max(
                resolved_start,
                safe_int(saved.get("next_id"), resolved_start),
            )

        local = threading.local()

        def scan_one(post_id):
            if not hasattr(local, "client"):
                local.client = self.client()
            time.sleep(random.uniform(0.15, 0.4))
            last_error = ""
            for attempt in range(3):
                data, error = self._article(local.client, str(post_id))
                if error == "cookie_expired":
                    return post_id, None, "cookie_expired"
                if error == "not_found":
                    return post_id, None, "missing"
                if error:
                    last_error = error
                    if attempt < 2:
                        time.sleep(1.0 + attempt)
                        continue
                    return post_id, None, f"error:{last_error}"
                if not data:
                    return post_id, None, "missing"
                parsed = normalize_detail(str(post_id), data)
                if parsed is None:
                    return post_id, None, "foreign"
                return post_id, parsed, "ok"
            return post_id, None, f"error:{last_error}"

        stats = {
            "start_id": resolved_start,
            "end_id": resolved_end,
            "next_id": next_id,
            "processed": safe_int(saved.get("processed")),
            "new": safe_int(saved.get("new")),
            "refreshed": safe_int(saved.get("refreshed")),
            "filtered": safe_int(saved.get("filtered")),
            "missing": safe_int(saved.get("missing")),
            "foreign": safe_int(saved.get("foreign")),
            "errors": safe_int(saved.get("errors")),
            "started_at": saved.get("started_at")
            or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        from_time = f"{from_date} 00:00:00" if from_date else ""
        to_time = f"{to_date} 23:59:59" if to_date else ""

        with database_write_lock(self.db_path, self.lock_timeout):
            with SQLitePostStore(self.db_path) as store:
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    chunk_start = next_id
                    while chunk_start <= resolved_end:
                        chunk_end = min(
                            resolved_end, chunk_start + chunk_size - 1
                        )
                        results = executor.map(
                            scan_one, range(chunk_start, chunk_end + 1)
                        )
                        cookie_expired = False
                        chunk_errors = 0
                        for post_id, parsed, status in results:
                            stats["processed"] += 1
                            if status == "ok":
                                post, comments = parsed
                                in_range = (
                                    not from_time
                                    or post["create_time"] >= from_time
                                ) and (
                                    not to_time
                                    or post["create_time"] <= to_time
                                )
                                if in_range:
                                    existing = store.get_post_counts(post_id)
                                    if not dry_run:
                                        store.upsert_post(
                                            post, comments, commit=False
                                        )
                                    key = (
                                        "new"
                                        if existing is None
                                        else "refreshed"
                                    )
                                    stats[key] += 1
                                else:
                                    stats["filtered"] += 1
                            elif status in ("missing", "foreign"):
                                stats[status] += 1
                            else:
                                stats["errors"] += 1
                                chunk_errors += 1
                                cookie_expired |= status == "cookie_expired"
                        stats["next_id"] = (
                            chunk_start if chunk_errors else chunk_end + 1
                        )
                        state = {
                            **stats,
                            "complete": False,
                            "updated_at": datetime.now().strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                        }
                        if not dry_run:
                            store.set_state(
                                state_key,
                                json.dumps(state, ensure_ascii=False),
                                commit=False,
                            )
                            store.conn.commit()
                        print(
                            f"[scan-id-range] {chunk_start}..{chunk_end} "
                            f"processed={stats['processed']} "
                            f"new={stats['new']} "
                            f"refreshed={stats['refreshed']} "
                            f"errors={stats['errors']}",
                            flush=True,
                        )
                        if chunk_errors:
                            reason = (
                                "cookie_expired"
                                if cookie_expired
                                else "request errors"
                            )
                            raise RuntimeError(
                                f"{reason}; retry from id {chunk_start}"
                            )
                        chunk_start = chunk_end + 1
                final_state = {
                    **stats,
                    "next_id": resolved_end + 1,
                    "complete": True,
                    "completed_at": datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                }
                if not dry_run:
                    store.set_state(
                        state_key,
                        json.dumps(final_state, ensure_ascii=False),
                        commit=True,
                    )
        print(
            "[scan-id-range] done",
            json.dumps(final_state, ensure_ascii=False),
            flush=True,
        )
        return final_state
