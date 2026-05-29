"""
RUC小喇叭 爬虫 — 帖子 + 评论 + 赞 + 关注 全部信息
Usage: python spider.py [max_pages] [start_page]
"""
import requests
import csv
import json
import time
import os
import sys
import urllib3

urllib3.disable_warnings()

BASE = "https://ys.qimiaoyuanfen.com"
CID = 4
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

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
    ids = set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ids.add(row.get("id", ""))
    return ids


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


def crawl(max_pages=50, start_page=1):
    cookie = load_cookie()
    s = requests.Session()
    s.headers.update(HEADERS)
    s.cookies.set("ys7_ysxy_session", cookie)
    s.verify = False
    os.makedirs(DATA_DIR, exist_ok=True)

    # ----- Step 1: crawl post list -----
    list_path = os.path.join(DATA_DIR, "posts_list.csv")
    existing = load_existing_ids(list_path)
    new_posts = []

    print("[1/2] Crawling post list...")
    for page in range(start_page, start_page + max_pages):
        print(f"  page {page}...", end=" ", flush=True)
        data, err = api(s, "/article/article/lists2", {"community_id": CID, "page": page})
        if err:
            print(f"ERR: {err}")
            if err == "expired":
                sys.exit("[!] Cookie expired. Update data/config.txt")
            break
        articles = data.get("list", [])
        if not articles:
            print("no more")
            break

        added = 0
        with open(list_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if page == start_page and not os.path.exists(list_path) or (page == start_page and os.path.getsize(list_path) == 0):
                writer.writerow(["id", "content", "category_name", "user_name", "create_time", "comment_count"])
            for a in articles:
                aid = str(a.get("id", ""))
                if aid in existing:
                    continue
                existing.add(aid)
                content = f"{(a.get('title') or '')} {(a.get('detail') or '')}".strip()
                writer.writerow([aid, content, a.get("category_name", ""),
                                 a.get("show_user_name", ""), a.get("create_time", ""),
                                 a.get("comment_count", 0)])
                new_posts.append(a)
                added += 1
        print(f"{len(articles)} fetched, {added} new")
        time.sleep(0.4)

    print(f"  Total new: {len(new_posts)}")

    # ----- Step 2: fetch each post detail (with comments, likes, views) -----
    detail_path = os.path.join(DATA_DIR, "posts_full.csv")
    existing_full = load_existing_ids(detail_path)
    posts_to_fetch = [p for p in new_posts if str(p.get("id", "")) not in existing_full]

    if not posts_to_fetch:
        print("[2/2] All already have details.")
        return

    print(f"[2/2] Fetching detail for {len(posts_to_fetch)} posts...")
    file_exists = os.path.exists(detail_path)

    with open(detail_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "id", "content", "category_name", "user_name",
                "create_time", "comment_count", "star_count",
                "trace_count", "views", "hot",
                "comments_json"
            ])

        for i, post in enumerate(posts_to_fetch):
            aid = str(post.get("id", ""))
            print(f"  [{i+1}/{len(posts_to_fetch)}] #{aid}...", end=" ", flush=True)

            data, err = api(s, "/article/article/info", {"community_id": CID, "id": aid})
            if err:
                print(f"ERR: {err}")
                continue

            comments = data.get("comment_list", [])
            content = f"{(data.get('title') or '')} {(data.get('detail') or '')}".strip()

            writer.writerow([
                aid, content,
                data.get("category_name", ""),
                data.get("show_user_name", ""),
                data.get("create_time", ""),
                data.get("count_comment", 0),
                data.get("count_star", 0),
                data.get("count_trace", 0),
                data.get("views", 0),
                data.get("hot", 0),
                json.dumps(comments, ensure_ascii=False),
            ])
            print(f"OK ({len(comments)} comments, {data.get('count_star',0)} stars, {data.get('views',0)} views)")
            time.sleep(1)

    print(f"\nDone -> {detail_path}")


if __name__ == "__main__":
    max_p = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    start_p = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    crawl(max_pages=max_p, start_page=start_p)
