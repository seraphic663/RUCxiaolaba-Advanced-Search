#!/usr/bin/env python3
"""Slim comment JSON payloads by keeping only the nested reply_comment_list.

This reduces reply_comment_list from ~836 bytes/row to ~120 bytes/row (only for
the 2.2% of comments that actually have nested replies), saving ~1.76 GB.

Usage:
    python scripts/migrate_slim_raw_json.py --db-path data/posts.db
    python scripts/migrate_slim_raw_json.py --db-path data/posts.db --dry-run

The script supports both schemas:
- old comments.raw_json
- new comments.reply_comment_list
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

PREFERRED_COLUMNS = ("reply_comment_list", "raw_json")


def detect_column(conn: sqlite3.Connection) -> str:
    cols = {row[1] for row in conn.execute("pragma table_info(comments)")}
    for col in PREFERRED_COLUMNS:
        if col in cols:
            return col
    raise RuntimeError("comments must contain reply_comment_list or raw_json")


def slim(data_str: str) -> tuple[str, int, int]:
    """Return (new_json, old_bytes, new_bytes). Keeps only reply_comment_list."""
    old_bytes = len(data_str.encode("utf-8"))
    try:
        obj = json.loads(data_str)
    except json.JSONDecodeError:
        return (data_str, old_bytes, old_bytes)

    slim_obj: dict = {}
    rcl = obj.get("reply_comment_list")
    if rcl:
        slim_obj["reply_comment_list"] = rcl

    new_str = json.dumps(slim_obj, ensure_ascii=False, separators=(",", ":"))
    new_bytes = len(new_str.encode("utf-8"))
    return (new_str, old_bytes, new_bytes)


def migrate(db_path: Path, batch_size: int = 5000, dry_run: bool = False) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(db_path)

    t0 = time.time()
    conn = sqlite3.connect(str(db_path))
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma synchronous=normal")

    column = detect_column(conn)
    print(f"[migrate] column: {column}")
    total = conn.execute(f"select count(*) from comments where {column} != ''").fetchone()[0]
    print(f"[migrate] comments to process: {total:,}")
    print(f"[migrate] batch size: {batch_size}")
    print(f"[migrate] dry run: {dry_run}")
    print()

    stats = {
        "total": total,
        "processed": 0,
        "kept_reply_list": 0,
        "emptied": 0,
        "old_bytes": 0,
        "new_bytes": 0,
        "errors": 0,
    }

    cursor = conn.execute(f"select row_key, {column} from comments where {column} != ''")
    batch = []
    last_report = t0

    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break

        for row_key, raw in rows:
            new_raw, old_b, new_b = slim(raw)
            stats["old_bytes"] += old_b
            stats["new_bytes"] += new_b
            stats["processed"] += 1
            if new_b > 2:
                stats["kept_reply_list"] += 1
            else:
                stats["emptied"] += 1
            batch.append((new_raw, row_key))

        if not dry_run:
            conn.executemany(f"update comments set {column} = ? where row_key = ?", batch)
            conn.commit()

        batch.clear()

        if time.time() - last_report > 30:
            elapsed = time.time() - t0
            pct = stats["processed"] / total * 100
            rate = stats["processed"] / elapsed
            eta = (total - stats["processed"]) / rate if rate > 0 else 0
            saved = stats["old_bytes"] - stats["new_bytes"]
            print(
                f"[migrate] {stats['processed']:,}/{total:,} ({pct:.1f}%) "
                f"rate={rate:.0f}/s saved={saved/1024/1024:.1f}MB "
                f"eta={eta/60:.0f}min"
            )
            last_report = time.time()

    elapsed = time.time() - t0
    stats["elapsed_sec"] = round(elapsed, 1)
    stats["saved_bytes"] = stats["old_bytes"] - stats["new_bytes"]
    stats["saved_mb"] = round(stats["saved_bytes"] / 1024 / 1024, 1)

    print()
    print(f"[migrate] done in {elapsed:.0f}s")
    print(f"  processed:    {stats['processed']:,}")
    print(f"  kept_reply:   {stats['kept_reply_list']:,} ({stats['kept_reply_list']/max(total,1)*100:.1f}%)")
    print(f"  emptied:      {stats['emptied']:,} ({stats['emptied']/max(total,1)*100:.1f}%)")
    print(f"  errors:       {stats['errors']}")
    print(f"  old size:     {stats['old_bytes']/1024/1024/1024:.2f} GB")
    print(f"  new size:     {stats['new_bytes']/1024/1024/1024:.2f} GB")
    print(f"  saved:        {stats['saved_mb']} MB ({stats['saved_bytes']/1024/1024/1024:.2f} GB)")

    conn.close()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Slim comment JSON payloads by keeping only nested replies"
    )
    parser.add_argument("--db-path", default="data/posts.db")
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db_path)
    stats = migrate(db_path, args.batch_size, args.dry_run)
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
