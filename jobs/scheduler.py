#!/usr/bin/env python3
"""Run conservative DB crawler updates inside the Railway web service."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crawler.automatic_quota import AUTOMATIC_QUOTA_KIND_ENV
from crawler.cookie_pool import COOKIE_KINDS, CookieLaneSpec, load_cookie_pool_specs
from crawler.id_ledger import ledger_state, set_ledger_state
from crawler.lock import database_write_lock
from crawler.manual_quota import exclusive_control_lock
from storage.post_writer import SQLitePostStore

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = os.environ.get("SQLITE_DB", "/app/data/posts.db")
CONFIG_PATH = os.environ.get("CRAWLER_CONFIG", "/app/data/config.txt")
COOKIE_POOL_PATH = os.environ.get("CRAWLER_COOKIE_POOL", "")
PAUSE_PATH = Path(
    os.environ.get(
        "CRAWLER_PAUSE_FILE",
        str(Path(DB_PATH).with_name(".crawler_pause.json")),
    )
)
QUOTA_PATH = Path(
    os.environ.get(
        "CRAWLER_QUOTA_FILE",
        str(Path(DB_PATH).with_name(".crawler_quota.json")),
    )
)
QUOTA_HISTORY_PATH = Path(
    os.environ.get(
        "CRAWLER_QUOTA_HISTORY_FILE",
        str(Path(DB_PATH).with_name(".crawler_quota_history.jsonl")),
    )
)
HEARTBEAT_PATH = Path(
    os.environ.get(
        "CRAWLER_SCHEDULER_HEARTBEAT_FILE",
        str(Path(DB_PATH).with_name(".crawler_scheduler_heartbeat.json")),
    )
)


def env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def env_nonnegative_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


def env_iso_dates(name: str) -> frozenset[str]:
    dates: set[str] = set()
    for raw_value in os.environ.get(name, "").split(","):
        value = raw_value.strip()
        if not value:
            continue
        try:
            dates.add(datetime.strptime(value, "%Y-%m-%d").date().isoformat())
        except ValueError:
            print(
                f"[scheduler] ignore invalid {name} date={value!r}",
                flush=True,
            )
    return frozenset(dates)


NEW_INTERVAL = env_int("CRAWLER_NEW_INTERVAL", 8 * 60 * 60)
REFRESH_INTERVAL = env_int("CRAWLER_REFRESH_INTERVAL", 8 * 60 * 60)
BACKFILL_INTERVAL = env_int("CRAWLER_BACKFILL_INTERVAL", 24 * 60 * 60)
PHASE1_INTERVAL = env_int("CRAWLER_PHASE1_INTERVAL", 7 * 24 * 60 * 60)
PHASE1_MARKER = Path(DB_PATH).with_name(".phase1_weekly_last")
CHINA_TZ = timezone(timedelta(hours=8))
TRICKLE_ENABLED = os.environ.get("CRAWLER_TRICKLE_ENABLED", "0") == "1"
TRICKLE_SINCE = os.environ.get("CRAWLER_TRICKLE_SINCE", "2026-06-25 00:00:00")
DISCOVER_INTERVAL = env_int("CRAWLER_DISCOVER_INTERVAL", 30 * 60)
NEW_DISCOVER_INTERVAL = env_int(
    "CRAWLER_NEW_DISCOVER_INTERVAL",
    60 * 60,
)
ACTIVE_DISCOVER_INTERVAL = env_int(
    "CRAWLER_ACTIVE_DISCOVER_INTERVAL",
    DISCOVER_INTERVAL,
)
BOOTSTRAP_PAGES = env_int("CRAWLER_BOOTSTRAP_PAGES", 20)
BOOTSTRAP_SINCE = os.environ.get(
    "CRAWLER_BOOTSTRAP_SINCE",
    "1970-01-01 00:00:00",
)
BOOTSTRAP_RETRY_INTERVAL = env_int(
    "CRAWLER_BOOTSTRAP_RETRY_INTERVAL",
    60 * 60,
)
TRICKLE_INTERVAL = env_int("CRAWLER_TRICKLE_INTERVAL", 10 * 60)
TRICKLE_LIMIT_CAP = env_int("CRAWLER_TRICKLE_LIMIT_CAP", 12)
TRICKLE_LIMIT = min(env_int("CRAWLER_TRICKLE_LIMIT", 12), TRICKLE_LIMIT_CAP)
TRICKLE_REFRESH_LIMIT = min(
    env_nonnegative_int("CRAWLER_TRICKLE_REFRESH_LIMIT", 5),
    TRICKLE_LIMIT,
)
TRICKLE_OBSERVATION_RETRY_DELAY = env_int(
    "CRAWLER_TRICKLE_OBSERVATION_RETRY_DELAY",
    6 * 60 * 60,
)
TRICKLE_MAX_OBSERVATION_ATTEMPTS = env_int(
    "CRAWLER_TRICKLE_MAX_OBSERVATION_ATTEMPTS",
    2,
)
TRICKLE_TRANSIENT_RETRY_DELAY = env_int(
    "CRAWLER_TRICKLE_TRANSIENT_RETRY_DELAY",
    60 * 60,
)
TRICKLE_MAX_TRANSIENT_ATTEMPTS = env_int(
    "CRAWLER_TRICKLE_MAX_TRANSIENT_ATTEMPTS",
    3,
)
TRICKLE_FRESH_COVERAGE_HOURS = env_int(
    "CRAWLER_TRICKLE_FRESH_COVERAGE_HOURS",
    72,
)
TRICKLE_MIN_DELAY = env_float("CRAWLER_TRICKLE_MIN_DELAY", 8.0)
TRICKLE_MAX_DELAY = max(
    TRICKLE_MIN_DELAY,
    env_float("CRAWLER_TRICKLE_MAX_DELAY", 14.0),
)
DISCOVER_LATEST_PAGES = env_int("CRAWLER_DISCOVER_LATEST_PAGES", 5)
DISCOVER_ACTIVE_PAGES = env_int("CRAWLER_DISCOVER_ACTIVE_PAGES", 5)
GAP_ENABLED = os.environ.get("CRAWLER_GAP_ENABLED", "1" if TRICKLE_ENABLED else "0") == "1"
GAP_SINCE = os.environ.get("CRAWLER_GAP_SINCE", TRICKLE_SINCE)
GAP_PLAN_INTERVAL = env_int("CRAWLER_GAP_PLAN_INTERVAL", 6 * 60 * 60)
GAP_PROBE_INTERVAL = env_int("CRAWLER_GAP_PROBE_INTERVAL", 2 * 60 * 60)
GAP_RANGE_LIMIT = env_int("CRAWLER_GAP_RANGE_LIMIT", 12)
GAP_SAMPLES = env_int("CRAWLER_GAP_SAMPLES", 1)
GAP_CHUNK_SIZE = env_int("CRAWLER_GAP_CHUNK_SIZE", 1000)
GAP_DENSITY_THRESHOLD = env_float("CRAWLER_GAP_DENSITY_THRESHOLD", 0.35)
COOKIE_ERROR_COOLDOWN = env_int("CRAWLER_COOKIE_ERROR_COOLDOWN", 6 * 60 * 60)
DAILY_LIST_BUDGET = env_int("CRAWLER_DAILY_LIST_BUDGET", 240)
DAILY_NEW_LIST_BUDGET = env_int(
    "CRAWLER_DAILY_NEW_LIST_BUDGET",
    max(1, DAILY_LIST_BUDGET // 3),
)
DAILY_ACTIVE_LIST_BUDGET = env_int(
    "CRAWLER_DAILY_ACTIVE_LIST_BUDGET",
    max(1, DAILY_LIST_BUDGET - DAILY_NEW_LIST_BUDGET),
)
DAILY_DETAIL_BUDGET = env_int("CRAWLER_DAILY_DETAIL_BUDGET", 1000)
DAILY_PROBE_BUDGET = env_nonnegative_int("CRAWLER_DAILY_PROBE_BUDGET", 0)
DAILY_ADMIN_PREVIEW_BUDGET = env_nonnegative_int("CRAWLER_DAILY_ADMIN_PREVIEW_BUDGET", 20)
DAILY_ADMIN_DETAIL_BUDGET = env_nonnegative_int("CRAWLER_DAILY_ADMIN_DETAIL_BUDGET", 10)
QUOTA_FIRST_RELEASE_HOUR = env_nonnegative_int("CRAWLER_QUOTA_FIRST_RELEASE_HOUR", 11)
QUOTA_SECOND_RELEASE_HOUR = env_nonnegative_int("CRAWLER_QUOTA_SECOND_RELEASE_HOUR", 22)
QUOTA_RELEASE_STEPS_TEXT = os.environ.get(
    "CRAWLER_QUOTA_RELEASE_STEPS",
    "11=0.20,14=0.35,17=0.50,20=0.65,21=0.75,22=0.88,23=0.97,23:30=1.00",
)
DETAIL_QUOTA_RELEASE_STEPS_TEXT = os.environ.get(
    "CRAWLER_DETAIL_QUOTA_RELEASE_STEPS",
    "0=0.05,6=0.10,10=0.20,12=0.38,15=0.58,18=0.74,20=0.84,21=0.91,22=0.97,23=0.99,23:30=1.00",
)
QUOTA_ADAPTIVE_ENABLED = os.environ.get("CRAWLER_QUOTA_ADAPTIVE_ENABLED", "1") == "1"
QUOTA_ADAPTIVE_LOOKBACK_DAYS = env_int("CRAWLER_QUOTA_ADAPTIVE_LOOKBACK_DAYS", 14)
QUOTA_RATE_LIMIT_EXCLUDED_DATES = env_iso_dates(
    "CRAWLER_QUOTA_RATE_LIMIT_EXCLUDED_DATES"
)
DETAIL_ADAPTIVE_ENABLED = (
    os.environ.get("CRAWLER_DETAIL_ADAPTIVE_ENABLED", "1") == "1"
)
DETAIL_ADAPTIVE_MIN = min(
    DAILY_DETAIL_BUDGET,
    env_int("CRAWLER_DETAIL_ADAPTIVE_MIN", 900),
)
DETAIL_ADAPTIVE_START = min(
    DAILY_DETAIL_BUDGET,
    max(
        DETAIL_ADAPTIVE_MIN,
        env_int("CRAWLER_DETAIL_ADAPTIVE_START", 900),
    ),
)
DETAIL_ADAPTIVE_STEP = env_int("CRAWLER_DETAIL_ADAPTIVE_STEP", 100)
DETAIL_ADAPTIVE_UTILIZATION = min(
    1.0,
    max(
        0.50,
        env_float("CRAWLER_DETAIL_ADAPTIVE_UTILIZATION", 0.95),
    ),
)
DETAIL_ADAPTIVE_SCHEDULE_UTILIZATION = min(
    1.0,
    max(
        0.50,
        env_float("CRAWLER_DETAIL_ADAPTIVE_SCHEDULE_UTILIZATION", 0.98),
    ),
)
RESET_GRACE_MINUTES = env_int("CRAWLER_RESET_GRACE_MINUTES", 5)
RATE_LIMIT_RETRY_COOLDOWN = env_int(
    "CRAWLER_RATE_LIMIT_RETRY_COOLDOWN",
    60 * 60,
)
RATE_LIMIT_HARD_THRESHOLD = max(
    2,
    env_int("CRAWLER_RATE_LIMIT_HARD_THRESHOLD", 2),
)
PAUSE_LOG_INTERVAL = env_int("CRAWLER_PAUSE_LOG_INTERVAL", 10 * 60)
HEARTBEAT_INTERVAL = env_int("CRAWLER_SCHEDULER_HEARTBEAT_INTERVAL", 30)


def cookie_pool_specs() -> tuple[CookieLaneSpec, ...]:
    """Return lane metadata; values never contain the cookie itself."""
    if not str(COOKIE_POOL_PATH or "").strip():
        return ()
    return load_cookie_pool_specs(COOKIE_POOL_PATH)


def cookie_pool_budget(kind: str, lane_id: str = "") -> int | None:
    specs = cookie_pool_specs()
    if not specs:
        return None
    if lane_id:
        for spec in specs:
            if spec.lane_id == lane_id:
                return spec.budget(kind)
        return 0
    return sum(spec.budget(kind) for spec in specs)


def ensure_cookie_lane_quota(quota: dict, lane_id: str) -> dict:
    """Create only numeric per-lane counters in the shared quota ledger."""
    lanes = quota.setdefault("cookie_lanes", {})
    lane = lanes.setdefault(
        str(lane_id),
        {f"{kind}_calls": 0 for kind in COOKIE_KINDS},
    )
    for kind in COOKIE_KINDS:
        lane.setdefault(f"{kind}_calls", 0)
    return lane


def sync_cookie_lane_quotas(quota: dict) -> None:
    for spec in cookie_pool_specs():
        ensure_cookie_lane_quota(quota, spec.lane_id)


JOBS = {
    "new": [
        "sync-latest",
        "--pages",
        "100",
        "--min-pages",
        "20",
        "--stop-unchanged",
        "220",
        "--max-details",
        "0",
    ],
    "refresh": [
        "sync-active",
        "--pages",
        "100",
        "--min-pages",
        "20",
        "--stop-unchanged",
        "220",
        "--max-details",
        "0",
    ],
    "backfill": [
        "scan-history",
        "--endpoint",
        "lists2",
        "--start-page",
        "2",
        "--pages",
        "99",
        "--min-pages",
        "99",
        "--stop-unchanged",
        "100000",
        "--max-details",
        "0",
    ],
}

TRICKLE_JOBS = {
    "bootstrap_new": [
        "discover-latest",
        "--bootstrap",
        "--since",
        BOOTSTRAP_SINCE,
        "--max-pages",
        str(BOOTSTRAP_PAGES),
        "--min-pages",
        str(BOOTSTRAP_PAGES),
        "--no-action-page-threshold",
        "0",
        "--no-write-stubs",
        "--min-delay",
        "0.1",
        "--max-delay",
        "0.3",
    ],
    "discover_new": [
        "discover-latest",
        "--since",
        TRICKLE_SINCE,
        "--max-pages",
        str(DISCOVER_LATEST_PAGES),
        "--min-pages",
        "2",
        "--no-action-page-threshold",
        "2",
        "--min-delay",
        "0.1",
        "--max-delay",
        "0.3",
    ],
    "discover_active": [
        "discover-active",
        "--since",
        TRICKLE_SINCE,
        "--max-pages",
        str(DISCOVER_ACTIVE_PAGES),
        "--min-pages",
        "2",
        "--no-action-page-threshold",
        "2",
        "--min-delay",
        "0.1",
        "--max-delay",
        "0.3",
    ],
    "trickle_fill": [
        "trickle-fill",
        "--limit",
        str(TRICKLE_LIMIT),
        "--refresh-limit",
        str(TRICKLE_REFRESH_LIMIT),
        "--observation-retry-delay",
        str(TRICKLE_OBSERVATION_RETRY_DELAY),
        "--max-observation-attempts",
        str(TRICKLE_MAX_OBSERVATION_ATTEMPTS),
        "--transient-retry-delay",
        str(TRICKLE_TRANSIENT_RETRY_DELAY),
        "--max-transient-attempts",
        str(TRICKLE_MAX_TRANSIENT_ATTEMPTS),
        "--fresh-coverage-hours",
        str(TRICKLE_FRESH_COVERAGE_HOURS),
        "--min-delay",
        str(TRICKLE_MIN_DELAY),
        "--max-delay",
        str(TRICKLE_MAX_DELAY),
    ],
}

if GAP_ENABLED:
    TRICKLE_JOBS.update(
        {
            "plan_gaps": [
                "plan-gaps",
                "--since",
                GAP_SINCE,
                "--chunk-size",
                str(GAP_CHUNK_SIZE),
                "--density-threshold",
                str(GAP_DENSITY_THRESHOLD),
            ],
            "probe_gaps": [
                "probe-gaps",
                "--range-limit",
                str(GAP_RANGE_LIMIT),
                "--samples-per-range",
                str(GAP_SAMPLES),
                "--min-delay",
                "8",
                "--max-delay",
                "15",
            ],
        }
    )


@dataclass(frozen=True)
class JobResult:
    succeeded: bool
    error_kind: str = ""
    stderr: str = ""
    returncode: int = 0
    deferred_until: float = 0.0
    source_calls: int = 0


OVERDUE_JOB_PRIORITY = {
    "bootstrap_new": 0,
    "trickle_fill": 0,
    "discover_active": 1,
    "discover_new": 2,
    "plan_gaps": 3,
    "probe_gaps": 4,
}

MONITOR_LIST1_SEED_KEY = "monitor_list1_seed_complete"
PIPELINE_PHASE_KEY = "crawler_pipeline_phase"
PIPELINE_PHASE_BOOTSTRAP = "bootstrap"
PIPELINE_PHASE_LIST1_SEED = "list1_seed"
PIPELINE_PHASE_DETAIL_BACKFILL = "detail_backfill"
PIPELINE_PHASE_MONITORING = "monitoring"
PIPELINE_PHASES = {
    PIPELINE_PHASE_BOOTSTRAP,
    PIPELINE_PHASE_LIST1_SEED,
    PIPELINE_PHASE_DETAIL_BACKFILL,
    PIPELINE_PHASE_MONITORING,
}
UNMETERED_LIST_KINDS = {"new_list", "active_list"}


def now_wall() -> float:
    return time.time()


def beijing_now() -> datetime:
    return datetime.now(CHINA_TZ)


def quota_date() -> str:
    return beijing_now().date().isoformat()


def next_beijing_reset() -> datetime:
    tomorrow = beijing_now().date() + timedelta(days=1)
    return datetime.combine(
        tomorrow,
        datetime.min.time(),
        tzinfo=CHINA_TZ,
    ) + timedelta(minutes=RESET_GRACE_MINUTES)


def parse_release_steps(text: str) -> list[tuple[int, float]]:
    steps: list[tuple[int, float]] = []
    for chunk in text.split(","):
        item = chunk.strip()
        if not item:
            continue
        if "=" in item:
            time_part, value_part = item.split("=", 1)
        elif ":" in item:
            time_part, value_part = item.split(":", 1)
        else:
            continue
        try:
            if ":" in time_part:
                hour_text, minute_text = time_part.split(":", 1)
                minutes = int(hour_text) * 60 + int(minute_text)
            else:
                minutes = int(time_part) * 60
            fraction = max(0.0, min(1.0, float(value_part)))
        except ValueError:
            continue
        if 0 <= minutes < 24 * 60:
            steps.append((minutes, fraction))
    steps.sort(key=lambda pair: pair[0])
    deduped: list[tuple[int, float]] = []
    for minutes, fraction in steps:
        if deduped and deduped[-1][0] == minutes:
            deduped[-1] = (minutes, fraction)
        else:
            deduped.append((minutes, fraction))
    return deduped


def quota_release_steps() -> list[tuple[int, float]]:
    steps = parse_release_steps(QUOTA_RELEASE_STEPS_TEXT)
    if steps:
        return steps
    first_hour = min(23, QUOTA_FIRST_RELEASE_HOUR)
    second_hour = min(23, max(first_hour + 1, QUOTA_SECOND_RELEASE_HOUR))
    return [(first_hour * 60, 0.5), (second_hour * 60, 1.0)]


def detail_quota_release_steps() -> list[tuple[int, float]]:
    steps = parse_release_steps(DETAIL_QUOTA_RELEASE_STEPS_TEXT)
    return steps or quota_release_steps()


def quota_release_fraction(at: datetime | None = None) -> float:
    at = at.astimezone(CHINA_TZ) if at else beijing_now()
    current_minute = at.hour * 60 + at.minute
    released = 0.0
    for minute, fraction in quota_release_steps():
        if current_minute >= minute:
            released = fraction
        else:
            break
    return released


def release_fraction_for_steps(
    steps: list[tuple[int, float]],
    at: datetime | None = None,
) -> float:
    at = at.astimezone(CHINA_TZ) if at else beijing_now()
    current_minute = at.hour * 60 + at.minute
    released = 0.0
    for minute, fraction in steps:
        if current_minute >= minute:
            released = fraction
        else:
            break
    return released


def detail_quota_release_fraction(at: datetime | None = None) -> float:
    return release_fraction_for_steps(detail_quota_release_steps(), at)


def next_detail_quota_release(at: datetime | None = None) -> datetime:
    """Return the next release point for the detail-only budget lane."""
    at = at.astimezone(CHINA_TZ) if at else beijing_now()
    current_minute = at.hour * 60 + at.minute
    current_fraction = detail_quota_release_fraction(at)
    steps = detail_quota_release_steps()
    for minute, fraction in steps:
        if minute > current_minute and fraction > current_fraction:
            return at.replace(
                hour=minute // 60,
                minute=minute % 60,
                second=0,
                microsecond=0,
            )
    tomorrow = at.date() + timedelta(days=1)
    first_minute = steps[0][0]
    return datetime.combine(
        tomorrow,
        datetime.min.time(),
        tzinfo=CHINA_TZ,
    ).replace(
        hour=first_minute // 60,
        minute=first_minute % 60,
    )


def quota_release_fraction_for_kind(
    kind: str,
    at: datetime | None = None,
) -> float:
    return (
        detail_quota_release_fraction(at)
        if kind == "detail"
        else quota_release_fraction(at)
    )


def next_quota_release_for_kind(
    kind: str,
    at: datetime | None = None,
) -> datetime:
    return (
        next_detail_quota_release(at)
        if kind == "detail"
        else next_quota_release(at)
    )


def quota_record_release_steps(quota: dict) -> list[tuple[int, float]]:
    records = quota.get("detail_release_steps") or quota.get("release_steps")
    if not isinstance(records, list):
        return []
    text = ",".join(
        f"{item.get('time')}={item.get('fraction')}"
        for item in records
        if isinstance(item, dict)
    )
    return parse_release_steps(text)


def estimated_released_capacity(
    budget: int,
    steps: list[tuple[int, float]],
) -> int:
    """Upper bound for one detail worker under a stepped release profile."""
    if budget <= 0 or not steps:
        return 0
    used = 0
    interval_minutes = max(1, int(TRICKLE_INTERVAL) // 60)
    per_run = max(1, int(TRICKLE_LIMIT))
    for index, (minute, fraction) in enumerate(steps):
        next_minute = steps[index + 1][0] if index + 1 < len(steps) else 24 * 60
        runs = max(0, (next_minute - minute) // interval_minutes)
        allowed = min(int(budget), int(int(budget) * float(fraction)))
        used = min(allowed, used + runs * per_run)
    return min(int(budget), used)


def next_quota_release(at: datetime | None = None) -> datetime:
    at = at.astimezone(CHINA_TZ) if at else beijing_now()
    current_minute = at.hour * 60 + at.minute
    current_fraction = quota_release_fraction(at)
    for minute, fraction in quota_release_steps():
        if minute > current_minute and fraction > current_fraction:
            return at.replace(
                hour=minute // 60,
                minute=minute % 60,
                second=0,
                microsecond=0,
            )
    tomorrow = at.date() + timedelta(days=1)
    first_minute = quota_release_steps()[0][0]
    return datetime.combine(
        tomorrow,
        datetime.min.time(),
        tzinfo=CHINA_TZ,
    ).replace(
        hour=first_minute // 60,
        minute=first_minute % 60,
    )


def quota_source_calls(quota: dict) -> int:
    return sum(
        int(quota.get(key, 0) or 0)
        for key in (
            "new_list_calls",
            "active_list_calls",
            "detail_calls",
            "probe_calls",
            "admin_preview_calls",
            "admin_detail_calls",
        )
    )


def configured_source_budget() -> int:
    pool_total = sum(
        value or 0
        for value in (
            cookie_pool_budget(kind)
            for kind in COOKIE_KINDS
        )
    )
    if cookie_pool_specs():
        return pool_total
    return (
        DAILY_NEW_LIST_BUDGET + DAILY_ACTIVE_LIST_BUDGET + DAILY_DETAIL_BUDGET + DAILY_PROBE_BUDGET
    )


def configured_admin_budget() -> int:
    return DAILY_ADMIN_PREVIEW_BUDGET + DAILY_ADMIN_DETAIL_BUDGET


def clamp_detail_budget(value: int) -> int:
    return min(
        DAILY_DETAIL_BUDGET,
        max(DETAIL_ADAPTIVE_MIN, int(value)),
    )


def detail_budget_target(quota: dict | None = None) -> int:
    if not DETAIL_ADAPTIVE_ENABLED:
        return DAILY_DETAIL_BUDGET
    stored = (quota or {}).get("detail_budget_target")
    if stored is None:
        return DETAIL_ADAPTIVE_START
    try:
        return clamp_detail_budget(int(stored))
    except (TypeError, ValueError):
        return DETAIL_ADAPTIVE_START


def next_detail_budget_target(previous: dict) -> tuple[int, str]:
    """Use yesterday's actual utilization to choose a stable daily target."""
    if not DETAIL_ADAPTIVE_ENABLED:
        return DAILY_DETAIL_BUDGET, "fixed"
    target = detail_budget_target(previous)
    effective = max(
        1,
        int(previous.get("effective_detail_budget", target) or target),
    )
    used = max(0, int(previous.get("detail_calls", 0) or 0))
    if int(previous.get("rate_limited", 0) or 0) > 0:
        # A shared-session wall varies with the user's own browsing. Keep the
        # target stable; the observation is used only to pace the next day.
        return target, "rate_limited_pacing_hold"
    utilization = used / effective
    if utilization >= DETAIL_ADAPTIVE_UTILIZATION:
        increased = clamp_detail_budget(target + DETAIL_ADAPTIVE_STEP)
        return (
            increased,
            "fully_used_increase" if increased > target else "at_ceiling",
        )
    previous_steps = quota_record_release_steps(previous)
    reachable = estimated_released_capacity(effective, previous_steps)
    if (
        0 < reachable < effective
        and used / reachable >= DETAIL_ADAPTIVE_SCHEDULE_UTILIZATION
    ):
        increased = clamp_detail_budget(target + DETAIL_ADAPTIVE_STEP)
        return (
            increased,
            "schedule_limited_increase" if increased > target else "at_ceiling",
        )
    return target, "underused_hold"


