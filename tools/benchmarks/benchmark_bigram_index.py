"""Build sampled trigram/Bigram indexes and report their relative size."""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

from storage.bigram_index import build_bigram_index, iter_source_rows


def file_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def mib(value: int) -> str:
    return f"{value / 1024 / 1024:.2f} MiB"


def build_trigram_index(source: Path, output: Path, sample_mod: int) -> None:
    output.unlink(missing_ok=True)
    source_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(output)
    try:
        target.execute("pragma journal_mode=off")
        target.execute("pragma synchronous=off")
        target.execute("pragma temp_store=file")
        target.execute(
            """
            create virtual table search_index using fts5(
                post_id unindexed, kind unindexed, body, tokenize='trigram'
            )
            """
        )
        target.execute("begin")
        target.executemany(
            "insert into search_index(post_id, kind, body) values (?,?,?)",
            iter_source_rows(source_conn, sample_mod),
        )
        target.commit()
        target.execute("insert into search_index(search_index) values ('optimize')")
        target.commit()
        target.execute("vacuum")
    finally:
        target.close()
        source_conn.close()


def build(
    source: Path,
    output_dir: Path,
    sample_mod: int,
    only_bigram: bool = False,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    trigram_path = output_dir / "sample_trigram.db"
    bigram_path = output_dir / (
        "bigram_index.db" if sample_mod == 1 else "sample_bigram.db"
    )
    started = time.perf_counter()
    if only_bigram:
        trigram_path.unlink(missing_ok=True)
    else:
        build_trigram_index(source, trigram_path, sample_mod)
    stats = build_bigram_index(source, bigram_path, sample_mod=sample_mod)

    trigram_bytes = file_size(trigram_path)
    bigram_bytes = file_size(bigram_path)
    total_rows = stats.rows * sample_mod
    print("\nRESULT")
    print(f"sample_mod={sample_mod}")
    print(f"sample_rows={stats.rows:,}")
    print(f"estimated_full_rows={total_rows:,}")
    print(f"sample_source_text={mib(stats.source_bytes)}")
    print(f"sample_bigram_text={mib(stats.token_bytes)}")
    if not only_bigram:
        print(f"sample_trigram_db={mib(trigram_bytes)}")
    print(f"sample_bigram_db={mib(bigram_bytes)}")
    if not only_bigram:
        print(f"estimated_full_trigram={mib(trigram_bytes * sample_mod)}")
    print(f"estimated_full_bigram={mib(bigram_bytes * sample_mod)}")
    if not only_bigram:
        print(f"bigram_vs_trigram={bigram_bytes / max(1, trigram_bytes):.2f}x")
    print(f"elapsed={time.perf_counter() - started:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default="data/posts.db")
    parser.add_argument("--output-dir", default="temp/bigram_demo")
    parser.add_argument(
        "--sample-mod",
        type=int,
        default=20,
        help="Sample rows whose rowid is divisible by N; 20 is about 5%%.",
    )
    parser.add_argument(
        "--only-bigram",
        action="store_true",
        help="Skip the comparison trigram database.",
    )
    args = parser.parse_args()
    if args.sample_mod < 1:
        parser.error("--sample-mod must be >= 1")
    build(Path(args.db_path), Path(args.output_dir), args.sample_mod, args.only_bigram)


if __name__ == "__main__":
    main()
