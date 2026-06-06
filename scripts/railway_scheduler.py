#!/usr/bin/env python3
"""Run conservative DB crawler updates inside the Railway web service."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = os.environ.get("SQLITE_DB", "/app/data/posts.db")
CONFIG_PATH = os.environ.get("CRAWLER_CONFIG", "/app/data/config.txt")


def env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


NEW_INTERVAL = env_int("CRAWLER_NEW_INTERVAL", 30 * 60)
REFRESH_INTERVAL = env_int("CRAWLER_REFRESH_INTERVAL", 60 * 60)
BACKFILL_INTERVAL = env_int("CRAWLER_BACKFILL_INTERVAL", 24 * 60 * 60)
ID_SCAN_FROM = os.environ.get("CRAWLER_ID_SCAN_FROM", "").strip()


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


def run_job(name: str) -> None:
    command = [
        sys.executable,
        str(ROOT / "crawler_db.py"),
        *JOBS[name],
        "--db-path", DB_PATH,
        "--config", CONFIG_PATH,
    ]
    print(f"[scheduler] start {name}", flush=True)
    result = subprocess.run(command, cwd=ROOT, check=False)
    print(f"[scheduler] done {name} exit={result.returncode}", flush=True)


def main() -> int:
    if not Path(DB_PATH).exists():
        raise FileNotFoundError(DB_PATH)
    if not Path(CONFIG_PATH).exists():
        raise FileNotFoundError(CONFIG_PATH)

    now = time.monotonic()
    next_run = {
        "new": now + 60,
        "refresh": now + 15 * 60,
        "backfill": now + 6 * 60 * 60,
    }
    intervals = {
        "new": NEW_INTERVAL,
        "refresh": REFRESH_INTERVAL,
        "backfill": BACKFILL_INTERVAL,
    }
    print(
        "[scheduler] enabled "
        f"new={NEW_INTERVAL}s refresh={REFRESH_INTERVAL}s "
        f"backfill={BACKFILL_INTERVAL}s",
        flush=True,
    )

    if ID_SCAN_FROM:
        run_job_command = [
            sys.executable,
            str(ROOT / "crawler_db.py"),
            "id-scan",
            "--from-date", ID_SCAN_FROM,
            "--db-path", DB_PATH,
            "--config", CONFIG_PATH,
            "--workers", os.environ.get("CRAWLER_ID_SCAN_WORKERS", "4"),
            "--chunk-size", os.environ.get("CRAWLER_ID_SCAN_CHUNK", "500"),
            "--lock-timeout", "21600",
        ]
        print(f"[scheduler] start id-scan from={ID_SCAN_FROM}", flush=True)
        result = subprocess.run(run_job_command, cwd=ROOT, check=False)
        print(f"[scheduler] done id-scan exit={result.returncode}", flush=True)

    while True:
        now = time.monotonic()
        due = min(next_run, key=next_run.get)
        wait = next_run[due] - now
        if wait > 0:
            time.sleep(min(wait, 30))
            continue
        run_job(due)
        next_run[due] = time.monotonic() + intervals[due]


if __name__ == "__main__":
    raise SystemExit(main())
