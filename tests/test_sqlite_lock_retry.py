from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from storage.post_writer import SQLitePostStore


class SQLiteLockRetryTest(unittest.TestCase):
    def test_set_state_retries_after_short_external_writer_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "posts.db"
            with SQLitePostStore(db_path) as store:
                store.init_schema()
                store.conn.execute("pragma busy_timeout=5")

                lock_started = threading.Event()
                release_lock = threading.Event()

                def hold_write_lock():
                    conn = sqlite3.connect(db_path, timeout=1)
                    try:
                        conn.execute("begin immediate")
                        lock_started.set()
                        release_lock.wait(timeout=2)
                        conn.rollback()
                    finally:
                        conn.close()

                holder = threading.Thread(target=hold_write_lock, daemon=True)
                holder.start()
                self.assertTrue(lock_started.wait(timeout=1))
                release = threading.Timer(0.05, release_lock.set)
                release.start()
                try:
                    store.set_state("lock-retry", "written")
                finally:
                    release.cancel()
                    release_lock.set()
                    holder.join(timeout=1)

                row = store.conn.execute(
                    "select value from crawl_state where key='lock-retry'"
                ).fetchone()

            self.assertEqual(row["value"], "written")


if __name__ == "__main__":
    unittest.main()
