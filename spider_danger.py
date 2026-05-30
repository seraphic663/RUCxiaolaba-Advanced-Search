"""
RUC小喇叭 全量爬虫 — 爬取 lists 端点全部历史帖子，含 show_user_id 等持久化标识
特性: 随机延迟、断点续爬、自动检测末尾
Usage: python spider_danger.py
"""
import requests
import csv
import json
import time
import os
import sys
import random
import urllib3

urllib3.disable_warnings()

BASE = "https://ys.qimiaoyuanfen.com"
CID = 4
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CHECKPOINT_FILE = os.path.join(DATA_DIR, ".crawl_checkpoint.json")
LIST_CSV = os.path.join(DATA_DIR, "posts_danger_list.csv")
DETAIL_CSV = os.path.join(DATA_DIR, "posts_danger.csv")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "MicroMessenger/7.0.20.1781 MiniProgramEnv/Windows WindowsWechat/WMPF"
    ),
    "Referer": "https://servicewechat.com/wxe23b94e06f71e89a/141/page-frame.html",
    "Xweb-Xhr": "1",
    "Accept": "application/json",
}


def load_cookie():
    p = os.path.join(DATA_DIR, "config.txt")
    if not os.path.exists(p):
        sys.exit(f"[!] Create {p} with: ys7_ysxy_session=YOUR_COOKIE")
    for line in open(p, encoding="utf-8"):
        if "ys7_ysxy_session=" in line:
            return line.strip().split("=", 1)[1]
    sys.exit("[!] Cookie not found in config.txt")


def load_existing_ids(path):
    csv.field_size_limit(10 ** 7)
    ids = set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ids.add(row.get("id", ""))
    return ids


def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"list_page": 0, "detail_done": [], "total_new": 0, "detail_total": 0}


def save_checkpoint(cp):
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump(cp, f)


def api(session, path, params=None):
    try:
        r = session.get(f"{BASE}{path}", params=params, timeout=15, verify=False)
        d = r.json()
        if d.get("code") == "1000":
            return None, "expired"
        if d.get("code") != "0000":
            return None, d.get("message", "error")
        return d.get("data", {}), None
    except Exception as e:
        return None, str(e)


