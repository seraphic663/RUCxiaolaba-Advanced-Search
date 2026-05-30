#!/bin/bash
# Railway start script: download data with Python stdlib, then start server.
set -e

DATA_URL="${DATA_URL:-https://github.com/seraphic663/RUCxiaolaba-Advanced-Search/releases/download/data-1/posts_scan.csv.gz}"

if [ ! -f data/posts_scan.csv ]; then
  echo "[boot] Downloading data..."
  mkdir -p data
  python - <<'PY'
import gzip
import os
import sys
import urllib.request

url = os.environ.get("DATA_URL", "https://github.com/seraphic663/RUCxiaolaba-Advanced-Search/releases/download/data-1/posts_scan.csv.gz")
tmp_path = "data/posts_scan.csv.gz"
out_path = "data/posts_scan.csv"

try:
    with urllib.request.urlopen(url, timeout=120) as response:
        with open(tmp_path, "wb") as f:
            f.write(response.read())
    with gzip.open(tmp_path, "rb") as src, open(out_path, "wb") as dst:
        dst.write(src.read())
finally:
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
    sys.exit("[boot] Data download produced an empty file")
PY
  echo "[boot] Data loaded ($(wc -c < data/posts_scan.csv) bytes)"
else
  echo "[boot] Using existing data"
fi

exec python -u server.py
