"""ID-range scan request model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IDRange:
    start_id: int
    end_id: int
    from_date: str = ""
    to_date: str = ""