def crawl():
    cp = load_checkpoint()
    cookie = load_cookie()
    s = requests.Session()
    s.headers.update(HEADERS)
    s.cookies.set("ys7_ysxy_session", cookie)
    s.verify = False
    os.makedirs(DATA_DIR, exist_ok=True)

    # ====== Step 1: crawl /article/article/lists ======
    existing_list = load_existing_ids(LIST_CSV)
    start_page = cp["list_page"] + 1

    print(f"[1/3] Crawling /article/article/lists from page {start_page}...")
    if cp["list_page"] > 0:
        print(f"      Resuming from checkpoint (page {cp['list_page']} done, {cp['total_new']} new so far)")

    end_reached = False
    consecutive_empty = 0
    max_page = start_page + 500  # safety limit

    for page in range(start_page, max_page):
        delay = random.uniform(0.5, 1.5)
        time.sleep(delay)

        data, err = api(s, "/article/article/lists", {"community_id": CID, "page": page})
        if err:
            print(f"  page {page}: ERR={err}")
            if err == "expired":
                print("[!] Cookie expired.")
                save_checkpoint(cp)
                sys.exit(1)
            consecutive_empty += 1
            if consecutive_empty >= 3:
                print(f"  3 consecutive errors, stopping.")
                save_checkpoint(cp)
                break
            continue

        articles = data.get("list", [])
        if not articles:
            print(f"  page {page}: empty (end reached)")
            end_reached = True
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
            continue

            added = 0
        first_time = articles[0].get("create_time", "?")[:16]
        last_time = articles[-1].get("create_time", "?")[:16]

        with open(LIST_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if page == 1 and (not os.path.exists(LIST_CSV) or os.path.getsize(LIST_CSV) == 0):
                writer.writerow(["id", "content", "category_name", "user_name",
                                 "show_user_id", "show_user_head", "real_user_id",
                                 "create_time", "comment_count"])
            for a in articles:
                aid = str(a.get("id", ""))
                if aid in existing_list:
                    continue
                existing_list.add(aid)
                content = f"{(a.get('title') or '')} {(a.get('detail') or '')}".strip()
                writer.writerow([
                    aid, content,
                    a.get("category_name", ""),
                    a.get("show_user_name", ""),
                    a.get("show_user_id", ""),
                    a.get("show_user_head", ""),
                    a.get("real_user_id", 0),
                    a.get("create_time", ""),
                    a.get("comment_count", 0),
                ])
                added += 1

        cp["list_page"] = page
        cp["total_new"] += added
        save_checkpoint(cp)

        if added == 0:
            consecutive_empty += 1
        else:
            consecutive_empty = 0

        marker = f" [no-new {consecutive_empty}/3]" if added == 0 else ""
        print(f"  page {page}: {len(articles)} posts, {added} new, {first_time} ~ {last_time}{marker}")

        if consecutive_empty >= 3:
            print(f"  List crawl done (3 consecutive pages with 0 new).")
            break

    print(f"  List crawl done. {cp['total_new']} new posts found across {cp['list_page']} pages.")

    # ====== Step 2: find all posts needing details ======
    all_list_ids = load_existing_ids(LIST_CSV)
    existing_detail_ids = load_existing_ids(DETAIL_CSV)
    need_detail = [aid for aid in all_list_ids if aid not in existing_detail_ids]
    done_set = set(cp.get("detail_done", []))
    to_fetch = [aid for aid in need_detail if aid not in done_set]

    if not to_fetch:
        print("[2/2] All posts already have details. Done.")
        if os.path.exists(CHECKPOINT_FILE):
            os.remove(CHECKPOINT_FILE)
        return

    print(f"[2/2] Fetching details: {len(need_detail)} total needed, {len(done_set)} already done, {len(to_fetch)} remaining")

    if len(to_fetch) < len(new_ids):
        print(f"      Resuming: {len(new_ids) - len(to_fetch)} already done, {len(to_fetch)} remaining")

    detail_exists = os.path.exists(DETAIL_CSV)
    existing_detail_ids = load_existing_ids(DETAIL_CSV)

    fetched_count = len(done_set)
    last_checkpoint = time.time()

    with open(DETAIL_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not detail_exists:
            writer.writerow([
                "id", "content", "category_name", "user_name",
                "show_user_id", "show_user_head", "real_user_id",
                "create_time", "comment_count", "star_count",
                "trace_count", "views", "hot",
                "comments_json"
            ])

        for i, aid in enumerate(to_fetch):
            # Random delay
            delay = random.uniform(1.0, 2.5)
            time.sleep(delay)

            data, err = api(s, "/article/article/info", {"community_id": CID, "id": aid})
            if err:
                print(f"  [{i+1}/{len(to_fetch)}] #{aid}: ERR={err}")
                if err == "expired":
                    print("[!] Cookie expired. Saving progress...")
                    cp["detail_done"] = list(done_set)
                    cp["detail_total"] = fetched_count
                    save_checkpoint(cp)
                    sys.exit(1)
                continue

            comments = data.get("comment_list", [])
            content = f"{(data.get('title') or '')} {(data.get('detail') or '')}".strip()

            writer.writerow([
                aid, content,
                data.get("category_name", ""),
                data.get("show_user_name", ""),
                data.get("show_user_id", ""),
                data.get("show_user_head", ""),
                data.get("real_user_id", 0),
                data.get("create_time", ""),
                data.get("count_comment", 0),
                data.get("count_star", 0),
                data.get("count_trace", 0),
                data.get("views", 0),
                data.get("hot", 0),
                json.dumps(comments, ensure_ascii=False),
            ])

            done_set.add(aid)
            fetched_count += 1

            # Checkpoint every 50 fetches or every 60 seconds
            now = time.time()
            if (i + 1) % 50 == 0 or (now - last_checkpoint) > 60:
                cp["detail_done"] = list(done_set)
                cp["detail_total"] = fetched_count
                save_checkpoint(cp)
                last_checkpoint = now

            progress = (i + 1) / len(to_fetch) * 100
            eta = (len(to_fetch) - i - 1) * delay / 60
            print(f"  [{i+1}/{len(to_fetch)} {progress:.0f}% ETA {eta:.0f}m] #{aid}: {len(comments)}c uid={data.get('show_user_id','?')}")

    cp["detail_done"] = list(done_set)
    cp["detail_total"] = fetched_count
    save_checkpoint(cp)

    # ====== Done ======
    print(f"\nDone! {fetched_count} details fetched -> {DETAIL_CSV}")
    # Clean checkpoint on success
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)


if __name__ == "__main__":
    crawl()
