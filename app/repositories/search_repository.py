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
    SCAN_BATCH_SIZE = 500

    def __init__(
        self,
        posts_db: str | Path,
        bigram_db: str | Path | None = None,
    ):
        self.posts_db = Path(posts_db)
        self.bigram_db = Path(bigram_db) if bigram_db else None

    def connect(self, *, include_bigram: bool = False):
        return connect_readonly(
            self.posts_db,
            self.bigram_db if include_bigram else None,
        )

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
            with self.connect(include_bigram=True) as conn:
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

    def _candidate_where(self, request: SearchQuery) -> tuple[str, list]:
        """Build non-text filters used before a cursor scan."""
        clauses: list[str] = []
        args: list = []
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
        return (
            " where " + " and ".join(clauses) if clauses else "",
            args,
        )

    @staticmethod
    def _order_by(sort_by: str) -> str:
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
        return order_map.get(sort_by, order_map["time"])

    def _plan(self, request: SearchQuery) -> tuple[bool, bool, str]:
        keywords = (request.text or "").lower().split()
        can_use_bigram = any(
            bigram_query(keyword) is not None for keyword in keywords
        )
        use_bigram = can_use_bigram and self.has_bigram_index()
        use_fts = (
            not use_bigram
            and request.scope == "all"
            and bool(request.text)
            and self.has_search_index()
        )
        used_fts = (
            use_fts
            and not request.admin
            and fts_query(keywords) is not None
        )
        if use_bigram:
            terms = [bigram_query(keyword) is not None for keyword in keywords]
            backend = "bigram" if terms and all(terms) else "hybrid"
        else:
            backend = "trigram" if used_fts else "like"
        return use_bigram, use_fts, backend

    @staticmethod
    def _comments_by_post(conn, post_ids: list[str]) -> dict[str, list]:
        if not post_ids:
            return {}
        placeholders = ",".join("?" for _ in post_ids)
        rows = conn.execute(
            f"""
            select post_id, detail, show_user_id, real_user_id,
                   reply_show_user_id, show_user_name, reply_show_user_name
            from comments
            where post_id in ({placeholders})
            """,
            post_ids,
        ).fetchall()
        grouped: dict[str, list] = {}
        for row in rows:
            grouped.setdefault(str(row["post_id"]), []).append(row)
        return grouped

    @staticmethod
    def _matches_scan_row(
        row,
        comments: list,
        request: SearchQuery,
    ) -> bool:
        keywords = (request.text or "").lower().split()
        if not keywords:
            return True
        fields = set(request.admin_fields)
        content = str(row["content"] or "").lower()
        post_id = str(row["id"] or "").lower()
        user_name = str(row["user_name"] or "").lower()
        post_show_id = str(row["show_user_id"] or "").lower()
        post_real_id = str(row["real_user_id"] or "").lower()

        def exact_or_contains(value: str, keyword: str, mode: str) -> bool:
            return keyword in value if mode == "contains" else keyword == value

        for keyword in keywords:
            if not request.admin:
                body_match = keyword in content or keyword in post_id
                comment_match = (
                    request.scope == "all"
                    and any(keyword in str(item["detail"] or "").lower() for item in comments)
                )
                if not body_match and not comment_match:
                    return False
                continue

            matched = False
            if "body" in fields:
                matched = keyword in content or keyword in post_id
            if not matched and "cmt" in fields:
                matched = any(
                    keyword in str(item["detail"] or "").lower()
                    for item in comments
                )
            if not matched and "uid" in fields:
                values = [post_id, post_show_id, post_real_id]
                for item in comments:
                    values.extend(
                        [
                            str(item["show_user_id"] or "").lower(),
                            str(item["real_user_id"] or "").lower(),
                            str(item["reply_show_user_id"] or "").lower(),
                        ]
                    )
                matched = any(
                    exact_or_contains(value, keyword, request.id_match)
                    for value in values
                )
            if not matched and "name" in fields:
                values = [user_name]
                for item in comments:
                    values.extend(
                        [
                            str(item["show_user_name"] or "").lower(),
                            str(item["reply_show_user_name"] or "").lower(),
                        ]
                    )
                matched = any(
                    exact_or_contains(value, keyword, request.name_match)
                    for value in values
                )
            if not matched:
                return False
        return True

    def _should_cursor_scan(self, request: SearchQuery, backend: str) -> bool:
        if not request.text or backend != "like":
            return False
        if not request.admin:
            return True
        fields = set(request.admin_fields)
        exact_identity_only = fields <= {"uid", "name"} and (
            ("uid" not in fields or request.id_match == "exact")
            and ("name" not in fields or request.name_match == "exact")
        )
        return not exact_identity_only

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

        order_by = self._order_by(request.sort_by)
        use_bigram, use_fts, backend = self._plan(request)
        where_sql, args = self._where(
            request, use_fts=use_fts, use_bigram=use_bigram
        )
        with self.connect(include_bigram=use_bigram) as conn:
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

        return {
            "total": total,
            "page": page,
            "page_size": request.limit,
            "total_pages": total_pages,
            "results": results,
            "search_backend": backend,
        }

    def search_cursor(
        self,
        request: SearchQuery,
        *,
        scan_offset: int = 0,
        matched_before: int = 0,
        batch_size: int | None = None,
    ) -> dict:
        """Return one sorted page, stopping once enough LIKE matches are found."""
        use_bigram, use_fts, backend = self._plan(request)
        if not self._should_cursor_scan(request, backend):
            result = self.search(request)
            result.update(
                {
                    "pagination_mode": "numbered",
                    "candidate_total": result["total"],
                    "scanned": result["total"],
                    "matched_so_far": result["total"],
                    "has_more": result["page"] < result["total_pages"],
                    "next_offset": None,
                    "total_exact": True,
                }
            )
            return result

        scan_offset = max(0, scan_offset)
        matched_before = max(0, matched_before)
        candidate_where, candidate_args = self._candidate_where(request)
        order_by = self._order_by(request.sort_by)
        results: list[dict] = []
        need_comments = request.scope == "all" or (
            request.admin
            and bool(set(request.admin_fields) & {"cmt", "uid", "name"})
        )
        default_batch = self.SCAN_BATCH_SIZE if need_comments else 5_000
        max_batch = 900 if need_comments else 5_000
        batch_size = max(
            request.limit,
            min(batch_size or default_batch, max_batch),
        )

        with self.connect() as conn:
            candidate_total = conn.execute(
                f"select count(*) from posts p{candidate_where}",
                candidate_args,
            ).fetchone()[0]
            cursor = min(scan_offset, candidate_total)
            while cursor < candidate_total and len(results) < request.limit:
                rows = conn.execute(
                    f"""
                    select p.id, p.content, p.category_name, p.user_name,
                           p.create_time, p.comment_count, p.star_count,
                           p.trace_count, p.views, p.hot,
                           p.show_user_id, p.real_user_id
                    from posts p
                    {candidate_where}
                    order by {order_by}
                    limit ? offset ?
                    """,
                    candidate_args + [batch_size, cursor],
                ).fetchall()
                if not rows:
                    cursor = candidate_total
                    break
                comments = (
                    self._comments_by_post(
                        conn, [str(row["id"]) for row in rows]
                    )
                    if need_comments
                    else {}
                )
                for index, row in enumerate(rows):
                    cursor += 1
                    if self._matches_scan_row(
                        row,
                        comments.get(str(row["id"]), []),
                        request,
                    ):
                        item = self._public_post(row)
                        if request.admin:
                            item["show_user_id"] = row["show_user_id"]
                            item["real_user_id"] = row["real_user_id"]
                        results.append(item)
                        if len(results) >= request.limit:
                            break

        has_more = cursor < candidate_total
        matched_so_far = matched_before + len(results)
        total = matched_so_far if not has_more else None
        return {
            "total": total,
            "page": max(1, request.page),
            "page_size": request.limit,
            "total_pages": request.page if not has_more else None,
            "results": results,
            "search_backend": "scan-like",
            "pagination_mode": "cursor",
            "candidate_total": candidate_total,
            "scanned": cursor,
            "matched_so_far": matched_so_far,
            "has_more": has_more,
            "next_offset": cursor if has_more else None,
            "total_exact": not has_more,
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
