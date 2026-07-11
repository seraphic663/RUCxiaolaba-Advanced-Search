"""Administrator-controlled upstream preview and immediate detail crawling."""

from __future__ import annotations

import os
import random
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from app.repositories.admin_crawl_repository import AdminCrawlRepository
from crawler.client import MiniProgramClient, load_cookie
from crawler.lock import database_write_lock
from crawler.manual_quota import ManualQuota, ManualQuotaError
from crawler.normalizer import normalize_detail
from storage.post_writer import SQLitePostStore, safe_int


class AdminCrawlError(RuntimeError):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class AdminCrawlService:
    SOURCES = {"search", "lists", "lists2"}
    STRATEGIES = {"smart", "force", "queue"}

    def __init__(
        self,
        posts_db: str | Path,
        *,
        config_path: str | Path,
        bigram_db: str | Path | None = None,
        symbol_db: str | Path | None = None,
        control_db: str | Path | None = None,
        quota: ManualQuota | None = None,
        client_factory=None,
        min_delay: float | None = None,
        max_delay: float | None = None,
    ):
        self.posts_db = Path(posts_db)
        self.config_path = Path(config_path)
        self.bigram_db = Path(bigram_db) if bigram_db else None
        self.symbol_db = Path(symbol_db) if symbol_db else None
        self.control_db = Path(
            control_db
            or os.environ.get(
                "ADMIN_CRAWL_DB",
                str(self.posts_db.with_name(".admin_crawl.db")),
            )
        )
        self.quota = quota or ManualQuota(self.posts_db)
        self.client_factory = client_factory or self._default_client
        self.min_delay = (
            float(os.environ.get("CRAWLER_ADMIN_MIN_DELAY", "8"))
            if min_delay is None
            else min_delay
        )
        self.max_delay = max(
            self.min_delay,
            float(os.environ.get("CRAWLER_ADMIN_MAX_DELAY", "14"))
            if max_delay is None
            else max_delay,
        )
        self._worker_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self.repository = AdminCrawlRepository(self.control_db)
        self._recover_jobs()

    def _recover_jobs(self) -> None:
        self.repository.recover_jobs()
        self._start_worker_if_needed()

    def _default_client(self):
        return MiniProgramClient(load_cookie(self.config_path))

    @staticmethod
    def _candidate(article: dict, source: str) -> dict | None:
        post_id = str(article.get("id") or "").strip()
        if not post_id.isdigit():
            return None
        content = f"{article.get('title') or ''} {article.get('detail') or ''}".strip()
        return {
            "id": post_id,
            "content": content[:1000],
            "category_name": str(article.get("category_name") or ""),
            "user_name": str(article.get("show_user_name") or ""),
            "create_time": str(article.get("create_time") or ""),
            "update_time": str(article.get("update_time") or ""),
            "comment_count": safe_int(
                article.get("comment_count", article.get("count_comment", 0))
            ),
            "source": source,
        }

    def _add_local_state(self, candidates: list[dict]) -> None:
        if not candidates or not self.posts_db.exists():
            for item in candidates:
                item.update(
                    local_exists=False,
                    local_status="missing",
                    local_comment_count=0,
                    recommended=True,
                )
            return
        conn = sqlite3.connect(f"file:{self.posts_db.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            columns = {row[1] for row in conn.execute("pragma table_info(posts)")}
            status_sql = "crawl_status" if "crawl_status" in columns else "'full'"
            ids = [item["id"] for item in candidates]
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"select id, comment_count, {status_sql} as crawl_status "
                f"from posts where id in ({placeholders})",
                ids,
            ).fetchall()
            local = {str(row["id"]): row for row in rows}
            for item in candidates:
                row = local.get(item["id"])
                local_count = safe_int(row["comment_count"]) if row else 0
                status = str(row["crawl_status"] or "full") if row else "missing"
                item.update(
                    local_exists=row is not None,
                    local_status=status,
                    local_comment_count=local_count,
                    recommended=(
                        row is None
                        or status != "full"
                        or item["comment_count"] > local_count
                    ),
                )
        finally:
            conn.close()

    def preview(self, source: str, query: str, pages: int) -> dict:
        source = str(source or "").strip()
        query = str(query or "").strip()
        pages = max(1, min(int(pages), 3))
        if source not in self.SOURCES:
            raise AdminCrawlError("invalid_source", "不支持的上游来源")
        if source == "search" and len(query) < 2:
            raise AdminCrawlError("invalid_query", "上游搜索至少输入两个字符")
        try:
            client = self.client_factory()
        except Exception as exc:
            raise AdminCrawlError("client_unavailable", str(exc), 503) from exc
        candidates: dict[str, dict] = {}
        calls = 0
        for page in range(1, pages + 1):
            kind = "active_list" if source == "lists2" else "new_list"
            try:
                self.quota.reserve(kind, "preview", 1)
            except ManualQuotaError as exc:
                raise AdminCrawlError(exc.code, str(exc), 429) from exc
            calls += 1
            if source == "search":
                data, error = client.search(query, page)
            else:
                data, error = client.list_page(source, page)
            if error:
                if str(error).startswith("rate_limited:"):
                    self.quota.pause_for_rate_limit(error)
                elif error == "cookie_expired":
                    self.quota.pause_for_cookie(error)
                raise AdminCrawlError("upstream_error", str(error), 502)
            articles = list((data or {}).get("list", []))
            if not articles:
                break
            for article in articles:
                item = self._candidate(article, source)
                if item:
                    candidates.setdefault(item["id"], item)
        items = list(candidates.values())
        self._add_local_state(items)
        preview_id = uuid.uuid4().hex
        self.repository.save_preview(preview_id, source, query, pages, items)
        return {
            "preview_id": preview_id,
            "source": source,
            "query": query,
            "calls": calls,
            "candidates": items,
            "quota": self.quota.status(),
        }

    def create_job(self, preview_id: str, selected_ids: list[str], strategy: str) -> dict:
        strategy = str(strategy or "smart")
        if strategy not in self.STRATEGIES:
            raise AdminCrawlError("invalid_strategy", "不支持的爬取方案")
        selected = list(dict.fromkeys(str(value).strip() for value in selected_ids))
        if not selected or len(selected) > 10 or any(not value.isdigit() for value in selected):
            raise AdminCrawlError("invalid_selection", "请选择1至10个有效帖子")
        preview = self.repository.get_preview(preview_id)
        if preview is None:
            raise AdminCrawlError("preview_expired", "候选预览已过期，请重新获取")
        candidates = {item["id"]: item for item in preview["candidates"]}
        if any(post_id not in candidates for post_id in selected):
            raise AdminCrawlError("invalid_selection", "选择中包含非本次预览帖子")
        estimated_details = 0
        if strategy == "force":
            estimated_details = len(selected)
        elif strategy == "smart":
            estimated_details = sum(
                1 for post_id in selected if candidates[post_id]["recommended"]
            )
        quota_status = self.quota.status()
        detail_remaining = int(quota_status.get("detail_remaining", 0) or 0)
        if estimated_details > detail_remaining:
            raise AdminCrawlError(
                "manual_budget_exhausted",
                f"本批预计需要 {estimated_details} 次详情 API，当前只剩 "
                f"{detail_remaining} 次人工额度",
                429,
            )
        job_id = uuid.uuid4().hex
        active = self.repository.create_job(
            job_id, preview_id, strategy, selected, candidates
        )
        if active:
            raise AdminCrawlError(
                "worker_busy",
                f"已有人工任务 {active[:8]} 正在执行",
                409,
            )
        self._start_worker_if_needed()
        return self.get_job(job_id)

    def _start_worker_if_needed(self) -> None:
        with self._worker_lock:
            if self._worker and self._worker.is_alive():
                return
            job_id = self.repository.next_queued_job_id()
            if not job_id:
                return
            self._worker = threading.Thread(
                target=self._run_job,
                args=(job_id,),
                daemon=True,
                name="admin-live-crawl",
            )
            self._worker.start()

    def _snapshot(self, post_id: str) -> tuple[str, int, int]:
        if not self.posts_db.exists():
            return "missing", 0, 0
        conn = sqlite3.connect(f"file:{self.posts_db.as_posix()}?mode=ro", uri=True)
        try:
            columns = {row[1] for row in conn.execute("pragma table_info(posts)")}
            status_sql = "crawl_status" if "crawl_status" in columns else "'full'"
            row = conn.execute(
                f"select comment_count, {status_sql} from posts where id=?",
                (post_id,),
            ).fetchone()
            comments = conn.execute(
                "select count(*) from comments where post_id=?", (post_id,)
            ).fetchone()[0]
            return (
                str(row[1] or "full") if row else "missing",
                safe_int(row[0]) if row else 0,
                safe_int(comments),
            )
        finally:
            conn.close()

    def _update_item(self, job_id: str, post_id: str, **values) -> None:
        self.repository.update_item(job_id, post_id, **values)

    def _queue_item(self, item: dict) -> str:
        status, db_count, _ = self._snapshot(str(item["post_id"]))
        with database_write_lock(self.posts_db, timeout=30):
            with SQLitePostStore(
                self.posts_db, self.bigram_db, self.symbol_db
            ) as store:
                store.ensure_runtime_schema()
                store.enqueue_crawler_candidate(
                    post_id=str(item["post_id"]),
                    source="admin",
                    priority=-10,
                    list_create_time="",
                    list_update_time="",
                    list_comment_count=safe_int(item["upstream_comment_count"]),
                    db_comment_count=None if status == "missing" else db_count,
                    reason="admin_selected",
                )
        return "已加入最高优先级队列"

    def _crawl_item(self, item: dict, strategy: str, client) -> str:
        post_id = str(item["post_id"])
        local_status, local_count, before_rows = self._snapshot(post_id)
        if (
            strategy == "smart"
            and local_status == "full"
            and safe_int(item["upstream_comment_count"]) <= local_count
        ):
            raise AdminCrawlError("smart_skip", "本地详情完整且评论数没有增加")
        try:
            self.quota.reserve("detail", "detail", 1)
        except ManualQuotaError as exc:
            raise AdminCrawlError(exc.code, str(exc), 429) from exc
        data, error = client.article(post_id)
        if error:
            if str(error).startswith("rate_limited:"):
                self.quota.pause_for_rate_limit(error)
                raise AdminCrawlError("rate_limited", str(error), 502)
            if error == "cookie_expired":
                self.quota.pause_for_cookie(error)
                raise AdminCrawlError("cookie_expired", str(error), 502)
            raise AdminCrawlError("upstream_error", str(error), 502)
        parsed = normalize_detail(post_id, data or {})
        if parsed is None:
            raise AdminCrawlError("invalid_payload", "上游返回的社区或帖子数据无效", 502)
        post, comments = parsed
        if not str(post.get("content") or "").strip():
            raise AdminCrawlError("suspicious_payload", "上游正文为空，已保留旧数据", 502)
        if safe_int(post.get("comment_count")) > 0 and not comments:
            raise AdminCrawlError("suspicious_payload", "上游评论异常为空，已保留旧数据", 502)
        self._update_item(item["job_id"], post_id, status="waiting_write")
        with database_write_lock(self.posts_db, timeout=180):
            with SQLitePostStore(
                self.posts_db, self.bigram_db, self.symbol_db
            ) as store:
                store.ensure_runtime_schema()
                store.upsert_post(post, comments)
        _, _, after_rows = self._snapshot(post_id)
        return f"已保存；评论行 {before_rows} → {after_rows}"

    def _run_job(self, job_id: str) -> None:
        job, items = self.repository.start_job(job_id)
        if job is None:
            return
        try:
            client = None if job["strategy"] == "queue" else self.client_factory()
        except Exception as exc:
            self.repository.fail_job_initialization(job_id, str(exc))
            return
        stop_error = ""
        for index, item in enumerate(items):
            post_id = str(item["post_id"])
            if index > 0 and job["strategy"] != "queue":
                time.sleep(random.uniform(self.min_delay, self.max_delay))
            self._update_item(job_id, post_id, status="running", error="")
            status = "succeeded"
            result = ""
            error = ""
            try:
                if job["strategy"] == "queue":
                    result = self._queue_item(item)
                else:
                    result = self._crawl_item(item, str(job["strategy"]), client)
            except AdminCrawlError as exc:
                if exc.code == "smart_skip":
                    status, result = "skipped", str(exc)
                else:
                    status, error = "failed", str(exc)
                    if exc.code in {
                        "paused",
                        "release_locked",
                        "manual_budget_exhausted",
                        "source_budget_exhausted",
                        "rate_limited",
                        "cookie_expired",
                    } or str(exc).startswith("rate_limited:"):
                        stop_error = str(exc)
            except Exception as exc:  # pragma: no cover - final worker fuse
                status, error = "failed", str(exc)
            self._update_item(
                job_id, post_id, status=status, result=result, error=error
            )
            if stop_error:
                break
        self.repository.finish_job(
            job_id,
            stop_error,
            queue_strategy=job["strategy"] == "queue",
        )

    def get_job(self, job_id: str) -> dict:
        payload = self.repository.get_job(job_id)
        if payload is None:
            raise AdminCrawlError("job_not_found", "任务不存在", 404)
        payload["quota"] = self.quota.status()
        return payload

    def wait_for_idle(self, timeout: float = 5.0) -> None:
        """Wait for the current worker to release its final DB handles."""
        worker = self._worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout=timeout)
