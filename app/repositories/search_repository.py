"""Read-side search repository for posts, comments and optional indexes."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from app.domain.search import SearchQuery
from app.repositories.connections import connect_readonly


BIGRAM_TOKEN_RUN = re.compile(r"[0-9A-Za-z_\u3400-\u4dbf\u4e00-\u9fff]+")
BIGRAM_BOUNDARY_TOKEN = "zzbigramsegmentboundaryzz"


def _safe_int(value, default=0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def fts_query(keywords: list[str]) -> str | None:
    if not keywords or any(len(keyword) < 3 for keyword in keywords):
        return None
    return " AND ".join(f'"{keyword.replace(chr(34), chr(34) * 2)}"' for keyword in keywords)


def bigram_query(keyword: str) -> str | None:
    runs = [run.lower() for run in BIGRAM_TOKEN_RUN.findall(keyword or "")]
    if sum(len(run) for run in runs) < 2:
        return None
    segments = []
    for run in runs:
        if len(run) == 1:
            segments.append(run)
        else:
            segments.append(" ".join(run[i : i + 2] for i in range(len(run) - 1)))
    phrase = f" {BIGRAM_BOUNDARY_TOKEN} ".join(segments)
    return f'"{phrase.replace(chr(34), chr(34) * 2)}"'


class SearchRepository:
    def __init__(
        self,
        posts_db: str | Path,
        bigram_db: str | Path | None = None,
    ):
        self.posts_db = Path(posts_db)
        self.bigram_db = Path(bigram_db) if bigram_db else None

    def connect(self):
        return connect_readonly(self.posts_db, self.bigram_db)

    def has_search_index(self) -> bool:
        if not self.posts_db.exists():
            return False
        with self.connect() as conn:
            row = conn.execute(
                "select 1 from sqlite_master "
                "where name='search_index' and type='table'"
            ).fetchone()
        return row is not None

    def has_bigram_index(self) -> bool:
        if not self.bigram_db or not self.bigram_db.exists():
            return False
        try:
            with self.connect() as conn:
                row = conn.execute(
                    "select value from bigram.index_meta "
                    "where key='schema_version'"
                ).fetchone()
            return row is not None and row[0] == "bigram-v1"
        except sqlite3.Error:
            return False

    def _where(
        self,
        request: SearchQuery,
        *,
        use_fts: bool,
        use_bigram: bool,
    ) -> tuple[str, list]:
        clauses: list[str] = []
        args: list = []
        keywords = (request.text or "").lower().split()
        fields = set(request.admin_fields)
        expression = (
            fts_query(keywords)
            if not use_bigram
            and request.scope == "all"
            and use_fts
            and not request.admin
            else None
        )
        if expression:
            clauses.append(
                "p.id in (select post_id from search_index where body match ?)"
            )
            args.append(expression)
        else:
            for keyword in keywords:
                like = f"%{keyword}%"
                token_query = bigram_query(keyword) if use_bigram else None
                if request.admin:
                    field_clauses: list[str] = []
                    if "body" in fields:
                        if token_query:
                            body_clause = (
                                "p.id in ("
                                "select m.post_id from bigram.search_bigram f "
                                "join bigram.search_rows m on m.row_id=f.rowid "
                                "where f.tokens match ? and m.kind='post'"
                                ")"
                            )
                            if keyword.isdigit():
                                body_clause = f"(p.id like ? or {body_clause})"
                                args.append(like)
                            field_clauses.append(body_clause)
                            args.append(token_query)
                        else:
                            field_clauses.append(
                                "(lower(p.content) like ? or p.id like ?)"
                            )
                            args.extend([like, like])
                    if "cmt" in fields:
                        if token_query:
                            field_clauses.append(
                                "p.id in ("
                                "select m.post_id from bigram.search_bigram f "
                                "join bigram.search_rows m on m.row_id=f.rowid "
                                "where f.tokens match ? and m.kind='comment'"
                                ")"
                            )
                            args.append(token_query)
                        else:
                            field_clauses.append(
                                "p.id in (select post_id from comments "
                                "where lower(detail) like ?)"
                            )
                            args.append(like)
                    if "uid" in fields:
                        operator = "like" if request.id_match == "contains" else "="
                        value = like if operator == "like" else keyword
                        field_clauses.append(
                            "("
                            f"p.id {operator} ? or p.show_user_id {operator} ? or "
                            f"p.real_user_id {operator} ? or "
                            "p.id in (select post_id from comments where "
                            f"show_user_id {operator} ? or real_user_id {operator} ? or "
                            f"reply_show_user_id {operator} ?)"
                            ")"
                        )
                        args.extend([value] * 6)
                    if "name" in fields:
                        operator = "like" if request.name_match == "contains" else "="
                        value = like if operator == "like" else keyword
                        field_clauses.append(
                            "("
                            f"lower(p.user_name) {operator} ? or "
                            "p.id in (select post_id from comments where "
                            f"lower(show_user_name) {operator} ? or "
                            f"lower(reply_show_user_name) {operator} ?)"
                            ")"
                        )
                        args.extend([value] * 3)
                    clauses.append("(" + " or ".join(field_clauses or ["0"]) + ")")
                elif token_query:
                    kind_filter = (
                        " and m.kind='post'" if request.scope == "content" else ""
                    )
                    text_clause = (
                        "p.id in ("
                        "select m.post_id from bigram.search_bigram f "
                        "join bigram.search_rows m on m.row_id=f.rowid "
                        f"where f.tokens match ?{kind_filter}"
                        ")"
                    )
                    if keyword.isdigit():
                        text_clause = f"(p.id like ? or {text_clause})"
                        args.append(like)
                    clauses.append(text_clause)
                    args.append(token_query)
                elif request.scope == "all":
                    clauses.append(
                        "p.id in ("
                        "select id from posts where lower(content) like ? or id like ? "
                        "union select post_id from comments where lower(detail) like ?"
                        ")"
                    )
                    args.extend([like, like, like])
                else:
                    clauses.append("(lower(p.content) like ? or p.id like ?)")
                    args.extend([like, like])

        if request.category:
            clauses.append("p.category_name = ?")
            args.append(request.category)
        if request.date_from:
            clauses.append("p.create_time >= ?")
            args.append(request.date_from.strftime("%Y-%m-%d %H:%M:%S"))
        if request.date_to:
            clauses.append("p.create_time <= ?")
            args.append(request.date_to.strftime("%Y-%m-%d %H:%M:%S"))
        if request.admin and request.user_id:
            clauses.append("p.show_user_id = ?")
            args.append(request.user_id)
        if request.admin and request.user_name:
            clauses.append("lower(p.user_name) like ?")
            args.append(f"%{request.user_name.lower()}%")
        if request.admin and request.identity == "anonymous":
            clauses.append("(p.real_user_id='' or p.real_user_id='0')")
        if request.admin and request.identity == "real":
            clauses.append("(p.real_user_id!='' and p.real_user_id!='0')")
        where_sql = " where " + " and ".join(clauses) if clauses else ""
        return where_sql, args

    @staticmethod
    def _public_post(row) -> dict:
        return {
            "id": row["id"],
            "content": row["content"],
            "category": row["category_name"],
            "user": row["user_name"],
            "time": row["create_time"],
            "comments": _safe_int(row["comment_count"]),
            "stars": _safe_int(row["star_count"]),
            "trace": _safe_int(row["trace_count"]),
            "views": _safe_int(row["views"]),
            "hot": _safe_int(row["hot"]),
        }

    def search(self, request: SearchQuery) -> dict:
        if not self.posts_db.exists():
            return {
                "total": 0,
                "page": 1,
                "page_size": request.limit,
                "total_pages": 1,
                "results": [],
            }

        order_map = {
            "time": "p.create_time desc, p.id desc",
            "stars": "p.star_count desc, cast(p.id as integer) desc",
            "comments": "p.comment_count desc, cast(p.id as integer) desc",
            "score": (
                "(p.star_count * 3 + p.comment_count * 5 + "
                "max(0, 30 - ((strftime('%s','now') - "
                "strftime('%s',p.create_time)) / 86400.0))) desc, "
                "p.create_time desc, cast(p.id as integer) desc"
            ),
        }
        order_by = order_map.get(request.sort_by, order_map["time"])
        use_bigram = bool(request.text) and self.has_bigram_index()
        use_fts = (
            not use_bigram
            and request.scope == "all"
            and bool(request.text)
            and self.has_search_index()
        )
        where_sql, args = self._where(
            request, use_fts=use_fts, use_bigram=use_bigram
        )
        with self.connect() as conn:
            total = conn.execute(
                f"select count(*) from posts p{where_sql}", args
            ).fetchone()[0]
            total_pages = max(1, (total + request.limit - 1) // request.limit)
            page = max(1, min(request.page, total_pages))
            offset = (page - 1) * request.limit
            rows = conn.execute(
                f"""
                select p.id, p.content, p.category_name, p.user_name,
                       p.create_time, p.comment_count, p.star_count,
                       p.trace_count, p.views, p.hot,
                       p.show_user_id, p.real_user_id
                from posts p
                {where_sql}
                order by {order_by}
                limit ? offset ?
                """,
                args + [request.limit, offset],
            ).fetchall()

        results = []
        for row in rows:
            item = self._public_post(row)
            if request.admin:
                item["show_user_id"] = row["show_user_id"]
                item["real_user_id"] = row["real_user_id"]
            results.append(item)

        if use_bigram:
            terms = [
                bigram_query(keyword) is not None
                for keyword in (request.text or "").lower().split()
            ]
            if terms and all(terms):
                backend = "bigram"
            elif any(terms):
                backend = "hybrid"
            else:
                backend = "like"
        else:
            backend = "trigram" if use_fts else "like"
        return {
            "total": total,
            "page": page,
            "page_size": request.limit,
            "total_pages": total_pages,
            "results": results,
            "search_backend": backend,
        }

    def categories(self) -> dict:
        if not self.posts_db.exists():
            return {"categories": []}
        with self.connect() as conn:
            rows = conn.execute(
                """
                select category_name from posts
                where category_name != ''
                group by category_name
                having count(*) >= 5
                order by category_name
                """
            ).fetchall()
        return {"categories": [row["category_name"] for row in rows]}

    def comments(
        self,
        post_id: str,
        *,
        admin: bool = False,
        limit: int = 500,
        normalize_publisher_name: bool = True,
    ) -> dict | None:
        if not self.posts_db.exists():
            return None
        with self.connect() as conn:
            post = conn.execute(
                "select user_name, comment_count from posts where id=?",
                (post_id,),
            ).fetchone()
            if post is None:
                return None
            rows = conn.execute(
                """
                select comment_id, parent_comment_id, detail, show_user_name,
                       create_time, show_user_id, real_user_id, is_publisher,
                       reply_show_user_name, reply_show_user_id
                from comments
                where post_id=?
                order by create_time, row_key
                limit ?
                """,
                (post_id, limit * 3),
            ).fetchall()

        top: list[dict] = []
        by_id: dict[str, dict] = {}
        for row in rows:
            item = {
                "detail": row["detail"],
                "show_user_name": row["show_user_name"],
                "create_time": row["create_time"],
                "is_publisher": row["is_publisher"],
                "reply_show_user_name": row["reply_show_user_name"],
                "reply_comment_list": [],
            }
            if admin:
                item["show_user_id"] = row["show_user_id"]
                item["real_user_id"] = row["real_user_id"]
                item["reply_show_user_id"] = row["reply_show_user_id"]
            if (
                normalize_publisher_name
                and row["is_publisher"] == 1
                and post["user_name"]
            ):
                item["show_user_name"] = post["user_name"]
            if row["parent_comment_id"]:
                parent = by_id.get(row["parent_comment_id"])
                if parent is not None:
                    parent["reply_comment_list"].append(item)
                else:
                    top.append(item)
            else:
                top.append(item)
                by_id[row["comment_id"]] = item
        return {
            "post_id": post_id,
            "comment_count": post["comment_count"],
            "comment_list": top[:limit],
        }
