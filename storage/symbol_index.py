"""Reusable symbol sidecar builder for emoji and punctuation searches."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.domain.search import symbol_tokens

SYMBOL_SCHEMA = """
create table symbol_rows(
    token text not null,
    post_id text not null,
    kind text not null,
    position integer not null
);
create index idx_symbol_token_kind_post on symbol_rows(token, kind, post_id);
create index idx_symbol_post_id on symbol_rows(post_id);
create table index_meta(
    key text primary key,
    value text not null
);
"""


@dataclass(frozen=True)
class SymbolBuildStats:
    rows: int
    source_rows: int
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


def build_symbol_index(
    source: Path,
    output: Path,
    *,
    sample_mod: int = 1,
    source_label: str | None = None,
    built_at: str | None = None,
) -> SymbolBuildStats:
    """Build a symbol sidecar without modifying the source DB."""
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
        target.executescript(SYMBOL_SCHEMA)
        inserted = 0
        source_rows = 0
        target.execute("begin")
        for source_rows, (post_id, kind, body) in enumerate(
            iter_source_rows(source_conn, sample_mod), start=1
        ):
            tokens = symbol_tokens(body or "")
            for position, token in enumerate(tokens):
                target.execute(
                    "insert into symbol_rows(token, post_id, kind, position) "
                    "values (?,?,?,?)",
                    (token, post_id, kind, position),
                )
                inserted += 1
            if source_rows % 25_000 == 0:
                print(f"[build-symbol] source_rows={source_rows:,} symbols={inserted:,}")
        target.executemany(
            "insert into index_meta(key, value) values (?,?)",
            [
                ("schema_version", "symbol-v1"),
                ("source_db", source_label or str(source.resolve())),
                ("source_db_bytes", str(source.stat().st_size)),
                ("source_rows", str(source_rows)),
                ("symbol_rows", str(inserted)),
                ("sample_mod", str(sample_mod)),
                ("built_at", built_at or datetime.now().isoformat(timespec="seconds")),
            ],
        )
        target.commit()
        target.execute("vacuum")
    finally:
        target.close()
        source_conn.close()
    return SymbolBuildStats(
        rows=inserted,
        source_rows=source_rows,
        elapsed_seconds=time.perf_counter() - started,
    )
