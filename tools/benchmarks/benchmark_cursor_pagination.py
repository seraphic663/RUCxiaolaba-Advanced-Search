"""Benchmark conventional full-count search against cursor page scanning."""

from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path

from app.domain.search import SearchQuery
from app.repositories.search_repository import SearchRepository


def median_ms(call, repeats: int) -> tuple[dict, float]:
    samples = []
    result = {}
    for _ in range(repeats):
        started = time.perf_counter()
        result = call()
        samples.append((time.perf_counter() - started) * 1000)
    return result, statistics.median(samples)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark first-page cursor scanning for slow LIKE searches"
    )
    parser.add_argument("--db-path", default="data/posts.db")
    parser.add_argument("--queries", nargs="+", default=["猫", "六"])
    parser.add_argument("--scopes", nargs="+", default=["content", "all"])
    parser.add_argument("--sorts", nargs="+", default=["time", "stars", "comments", "score"])
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    path = Path(args.db_path)
    if not path.exists():
        parser.error(f"database not found: {path}")
    repository = SearchRepository(path)
    mismatches = 0

    print(
        f"{'query':<6} {'scope':<8} {'sort':<9} "
        f"{'normal':>10} {'cursor':>10} {'speedup':>9} "
        f"{'scanned':>15} {'correct':>8}"
    )
    print("-" * 88)
    for query in args.queries:
        for scope in args.scopes:
            for sort_by in args.sorts:
                request = SearchQuery(
                    text=query,
                    sort_by=sort_by,
                    limit=args.limit,
                    scope=scope,
                )
                normal, normal_ms = median_ms(
                    lambda: repository.search(request), args.repeats
                )
                cursor, cursor_ms = median_ms(
                    lambda: repository.search_cursor(request), args.repeats
                )
                normal_ids = [row["id"] for row in normal["results"]]
                cursor_ids = [row["id"] for row in cursor["results"]]
                correct = normal_ids == cursor_ids
                mismatches += not correct
                print(
                    f"{query:<6} {scope:<8} {sort_by:<9} "
                    f"{normal_ms:>8.1f}ms {cursor_ms:>8.1f}ms "
                    f"{normal_ms / max(cursor_ms, .001):>8.2f}x "
                    f"{cursor['scanned']:>7}/{cursor['candidate_total']:<7} "
                    f"{str(correct):>8}"
                )

    print(f"\nMismatches={mismatches}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
