"""Crawl posts from 2026-05-28 00:00 onwards, dedup by id."""
import requests
import csv
import time
import os
import urllib3

urllib3.disable_warnings()

BASE = "https://ys.qimiaoyuanfen.com"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
TARGET_DATE = "2026-05-28"

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
    config_path = os.path.join(DATA_DIR, "config.txt")
    if not os.path.exists(config_path):
        print(f"[!] {config_path} not found")
        return None
    for line in open(config_path, encoding="utf-8"):
        if "ys7_ysxy_session=" in line:
            return line.strip().split("=", 1)[1]
    return None


def crawl_today():
    cookie = load_cookie()
    if not cookie:
        return

    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.set("ys7_ysxy_session", cookie)
    session.verify = False

    seen = set()

    # Load existing dedup set
    csv_path = os.path.join(DATA_DIR, "articles.csv")
    if os.path.exists(csv_path):
        with open(csv_path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                seen.add(row.get("id", ""))

    new_posts = []
    page = 1
    before_cutoff = False

    while not before_cutoff:
        print(f"[page {page}] ", end="", flush=True)
        try:
            resp = session.get(
                f"{BASE}/article/article/lists2",
                params={"community_id": 4, "page": page},
                timeout=15, verify=False,
            )
            data = resp.json()
            if data.get("code") == "1000":
                print("\n[!] Cookie expired!")
                break
            if data.get("code") != "0000":
                print(f"API error: {data.get('message')}")
                break

            articles = data["data"].get("list", [])
            if not articles:
                print("no more posts")
                break

            in_range = 0
            for a in articles:
                aid = str(a.get("id", ""))
                ctime = a.get("create_time", "")[:10]  # "2026-05-28 16:55:07"

                if ctime < TARGET_DATE:
                    before_cutoff = True
                    break

                if aid not in seen:
                    seen.add(aid)
                    content = f"{(a.get('title') or '')} {(a.get('detail') or '')}".strip()
                    new_posts.append({
                        "id": aid,
                        "content": content,
                        "category": a.get("category_name", ""),
                        "user": a.get("show_user_name", ""),
                        "time": ctime,
                    })
                    in_range += 1

            print(f"{len(articles)} fetched, {in_range} new (today)")
            page += 1
            time.sleep(0.4)

        except Exception as e:
            print(f"\n[!] Error: {e}")
            break

    # Append to CSV
    if new_posts:
        file_exists = os.path.exists(csv_path)
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["id", "content", "category_name", "user_name", "time", "comment_count"])
            for p in new_posts:
                writer.writerow([p["id"], p["content"], p["category"], p["user"], p["time"], ""])

    print(f"\n=== Done: {len(new_posts)} new posts from {TARGET_DATE} ===")
    print(f"Total unique in CSV: {len(seen)}")

    # Show latest
    print(f"\nLatest 10 today:")
    for i, p in enumerate(new_posts[:10]):
        print(f"  [#{p['id']}] [{p['category']}] {p['user']}")
        print(f"    {p['content'][:120]}")
        print()


if __name__ == "__main__":
    crawl_today()
