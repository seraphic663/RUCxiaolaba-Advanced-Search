"""Compare production search with and without the Bigram sidecar.

The speed pass returns only the first page. The correctness pass separately
compares complete post ID sets, so result serialization does not distort the
latency numbers.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app.services.search_service import SearchService


DEFAULT_QUERIES = ("食堂", "选课", "六一", "图书馆", "校园卡", "猫")


@dataclass
class Timing:
    median_ms: float
    p95_ms: float
    samples_ms: list[float]


@dataclass
class CaseResult:
    query: str
    scope: str
    baseline_backend: str
    bigram_backend: str
    baseline_total: int
    bigram_total: int
    missing_ids: list[str]
    extra_ids: list[str]
    baseline_timing: Timing
    bigram_timing: Timing

    @property
    def correct(self) -> bool:
        return (
            self.baseline_total == self.bigram_total
            and not self.missing_ids
            and not self.extra_ids
        )

    @property
    def speedup(self) -> float:
        return self.baseline_timing.median_ms / max(
            self.bigram_timing.median_ms, 0.001
        )


def percentile_95(values: list[float]) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95 + 0.999) - 1))
    return ordered[index]


def timed_pair(
    baseline: SearchService,
    bigram: SearchService,
    query: str,
    scope: str,
    *,
    repeats: int,
    limit: int,
    max_sample_seconds: float,
) -> tuple[dict, Timing, dict, Timing]:
    baseline_samples: list[float] = []
    bigram_samples: list[float] = []
    baseline_result: dict = {}
    bigram_result: dict = {}
    attempts = 0
    max_attempts = repeats + 5
    while (
        len(baseline_samples) < repeats or len(bigram_samples) < repeats
    ) and attempts < max_attempts:
        order = (
            (("baseline", baseline), ("bigram", bigram))
            if attempts % 2 == 0
            else (("bigram", bigram), ("baseline", baseline))
        )
        attempts += 1
        for name, service in order:
            target = baseline_samples if name == "baseline" else bigram_samples
            if len(target) >= repeats:
                continue
            started = time.perf_counter()
            result = service.search(query, "time", 1, limit, scope=scope)
            elapsed = time.perf_counter() - started
            if name == "baseline":
                baseline_result = result
            else:
                bigram_result = result
            if elapsed > max_sample_seconds:
                print(
                    f"[benchmark] discard suspended/outlier sample "
                    f"backend={name} query={query!r} scope={scope} "
                    f"elapsed={elapsed:.1f}s",
                    flush=True,
                )
                continue
            target.append(elapsed * 1000)
    if len(baseline_samples) < repeats or len(bigram_samples) < repeats:
        raise RuntimeError(
            f"not enough valid timing samples for {query!r}/{scope}: "
            f"without={len(baseline_samples)}/{repeats}, "
            f"bigram={len(bigram_samples)}/{repeats}"
        )

    def timing(samples: list[float]) -> Timing:
        return Timing(
            median_ms=round(statistics.median(samples), 2),
            p95_ms=round(percentile_95(samples), 2),
            samples_ms=[round(value, 2) for value in samples],
        )

    return (
        baseline_result,
        timing(baseline_samples),
        bigram_result,
        timing(bigram_samples),
    )


def complete_ids(
    service: SearchService,
    query: str,
    scope: str,
    total: int,
) -> set[str]:
    if total <= 0:
        return set()
    result = service.search(query, "time", 1, total, scope=scope)
    return {str(item["id"]) for item in result["results"]}


def benchmark_case(
    baseline: SearchService,
    bigram: SearchService,
    query: str,
    scope: str,
    *,
    repeats: int,
    limit: int,
    warmups: int,
    max_sample_seconds: float,
) -> CaseResult:
    for _ in range(warmups):
        baseline.search(query, "time", 1, limit, scope=scope)
        bigram.search(query, "time", 1, limit, scope=scope)

    (
        baseline_result,
        baseline_timing,
        bigram_result,
        bigram_timing,
    ) = timed_pair(
        baseline,
        bigram,
        query,
        scope,
        repeats=repeats,
        limit=limit,
        max_sample_seconds=max_sample_seconds,
    )
    baseline_ids = complete_ids(
        baseline, query, scope, int(baseline_result["total"])
    )
    bigram_ids = complete_ids(
        bigram, query, scope, int(bigram_result["total"])
    )
    return CaseResult(
        query=query,
        scope=scope,
        baseline_backend=str(baseline_result.get("search_backend", "")),
        bigram_backend=str(bigram_result.get("search_backend", "")),
        baseline_total=int(baseline_result["total"]),
        bigram_total=int(bigram_result["total"]),
        missing_ids=sorted(baseline_ids - bigram_ids),
        extra_ids=sorted(bigram_ids - baseline_ids),
        baseline_timing=baseline_timing,
        bigram_timing=bigram_timing,
    )


def print_report(results: list[CaseResult]) -> None:
    header = (
        f"{'query':<10} {'scope':<8} {'correct':<8} "
        f"{'without med/p95':>22} {'bigram med/p95':>22} "
        f"{'speedup':>9} {'totals':>15} {'backend':>16}"
    )
    print(header)
    print("-" * len(header))
    for item in results:
        print(
            f"{item.query:<10} {item.scope:<8} "
            f"{str(item.correct):<8} "
            f"{item.baseline_timing.median_ms:>8.2f}/"
            f"{item.baseline_timing.p95_ms:<8.2f}ms "
            f"{item.bigram_timing.median_ms:>8.2f}/"
            f"{item.bigram_timing.p95_ms:<8.2f}ms "
            f"{item.speedup:>8.2f}x "
            f"{item.baseline_total:>7}/{item.bigram_total:<7} "
            f"{item.baseline_backend:>7}/{item.bigram_backend:<7}"
        )
        if not item.correct:
            print(
                f"  mismatch: missing={item.missing_ids[:10]} "
                f"extra={item.extra_ids[:10]}"
            )


def write_json(path: str, results: list[CaseResult]) -> None:
    if not path:
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark search correctness and speed with/without Bigram"
    )
    parser.add_argument("--db-path", default="data/posts.db")
    parser.add_argument("--bigram-db", default="data/bigram_index.db")
    parser.add_argument("--queries", nargs="+", default=list(DEFAULT_QUERIES))
    parser.add_argument(
        "--scopes",
        nargs="+",
        choices=("content", "all"),
        default=["content", "all"],
    )
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--max-sample-seconds",
        type=float,
        default=60,
        help="discard and retry samples longer than this (for machine sleep)",
    )
    parser.add_argument("--json-output", default="")
    parser.add_argument(
        "--allow-mismatch",
        action="store_true",
        help="return success even when complete result ID sets differ",
    )
    args = parser.parse_args()

    posts_db = Path(args.db_path)
    bigram_db = Path(args.bigram_db)
    if not posts_db.exists():
        parser.error(f"posts database not found: {posts_db}")
    if not bigram_db.exists():
        parser.error(f"Bigram database not found: {bigram_db}")
    if (
        args.repeats < 1
        or args.warmups < 0
        or args.limit < 1
        or args.max_sample_seconds <= 0
    ):
        parser.error(
            "repeats/limit/max-sample-seconds must be positive "
            "and warmups cannot be negative"
        )

    baseline = SearchService(posts_db, None)
    indexed = SearchService(posts_db, bigram_db)
    cases = [
        (query, scope)
        for scope in args.scopes
        for query in args.queries
    ]
    results: list[CaseResult] = []
    for index, (query, scope) in enumerate(cases, start=1):
        print(
            f"[case {index}/{len(cases)}] query={query!r} scope={scope}",
            flush=True,
        )
        item = benchmark_case(
            baseline,
            indexed,
            query,
            scope,
            repeats=args.repeats,
            limit=args.limit,
            warmups=args.warmups,
            max_sample_seconds=args.max_sample_seconds,
        )
        results.append(item)
        write_json(args.json_output, results)
        print(
            f"  correct={item.correct} "
            f"without={item.baseline_timing.median_ms:.2f}ms "
            f"bigram={item.bigram_timing.median_ms:.2f}ms "
            f"speedup={item.speedup:.2f}x",
            flush=True,
        )

    print()
    print_report(results)

    if args.json_output:
        print(f"\nJSON: {Path(args.json_output)}")

    mismatches = sum(not item.correct for item in results)
    print(f"\nCases={len(results)} correct={len(results) - mismatches} mismatches={mismatches}")
    return 0 if args.allow_mismatch or mismatches == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
