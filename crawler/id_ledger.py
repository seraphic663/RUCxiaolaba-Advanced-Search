"""Durable ID-ledger helpers shared by list discovery and detail filling.

The ledger is deliberately sidecar state: ``posts`` remains the content
store, ``crawler_queue`` remains the single de-duplicated work queue, and this
module only records list observations and the detail timeline.  It never
creates a source client or performs network I/O.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

CHINA_TZ = timezone(timedelta(hours=8))


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def now_iso() -> str:
    return datetime.now(CHINA_TZ).isoformat()


def event_key(article: dict) -> str:
    """Return a stable, compact key for one ``lists2`` observation."""

    values = (
        str(article.get("id") or ""),
        str(article.get("update_time") or ""),
        str(article.get("count_comment") or article.get("comment_count") or 0),
        str(article.get("refresh_time") or ""),
        str(article.get("content_time") or ""),
    )
    return "|".join(values)


def _newer(left: str, right: str) -> bool:
    left = str(left or "")
    right = str(right or "")
    return bool(left and (not right or left > right))


def _union_sources(old: str, source: str) -> str:
    values = {item for item in str(old or "").split(",") if item}
    if source:
        values.add(str(source))
    return ",".join(sorted(values))


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"pragma table_info({table})")}


def ensure_ledger_schema(conn: sqlite3.Connection) -> None:
    """Create or migrate the ledger without committing the caller's transaction."""

    conn.executescript(
        """
        create table if not exists post_id_ledger (
            post_id text primary key,
            first_seen_at text not null,
            first_seen_source text not null default '',
            first_seen_page integer not null default 0,
            first_seen_rank integer,
            discovery_order integer not null default 0,
            last_list_seen_at text not null,
            last_lists_seen_at text not null default '',
            last_lists2_seen_at text not null default '',
            source_create_time text not null default '',
            source_update_time text not null default '',
            source_comment_count integer not null default 0,
            source_star_count integer not null default 0,
            source_trace_count integer not null default 0,
            seen_via text not null default '',
            source_presence_status text not null default 'unknown',
            presence_checked_at text not null default '',
            not_found_streak integer not null default 0,
            last_lists2_event_key text not null default '',
            last_lists2_event_seen_at text not null default '',
            last_lists2_detail_attempt_at text not null default '',
            last_lists2_detail_result text not null default '',
            needs_detail integer not null default 0,
            last_detail_started_at text not null default '',
            last_detail_finished_at text not null default '',
            last_detail_success_at text not null default '',
            last_detail_source_update_time text not null default '',
            detail_comment_count integer not null default 0,
            detail_status text not null default 'never',
            next_detail_at text not null default '',
            last_detail_error text not null default '',
            bootstrap_run_id text not null default '',
            bootstrap_rank integer,
            updated_at text not null
        );

        create table if not exists list2_observation_log (
            observation_id integer primary key autoincrement,
            post_id text not null,
            event_key text not null,
            observed_at text not null,
            run_id text not null,
            page integer not null,
            source_update_time text not null default '',
            source_comment_count integer not null default 0,
            detail_attempted integer not null default 0,
            detail_result text not null default '',
            detail_at text not null default '',
            unique(post_id, event_key)
        );

        create table if not exists ledger_state (
            key text primary key,
            value text not null,
            updated_at text not null
        );
        """
    )

    # The table was introduced by an earlier local monitor draft.  Keep the
    # migration additive so an existing Railway Volume never needs a rebuild.
    ledger_columns = _columns(conn, "post_id_ledger")
    additions = {
        "first_seen_source": "alter table post_id_ledger add column first_seen_source text not null default ''",
        "first_seen_page": "alter table post_id_ledger add column first_seen_page integer not null default 0",
        "first_seen_rank": "alter table post_id_ledger add column first_seen_rank integer",
        "discovery_order": "alter table post_id_ledger add column discovery_order integer not null default 0",
    }
    for name, ddl in additions.items():
        if name not in ledger_columns:
            conn.execute(ddl)

    conn.executescript(
        """
        create index if not exists idx_post_id_ledger_detail
            on post_id_ledger(needs_detail, detail_status, next_detail_at);
        create index if not exists idx_post_id_ledger_lists2
            on post_id_ledger(last_lists2_seen_at, source_update_time);
        create index if not exists idx_list2_observation_run
            on list2_observation_log(run_id, page, observed_at);
        """
    )


