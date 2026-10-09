"""Simple append-only crawler metrics ledger.

The production posts database remains the source of current queue state. This
module writes a separate CSV ledger so historical counts and provenance do not
become another SQLite runtime schema. Reports are derived from this ledger and
can be rebuilt or removed.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from crawler.lock import database_write_lock
from crawler.task_routing import TASK_HISTORY_DETAIL, TASK_ID_FOLLOWUP


CHINA_TZ = timezone(timedelta(hours=8))
METRICS_SCHEMA_VERSION = "crawler-metrics-csv-v1"

HISTORY_COLUMNS = (
    "record_id",
    "observed_at",
    "window_start",
    "window_end",
    "metric_group",
    "metric_name",
    "task_type",
    "status",
    "value",
    "unit",
    "method",
    "quality",
    "source_id",
    "source_run_id",
    "notes",
    "extra_json",
)

SOURCE_COLUMNS = (
    "source_id",
    "source_file",
    "source_kind",
    "report_title",
    "window_start",
    "window_end",
    "queried_at",
    "method",
    "quality",
    "imported_records",
    "notes",
    "raw_content",
)


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _now_iso() -> str:
    return datetime.now(CHINA_TZ).isoformat()


def format_crawler_pool_snapshot_log(
    snapshot: dict[str, Any],
    *,
    source: str,
    state: str = "",
    job: str = "",
    lane: str = "",
) -> str:
    """Format the exact local queue snapshot for operational logs."""

    fields = [
        "[crawler-metrics]",
        f"sample={snapshot.get('sample_kind') or 'snapshot'}",
        f"source={source or 'snapshot'}",
        f"id_ledger_total={_safe_int(snapshot.get('id_ledger_total'))}",
        f"id_pending={_safe_int(snapshot.get('id_pending'))}",
        f"history_pending={_safe_int(snapshot.get('history_pending'))}",
        f"queue_pending_total={_safe_int(snapshot.get('queue_pending_total'))}",
        f"list2_observation_total={_safe_int(snapshot.get('list2_observation_total'))}",
    ]
    if state:
        fields.append(f"state={state}")
    if job:
        fields.append(f"job={job}")
    if lane:
        fields.append(f"lane={lane}")
    return " ".join(fields)


def metrics_path_for_db(
    db_path: str | Path | None = None,
    *,
    metrics_path: str | Path | None = None,
) -> Path:
    """Resolve the append-only CSV path without embedding it in SQLite."""

    explicit = metrics_path or os.environ.get("CRAWLER_METRICS_PATH", "")
    if explicit:
        return Path(explicit)
    if db_path:
        return Path(db_path).resolve().parent / "crawler_metrics" / "crawler_history.csv"
    return Path("metrics") / "crawler_history.csv"


def sources_path_for_metrics(metrics_path: str | Path) -> Path:
    path = Path(metrics_path)
    return path.with_name("crawler_sources.csv")


def _lock_path_for(path: Path) -> Path:
    return Path(str(path) + ".lock")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "select 1 from sqlite_master where type='table' and name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"pragma table_info({table})")}


def current_crawler_pool_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return exact current queue/ledger counts from one SQLite connection."""

    queue_status: dict[str, dict[str, int]] = {}
    queue_columns = _columns(conn, "crawler_queue")
    if queue_columns:
        if "task_type" in queue_columns:
            rows = conn.execute(
                """
                select coalesce(nullif(task_type, ''), 'unknown') as task_type,
                       coalesce(nullif(status, ''), 'unknown') as status,
                       count(*) as n
                from crawler_queue
                group by task_type, status
                order by task_type, status
                """
            )
        else:
            rows = conn.execute(
                """
                select 'unknown' as task_type,
                       coalesce(nullif(status, ''), 'unknown') as status,
                       count(*) as n
                from crawler_queue
                group by status
                order by status
                """
            )
        for row in rows:
            task_type = str(row[0])
            status = str(row[1])
            queue_status.setdefault(task_type, {})[status] = _safe_int(row[2])

    def queue_count(*, status: str | None = None, task_type: str | None = None) -> int:
        if not queue_columns:
            return 0
        clauses: list[str] = []
        params: list[str] = []
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if task_type is not None:
            if "task_type" not in queue_columns:
                return 0
            clauses.append("task_type=?")
            params.append(task_type)
        where = f" where {' and '.join(clauses)}" if clauses else ""
        return _safe_int(
            conn.execute(f"select count(*) from crawler_queue{where}", params).fetchone()[0]
        )

    def table_count(table: str) -> int:
        if not _table_exists(conn, table):
            return 0
        return _safe_int(conn.execute(f"select count(*) from {table}").fetchone()[0])

    return {
        "id_ledger_total": table_count("post_id_ledger"),
        "list2_observation_total": table_count("list2_observation_log"),
        "queue_pending_total": queue_count(status="pending"),
        "id_pending": queue_count(status="pending", task_type=TASK_ID_FOLLOWUP),
        "history_pending": queue_count(status="pending", task_type=TASK_HISTORY_DETAIL),
        "queue_status": queue_status,
    }


