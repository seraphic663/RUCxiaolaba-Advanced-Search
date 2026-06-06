#!/usr/bin/env python3
"""DB-first crawler entrypoint.

This is the migration-safe crawler path: it writes normalized posts/comments
directly into SQLite through storage.sqlite_store.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
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
DEFAULT_LOCK_TIMEOUT = 180
STALE_LOCK_SECONDS = 6 * 60 * 60

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


def latest_list_id(session: requests.Session) -> int:
    data, err = api_get(session, "/article/article/lists", {"community_id": CID, "page": 1})
    if err:
        raise RuntimeError(f"cannot determine latest id: {err}")
    ids = [safe_int(item.get("id")) for item in (data or {}).get("list", [])]
    latest = max(ids, default=0)
    if latest <= 0:
        raise RuntimeError("cannot determine latest id from lists page 1")
    return latest


@contextmanager
def db_write_lock(db_path: str | Path, timeout: int = DEFAULT_LOCK_TIMEOUT):
    lock_path = Path(str(db_path) + ".crawler.lock")
    deadline = time.time() + timeout
    fd = None

    def remove_stale_lock() -> bool:
        try:
            owner_pid = int(lock_path.read_text(encoding="ascii").strip())
        except (FileNotFoundError, OSError, ValueError):
            owner_pid = 0

        owner_alive = False
        if owner_pid > 0:
            try:
                os.kill(owner_pid, 0)
                owner_alive = True
            except PermissionError:
                owner_alive = True
            except OSError:
                pass

        try:
            lock_age = time.time() - lock_path.stat().st_mtime
        except FileNotFoundError:
            return True

        if owner_alive:
            return False
        if owner_pid <= 0 and lock_age < STALE_LOCK_SECONDS:
            return False
        try:
            lock_path.unlink()
            print(
                f"[lock] removed stale lock pid={owner_pid} age={int(lock_age)}s "
                f"path={lock_path}",
                flush=True,
            )
            return True
        except FileNotFoundError:
            return True

    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
            break
        except FileExistsError:
            if remove_stale_lock():
                continue
            if time.time() >= deadline:
                raise TimeoutError(f"crawler lock timeout: {lock_path}")
            time.sleep(2)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


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
    with db_write_lock(args.db_path, args.lock_timeout):
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

    with db_write_lock(args.db_path, args.lock_timeout):
        with SQLitePostStore(args.db_path) as store:
            if args.init_schema:
                store.init_schema()
            end_page = args.start_page + args.pages
            for page in range(args.start_page, end_page):
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
                print(f"[{args.command}:{args.endpoint}] page={page} articles={len(articles)} new={page_new} updated={page_updated} unchanged_run={consecutive_unchanged}", flush=True)
                if consecutive_unchanged >= args.stop_unchanged and stats["pages"] >= args.min_pages:
                    print(f"[incremental] stop unchanged_run={consecutive_unchanged}", flush=True)
                    break
            if not args.dry_run:
                store.set_state("crawler_db_incremental", json.dumps(stats, ensure_ascii=False), commit=True)
    print("[incremental] done", json.dumps(stats, ensure_ascii=False), "dry_run=", args.dry_run)
    return 0


def command_id_scan(args: argparse.Namespace) -> int:
    cookie = load_cookie(Path(args.config))
    probe_session = make_session(cookie)
    state_key = args.state_key or f"crawler_db_id_scan_{args.from_date.replace('-', '')}"

    with SQLitePostStore(args.db_path) as state_store:
        row = state_store.conn.execute(
            "select value from crawl_state where key=?", (state_key,)
        ).fetchone()
        saved = json.loads(row[0]) if row else {}
        if saved.get("complete") and not args.force:
            print(f"[id-scan] already complete state={state_key}", flush=True)
            return 0

        start_id = safe_int(args.start_id)
        if start_id <= 0:
            start_row = state_store.conn.execute(
                """
                select min(cast(id as integer))
                from posts
                where create_time >= ?
                """,
                (f"{args.from_date} 00:00:00",),
            ).fetchone()
            start_id = safe_int(start_row[0] if start_row else 0)
        if start_id <= 0:
            raise RuntimeError(f"cannot determine start id for {args.from_date}")
        start_id = max(1, start_id - max(0, args.id_margin))

        end_id = safe_int(args.end_id) or safe_int(saved.get("end_id"))
        if end_id <= 0:
            end_id = latest_list_id(probe_session) + max(0, args.id_margin)
        next_id = start_id
        if args.resume and not args.force:
            next_id = max(start_id, safe_int(saved.get("next_id"), start_id))

    if next_id > end_id:
        print(f"[id-scan] empty range {next_id}>{end_id}", flush=True)
        return 0

    print(
        f"[id-scan] range={next_id}..{end_id} from_date={args.from_date} "
        f"workers={args.workers} chunk={args.chunk_size} state={state_key}",
        flush=True,
    )

    local = threading.local()

    def scan_one(post_id: int):
        if not hasattr(local, "session"):
            local.session = make_session(cookie)
        time.sleep(random.uniform(args.min_delay, args.max_delay))
        last_error = ""
        for attempt in range(args.retries + 1):
            data, err = api_get(
                local.session,
                "/article/article/info",
                {"community_id": CID, "id": str(post_id)},
            )
            if err == "cookie_expired":
                return post_id, None, "cookie_expired"
            if err == "not_found" or not data:
                return post_id, None, "missing"
            if err:
                last_error = err
                if attempt < args.retries:
                    time.sleep(1.0 + attempt)
                    continue
                return post_id, None, f"error:{last_error}"
            parsed = normalize_detail(str(post_id), data)
            if parsed is None:
                return post_id, None, "foreign"
            return post_id, parsed, "ok"
        return post_id, None, f"error:{last_error}"

    stats = {
        "start_id": start_id,
        "end_id": end_id,
        "next_id": next_id,
        "processed": 0,
        "written": 0,
        "missing": 0,
        "foreign": 0,
        "errors": 0,
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    with db_write_lock(args.db_path, args.lock_timeout):
        with SQLitePostStore(args.db_path) as store:
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                chunk_start = next_id
                while chunk_start <= end_id:
                    chunk_end = min(end_id, chunk_start + args.chunk_size - 1)
                    results = executor.map(scan_one, range(chunk_start, chunk_end + 1))
                    cookie_expired = False
                    for post_id, parsed, status in results:
                        stats["processed"] += 1
                        if status == "ok":
                            post, comments = parsed
                            if post["create_time"] >= f"{args.from_date} 00:00:00":
                                if not args.dry_run:
                                    store.upsert_post(post, comments, commit=False)
                                stats["written"] += 1
                            else:
                                stats["foreign"] += 1
                        elif status == "missing":
                            stats["missing"] += 1
                        elif status == "foreign":
                            stats["foreign"] += 1
                        elif status == "cookie_expired":
                            cookie_expired = True
                            stats["errors"] += 1
                        else:
                            stats["errors"] += 1

                    stats["next_id"] = chunk_end + 1
                    state = {
                        **stats,
                        "complete": False,
                        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    if not args.dry_run:
                        store.set_state(state_key, json.dumps(state, ensure_ascii=False), commit=False)
                        store.conn.commit()
                    print(
                        f"[id-scan] {chunk_start}..{chunk_end} "
                        f"processed={stats['processed']} written={stats['written']} "
                        f"missing={stats['missing']} foreign={stats['foreign']} "
                        f"errors={stats['errors']}",
                        flush=True,
                    )
                    if cookie_expired:
                        raise RuntimeError("cookie_expired")
                    chunk_start = chunk_end + 1

            final_state = {
                **stats,
                "next_id": end_id + 1,
                "complete": True,
                "completed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            if not args.dry_run:
                store.set_state(state_key, json.dumps(final_state, ensure_ascii=False), commit=True)
    print("[id-scan] done", json.dumps(final_state, ensure_ascii=False), flush=True)
    return 0


def command_new(args: argparse.Namespace) -> int:
    args.endpoint = "lists"
    return command_incremental(args)


def command_refresh(args: argparse.Namespace) -> int:
    args.endpoint = "lists2"
    return command_incremental(args)


def command_backfill(args: argparse.Namespace) -> int:
    if args.start_page < 2 and not args.force_start_page:
        args.start_page = 2
    return command_incremental(args)


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db-path", default=str(DEFAULT_DB))
    parser.add_argument("--init-schema", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--lock-timeout", type=int, default=DEFAULT_LOCK_TIMEOUT)


def add_scan_options(parser: argparse.ArgumentParser, *, endpoint: str | None = None,
                     pages: int = 500, min_pages: int = 20, stop_unchanged: int = 300) -> None:
    add_common(parser)
    parser.add_argument("--config", default=str(DATA_DIR / "config.txt"))
    if endpoint is None:
        parser.add_argument("--endpoint", choices=("lists", "lists2"), default="lists2")
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--pages", type=int, default=pages)
    parser.add_argument("--min-pages", type=int, default=min_pages)
    parser.add_argument("--stop-unchanged", type=int, default=stop_unchanged)
    parser.add_argument("--max-details", type=int, default=0, help="stop after fetching this many detail records; 0 means unlimited")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-delay", type=float, default=0.3)
    parser.add_argument("--max-delay", type=float, default=0.8)


def main() -> int:
    parser = argparse.ArgumentParser(description="DB-first crawler entrypoint")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="scan newest post stream and upsert missing/changed posts")
    add_scan_options(new, endpoint="lists", pages=500, min_pages=20, stop_unchanged=300)
    new.set_defaults(func=command_new, endpoint="lists")

    refresh = sub.add_parser("refresh", help="scan active/comment stream and refresh changed posts")
    add_scan_options(refresh, endpoint="lists2", pages=500, min_pages=20, stop_unchanged=300)
    refresh.set_defaults(func=command_refresh, endpoint="lists2")

    backfill = sub.add_parser("backfill", help="scan older pages to fill historical gaps")
    add_scan_options(backfill, endpoint=None, pages=500, min_pages=20, stop_unchanged=600)
    backfill.add_argument("--force-start-page", action="store_true", help="allow start page 1 for explicit rechecks")
    backfill.set_defaults(func=command_backfill)

    detail = sub.add_parser("detail-fill", help="fetch detail for explicit ids and upsert DB")
    add_common(detail)
    detail.add_argument("--config", default=str(DATA_DIR / "config.txt"))
    detail.add_argument("--ids", required=True)
    detail.add_argument("--dry-run", action="store_true")
    detail.add_argument("--min-delay", type=float, default=0.8)
    detail.add_argument("--max-delay", type=float, default=2.0)
    detail.set_defaults(func=command_detail_fill)

    id_scan = sub.add_parser("id-scan", help="scan a continuous ID range with resume support")
    add_common(id_scan)
    id_scan.add_argument("--config", default=str(DATA_DIR / "config.txt"))
    id_scan.add_argument("--from-date", default="2026-06-01")
    id_scan.add_argument("--start-id", type=int, default=0)
    id_scan.add_argument("--end-id", type=int, default=0)
    id_scan.add_argument("--id-margin", type=int, default=100)
    id_scan.add_argument("--workers", type=int, default=4)
    id_scan.add_argument("--chunk-size", type=int, default=500)
    id_scan.add_argument("--retries", type=int, default=2)
    id_scan.add_argument("--state-key", default="")
    id_scan.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    id_scan.add_argument("--force", action="store_true")
    id_scan.add_argument("--dry-run", action="store_true")
    id_scan.add_argument("--min-delay", type=float, default=0.15)
    id_scan.add_argument("--max-delay", type=float, default=0.4)
    id_scan.set_defaults(func=command_id_scan)

    inc = sub.add_parser("incremental", help="compatibility alias: scan selected endpoint")
    add_scan_options(inc, endpoint=None, pages=500, min_pages=20, stop_unchanged=300)
    inc.set_defaults(func=command_incremental)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
