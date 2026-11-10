"""Known IDs that never reached a queue, or lost an unattempted refresh."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def find_crawler_omissions(conn, *, post_ids=None, completed_before=None):
    clauses = ["l.needs_detail=1",
               "(q.post_id IS NULL OR (q.status='done' AND l.source_comment_count>coalesce(q.last_attempt_list_comment_count,-1)))",
               "(p.id IS NULL OR p.crawl_status!='full')"]
    params = []
    if post_ids is not None:
        ids = list(dict.fromkeys(str(value) for value in post_ids))
        if not ids:
            return []
        clauses.append("l.post_id IN (" + ",".join("?" for _ in ids) + ")")
        params.extend(ids)
    if completed_before is not None:
        clauses.append("l.last_list_seen_at<=?")
        params.append(completed_before)
    rows = conn.execute("""
        SELECT l.post_id,l.first_seen_at,l.first_seen_source,l.source_create_time,l.source_update_time,
               l.source_comment_count,l.seen_via,p.crawl_status,
               q.status queue_status,q.last_attempt_list_comment_count,
               (SELECT count(*) FROM comments c WHERE c.post_id=l.post_id) actual_comments
        FROM post_id_ledger l
        LEFT JOIN crawler_queue q ON q.post_id=l.post_id
        LEFT JOIN posts p ON p.id=l.post_id
        WHERE """ + " AND ".join(clauses), params)
    result = []
    for raw in rows:
        row = dict(raw)
        if row["queue_status"] is None:
            row["kind"] = "missing_post" if row["crawl_status"] is None else "missing_detail"
        else:
            attempted = row["last_attempt_list_comment_count"]
            count = int(row["source_comment_count"] or 0)
            # An already-attempted partial response is not a new lost refresh.
            if count <= row["actual_comments"] or (attempted is not None and count <= attempted):
                continue
            row["kind"] = "missing_refresh"
        result.append(row)
    return result


def crawler_omission_snapshot(conn):
    """Count omissions after the last completed list run; exclude live writes."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"post_id_ledger", "crawler_queue", "posts", "comments", "crawler_run_history"}.issubset(tables):
        return {"available": False}
    completed = conn.execute("""SELECT max(finished_at) FROM crawler_run_history
        WHERE command IN ('discover-latest','discover-active') AND errors=0""").fetchone()[0]
    if not completed:
        return {"available": False}
    rows = find_crawler_omissions(conn, completed_before=completed)
    counts = {key: sum(r["kind"] == key for r in rows)
              for key in ("missing_post", "missing_detail", "missing_refresh")}
    return {"available": True, "checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
            "completed_list_at": completed, **counts, "total": len(rows)}


def requeue_crawler_omissions(store, post_ids):
    """Recheck explicit IDs under the caller's existing database write lock."""
    results = []
    for row in find_crawler_omissions(store.conn, post_ids=post_ids):
        refresh = row["kind"] == "missing_refresh"
        action = store.enqueue_crawler_candidate(
            post_id=row["post_id"], source=row["first_seen_source"] or "local_audit",
            priority=0 if refresh else 60,
            list_create_time=row["source_create_time"], list_update_time=row["source_update_time"],
            list_comment_count=row["source_comment_count"], db_comment_count=row["actual_comments"],
            reason="comment_changed" if refresh else "observed_missing",
            task_type="id_followup", commit=False,
        )
        results.append({**row, "action": action})
    store.conn.commit()
    return results