def _row(
    *,
    observed_at: str,
    metric_group: str,
    metric_name: str,
    value: object,
    source_id: str,
    task_type: str = "",
    status: str = "",
    unit: str = "count",
    method: str = "exact",
    quality: str = "exact",
    window_start: str = "",
    window_end: str = "",
    source_run_id: int | str | None = None,
    notes: str = "",
    extra: object | None = None,
) -> dict[str, str]:
    return {
        "record_id": uuid.uuid4().hex,
        "observed_at": str(observed_at or ""),
        "window_start": str(window_start or ""),
        "window_end": str(window_end or ""),
        "metric_group": str(metric_group or ""),
        "metric_name": str(metric_name or ""),
        "task_type": str(task_type or ""),
        "status": str(status or ""),
        "value": "" if value is None else str(value),
        "unit": str(unit or ""),
        "method": str(method or ""),
        "quality": str(quality or ""),
        "source_id": str(source_id or ""),
        "source_run_id": "" if source_run_id is None else str(source_run_id),
        "notes": str(notes or ""),
        "extra_json": (
            ""
            if extra is None
            else json.dumps(extra, ensure_ascii=False, sort_keys=True, default=str)
        ),
    }


def append_rows(
    path: str | Path,
    rows: Iterable[dict[str, object]],
    *,
    columns: tuple[str, ...] = HISTORY_COLUMNS,
    timeout: int = 60,
) -> int:
    """Append rows under a small file lease and create the header once."""

    path = Path(path)
    materialized = [
        {column: "" if row.get(column) is None else row.get(column, "") for column in columns}
        for row in rows
    ]
    if not materialized:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path_for(path)
    with database_write_lock(path, timeout, lock_path=lock_path):
        needs_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            if needs_header:
                writer.writeheader()
            writer.writerows(materialized)
    return len(materialized)


