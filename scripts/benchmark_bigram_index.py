"""Build sampled trigram and bigram FTS databases to estimate index size.

The source database is read-only. Output databases are disposable and contain
only sampled search rows, so the production database is never modified.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path


TOKEN_RUN = re.compile(r"[0-9A-Za-z_\u3400-\u4dbf\u4e00-\u9fff]+")
BOUNDARY_TOKEN = "zzbigramsegmentboundaryzz"


def bigram_tokens(text: str) -> str:
    segments: list[str] = []
    for run in TOKEN_RUN.findall(text or ""):
        lowered = run.lower()
        if len(lowered) == 1:
            segments.append(lowered)
        else:
            segments.append(" ".join(lowered[i : i + 2] for i in range(len(lowered) - 1)))
    return f" {BOUNDARY_TOKEN} ".join(segments)


def file_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def mib(value: int) -> str:
    return f"{value / 1024 / 1024:.2f} MiB"


def source_rows(conn: sqlite3.Connection, sample_mod: int):
    yield from conn.execute(
        """
        select id, 'post', content
        from posts
        where rowid % ? = 0 and content != ''
        """,
        (sample_mod,),
    )
    yield from conn.execute(
        """
        select post_id, 'comment', detail
        from comments
        where rowid % ? = 0 and detail != ''
        """,
        (sample_mod,),
    )


def prepare(path: Path, schema: str) -> sqlite3.Connection:
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("pragma journal_mode=off")
    conn.execute("pragma synchronous=off")
    conn.execute("pragma temp_store=file")
    conn.executescript(schema)
    return conn


def build(source: Path, output_dir: Path, sample_mod: int, only_bigram: bool = False) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)

    trigram_path = output_dir / "sample_trigram.db"
    bigram_path = output_dir / ("bigram_index.db" if sample_mod == 1 else "sample_bigram.db")
    trigram = None
    if only_bigram:
        trigram_path.unlink(missing_ok=True)
    else:
        trigram = prepare(
            trigram_path,
            """
            create virtual table search_index using fts5(
                post_id unindexed, kind unindexed, body, tokenize='trigram'
            );
            """,
        )
    bigram = prepare(
        bigram_path,
        """
        create table search_rows(
            row_id integer primary key,
            post_id text not null,
            kind text not null
        );
        create index idx_search_rows_post_id on search_rows(post_id);
        create table index_meta(
            key text primary key,
            value text not null
        );
        create virtual table search_bigram using fts5(
            tokens,
            content='',
            contentless_delete=1,
            tokenize='unicode61'
        );
        """,
    )

    count = 0
    source_bytes = 0
    token_bytes = 0
    started = time.perf_counter()
    if trigram is not None:
        trigram.execute("begin")
    bigram.execute("begin")
    for post_id, kind, body in source_rows(source_conn, sample_mod):
        body = body or ""
        count += 1
        source_bytes += len(body.encode("utf-8"))
        if trigram is not None:
            trigram.execute(
                "insert into search_index(post_id, kind, body) values (?,?,?)",
                (post_id, kind, body),
            )
        tokens = bigram_tokens(body)
        token_bytes += len(tokens.encode("utf-8"))
        bigram.execute(
            "insert into search_rows(row_id, post_id, kind) values (?,?,?)",
            (count, post_id, kind),
        )
        bigram.execute(
            "insert into search_bigram(rowid, tokens) values (?,?)",
            (count, tokens),
        )
        if count % 25_000 == 0:
            print(f"[build] rows={count:,}")

    if trigram is not None:
        trigram.commit()
    bigram.executemany(
        "insert into index_meta(key, value) values (?, ?)",
        [
            ("schema_version", "bigram-v1"),
            ("source_db", str(source.resolve())),
            ("source_db_bytes", str(source.stat().st_size)),
            ("source_rows", str(count)),
            ("sample_mod", str(sample_mod)),
            ("built_at", datetime.now().isoformat(timespec="seconds")),
        ],
    )
    bigram.commit()
    if trigram is not None:
        trigram.execute("insert into search_index(search_index) values ('optimize')")
    bigram.execute("insert into search_bigram(search_bigram) values ('optimize')")
    if trigram is not None:
        trigram.commit()
    bigram.commit()
    if trigram is not None:
        trigram.execute("vacuum")
    bigram.execute("vacuum")
    if trigram is not None:
        trigram.close()
    bigram.close()
    source_conn.close()

    trigram_bytes = file_size(trigram_path)
    bigram_bytes = file_size(bigram_path)
    total_rows = count * sample_mod
    print("\nRESULT")
    print(f"sample_mod={sample_mod}")
    print(f"sample_rows={count:,}")
    print(f"estimated_full_rows={total_rows:,}")
    print(f"sample_source_text={mib(source_bytes)}")
    print(f"sample_bigram_text={mib(token_bytes)}")
    if trigram is not None:
        print(f"sample_trigram_db={mib(trigram_bytes)}")
    print(f"sample_bigram_db={mib(bigram_bytes)}")
    if trigram is not None:
        print(f"estimated_full_trigram={mib(trigram_bytes * sample_mod)}")
    print(f"estimated_full_bigram={mib(bigram_bytes * sample_mod)}")
    if trigram is not None:
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
