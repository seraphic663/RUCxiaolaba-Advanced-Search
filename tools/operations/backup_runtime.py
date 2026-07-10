#!/usr/bin/env python3
"""Create operator-requested backups for crawler configuration and SQLite data.

Default mode backs up only small mutable site files. Use --include-db only when
there is enough free volume space for a full SQLite copy.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import time
from pathlib import Path

SMALL_FILES = ("config.txt",)


def copy_if_exists(source: Path, target: Path) -> None:
    if source.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def backup_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def prune_old_backups(backups_dir: Path, keep: int) -> None:
    if keep <= 0 or not backups_dir.exists():
        return
    dirs = sorted([p for p in backups_dir.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True)
    for old in dirs[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up Railway/runtime data files")
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "data"))
    parser.add_argument("--backup-dir", default=None)
    parser.add_argument("--include-db", action="store_true", help="also make an online SQLite DB backup")
    parser.add_argument("--db-name", default="posts.db")
    parser.add_argument("--keep", type=int, default=24, help="number of timestamped backup folders to keep")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    backups_dir = Path(args.backup_dir) if args.backup_dir else data_dir / "backups"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target_dir = backups_dir / stamp
    target_dir.mkdir(parents=True, exist_ok=True)

    for name in SMALL_FILES:
        copy_if_exists(data_dir / name, target_dir / name)

    if args.include_db:
        source_db = data_dir / args.db_name
        if not source_db.exists():
            raise FileNotFoundError(source_db)
        backup_sqlite(source_db, target_dir / args.db_name)

    prune_old_backups(backups_dir, args.keep)
    print(f"backup written: {target_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