def read_rows(path: str | Path) -> list[dict[str, str]]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _snapshot_rows(
    metrics: dict[str, Any],
    *,
    observed_at: str,
    source_id: str,
    source_run_id: int | str | None,
    source_command: str,
    source_lane_id: str,
    sample_kind: str,
    method: str,
    stats: dict[str, Any],
    window_start: str = "",
    window_end: str = "",
) -> list[dict[str, str]]:
    notes = f"sample_kind={sample_kind};source_command={source_command}"
    rows = [
        _row(
            observed_at=observed_at,
            metric_group="queue",
            metric_name="id_pending",
            task_type=TASK_ID_FOLLOWUP,
            status="pending",
            value=metrics["id_pending"],
            source_id=source_id,
            source_run_id=source_run_id,
            method=method,
            window_start=window_start,
            window_end=window_end,
            notes=notes,
        ),
        _row(
            observed_at=observed_at,
            metric_group="queue",
            metric_name="history_pending",
            task_type=TASK_HISTORY_DETAIL,
            status="pending",
            value=metrics["history_pending"],
            source_id=source_id,
            source_run_id=source_run_id,
            method=method,
            window_start=window_start,
            window_end=window_end,
            notes=notes,
        ),
        _row(
            observed_at=observed_at,
            metric_group="queue",
            metric_name="queue_pending_total",
            value=metrics["queue_pending_total"],
            source_id=source_id,
            source_run_id=source_run_id,
            method=method,
            window_start=window_start,
            window_end=window_end,
            notes=notes,
        ),
        _row(
            observed_at=observed_at,
            metric_group="ledger",
            metric_name="id_ledger_total",
            value=metrics["id_ledger_total"],
            source_id=source_id,
            source_run_id=source_run_id,
            method=method,
            window_start=window_start,
            window_end=window_end,
            notes=notes,
        ),
        _row(
            observed_at=observed_at,
            metric_group="ledger",
            metric_name="list2_observation_total",
            value=metrics["list2_observation_total"],
            source_id=source_id,
            source_run_id=source_run_id,
            method=method,
            window_start=window_start,
            window_end=window_end,
            notes=notes,
        ),
    ]
    for task_type, statuses in sorted(metrics["queue_status"].items()):
        for status, value in sorted(statuses.items()):
            rows.append(
                _row(
                    observed_at=observed_at,
                    metric_group="queue",
                    metric_name="queue_count",
                    task_type=task_type,
                    status=status,
                    value=value,
                    source_id=source_id,
                    source_run_id=source_run_id,
                    method=method,
                    window_start=window_start,
                    window_end=window_end,
                    notes=notes,
                )
            )

    run_fields = (
        "source_calls",
        "selected",
        "written",
        "queue_inserted",
        "queue_reopened",
        "queue_delta_total",
        "comment_row_delta",
        "rate_limited",
    )
    for field in run_fields:
        if field not in stats:
            continue
        rows.append(
            _row(
                observed_at=observed_at,
                metric_group="run",
                metric_name=field,
                value=stats.get(field),
                unit="boolean" if field == "rate_limited" else "count",
                source_id=source_id,
                source_run_id=source_run_id,
                method=method,
                window_start=window_start,
                window_end=window_end,
                notes=notes,
                extra={"lane_id": source_lane_id} if source_lane_id else None,
            )
        )
    for field, value in sorted(stats.items()):
        if field in run_fields or not isinstance(value, (bool, int, float)):
            continue
        rows.append(
            _row(
                observed_at=observed_at,
                metric_group="run_meta",
                metric_name=str(field),
                value=value,
                unit="boolean" if isinstance(value, bool) else "count",
                source_id=source_id,
                source_run_id=source_run_id,
                method=method,
                window_start=window_start,
                window_end=window_end,
                notes=notes,
            )
        )
    if stats:
        rows.append(
            _row(
                observed_at=observed_at,
                metric_group="run",
                metric_name="raw_stats",
                value="",
                unit="json",
                source_id=source_id,
                source_run_id=source_run_id,
                method=method,
                window_start=window_start,
                window_end=window_end,
                notes=notes,
                extra=stats,
            )
        )
    return rows


def record_crawler_pool_snapshot(
    conn: sqlite3.Connection,
    *,
    metrics_path: str | Path | None = None,
    source_run_id: int | str | None = None,
    source_command: str = "",
    source_lane_id: str = "",
    sample_kind: str = "run",
    method: str = "exact",
    stats: dict[str, Any] | None = None,
    sampled_at: str | None = None,
    window_start: str = "",
    window_end: str = "",
) -> dict[str, Any]:
    """Append an exact queue snapshot to the standalone CSV ledger."""

    stats = dict(stats or {})
    metrics = current_crawler_pool_metrics(conn)
    sampled_at = str(sampled_at or _now_iso())
    source_id = f"runtime:{source_command or 'snapshot'}:{source_run_id or sampled_at}"
    rows = _snapshot_rows(
        metrics,
        observed_at=sampled_at,
        source_id=source_id,
        source_run_id=source_run_id,
        source_command=source_command,
        source_lane_id=source_lane_id or str(stats.get("lane_id") or ""),
        sample_kind=sample_kind,
        method=method,
        stats=stats,
        window_start=window_start,
        window_end=window_end,
    )
    path = metrics_path_for_db(metrics_path=metrics_path)
    append_rows(path, rows)
    return {
        "sampled_at": sampled_at,
        "sample_kind": sample_kind,
        "method": method,
        "source_id": source_id,
        "source_run_id": source_run_id,
        "metrics_path": str(path),
        **metrics,
    }
