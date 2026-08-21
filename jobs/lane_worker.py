"""Dedicated worker for the old cookie lane.

The main scheduler owns the new-cookie monitoring lane.  When parallel lane
mode is enabled, this process owns historical details and the nightly gap
probe so the two sessions can make source requests at the same time.  Queue
claims remain in the shared SQLite database; this worker never creates a
second queue.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from crawler.lock import database_write_lock
from jobs import scheduler
from storage.post_writer import SQLitePostStore


LANE_ID = os.environ.get("CRAWLER_LANE_WORKER_MODE", "").strip().lower()
WORKER_LOCK_TIMEOUT = scheduler.env_int("CRAWLER_LANE_WORKER_LOCK_TIMEOUT", 3600)
PROBE_START = os.environ.get("CRAWLER_PROBE_START", "23:00")
PROBE_CHECK_INTERVAL = scheduler.env_int("CRAWLER_PROBE_CHECK_INTERVAL", 60)
PROBE_STATE_KEY = "crawler_probe_last_run_date"


def _probe_start(now: datetime) -> datetime:
    minute = scheduler._clock_minute(PROBE_START, 23 * 60)
    return datetime.combine(now.date(), datetime.min.time(), tzinfo=scheduler.CHINA_TZ).replace(
        hour=minute // 60,
        minute=minute % 60,
    )


def _state_value(key: str) -> str:
    with database_write_lock(scheduler.DB_PATH, 60):
        with SQLitePostStore(scheduler.DB_PATH) as store:
            store.ensure_runtime_schema()
            row = store.conn.execute(
                "select value from crawl_state where key=?",
                (str(key),),
            ).fetchone()
            return str(row[0] or "") if row else ""


def _set_state(key: str, value: str) -> None:
    with database_write_lock(scheduler.DB_PATH, 60):
        with SQLitePostStore(scheduler.DB_PATH) as store:
            store.ensure_runtime_schema()
            store.set_state(str(key), str(value), commit=True)


def _probe_due(now: datetime) -> bool:
    last = _state_value(PROBE_STATE_KEY)
    return now >= _probe_start(now) and last != now.date().isoformat()


@contextmanager
def _single_worker_lock():
    lock_path = Path(scheduler.DB_PATH).with_name(
        f".crawler_lane_worker_{LANE_ID}.lock"
    )
    with database_write_lock(
        scheduler.DB_PATH,
        WORKER_LOCK_TIMEOUT,
        lock_path=lock_path,
    ):
        yield


def _monitoring_ready() -> bool:
    try:
        return scheduler.bootstrap_is_complete() and scheduler.pipeline_phase() == scheduler.PIPELINE_PHASE_MONITORING
    except Exception as exc:
        print(f"[lane-worker] state check failed: {exc}", flush=True)
        return False


def _handle_result(job: str, result) -> None:
    if result.error_kind == "rate_limited":
        scheduler.handle_rate_limit(
            job=job,
            detail=result.stderr,
            lane_id=result.lane_id,
        )
    elif result.error_kind == "cookie_expired":
        scheduler.save_pause(
            reason="cookie_expired",
            job=job,
            seconds=scheduler.COOKIE_ERROR_COOLDOWN,
            detail=result.stderr,
            lane_id=result.lane_id,
        )


def main() -> int:
    if LANE_ID != "old":
        raise SystemExit("lane worker must run with CRAWLER_LANE_WORKER_MODE=old")
    if not scheduler.PARALLEL_LANES_ENABLED:
        raise SystemExit("lane worker requires CRAWLER_PARALLEL_LANES=1")

    print(
        f"[lane-worker] started lane={LANE_ID} "
        f"history_interval={scheduler.HISTORY_TRICKLE_INTERVAL}s "
        f"probe_start={PROBE_START} probe_samples={scheduler.NIGHT_PROBE_SAMPLES}",
        flush=True,
    )
    with _single_worker_lock():
        next_history = time.monotonic() + 3 * 60
        while True:
            now_mono = time.monotonic()
            now_wall = scheduler.beijing_now()
            if _monitoring_ready():
                if now_mono >= next_history:
                    started = now_mono
                    result = scheduler.run_job("trickle_fill_history")
                    _handle_result("trickle_fill_history", result)
                    finished = time.monotonic()
                    if result.deferred_until:
                        next_history = max(
                            finished + 30,
                            finished + max(0.0, result.deferred_until - scheduler.now_wall()),
                        )
                    else:
                        next_history = scheduler.next_job_run(
                            started,
                            finished,
                            scheduler.HISTORY_TRICKLE_INTERVAL,
                        )
                if _probe_due(now_wall):
                    result = scheduler.run_job("probe_gaps")
                    _handle_result("probe_gaps", result)
                    if result.succeeded or result.error_kind in {"rate_limited", "cookie_expired"}:
                        _set_state(PROBE_STATE_KEY, now_wall.date().isoformat())
            time.sleep(max(1, min(30, PROBE_CHECK_INTERVAL)))


if __name__ == "__main__":
    raise SystemExit(main())
