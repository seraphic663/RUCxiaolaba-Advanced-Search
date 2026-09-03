"""Queue claim and terminal-state operations for the SQLite store facade."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _later_text(seconds: int) -> str:
    return (datetime.now() + timedelta(seconds=max(0, int(seconds)))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class QueueClaim:
    """The fencing identity a worker must present when finishing a claim."""

    owner: str
    token: str
    lane_id: str = ""


class CrawlerQueueRepository:
    """Own claim fencing and queue terminal transitions behind the store facade."""

    def __init__(self, store):
        self.store = store

    @property
    def conn(self):
        return self.store.conn

    @staticmethod
    def _where_claim(post_id: str, claim: QueueClaim | None) -> tuple[str, tuple]:
        if claim is None:
            return "post_id=?", (str(post_id),)
        return (
            "post_id=? and status='in_progress' and claim_owner=? and claim_token=?",
            (str(post_id), claim.owner, claim.token),
        )

    def claim(
        self,
        post_id: str,
        *,
        owner: str,
        lane_id: str = "",
        claim_ttl_seconds: int = 30 * 60,
        token: str = "",
        commit: bool = True,
    ) -> QueueClaim | None:
        self.store.ensure_crawler_queue(commit=False)
        now = _now_text()
        claim_token = str(token or uuid.uuid4().hex)
        cursor = self.conn.execute(
            """
            update crawler_queue
            set status='in_progress',
                claim_owner=?, claim_lane_id=?, claim_token=?,
                claim_started_at=?, claim_until=?,
                last_lane_id=case when ?='' then last_lane_id else ? end,
                updated_at=?
            where post_id=?
              and status='pending'
              and (next_attempt_at='' or next_attempt_at <= ?)
            """,
            (
                str(owner),
                str(lane_id or ""),
                claim_token,
                now,
                _later_text(claim_ttl_seconds),
                str(lane_id or ""),
                str(lane_id or ""),
                now,
                str(post_id),
                now,
            ),
        )
        if commit:
            self.conn.commit()
        if not cursor.rowcount:
            return None
        return QueueClaim(str(owner), claim_token, str(lane_id or ""))

    def set_claim_lane(
        self,
        post_id: str,
        *,
        owner: str,
        lane_id: str,
        claim: QueueClaim | None = None,
        commit: bool = True,
    ) -> bool:
        self.store.ensure_crawler_queue(commit=False)
        if claim is not None:
            where = (
                "post_id=? and status='in_progress' "
                "and claim_owner=? and claim_token=?"
            )
            params = (str(post_id), claim.owner, claim.token)
        else:
            where = "post_id=? and status='in_progress' and claim_owner=?"
            params = (str(post_id), str(owner))
        cursor = self.conn.execute(
            f"""
            update crawler_queue
            set claim_lane_id=?, last_lane_id=?, updated_at=?
            where {where}
            """,
            (str(lane_id or ""), str(lane_id or ""), _now_text(), *params),
        )
        if commit:
            self.conn.commit()
        return bool(cursor.rowcount)

    def mark(
        self,
        post_id: str,
        *,
        status: str,
        last_error: str = "",
        increment_attempts: bool = True,
        record_observation: bool = False,
        next_attempt_at: str = "",
        claim: QueueClaim | None = None,
        commit: bool = True,
    ) -> bool:
        self.store.ensure_crawler_queue(commit=False)
        attempts_sql = "attempts + 1" if increment_attempts else "attempts"
        observation_sql = ""
        if record_observation:
            observation_sql = """
                , last_attempt_list_comment_count=list_comment_count
                , last_attempt_list_update_time=list_update_time
                , same_observation_attempts=same_observation_attempts + 1
            """
        where, params = self._where_claim(str(post_id), claim)
        cursor = self.conn.execute(
            f"""
            update crawler_queue
            set status=?, last_error=?, attempts={attempts_sql},
                next_attempt_at=?,
                claim_owner='', claim_lane_id='', claim_token='',
                claim_started_at='', claim_until='',
                updated_at=?
                {observation_sql}
            where {where}
            """,
            (status, last_error, next_attempt_at, _now_text(), *params),
        )
        if commit:
            self.conn.commit()
        return bool(cursor.rowcount)

    def _observation_attempt(self, post_id: str, claim: QueueClaim | None):
        where, params = self._where_claim(str(post_id), claim)
        row = self.conn.execute(
            f"""
            select list_comment_count, list_update_time,
                   last_attempt_list_comment_count,
                   last_attempt_list_update_time,
                   same_observation_attempts
            from crawler_queue where {where}
            """,
            params,
        ).fetchone()
        if row is None:
            return None, 0
        same_observation = (
            row["last_attempt_list_comment_count"] is not None
            and _safe_int(row["last_attempt_list_comment_count"])
            == _safe_int(row["list_comment_count"])
            and str(row["last_attempt_list_update_time"] or "")
            == str(row["list_update_time"] or "")
        )
        attempts = (
            _safe_int(row["same_observation_attempts"]) + 1
            if same_observation
            else 1
        )
        return row, attempts

    def finish_detail(
        self,
        post_id: str,
        *,
        detail_comment_count: int,
        retry_delay_seconds: int,
        max_same_observation_attempts: int,
        accept_detail_count: bool = False,
        claim: QueueClaim | None = None,
        commit: bool = True,
    ) -> str:
        """Record a detail result only while the supplied claim is current."""
        self.store.ensure_crawler_queue(commit=False)
        row, observation_attempts = self._observation_attempt(post_id, claim)
        if row is None:
            return "stale_claim" if claim is not None else "missing"
        list_count = _safe_int(row["list_comment_count"])
        detail_count = _safe_int(detail_comment_count)
        if accept_detail_count:
            list_count = detail_count
        if detail_count >= list_count:
            status = "done"
            next_attempt_at = ""
            last_error = ""
        elif observation_attempts < max(1, int(max_same_observation_attempts)):
            status = "pending"
            next_attempt_at = _later_text(retry_delay_seconds)
            last_error = (
                f"list_detail_comment_gap:list={list_count},detail={detail_count},"
                f"retry={observation_attempts}"
            )
        else:
            status = "deferred"
            next_attempt_at = ""
            last_error = (
                f"list_detail_comment_gap:list={list_count},detail={detail_count},"
                f"deferred={observation_attempts}"
            )
        where, params = self._where_claim(str(post_id), claim)
        cursor = self.conn.execute(
            f"""
            update crawler_queue
            set status=?, list_comment_count=?, db_comment_count=?, last_error=?,
                attempts=attempts + 1,
                last_attempt_list_comment_count=?,
                last_attempt_list_update_time=list_update_time,
                last_detail_comment_count=?,
                same_observation_attempts=?,
                next_attempt_at=?,
                claim_owner='', claim_lane_id='', claim_token='',
                claim_started_at='', claim_until='',
                updated_at=?
            where {where}
            """,
            (
                status,
                list_count,
                detail_count,
                last_error,
                list_count,
                detail_count,
                observation_attempts,
                next_attempt_at,
                _now_text(),
                *params,
            ),
        )
        if commit:
            self.conn.commit()
        return status if cursor.rowcount else "stale_claim"

    def defer_failure(
        self,
        post_id: str,
        *,
        last_error: str,
        retry_delay_seconds: int,
        max_same_observation_attempts: int,
        claim: QueueClaim | None = None,
        commit: bool = True,
    ) -> str:
        self.store.ensure_crawler_queue(commit=False)
        row, observation_attempts = self._observation_attempt(post_id, claim)
        if row is None:
            return "stale_claim" if claim is not None else "missing"
        terminal = observation_attempts >= max(1, int(max_same_observation_attempts))
        status = "failed" if terminal else "pending"
        next_attempt_at = "" if terminal else _later_text(retry_delay_seconds)
        where, params = self._where_claim(str(post_id), claim)
        cursor = self.conn.execute(
            f"""
            update crawler_queue
            set status=?, last_error=?, attempts=attempts + 1,
                last_attempt_list_comment_count=list_comment_count,
                last_attempt_list_update_time=list_update_time,
                same_observation_attempts=?, next_attempt_at=?,
                claim_owner='', claim_lane_id='', claim_token='',
                claim_started_at='', claim_until='',
                updated_at=?
            where {where}
            """,
            (
                status,
                last_error,
                observation_attempts,
                next_attempt_at,
                _now_text(),
                *params,
            ),
        )
        if commit:
            self.conn.commit()
        return status if cursor.rowcount else "stale_claim"