def append_quota_history(quota: dict, *, reason: str, job: str = "") -> None:
    if not quota or not quota.get("date"):
        return
    record = {
        "date": quota.get("date"),
        "reason": reason,
        "job": job,
        "recorded_at": beijing_now().isoformat(),
        "source_calls": quota_source_calls(quota),
        "new_list_calls": int(quota.get("new_list_calls", 0) or 0),
        "active_list_calls": int(quota.get("active_list_calls", 0) or 0),
        "detail_calls": int(quota.get("detail_calls", 0) or 0),
        "probe_calls": int(quota.get("probe_calls", 0) or 0),
        "admin_preview_calls": int(quota.get("admin_preview_calls", 0) or 0),
        "admin_detail_calls": int(quota.get("admin_detail_calls", 0) or 0),
        "rate_limited": int(quota.get("rate_limited", 0) or 0),
        "rate_limit_excluded_dates": sorted(QUOTA_RATE_LIMIT_EXCLUDED_DATES),
        "rate_limit_pacing_anchor": int(
            quota.get("rate_limit_pacing_anchor", 0) or 0
        ),
        "detail_budget_target": detail_budget_target(quota),
        "effective_detail_budget": int(
            quota.get("effective_detail_budget", 0) or 0
        ),
        "detail_budget_decision": str(
            quota.get("detail_budget_decision", "")
        ),
        "configured_source_budget": int(
            quota.get("configured_source_budget", 0)
            or configured_source_budget()
        ),
        "configured_admin_budget": int(
            quota.get("configured_admin_budget", 0)
            or configured_admin_budget()
        ),
        "configured_total_budget": int(
            quota.get("configured_total_budget", 0)
            or configured_source_budget() + configured_admin_budget()
        ),
        "release_fraction": float(
            quota.get("release_fraction", quota_release_fraction()) or 0
        ),
        "detail_release_fraction": float(
            quota.get(
                "detail_release_fraction",
                detail_quota_release_fraction(),
            )
            or 0
        ),
        "cookie_lanes": {
            str(lane_id): {
                f"{kind}_calls": int(
                    lane.get(f"{kind}_calls", 0) or 0
                )
                for kind in COOKIE_KINDS
            }
            for lane_id, lane in (quota.get("cookie_lanes") or {}).items()
            if isinstance(lane, dict)
        },
    }
    with QUOTA_HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def rate_limit_pacing_anchor() -> int:
    """Return the latest observed shared wall, advanced only by safe usage.

    A wall observation is that day's crawler plus user usage, not a permanent
    source capacity. Safe day rollovers may raise the anchor, but only another
    rate-limit observation may lower it.
    """
    if not QUOTA_ADAPTIVE_ENABLED:
        return 0
    try:
        lines = QUOTA_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0
    except Exception as exc:
        print(f"[scheduler] ignore invalid quota history: {exc}", flush=True)
        return 0
    cutoff = beijing_now().date() - timedelta(days=QUOTA_ADAPTIVE_LOOKBACK_DAYS)
    anchor = 0
    observed = False
    for line in lines[-200:]:
        try:
            record = json.loads(line)
            date_text = str(record.get("date", ""))
            if date_text and datetime.fromisoformat(date_text).date() < cutoff:
                continue
            source_calls = int(record.get("source_calls", 0) or 0)
        except Exception:
            continue
        if source_calls <= 0:
            continue
        if record.get("reason") in {"rate_limited_soft", "rate_limited"}:
            anchor = source_calls
            observed = True
        elif (
            observed
            and record.get("reason") == "day_rollover"
            and int(record.get("rate_limited", 0) or 0) == 0
        ):
            anchor = max(anchor, source_calls)
    return anchor if observed else 0


