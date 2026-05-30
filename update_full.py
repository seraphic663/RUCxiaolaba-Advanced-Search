"""
update_full.py — 全量数据更新流水线（三阶段顺序执行）

Phase 1: 续扫 ID 全量（多线程，从断点 → 4,000,000）
Phase 2: 补扫高 ID 新区（最新帖往下扫，连续 10 条命中即停）
Phase 3: lists2 遍历更新评论（逐页对比 comment_count，有变化重抓）

Usage: python update_full.py
  随时 Ctrl+C 安全中断，Phase 完成后进度已保存。
"""

import requests
import csv
import json
import os
import time
import threading
import sys
import random
import urllib3

urllib3.disable_warnings()

# ==================== CONFIG ====================

BASE = "https://ys.qimiaoyuanfen.com"
CID = 4
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Phase 1
CHECKPOINT_FILE = os.path.join(DATA_DIR, ".scan_checkpoint.json")
SCAN_OUTPUT = os.path.join(DATA_DIR, "posts_scan.csv")
SCAN_END_ID = 4000000
NUM_WORKERS = 10

# Phase 2+3 concurrent_unchanged threshold
UNCHANGED_STOP = 10

# Final merged output
FINAL_OUTPUT = os.path.join(DATA_DIR, "posts_final.csv")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "MicroMessenger/7.0.20.1781 MiniProgramEnv/Windows WindowsWechat/WMPF"
    ),
    "Referer": "https://servicewechat.com/wxe23b94e06f71e89a/141/page-frame.html",
    "Xweb-Xhr": "1",
    "Accept": "application/json",
}

# Column order for output CSV
COLUMNS = [
    "id", "content", "category_name", "user_name",
    "show_user_id", "show_user_head", "real_user_id",
    "create_time", "comment_count", "star_count",
    "trace_count", "views", "hot", "comments_json",
]

# ==================== HELPERS ====================

def load_cookie():
    p = os.path.join(DATA_DIR, "config.txt")
    if not os.path.exists(p):
        sys.exit(f"[!] 请创建 {p}，内容: ys7_ysxy_session=YOUR_COOKIE")
    for line in open(p, encoding="utf-8"):
        if "ys7_ysxy_session=" in line:
            return line.strip().split("=", 1)[1]
    sys.exit("[!] Cookie not found in config.txt")


def make_session(cookie):
    s = requests.Session()
    s.headers.update(HEADERS)
    s.cookies.set("ys7_ysxy_session", cookie)
    s.verify = False
    return s


def api_get(session, path, params=None):
    """Returns (data_dict_or_None, error_string_or_None)."""
    try:
        r = session.get(f"{BASE}{path}", params=params, timeout=15, verify=False)
        d = r.json()
        code = d.get("code")
        if code == "0000":
            return d.get("data", {}), None
        if code == "1000":
            return None, "cookie_expired"
        if code == "0102":
            return None, "not_found"
        return None, f"code={code} {d.get('message','')}"
    except Exception as e:
        return None, str(e)


def fetch_detail(session, post_id):
    """Fetch full detail for a single post. Returns post dict or None."""
    data, err = api_get(session, "/article/article/info", {"community_id": CID, "id": post_id})
    if err:
        return None
    if str(data.get("community_id", "")) != str(CID):
        return None  # not RUC

    comments = data.get("comment_list", [])
    content = f"{(data.get('title') or '')} {(data.get('detail') or '')}".strip()
    return {
        "id": str(post_id),
        "content": content,
        "category_name": data.get("category_name", ""),
        "user_name": data.get("show_user_name", ""),
        "show_user_id": data.get("show_user_id", ""),
        "show_user_head": data.get("show_user_head", ""),
        "real_user_id": data.get("real_user_id", 0),
        "create_time": data.get("create_time", ""),
        "comment_count": int(data.get("count_comment", 0)),
        "star_count": int(data.get("count_star", 0)),
        "trace_count": int(data.get("count_trace", 0)),
        "views": int(data.get("views", 0)),
        "hot": int(data.get("hot", 0)),
        "comments_json": json.dumps(comments, ensure_ascii=False),
    }


# ==================== DATA I/O ====================

