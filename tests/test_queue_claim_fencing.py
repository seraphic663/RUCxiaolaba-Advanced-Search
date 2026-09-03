from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from storage.post_writer import SQLitePostStore
from storage.queue_repository import QueueClaim


def enqueue(store: SQLitePostStore, post_id: str) -> None:
    store.enqueue_crawler_candidate(
        post_id=post_id,
        source="test",
        priority=10,
        list_create_time="2026-09-01 00:00:00",
        list_update_time="2026-09-01 00:00:00",
        list_comment_count=1,
        db_comment_count=0,
        reason="test",
        commit=False,
    )


def read_queue(db_path: Path, post_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "select status, claim_owner, claim_token from crawler_queue where post_id=?",
            (post_id,),
        ).fetchone()
    finally:
        conn.close()


def test_stale_worker_cannot_mark_reclaimed_queue_item():
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "posts.db"
        with SQLitePostStore(db_path) as store:
            store.init_schema()
            enqueue(store, "1")
            store.conn.commit()

            assert store.claim_crawler_queue_item(
                "1", owner="worker-a", token="token-a", commit=False
            )
            assert store.recover_expired_crawler_queue_claims(
                now="9999-01-01 00:00:00", commit=False
            ) == 1
            assert store.claim_crawler_queue_item(
                "1", owner="worker-b", token="token-b", commit=False
            )

            stale = store.mark_crawler_queue_item(
                "1",
                status="done",
                last_error="stale-worker",
                claim=QueueClaim("worker-a", "token-a"),
                commit=False,
            )
            assert stale is False
            current = store.mark_crawler_queue_item(
                "1",
                status="done",
                claim=QueueClaim("worker-b", "token-b"),
                commit=False,
            )
            assert current is True
            store.conn.commit()

        row = read_queue(db_path, "1")
        assert dict(row) == {
            "status": "done",
            "claim_owner": "",
            "claim_token": "",
        }


def test_stale_worker_cannot_finish_reclaimed_queue_item():
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "posts.db"
        with SQLitePostStore(db_path) as store:
            store.init_schema()
            enqueue(store, "2")
            store.conn.commit()
            assert store.claim_crawler_queue_item(
                "2", owner="worker-b", token="token-b", commit=False
            )

            stale = store.finish_crawler_queue_detail(
                "2",
                detail_comment_count=1,
                retry_delay_seconds=60,
                max_same_observation_attempts=2,
                claim=QueueClaim("worker-a", "token-a"),
                commit=False,
            )
            assert stale == "stale_claim"
            row = store.conn.execute(
                "select status, claim_owner, claim_token from crawler_queue where post_id='2'"
            ).fetchone()
            assert (row["status"], row["claim_owner"], row["claim_token"]) == (
                "in_progress",
                "worker-b",
                "token-b",
            )
