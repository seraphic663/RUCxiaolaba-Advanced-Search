"""Shared boundary for normalizing and validating one detail response."""

from __future__ import annotations

from dataclasses import dataclass

from crawler.normalizer import normalize_detail, validate_normalized_detail


@dataclass(frozen=True)
class DetailParseResult:
    """Normalized detail plus an optional reason it must not fully replace data."""

    parsed: tuple[dict, list[dict]] | None
    error: str | None


def parse_detail_payload(
    post_id: str,
    data: dict | None,
    *,
    empty_error: str = "empty_detail",
) -> DetailParseResult:
    """Normalize one upstream payload and apply the shared safety checks.

    A suspicious payload keeps its normalized value so callers can perform the
    existing partial-merge policy.  A foreign or empty payload has no safe
    normalized value and must be handled by the caller.
    """

    if not isinstance(data, dict) or not data:
        return DetailParseResult(None, empty_error)
    parsed = normalize_detail(str(post_id), data)
    if parsed is None:
        return DetailParseResult(None, "foreign_or_invalid")
    post, comments = parsed
    payload_error = validate_normalized_detail(post, comments)
    if payload_error:
        return DetailParseResult(parsed, f"suspicious_payload:{payload_error}")
    return DetailParseResult(parsed, None)