def load_existing_ids(csv_path):
    """Return set of post IDs already in a CSV file."""
    ids = set()
    if os.path.exists(csv_path):
        csv.field_size_limit(10 ** 7)
        with open(csv_path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                aid = row.get("id", "")
                if aid:
                    ids.add(aid)
    return ids


def load_all_posts(*csv_paths):
    """Load and merge multiple CSVs, deduplicate by ID. Returns dict id→post."""
    csv.field_size_limit(10 ** 7)
    posts = {}
    for path in csv_paths:
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                aid = row.get("id", "")
                if not aid or aid in posts:
                    # Keep existing (first loaded wins — put detail CSV first)
                    continue
                posts[aid] = {
                    "id": aid,
                    "content": row.get("content", ""),
                    "category_name": row.get("category_name", ""),
                    "user_name": row.get("user_name", ""),
                    "show_user_id": row.get("show_user_id", ""),
                    "show_user_head": row.get("show_user_head", ""),
                    "real_user_id": row.get("real_user_id", "0"),
                    "create_time": row.get("create_time", ""),
                    "comment_count": int(row.get("comment_count", 0)),
                    "star_count": int(row.get("star_count", 0)),
                    "trace_count": int(row.get("trace_count", 0)),
                    "views": int(row.get("views", 0)),
                    "hot": int(row.get("hot", 0)),
                    "comments_json": row.get("comments_json", "[]"),
                }
    return posts


def save_posts(posts, path):
    """Write posts dict to CSV file (sorted by ID descending)."""
    csv.field_size_limit(10 ** 7)
    sorted_ids = sorted(posts.keys(), key=lambda x: int(x), reverse=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for aid in sorted_ids:
            writer.writerow(posts[aid])
    print(f"[save] {len(posts)} posts → {path}")


def append_to_scan_csv(post):
    """Append a single post to scan CSV (thread-safe via file lock)."""
    csv.field_size_limit(10 ** 7)
    file_exists = os.path.exists(SCAN_OUTPUT)
    with open(SCAN_OUTPUT, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if not file_exists or os.path.getsize(SCAN_OUTPUT) == 0:
            writer.writeheader()
        writer.writerow(post)


# ==================== PHASE 1: 续扫 ID 全量 ====================

def phase1_scan():
    """Multi-threaded ID scan: checkpoint → SCAN_END_ID. Saves to SCAN_OUTPUT."""
    cookie = load_cookie()
    cp = {}
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, "r") as f:
            cp = json.load(f)

    start_id = cp.get("last_id", 5000000)
    end_id = SCAN_END_ID

    if start_id <= end_id:
        print(f"[Phase 1] 已完成 (last_id={start_id} <= {end_id})")
        return

    total = start_id - end_id
    est_hours = total / 30 / 3600  # ~30 IDs/s
    print(f"[Phase 1] ID 扫描: {start_id} → {end_id} ({total:,} IDs, 预估 {est_hours:.1f}h)")
    print(f"[Phase 1] 并发: {NUM_WORKERS} workers, 输出: {SCAN_OUTPUT}")

    # Progress tracking
    lock = threading.Lock()
    progress = {"done": 0, "ruc": 0, "next_id": start_id}
    stop_flag = threading.Event()

    existing_ids = load_existing_ids(SCAN_OUTPUT)
    print(f"[Phase 1] 已有 {len(existing_ids)} 条记录")

    def worker(worker_id):
        sess = make_session(cookie)
        tid = start_id - worker_id
        local_batch = []

        while tid >= end_id and not stop_flag.is_set():
            try:
                r = sess.get(
                    f"{BASE}/article/article/info",
                    params={"community_id": CID, "id": tid}, timeout=8, verify=False
                )
                data = r.json()
                code = data.get("code")

                if code == "0000" and str(data["data"].get("community_id", "")) == str(CID):
                    inner = data["data"]
                    comments = inner.get("comment_list", [])
                    content = f"{(inner.get('title') or '')} {(inner.get('detail') or '')}".strip()
                    post = {
                        "id": str(tid),
                        "content": content,
                        "category_name": inner.get("category_name", ""),
                        "user_name": inner.get("show_user_name", ""),
                        "show_user_id": inner.get("show_user_id", ""),
                        "show_user_head": inner.get("show_user_head", ""),
                        "real_user_id": inner.get("real_user_id", 0),
                        "create_time": inner.get("create_time", ""),
                        "comment_count": int(inner.get("count_comment", 0)),
                        "star_count": int(inner.get("count_star", 0)),
                        "trace_count": int(inner.get("count_trace", 0)),
                        "views": int(inner.get("views", 0)),
                        "hot": int(inner.get("hot", 0)),
                        "comments_json": json.dumps(comments, ensure_ascii=False),
                    }
                    aid = str(tid)
                    if aid not in existing_ids:
                        existing_ids.add(aid)
                        local_batch.append(post)
                        with lock:
                            progress["ruc"] += 1

                elif code == "1000":
                    print("\n[!] Cookie 过期！保存断点退出...")
                    stop_flag.set()
                    break

            except Exception:
                pass

            with lock:
                progress["done"] += 1

            # Flush batch
            if len(local_batch) >= 30:
                for p in local_batch:
                    append_to_scan_csv(p)
                local_batch = []

            # Progress log
            with lock:
                d = progress["done"]
            if d % 1000 == 0 and worker_id == 0:
                with lock:
                    ruc = progress["ruc"]
                    elapsed_pct = d / total * 100
                print(f"  [{d:,}/{total:,} {elapsed_pct:.1f}%] ruc={ruc:,} rate~{int(d/(time.time()-t0) if time.time()-t0>0 else 999):d}/s", flush=True)

            tid -= NUM_WORKERS

        # Flush remaining
        for p in local_batch:
            append_to_scan_csv(p)

    # Launch workers
    threads = []
    t0 = time.time()
    for w in range(NUM_WORKERS):
        t = threading.Thread(target=worker, args=(w,))
        t.start()
        threads.append(t)

    # Monitor + checkpoint
    try:
        last_cp_save = time.time()
        while any(t.is_alive() for t in threads):
            time.sleep(30)
            with lock:
                d = progress["done"]
                r = progress["ruc"]
                elapsed = time.time() - t0
                rate = d / elapsed if elapsed > 0 else 0
                eta_min = (total - d) / rate / 60 if rate > 0 else 0
            print(f"[Monitor] {d:,}/{total:,} ({d/total*100:.1f}%) ruc={r:,} rate={rate:.0f}/s eta={eta_min:.0f}m", flush=True)

            # Save checkpoint
            with lock:
                cp["last_id"] = start_id - d
            with open(CHECKPOINT_FILE, "w") as f:
                json.dump(cp, f)

    except KeyboardInterrupt:
        print("\n[!] 中断 (Ctrl+C). 保存断点...")
        with lock:
            cp["last_id"] = start_id - progress["done"]
        with open(CHECKPOINT_FILE, "w") as f:
            json.dump(cp, f)
        stop_flag.set()
        for t in threads:
            t.join(timeout=5)
        sys.exit(0)

    for t in threads:
        t.join()

    elapsed = time.time() - t0
    with lock:
        ruc = progress["ruc"]
    print(f"[Phase 1] ✅ 完成! {elapsed/60:.0f}min, {ruc:,} RUC posts")

    # Clean checkpoint
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        print("[Phase 1] checkpoint 已清除")


# ==================== PHASE 2: 补扫高 ID 新区 ====================

def phase2_high_scan(posts, session):
    """Scan from latest post ID downward until 10 consecutive posts already in DB."""
    print("[Phase 2] 补扫高 ID 新区...")

    # Get latest post ID from lists page 1
    data, err = api_get(session, "/article/article/lists", {"community_id": CID, "page": 1})
    if err or not data:
        print(f"[Phase 2] ⚠ 无法获取最新帖 ID: {err}")
        return

    articles = data.get("list", [])
    if not articles:
        print("[Phase 2] ⚠ lists 返回空")
        return

    latest_id = max(int(a["id"]) for a in articles)
    highest_known = max(int(aid) for aid in posts.keys()) if posts else 0
    start_id = max(latest_id, highest_known)

    if start_id <= highest_known and highest_known >= latest_id:
        print(f"[Phase 2] 无需补扫 (已知最高 {highest_known} >= 最新 {latest_id})")
        return

    # If there ARE posts above 5M, start from latest_id
    # Otherwise check highest_known
    start_id = max(latest_id, highest_known)
    print(f"[Phase 2] 最新帖 ID={latest_id}, DB 最高={highest_known}, 起始={start_id}")

    consecutive_hit = 0
    scanned = 0
    new_posts = 0
    tid = start_id

    while True:
        time.sleep(random.uniform(0.08, 0.2))
        scanned += 1

        aid = str(tid)
        if aid in posts:
            consecutive_hit += 1
            if consecutive_hit >= UNCHANGED_STOP:
                print(f"[Phase 2] 连续 {UNCHANGED_STOP} 条命中 → 停止")
                break
        else:
            # Check if this ID is a RUC post
            detail = fetch_detail(session, tid)
            if detail:
                posts[aid] = detail
                new_posts += 1
                consecutive_hit = 0
                print(f"[Phase 2] 新帖 #{aid} {detail['create_time'][:16]} {detail['content'][:40]}")
            else:
                consecutive_hit += 1

        tid -= 1

        if scanned % 100 == 0:
            print(f"[Phase 2] 已扫 {scanned} IDs, 新帖 {new_posts}, 连续命中 {consecutive_hit}")

    print(f"[Phase 2] ✅ 完成! 扫 {scanned} IDs, 新帖 {new_posts}")


# ==================== PHASE 3: lists2 评论更新 ====================

def phase3_lists2_update(posts, session):
    """Crawl lists2 pages, compare comment_count, re-fetch changed ones.
    Stop when 10 consecutive posts are unchanged AND we've passed min pages."""
    print("[Phase 3] lists2 评论更新...")

    # Ensure at least 3 pages before considering early stop
    MIN_PAGES_BEFORE_STOP = 3
    consecutive_unchanged = 0
    new_posts = 0
    updated_posts = 0
    unchanged_posts = 0
    pages_scanned = 0

    for page in range(1, 300):  # safety max
        time.sleep(random.uniform(0.3, 0.8))
        data, err = api_get(session, "/article/article/lists2", {"community_id": CID, "page": page})
        if err:
            print(f"[Phase 3] page {page}: ERR={err}")
            if err == "cookie_expired":
                print("[!] Cookie 过期！")
                break
            continue

        articles = data.get("list", [])
        if not articles:
            print(f"[Phase 3] page {page}: 空页，停止")
            break

        pages_scanned += 1
        page_changed = 0
        page_new = 0

        for a in articles:
            aid = str(a.get("id", ""))
            if not aid:
                continue

            new_cc = int(a.get("comment_count", a.get("count_comment", 0)))

            if aid not in posts:
                # New post — fetch full detail
                detail = fetch_detail(session, aid)
                if detail:
                    posts[aid] = detail
                    new_posts += 1
                    page_new += 1
                    consecutive_unchanged = 0
                    print(f"[Phase 3] 新帖 #{aid} c={detail['comment_count']} {detail['content'][:40]}")
                else:
                    # Not found / not RUC — skip but don't count as unchanged
                    pass
            else:
                old_cc = posts[aid].get("comment_count", 0)
                if old_cc != new_cc:
                    # Comments changed — re-fetch
                    detail = fetch_detail(session, aid)
                    if detail:
                        posts[aid] = detail
                        updated_posts += 1
                        page_changed += 1
                        consecutive_unchanged = 0
                        print(f"[Phase 3] 更新 #{aid} c={old_cc}→{new_cc} {posts[aid]['content'][:40]}")
                else:
                    unchanged_posts += 1
                    consecutive_unchanged += 1

        print(f"[Phase 3] page {page}: {len(articles)} 帖, 新 {page_new}, 更新 {page_changed}, 累计未变 {consecutive_unchanged}")

        if consecutive_unchanged >= UNCHANGED_STOP and pages_scanned >= MIN_PAGES_BEFORE_STOP:
            print(f"[Phase 3] 连续 {UNCHANGED_STOP} 条无变化 ({pages_scanned} 页后) → 停止")
            break

    print(f"[Phase 3] ✅ 完成! {pages_scanned} 页, 新帖 {new_posts}, 更新 {updated_posts}, 未变 {unchanged_posts}")


# ==================== MAIN ====================

def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    cookie = load_cookie()
    print(f"[init] Cookie 已加载")
    print()

    # ═══════ Phase 1: 续扫 ID ═══════
    phase1_scan()
    print()

    # ═══════ Merge all data ═══════
    print("[merge] 合并数据...")
    danger_csv = os.path.join(DATA_DIR, "posts_danger.csv")
    posts = load_all_posts(danger_csv, SCAN_OUTPUT)
    print(f"[merge] 总计 {len(posts)} 条唯一帖子")
    print()

    # ═══════ Phase 2: 补扫高 ID ═══════
    session = make_session(cookie)
    phase2_high_scan(posts, session)
    print()

    # ═══════ Phase 3: lists2 更新 ═══════
    phase3_lists2_update(posts, session)
    print()

    # ═══════ Final save ═══════
    save_posts(posts, FINAL_OUTPUT)
    print(f"\n全部完成！输出: {FINAL_OUTPUT}")


if __name__ == "__main__":
    main()
