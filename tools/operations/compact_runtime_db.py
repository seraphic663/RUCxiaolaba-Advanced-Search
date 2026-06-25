"""Compact the runtime SQLite DB for Railway Volume deployments.

This script builds a replacement posts DB without low-value legacy columns:
`posts.show_user_head`, `posts.views`, `posts.hot`, and
`comments.reply_comment_list`. It keeps the Bigram and symbol sidecars separate.

Typical Railway SSH flow:

    python -m tools.operations.compact_runtime_db plan --db /app/data/posts.db --bigram /app/data/bigram_index.db --symbol /app/data/symbol_index.db
    python -m tools.operations.compact_runtime_db migrate --db /app/data/posts.db --out /app/data/posts.next.db
    python -m tools.operations.compact_runtime_db verify --db /app/data/posts.next.db --bigram /app/data/bigram_index.db --symbol /app/data/symbol_index.db
    python -m tools.operations.compact_runtime_db swap --db /app/data/posts.db --next /app/data/posts.next.db
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from storage.bigram_index import build_bigram_index
from storage.symbol_index import build_symbol_index

POST_COLUMNS = [
    "id",
    "content",
    "category_name",
    "user_name",
    "show_user_id",
    "real_user_id",
    "create_time",
    "comment_count",
    "star_count",
    "trace_count",
    "updated_at",
]

COMMENT_COLUMNS = [
    "row_key",
    "comment_id",
    "post_id",
    "parent_comment_id",
    "detail",
    "show_user_name",
    "show_user_id",
    "real_user_id",
    "reply_show_user_name",
    "reply_show_user_id",
    "is_publisher",
    "create_time",
    "updated_at",
]


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("pragma journal_mode=off")
    conn.execute("pragma synchronous=off")
    conn.execute("pragma temp_store=file")
    # Keep memory use conservative for Railway's web container. The migration
    # is I/O-heavy anyway, and a large page cache can get the SSH command killed
    # while indexes are being built.
    conn.execute("pragma cache_size=-20000")
    return conn


def table_columns(conn: sqlite3.Connection, schema: str, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"pragma {schema}.table_info({table})")}


def table_exists(conn: sqlite3.Connection, schema: str, table: str) -> bool:
    row = conn.execute(
        f"select 1 from {schema}.sqlite_master where type='table' and name=?",
        (table,),
    ).fetchone()
    return row is not None


def db_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"select count(*) from {table}").fetchone()[0])


class Heartbeat:
    def __init__(self, label: str, interval: int = 20):
        self.label = label
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            print(f"[busy] {self.label} still running", flush=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        self._thread.join(timeout=1)


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table posts (
            id text primary key,
            content text not null,
            category_name text not null,
            user_name text not null,
            show_user_id text not null,
            real_user_id text not null,
            create_time text not null,
            comment_count integer not null,
            star_count integer not null,
            trace_count integer not null,
            updated_at text not null
        );

        create table comments (
            row_key text primary key,
            comment_id text not null,
            post_id text not null,
            parent_comment_id text not null,
            detail text not null,
            show_user_name text not null,
            show_user_id text not null,
            real_user_id text not null,
            reply_show_user_name text not null,
            reply_show_user_id text not null,
            is_publisher integer not null,
            create_time text not null,
            updated_at text not null
        );

        create table crawl_state (
            key text primary key,
            value text not null,
            updated_at text not null
        );
        """
    )


def create_indexes(conn: sqlite3.Connection) -> None:
    statements = [
        "create index idx_posts_create_time on posts(create_time)",
        "create index idx_posts_stars on posts(star_count desc, id desc)",
        "create index idx_posts_category on posts(category_name)",
        "create index idx_posts_show_user_id on posts(show_user_id)",
        "create index idx_posts_real_user_id on posts(real_user_id)",
        "create index idx_posts_user_name_lower on posts(lower(user_name))",
        "create index idx_comments_post_id on comments(post_id)",
        "create index idx_comments_create_time on comments(create_time)",
        "create index idx_comments_post_time on comments(post_id, create_time, row_key)",
        "create index idx_comments_show_user_id on comments(show_user_id)",
        "create index idx_comments_real_user_id on comments(real_user_id)",
        "create index idx_comments_reply_show_user_id on comments(reply_show_user_id)",
        "create index idx_comments_show_user_name_lower on comments(lower(show_user_name))",
        "create index idx_comments_reply_user_name_lower on comments(lower(reply_show_user_name))",
    ]
    for statement in statements:
        print(f"[migrate] {statement}", flush=True)
        with Heartbeat(statement):
            conn.execute(statement)


