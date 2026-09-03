"""Build validated application-level search requests in one place."""

from __future__ import annotations

from datetime import datetime

from app.domain.search import SearchQuery

DEFAULT_ADMIN_FIELDS = frozenset({"body", "cmt", "uid", "name"})


def build_search_query(
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
    admin_fields: set[str] | frozenset[str] | None = None,
    id_match: str = "exact",
    name_match: str = "exact",
    gender_method: str = "combined",
) -> SearchQuery:
    """Create the repository request shared by numbered and cursor search."""

    return SearchQuery(
        text=query,
        sort_by=sort_by,
        page=page,
        limit=limit,
        category=category,
        l2=l2,
        date_from=date_from,
        date_to=date_to,
        scope=scope,
        user_id=uid,
        user_name=uname,
        admin=admin,
        identity=identity,
        source_state=source_state,
        gender_method=gender_method,
        admin_fields=frozenset(admin_fields or DEFAULT_ADMIN_FIELDS),
        id_match=id_match,
        name_match=name_match,
    )