def adaptive_source_budget() -> int:
    """Keep the configured ceiling; shared-session walls affect pacing only."""
    return configured_source_budget()


def adaptive_scale() -> float:
    return 1.0


def daily_budget(
    kind: str,
    quota: dict | None = None,
    *,
    lane_id: str = "",
) -> int:
    pool_budget = cookie_pool_budget(kind, lane_id=lane_id)
    if pool_budget is not None:
        # Pool budgets are explicit per-session ceilings.  Do not apply the
        # single-session adaptive detail target to them a second time.
        base = pool_budget
    else:
        base = {
            "new_list": DAILY_NEW_LIST_BUDGET,
            "active_list": DAILY_ACTIVE_LIST_BUDGET,
            "detail": detail_budget_target(quota),
            "probe": DAILY_PROBE_BUDGET,
        }[kind]
    if base <= 0:
        return 0
    return max(1, int(base * adaptive_scale()))


def current_source_budget(quota: dict | None = None) -> int:
    return sum(
        daily_budget(kind, quota)
        for kind in ("new_list", "active_list", "detail", "probe")
    )


def source_pacing_allowance(quota: dict | None = None) -> int:
    """Pace near the observed wall, then restore the full ceiling at 23:30."""
    quota = quota or {}
    total_budget = current_source_budget(quota)
    anchor = int(
        quota.get("rate_limit_pacing_anchor", 0)
        or rate_limit_pacing_anchor()
        or 0
    )
    fraction = detail_quota_release_fraction()
    if anchor <= 0 or fraction >= 1.0:
        return total_budget
    return min(total_budget, max(1, int(anchor * fraction)))


