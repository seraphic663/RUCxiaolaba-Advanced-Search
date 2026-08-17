from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crawler.task_routing import TASK_HISTORY_DETAIL, TASK_ID_FOLLOWUP
from storage.post_writer import SQLitePostStore


def add_ledger_row(store: SQLitePostStore, post_id: str) -> None:
    store.conn.execute(
        """
        insert into post_id_ledger(
            post_id, first_seen_at, first_seen_source,
            last_list_seen_at, updated_at
        ) values (?, ?, ?, ?, ?)
        """,
        (
            str(post_id),
            "2026-08-17T00:00:00+08:00",
            "lists",
            "2026-08-17T00:00:00+08:00",
            "2026-08-17T00:00:00+08:00",
        ),
    )


def enqueue(store: SQLitePostStore, post_id: str, task_type: str) -> None:
    store.enqueue_crawler_candidate(
        post_id=post_id,
        source="history",
        priority=10,
        list_create_time="2026-08-16 00:00:00",
        list_update_time="2026-08-16 00:00:00",
        list_comment_count=0,
        db_comment_count=None,
        reason="history_candidate",
        task_type=task_type,
    )


class QueueRoutePrecedenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "posts.db"
        with SQLitePostStore(self.db) as store:
            store.init_schema()

    def tearDown(self):
        self.temp.cleanup()

    def test_id_ledger_wins_when_history_enqueues_duplicate(self):
        with SQLitePostStore(self.db) as store:
            add_ledger_row(store, "100")
            store.conn.commit()

            enqueue(store, "100", TASK_HISTORY_DETAIL)
            row = store.conn.execute(
                "select task_type from crawler_queue where post_id='100'"
            ).fetchone()

        self.assertEqual(row[0], TASK_ID_FOLLOWUP)

    def test_existing_id_route_cannot_be_downgraded_by_history(self):
        with SQLitePostStore(self.db) as store:
            enqueue(store, "101", TASK_ID_FOLLOWUP)
            enqueue(store, "101", TASK_HISTORY_DETAIL)
            row = store.conn.execute(
                "select task_type from crawler_queue where post_id='101'"
            ).fetchone()

        self.assertEqual(row[0], TASK_ID_FOLLOWUP)

    def test_legacy_history_duplicate_is_migrated_and_filtered(self):
        with SQLitePostStore(self.db) as store:
            enqueue(store, "102", TASK_HISTORY_DETAIL)
            add_ledger_row(store, "102")
            store.conn.commit()

            self.assertEqual(store.migrate_history_duplicates_to_id_followup(), 1)
            row = store.conn.execute(
                "select task_type from crawler_queue where post_id='102'"
            ).fetchone()
            self.assertEqual(row[0], TASK_ID_FOLLOWUP)

            enqueue(store, "103", TASK_HISTORY_DETAIL)
            add_ledger_row(store, "103")
            store.conn.commit()
            history_rows = store.next_crawler_queue_items(
                10,
                task_type=TASK_HISTORY_DETAIL,
            )

        self.assertEqual(history_rows, [])

    def test_runtime_schema_migrates_active_duplicate_on_reopen(self):
        with SQLitePostStore(self.db) as store:
            enqueue(store, "104", TASK_HISTORY_DETAIL)
            add_ledger_row(store, "104")
            store.conn.commit()

        with SQLitePostStore(self.db) as store:
            store.ensure_runtime_schema()
            row = store.conn.execute(
                "select task_type from crawler_queue where post_id='104'"
            ).fetchone()

        self.assertEqual(row[0], TASK_ID_FOLLOWUP)


if __name__ == "__main__":
    unittest.main()