def rebuild_search_index(conn: sqlite3.Connection, batch_size: int) -> int:
    conn.execute(
        """
        create virtual table search_index using fts5(
            post_id unindexed,
            kind unindexed,
            body,
            tokenize='trigram'
        )
        """
    )
    insert_sql = "insert into search_index(post_id, kind, body) values (?,?,?)"
    total = 0
    for sql, kind in [
        ("select id, content from posts where content != ''", "post"),
        ("select post_id, detail from comments where detail != ''", "comment"),
    ]:
        cursor = conn.execute(sql)
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            conn.executemany(insert_sql, ((pid, kind, body) for pid, body in rows))
            total += len(rows)
            if total % (batch_size * 20) == 0:
                print(f"[migrate] indexed search rows={total:,}", flush=True)
    conn.execute("insert into search_index(search_index) values ('optimize')")
    return total


def copy_intersection(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
    batch_size: int,
) -> None:
    src_cols = table_columns(conn, "src", table)
    selected = [col for col in columns if col in src_cols]
    if selected != columns:
        missing = [col for col in columns if col not in src_cols]
        raise RuntimeError(f"src.{table} missing required columns: {missing}")
    col_sql = ",".join(columns)
    placeholders = ",".join("?" for _ in columns)
    insert_sql = f"insert into {table}({col_sql}) values ({placeholders})"
    cursor = conn.execute(f"select {col_sql} from src.{table}")
    copied = 0
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        conn.executemany(insert_sql, rows)
        copied += len(rows)
        if copied % (batch_size * 10) == 0:
            print(f"[migrate] copied {table}={copied:,}", flush=True)
    print(f"[migrate] copied {table}={copied:,}", flush=True)


def build_compacted(source: Path, target: Path, batch_size: int) -> dict:
    source = source.resolve()
    target = target.resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.unlink(missing_ok=True)

    started = time.time()
    conn = connect(tmp)
    try:
        init_schema(conn)
        conn.execute("attach database ? as src", (str(source),))
        print("[migrate] copying posts without show_user_head/views/hot", flush=True)
        copy_intersection(conn, "posts", POST_COLUMNS, batch_size)
        print("[migrate] copying comments without reply_comment_list", flush=True)
        copy_intersection(conn, "comments", COMMENT_COLUMNS, batch_size)
        if table_exists(conn, "src", "crawl_state"):
            print("[migrate] copying crawl_state", flush=True)
            conn.execute("insert into crawl_state select * from src.crawl_state")
        conn.commit()
        conn.execute("detach database src")

        print("[migrate] creating b-tree indexes", flush=True)
        create_indexes(conn)
        conn.commit()
        print("[migrate] rebuilding search_index", flush=True)
        search_rows = rebuild_search_index(conn, batch_size)
        conn.commit()
        stats = {
            "posts": db_count(conn, "posts"),
            "comments": db_count(conn, "comments"),
            "search_index": search_rows,
            "quick_check": conn.execute("pragma quick_check").fetchone()[0],
        }
    finally:
        conn.close()

    if target.exists():
        raise FileExistsError(f"target already exists: {target}")
    os.replace(tmp, target)
    stats["size_bytes"] = target.stat().st_size
    stats["elapsed_sec"] = round(time.time() - started, 2)
    return stats


def sidecar_meta(path: Path, expected_schema: str) -> dict:
    if not path or not path.exists():
        return {"exists": False}
    conn = sqlite3.connect(path)
    try:
        meta = dict(conn.execute("select key, value from index_meta").fetchall())
        return {
            "exists": True,
            "schema_version": meta.get("schema_version"),
            "source_rows": int(meta.get("source_rows", "0") or 0),
            "ok": meta.get("schema_version") == expected_schema,
        }
    finally:
        conn.close()