def classify_error(stderr: str) -> str:
    text = stderr.lower()
    if "rate_limited:" in text:
        return "rate_limited"
    if "cookie_expired" in text:
        return "cookie_expired"
    return ""


def load_pause() -> dict:
    try:
        return json.loads(PAUSE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"[scheduler] ignore invalid pause file: {exc}", flush=True)
        return {}


def save_pause(*, reason: str, job: str, seconds: int, detail: str) -> dict:
    until_dt = datetime.fromtimestamp(now_wall() + max(1, int(seconds)), CHINA_TZ)
    return save_pause_until(reason=reason, job=job, until_dt=until_dt, detail=detail)


def save_pause_until(
    *,
    reason: str,
    job: str,
    until_dt: datetime,
    detail: str,
) -> dict:
    until = until_dt.timestamp()
    pause = {
        "reason": reason,
        "job": job,
        "until": until,
        "until_text": until_dt.astimezone(CHINA_TZ).isoformat(),
        "detail": detail[-500:],
        "updated_at": beijing_now().isoformat(),
    }
    PAUSE_PATH.write_text(
        json.dumps(pause, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"[scheduler] pause crawler reason={reason} job={job} until={pause['until_text']}",
        flush=True,
    )
    return pause


def clear_pause(reason: str) -> None:
    try:
        PAUSE_PATH.unlink()
    except FileNotFoundError:
        pass
    print(f"[scheduler] clear pause reason={reason}", flush=True)


def normalize_pause(pause: dict) -> dict:
    if pause.get("reason") != "rate_limited":
        return pause
    if str(pause.get("updated_at", ""))[:10] != quota_date():
        return pause
    reset_dt = next_beijing_reset()
    reset_ts = reset_dt.timestamp()
    until = float(pause.get("until") or 0)
    if until >= reset_ts:
        return pause
    pause["until"] = reset_ts
    pause["until_text"] = reset_dt.isoformat()
    pause["detail"] = str(pause.get("detail", ""))[-500:]
    pause["updated_at"] = beijing_now().isoformat()
    PAUSE_PATH.write_text(
        json.dumps(pause, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"[scheduler] extend rate-limit pause until={pause['until_text']}",
        flush=True,
    )
    return pause


def active_pause() -> dict:
    pause = load_pause()
    if pause:
        pause = normalize_pause(pause)
    until = float(pause.get("until") or 0)
    if until > now_wall():
        return pause
    if pause:
        clear_pause("expired")
    return {}


def handle_rate_limit(*, job: str, detail: str) -> dict:
    """Record a bounded soft-limit recovery before declaring a daily hard wall."""
    lock_path = QUOTA_PATH.with_name(QUOTA_PATH.name + ".lock")
    with exclusive_control_lock(lock_path):
        quota = load_quota()
        count = int(quota.get("rate_limited", 0) or 0) + 1
        quota["rate_limited"] = count
        quota["last_rate_limited_at"] = beijing_now().isoformat()
        quota["last_rate_limited_job"] = job
        quota["last_rate_limited_source_calls"] = quota_source_calls(quota)
        hard = count >= RATE_LIMIT_HARD_THRESHOLD
        quota["rate_limit_state"] = "hard" if hard else "cooldown"
        quota["rate_limit_pacing_anchor"] = quota_source_calls(quota)
        save_quota(quota)
        append_quota_history(
            quota,
            reason="rate_limited" if hard else "rate_limited_soft",
            job=job,
        )

    if hard:
        return save_pause_until(
            reason="rate_limited",
            job=job,
            until_dt=next_beijing_reset(),
            detail=detail,
        )

    cooldown_until = beijing_now() + timedelta(seconds=RATE_LIMIT_RETRY_COOLDOWN)
    return save_pause_until(
        reason="rate_limited_cooldown",
        job=job,
        until_dt=min(cooldown_until, next_beijing_reset()),
        detail=detail,
    )


def load_quota() -> dict:
    today = quota_date()
    try:
        quota = json.loads(QUOTA_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        quota = {}
    except Exception as exc:
        print(f"[scheduler] ignore invalid quota file: {exc}", flush=True)
        quota = {}
    if quota.get("date") != today:
        previous = quota
        if quota.get("date"):
            append_quota_history(quota, reason="day_rollover")
            detail_target, detail_decision = next_detail_budget_target(
                previous
            )
        else:
            detail_target = DETAIL_ADAPTIVE_START
            detail_decision = "initial"
        quota = {
            "date": today,
            "new_list_calls": 0,
            "active_list_calls": 0,
            "detail_calls": 0,
            "probe_calls": 0,
            "rate_limited": 0,
            "admin_preview_calls": 0,
            "admin_detail_calls": 0,
            "cookie_lanes": {},
            "detail_budget_target": detail_target,
            "detail_budget_decision": detail_decision,
            "updated_at": beijing_now().isoformat(),
        }
        save_quota(quota)
    quota.setdefault("new_list_calls", 0)
    quota.setdefault("active_list_calls", 0)
    quota.setdefault("admin_preview_calls", 0)
    quota.setdefault("admin_detail_calls", 0)
    if "detail_budget_target" not in quota:
        quota["detail_budget_target"] = DETAIL_ADAPTIVE_START
        quota["detail_budget_decision"] = "initial"
    else:
        quota["detail_budget_target"] = detail_budget_target(quota)
        quota.setdefault("detail_budget_decision", "carried")
    before_lanes = json.dumps(quota.get("cookie_lanes", {}), sort_keys=True)
    sync_cookie_lane_quotas(quota)
    if json.dumps(quota.get("cookie_lanes", {}), sort_keys=True) != before_lanes:
        save_quota(quota)
    if "list_calls" in quota:
        # Older quota files only had a combined list counter. Keep the value
        # visible but do not split it retroactively; the new per-source counters
        # are authoritative from this deployment onward.
        quota.setdefault("legacy_list_calls", quota.get("list_calls", 0))
        quota.pop("list_calls", None)
    return quota


def save_quota(quota: dict) -> None:
    recorded_detail_steps = quota.get("detail_release_steps")
    if not recorded_detail_steps and int(quota.get("detail_calls", 0) or 0) > 0:
        recorded_detail_steps = quota.get("release_steps")
    quota["detail_budget_target"] = detail_budget_target(quota)
    quota["updated_at"] = beijing_now().isoformat()
    quota["release_fraction"] = quota_release_fraction()
    quota["detail_release_fraction"] = detail_quota_release_fraction()
    quota["configured_source_budget"] = configured_source_budget()
    quota["configured_admin_budget"] = configured_admin_budget()
    quota["configured_total_budget"] = configured_source_budget() + configured_admin_budget()
    quota["adaptive_source_budget"] = adaptive_source_budget()
    quota["adaptive_scale"] = adaptive_scale()
    quota["rate_limit_excluded_dates"] = sorted(
        QUOTA_RATE_LIMIT_EXCLUDED_DATES
    )
    quota["effective_detail_budget"] = daily_budget("detail", quota)
    quota["effective_source_budget"] = sum(
        daily_budget(kind, quota)
        for kind in ("new_list", "active_list", "detail", "probe")
    )
    quota.setdefault("rate_limit_pacing_anchor", rate_limit_pacing_anchor())
    quota["source_pacing_allowance"] = source_pacing_allowance(quota)
    quota["release_steps"] = [
        {
            "time": f"{minute // 60:02d}:{minute % 60:02d}",
            "fraction": fraction,
        }
        for minute, fraction in quota_release_steps()
    ]
    quota["detail_release_steps"] = recorded_detail_steps or [
        {
            "time": f"{minute // 60:02d}:{minute % 60:02d}",
            "fraction": fraction,
        }
        for minute, fraction in detail_quota_release_steps()
    ]
    QUOTA_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = QUOTA_PATH.with_name(f"{QUOTA_PATH.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(quota, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(QUOTA_PATH)


def save_heartbeat(*, state: str, job: str = "", detail: str = "") -> None:
    """Publish scheduler liveness without letting telemetry stop the crawler."""
    payload = {
        "date": quota_date(),
        "updated_at": beijing_now().isoformat(),
        "state": state,
        "job": job,
        "detail": str(detail)[-500:],
        "pid": os.getpid(),
    }
    try:
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = HEARTBEAT_PATH.with_name(f"{HEARTBEAT_PATH.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(HEARTBEAT_PATH)
    except Exception as exc:
        print(f"[scheduler] heartbeat write failed: {exc}", flush=True)


def refresh_runtime_state() -> dict:
    """Refresh quota metadata and repair local queue invariants without source I/O."""
    quota_lock = QUOTA_PATH.with_name(QUOTA_PATH.name + ".lock")
    with exclusive_control_lock(quota_lock):
        quota = load_quota()
        save_quota(quota)
    with database_write_lock(DB_PATH):
        with SQLitePostStore(DB_PATH) as store:
            store.ensure_runtime_schema()
    return quota


def bootstrap_is_complete() -> bool:
    """Check the durable 20-page list1 baseline without source I/O."""
    try:
        with database_write_lock(DB_PATH):
            with SQLitePostStore(DB_PATH) as store:
                store.ensure_runtime_schema()
                row = store.conn.execute(
                    "select value from ledger_state where key=?",
                    ("lists_bootstrap_complete",),
                ).fetchone()
                return bool(
                    row
                    and str(row[0] or "")
                    in {"1", "true", '"1"', '"true"'}
                )
    except Exception as exc:
        print(
            f"[scheduler] bootstrap state check failed: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return False


def pipeline_phase() -> str:
    """Read the durable three-stage crawler phase without source I/O."""
    try:
        with database_write_lock(DB_PATH):
            with SQLitePostStore(DB_PATH) as store:
                store.ensure_runtime_schema()
                value = ledger_state(store.conn, PIPELINE_PHASE_KEY, "")
                return value if value in PIPELINE_PHASES else ""
    except Exception as exc:
        print(
            f"[scheduler] pipeline phase check failed: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return ""


def set_pipeline_phase(phase: str) -> str:
    """Persist one of the explicit crawler phases across restarts."""
    if phase not in PIPELINE_PHASES:
        raise ValueError(f"unsupported crawler pipeline phase: {phase}")
    with database_write_lock(DB_PATH):
        with SQLitePostStore(DB_PATH) as store:
            store.ensure_runtime_schema()
            set_ledger_state(store.conn, PIPELINE_PHASE_KEY, phase)
            store.conn.commit()
    return phase


def ensure_pipeline_phase() -> str:
    """Migrate old monitor state into list1 -> backfill -> monitoring.

    The previous scheduler used ``monitor_list1_seed_complete`` as a gate and
    could start monitoring before the initial detail queue was drained.  The
    new phase is authoritative.  The old ``monitor_list1_seed_complete``
    marker is deliberately ignored, so a fresh list1 seed is required before
    any detail task or list2 monitor is scheduled.
    """
    with database_write_lock(DB_PATH):
        with SQLitePostStore(DB_PATH) as store:
            store.ensure_runtime_schema()
            current = ledger_state(store.conn, PIPELINE_PHASE_KEY, "")
            bootstrap_done = ledger_state(
                store.conn,
                "lists_bootstrap_complete",
                "0",
            ) in {"1", "true", "True", '"1"', '"true"'}
            if not bootstrap_done:
                target = PIPELINE_PHASE_BOOTSTRAP
            elif current == PIPELINE_PHASE_BOOTSTRAP:
                target = PIPELINE_PHASE_DETAIL_BACKFILL
            elif current in {
                PIPELINE_PHASE_LIST1_SEED,
                PIPELINE_PHASE_DETAIL_BACKFILL,
                PIPELINE_PHASE_MONITORING,
            }:
                target = current
            else:
                # ``monitor_list1_seed_complete`` belongs to the retired
                # scheduler.  Do not let that legacy marker skip the fresh
                # list1 stage after this architecture is enabled.
                target = PIPELINE_PHASE_LIST1_SEED
            if current != target:
                set_ledger_state(store.conn, PIPELINE_PHASE_KEY, target)
                store.conn.commit()
            return target


def bootstrap_details_are_complete() -> bool:
    """Return whether every ID from the bootstrap queue reached a terminal state."""
    try:
        with database_write_lock(DB_PATH):
            with SQLitePostStore(DB_PATH) as store:
                store.ensure_runtime_schema()
                total = int(
                    store.conn.execute(
                        """
                        select count(*) from post_id_ledger
                        where bootstrap_run_id!=''
                        """
                    ).fetchone()[0]
                    or 0
                )
                if total <= 0:
                    return False
                incomplete = int(
                    store.conn.execute(
                        """
                        select count(*)
                        from post_id_ledger l
                        left join crawler_queue q on q.post_id=l.post_id
                        left join posts p on p.id=l.post_id
                        where l.bootstrap_run_id!=''
                          and not (
                              (
                                  l.detail_status='succeeded'
                                  and coalesce(q.status,'') in ('done','deferred')
                              )
                              or (
                                  l.detail_status='not_found'
                                  and coalesce(q.status,'')='skipped'
                              )
                              or (
                                  coalesce(q.status,'')='done'
                                  and coalesce(p.crawl_status,'')='full'
                              )
                              or coalesce(q.status,'') in
                                  ('failed','skipped','deferred')
                          )
                        """
                    ).fetchone()[0]
                    or 0
                )
                return incomplete == 0
    except Exception as exc:
        print(
            f"[scheduler] bootstrap detail state check failed: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return False


def detail_backfill_is_complete() -> bool:
    """Return whether the initial list1 ID cohort reached terminal detail state."""
    return bootstrap_details_are_complete()


def prepare_monitor_cutover() -> dict[str, object]:
    """Pause historical coverage rows at one durable queue boundary.

    The monitor still uses the one shared queue.  Only positive-priority rows
    that existed at this boundary are held; priority 0 refreshes and rows
    appended by later list scans remain eligible.  The boundary is written
    once so a restart cannot gradually move the definition of "old" work.
    """

    with database_write_lock(DB_PATH):
        with SQLitePostStore(DB_PATH) as store:
            store.ensure_runtime_schema()
            existing = ledger_state(store.conn, "monitor_queue_order_cutoff", "")
            if existing:
                try:
                    cutoff = int(existing)
                except (TypeError, ValueError):
                    cutoff = 0
                if ledger_state(store.conn, "monitor_old_coverage_paused", "") not in {
                    "1",
                    "true",
                    "True",
                }:
                    set_ledger_state(store.conn, "monitor_old_coverage_paused", "1")
                    store.conn.commit()
                return {"queue_order_cutoff": cutoff, "created": False}

            cutoff = int(
                store.conn.execute(
                    "select coalesce(max(queue_order), 0) from crawler_queue"
                ).fetchone()[0]
                or 0
            )
            set_ledger_state(store.conn, "monitor_queue_order_cutoff", str(cutoff))
            set_ledger_state(store.conn, "monitor_old_coverage_paused", "1")
            set_ledger_state(
                store.conn,
                "monitor_cutover_at",
                beijing_now().isoformat(),
            )
            store.conn.commit()
            return {"queue_order_cutoff": cutoff, "created": True}


def monitor_list1_seed_is_complete() -> bool:
    """Return whether the post-cutover list1 seed made a real source call."""
    try:
        with database_write_lock(DB_PATH):
            with SQLitePostStore(DB_PATH) as store:
                store.ensure_runtime_schema()
                return ledger_state(store.conn, MONITOR_LIST1_SEED_KEY, "") in {
                    "1",
                    "true",
                    "True",
                }
    except Exception as exc:
        print(
            f"[scheduler] list1 seed state check failed: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return False


def mark_monitor_list1_seed_complete() -> None:
    """Persist the one-time list1 seed completion across restarts."""
    with database_write_lock(DB_PATH):
        with SQLitePostStore(DB_PATH) as store:
            store.ensure_runtime_schema()
            set_ledger_state(store.conn, MONITOR_LIST1_SEED_KEY, "1")
            store.conn.commit()


def enable_monitor_jobs(
    next_run: dict[str, float],
    intervals: dict[str, float],
    now: float,
) -> None:
    """Schedule the first list1 monitor pass after bootstrap cutover.

    list2 is deliberately added only after this first list1 job has made at
    least one source request; otherwise a quota-window deferral could make
    both jobs due together and select list2 first.
    """
    if "discover_new" in next_run:
        return

    next_run["discover_new"] = now + 3 * 60
    intervals["discover_new"] = NEW_DISCOVER_INTERVAL


def enable_remaining_monitor_jobs(
    next_run: dict[str, float],
    intervals: dict[str, float],
    now: float,
) -> None:
    """Add detail/list2 jobs after the first list1 request."""
    if "discover_new" not in next_run:
        next_run["discover_new"] = now + 3 * 60
        intervals["discover_new"] = NEW_DISCOVER_INTERVAL
    if "trickle_fill" not in next_run:
        next_run["trickle_fill"] = now + 90
        intervals["trickle_fill"] = TRICKLE_INTERVAL
    if "discover_active" not in next_run:
        next_run["discover_active"] = now + 8 * 60
        intervals["discover_active"] = ACTIVE_DISCOVER_INTERVAL
    if GAP_ENABLED:
        next_run.update(
            {
                "plan_gaps": now + 10 * 60,
                "probe_gaps": now + 20 * 60,
            }
        )
        intervals.update(
            {
                "plan_gaps": GAP_PLAN_INTERVAL,
                "probe_gaps": GAP_PROBE_INTERVAL,
            }
        )


def sync_pipeline_jobs(
    phase: str,
    next_run: dict[str, float],
    intervals: dict[str, float],
    now: float,
) -> None:
    """Make the in-memory schedule match the durable three-stage phase."""
    desired: set[str]
    if phase == PIPELINE_PHASE_BOOTSTRAP:
        desired = {"bootstrap_new"}
    elif phase == PIPELINE_PHASE_LIST1_SEED:
        desired = {"discover_new"}
    elif phase == PIPELINE_PHASE_DETAIL_BACKFILL:
        desired = {"trickle_fill"}
    elif phase == PIPELINE_PHASE_MONITORING:
        desired = {"trickle_fill", "discover_new", "discover_active"}
        prepare_monitor_cutover()
    else:
        raise ValueError(f"unsupported pipeline phase: {phase}")

    for name in list(next_run):
        if name not in desired:
            next_run.pop(name, None)
            intervals.pop(name, None)

    starts = {
        "bootstrap_new": (60.0, BOOTSTRAP_RETRY_INTERVAL),
        "discover_new": (60.0 if phase == PIPELINE_PHASE_LIST1_SEED else 3 * 60, NEW_DISCOVER_INTERVAL),
        "discover_active": (8 * 60, ACTIVE_DISCOVER_INTERVAL),
        "trickle_fill": (90.0, TRICKLE_INTERVAL),
    }
    for name in desired:
        if name not in next_run:
            delay, interval = starts[name]
            next_run[name] = now + delay
            intervals[name] = interval


@contextmanager
def running_heartbeat(job: str):
    """Keep liveness fresh while a long crawler subprocess is running."""
    stopped = threading.Event()

    def refresh() -> None:
        while not stopped.wait(HEARTBEAT_INTERVAL):
            save_heartbeat(state="running", job=job)

    worker = threading.Thread(
        target=refresh,
        daemon=True,
        name=f"scheduler-heartbeat-{job}",
    )
    save_heartbeat(state="running", job=job)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join(timeout=max(1.0, HEARTBEAT_INTERVAL + 1.0))


def select_next_job(next_run: dict[str, float], now: float) -> str:
    """Prefer valuable overdue work; otherwise return the earliest future job."""
    overdue = [name for name, due_at in next_run.items() if due_at <= now]
    if overdue:
        return min(
            overdue,
            key=lambda name: (
                OVERDUE_JOB_PRIORITY.get(name, 100),
                next_run[name],
                name,
            ),
        )
    return min(next_run, key=lambda name: (next_run[name], name))


def next_job_run(started_at: float, finished_at: float, interval: float) -> float:
    """Keep start-to-start cadence without replaying missed runs in a burst."""
    return max(started_at + interval, finished_at + 1.0)


def replace_arg(args: list[str], flag: str, value: int) -> list[str]:
    updated = list(args)
    try:
        index = updated.index(flag)
    except ValueError:
        return [*updated, flag, str(value)]
    updated[index + 1] = str(value)
    return updated


def job_budget_kind(name: str) -> str:
    if name in {"bootstrap_new", "discover_new"}:
        return "new_list"
    if name == "discover_active":
        return "active_list"
    if name == "trickle_fill":
        return "detail"
    if name == "probe_gaps":
        return "probe"
    return ""


def planned_job_calls(name: str, args: list[str]) -> int:
    if name in {"bootstrap_new", "discover_new", "discover_active"}:
        return int(args[args.index("--max-pages") + 1])
    if name == "trickle_fill":
        return int(args[args.index("--limit") + 1])
    if name == "probe_gaps":
        ranges = int(args[args.index("--range-limit") + 1])
        samples = int(args[args.index("--samples-per-range") + 1])
        return ranges * samples
    return 0


def remaining_budget(
    kind: str,
    quota: dict,
    *,
    lane_id: str = "",
) -> int:
    # List observations remain counted for audit, but do not consume the
    # detail-only budget or the old all-source pacing wall.  The upstream API
    # can still return rate_limited, which the scheduler handles separately.
    if kind in UNMETERED_LIST_KINDS:
        return 2**31 - 1
    fraction = (
        detail_quota_release_fraction()
        if kind == "detail"
        else quota_release_fraction()
    )
    if fraction <= 0:
        return 0
    key = quota_key(kind)
    if lane_id:
        lane = ensure_cookie_lane_quota(quota, lane_id)
        used = int(lane.get(key, 0) or 0)
    else:
        used = int(quota.get(key, 0) or 0)
    allowed = int(daily_budget(kind, quota, lane_id=lane_id) * fraction)
    lane_remaining = max(0, allowed - used)
    if kind == "detail":
        # Detail is the only internally budgeted source lane now.  Subtracting
        # list calls here would make an otherwise available detail slot look
        # exhausted.
        return lane_remaining
    pacing_remaining = max(
        0,
        source_pacing_allowance(quota) - quota_source_calls(quota),
    )
    return min(lane_remaining, pacing_remaining)


def quota_key(kind: str) -> str:
    return {
        "new_list": "new_list_calls",
        "active_list": "active_list_calls",
        "detail": "detail_calls",
        "probe": "probe_calls",
    }[kind]


def quota_counter_snapshot(kind: str) -> tuple[str, int]:
    if not kind:
        return "", 0
    lock_path = QUOTA_PATH.with_name(QUOTA_PATH.name + ".lock")
    with exclusive_control_lock(lock_path):
        quota = load_quota()
        return (
            str(quota.get("date") or ""),
            int(quota.get(quota_key(kind), 0) or 0),
        )


def record_failed_crawler_run(
    *,
    name: str,
    started_at: str,
    source_calls: int,
    returncode: int,
    error_kind: str,
    stderr: str,
    db_path: str | Path | None = None,
) -> None:
    command = {
        "bootstrap_new": "discover-latest",
        "discover_new": "discover-latest",
        "discover_active": "discover-active",
        "trickle_fill": "trickle-fill",
        "plan_gaps": "plan-gaps",
        "probe_gaps": "probe-gaps",
    }.get(name, name.replace("_", "-"))
    stats = {
        "source_calls": max(0, int(source_calls)),
        "errors": 1,
        "rate_limited": error_kind == "rate_limited",
        "scheduler_failed": True,
        "scheduler_job": name,
        "returncode": int(returncode),
        "error_kind": str(error_kind or "process_error"),
        "stderr_tail": str(stderr or "")[-2000:],
    }
    target_db = str(db_path or DB_PATH)
    try:
        with database_write_lock(target_db, 30):
            with SQLitePostStore(target_db) as store:
                store.record_crawler_run(
                    command=command,
                    stats=stats,
                    started_at=started_at,
                    commit=True,
                )
    except Exception as exc:
        print(
            f"[scheduler] failed to record job history name={name}: {exc}",
            file=sys.stderr,
            flush=True,
        )


def prepare_job(name: str) -> tuple[list[str] | None, str]:
    lock_path = QUOTA_PATH.with_name(QUOTA_PATH.name + ".lock")
    with exclusive_control_lock(lock_path):
        args = job_args(name)
        kind = job_budget_kind(name)
        if not kind:
            return args, ""
        if kind in UNMETERED_LIST_KINDS:
            planned = planned_job_calls(name, args)
            return args, f"{kind}_observation_unmetered planned_max={planned}"
        quota = load_quota()
        remaining = remaining_budget(kind, quota)
        if remaining <= 0:
            if quota_release_fraction_for_kind(kind) <= 0:
                return None, (
                    f"{kind}_quota_window_locked_until="
                    f"{next_quota_release_for_kind(kind).isoformat()}"
                )
            return None, f"{kind}_budget_exhausted"
        if name in {"discover_new", "discover_active"}:
            max_pages = max(
                1,
                min(int(args[args.index("--max-pages") + 1]), remaining),
            )
            args = replace_arg(args, "--max-pages", max_pages)
        elif name == "trickle_fill":
            args = replace_arg(
                args,
                "--limit",
                max(1, min(int(args[args.index("--limit") + 1]), remaining)),
            )
        elif name == "probe_gaps":
            if daily_budget("probe", quota) <= 0:
                return None, "probe_budget_disabled"
            range_limit = max(
                1,
                min(int(args[args.index("--range-limit") + 1]), remaining),
            )
            samples = max(
                1,
                min(
                    int(args[args.index("--samples-per-range") + 1]),
                    max(1, remaining // range_limit),
                ),
            )
            args = replace_arg(args, "--range-limit", range_limit)
            args = replace_arg(args, "--samples-per-range", samples)
        planned = planned_job_calls(name, args)
        return args, f"{kind}_calls_available={remaining} planned_max={planned}"


def job_args(name: str) -> list[str]:
    if name in TRICKLE_JOBS:
        return TRICKLE_JOBS[name]
    if name == "phase1":
        from_date = (datetime.now(CHINA_TZ).date() - timedelta(days=7)).isoformat()
        return [
            "scan-id-range",
            "--from-date",
            from_date,
            "--workers",
            "10",
            "--chunk-size",
            "500",
            "--lock-timeout",
            "21600",
        ]
    return JOBS[name]


def run_job(name: str) -> JobResult:
    args, quota_note = prepare_job(name)
    if args is None:
        print(f"[scheduler] skip {name} reason={quota_note}", flush=True)
        deferred_until = 0.0
        if "_quota_window_locked_until=" in quota_note or quota_note.startswith(
            "quota_window_locked_until="
        ):
            try:
                release_at = datetime.fromisoformat(
                    quota_note.rsplit("=", 1)[1]
                )
                deferred_until = release_at.timestamp()
            except (TypeError, ValueError):
                deferred_until = 0.0
        return JobResult(
            succeeded=True,
            error_kind="quota_window_locked" if deferred_until else "",
            stderr=quota_note,
            deferred_until=deferred_until,
        )
    if quota_note:
        print(f"[scheduler] quota {name} {quota_note}", flush=True)
    command = [
        sys.executable,
        str(ROOT / "crawler_db.py"),
        *args,
        "--db-path",
        DB_PATH,
        "--config",
        CONFIG_PATH,
    ]
    if str(COOKIE_POOL_PATH or "").strip():
        command.extend(["--cookie-pool", COOKIE_POOL_PATH])
    child_env = os.environ.copy()
    child_env["SQLITE_DB"] = DB_PATH
    child_env["CRAWLER_QUOTA_FILE"] = str(QUOTA_PATH)
    child_env["CRAWLER_QUOTA_HISTORY_FILE"] = str(QUOTA_HISTORY_PATH)
    child_env["CRAWLER_PAUSE_FILE"] = str(PAUSE_PATH)
    if str(COOKIE_POOL_PATH or "").strip():
        child_env["CRAWLER_COOKIE_POOL"] = COOKIE_POOL_PATH
    kind = job_budget_kind(name)
    if kind:
        child_env[AUTOMATIC_QUOTA_KIND_ENV] = kind
    else:
        child_env.pop(AUTOMATIC_QUOTA_KIND_ENV, None)
    run_started_at = beijing_now().isoformat()
    before_date, before_calls = quota_counter_snapshot(kind)
    print(f"[scheduler] start {name}", flush=True)
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=child_env,
            check=False,
            text=True,
            stderr=subprocess.PIPE,
        )
    except Exception as exc:
        after_date, after_calls = quota_counter_snapshot(kind)
        source_calls = (
            max(0, after_calls - before_calls)
            if before_date == after_date
            else max(0, after_calls)
        )
        record_failed_crawler_run(
            name=name,
            started_at=run_started_at,
            source_calls=source_calls,
            returncode=-1,
            error_kind=type(exc).__name__,
            stderr=str(exc),
        )
        raise
    stderr = result.stderr or ""
    if stderr:
        print(stderr, file=sys.stderr, end="" if stderr.endswith("\n") else "\n")
    print(f"[scheduler] done {name} exit={result.returncode}", flush=True)
    after_date, after_calls = quota_counter_snapshot(kind)
    source_calls = (
        max(0, after_calls - before_calls)
        if before_date == after_date
        else max(0, after_calls)
    )
    job_result = JobResult(
        succeeded=result.returncode == 0,
        error_kind=classify_error(stderr),
        stderr=stderr,
        returncode=result.returncode,
        source_calls=source_calls,
    )
    if not job_result.succeeded:
        record_failed_crawler_run(
            name=name,
            started_at=run_started_at,
            source_calls=source_calls,
            returncode=job_result.returncode,
            error_kind=job_result.error_kind,
            stderr=job_result.stderr,
        )
    return job_result


def phase1_delay() -> float:
    if not PHASE1_MARKER.exists():
        PHASE1_MARKER.touch()
    elapsed = max(0.0, time.time() - PHASE1_MARKER.stat().st_mtime)
    return max(60.0, PHASE1_INTERVAL - elapsed)


def main() -> int:
    if not Path(DB_PATH).exists():
        raise FileNotFoundError(DB_PATH)
    if not Path(CONFIG_PATH).exists():
        raise FileNotFoundError(CONFIG_PATH)
    if str(COOKIE_POOL_PATH or "").strip():
        for spec in cookie_pool_specs():
            if not spec.config_path.exists():
                raise FileNotFoundError(spec.config_path)

    try:
        startup_quota = refresh_runtime_state()
        print(
            "[scheduler] quota "
            f"detail_target={startup_quota.get('detail_budget_target')} "
            f"detail_effective={startup_quota.get('effective_detail_budget')} "
            f"detail_decision={startup_quota.get('detail_budget_decision')} "
            f"source_effective={startup_quota.get('effective_source_budget')} "
            f"source_ceiling={startup_quota.get('configured_source_budget')} "
            f"pacing_anchor={startup_quota.get('rate_limit_pacing_anchor')} "
            f"source_released={startup_quota.get('source_pacing_allowance')} "
            f"rate_limit_excluded_dates="
            f"{startup_quota.get('rate_limit_excluded_dates')}",
            flush=True,
        )
    except Exception as exc:
        print(
            f"[scheduler] runtime-state refresh failed: {exc}",
            file=sys.stderr,
            flush=True,
        )

    now = time.monotonic()
    if TRICKLE_ENABLED:
        bootstrap_pending = not bootstrap_is_complete()
        phase = ensure_pipeline_phase()
        if bootstrap_pending:
            phase = PIPELINE_PHASE_BOOTSTRAP
        next_run: dict[str, float] = {}
        intervals: dict[str, float] = {}
        sync_pipeline_jobs(phase, next_run, intervals, now)
        print(
            "[scheduler] trickle enabled "
            f"bootstrap={'pending' if bootstrap_pending else 'complete'} "
            f"phase={phase} "
            f"bootstrap_pages={BOOTSTRAP_PAGES} "
            f"since={TRICKLE_SINCE!r} list1={NEW_DISCOVER_INTERVAL}s "
            f"list2={ACTIVE_DISCOVER_INTERVAL}s "
            f"trickle={TRICKLE_INTERVAL}s limit={TRICKLE_LIMIT} "
            f"refresh_limit={TRICKLE_REFRESH_LIMIT} "
            f"fresh_hours={TRICKLE_FRESH_COVERAGE_HOURS} "
            f"gap={GAP_ENABLED} gap_since={GAP_SINCE!r}",
            flush=True,
        )
    else:
        next_run = {
            # Avoid an extra full scan on every deployment. The two regular jobs
            # remain staggered, but the first run also respects that cadence.
            "new": now + NEW_INTERVAL / 2,
            "refresh": now + REFRESH_INTERVAL,
            "backfill": now + 6 * 60 * 60,
            "phase1": now + phase1_delay(),
        }
        intervals = {
            "new": NEW_INTERVAL,
            "refresh": REFRESH_INTERVAL,
            "backfill": BACKFILL_INTERVAL,
            "phase1": PHASE1_INTERVAL,
        }
        print(
            "[scheduler] enabled "
            f"new={NEW_INTERVAL}s refresh={REFRESH_INTERVAL}s "
            f"backfill={BACKFILL_INTERVAL}s phase1={PHASE1_INTERVAL}s",
            flush=True,
        )

    last_pause_log: dict[str, float] = {}
    last_heartbeat = 0.0
    save_heartbeat(state="started")
    while True:
        now = time.monotonic()
        if TRICKLE_ENABLED:
            phase = ensure_pipeline_phase()
            if not bootstrap_is_complete():
                phase = PIPELINE_PHASE_BOOTSTRAP
            sync_pipeline_jobs(phase, next_run, intervals, now)
        due = select_next_job(next_run, now)
        pause = active_pause()
        if pause:
            until_monotonic = now + max(1.0, float(pause["until"]) - now_wall())
            last_logged = last_pause_log.get(due, 0.0)
            if now - last_logged >= PAUSE_LOG_INTERVAL:
                print(
                    "[scheduler] paused "
                    f"reason={pause.get('reason')} due={due} "
                    f"until={pause.get('until_text')}",
                    flush=True,
                )
                last_pause_log[due] = now
            if now - last_heartbeat >= 60:
                save_heartbeat(
                    state="paused",
                    job=due,
                    detail=str(pause.get("reason") or ""),
                )
                last_heartbeat = now
            for name in next_run:
                next_run[name] = max(next_run[name], until_monotonic)
            time.sleep(min(max(1.0, until_monotonic - now), 30))
            continue
        wait = next_run[due] - now
        if wait > 0:
            if now - last_heartbeat >= 60:
                save_heartbeat(state="idle", job=due)
                last_heartbeat = now
            time.sleep(min(wait, 30))
            continue
        started_at = time.monotonic()
        last_heartbeat = started_at
        try:
            with running_heartbeat(due):
                result = run_job(due)
            if result.error_kind == "rate_limited":
                handle_rate_limit(job=due, detail=result.stderr)
            elif result.error_kind == "cookie_expired":
                save_pause(
                    reason="cookie_expired",
                    job=due,
                    seconds=COOKIE_ERROR_COOLDOWN,
                    detail=result.stderr,
                )
            if due == "phase1" and result.succeeded:
                PHASE1_MARKER.touch()
            if (
                due == "discover_new"
                and result.succeeded
                and result.source_calls > 0
                and pipeline_phase() == PIPELINE_PHASE_LIST1_SEED
            ):
                set_pipeline_phase(PIPELINE_PHASE_DETAIL_BACKFILL)
                sync_pipeline_jobs(
                    PIPELINE_PHASE_DETAIL_BACKFILL,
                    next_run,
                    intervals,
                    time.monotonic(),
                )
                print(
                    "[scheduler] list1 seed completed; detail backfill started",
                    flush=True,
                )
            if (
                due == "bootstrap_new"
                and result.succeeded
                and bootstrap_is_complete()
            ):
                set_pipeline_phase(PIPELINE_PHASE_DETAIL_BACKFILL)
                sync_pipeline_jobs(
                    PIPELINE_PHASE_DETAIL_BACKFILL,
                    next_run,
                    intervals,
                    time.monotonic(),
                )
                save_heartbeat(
                    state="idle",
                    job=due,
                    detail="bootstrap_complete_detail_backfill",
                )
                last_heartbeat = time.monotonic()
                continue
            if (
                due == "trickle_fill"
                and result.succeeded
                and pipeline_phase() == PIPELINE_PHASE_DETAIL_BACKFILL
                and detail_backfill_is_complete()
            ):
                set_pipeline_phase(PIPELINE_PHASE_MONITORING)
                sync_pipeline_jobs(
                    PIPELINE_PHASE_MONITORING,
                    next_run,
                    intervals,
                    time.monotonic(),
                )
                print(
                    "[scheduler] detail backfill completed; list1/list2 monitoring started",
                    flush=True,
                )
                save_heartbeat(
                    state="idle",
                    job=due,
                    detail="detail_backfill_complete_monitoring",
                )
                last_heartbeat = time.monotonic()
                continue
            if result.deferred_until:
                next_run[due] = max(
                    time.monotonic() + 1.0,
                    time.monotonic() + max(0.0, result.deferred_until - now_wall()),
                )
                save_heartbeat(
                    state="idle",
                    job=due,
                    detail=result.stderr,
                )
                last_heartbeat = time.monotonic()
                continue
        except Exception as exc:
            print(
                f"[scheduler] job error name={due} type={type(exc).__name__} detail={exc}",
                file=sys.stderr,
                flush=True,
            )
            save_heartbeat(state="error", job=due, detail=str(exc))
            next_run[due] = time.monotonic() + 60
            continue
        retry_delay = 60 * 60 if due == "phase1" and not result.succeeded else intervals[due]
        finished_at = time.monotonic()
        next_run[due] = next_job_run(started_at, finished_at, retry_delay)
        save_heartbeat(
            state="idle",
            job=due,
            detail="succeeded" if result.succeeded else "failed",
        )
        last_heartbeat = finished_at


if __name__ == "__main__":
    raise SystemExit(main())
