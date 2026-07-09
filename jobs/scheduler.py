#!/usr/bin/env python3
"""Run conservative DB crawler updates inside the Railway web service."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = os.environ.get("SQLITE_DB", "/app/data/posts.db")
CONFIG_PATH = os.environ.get("CRAWLER_CONFIG", "/app/data/config.txt")
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


NEW_INTERVAL = env_int("CRAWLER_NEW_INTERVAL", 8 * 60 * 60)
REFRESH_INTERVAL = env_int("CRAWLER_REFRESH_INTERVAL", 8 * 60 * 60)
BACKFILL_INTERVAL = env_int("CRAWLER_BACKFILL_INTERVAL", 24 * 60 * 60)
PHASE1_INTERVAL = env_int("CRAWLER_PHASE1_INTERVAL", 7 * 24 * 60 * 60)
PHASE1_MARKER = Path(DB_PATH).with_name(".phase1_weekly_last")
CHINA_TZ = timezone(timedelta(hours=8))
TRICKLE_ENABLED = os.environ.get("CRAWLER_TRICKLE_ENABLED", "0") == "1"
TRICKLE_SINCE = os.environ.get("CRAWLER_TRICKLE_SINCE", "2026-06-25 00:00:00")
DISCOVER_INTERVAL = env_int("CRAWLER_DISCOVER_INTERVAL", 30 * 60)
TRICKLE_INTERVAL = env_int("CRAWLER_TRICKLE_INTERVAL", 10 * 60)
TRICKLE_LIMIT = env_int("CRAWLER_TRICKLE_LIMIT", 30)
DISCOVER_LATEST_PAGES = env_int("CRAWLER_DISCOVER_LATEST_PAGES", 60)
DISCOVER_ACTIVE_PAGES = env_int("CRAWLER_DISCOVER_ACTIVE_PAGES", 80)
GAP_ENABLED = (
    os.environ.get("CRAWLER_GAP_ENABLED", "1" if TRICKLE_ENABLED else "0") == "1"
)
GAP_SINCE = os.environ.get("CRAWLER_GAP_SINCE", TRICKLE_SINCE)
GAP_PLAN_INTERVAL = env_int("CRAWLER_GAP_PLAN_INTERVAL", 6 * 60 * 60)
GAP_PROBE_INTERVAL = env_int("CRAWLER_GAP_PROBE_INTERVAL", 2 * 60 * 60)
GAP_RANGE_LIMIT = env_int("CRAWLER_GAP_RANGE_LIMIT", 1)
GAP_SAMPLES = env_int("CRAWLER_GAP_SAMPLES", 12)
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
DAILY_DETAIL_BUDGET = env_int("CRAWLER_DAILY_DETAIL_BUDGET", 450)
DAILY_PROBE_BUDGET = env_nonnegative_int("CRAWLER_DAILY_PROBE_BUDGET", 0)
QUOTA_FIRST_RELEASE_HOUR = env_nonnegative_int("CRAWLER_QUOTA_FIRST_RELEASE_HOUR", 11)
QUOTA_SECOND_RELEASE_HOUR = env_nonnegative_int("CRAWLER_QUOTA_SECOND_RELEASE_HOUR", 23)
RESET_GRACE_MINUTES = env_int("CRAWLER_RESET_GRACE_MINUTES", 5)
PAUSE_LOG_INTERVAL = env_int("CRAWLER_PAUSE_LOG_INTERVAL", 10 * 60)


JOBS = {
    "new": [
        "sync-latest", "--pages", "100", "--min-pages", "20",
        "--stop-unchanged", "220", "--max-details", "0",
    ],
    "refresh": [
        "sync-active", "--pages", "100", "--min-pages", "20",
        "--stop-unchanged", "220", "--max-details", "0",
    ],
    "backfill": [
        "scan-history", "--endpoint", "lists2", "--start-page", "2",
        "--pages", "99", "--min-pages", "99",
        "--stop-unchanged", "100000", "--max-details", "0",
    ],
}

TRICKLE_JOBS = {
    "discover_new": [
        "discover-latest", "--since", TRICKLE_SINCE,
        "--max-pages", str(DISCOVER_LATEST_PAGES),
        "--min-pages", "5", "--no-action-page-threshold", "5",
        "--min-delay", "0.1", "--max-delay", "0.3",
    ],
    "discover_active": [
        "discover-active", "--since", TRICKLE_SINCE,
        "--max-pages", str(DISCOVER_ACTIVE_PAGES),
        "--min-pages", "5", "--no-action-page-threshold", "3",
        "--min-delay", "0.1", "--max-delay", "0.3",
    ],
    "trickle_fill": [
        "trickle-fill", "--limit", str(TRICKLE_LIMIT),
        "--min-delay", "5", "--max-delay", "10",
    ],
}

if GAP_ENABLED:
    TRICKLE_JOBS.update(
        {
            "plan_gaps": [
                "plan-gaps", "--since", GAP_SINCE,
                "--chunk-size", str(GAP_CHUNK_SIZE),
                "--density-threshold", str(GAP_DENSITY_THRESHOLD),
            ],
            "probe_gaps": [
                "probe-gaps",
                "--range-limit", str(GAP_RANGE_LIMIT),
                "--samples-per-range", str(GAP_SAMPLES),
                "--min-delay", "8", "--max-delay", "15",
            ],
        }
    )


@dataclass(frozen=True)
class JobResult:
    succeeded: bool
    error_kind: str = ""
    stderr: str = ""


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


def quota_release_fraction(at: datetime | None = None) -> float:
    at = at.astimezone(CHINA_TZ) if at else beijing_now()
    first_hour = min(23, QUOTA_FIRST_RELEASE_HOUR)
    second_hour = min(23, max(first_hour + 1, QUOTA_SECOND_RELEASE_HOUR))
    if at.hour < first_hour:
        return 0.0
    if at.hour < second_hour:
        return 0.5
    return 1.0


def next_quota_release(at: datetime | None = None) -> datetime:
    at = at.astimezone(CHINA_TZ) if at else beijing_now()
    first_hour = min(23, QUOTA_FIRST_RELEASE_HOUR)
    second_hour = min(23, max(first_hour + 1, QUOTA_SECOND_RELEASE_HOUR))
    first = at.replace(hour=first_hour, minute=0, second=0, microsecond=0)
    second = at.replace(hour=second_hour, minute=0, second=0, microsecond=0)
    if at < first:
        return first
    if at < second:
        return second
    tomorrow = at.date() + timedelta(days=1)
    return datetime.combine(tomorrow, datetime.min.time(), tzinfo=CHINA_TZ).replace(
        hour=first_hour
    )


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
        "[scheduler] pause crawler "
        f"reason={reason} job={job} until={pause['until_text']}",
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
        "[scheduler] extend rate-limit pause "
        f"until={pause['until_text']}",
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
        quota = {
            "date": today,
            "new_list_calls": 0,
            "active_list_calls": 0,
            "detail_calls": 0,
            "probe_calls": 0,
            "rate_limited": 0,
            "updated_at": beijing_now().isoformat(),
        }
        save_quota(quota)
    quota.setdefault("new_list_calls", 0)
    quota.setdefault("active_list_calls", 0)
    if "list_calls" in quota:
        # Older quota files only had a combined list counter. Keep the value
        # visible but do not split it retroactively; the new per-source counters
        # are authoritative from this deployment onward.
        quota.setdefault("legacy_list_calls", quota.get("list_calls", 0))
        quota.pop("list_calls", None)
    return quota


def save_quota(quota: dict) -> None:
    quota["updated_at"] = beijing_now().isoformat()
    QUOTA_PATH.write_text(
        json.dumps(quota, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def replace_arg(args: list[str], flag: str, value: int) -> list[str]:
    updated = list(args)
    try:
        index = updated.index(flag)
    except ValueError:
        return [*updated, flag, str(value)]
    updated[index + 1] = str(value)
    return updated


def job_budget_kind(name: str) -> str:
    if name == "discover_new":
        return "new_list"
    if name == "discover_active":
        return "active_list"
    if name == "plan_gaps":
        return "new_list"
    if name == "trickle_fill":
        return "detail"
    if name == "probe_gaps":
        return "probe"
    return ""


def planned_job_calls(name: str, args: list[str]) -> int:
    if name in {"discover_new", "discover_active"}:
        return int(args[args.index("--max-pages") + 1]) + 1
    if name == "plan_gaps":
        # plan-gaps asks the source for the current latest id when --end-id is
        # omitted. Count it so gap planning cannot silently consume list quota.
        return 1
    if name == "trickle_fill":
        return int(args[args.index("--limit") + 1])
    if name == "probe_gaps":
        ranges = int(args[args.index("--range-limit") + 1])
        samples = int(args[args.index("--samples-per-range") + 1])
        return ranges * samples
    return 0


def remaining_budget(kind: str, quota: dict) -> int:
    fraction = quota_release_fraction()
    if fraction <= 0:
        return 0
    if kind == "new_list":
        allowed = int(DAILY_NEW_LIST_BUDGET * fraction)
        return max(0, allowed - int(quota.get("new_list_calls", 0)))
    if kind == "active_list":
        allowed = int(DAILY_ACTIVE_LIST_BUDGET * fraction)
        return max(0, allowed - int(quota.get("active_list_calls", 0)))
    if kind == "detail":
        allowed = int(DAILY_DETAIL_BUDGET * fraction)
        return max(0, allowed - int(quota.get("detail_calls", 0)))
    if kind == "probe":
        allowed = int(DAILY_PROBE_BUDGET * fraction)
        return max(0, allowed - int(quota.get("probe_calls", 0)))
    return 10**9


def quota_key(kind: str) -> str:
    return {
        "new_list": "new_list_calls",
        "active_list": "active_list_calls",
        "detail": "detail_calls",
        "probe": "probe_calls",
    }[kind]


def prepare_job(name: str) -> tuple[list[str] | None, str]:
    args = job_args(name)
    kind = job_budget_kind(name)
    if not kind:
        return args, ""
    quota = load_quota()
    remaining = remaining_budget(kind, quota)
    if remaining <= 0:
        if quota_release_fraction() <= 0:
            return None, f"quota_window_locked_until={next_quota_release().isoformat()}"
        return None, f"{kind}_budget_exhausted"
    if name in {"discover_new", "discover_active"}:
        if remaining < 2:
            return None, "list_budget_exhausted"
        # Reserve one extra list call for the repeat/empty stop page.
        max_pages = max(1, min(int(args[args.index("--max-pages") + 1]), remaining - 1))
        args = replace_arg(args, "--max-pages", max_pages)
    elif name == "trickle_fill":
        args = replace_arg(
            args,
            "--limit",
            max(1, min(int(args[args.index("--limit") + 1]), remaining)),
        )
    elif name == "probe_gaps":
        if DAILY_PROBE_BUDGET <= 0:
            return None, "probe_budget_disabled"
        samples = max(
            1,
            min(int(args[args.index("--samples-per-range") + 1]), remaining),
        )
        args = replace_arg(args, "--range-limit", 1)
        args = replace_arg(args, "--samples-per-range", samples)
    planned = planned_job_calls(name, args)
    quota[quota_key(kind)] = int(quota.get(quota_key(kind), 0)) + planned
    save_quota(quota)
    return args, f"{kind}_calls_reserved={planned}"


def job_args(name: str) -> list[str]:
    if name in TRICKLE_JOBS:
        return TRICKLE_JOBS[name]
    if name == "phase1":
        from_date = (datetime.now(CHINA_TZ).date() - timedelta(days=7)).isoformat()
        return [
            "scan-id-range", "--from-date", from_date,
            "--workers", "10", "--chunk-size", "500",
            "--lock-timeout", "21600",
        ]
    return JOBS[name]


def run_job(name: str) -> JobResult:
    args, quota_note = prepare_job(name)
    if args is None:
        print(f"[scheduler] skip {name} reason={quota_note}", flush=True)
        return JobResult(succeeded=True)
    if quota_note:
        print(f"[scheduler] quota {name} {quota_note}", flush=True)
    command = [
        sys.executable,
        str(ROOT / "crawler_db.py"),
        *args,
        "--db-path", DB_PATH,
        "--config", CONFIG_PATH,
    ]
    print(f"[scheduler] start {name}", flush=True)
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        text=True,
        stderr=subprocess.PIPE,
    )
    stderr = result.stderr or ""
    if stderr:
        print(stderr, file=sys.stderr, end="" if stderr.endswith("\n") else "\n")
    print(f"[scheduler] done {name} exit={result.returncode}", flush=True)
    return JobResult(
        succeeded=result.returncode == 0,
        error_kind=classify_error(stderr),
        stderr=stderr,
    )


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

    now = time.monotonic()
    if TRICKLE_ENABLED:
        next_run = {
            "discover_new": now + 60,
            "discover_active": now + 3 * 60,
            "trickle_fill": now + 5 * 60,
        }
        intervals = {
            "discover_new": DISCOVER_INTERVAL,
            "discover_active": DISCOVER_INTERVAL,
            "trickle_fill": TRICKLE_INTERVAL,
        }
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
        print(
            "[scheduler] trickle enabled "
            f"since={TRICKLE_SINCE!r} discover={DISCOVER_INTERVAL}s "
            f"trickle={TRICKLE_INTERVAL}s limit={TRICKLE_LIMIT} "
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
    while True:
        now = time.monotonic()
        due = min(next_run, key=next_run.get)
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
            for name in next_run:
                next_run[name] = max(next_run[name], until_monotonic)
            time.sleep(min(max(1.0, until_monotonic - now), 30))
            continue
        wait = next_run[due] - now
        if wait > 0:
            time.sleep(min(wait, 30))
            continue
        result = run_job(due)
        if result.error_kind == "rate_limited":
            quota = load_quota()
            quota["rate_limited"] = int(quota.get("rate_limited", 0)) + 1
            save_quota(quota)
            save_pause_until(
                reason="rate_limited",
                job=due,
                until_dt=next_beijing_reset(),
                detail=result.stderr,
            )
        elif result.error_kind == "cookie_expired":
            save_pause(
                reason="cookie_expired",
                job=due,
                seconds=COOKIE_ERROR_COOLDOWN,
                detail=result.stderr,
            )
        if due == "phase1" and result.succeeded:
            PHASE1_MARKER.touch()
        retry_delay = (
            60 * 60
            if due == "phase1" and not result.succeeded
            else intervals[due]
        )
        next_run[due] = time.monotonic() + retry_delay


if __name__ == "__main__":
    raise SystemExit(main())
