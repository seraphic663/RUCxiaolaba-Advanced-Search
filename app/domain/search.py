"""Search request model."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

BIGRAM_TOKEN_RUN = re.compile(r"[0-9A-Za-z_\u3400-\u4dbf\u4e00-\u9fff]+")
BIGRAM_BOUNDARY_TOKEN = "zzbigramsegmentboundaryzz"


def _bigram_segments(text: str) -> list[str]:
    segments: list[str] = []
    for run in BIGRAM_TOKEN_RUN.findall(text or ""):
        lowered = run.lower()
        if len(lowered) == 1:
            segments.append(lowered)
        else:
            segments.append(
                " ".join(lowered[index : index + 2] for index in range(len(lowered) - 1))
            )
    return segments


def bigram_tokens(text: str) -> str:
    """Convert searchable text to the token stream stored in Bigram FTS."""
    return f" {BIGRAM_BOUNDARY_TOKEN} ".join(_bigram_segments(text))


def bigram_query(keyword: str) -> str | None:
    """Return a quoted FTS phrase, or None when a keyword is too short."""
    runs = BIGRAM_TOKEN_RUN.findall(keyword or "")
    if sum(len(run) for run in runs) < 2:
        return None
    phrase = bigram_tokens(keyword)
    return f'"{phrase.replace(chr(34), chr(34) * 2)}"'


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
