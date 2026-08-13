"""Per-request quota claims for scheduler-started crawler processes."""

from __future__ import annotations

import os

from crawler.manual_quota import exclusive_control_lock

AUTOMATIC_QUOTA_KIND_ENV = "CRAWLER_AUTOMATIC_QUOTA_KIND"


class AutomaticQuotaError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class AutomaticQuota:
    """Atomically consume automatic quota immediately before a source request."""

    VALID_KINDS = {"new_list", "active_list", "detail", "probe"}

    def __init__(self, kind: str, lane_id: str = ""):
        if kind not in self.VALID_KINDS:
            raise ValueError(f"unsupported automatic quota kind: {kind}")
        self.kind = kind
        self.lane_id = str(lane_id or "")

    @classmethod
    def from_environment(
        cls,
        *,
        lane_id: str = "",
    ) -> "AutomaticQuota | None":
        kind = str(os.environ.get(AUTOMATIC_QUOTA_KIND_ENV, "")).strip()
        return cls(kind, lane_id=lane_id) if kind else None

    @staticmethod
    def _scheduler():
        # Import lazily so the API client remains usable outside Railway's
        # scheduler process without creating an import cycle.
        from jobs import scheduler

        return scheduler

    def claim(self, count: int = 1) -> dict:
        if count <= 0:
            return {}
        scheduler = self._scheduler()
        lock_path = scheduler.QUOTA_PATH.with_name(scheduler.QUOTA_PATH.name + ".lock")
        with exclusive_control_lock(lock_path):
            pause = scheduler.active_pause()
            if pause:
                raise AutomaticQuotaError(
                    "source_quota_paused",
                    f"crawler paused until {pause.get('until_text') or 'later'}",
                )
            quota = scheduler.load_quota()
            remaining = scheduler.remaining_budget(
                self.kind,
                quota,
                lane_id=self.lane_id,
            )
            if remaining < count:
                if scheduler.quota_release_fraction_for_kind(self.kind) <= 0:
                    raise AutomaticQuotaError(
                        "source_quota_window_locked",
                        f"automatic quota locked until "
                        f"{scheduler.next_quota_release_for_kind(self.kind).isoformat()}",
                    )
                raise AutomaticQuotaError(
                    "source_quota_budget_exhausted",
                    f"{self.kind} automatic quota exhausted",
                )
            key = scheduler.quota_key(self.kind)
            quota[key] = int(quota.get(key, 0) or 0) + count
            if self.lane_id:
                lane = scheduler.ensure_cookie_lane_quota(quota, self.lane_id)
                lane[key] = int(lane.get(key, 0) or 0) + count
            scheduler.save_quota(quota)
            return {
                "kind": self.kind,
                "lane_id": self.lane_id,
                "used": quota[key],
                "lane_used": (
                    int(
                        scheduler.ensure_cookie_lane_quota(
                            quota,
                            self.lane_id,
                        ).get(key, 0)
                    )
                    if self.lane_id
                    else None
                ),
                "remaining": remaining - count,
            }
