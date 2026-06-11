"""Incremental bigram sidecar maintenance tests."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from storage.post_writer import SQLitePostStore, bigram_tokens


def create_sidecar(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table search_rows(
            row_id integer primary key,
            post_id text not null,
            kind text not null
        );
        create index idx_search_rows_post_id on search_rows(post_id);
        create table index_meta(key text primary key, value text not null);
        insert into index_meta values ('schema_version', 'bigram-v1');
        create virtual table search_bigram using fts5(
            tokens,
            content='',
            contentless_delete=1,
            tokenize='unicode61'
        );
        """
    )
    conn.close()


class BigramStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.posts_db = root / "posts.db"
        self.bigram_db = root / "bigram.db"
        create_sidecar(self.bigram_db)
        with SQLitePostStore(self.posts_db) as store:
            store.init_schema()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def count(self, query: str, kind: str | None = None) -> int:
        conn = sqlite3.connect(self.bigram_db)
        sql = (
            "select count(*) from search_bigram f "
            "join search_rows r on r.row_id=f.rowid "
            "where f.tokens match ?"
        )
        args = ['"' + bigram_tokens(query) + '"']
        if kind:
            sql += " and r.kind=?"
            args.append(kind)
        result = conn.execute(sql, args).fetchone()[0]
        conn.close()
        return result

    def test_upsert_replaces_post_and_comment_terms(self) -> None:
        with SQLitePostStore(self.posts_db, self.bigram_db) as store:
            store.upsert_post(
                {"id": "1", "content": "食堂今天开门", "create_time": "2026-06-09"},
                [{"id": "c1", "detail": "食堂座位很多"}],
                commit=False,
            )
            store.conn.commit()

        self.assertEqual(self.count("食堂", "post"), 1)
        self.assertEqual(self.count("座位", "comment"), 1)

        with SQLitePostStore(self.posts_db, self.bigram_db) as store:
            store.upsert_post(
                {"id": "1", "content": "图书馆今天开放", "create_time": "2026-06-09"},
                [{"id": "c2", "detail": "图书馆座位充足"}],
                commit=False,
            )
            store.conn.commit()

        self.assertEqual(self.count("食堂"), 0)
        self.assertEqual(self.count("图书馆", "post"), 1)
        self.assertEqual(self.count("图书馆", "comment"), 1)
        conn = sqlite3.connect(self.bigram_db)
        self.assertEqual(
            conn.execute("select count(*) from search_rows where post_id='1'").fetchone()[0],
            2,
        )
        conn.close()


if __name__ == "__main__":
    unittest.main()
