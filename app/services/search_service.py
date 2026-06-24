"""Search application service and compatibility-facing API."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.domain.search import SearchQuery
from app.repositories.search_repository import SearchRepository


class SearchService:
    def __init__(
        self,
        posts_db: str | Path,
        bigram_db: str | Path | None = None,
        symbol_db: str | Path | None = None,
    ):
        self.repository = SearchRepository(posts_db, bigram_db, symbol_db)

    def search(
        self,
        query: str,
        sort_by: str,
        page: int,
        limit: int,
        *,
        category: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        scope: str = "content",
        uid: str | None = None,
        uname: str | None = None,
        admin: bool = False,
        identity: str | None = None,
        admin_fields: set[str] | None = None,
        id_match: str = "exact",
        name_match: str = "exact",
    ) -> dict:
        request = SearchQuery(
            text=query,
            sort_by=sort_by,
            page=page,
            limit=limit,
            category=category,
            date_from=date_from,
            date_to=date_to,
            scope=scope,
            user_id=uid,
            user_name=uname,
            admin=admin,
            identity=identity,
            admin_fields=frozenset(
                admin_fields or {"body", "cmt", "uid", "name"}
            ),
            id_match=id_match,
            name_match=name_match,
        )
        return self.repository.search(request)

    def categories(self, min_count: int = 200) -> dict:
        return self.repository.categories(min_count=min_count)

    def search_cursor(
        self,
        query: str,
        sort_by: str,
        page: int,
        limit: int,
        *,
        scan_offset: int = 0,
        matched_before: int = 0,
        category: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        scope: str = "content",
        uid: str | None = None,
        uname: str | None = None,
        admin: bool = False,
        identity: str | None = None,
        admin_fields: set[str] | None = None,
        id_match: str = "exact",
        name_match: str = "exact",
    ) -> dict:
        request = SearchQuery(
            text=query,
            sort_by=sort_by,
            page=page,
            limit=limit,
            category=category,
            date_from=date_from,
            date_to=date_to,
            scope=scope,
            user_id=uid,
            user_name=uname,
            admin=admin,
            identity=identity,
            admin_fields=frozenset(
                admin_fields or {"body", "cmt", "uid", "name"}
            ),
            id_match=id_match,
            name_match=name_match,
        )
        return self.repository.search_cursor(
            request,
            scan_offset=scan_offset,
            matched_before=matched_before,
        )

    def comments(self, post_id: str, *, admin: bool = False) -> dict | None:
        return self.repository.comments(post_id, admin=admin)
