"""Add indexes used by exact-match admin ID and nickname search."""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

INDEXES = [
    "create index if not exists idx_posts_show_user_id on posts(show_user_id)",
    "create index if not exists idx_posts_real_user_id on posts(real_user_id)",
    "create index if not exists idx_posts_user_name_lower on posts(lower(user_name))",
    "create index if not exists idx_comments_show_user_id on comments(show_user_id)",
    "create index if not exists idx_comments_real_user_id on comments(real_user_id)",
    "create index if not exists idx_comments_reply_show_user_id on comments(reply_show_user_id)",
    "create index if not exists idx_comments_show_user_name_lower on comments(lower(show_user_name))",
    "create index if not exists idx_comments_reply_user_name_lower on comments(lower(reply_show_user_name))",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default="data/posts.db")
    args = parser.parse_args()
    path = Path(args.db_path)
    if not path.exists():
        parser.error(f"database not found: {path}")

    started = time.perf_counter()
    conn = sqlite3.connect(path)
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma synchronous=normal")
    for sql in INDEXES:
        name = sql.split()[5]
        step = time.perf_counter()
        conn.execute(sql)
        conn.commit()
        print(f"[index] {name} {time.perf_counter() - step:.2f}s", flush=True)
    conn.execute("analyze")
    conn.commit()
    conn.close()
    print(f"[index] done {time.perf_counter() - started:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
