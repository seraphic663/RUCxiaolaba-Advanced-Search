"""Search request model."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

BIGRAM_TOKEN_RUN = re.compile(r"[0-9A-Za-z_\u3400-\u4dbf\u4e00-\u9fff]+")
BIGRAM_BOUNDARY_TOKEN = "zzbigramsegmentboundaryzz"
VARIATION_SELECTORS = {"\ufe0e", "\ufe0f"}


def is_symbol_char(char: str) -> bool:
    """Return true for punctuation/symbol codepoints that need exact routing."""
    if not char or char.isspace() or char in VARIATION_SELECTORS:
        return False
    category = unicodedata.category(char)
    return category[0] in {"P", "S"}


def symbol_tokens(text: str) -> list[str]:
    """Extract distinct symbol tokens in input order."""
    seen: set[str] = set()
    tokens: list[str] = []
    for char in text or "":
        if is_symbol_char(char) and char not in seen:
            seen.add(char)
            tokens.append(char)
    return tokens


def searchable_text_length(text: str) -> int:
    """Count ordinary searchable letters/numbers/CJK chars, excluding symbols."""
    return sum(len(run) for run in BIGRAM_TOKEN_RUN.findall(text or ""))


def query_kind(text: str) -> str:
    """Classify a query for backend routing and safety limits."""
    stripped = (text or "").strip()
    if not stripped:
        return "empty"
    ordinary_len = searchable_text_length(stripped)
    symbols = symbol_tokens(stripped)
    if symbols and ordinary_len == 0:
        return "symbol_only"
    if symbols:
        return "symbol_mixed"
    if ordinary_len <= 1:
        return "single_char"
    return "normal_text"


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
