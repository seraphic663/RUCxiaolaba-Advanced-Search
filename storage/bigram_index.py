"""Reusable Bigram sidecar builder shared by tools and demo fixtures."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.domain.search import bigram_tokens

BIGRAM_SCHEMA = """
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
"""


@dataclass(frozen=True)
class BigramBuildStats:
    rows: int
    source_bytes: int
    token_bytes: int
    elapsed_seconds: float


def iter_source_rows(conn: sqlite3.Connection, sample_mod: int = 1):
    """Yield searchable post and comment bodies from a posts database."""
    yield from conn.execute(
        """
        select id, 'post', content
        from posts
        where rowid % ? = 0 and content != ''
        order by rowid
        """,
        (sample_mod,),
    )
    yield from conn.execute(
        """
        select post_id, 'comment', detail
        from comments
        where rowid % ? = 0 and detail != ''
        order by rowid
        """,
        (sample_mod,),
    )


def build_bigram_index(
    source: Path,
    output: Path,
    *,
    sample_mod: int = 1,
    source_label: str | None = None,
    built_at: str | None = None,
) -> BigramBuildStats:
    """Build a contentless Bigram FTS sidecar without modifying the source DB."""
    if sample_mod < 1:
        raise ValueError("sample_mod must be >= 1")
    source = Path(source)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    started = time.perf_counter()
    source_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(output)
    try:
        target.execute("pragma journal_mode=off")
        target.execute("pragma synchronous=off")
        target.execute("pragma temp_store=file")
        target.executescript(BIGRAM_SCHEMA)
        source_bytes = 0
        token_bytes = 0
        count = 0
        target.execute("begin")
        for count, (post_id, kind, body) in enumerate(
            iter_source_rows(source_conn, sample_mod), start=1
        ):
            body = body or ""
            tokens = bigram_tokens(body)
            source_bytes += len(body.encode("utf-8"))
            token_bytes += len(tokens.encode("utf-8"))
            target.execute(
                "insert into search_rows(row_id, post_id, kind) values (?,?,?)",
                (count, post_id, kind),
            )
            target.execute(
                "insert into search_bigram(rowid, tokens) values (?,?)",
                (count, tokens),
            )
            if count % 25_000 == 0:
                print(f"[build] rows={count:,}")
        target.executemany(
            "insert into index_meta(key, value) values (?,?)",
            [
                ("schema_version", "bigram-v1"),
                ("source_db", source_label or str(source.resolve())),
                ("source_db_bytes", str(source.stat().st_size)),
                ("source_rows", str(count)),
                ("sample_mod", str(sample_mod)),
                ("built_at", built_at or datetime.now().isoformat(timespec="seconds")),
            ],
        )
        target.commit()
        target.execute("insert into search_bigram(search_bigram) values ('optimize')")
        target.commit()
        target.execute("vacuum")
    finally:
        target.close()
        source_conn.close()
    return BigramBuildStats(
        rows=count,
        source_bytes=source_bytes,
        token_bytes=token_bytes,
        elapsed_seconds=time.perf_counter() - started,
    )