def _row_value(row: sqlite3.Row | None, key: str, default: object = "") -> object:
    if row is None:
        return default
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def record_list_page(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    endpoint: str,
    page: int,
    articles: list[dict],
    baseline: bool = False,
    bootstrap: bool = False,
    observed_at: str = "",
) -> dict:
    """Persist one list page and return page-level stop signals.

    ``baseline=True`` is used only for the first lists2 pass after the ledger
    is created.  It records existing events but does not turn every historical
    event into a detail refresh.  Later passes are event-driven.
    """

    ensure_ledger_schema(conn)
    stats = {
        "rows": len(articles),
        "new_ids": 0,
        "new_events": 0,
        "actionable": 0,
        "actionable_ids": [],
        "stable": True,
        "source_create_time_min": "",
        "source_create_time_max": "",
    }
    current = str(observed_at or now_iso())
    for position, article in enumerate(articles):
        if not isinstance(article, dict):
            continue
        post_id = str(article.get("id") or "").strip()
        if not post_id:
            continue
        row = conn.execute(
            "select * from post_id_ledger where post_id=?",
            (post_id,),
        ).fetchone()
        was_known = row is not None
        discovery_order = safe_int(_row_value(row, "discovery_order", 0))
        if not was_known:
            discovery_order = safe_int(
                ledger_state(conn, "discovery_order", default="0")
            ) + 1
            set_ledger_state(conn, "discovery_order", str(discovery_order))
        old_update = str(_row_value(row, "source_update_time", "") or "")
        old_count = safe_int(_row_value(row, "source_comment_count", 0))
        old_status = str(_row_value(row, "detail_status", "never") or "never")
        create_time = str(article.get("create_time") or article.get("show_create_time") or "")
        source_update_time = str(article.get("update_time") or create_time)
        comment_count = safe_int(
            article.get("comment_count", article.get("count_comment", 0))
        )
        star_count = safe_int(article.get("count_star", article.get("star_count", 0)))
        trace_count = safe_int(article.get("count_trace", article.get("trace_count", 0)))
        source_changed = _newer(source_update_time, old_update) or comment_count != old_count
        event = event_key(article) if endpoint == "lists2" else ""
        event_new = False
        if event:
            cursor = conn.execute(
                """
                insert or ignore into list2_observation_log(
                    post_id,event_key,observed_at,run_id,page,
                    source_update_time,source_comment_count
                ) values (?,?,?,?,?,?,?)
                """,
                (
                    post_id,
                    event,
                    current,
                    str(run_id),
                    int(page),
                    source_update_time,
                    comment_count,
                ),
            )
            event_new = cursor.rowcount == 1
            if event_new:
                stats["new_events"] += 1

        seen_via = _union_sources(str(_row_value(row, "seen_via", "") or ""), endpoint)
        needs_detail = bool(safe_int(_row_value(row, "needs_detail", 0)))
        detail_status = old_status
        actionable = False
        if endpoint == "lists":
            if not was_known or detail_status in {"never", "failed", "partial", "running"}:
                needs_detail = True
                detail_status = "queued"
                actionable = True
            elif source_changed:
                needs_detail = True
                if detail_status in {"succeeded", "not_found", "blocked"}:
                    detail_status = "queued"
                actionable = True
        elif endpoint == "lists2" and event_new and not baseline:
            # A new event is worth one detail check even when a comment was
            # briefly added and deleted before the detail request runs.
            needs_detail = True
            detail_status = "queued"
            actionable = True

        if not was_known:
            stats["new_ids"] += 1
            stats["stable"] = False
        if source_changed:
            stats["stable"] = False
        if event_new and not baseline:
            stats["stable"] = False
        if actionable:
            stats["actionable"] += 1
            stats["actionable_ids"].append(post_id)

        if create_time:
            if not stats["source_create_time_min"] or create_time < stats["source_create_time_min"]:
                stats["source_create_time_min"] = create_time
            if not stats["source_create_time_max"] or create_time > stats["source_create_time_max"]:
                stats["source_create_time_max"] = create_time

        stored_update = source_update_time if _newer(source_update_time, old_update) else old_update
        values = {
            "last_list_seen_at": current,
            "last_lists_seen_at": current if endpoint == "lists" else str(_row_value(row, "last_lists_seen_at", "") or ""),
            "last_lists2_seen_at": current if endpoint == "lists2" else str(_row_value(row, "last_lists2_seen_at", "") or ""),
            "source_create_time": create_time or str(_row_value(row, "source_create_time", "") or ""),
            "source_update_time": stored_update,
            "source_comment_count": comment_count,
            "source_star_count": star_count,
            "source_trace_count": trace_count,
            "seen_via": seen_via,
            "source_presence_status": "observed_in_list",
            "presence_checked_at": current,
            "needs_detail": int(needs_detail),
            "detail_status": detail_status,
            "last_lists2_event_key": event or str(_row_value(row, "last_lists2_event_key", "") or ""),
            "last_lists2_event_seen_at": current if event else str(_row_value(row, "last_lists2_event_seen_at", "") or ""),
            "updated_at": current,
        }
        if row is None:
            conn.execute(
                """
                insert into post_id_ledger(
                    post_id,first_seen_at,first_seen_source,first_seen_page,
                    first_seen_rank,discovery_order,last_list_seen_at,last_lists_seen_at,
                    last_lists2_seen_at,source_create_time,source_update_time,
                    source_comment_count,source_star_count,source_trace_count,
                    seen_via,source_presence_status,presence_checked_at,
                    last_lists2_event_key,last_lists2_event_seen_at,needs_detail,
                    detail_status,bootstrap_run_id,bootstrap_rank,updated_at
                ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    post_id,
                    current,
                    endpoint,
                    int(page),
                    (int(page) - 1) * 20 + position,
                    discovery_order,
                    current,
                    values["last_lists_seen_at"],
                    values["last_lists2_seen_at"],
                    values["source_create_time"],
                    values["source_update_time"],
                    comment_count,
                    star_count,
                    trace_count,
                    seen_via,
                    "observed_in_list",
                    current,
                    values["last_lists2_event_key"],
                    values["last_lists2_event_seen_at"],
                    int(needs_detail),
                    detail_status,
                    str(run_id) if bootstrap else "",
                    (int(page) - 1) * 20 + position if bootstrap else None,
                    current,
                ),
            )
        else:
            assignments = ",".join(f"{key}=?" for key in values)
            params = [values[key] for key in values]
            if bootstrap:
                assignments += ",bootstrap_run_id=?,bootstrap_rank=?"
                params.extend([str(run_id), (int(page) - 1) * 20 + position])
            params.append(post_id)
            conn.execute(
                f"update post_id_ledger set {assignments} where post_id=?",
                params,
            )

    return stats


def _ensure_detail_row(conn: sqlite3.Connection, post_id: str) -> None:
    now = now_iso()
    conn.execute(
        """
        insert or ignore into post_id_ledger(
            post_id,first_seen_at,first_seen_source,last_list_seen_at,
            source_presence_status,updated_at
        ) values (?,?,?,?,?,?)
        """,
        (str(post_id), now, "queue", now, "unknown", now),
    )


def mark_detail_started(conn: sqlite3.Connection, post_id: str) -> None:
    ensure_ledger_schema(conn)
    _ensure_detail_row(conn, post_id)
    now = now_iso()
    conn.execute(
        """
        update post_id_ledger
        set last_detail_started_at=?, last_lists2_detail_attempt_at=?,
            detail_status='running', updated_at=?
        where post_id=?
        """,
        (now, now, now, str(post_id)),
    )


def mark_detail_finished(
    conn: sqlite3.Connection,
    post_id: str,
    *,
    status: str,
    comment_count: int = 0,
    source_update_time: str = "",
    error: str = "",
) -> None:
    """Record one detail outcome and attach it to the latest list2 event."""

    ensure_ledger_schema(conn)
    _ensure_detail_row(conn, post_id)
    now = now_iso()
    normalized = str(status or "failed")
    if normalized == "succeeded":
        conn.execute(
            """
            update post_id_ledger
            set source_presence_status='confirmed_present',
                presence_checked_at=?, not_found_streak=0, needs_detail=0,
                last_detail_finished_at=?, last_detail_success_at=?,
                last_detail_source_update_time=case when ?!='' then ? else source_update_time end,
                detail_comment_count=?, detail_status='succeeded',
                next_detail_at='', last_detail_error='', updated_at=?
            where post_id=?
            """,
            (now, now, now, source_update_time, source_update_time, safe_int(comment_count), now, str(post_id)),
        )
    elif normalized == "not_found":
        conn.execute(
            """
            update post_id_ledger
            set source_presence_status='not_found_candidate', presence_checked_at=?,
                not_found_streak=not_found_streak+1, needs_detail=0,
                last_detail_finished_at=?, detail_status='not_found',
                last_detail_error=?, updated_at=?
            where post_id=?
            """,
            (now, now, str(error or "not_found"), now, str(post_id)),
        )
    elif normalized == "blocked":
        conn.execute(
            """
            update post_id_ledger
            set needs_detail=1, detail_status='blocked', last_detail_finished_at=?,
                last_detail_error=?, updated_at=?
            where post_id=?
            """,
            (now, str(error), now, str(post_id)),
        )
    elif normalized == "partial":
        conn.execute(
            """
            update post_id_ledger
            set source_presence_status='confirmed_present', presence_checked_at=?,
                needs_detail=1, detail_comment_count=?, detail_status='partial',
                last_detail_finished_at=?, last_detail_error=?, updated_at=?
            where post_id=?
            """,
            (now, safe_int(comment_count), now, str(error), now, str(post_id)),
        )
    else:
        conn.execute(
            """
            update post_id_ledger
            set source_presence_status='transient_error', needs_detail=1,
                detail_status='failed', last_detail_finished_at=?,
                last_detail_error=?, updated_at=?
            where post_id=?
            """,
            (now, str(error), now, str(post_id)),
        )

    conn.execute(
        """
        update list2_observation_log
        set detail_attempted=1, detail_result=?, detail_at=?
        where observation_id=(
            select max(observation_id) from list2_observation_log where post_id=?
        )
        """,
        (normalized, now, str(post_id)),
    )


def ledger_state(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    ensure_ledger_schema(conn)
    row = conn.execute("select value from ledger_state where key=?", (str(key),)).fetchone()
    return str(row[0]) if row is not None else default


def set_ledger_state(conn: sqlite3.Connection, key: str, value: object) -> None:
    ensure_ledger_schema(conn)
    encoded = value if isinstance(value, str) else json.dumps(
        value,
        ensure_ascii=False,
        default=str,
    )
    conn.execute(
        """
        insert into ledger_state(key,value,updated_at) values (?,?,?)
        on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at
        """,
        (str(key), str(encoded), now_iso()),
    )