def inspect_db(
    db: Path,
    bigram: Path | None,
    symbol: Path | None,
    *,
    quick_check: bool = False,
) -> dict:
    conn = sqlite3.connect(db)
    try:
        post_cols = table_columns(conn, "main", "posts")
        comment_cols = table_columns(conn, "main", "comments")
        stats = {
            "db": str(db),
            "size_bytes": db.stat().st_size,
            "posts": db_count(conn, "posts"),
            "comments": db_count(conn, "comments"),
            "searchable_rows": int(
                conn.execute(
                    """
                    select
                        (select count(*) from posts where content != '') +
                        (select count(*) from comments where detail != '')
                    """
                ).fetchone()[0]
            ),
            "post_columns": sorted(post_cols),
            "comment_columns": sorted(comment_cols),
            "has_search_index": table_exists(conn, "main", "search_index"),
            "legacy_post_columns": sorted(post_cols & {"show_user_head", "views", "hot"}),
            "legacy_comment_columns": sorted(comment_cols & {"reply_comment_list"}),
        }
        if quick_check:
            print("[inspect] running pragma quick_check", flush=True)
            with Heartbeat("pragma quick_check"):
                stats["quick_check"] = conn.execute("pragma quick_check").fetchone()[0]
        if stats["has_search_index"]:
            stats["search_index"] = db_count(conn, "search_index")
    finally:
        conn.close()
    expected_rows = stats["searchable_rows"]
    if bigram:
        stats["bigram"] = sidecar_meta(bigram, "bigram-v1")
        stats["bigram"]["expected_source_rows"] = expected_rows
    if symbol:
        stats["symbol"] = sidecar_meta(symbol, "symbol-v1")
        stats["symbol"]["expected_source_rows"] = expected_rows
    return stats


def print_kv(stats: dict, prefix: str = "") -> None:
    for key, value in stats.items():
        print(f"{prefix}{key}: {value}")


def command_plan(args) -> int:
    stats = inspect_db(args.db, args.bigram, args.symbol, quick_check=args.quick_check)
    print_kv(stats)
    usage = shutil.disk_usage(args.db.parent)
    print(f"volume_free_bytes: {usage.free}")
    print(f"estimated_extra_bytes_needed: {int(stats['size_bytes'] * 1.1)}")
    if usage.free < stats["size_bytes"]:
        print("[warn] free space is less than source DB size; migrate may fail")
    return 0


def command_migrate(args) -> int:
    stats = build_compacted(args.db, args.out, args.batch_size)
    print_kv(stats)
    return 0


def command_verify(args) -> int:
    stats = inspect_db(args.db, args.bigram, args.symbol, quick_check=True)
    print_kv(stats)
    if stats["quick_check"] != "ok":
        raise RuntimeError(f"quick_check failed: {stats['quick_check']}")
    if stats["legacy_post_columns"] or stats["legacy_comment_columns"]:
        raise RuntimeError("legacy columns still present")
    expected = stats["searchable_rows"]
    if stats.get("search_index") != expected:
        raise RuntimeError(
            f"search_index rows {stats.get('search_index')} != searchable rows {expected}"
        )
    for name in ("bigram", "symbol"):
        meta = stats.get(name)
        if meta and meta.get("exists") and meta.get("source_rows") not in (0, expected):
            print(
                f"[warn] {name} source_rows={meta.get('source_rows')} "
                f"but searchable_rows={expected}; rebuild sidecar if search looks stale"
            )
    return 0


def command_swap(args) -> int:
    db = args.db.resolve()
    next_db = args.next.resolve()
    if not next_db.exists():
        raise FileNotFoundError(next_db)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = args.backup or db.with_name(f"{db.stem}.before-{stamp}{db.suffix}")
    if backup.exists():
        raise FileExistsError(backup)
    os.replace(db, backup)
    os.replace(next_db, db)
    print(f"backup: {backup}")
    print(f"active: {db}")
    print("[note] restart the Railway service so existing SQLite connections reopen")
    return 0


