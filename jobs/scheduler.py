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


def env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
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
RATE_LIMIT_COOLDOWN = env_int("CRAWLER_RATE_LIMIT_COOLDOWN", 6 * 60 * 60)
COOKIE_ERROR_COOLDOWN = env_int("CRAWLER_COOKIE_ERROR_COOLDOWN", 6 * 60 * 60)


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
        "--max-pages", "180", "--min-delay", "0.1", "--max-delay", "0.3",
    ],
    "discover_active": [
        "discover-active", "--since", TRICKLE_SINCE,
        "--max-pages", "120", "--min-delay", "0.1", "--max-delay", "0.3",
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
    until = now_wall() + max(1, int(seconds))
    pause = {
        "reason": reason,
        "job": job,
        "until": until,
        "until_text": datetime.fromtimestamp(until, CHINA_TZ).isoformat(),
        "detail": detail[-500:],
        "updated_at": datetime.now(CHINA_TZ).isoformat(),
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


def active_pause() -> dict:
    pause = load_pause()
    until = float(pause.get("until") or 0)
    if until > now_wall():
        return pause
    if pause:
        clear_pause("expired")
    return {}


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
    command = [
        sys.executable,
        str(ROOT / "crawler_db.py"),
        *job_args(name),
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

    while True:
        now = time.monotonic()
        due = min(next_run, key=next_run.get)
        pause = active_pause()
        if pause:
            until_monotonic = now + max(1.0, float(pause["until"]) - now_wall())
            print(
                "[scheduler] paused "
                f"reason={pause.get('reason')} due={due} "
                f"until={pause.get('until_text')}",
                flush=True,
            )
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
            save_pause(
                reason="rate_limited",
                job=due,
                seconds=RATE_LIMIT_COOLDOWN,
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
