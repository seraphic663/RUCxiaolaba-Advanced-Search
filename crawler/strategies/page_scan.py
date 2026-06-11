"""Stopping policy for list-based crawler modes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PageScanProgress:
    """Stateful stopping policy independent from API and SQLite code."""

    pages: int = 0
    consecutive_unchanged: int = 0

    def page_read(self) -> None:
        self.pages += 1

    def unchanged(self) -> None:
        self.consecutive_unchanged += 1

    def changed(self) -> None:
        self.consecutive_unchanged = 0

    def should_stop(self, *, min_pages: int, threshold: int) -> bool:
        return (
            self.pages >= min_pages
            and self.consecutive_unchanged >= threshold
        )
