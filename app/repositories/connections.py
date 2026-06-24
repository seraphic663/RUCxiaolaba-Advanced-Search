"""SQLite connection factories shared by read-side repositories."""

from __future__ import annotations

import sqlite3
from pathlib import Path


class ClosingSQLiteConnection(sqlite3.Connection):
    """Commit/rollback through the context manager and always close afterward."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect_readonly(
    posts_db: str | Path,
    bigram_db: str | Path | None = None,
    symbol_db: str | Path | None = None,
) -> sqlite3.Connection:
    conn = sqlite3.connect(str(posts_db), factory=ClosingSQLiteConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma query_only=on")
    conn.execute("pragma mmap_size=0")
    conn.execute("pragma cache_size=-2000")
    conn.execute("pragma temp_store=file")
    if bigram_db:
        conn.execute("attach database ? as bigram", (str(Path(bigram_db).resolve()),))
    if symbol_db:
        conn.execute("attach database ? as symbol", (str(Path(symbol_db).resolve()),))
    return conn
