#!/bin/bash
# Railway start script: DB-only mode. The SQLite DB must already exist on the
# mounted volume, usually /app/data/posts.db.
set -e

DB_PATH="${SQLITE_DB:-/app/data/posts.db}"

if [ ! -f "$DB_PATH" ]; then
  echo "[boot] SQLite DB not found: $DB_PATH"
  echo "[boot] Upload data/posts.db to the Railway volume as /app/data/posts.db first."
  exit 1
fi

python - <<'PY'
import os
import sqlite3
import sys

path = os.environ.get("SQLITE_DB", "/app/data/posts.db")
try:
    conn = sqlite3.connect(path)
    row = conn.execute("select count(*), max(nullif(create_time, '')) from posts").fetchone()
    conn.close()
except Exception as exc:
    sys.exit(f"[boot] Invalid SQLite DB {path}: {exc}")

print(f"[boot] Using SQLite DB: {path}")
print(f"[boot] posts={row[0]:,} latest={row[1]}")
PY

exec python -u server.py --db --sqlite-db "$DB_PATH"
