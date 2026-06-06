#!/usr/bin/env python3
"""Run conservative DB crawler updates inside the Railway web service."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = os.environ.get("SQLITE_DB", "/app/data/posts.db")
CONFIG_PATH = os.environ.get("CRAWLER_CONFIG", "/app/data/config.txt")


def env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


NEW_INTERVAL = env_int("CRAWLER_NEW_INTERVAL", 4 * 60 * 60)
REFRESH_INTERVAL = env_int("CRAWLER_REFRESH_INTERVAL", 4 * 60 * 60)
BACKFILL_INTERVAL = env_int("CRAWLER_BACKFILL_INTERVAL", 24 * 60 * 60)
PHASE1_INTERVAL = env_int("CRAWLER_PHASE1_INTERVAL", 7 * 24 * 60 * 60)
PHASE1_MARKER = Path(DB_PATH).with_name(".phase1_weekly_last")
CHINA_TZ = timezone(timedelta(hours=8))


JOBS = {
    "new": [
        "new", "--pages", "100", "--min-pages", "20",
        "--stop-unchanged", "220", "--max-details", "0",
    ],
    "refresh": [
        "refresh", "--pages", "100", "--min-pages", "20",
        "--stop-unchanged", "220", "--max-details", "0",
    ],
    "backfill": [
        "backfill", "--endpoint", "lists2", "--start-page", "2",
        "--pages", "99", "--min-pages", "99",
        "--stop-unchanged", "100000", "--max-details", "0",
    ],
}


def job_args(name: str) -> list[str]:
    if name == "phase1":
        from_date = (datetime.now(CHINA_TZ).date() - timedelta(days=7)).isoformat()
        return [
            "phase1", "--from-date", from_date,
            "--workers", "10", "--chunk-size", "500",
            "--lock-timeout", "21600",
        ]
    return JOBS[name]


def run_job(name: str) -> bool:
    command = [
        sys.executable,
        str(ROOT / "crawler_db.py"),
        *job_args(name),
        "--db-path", DB_PATH,
        "--config", CONFIG_PATH,
    ]
    print(f"[scheduler] start {name}", flush=True)
    result = subprocess.run(command, cwd=ROOT, check=False)
    print(f"[scheduler] done {name} exit={result.returncode}", flush=True)
    return result.returncode == 0


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
    next_run = {
        "new": now + 60,
        "refresh": now + 2 * 60 * 60,
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

    while True:
        now = time.monotonic()
        due = min(next_run, key=next_run.get)
        wait = next_run[due] - now
        if wait > 0:
            time.sleep(min(wait, 30))
            continue
        succeeded = run_job(due)
        if due == "phase1" and succeeded:
            PHASE1_MARKER.touch()
        retry_delay = 60 * 60 if due == "phase1" and not succeeded else intervals[due]
        next_run[due] = time.monotonic() + retry_delay


if __name__ == "__main__":
    raise SystemExit(main())
