"""AI feature backing store — independent SQLite DB (data/ai.db).

Tables:
  invite_codes  — SHA-256 hashes, never stores plaintext codes
  ai_sessions   — persistent sessions (survive deploy restarts)
  daily_usage   — atomic per-day per-code counters

This file MUST NOT import from server.py or crawler_db.py.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AI_DB = ROOT / "data" / "ai.db"
CHINA_TZ = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now(CHINA_TZ).strftime("%Y-%m-%d")


class AIStore:
    """Thread-safe store for invite codes, sessions, and daily quotas."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or DEFAULT_AI_DB)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def hash_code(plaintext: str) -> str:
        return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()

    def _connect(self, **kwargs) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), **kwargs)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma journal_mode=wal")
        conn.execute("pragma synchronous=normal")
        conn.execute("pragma foreign_keys=on")
        return conn

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------

    def init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    create table if not exists invite_codes (
                        code_hash    text primary key,
                        daily_quota  integer not null default 30,
                        max_quota    integer not null default 0,
                        used_total   integer not null default 0,
                        is_active    integer not null default 1,
                        note         text    not null default '',
                        created_at   text    not null
                    );

                    create table if not exists ai_sessions (
                        session_token text primary key,
                        code_hash     text    not null,
                        expires_at    text    not null,
                        created_at    text    not null,
                        foreign key (code_hash) references invite_codes(code_hash)
                    );

                    create table if not exists daily_usage (
                        code_hash   text not null,
                        usage_date  text not null,
                        query_count integer not null default 0,
                        primary key (code_hash, usage_date),
                        foreign key (code_hash) references invite_codes(code_hash)
                    );

                    create index if not exists idx_ai_sessions_token
                        on ai_sessions(session_token, expires_at);

                    create index if not exists idx_daily_usage_date
                        on daily_usage(usage_date);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # invite codes — generation (plaintext only returned once)
    # ------------------------------------------------------------------

    def generate_codes(
        self,
        count: int = 10,
        daily_quota: int = 30,
        max_quota: int = 0,
        note: str = "",
        prefix: str = "XLB",
    ) -> list[str]:
        """Generate *count* invite codes.  Returns the PLAINTEXT codes.
        Only SHA-256 hashes are persisted.  Caller MUST display codes to user.
        """
        now = _now()
        plaintexts: list[str] = []
        with self._lock:
            conn = self._connect()
            try:
                while len(plaintexts) < count:
                    token = secrets.token_hex(8).upper()
                    plain = (
                        f"{prefix}-{token[:4]}-{token[4:8]}-"
                        f"{token[8:12]}-{token[12:16]}"
                    )
                    code_hash = self.hash_code(plain)
                    cur = conn.execute(
                        """insert or ignore into invite_codes
                           (code_hash, daily_quota, max_quota, note, created_at)
                           values (?, ?, ?, ?, ?)""",
                        (code_hash, daily_quota, max_quota, note, now),
                    )
                    if cur.rowcount:
                        plaintexts.append(plain)
                conn.commit()
            finally:
                conn.close()
        return plaintexts

    # ------------------------------------------------------------------
    # activation / session
    # ------------------------------------------------------------------

    def activate(self, plaintext: str) -> tuple[bool, str]:
        """Verify a plaintext invite code and create a persistent session.

        Returns (ok, session_token_or_error).
        """
        code_hash = self.hash_code(plaintext)
        now = _now()
        expires = (datetime.now(CHINA_TZ) + timedelta(days=30)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        session_token = secrets.token_hex(32)

        conn = self._connect()
        try:
            row = conn.execute(
                "select code_hash, is_active from invite_codes where code_hash = ?",
                (code_hash,),
            ).fetchone()
            if not row:
                return False, "invite_code_invalid"
            if not row["is_active"]:
                return False, "invite_code_disabled"

            conn.execute(
                "insert into ai_sessions (session_token, code_hash, expires_at, created_at) values (?,?,?,?)",
                (session_token, code_hash, expires, now),
            )
            conn.commit()
            return True, session_token
        finally:
            conn.close()

    def validate_session(self, session_token: str | None) -> str | None:
        """Return code_hash if the session is valid and not expired, else None."""
        if not session_token:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                """select s.code_hash, s.expires_at
                   from ai_sessions s
                   join invite_codes c on c.code_hash = s.code_hash
                   where s.session_token = ? and c.is_active = 1""",
                (session_token,),
            ).fetchone()
            if not row:
                return None
            if row["expires_at"] < _now():
                return None
            return row["code_hash"]
        finally:
            conn.close()

    def remove_session(self, session_token: str) -> None:
        conn = self._connect()
        try:
            conn.execute("delete from ai_sessions where session_token = ?", (session_token,))
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # atomic quota
    # ------------------------------------------------------------------

    def reserve_quota(self, code_hash: str) -> tuple[bool, int | str]:
        """Atomically reserve one daily query slot.

        Returns:
          (True, used_count)   on success
          (False, "quota_exceeded")  when over daily limit
          (False, "invite_code_invalid")  when code is missing/disabled
        """
        today = _today()
        conn = self._connect()
        try:
            conn.execute("begin immediate")

            row = conn.execute(
                """select daily_quota, max_quota, used_total, is_active
                   from invite_codes where code_hash = ?""",
                (code_hash,),
            ).fetchone()
            if not row or not row["is_active"]:
                conn.rollback()
                return False, "invite_code_invalid"

            daily_quota = row["daily_quota"]
            max_quota = row["max_quota"]
            if max_quota > 0 and row["used_total"] >= max_quota:
                conn.rollback()
                return False, "quota_exceeded"

            conn.execute(
                """insert into daily_usage (code_hash, usage_date, query_count)
                   values (?, ?, 1)
                   on conflict(code_hash, usage_date)
                   do update set query_count = query_count + 1""",
                (code_hash, today),
            )

            used = conn.execute(
                "select query_count from daily_usage where code_hash = ? and usage_date = ?",
                (code_hash, today),
            ).fetchone()

            if used and used["query_count"] > daily_quota:
                conn.rollback()
                return False, "quota_exceeded"

            # Also bump total
            conn.execute(
                "update invite_codes set used_total = used_total + 1 where code_hash = ?",
                (code_hash,),
            )
            conn.commit()
            return True, used["query_count"] if used else 0
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def release_quota(self, code_hash: str) -> None:
        """Return one quota slot (call when AI call fails after reserve)."""
        today = _today()
        conn = self._connect()
        try:
            conn.execute("begin immediate")
            conn.execute(
                "update daily_usage set query_count = max(0, query_count - 1) where code_hash = ? and usage_date = ?",
                (code_hash, today),
            )
            conn.execute(
                "update invite_codes set used_total = max(0, used_total - 1) where code_hash = ?",
                (code_hash,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def get_status(self, code_hash: str) -> dict:
        """Return quota status for display."""
        today = _today()
        conn = self._connect()
        try:
            row = conn.execute(
                "select daily_quota, max_quota, used_total from invite_codes where code_hash = ?",
                (code_hash,),
            ).fetchone()
            if not row:
                return {"daily_quota": 0, "used_today": 0, "remaining": 0}
            daily_quota = row["daily_quota"]
            usage = conn.execute(
                "select query_count from daily_usage where code_hash = ? and usage_date = ?",
                (code_hash, today),
            ).fetchone()
            used = usage["query_count"] if usage else 0
            remaining = max(0, daily_quota - used)
            if row["max_quota"] > 0:
                remaining = min(remaining, max(0, row["max_quota"] - row["used_total"]))
            return {
                "daily_quota": daily_quota,
                "used_today": used,
                "remaining": remaining,
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # management
    # ------------------------------------------------------------------

    def list_codes(self) -> list[dict]:
        today = _today()
        conn = self._connect()
        try:
            rows = conn.execute(
                """select c.code_hash, c.daily_quota, c.max_quota, c.used_total,
                          c.is_active, c.note, c.created_at,
                          coalesce(u.query_count, 0) as used_today
                   from invite_codes c
                   left join daily_usage u on c.code_hash = u.code_hash and u.usage_date = ?
                   order by c.created_at desc""",
                (today,),
            ).fetchall()
            return [
                {
                    "hash_prefix": r["code_hash"][:16],
                    "daily_quota": r["daily_quota"],
                    "max_quota": r["max_quota"],
                    "used_total": r["used_total"],
                    "used_today": r["used_today"],
                    "is_active": bool(r["is_active"]),
                    "note": r["note"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def set_active(self, hash_prefix: str, active: bool) -> bool:
        conn = self._connect()
        try:
            code_hash = self._resolve_hash_prefix(conn, hash_prefix)
            if code_hash is None:
                return False
            cur = conn.execute(
                "update invite_codes set is_active = ? where code_hash = ?",
                (1 if active else 0, code_hash),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def set_quota(self, hash_prefix: str, daily_quota: int, max_quota: int | None = None) -> bool:
        conn = self._connect()
        try:
            code_hash = self._resolve_hash_prefix(conn, hash_prefix)
            if code_hash is None:
                return False
            if max_quota is not None:
                cur = conn.execute(
                    "update invite_codes set daily_quota = ?, max_quota = ? where code_hash = ?",
                    (daily_quota, max_quota, code_hash),
                )
            else:
                cur = conn.execute(
                    "update invite_codes set daily_quota = ? where code_hash = ?",
                    (daily_quota, code_hash),
                )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    @staticmethod
    def _resolve_hash_prefix(conn: sqlite3.Connection, hash_prefix: str) -> str | None:
        prefix = hash_prefix.strip().lower()
        if len(prefix) < 8:
            return None
        rows = conn.execute(
            "select code_hash from invite_codes where code_hash like ? limit 2",
            (f"{prefix}%",),
        ).fetchall()
        return rows[0]["code_hash"] if len(rows) == 1 else None

    def get_stats(self) -> dict:
        """Aggregate stats for admin."""
        conn = self._connect()
        try:
            total = conn.execute("select count(*) from invite_codes").fetchone()[0]
            active = conn.execute("select count(*) from invite_codes where is_active = 1").fetchone()[0]
            sessions = conn.execute("select count(*) from ai_sessions where expires_at > ?", (_now(),)).fetchone()[0]
            today = _today()
            today_queries = conn.execute(
                "select coalesce(sum(query_count), 0) from daily_usage where usage_date = ?",
                (today,),
            ).fetchone()[0]
            return {
                "total_codes": total,
                "active_codes": active,
                "active_sessions": sessions,
                "today_queries": today_queries,
            }
        finally:
            conn.close()

    def cleanup_expired_sessions(self) -> int:
        conn = self._connect()
        try:
            cur = conn.execute("delete from ai_sessions where expires_at < ?", (_now(),))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


# ------------------------------------------------------------------
# module-level convenience
# ------------------------------------------------------------------

_stores: dict[str, AIStore] = {}
_stores_lock = threading.Lock()


def get_store(db_path: str | Path | None = None) -> AIStore:
    path = Path(db_path or DEFAULT_AI_DB).resolve()
    key = str(path)
    with _stores_lock:
        store = _stores.get(key)
        if store is None:
            store = AIStore(path)
            store.init_schema()
            _stores[key] = store
        return store
