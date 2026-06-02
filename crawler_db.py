#!/usr/bin/env python3
"""DB-first crawler entrypoint.

This is the migration-safe crawler path: it writes normalized posts/comments
directly into SQLite through storage.sqlite_store. Legacy CSV crawlers remain in
place for rollback/reference.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time
from pathlib import Path

import requests
import urllib3

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storage.sqlite_store import SQLitePostStore, safe_int

urllib3.disable_warnings()

BASE = "https://ys.qimiaoyuanfen.com"
CID = 4
DATA_DIR = ROOT / "data"
DEFAULT_DB = DATA_DIR / "posts.db"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "MicroMessenger/7.0.20.1781 MiniProgramEnv/Windows WindowsWechat/WMPF"
    ),
    "Referer": "https://servicewechat.com/wxe23b94e06f71e89a/141/page-frame.html",
    "Xweb-Xhr": "1",
    "Accept": "application/json",
}


def load_cookie(config_path: Path) -> str:
    if not config_path.exists():
        raise FileNotFoundError(f"missing cookie config: {config_path}")
    for line in config_path.read_text(encoding="utf-8").splitlines():
        if "ys7_ysxy_session=" in line:
            return line.strip().split("=", 1)[1]
    raise RuntimeError(f"cookie not found in {config_path}")


def make_session(cookie: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.set("ys7_ysxy_session", cookie)
    session.verify = False
    return session


def api_get(session: requests.Session, path: str, params: dict | None = None) -> tuple[dict | None, str | None]:
    try:
        response = session.get(f"{BASE}{path}", params=params, timeout=15, verify=False)
        payload = response.json()
    except Exception as exc:
        return None, str(exc)

    code = payload.get("code")
    if code == "0000":
        return payload.get("data", {}), None
    if code == "1000":
        return None, "cookie_expired"
    if code == "0102":
        return None, "not_found"
    return None, f"code={code} {payload.get('message', '')}"


def normalize_detail(post_id: str, data: dict) -> tuple[dict, list[dict]] | None:
    if str(data.get("community_id", "")) != str(CID):
        return None
    comments = data.get("comment_list", [])
    if not isinstance(comments, list):
        comments = []
    content = f"{data.get('title') or ''} {data.get('detail') or ''}".strip()
    post = {
        "id": str(post_id),
        "content": content,
        "category_name": data.get("category_name", ""),
        "user_name": data.get("show_user_name", ""),
        "show_user_id": data.get("show_user_id", ""),
        "show_user_head": data.get("show_user_head", ""),
        "real_user_id": data.get("real_user_id", 0),
        "create_time": data.get("create_time", ""),
        "comment_count": safe_int(data.get("count_comment")),
        "star_count": safe_int(data.get("count_star")),
        "trace_count": safe_int(data.get("count_trace")),
        "views": safe_int(data.get("views")),
        "hot": safe_int(data.get("hot")),
    }
    return post, comments


def fetch_detail(session: requests.Session, post_id: str) -> tuple[dict, list[dict]] | None:
    data, err = api_get(session, "/article/article/info", {"community_id": CID, "id": post_id})
    if err or not data:
        return None
    return normalize_detail(str(post_id), data)


def csv_row_to_post(row: dict) -> tuple[dict, list[dict]] | None:
    post_id = str(row.get("id") or "")
    if not post_id or not post_id.isdigit():
        return None
    try:
        comments = json.loads(row.get("comments_json") or "[]")
    except Exception:
        comments = []
    if not isinstance(comments, list):
        comments = []
    post = {
        "id": post_id,
        "content": row.get("content", ""),
        "category_name": row.get("category_name", ""),
        "user_name": row.get("user_name", ""),
        "show_user_id": row.get("show_user_id", ""),
        "show_user_head": row.get("show_user_head", ""),
        "real_user_id": row.get("real_user_id", "0"),
        "create_time": row.get("create_time", ""),
        "comment_count": safe_int(row.get("comment_count")),
        "star_count": safe_int(row.get("star_count")),
        "trace_count": safe_int(row.get("trace_count")),
        "views": safe_int(row.get("views")),
        "hot": safe_int(row.get("hot")),
    }
    return post, comments


def command_mock_csv(args: argparse.Namespace) -> int:
    csv.field_size_limit(10 ** 9)
    written = 0
    skipped = 0
    with SQLitePostStore(args.db_path) as store:
        if args.init_schema:
            store.init_schema()
        with Path(args.csv_path).open("r", encoding="utf-8", errors="replace", newline="") as f:
            for row in csv.DictReader(f):
                parsed = csv_row_to_post(row)
                if parsed is None:
                    skipped += 1
                    continue
                post, comments = parsed
                store.upsert_post(post, comments, commit=False)
                written += 1
                if written % args.batch_size == 0:
                    store.conn.commit()
                    print(f"[mock-csv] written={written:,}", flush=True)
                if args.limit and written >= args.limit:
                    break
        store.set_state("crawler_db_mock_csv", json.dumps({"csv": args.csv_path, "written": written, "skipped": skipped}, ensure_ascii=False), commit=False)
        store.conn.commit()
    print(f"[mock-csv] done written={written:,} skipped={skipped:,} db={args.db_path}")
    return 0


def command_detail_fill(args: argparse.Namespace) -> int:
    cookie = load_cookie(Path(args.config))
    session = make_session(cookie)
    ids = []
    for token in args.ids.replace(",", " ").split():
        if token.strip():
            ids.append(token.strip())
    if not ids:
        raise RuntimeError("no ids provided")

    written = 0
    misses = 0
    with SQLitePostStore(args.db_path) as store:
        if args.init_schema:
            store.init_schema()
        for idx, post_id in enumerate(ids, 1):
            time.sleep(random.uniform(args.min_delay, args.max_delay))
            parsed = fetch_detail(session, post_id)
            if parsed is None:
                misses += 1
                print(f"[detail-fill] miss #{post_id}", flush=True)
                continue
            post, comments = parsed
            if args.dry_run:
                print(f"[detail-fill] dry #{post_id} c={post['comment_count']} {post['content'][:50]}", flush=True)
            else:
                store.upsert_post(post, comments, commit=False)
                written += 1
                if written % args.batch_size == 0:
                    store.conn.commit()
            if idx % 20 == 0:
                print(f"[detail-fill] progress {idx}/{len(ids)} written={written} miss={misses}", flush=True)
        if not args.dry_run:
            store.set_state("crawler_db_detail_fill", json.dumps({"ids": ids, "written": written, "misses": misses}, ensure_ascii=False), commit=False)
            store.conn.commit()
    print(f"[detail-fill] done written={written} misses={misses} dry_run={args.dry_run}")
    return 0


def command_incremental(args: argparse.Namespace) -> int:
    cookie = load_cookie(Path(args.config))
    session = make_session(cookie)
    stats = {"pages": 0, "seen": 0, "new": 0, "updated": 0, "unchanged": 0, "misses": 0, "details": 0}
    consecutive_unchanged = 0
    limit_reached = False

    with SQLitePostStore(args.db_path) as store:
        if args.init_schema:
            store.init_schema()
        for page in range(1, args.pages + 1):
            if limit_reached:
                break
            time.sleep(random.uniform(args.min_delay, args.max_delay))
            data, err = api_get(session, f"/article/article/{args.endpoint}", {"community_id": CID, "page": page})
            if err:
                print(f"[incremental] page={page} err={err}", flush=True)
                if err == "cookie_expired":
                    break
                continue
            articles = data.get("list", []) if data else []
            if not articles:
                print(f"[incremental] page={page} empty stop", flush=True)
                break
            stats["pages"] += 1
            page_new = 0
            page_updated = 0
            for article in articles:
                post_id = str(article.get("id") or "")
                if not post_id:
                    continue
                stats["seen"] += 1
                new_cc = safe_int(article.get("comment_count", article.get("count_comment", 0)))
                existing = store.get_post_counts(post_id)
                needs_detail = existing is None or existing[0] != new_cc
                if not needs_detail:
                    stats["unchanged"] += 1
                    consecutive_unchanged += 1
                    continue
                if args.max_details and stats["details"] >= args.max_details:
                    limit_reached = True
                    break
                parsed = fetch_detail(session, post_id)
                if parsed is None:
                    stats["misses"] += 1
                    continue
                post, comments = parsed
                stats["details"] += 1
                if args.dry_run:
                    action = "new" if existing is None else "update"
                    print(f"[incremental] dry {action} #{post_id} c={post['comment_count']} {post['content'][:50]}", flush=True)
                else:
                    store.upsert_post(post, comments, commit=False)
                if existing is None:
                    stats["new"] += 1
                    page_new += 1
                else:
                    stats["updated"] += 1
                    page_updated += 1
                consecutive_unchanged = 0
            if not args.dry_run:
                store.conn.commit()
            print(f"[incremental:{args.endpoint}] page={page} articles={len(articles)} new={page_new} updated={page_updated} unchanged_run={consecutive_unchanged}", flush=True)
            if consecutive_unchanged >= args.stop_unchanged and page >= args.min_pages:
                print(f"[incremental] stop unchanged_run={consecutive_unchanged}", flush=True)
                break
        if not args.dry_run:
            store.set_state("crawler_db_incremental", json.dumps(stats, ensure_ascii=False), commit=True)
    print("[incremental] done", json.dumps(stats, ensure_ascii=False), "dry_run=", args.dry_run)
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db-path", default=str(DEFAULT_DB))
    parser.add_argument("--init-schema", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)


def main() -> int:
    parser = argparse.ArgumentParser(description="DB-first crawler entrypoint")
    sub = parser.add_subparsers(dest="command", required=True)

    mock = sub.add_parser("mock-csv", help="write sample rows from CSV into DB for local tests")
    add_common(mock)
    mock.add_argument("--csv-path", default=str(DATA_DIR / "posts_final.csv"))
    mock.add_argument("--limit", type=int, default=100)
    mock.set_defaults(func=command_mock_csv)

    detail = sub.add_parser("detail-fill", help="fetch detail for explicit ids and upsert DB")
    add_common(detail)
    detail.add_argument("--config", default=str(DATA_DIR / "config.txt"))
    detail.add_argument("--ids", required=True)
    detail.add_argument("--dry-run", action="store_true")
    detail.add_argument("--min-delay", type=float, default=0.8)
    detail.add_argument("--max-delay", type=float, default=2.0)
    detail.set_defaults(func=command_detail_fill)

    inc = sub.add_parser("incremental", help="scan recent lists2 pages and upsert changed posts")
    add_common(inc)
    inc.add_argument("--config", default=str(DATA_DIR / "config.txt"))
    inc.add_argument("--endpoint", choices=("lists", "lists2"), default="lists2", help="lists for newest posts; lists2 for activity/comment refresh")
    inc.add_argument("--pages", type=int, default=3)
    inc.add_argument("--min-pages", type=int, default=3)
    inc.add_argument("--stop-unchanged", type=int, default=10)
    inc.add_argument("--max-details", type=int, default=0, help="stop after fetching this many detail records; 0 means unlimited")
    inc.add_argument("--dry-run", action="store_true")
    inc.add_argument("--min-delay", type=float, default=0.3)
    inc.add_argument("--max-delay", type=float, default=0.8)
    inc.set_defaults(func=command_incremental)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
