"""Crawler use cases: detail fill, page scans and complete ID scans."""

from __future__ import annotations

import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crawler.client import MiniProgramClient
from crawler.cookie_pool import CookiePoolClient
from crawler.id_ledger import (
    ledger_state,
    mark_detail_finished,
    mark_detail_started,
    record_list_page,
    set_ledger_state,
)
from crawler.lock import database_write_lock
from crawler.normalizer import (
    normalize_detail,
    validate_normalized_detail,
)
from crawler.strategies.page_scan import PageScanProgress
from crawler.task_routing import (
    TASK_HISTORY_DETAIL,
    TASK_ID_FOLLOWUP,
    TASK_LIST_ACTIVE,
    TASK_LIST_NEW,
    normalize_task_type,
)
from storage.post_writer import SQLitePostStore, has_media_json, safe_int

CHINA_TZ = timezone(timedelta(hours=8))
OBSERVED_MISSING_PRIORITY = 60


class CrawlerService:
    def __init__(
        self,
        *,
        db_path: str | Path,
        cookie: str = "",
        cookie_pool_path: str | Path | None = None,
        lock_timeout: int,
        init_schema: bool = False,
        api_get_fn=None,
    ):
        self.db_path = Path(db_path)
        self.cookie = cookie
        self.cookie_pool_path = (
            Path(cookie_pool_path) if cookie_pool_path else None
        )
        self.lock_timeout = lock_timeout
        self.init_schema = init_schema
        self.api_get_fn = api_get_fn

    def client(self) -> MiniProgramClient | CookiePoolClient:
        if self.cookie_pool_path:
            return CookiePoolClient.from_file(self.cookie_pool_path)
        return MiniProgramClient(self.cookie)

    def _write_lock(self, task_type: str = "", client=None):
        """Use a cooperative lane lock only for explicitly parallel workers.

        The default remains the historical global writer lock.  In parallel
        mode each worker still relies on SQLite WAL and queue claims for
        correctness, while a lane-specific lease prevents duplicate workers
        for the same cookie route from running at once.
        """

        if os.environ.get("CRAWLER_PARALLEL_LANES", "0") != "1":
            return database_write_lock(self.db_path, self.lock_timeout)
        route = getattr(client, "lane_for_task", None)
        lane_id = str(route(task_type) or "") if route and task_type else ""
        if not lane_id:
            return database_write_lock(self.db_path, self.lock_timeout)
        lane_lock = Path(f"{self.db_path}.crawler.{lane_id}.lock")
        return database_write_lock(
            self.db_path,
            self.lock_timeout,
            lock_path=lane_lock,
        )

    @contextmanager
    def _task_scope(self, client, task_type: str):
        """Pin a pooled client to one semantic queue route for one call."""

        task = normalize_task_type(task_type)
        route = getattr(client, "task", None)
        if route is None:
            yield client
            return
        with route(task):
            yield client

    def _article(
        self,
        client: MiniProgramClient,
        post_id: str,
        *,
        task_type: str = TASK_ID_FOLLOWUP,
    ):
        with self._task_scope(client, task_type):
            if self.api_get_fn:
                return self.api_get_fn(
                    client,
                    "/article/article/info",
                    {"community_id": 4, "id": str(post_id)},
                )
            return client.article(post_id)

    def _list_page(
        self,
        client: MiniProgramClient,
        endpoint: str,
        page: int,
        *,
        task_type: str = "",
    ):
        task = task_type or (
            TASK_LIST_ACTIVE if endpoint == "lists2" else TASK_LIST_NEW
        )
        with self._task_scope(client, task):
            if self.api_get_fn:
                return self.api_get_fn(
                    client,
                    f"/article/article/{endpoint}",
                    {"community_id": 4, "page": page},
                )
            return client.list_page(endpoint, page)

    def _latest_id(
        self,
        client: MiniProgramClient,
        *,
        task_type: str = TASK_LIST_NEW,
    ) -> int:
        if not self.api_get_fn:
            with self._task_scope(client, task_type):
                return client.latest_id(task_type=task_type)
        data, error = self._list_page(
            client,
            "lists",
            1,
            task_type=task_type,
        )
        if error:
            raise RuntimeError(f"cannot determine latest id: {error}")
        return max(
            (safe_int(item.get("id")) for item in (data or {}).get("list", [])),
            default=0,
        )

    def fetch_detail(
        self,
        client: MiniProgramClient,
        post_id: str,
        *,
        task_type: str = TASK_ID_FOLLOWUP,
    ) -> tuple[dict, list[dict]] | None:
        data, error = self._article(client, post_id, task_type=task_type)
        if error or not data:
            return None
        parsed = normalize_detail(str(post_id), data)
        if parsed is None:
            return None
        post, comments = parsed
        if validate_normalized_detail(post, comments):
            return None
        return parsed

    @staticmethod
    def article_time(article: dict, key: str) -> str:
        return str(
            article.get(key) or article.get("create_time") or article.get("update_time") or ""
        )

    @staticmethod
    def normalize_cutoff_time(value: str) -> str:
        text = str(value or "").strip()
        if len(text) >= 19 and text[10] == "T":
            return f"{text[:10]} {text[11:19]}"
        return text

    @staticmethod
    def page_signature(articles: list[dict]) -> str:
        return ",".join(str(item.get("id") or "") for item in articles)

    @staticmethod
    def is_rate_limited(error: str | None) -> bool:
        return bool(error and error.startswith("rate_limited:"))

    @staticmethod
    def is_source_quota_stop(error: str | None) -> bool:
        return bool(error and error.startswith("source_quota_"))

    @staticmethod
    def add_queue_snapshot(
        stats: dict,
        *,
        before: dict[str, int],
        after: dict[str, int] | None = None,
    ) -> None:
        for key, value in before.items():
            stats[f"queue_before_{key}"] = safe_int(value)
        if after is None:
            return
        for key, value in after.items():
            current = safe_int(value)
            stats[f"queue_after_{key}"] = current
            stats[f"queue_delta_{key}"] = current - safe_int(before.get(key))

    @staticmethod
    def add_client_source_stats(stats: dict, client) -> None:
        stats["source_calls"] = safe_int(getattr(client, "request_count", 0))
        lane_counts = getattr(client, "lane_request_counts", None)
        if lane_counts:
            stats["cookie_lane_requests"] = {
                str(key): safe_int(value)
                for key, value in lane_counts.items()
            }

    def fetch_detail_with_error(
        self,
        client: MiniProgramClient,
        post_id: str,
        *,
        task_type: str = TASK_ID_FOLLOWUP,
    ) -> tuple[tuple[dict, list[dict]] | None, str | None]:
        data, error = self._article(client, post_id, task_type=task_type)
        if error or not data:
            return None, error or "empty_detail"
        parsed = normalize_detail(str(post_id), data)
        if parsed is None:
            return None, "foreign_or_invalid"
        post, comments = parsed
        payload_error = validate_normalized_detail(post, comments)
        if payload_error:
            return parsed, f"suspicious_payload:{payload_error}"
        return parsed, None

    @staticmethod
    def merge_partial_detail(
        store: SQLitePostStore,
        parsed: tuple[dict, list[dict]],
        payload_error: str,
    ) -> dict[str, int]:
        post, comments = parsed
        return store.merge_partial_post(
            post,
            comments,
            preserve_existing_content=payload_error.endswith("empty_content"),
            commit=False,
        )

    def discover_queue(
        self,
        *,
        command: str,
        endpoint: str,
        since: str,
        max_pages: int,
        old_page_threshold: int,
        stop_on_repeat: bool = True,
        min_pages: int = 3,
        no_action_page_threshold: int = 3,
        dry_run: bool = False,
        write_stubs: bool = True,
        bootstrap: bool = False,
        min_delay: float = 0.1,
        max_delay: float = 0.3,
    ) -> dict:
        run_started_at = datetime.now(CHINA_TZ).isoformat()
        since = self.normalize_cutoff_time(since)
        client = self.client()
        stats = {
            "endpoint": endpoint,
            "pages": 0,
            "seen": 0,
            "observed_ids": 0,
            "missing_observed": 0,
            "observed_missing_queued": 0,
            "retained_ids": 0,
            "queued": 0,
            "queue_inserted": 0,
            "queue_reopened": 0,
            "queue_updated": 0,
            "queue_unchanged": 0,
            "existing": 0,
            "comment_changed": 0,
            "errors": 0,
            "repeat_stop": False,
            "old_page_stop": False,
            "no_action_stop": False,
            "quota_stop": False,
            "bootstrap": bool(bootstrap),
            "bootstrap_complete": False,
            "ledger_baseline": False,
            "ledger_new_ids": 0,
            "ledger_new_events": 0,
            "ledger_fresh_signals": 0,
            "ledger_new_id_signals": 0,
            "ledger_source_change_signals": 0,
            "ledger_event_signals": 0,
            "ledger_actionable": 0,
            "ledger_stable_pages": 0,
            "source_create_time_min": "",
            "source_create_time_max": "",
            "source_calls": 0,
        }
        observed_ids: set[str] = set()
        missing_observed_ids: set[str] = set()
        observed_missing_ids: set[str] = set()
        retained_ids: set[str] = set()
        seen_signatures: dict[str, int] = {}
        old_pages = 0
        no_action_pages = 0
        list_task_type = (
            TASK_LIST_ACTIVE if endpoint == "lists2" else TASK_LIST_NEW
        )
        with self._write_lock(list_task_type, client=client):
            with SQLitePostStore(self.db_path) as store:
                if self.init_schema:
                    store.init_schema()
                else:
                    store.ensure_runtime_schema()
                bootstrap_target_page = max(1, int(max_pages)) if bootstrap else 0
                bootstrap_start_page = 1
                if bootstrap and not dry_run:
                    stored_next_page = safe_int(
                        ledger_state(
                            store.conn,
                            "lists_bootstrap_next_page",
                            default="0",
                        )
                    )
                    if stored_next_page <= 0:
                        inferred = store.conn.execute(
                            """
                            select coalesce(max(first_seen_page), 0)
                            from post_id_ledger
                            where bootstrap_run_id!=''
                            """
                        ).fetchone()[0]
                        stored_next_page = safe_int(inferred) + 1
                    bootstrap_start_page = max(1, stored_next_page)
                    set_ledger_state(
                        store.conn,
                        "lists_bootstrap_next_page",
                        str(bootstrap_start_page),
                    )
                    if bootstrap_start_page > bootstrap_target_page:
                        set_ledger_state(
                            store.conn,
                            "lists_bootstrap_complete",
                            "1",
                        )
                        stats["bootstrap_complete"] = True
                baseline_ready = ledger_state(
                    store.conn,
                    "lists2_baseline_ready",
                    default="0",
                ) in {"1", "true", '"1"', '"true"'}
                if not baseline_ready:
                    baseline_ready = bool(
                        store.conn.execute(
                            "select 1 from list2_observation_log limit 1"
                        ).fetchone()
                    )
                list2_baseline = endpoint == "lists2" and not baseline_ready
                stats["ledger_baseline"] = bool(list2_baseline)
                ledger_run_id = f"{command}:{run_started_at}"
                queue_before = store.crawler_queue_pending_snapshot()
                self.add_queue_snapshot(stats, before=queue_before)
                page_start = bootstrap_start_page if bootstrap else 1
                page_end = bootstrap_target_page if bootstrap else max_pages
                for page in range(page_start, page_end + 1):
                    time.sleep(random.uniform(min_delay, max_delay))
                    data, error = self._list_page(
                        client,
                        endpoint,
                        page,
                        task_type=list_task_type,
                    )
                    if error:
                        if self.is_source_quota_stop(error):
                            stats["quota_stop"] = True
                            print(f"[{command}] stop {error}", flush=True)
                            break
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
                    if stop_on_repeat and not bootstrap and signature in seen_signatures:
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
                    ledger_page = {}
                    if not dry_run:
                        ledger_page = record_list_page(
                            store.conn,
                            run_id=ledger_run_id,
                            endpoint=endpoint,
                            page=page,
                            articles=articles,
                            baseline=list2_baseline,
                            bootstrap=bootstrap,
                            observed_at=datetime.now(CHINA_TZ).isoformat(),
                        )
                        stats["ledger_new_ids"] += safe_int(
                            ledger_page.get("new_ids")
                        )
                        stats["ledger_new_events"] += safe_int(
                            ledger_page.get("new_events")
                        )
                        stats["ledger_fresh_signals"] += safe_int(
                            ledger_page.get("fresh_signals")
                        )
                        stats["ledger_new_id_signals"] += safe_int(
                            ledger_page.get("new_id_signals")
                        )
                        stats["ledger_source_change_signals"] += safe_int(
                            ledger_page.get("source_change_signals")
                        )
                        stats["ledger_event_signals"] += safe_int(
                            ledger_page.get("event_signals")
                        )
                        stats["ledger_actionable"] += safe_int(
                            ledger_page.get("actionable")
                        )
                        if not ledger_page.get("stable", True):
                            stats["ledger_stable_pages"] = 0
                        else:
                            stats["ledger_stable_pages"] += 1
                        source_min = str(
                            ledger_page.get("source_create_time_min") or ""
                        )
                        source_max = str(
                            ledger_page.get("source_create_time_max") or ""
                        )
                        if source_min and (
                            not stats["source_create_time_min"]
                            or source_min < stats["source_create_time_min"]
                        ):
                            stats["source_create_time_min"] = source_min
                        if source_max and (
                            not stats["source_create_time_max"]
                            or source_max > stats["source_create_time_max"]
                        ):
                            stats["source_create_time_max"] = source_max
                    ledger_actionable_ids = set(
                        str(item)
                        for item in ledger_page.get("actionable_ids", [])
                    )
                    page_queued = page_existing = page_changed = page_mutations = 0
                    page_queue_inserted = page_queue_reopened = 0
                    page_has_since = False
                    for article in articles:
                        post_id = str(article.get("id") or "")
                        if not post_id:
                            continue
                        observed_ids.add(post_id)
                        stats["observed_ids"] = len(observed_ids)
                        create_time = self.article_time(article, "create_time")
                        update_time = self.article_time(article, "update_time")
                        comment_count = safe_int(
                            article.get(
                                "comment_count",
                                article.get("count_comment", 0),
                            )
                        )
                        snapshot = store.get_post_crawl_snapshot(post_id)
                        db_comment_count = None if snapshot is None else snapshot["comment_count"]
                        crawl_status = "missing" if snapshot is None else snapshot["crawl_status"]
                        missing = snapshot is None
                        if missing:
                            missing_observed_ids.add(post_id)
                            stats["missing_observed"] = len(missing_observed_ids)
                        needs_detail = missing or crawl_status != "full"
                        create_after_since = create_time >= since
                        update_after_since = update_time >= since
                        if create_after_since or update_after_since:
                            page_has_since = True
                        reason = ""
                        priority = 99
                        if endpoint == "lists":
                            if needs_detail and create_after_since:
                                reason = "new_post"
                                priority = 10 if comment_count > 0 else 40
                        else:
                            if db_comment_count is not None and comment_count > db_comment_count:
                                reason = "comment_changed"
                                priority = 0
                                stats["comment_changed"] += 1
                                page_changed += 1
                            elif post_id in ledger_actionable_ids:
                                reason = "active_event"
                                priority = 0
                                page_changed += 1
                            elif needs_detail and update_after_since:
                                reason = "active_missing"
                                priority = 20 if comment_count > 0 else 50
                        # A list response is already authoritative evidence that
                        # this ID exists. Keep every incomplete ID even when it
                        # falls outside the configured freshness window, but put
                        # it behind all time-sensitive coverage work.
                        if not reason and needs_detail:
                            reason = "observed_missing"
                            priority = OBSERVED_MISSING_PRIORITY
                            observed_missing_ids.add(post_id)
                            stats["observed_missing_queued"] = len(
                                observed_missing_ids
                            )
                        if not dry_run and write_stubs and not bootstrap:
                            # The paid list response already contains current
                            # counters. Refresh them for full posts too without
                            # spending an additional detail request.
                            store.upsert_list_stub(
                                article,
                                source=endpoint,
                                commit=False,
                            )
                        if reason:
                            page_queued += 1
                            stats["queued"] += 1
                            if not dry_run:
                                action = store.enqueue_crawler_candidate(
                                    post_id=post_id,
                                    source=endpoint,
                                    priority=priority,
                                    list_create_time=create_time,
                                    list_update_time=update_time,
                                    list_comment_count=comment_count,
                                    db_comment_count=db_comment_count,
                                    reason=reason,
                                    task_type=TASK_ID_FOLLOWUP,
                                    commit=False,
                                )
                                stats[f"queue_{action}"] += 1
                                if action == "inserted":
                                    page_queue_inserted += 1
                                elif action == "reopened":
                                    page_queue_reopened += 1
                                if action != "unchanged":
                                    page_mutations += 1
                            elif dry_run:
                                page_mutations += 1
                        else:
                            stats["existing"] += 1
                            page_existing += 1
                        if not dry_run:
                            # Existing posts are already durable; incomplete
                            # posts are now durable in posts and/or the queue.
                            retained_ids.add(post_id)
                            stats["retained_ids"] = len(retained_ids)
                    if not dry_run:
                        if bootstrap:
                            set_ledger_state(
                                store.conn,
                                "lists_bootstrap_next_page",
                                str(page + 1),
                            )
                        store.conn.commit()
                    if endpoint == "lists":
                        page_has_effective_signal = bool(
                            safe_int(ledger_page.get("source_change_signals")) > 0
                            or (
                                safe_int(ledger_page.get("new_id_signals")) > 0
                                and page_queue_inserted + page_queue_reopened > 0
                            )
                        )
                    else:
                        page_has_effective_signal = bool(
                            safe_int(ledger_page.get("event_signals")) > 0
                            or safe_int(ledger_page.get("source_change_signals")) > 0
                        )
                    if dry_run:
                        page_has_effective_signal = page_mutations > 0
                    if not page_has_effective_signal:
                        no_action_pages += 1
                    else:
                        no_action_pages = 0
                    if endpoint == "lists" and not page_has_since:
                        old_pages += 1
                    else:
                        old_pages = 0
                    print(
                        f"[{command}:{endpoint}] page={page} "
                        f"articles={len(articles)} queued={page_queued} "
                        f"mutations={page_mutations} "
                        f"existing={page_existing} changed={page_changed} "
                        f"old_pages={old_pages} "
                        f"ledger_stable={ledger_page.get('stable', True)} "
                        f"ledger_events={safe_int(ledger_page.get('new_events'))}",
                        flush=True,
                    )
                    if endpoint == "lists" and old_pages >= old_page_threshold:
                        stats["old_page_stop"] = True
                        print(
                            f"[{command}] stop old_pages={old_pages}",
                            flush=True,
                        )
                        break
                    if (
                        page >= min_pages
                        and no_action_page_threshold > 0
                        and no_action_pages >= no_action_page_threshold
                    ):
                        stats["no_action_stop"] = True
                        print(
                            f"[{command}] stop no_action_pages={no_action_pages}",
                            flush=True,
                        )
                        break
                if (
                    endpoint == "lists2"
                    and not dry_run
                    and not stats["quota_stop"]
                    and stats["pages"] > 0
                ):
                    set_ledger_state(store.conn, "lists2_baseline_ready", "1")
                if (
                    endpoint == "lists"
                    and bootstrap
                    and not dry_run
                    and not stats["quota_stop"]
                ):
                    next_page = safe_int(
                        ledger_state(
                            store.conn,
                            "lists_bootstrap_next_page",
                            default="1",
                        )
                    )
                    if next_page - 1 >= max(1, int(min_pages)):
                        set_ledger_state(
                            store.conn,
                            "lists_bootstrap_complete",
                            "1",
                        )
                        stats["bootstrap_complete"] = True
                queue_after = store.crawler_queue_pending_snapshot()
                self.add_queue_snapshot(
                    stats,
                    before=queue_before,
                    after=queue_after,
                )
                if not dry_run:
                    self.add_client_source_stats(stats, client)
                    store.set_state(
                        f"crawler_{command.replace('-', '_')}",
                        json.dumps(stats, ensure_ascii=False),
                        commit=False,
                    )
                    store.record_crawler_run(
                        command=command,
                        stats=stats,
                        started_at=run_started_at,
                        commit=True,
                    )
        print(
            f"[{command}] done {json.dumps(stats, ensure_ascii=False)} dry_run={dry_run}",
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
        refresh_limit: int | None = None,
        observation_retry_delay: int = 6 * 60 * 60,
        max_observation_attempts: int = 2,
        transient_retry_delay: int = 60 * 60,
        max_transient_attempts: int = 3,
        fresh_coverage_hours: int = 72,
        task_type: str = TASK_ID_FOLLOWUP,
    ) -> dict:
        run_started_at = datetime.now(CHINA_TZ).isoformat()
        task_type = normalize_task_type(task_type)
        client = self.client()
        stats = {
            "limit": limit,
            "task_type": task_type,
            "refresh_limit": None,
            "fresh_coverage_after": "",
            "selected": 0,
            "claimed": 0,
            "claim_conflicts": 0,
            "recovered_claims": 0,
            "written": 0,
            "misses": 0,
            "rate_limited": False,
            "quota_stop": False,
            "suspicious_payloads": 0,
            "partial_saved": 0,
            "partial_completed": 0,
            "partial_comment_rows_added": 0,
            "selected_urgent": 0,
            "selected_refresh": 0,
            "selected_coverage": 0,
            "selected_fresh_coverage": 0,
            "selected_backlog_coverage": 0,
            "selected_quiet_coverage": 0,
            "retry_scheduled": 0,
            "deferred_observations": 0,
            "transient_retries": 0,
            "terminal_failures": 0,
            "completed_details": 0,
            "refreshed_details": 0,
            "new_comment_rows": 0,
            "removed_comment_rows": 0,
            "comment_row_delta": 0,
            "unchanged_comment_rows": 0,
            "posts_with_media": 0,
            "new_media_posts": 0,
            "comment_media_rows": 0,
            "new_comment_media_rows": 0,
            "removed_comment_media_rows": 0,
            "source_calls": 0,
        }
        for lane in ("urgent", "refresh", "coverage"):
            for metric in (
                "written",
                "misses",
                "completed",
                "refreshed",
                "new_comment_rows",
                "comment_row_delta",
                "unchanged_comment_rows",
                "new_media_posts",
                "new_comment_media_rows",
            ):
                stats[f"{metric}_{lane}"] = 0
        stats["completed_fresh_coverage"] = 0
        stats["completed_backlog_coverage"] = 0
        stats["completed_quiet_coverage"] = 0
        consecutive_misses = 0
        with self._write_lock(task_type, client=client):
            with SQLitePostStore(self.db_path) as store:
                if self.init_schema:
                    store.init_schema()
                else:
                    store.ensure_runtime_schema()
                stats["recovered_claims"] = store.recover_expired_crawler_queue_claims(
                    commit=False
                )
                queue_before = store.crawler_queue_pending_snapshot()
                self.add_queue_snapshot(stats, before=queue_before)
                effective_refresh_limit = (
                    None
                    if refresh_limit is None
                    else min(
                        max(0, int(refresh_limit)),
                        max(1, max(1, int(limit)) // 2),
                    )
                )
                stats["refresh_limit"] = effective_refresh_limit
                fresh_coverage_after = (
                    datetime.now(CHINA_TZ)
                    - timedelta(hours=max(1, int(fresh_coverage_hours)))
                ).strftime("%Y-%m-%d %H:%M:%S")
                stats["fresh_coverage_after"] = fresh_coverage_after
                items = store.next_crawler_queue_items(
                    limit,
                    refresh_limit=effective_refresh_limit,
                    fresh_coverage_after=fresh_coverage_after,
                    task_type=task_type,
                )
                stats["selected"] = len(items)
                claim_owner = f"trickle-fill:{os.getpid()}:{run_started_at}"
                claim_ttl_seconds = max(
                    30 * 60,
                    int(max(1.0, float(max_delay)) * max(1, int(limit)) + 10 * 60),
                )
                stats["selected_urgent"] = sum(
                    1 for item in items if safe_int(item["priority"]) < 0
                )
                stats["selected_refresh"] = sum(
                    1 for item in items if safe_int(item["priority"]) == 0
                )
                stats["selected_coverage"] = sum(
                    1 for item in items if safe_int(item["priority"]) > 0
                )
                stats["selected_fresh_coverage"] = sum(
                    1
                    for item in items
                    if 0 < safe_int(item["priority"]) < 40
                    and str(item["list_create_time"] or "") >= fresh_coverage_after
                )
                stats["selected_quiet_coverage"] = sum(
                    1 for item in items if safe_int(item["priority"]) >= 40
                )
                stats["selected_backlog_coverage"] = (
                    stats["selected_coverage"]
                    - stats["selected_fresh_coverage"]
                    - stats["selected_quiet_coverage"]
                )
                for item in items:
                    post_id = str(item["post_id"])
                    priority = safe_int(item["priority"])
                    lane = (
                        "urgent"
                        if priority < 0
                        else "refresh"
                        if priority == 0
                        else "coverage"
                    )
                    is_fresh_coverage = (
                        0 < priority < 40
                        and str(item["list_create_time"] or "")
                        >= fresh_coverage_after
                    )
                    is_quiet_coverage = priority >= 40
                    expected_lane = ""
                    lane_for_task = getattr(client, "lane_for_task", None)
                    if lane_for_task is not None:
                        expected_lane = str(lane_for_task(task_type) or "")
                    claimed = dry_run or store.claim_crawler_queue_item(
                        post_id,
                        owner=claim_owner,
                        lane_id=expected_lane,
                        claim_ttl_seconds=claim_ttl_seconds,
                        commit=False,
                    )
                    if not claimed:
                        stats["claim_conflicts"] += 1
                        continue
                    if not dry_run:
                        stats["claimed"] += 1
                        store.conn.commit()
                        mark_detail_started(store.conn, post_id)
                        store.conn.commit()
                    time.sleep(random.uniform(min_delay, max_delay))
                    parsed, error = self.fetch_detail_with_error(
                        client,
                        post_id,
                        task_type=task_type,
                    )
                    routed_lane = str(getattr(client, "last_lane_id", "") or "")
                    if routed_lane and not dry_run:
                        store.set_crawler_queue_claim_lane(
                            post_id,
                            owner=claim_owner,
                            lane_id=routed_lane,
                            commit=False,
                        )
                    if error:
                        if self.is_source_quota_stop(error):
                            stats["quota_stop"] = True
                            if not dry_run:
                                mark_detail_finished(
                                    store.conn,
                                    post_id,
                                    status="blocked",
                                    error=error,
                                )
                                store.mark_crawler_queue_item(
                                    post_id,
                                    status="pending",
                                    last_error="",
                                    increment_attempts=False,
                                    commit=False,
                                )
                                store.conn.commit()
                            print(f"[trickle-fill] stop {error}", flush=True)
                            break
                        if error in {"not_found", "foreign_or_invalid"}:
                            if not dry_run:
                                mark_detail_finished(
                                    store.conn,
                                    post_id,
                                    status=(
                                        "not_found"
                                        if error == "not_found"
                                        else "failed"
                                    ),
                                    error=error,
                                )
                                store.mark_crawler_queue_item(
                                    post_id,
                                    status="skipped",
                                    last_error=error,
                                    increment_attempts=True,
                                    record_observation=True,
                                    commit=False,
                                )
                                if error == "not_found":
                                    store.mark_post_source_unavailable(
                                        post_id,
                                        error,
                                        commit=False,
                                    )
                                store.conn.commit()
                            stats["misses"] += 1
                            stats[f"misses_{lane}"] += 1
                            print(
                                f"[trickle-fill] skip #{post_id} err={error}",
                                flush=True,
                            )
                            continue
                        if error.startswith("suspicious_payload:"):
                            stats["misses"] += 1
                            stats[f"misses_{lane}"] += 1
                            stats["suspicious_payloads"] += 1
                            if not dry_run:
                                partial = self.merge_partial_detail(
                                    store,
                                    parsed,
                                    error,
                                )
                                stats["partial_saved"] += 1
                                stats["partial_comment_rows_added"] += safe_int(
                                    partial["added_comment_rows"]
                                )
                                mark_detail_finished(
                                    store.conn,
                                    post_id,
                                    status="partial",
                                    comment_count=safe_int(
                                        partial["after_comment_rows"]
                                    ),
                                    error=error,
                                )
                                if error.endswith("empty_content"):
                                    status = store.defer_crawler_queue_failure(
                                        post_id,
                                        last_error=error,
                                        retry_delay_seconds=transient_retry_delay,
                                        max_same_observation_attempts=max_transient_attempts,
                                        commit=False,
                                    )
                                    if status == "pending":
                                        stats["transient_retries"] += 1
                                    else:
                                        stats["terminal_failures"] += 1
                                else:
                                    status = store.finish_crawler_queue_detail(
                                        post_id,
                                        detail_comment_count=safe_int(
                                            partial["after_comment_rows"]
                                        ),
                                        retry_delay_seconds=observation_retry_delay,
                                        max_same_observation_attempts=(
                                            max_observation_attempts
                                        ),
                                        commit=False,
                                    )
                                    if status == "pending":
                                        stats["retry_scheduled"] += 1
                                    elif status == "deferred":
                                        stats["deferred_observations"] += 1
                                    elif status == "done":
                                        stats["partial_completed"] += 1
                                        stats["completed_details"] += 1
                                        stats[f"completed_{lane}"] += 1
                                store.conn.commit()
                            print(
                                f"[trickle-fill] partial #{post_id} err={error}",
                                flush=True,
                            )
                            continue
                        stats["misses"] += 1
                        stats[f"misses_{lane}"] += 1
                        consecutive_misses += 1
                        if self.is_rate_limited(error):
                            stats["rate_limited"] = True
                        if not dry_run:
                            mark_detail_finished(
                                store.conn,
                                post_id,
                                status=(
                                    "blocked"
                                    if error == "cookie_expired"
                                    or self.is_rate_limited(error)
                                    else "failed"
                                ),
                                error=error,
                            )
                            if stats["rate_limited"]:
                                store.mark_crawler_queue_item(
                                    post_id,
                                    status="pending",
                                    last_error=error,
                                    increment_attempts=True,
                                    commit=False,
                                )
                            else:
                                status = store.defer_crawler_queue_failure(
                                    post_id,
                                    last_error=error,
                                    retry_delay_seconds=transient_retry_delay,
                                    max_same_observation_attempts=max_transient_attempts,
                                    commit=False,
                                )
                                if status == "pending":
                                    stats["transient_retries"] += 1
                                else:
                                    stats["terminal_failures"] += 1
                            store.conn.commit()
                        print(
                            f"[trickle-fill] miss #{post_id} err={error}",
                            flush=True,
                        )
                        if stats["rate_limited"]:
                            raise RuntimeError(error)
                        if consecutive_misses >= stop_after_misses:
                            raise RuntimeError(
                                f"too many consecutive detail misses: {consecutive_misses}"
                            )
                        continue
                    consecutive_misses = 0
                    post, comments = parsed
                    before = store.get_post_crawl_snapshot(post_id)
                    before_rows = safe_int(
                        store.conn.execute(
                            "select count(*) from comments where post_id=?",
                            (post_id,),
                        ).fetchone()[0]
                    )
                    before_comment_media_rows = safe_int(
                        store.conn.execute(
                            """
                            select count(*) from comments
                            where post_id=? and media_json not in ('', '{}')
                            """,
                            (post_id,),
                        ).fetchone()[0]
                    )
                    if dry_run:
                        print(
                            f"[trickle-fill] dry #{post_id} "
                            f"c={post['comment_count']} {post['content'][:50]}",
                            flush=True,
                        )
                    else:
                        store.upsert_post(post, comments, commit=False)
                        mark_detail_finished(
                            store.conn,
                            post_id,
                            status="succeeded",
                            comment_count=safe_int(post["comment_count"]),
                            source_update_time=str(
                                post.get("list_update_time")
                                or post.get("update_time")
                                or ""
                            ),
                        )
                        queue_status = store.finish_crawler_queue_detail(
                            post_id,
                            detail_comment_count=safe_int(post["comment_count"]),
                            retry_delay_seconds=observation_retry_delay,
                            max_same_observation_attempts=max_observation_attempts,
                            accept_detail_count=(
                                "comment_rows_incomplete"
                                in str(item["reason"] or "").split("|")
                            ),
                            commit=False,
                        )
                        if queue_status == "pending":
                            stats["retry_scheduled"] += 1
                        elif queue_status == "deferred":
                            stats["deferred_observations"] += 1
                        store.conn.commit()
                        after_rows = safe_int(
                            store.conn.execute(
                                "select count(*) from comments where post_id=?",
                                (post_id,),
                            ).fetchone()[0]
                        )
                        after_comment_media_rows = safe_int(
                            store.conn.execute(
                                """
                                select count(*) from comments
                                where post_id=? and media_json not in ('', '{}')
                                """,
                                (post_id,),
                            ).fetchone()[0]
                        )
                        post_has_media = has_media_json(post.get("media_json"))
                        if post_has_media:
                            stats["posts_with_media"] += 1
                            if before is None or not has_media_json(
                                before.get("media_json")
                            ):
                                stats["new_media_posts"] += 1
                                stats[f"new_media_posts_{lane}"] += 1
                        stats["comment_media_rows"] += after_comment_media_rows
                        added_comment_media = max(
                            0,
                            after_comment_media_rows - before_comment_media_rows,
                        )
                        removed_comment_media = max(
                            0,
                            before_comment_media_rows - after_comment_media_rows,
                        )
                        stats["new_comment_media_rows"] += added_comment_media
                        stats[f"new_comment_media_rows_{lane}"] += added_comment_media
                        stats["removed_comment_media_rows"] += removed_comment_media
                        if before is None or before["crawl_status"] != "full":
                            stats["completed_details"] += 1
                            stats[f"completed_{lane}"] += 1
                            if lane == "coverage":
                                if is_quiet_coverage:
                                    stats["completed_quiet_coverage"] += 1
                                else:
                                    coverage_age = (
                                        "fresh" if is_fresh_coverage else "backlog"
                                    )
                                    stats[
                                        f"completed_{coverage_age}_coverage"
                                    ] += 1
                        else:
                            stats["refreshed_details"] += 1
                            stats[f"refreshed_{lane}"] += 1
                        added_rows = max(0, after_rows - before_rows)
                        removed_rows = max(0, before_rows - after_rows)
                        stats["new_comment_rows"] += added_rows
                        stats[f"new_comment_rows_{lane}"] += added_rows
                        stats["removed_comment_rows"] += removed_rows
                        row_delta = after_rows - before_rows
                        stats["comment_row_delta"] += row_delta
                        stats[f"comment_row_delta_{lane}"] += row_delta
                        if after_rows == before_rows:
                            stats["unchanged_comment_rows"] += 1
                            stats[f"unchanged_comment_rows_{lane}"] += 1
                    stats["written"] += 1
                    stats[f"written_{lane}"] += 1
                    print(
                        f"[trickle-fill] ok #{post_id} "
                        f"written={stats['written']}/{stats['selected']}",
                        flush=True,
                    )
                queue_after = store.crawler_queue_pending_snapshot()
                self.add_queue_snapshot(
                    stats,
                    before=queue_before,
                    after=queue_after,
                )
                if not dry_run:
                    self.add_client_source_stats(stats, client)
                    store.set_state(
                        "crawler_trickle_fill"
                        if task_type == TASK_ID_FOLLOWUP
                        else f"crawler_trickle_fill_{task_type}",
                        json.dumps(stats, ensure_ascii=False),
                        commit=False,
                    )
                    store.record_crawler_run(
                        command=(
                            "trickle-fill"
                            if task_type == TASK_ID_FOLLOWUP
                            else f"trickle-fill:{task_type}"
                        ),
                        stats=stats,
                        started_at=run_started_at,
                        commit=True,
                    )
        print(
            f"[trickle-fill] done {json.dumps(stats, ensure_ascii=False)} dry_run={dry_run}",
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
        task_type: str = TASK_ID_FOLLOWUP,
    ) -> dict:
        requested_ids = [str(post_id).strip() for post_id in ids if str(post_id).strip()]
        unique_ids = list(dict.fromkeys(requested_ids))
        if not unique_ids:
            raise RuntimeError("no ids provided")
        task_type = normalize_task_type(task_type)
        batch_size = max(1, int(batch_size))
        run_started_at = datetime.now(CHINA_TZ).isoformat()
        client = self.client()
        stats = {
            "ids": unique_ids,
            "requested": len(requested_ids),
            "selected": len(unique_ids),
            "task_type": task_type,
            "written": 0,
            "misses": 0,
            "skipped": 0,
            "failed": 0,
            "transient_retries": 0,
            "partial_saved": 0,
            "partial_comment_rows_added": 0,
            "rate_limited": False,
            "quota_stop": False,
            "source_calls": 0,
            "queue_inserted": 0,
            "queue_reopened": 0,
            "queue_updated": 0,
            "queue_unchanged": 0,
        }
        fatal_error = ""
        with database_write_lock(self.db_path, self.lock_timeout):
            with SQLitePostStore(self.db_path) as store:
                if self.init_schema:
                    store.init_schema()
                else:
                    store.ensure_runtime_schema()
                queue_before = store.crawler_queue_pending_snapshot()
                self.add_queue_snapshot(stats, before=queue_before)
                if not dry_run:
                    for post_id in unique_ids:
                        snapshot = store.get_post_crawl_snapshot(post_id)
                        db_comment_count = (
                            None
                            if snapshot is None
                            else safe_int(snapshot["comment_count"])
                        )
                        action = store.enqueue_crawler_candidate(
                            post_id=post_id,
                            source="manual_ids",
                            priority=-10,
                            list_create_time="",
                            list_update_time="",
                            list_comment_count=safe_int(db_comment_count),
                            db_comment_count=db_comment_count,
                            reason="explicit_id",
                            task_type=task_type,
                            commit=False,
                        )
                        stats[f"queue_{action}"] += 1
                    # Every explicit ID is durable before the first source call.
                    store.conn.commit()
                for index, post_id in enumerate(unique_ids, 1):
                    time.sleep(random.uniform(min_delay, max_delay))
                    parsed, error = self.fetch_detail_with_error(
                        client,
                        post_id,
                        task_type=task_type,
                    )
                    if error:
                        if self.is_source_quota_stop(error):
                            stats["quota_stop"] = True
                            print(f"[fill-details] stop {error}", flush=True)
                            break
                        stats["misses"] += 1
                        if self.is_rate_limited(error):
                            stats["rate_limited"] = True
                        if not dry_run:
                            if error in {"not_found", "foreign_or_invalid"}:
                                store.mark_crawler_queue_item(
                                    post_id,
                                    status="skipped",
                                    last_error=error,
                                    increment_attempts=True,
                                    record_observation=True,
                                    commit=False,
                                )
                                if error == "not_found":
                                    store.mark_post_source_unavailable(
                                        post_id,
                                        error,
                                        commit=False,
                                    )
                                stats["skipped"] += 1
                            elif error.startswith("suspicious_payload:"):
                                post, _comments = parsed
                                partial = self.merge_partial_detail(
                                    store,
                                    parsed,
                                    error,
                                )
                                stats["partial_saved"] += 1
                                stats["partial_comment_rows_added"] += safe_int(
                                    partial["added_comment_rows"]
                                )
                                store.enqueue_crawler_candidate(
                                    post_id=post_id,
                                    source="manual_ids",
                                    priority=-10,
                                    list_create_time=str(
                                        post.get("create_time") or ""
                                    ),
                                    list_update_time=str(
                                        post.get("create_time") or ""
                                    ),
                                    list_comment_count=safe_int(
                                        post.get("comment_count")
                                    ),
                                    db_comment_count=safe_int(
                                        partial["after_comment_rows"]
                                    ),
                                    reason="explicit_id",
                                    task_type=task_type,
                                    commit=False,
                                )
                                if error.endswith("empty_content"):
                                    status = store.defer_crawler_queue_failure(
                                        post_id,
                                        last_error=error,
                                        retry_delay_seconds=60 * 60,
                                        max_same_observation_attempts=3,
                                        commit=False,
                                    )
                                else:
                                    status = store.finish_crawler_queue_detail(
                                        post_id,
                                        detail_comment_count=safe_int(
                                            partial["after_comment_rows"]
                                        ),
                                        retry_delay_seconds=6 * 60 * 60,
                                        max_same_observation_attempts=2,
                                        commit=False,
                                    )
                                if status == "pending":
                                    stats["transient_retries"] += 1
                                elif status in {"failed", "deferred"}:
                                    stats["failed"] += 1
                            elif stats["rate_limited"] or error == "cookie_expired":
                                store.mark_crawler_queue_item(
                                    post_id,
                                    status="pending",
                                    last_error=error,
                                    increment_attempts=True,
                                    record_observation=True,
                                    commit=False,
                                )
                            else:
                                status = store.defer_crawler_queue_failure(
                                    post_id,
                                    last_error=error,
                                    retry_delay_seconds=60 * 60,
                                    max_same_observation_attempts=3,
                                    commit=False,
                                )
                                if status == "pending":
                                    stats["transient_retries"] += 1
                                else:
                                    stats["failed"] += 1
                            store.conn.commit()
                        print(
                            f"[fill-details] miss #{post_id} err={error}",
                            flush=True,
                        )
                        if stats["rate_limited"] or error == "cookie_expired":
                            fatal_error = error
                            break
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
                        store.finish_crawler_queue_detail(
                            post_id,
                            detail_comment_count=safe_int(post["comment_count"]),
                            retry_delay_seconds=6 * 60 * 60,
                            max_same_observation_attempts=2,
                            accept_detail_count=True,
                            commit=False,
                        )
                        stats["written"] += 1
                        if stats["written"] % batch_size == 0:
                            store.conn.commit()
                    if index % 20 == 0:
                        print(
                            f"[fill-details] progress {index}/{len(unique_ids)} "
                            f"written={stats['written']} "
                            f"miss={stats['misses']}",
                            flush=True,
                        )
                queue_after = store.crawler_queue_pending_snapshot()
                self.add_queue_snapshot(
                    stats,
                    before=queue_before,
                    after=queue_after,
                )
                if not dry_run:
                    self.add_client_source_stats(stats, client)
                    store.set_state(
                        "crawler_fill_details",
                        json.dumps(stats, ensure_ascii=False),
                        commit=False,
                    )
                    store.record_crawler_run(
                        command="fill-details",
                        stats=stats,
                        started_at=run_started_at,
                        commit=True,
                    )
        print(
            f"[fill-details] done written={stats['written']} "
            f"misses={stats['misses']} dry_run={dry_run}"
        )
        if fatal_error:
            raise RuntimeError(fatal_error)
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
        task_type: str = TASK_ID_FOLLOWUP,
    ) -> dict:
        task_type = normalize_task_type(task_type)
        client = self.client()
        list_task_type = (
            task_type
            if task_type == TASK_HISTORY_DETAIL
            else TASK_LIST_ACTIVE
            if endpoint == "lists2"
            else TASK_LIST_NEW
        )
        stats = {
            "pages": 0,
            "seen": 0,
            "observed_ids": 0,
            "retained_ids": 0,
            "new": 0,
            "updated": 0,
            "unchanged": 0,
            "misses": 0,
            "details": 0,
            "errors": 0,
            "queued": 0,
            "queue_inserted": 0,
            "queue_reopened": 0,
            "queue_updated": 0,
            "queue_unchanged": 0,
        }
        observed_ids: set[str] = set()
        retained_ids: set[str] = set()
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
                    data, error = self._list_page(
                        client,
                        endpoint,
                        page,
                        task_type=list_task_type,
                    )
                    if error:
                        stats["errors"] += 1
                        print(f"[{command}] page={page} err={error}", flush=True)
                        if error == "cookie_expired":
                            raise RuntimeError("crawler authentication expired; update cookie")
                        continue
                    articles = data.get("list", []) if data else []
                    if not articles:
                        print(f"[{command}] page={page} empty stop", flush=True)
                        break
                    stats["pages"] += 1
                    progress.page_read()
                    page_new = page_updated = 0
                    observations = []
                    for article in articles:
                        post_id = str(article.get("id") or "")
                        if not post_id:
                            continue
                        stats["seen"] += 1
                        observed_ids.add(post_id)
                        stats["observed_ids"] = len(observed_ids)
                        comment_count = safe_int(
                            article.get(
                                "comment_count",
                                article.get("count_comment", 0),
                            )
                        )
                        existing = store.get_post_counts(post_id)
                        queue_action = ""
                        if existing is None or existing != comment_count:
                            stats["queued"] += 1
                            if not dry_run:
                                if existing is None:
                                    store.upsert_list_stub(
                                        article,
                                        source=endpoint,
                                        commit=False,
                                    )
                                if existing is None:
                                    if endpoint == "lists":
                                        reason = "new_post"
                                        priority = 10 if comment_count > 0 else 40
                                    else:
                                        reason = "active_missing"
                                        priority = 20 if comment_count > 0 else 50
                                elif comment_count > existing:
                                    reason = "comment_changed"
                                    priority = 0
                                else:
                                    reason = "legacy_changed"
                                    priority = 30
                                queue_action = store.enqueue_crawler_candidate(
                                    post_id=post_id,
                                    source=endpoint,
                                    priority=priority,
                                    list_create_time=self.article_time(
                                        article,
                                        "create_time",
                                    ),
                                    list_update_time=self.article_time(
                                        article,
                                        "update_time",
                                    ),
                                    list_comment_count=comment_count,
                                    db_comment_count=existing,
                                    reason=reason,
                                    task_type=task_type,
                                    commit=False,
                                )
                                stats[f"queue_{queue_action}"] += 1
                        observations.append(
                            {
                                "post_id": post_id,
                                "comment_count": comment_count,
                                "existing": existing,
                                "queue_action": queue_action,
                            }
                        )
                        if not dry_run:
                            retained_ids.add(post_id)
                            stats["retained_ids"] = len(retained_ids)
                    # The paid list response is durable before any detail call.
                    # A crash, detail miss, or max-details stop can no longer
                    # discard IDs from the remainder of this page.
                    if not dry_run:
                        store.conn.commit()
                    for observation in observations:
                        post_id = observation["post_id"]
                        comment_count = observation["comment_count"]
                        existing = observation["existing"]
                        if existing is not None and existing == comment_count:
                            stats["unchanged"] += 1
                            progress.unchanged()
                            continue
                        if max_details and stats["details"] >= max_details:
                            limit_reached = True
                            break
                        parsed = self.fetch_detail(
                            client,
                            post_id,
                            task_type=task_type,
                        )
                        if parsed is None:
                            stats["misses"] += 1
                            if observation["queue_action"] not in {"", "unchanged"}:
                                progress.changed()
                            continue
                        post, comments = parsed
                        stats["details"] += 1
                        if not dry_run:
                            store.upsert_post(post, comments, commit=False)
                            store.finish_crawler_queue_detail(
                                post_id,
                                detail_comment_count=safe_int(post["comment_count"]),
                                retry_delay_seconds=6 * 60 * 60,
                                max_same_observation_attempts=2,
                                commit=False,
                            )
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
                            f"[{command}] stop unchanged_run={progress.consecutive_unchanged}",
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
                f"{command} failed before reading any page ({stats['errors']} request error(s))"
            )
        print(f"[{command}] done {json.dumps(stats, ensure_ascii=False)} dry_run={dry_run}")
        return stats

    @staticmethod
    def parse_date(value: str, option: str) -> str:
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"{option} must use YYYY-MM-DD: {value}") from exc

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
        from_date = self.parse_date(from_date, "--from-date") if from_date else ""
        to_date = self.parse_date(to_date, "--to-date") if to_date else ""
        if from_date and to_date and from_date > to_date:
            raise ValueError("--to-date must not be earlier than --from-date")
        if not start_id and not from_date:
            raise ValueError("provide --start-id or --from-date")
        workers = max(1, int(workers))
        if self.cookie_pool_path and workers > 1:
            print(
                "[scan-id-range] cookie pool enabled; forcing one sequential request stream",
                flush=True,
            )
            workers = 1

        client = self.client()
        with SQLitePostStore(self.db_path) as state_store:
            if self.init_schema:
                state_store.init_schema()
            resolved_start = safe_int(start_id)
            if resolved_start <= 0:
                row = state_store.conn.execute(
                    "select min(cast(id as integer)) from posts where create_time >= ?",
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
                        "select max(cast(id as integer)) from posts where create_time <= ?",
                        (f"{to_date} 23:59:59",),
                    ).fetchone()
                    resolved_end = safe_int(row[0] if row else 0)
                    if resolved_end <= 0:
                        raise RuntimeError(f"cannot determine end id for {to_date}")
                    resolved_end += 100
                else:
                    resolved_end = self._latest_id(
                        client,
                        task_type=TASK_HISTORY_DETAIL,
                    ) + 100
            if resolved_end < resolved_start:
                raise ValueError(f"end id {resolved_end} is earlier than start id {resolved_start}")

            # Keep the historical key so in-progress production scans resume
            # across the architecture migration.
            state_key = f"crawler_db_phase1_{resolved_start}_{resolved_end}"
            row = state_store.conn.execute(
                "select value from crawl_state where key=?", (state_key,)
            ).fetchone()
            saved = json.loads(row[0]) if row and not restart else {}
            if saved.get("complete"):
                print(
                    f"[scan-id-range] already complete range={resolved_start}..{resolved_end}",
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
                data, error = self._article(
                    local.client,
                    str(post_id),
                    task_type=TASK_HISTORY_DETAIL,
                )
                if error == "cookie_expired":
                    return post_id, None, "cookie_expired"
                if self.is_rate_limited(error):
                    return post_id, None, str(error)
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
                post, comments = parsed
                payload_error = validate_normalized_detail(post, comments)
                if payload_error:
                    return post_id, parsed, f"suspicious:{payload_error}"
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
            "saved_filtered": safe_int(saved.get("saved_filtered")),
            "suspicious": safe_int(saved.get("suspicious")),
            "partial_saved": safe_int(saved.get("partial_saved")),
            "partial_comment_rows_added": safe_int(
                saved.get("partial_comment_rows_added")
            ),
            "missing": safe_int(saved.get("missing")),
            "foreign": safe_int(saved.get("foreign")),
            "errors": safe_int(saved.get("errors")),
            "started_at": saved.get("started_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        from_time = f"{from_date} 00:00:00" if from_date else ""
        to_time = f"{to_date} 23:59:59" if to_date else ""

        with database_write_lock(self.db_path, self.lock_timeout):
            with SQLitePostStore(self.db_path) as store:
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    chunk_start = next_id
                    while chunk_start <= resolved_end:
                        chunk_end = min(resolved_end, chunk_start + chunk_size - 1)
                        results = executor.map(scan_one, range(chunk_start, chunk_end + 1))
                        stop_error = ""
                        chunk_errors = 0
                        for post_id, parsed, status in results:
                            stats["processed"] += 1
                            if status == "ok":
                                post, comments = parsed
                                in_range = (not from_time or post["create_time"] >= from_time) and (
                                    not to_time or post["create_time"] <= to_time
                                )
                                existing = store.get_post_counts(post_id)
                                if not dry_run:
                                    store.upsert_post(post, comments, commit=False)
                                if in_range:
                                    key = "new" if existing is None else "refreshed"
                                    stats[key] += 1
                                else:
                                    stats["filtered"] += 1
                                    if not dry_run:
                                        stats["saved_filtered"] += 1
                            elif status.startswith("suspicious:"):
                                stats["suspicious"] += 1
                                if not dry_run:
                                    post, _comments = parsed
                                    partial = store.merge_partial_post(
                                        post,
                                        _comments,
                                        preserve_existing_content=(
                                            status == "suspicious:empty_content"
                                        ),
                                        commit=False,
                                    )
                                    stats["partial_saved"] += 1
                                    stats[
                                        "partial_comment_rows_added"
                                    ] += safe_int(
                                        partial["added_comment_rows"]
                                    )
                                    store.enqueue_crawler_candidate(
                                        post_id=str(post_id),
                                        source="id_range",
                                        priority=15,
                                        list_create_time=str(post.get("create_time") or ""),
                                        list_update_time=str(post.get("create_time") or ""),
                                        list_comment_count=safe_int(
                                            post.get("comment_count")
                                        ),
                                        db_comment_count=safe_int(
                                            partial["after_comment_rows"]
                                        ),
                                        reason="id_range_suspicious",
                                        task_type=TASK_HISTORY_DETAIL,
                                        commit=False,
                                    )
                            elif status in ("missing", "foreign"):
                                stats[status] += 1
                            else:
                                stats["errors"] += 1
                                chunk_errors += 1
                                if status == "cookie_expired" or status.startswith(
                                    "rate_limited:"
                                ):
                                    stop_error = status
                        stats["next_id"] = chunk_start if chunk_errors else chunk_end + 1
                        state = {
                            **stats,
                            "complete": False,
                            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
                            reason = stop_error or "request errors"
                            raise RuntimeError(f"{reason}; retry from id {chunk_start}")
                        chunk_start = chunk_end + 1
                final_state = {
                    **stats,
                    "next_id": resolved_end + 1,
                    "complete": True,
                    "completed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
