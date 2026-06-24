"""Tests for the optional bigram search sidecar."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from storage.post_writer import bigram_tokens
from storage.symbol_index import build_symbol_index


class BigramSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.posts_db = root / "posts.db"
        self.bigram_db = root / "bigram.db"
        self.symbol_db = root / "symbol.db"

        conn = sqlite3.connect(self.posts_db)
        conn.executescript(
            """
            create table posts(
                id text primary key,
                content text,
                category_name text,
                user_name text,
                show_user_id text,
                real_user_id text,
                create_time text,
                comment_count integer,
                star_count integer,
                trace_count integer,
                views integer,
                hot integer
            );
            create table comments(
                post_id text,
                detail text,
                show_user_id text,
                real_user_id text,
                reply_show_user_id text,
                show_user_name text,
                reply_show_user_name text
            );
            """
        )
        conn.executemany(
            "insert into posts values (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("1", "食堂今天开门", "", "甲", "u1", "0", "2026-06-01", 0, 0, 0, 0, 0),
                ("2", "普通正文", "", "测试昵称", "u2", "real-2", "2026-06-02", 1, 0, 0, 0, 0),
                ("3", "只有单字猫", "", "丙", "u3", "0", "2026-06-03", 0, 0, 0, 0, 0),
                ("4", "♀找搭子", "", "丁", "u4", "0", "2026-06-04", 0, 0, 0, 0, 0),
            ],
        )
        conn.execute(
            "insert into comments values (?,?,?,?,?,?,?)",
            ("2", "食堂座位很多", "comment-id", "comment-real", "reply-id", "评论者", "回复昵称"),
        )
        conn.execute(
            "insert into comments values (?,?,?,?,?,?,?)",
            ("2", "评论里♀找室友", "comment-symbol", "0", "", "符号评论者", ""),
        )
        conn.commit()
        conn.close()
        build_symbol_index(self.posts_db, self.symbol_db)

        sidecar = sqlite3.connect(self.bigram_db)
        sidecar.executescript(
            """
            create table search_rows(
                row_id integer primary key,
                post_id text not null,
                kind text not null
            );
            create table index_meta(key text primary key, value text not null);
            insert into index_meta values ('schema_version', 'bigram-v1');
            create virtual table search_bigram using fts5(
                tokens, content='', contentless_delete=1, tokenize='unicode61'
            );
            """
        )
        rows = [
            (1, "1", "post", "食堂今天开门"),
            (2, "2", "post", "普通正文"),
            (3, "2", "comment", "食堂座位很多"),
            (4, "3", "post", "只有单字猫"),
        ]
        for row_id, post_id, kind, body in rows:
            sidecar.execute("insert into search_rows values (?,?,?)", (row_id, post_id, kind))
            sidecar.execute(
                "insert into search_bigram(rowid,tokens) values (?,?)",
                (row_id, bigram_tokens(body)),
            )
        sidecar.commit()
        sidecar.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def search(self, query: str, **kwargs):
        with (
            patch.object(server, "SQLITE_DB", str(self.posts_db)),
            patch.object(server, "BIGRAM_DB", str(self.bigram_db)),
            patch.object(server, "SYMBOL_DB", str(self.symbol_db)),
        ):
            return server.api_search_sqlite(query, "time", 1, 50, **kwargs)

    def test_content_scope_uses_post_rows_only(self) -> None:
        result = self.search("食堂", scope="content")
        self.assertEqual(result["search_backend"], "bigram")
        self.assertEqual([item["id"] for item in result["results"]], ["1"])

    def test_all_scope_includes_comment_rows(self) -> None:
        result = self.search("食堂", scope="all")
        self.assertEqual({item["id"] for item in result["results"]}, {"1", "2"})

    def test_admin_field_scope_is_preserved(self) -> None:
        body = self.search("食堂", scope="all", admin=True, admin_fields={"body"})
        comments = self.search("食堂", scope="all", admin=True, admin_fields={"cmt"})
        self.assertEqual([item["id"] for item in body["results"]], ["1"])
        self.assertEqual([item["id"] for item in comments["results"]], ["2"])

    def test_post_id_is_explicit_admin_field(self) -> None:
        public = self.search("2", scope="content")
        body = self.search("2", admin=True, admin_fields={"body"})
        uid = self.search("2", admin=True, admin_fields={"uid"}, id_match="contains")
        post_exact = self.search("2", admin=True, admin_fields={"post"})
        post_partial = self.search("1", admin=True, admin_fields={"post"})
        self.assertEqual(public["results"], [])
        self.assertEqual(body["results"], [])
        self.assertEqual([item["id"] for item in uid["results"]], ["2"])
        self.assertEqual([item["id"] for item in post_exact["results"]], ["2"])
        self.assertEqual([item["id"] for item in post_partial["results"]], ["1"])

    def test_one_character_query_falls_back_to_like(self) -> None:
        result = self.search("猫", scope="content")
        self.assertEqual(result["search_backend"], "like")
        self.assertEqual([item["id"] for item in result["results"]], ["3"])

    def test_two_character_query_without_bigram_reports_like(self) -> None:
        with (
            patch.object(server, "SQLITE_DB", str(self.posts_db)),
            patch.object(server, "BIGRAM_DB", ""),
        ):
            result = server.api_search_sqlite("食堂", "time", 1, 50, scope="all")
        self.assertEqual(result["search_backend"], "like")

    def test_admin_id_defaults_to_exact_and_can_use_contains(self) -> None:
        exact = self.search("u2", admin=True, admin_fields={"uid"})
        partial_exact = self.search("u", admin=True, admin_fields={"uid"})
        partial_contains = self.search(
            "u",
            admin=True,
            admin_fields={"uid"},
            id_match="contains",
        )
        comment_exact_without_cmt = self.search("comment-id", admin=True, admin_fields={"uid"})
        comment_exact = self.search("comment-id", admin=True, admin_fields={"uid", "cmt"})
        self.assertEqual([item["id"] for item in exact["results"]], ["2"])
        self.assertEqual(partial_exact["results"], [])
        self.assertEqual({item["id"] for item in partial_contains["results"]}, {"1", "2", "3", "4"})
        self.assertEqual(comment_exact_without_cmt["results"], [])
        self.assertEqual([item["id"] for item in comment_exact["results"]], ["2"])

    def test_admin_id_uses_body_and_comment_as_locations(self) -> None:
        post_only = self.search(
            "comment-id", admin=True, admin_fields={"body", "uid"}
        )
        comment_only = self.search(
            "comment-id", admin=True, admin_fields={"cmt", "uid"}
        )
        both = self.search(
            "comment-id", admin=True, admin_fields={"body", "cmt", "uid"}
        )
        self.assertEqual(post_only["results"], [])
        self.assertEqual([item["id"] for item in comment_only["results"]], ["2"])
        self.assertEqual([item["id"] for item in both["results"]], ["2"])
        self.assertEqual(both["search_backend"], "like")

    def test_identity_mode_does_not_or_text_content(self) -> None:
        result = self.search(
            "食堂", admin=True, admin_fields={"body", "cmt", "uid"}
        )
        self.assertEqual(result["results"], [])

    def test_admin_name_defaults_to_exact_and_can_use_contains(self) -> None:
        exact = self.search("测试昵称", admin=True, admin_fields={"name"})
        partial_exact = self.search("昵称", admin=True, admin_fields={"name"})
        partial_contains = self.search(
            "昵称",
            admin=True,
            admin_fields={"name"},
            name_match="contains",
        )
        reply_exact_without_cmt = self.search("回复昵称", admin=True, admin_fields={"name"})
        reply_exact = self.search("回复昵称", admin=True, admin_fields={"name", "cmt"})
        self.assertEqual([item["id"] for item in exact["results"]], ["2"])
        self.assertEqual(partial_exact["results"], [])
        self.assertEqual([item["id"] for item in partial_contains["results"]], ["2"])
        self.assertEqual(reply_exact_without_cmt["results"], [])
        self.assertEqual([item["id"] for item in reply_exact["results"]], ["2"])

    def test_public_search_never_uses_admin_identity_fields(self) -> None:
        result = self.search("u2", admin=False, admin_fields={"uid"})
        self.assertEqual(result["results"], [])

    def test_symbol_mixed_query_uses_symbol_index(self) -> None:
        content = self.search("♀找", scope="content")
        all_scope = self.search("♀找", scope="all")
        self.assertEqual(content["search_backend"], "symbol")
        self.assertEqual([item["id"] for item in content["results"]], ["4"])
        self.assertEqual({item["id"] for item in all_scope["results"]}, {"2", "4"})

    def test_admin_symbol_query_respects_body_comment_locations(self) -> None:
        body = self.search("♀找", admin=True, admin_fields={"body"})
        comments = self.search("♀找", admin=True, admin_fields={"cmt"})
        both = self.search("♀找", admin=True, admin_fields={"body", "cmt"})
        self.assertEqual([item["id"] for item in body["results"]], ["4"])
        self.assertEqual([item["id"] for item in comments["results"]], ["2"])
        self.assertEqual({item["id"] for item in both["results"]}, {"2", "4"})

    def test_short_symbol_query_without_index_is_limited_in_cursor_mode(self) -> None:
        with (
            patch.object(server, "SQLITE_DB", str(self.posts_db)),
            patch.object(server, "BIGRAM_DB", str(self.bigram_db)),
            patch.object(server, "SYMBOL_DB", ""),
        ):
            result = server._search_service().search_cursor("♀找", "time", 1, 2, scope="all")
        self.assertEqual(result["search_backend"], "scan-like")
        self.assertEqual(result["query_kind"], "symbol_mixed")
        self.assertIn("limited", result)


if __name__ == "__main__":
    unittest.main()
