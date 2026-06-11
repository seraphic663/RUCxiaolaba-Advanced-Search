"""Stable definitions for list-based crawler modes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PageScanStrategy:
    name: str
    endpoint: str
    start_page: int
    pages: int
    min_pages: int
    stop_unchanged: int


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


LATEST_POSTS = PageScanStrategy(
    "sync-latest", "lists", 1, 500, 20, 300
)
ACTIVE_POSTS = PageScanStrategy(
    "sync-active", "lists2", 1, 500, 20, 300
)
HISTORY_PAGES = PageScanStrategy(
    "scan-history", "lists2", 2, 500, 20, 600
)
