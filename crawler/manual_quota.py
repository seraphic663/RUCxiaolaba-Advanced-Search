"""Atomic quota and pause handling for administrator-triggered source calls."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path


class ManualQuotaError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@contextmanager
def exclusive_control_lock(path: str | Path, timeout: float = 10.0):
    lock_path = Path(path)
    deadline = time.time() + timeout
    descriptor = None
    while True:
        try:
            descriptor = os.open(
                str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
            )
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 60:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.time() >= deadline:
                raise TimeoutError(f"crawler control lock timeout: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


class ManualQuota:
    def __init__(self, posts_db: str | Path):
        db_path = Path(posts_db)
        self.quota_path = Path(
            os.environ.get(
                "CRAWLER_QUOTA_FILE",
                str(db_path.with_name(".crawler_quota.json")),
            )
        )
        self.pause_path = Path(
            os.environ.get(
                "CRAWLER_PAUSE_FILE",
                str(db_path.with_name(".crawler_pause.json")),
            )
        )
        self.lock_path = self.quota_path.with_name(
            self.quota_path.name + ".lock"
        )
        self.preview_cap = max(
            0, int(os.environ.get("CRAWLER_DAILY_ADMIN_PREVIEW_BUDGET", "20"))
        )
        self.detail_cap = max(
            0, int(os.environ.get("CRAWLER_DAILY_ADMIN_DETAIL_BUDGET", "10"))
        )

    @staticmethod
    def _scheduler():
        from jobs import scheduler

        return scheduler

    def _active_pause(self) -> dict:
        try:
            pause = json.loads(self.pause_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return {}
        return pause if float(pause.get("until") or 0) > time.time() else {}

    def _load_quota(self, scheduler) -> dict:
        try:
            quota = json.loads(self.quota_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            quota = {}
        today = scheduler.quota_date()
        if quota.get("date") != today:
            quota = {
                "date": today,
                "new_list_calls": 0,
                "active_list_calls": 0,
                "detail_calls": 0,
                "probe_calls": 0,
                "rate_limited": 0,
            }
        return quota

    def _save_quota(self, scheduler, quota: dict) -> None:
        self.quota_path.parent.mkdir(parents=True, exist_ok=True)
        quota["updated_at"] = scheduler.beijing_now().isoformat()
        quota["release_fraction"] = scheduler.quota_release_fraction()
        quota["configured_source_budget"] = scheduler.configured_source_budget()
        quota["adaptive_source_budget"] = scheduler.adaptive_source_budget()
        quota["adaptive_scale"] = scheduler.adaptive_scale()
        temporary = self.quota_path.with_name(self.quota_path.name + ".tmp")
        temporary.write_text(
            json.dumps(quota, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.quota_path)

    def reserve(self, kind: str, manual_kind: str, count: int = 1) -> dict:
        if count <= 0:
            return {}
        scheduler = self._scheduler()
        key = scheduler.quota_key(kind)
        manual_key = f"admin_{manual_kind}_calls"
        cap = self.preview_cap if manual_kind == "preview" else self.detail_cap
        with exclusive_control_lock(self.lock_path):
            pause = self._active_pause()
            if pause:
                raise ManualQuotaError(
                    "paused",
                    f"爬虫已暂停到 {pause.get('until_text') or '稍后'}",
                )
            quota = self._load_quota(scheduler)
            fraction = scheduler.quota_release_fraction()
            if fraction <= 0:
                raise ManualQuotaError(
                    "release_locked",
                    f"额度尚未释放，下一窗口 {scheduler.next_quota_release().isoformat()}",
                )
            allowed = int(scheduler.daily_budget(kind) * fraction)
            manual_allowed = int(cap * fraction)
            used = int(quota.get(key, 0) or 0)
            manual_used = int(quota.get(manual_key, 0) or 0)
            if manual_used + count > manual_allowed:
                raise ManualQuotaError("manual_budget_exhausted", "后台人工额度已用完")
            if used + count > allowed:
                raise ManualQuotaError("source_budget_exhausted", "对应源 API 额度已用完")
            quota[key] = used + count
            quota[manual_key] = manual_used + count
            self._save_quota(scheduler, quota)
            return {
                "kind": kind,
                "used": quota[key],
                "allowed": allowed,
                "manual_used": quota[manual_key],
                "manual_allowed": manual_allowed,
            }

    def pause_for_rate_limit(self, detail: str) -> None:
        scheduler = self._scheduler()
        reset = scheduler.next_beijing_reset()
        pause = {
            "reason": "rate_limited",
            "job": "admin_live_crawl",
            "until": reset.timestamp(),
            "until_text": reset.isoformat(),
            "detail": str(detail)[-500:],
            "updated_at": scheduler.beijing_now().isoformat(),
        }
        with exclusive_control_lock(self.lock_path):
            self.pause_path.write_text(
                json.dumps(pause, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def pause_for_cookie(self, detail: str) -> None:
        scheduler = self._scheduler()
        until = scheduler.beijing_now() + timedelta(
            seconds=scheduler.COOKIE_ERROR_COOLDOWN
        )
        pause = {
            "reason": "cookie_expired",
            "job": "admin_live_crawl",
            "until": until.timestamp(),
            "until_text": until.isoformat(),
            "detail": str(detail)[-500:],
            "updated_at": scheduler.beijing_now().isoformat(),
        }
        with exclusive_control_lock(self.lock_path):
            self.pause_path.write_text(
                json.dumps(pause, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def status(self) -> dict:
        scheduler = self._scheduler()
        with exclusive_control_lock(self.lock_path):
            quota = self._load_quota(scheduler)
        fraction = scheduler.quota_release_fraction()
        preview_allowed = int(self.preview_cap * fraction)
        detail_allowed = int(self.detail_cap * fraction)
        preview_used = int(quota.get("admin_preview_calls", 0) or 0)
        detail_used = int(quota.get("admin_detail_calls", 0) or 0)
        return {
            "release_fraction": fraction,
            "paused": self._active_pause(),
            "preview_used": preview_used,
            "preview_allowed": preview_allowed,
            "preview_remaining": max(0, preview_allowed - preview_used),
            "detail_used": detail_used,
            "detail_allowed": detail_allowed,
            "detail_remaining": max(0, detail_allowed - detail_used),
        }
