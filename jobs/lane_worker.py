"""Dedicated worker for the old cookie lane.

The main scheduler owns the new-cookie monitoring lane.  When parallel lane
mode is enabled, this process owns whichever detail task is assigned to the
old lane. Queue claims remain in the shared SQLite database; this worker never
creates a second queue.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

from crawler.lock import database_write_lock
from jobs import scheduler


LANE_ID = os.environ.get("CRAWLER_LANE_WORKER_MODE", "").strip().lower()
WORKER_LOCK_TIMEOUT = scheduler.env_int("CRAWLER_LANE_WORKER_LOCK_TIMEOUT", 3600)
WORKER_CHECK_INTERVAL = scheduler.env_int("CRAWLER_LANE_WORKER_CHECK_INTERVAL", 30)
STARTUP_GRACE_SECONDS = scheduler.env_int("CRAWLER_LANE_STARTUP_GRACE", 90)


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


def _worker_job() -> str:
    """Select the old lane's configured detail queue without guessing.

    The history lane remains supported for deployments that are still draining
    it. Once history is finished, assigning ``id_followup`` to the same lane
    automatically moves this worker onto the shared ID table.
    """

    lane = next(
        (spec for spec in scheduler.cookie_pool_specs() if spec.lane_id == LANE_ID),
        None,
    )
    if lane is None:
        raise SystemExit(f"cookie pool has no configured lane: {LANE_ID!r}")
    if lane.supports_task(scheduler.TASK_ID_FOLLOWUP):
        return "trickle_fill"
    if lane.supports_task(scheduler.TASK_HISTORY_DETAIL):
        return "trickle_fill_history"
    raise SystemExit(f"cookie lane {LANE_ID!r} has no detail task assignment")


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

    job = _worker_job()
    interval = (
        scheduler.detail_trickle_interval()
        if job == "trickle_fill"
        else scheduler.HISTORY_TRICKLE_INTERVAL
    )
    print(
        f"[lane-worker] started lane={LANE_ID} job={job} interval={interval}s",
        flush=True,
    )
    with _single_worker_lock():
        started_at = time.monotonic()
        next_run = started_at + 3 * 60
        while True:
            now_mono = time.monotonic()
            if now_mono - started_at >= STARTUP_GRACE_SECONDS and _monitoring_ready():
                if now_mono >= next_run:
                    started = now_mono
                    result = scheduler.run_job(job)
                    _handle_result(job, result)
                    finished = time.monotonic()
                    if result.deferred_until:
                        next_run = max(
                            finished + 30,
                            finished + max(0.0, result.deferred_until - scheduler.now_wall()),
                        )
                    elif job == "trickle_fill" and result.succeeded:
                        next_run = scheduler.next_detail_trickle_run(finished)
                    else:
                        next_run = scheduler.next_job_run(
                            started,
                            finished,
                            interval,
                        )
            time.sleep(max(1, min(30, WORKER_CHECK_INTERVAL)))


if __name__ == "__main__":
    raise SystemExit(main())
