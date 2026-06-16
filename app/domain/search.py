"""Search request model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class SearchQuery:
    text: str
    sort_by: str = "time"
    page: int = 1
    limit: int = 50
    category: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    scope: str = "content"
    user_id: str | None = None
    user_name: str | None = None
    admin: bool = False
    identity: str | None = None
    admin_fields: frozenset[str] = field(
        default_factory=lambda: frozenset({"body", "cmt", "uid", "name", "post"})
    )
    id_match: str = "exact"
    name_match: str = "exact"