def command_rollback(args) -> int:
    db = args.db.resolve()
    backup = args.backup.resolve()
    if not backup.exists():
        raise FileNotFoundError(backup)
    failed = db.with_name(f"{db.stem}.failed-{time.strftime('%Y%m%d-%H%M%S')}{db.suffix}")
    if db.exists():
        os.replace(db, failed)
        print(f"failed_active: {failed}")
    os.replace(backup, db)
    print(f"restored: {db}")
    print("[note] restart the Railway service so existing SQLite connections reopen")
    return 0


def command_rebuild_sidecars(args) -> int:
    if args.bigram_out:
        print(f"[sidecar] building bigram: {args.bigram_out}", flush=True)
        stats = build_bigram_index(args.db, args.bigram_out)
        print(
            f"bigram_rows={stats.rows:,} "
            f"bigram_size_bytes={args.bigram_out.stat().st_size} "
            f"elapsed_sec={stats.elapsed_seconds:.2f}",
            flush=True,
        )
    if args.symbol_out:
        print(f"[sidecar] building symbol: {args.symbol_out}", flush=True)
        stats = build_symbol_index(args.db, args.symbol_out)
        print(
            f"symbol_source_rows={stats.source_rows:,} "
            f"symbol_rows={stats.rows:,} "
            f"symbol_size_bytes={args.symbol_out.stat().st_size} "
            f"elapsed_sec={stats.elapsed_seconds:.2f}",
            flush=True,
        )
    return 0


def swap_one(active: Path, next_path: Path, label: str) -> None:
    if not next_path.exists():
        raise FileNotFoundError(next_path)
    if not active.exists():
        raise FileNotFoundError(active)
    backup = active.with_name(
        f"{active.stem}.before-{time.strftime('%Y%m%d-%H%M%S')}{active.suffix}"
    )
    os.replace(active, backup)
    os.replace(next_path, active)
    print(f"{label}_backup: {backup}")
    print(f"{label}_active: {active}")


def command_swap_sidecars(args) -> int:
    if args.bigram and args.bigram_next:
        swap_one(args.bigram.resolve(), args.bigram_next.resolve(), "bigram")
    if args.symbol and args.symbol_next:
        swap_one(args.symbol.resolve(), args.symbol_next.resolve(), "symbol")
    print("[note] restart the Railway service so existing SQLite connections reopen")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=Path("data/posts.db"))
    common.add_argument("--bigram", type=Path, default=None)
    common.add_argument("--symbol", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", parents=[common])
    plan.add_argument(
        "--quick-check",
        action="store_true",
        help="also run pragma quick_check during plan; verify always runs it",
    )
    migrate = sub.add_parser("migrate", parents=[common])
    migrate.add_argument("--out", type=Path, required=True)
    migrate.add_argument("--batch-size", type=int, default=20000)
    sub.add_parser("verify", parents=[common])
    swap = sub.add_parser("swap", parents=[common])
    swap.add_argument("--next", type=Path, required=True)
    swap.add_argument("--backup", type=Path, default=None)
    rollback = sub.add_parser("rollback", parents=[common])
    rollback.add_argument("--backup", type=Path, required=True)
    sidecars = sub.add_parser("rebuild-sidecars")
    sidecars.add_argument("--db", type=Path, required=True)
    sidecars.add_argument("--bigram-out", type=Path, default=None)
    sidecars.add_argument("--symbol-out", type=Path, default=None)
    swap_sidecars = sub.add_parser("swap-sidecars")
    swap_sidecars.add_argument("--bigram", type=Path, default=None)
    swap_sidecars.add_argument("--bigram-next", type=Path, default=None)
    swap_sidecars.add_argument("--symbol", type=Path, default=None)
    swap_sidecars.add_argument("--symbol-next", type=Path, default=None)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {
        "plan": command_plan,
        "migrate": command_migrate,
        "verify": command_verify,
        "swap": command_swap,
        "rollback": command_rollback,
        "rebuild-sidecars": command_rebuild_sidecars,
        "swap-sidecars": command_swap_sidecars,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
