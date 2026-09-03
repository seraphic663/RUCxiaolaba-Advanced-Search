"""Search application service and compatibility-facing API."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.repositories.search_repository import SearchRepository
from app.services.search_request import build_search_query


class SearchService:
    def __init__(
        self,
        posts_db: str | Path,
        bigram_db: str | Path | None = None,
        symbol_db: str | Path | None = None,
        gender_db: str | Path | None = None,
    ):
        self.repository = SearchRepository(posts_db, bigram_db, symbol_db, gender_db)

    def search(
        self,
        query: str,
        sort_by: str,
        page: int,
        limit: int,
        *,
        category: str | None = None,
        l2: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        scope: str = "content",
        uid: str | None = None,
        uname: str | None = None,
        admin: bool = False,
        identity: str | None = None,
        source_state: str = "all",
        admin_fields: set[str] | None = None,
        id_match: str = "exact",
        name_match: str = "exact",
        gender_method: str = "combined",
    ) -> dict:
        request = build_search_query(
            query,
            sort_by,
            page,
            limit,
            category=category,
            l2=l2,
            date_from=date_from,
            date_to=date_to,
            scope=scope,
            uid=uid,
            uname=uname,
            admin=admin,
            identity=identity,
            source_state=source_state,
            admin_fields=admin_fields,
            id_match=id_match,
            name_match=name_match,
            gender_method=gender_method,
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
        l2: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        scope: str = "content",
        uid: str | None = None,
        uname: str | None = None,
        admin: bool = False,
        identity: str | None = None,
        source_state: str = "all",
        admin_fields: set[str] | None = None,
        id_match: str = "exact",
        name_match: str = "exact",
        gender_method: str = "combined",
    ) -> dict:
        request = build_search_query(
            query,
            sort_by,
            page,
            limit,
            category=category,
            l2=l2,
            date_from=date_from,
            date_to=date_to,
            scope=scope,
            uid=uid,
            uname=uname,
            admin=admin,
            identity=identity,
            source_state=source_state,
            admin_fields=admin_fields,
            id_match=id_match,
            name_match=name_match,
            gender_method=gender_method,
        )
        return self.repository.search_cursor(
            request,
            scan_offset=scan_offset,
            matched_before=matched_before,
        )

    def comments(
        self,
        post_id: str,
        *,
        admin: bool = False,
        gender_sort: str = "time",
        gender_method: str = "combined",
    ) -> dict | None:
        return self.repository.comments(
            post_id,
            admin=admin,
            gender_sort=gender_sort,
            gender_method=gender_method,
        )
