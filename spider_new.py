"""
New spider for RUC-Xiaolaba (云上校友圈 / RUC小喇叭).
Targets the NEW API at ys.qimiaoyuanfen.com with cookie-based auth.

Usage:
  1. Create data/config.txt with your session cookie (see data/config.example.txt)
  2. python spider_new.py

The cookie can be found via mitmproxy (see mitm_filter.py) or by inspecting
any ys.qimiaoyuanfen.com request in WeChat PC DevTools.
"""
import requests
import csv
import time
import os
import urllib3

urllib3.disable_warnings()

BASE = "https://ys.qimiaoyuanfen.com"
COMMUNITY_ID = 4
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
    config_path = os.path.join(DATA_DIR, "config.txt")
    if not os.path.exists(config_path):
        print(f"[!] Create {config_path} with: ys7_ysxy_session=YOUR_SESSION")
        return None
    for line in open(config_path, encoding="utf-8"):
        if "ys7_ysxy_session=" in line:
            return line.strip().split("=", 1)[1]
    return None


def fetch_page(session, page):
    url = f"{BASE}/article/article/lists2"
    try:
        resp = session.get(url, params={"community_id": COMMUNITY_ID, "page": page},
                           timeout=15, verify=False)
        data = resp.json()
        if data.get("code") == "1000":
            print("[!] Cookie expired. Update data/config.txt")
            return None, True
        if data.get("code") != "0000":
            print(f"[!] API error: {data.get('message', '')}")
            return None, False
        return data["data"].get("list", []), False
    except Exception as e:
        print(f"[!] Request failed: {e}")
        return None, False


def crawl(max_pages=50, start_page=1):
    cookie = load_cookie()
    if not cookie:
        return

    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.set("ys7_ysxy_session", cookie)
    session.verify = False

    os.makedirs(DATA_DIR, exist_ok=True)
    csv_path = os.path.join(DATA_DIR, "articles.csv")

    existing = set()
    if os.path.exists(csv_path):
        with open(csv_path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing.add(row.get("id", ""))

    new_count = 0
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not existing:
            writer.writerow(["id", "content", "category_name",
                             "user_name", "time", "comment_count"])

        for page in range(start_page, start_page + max_pages):
            print(f"[page {page}] Fetching...")
            articles, expired = fetch_page(session, page)

            if expired:
                break
            if articles is None:
                print("  Stopping.")
                break
            if not articles:
                print("  No more articles.")
                break

            for a in articles:
                aid = str(a.get("id", ""))
                if aid in existing:
                    continue
                existing.add(aid)
                content = f"{(a.get('title') or '')} {(a.get('detail') or '')}".strip()
                writer.writerow([
                    aid,
                    content,
                    a.get("category_name", ""),
                    a.get("show_user_name", ""),
                    a.get("create_time", ""),
                    a.get("comment_count", 0),
                ])
                new_count += 1

            print(f"  {len(articles)} articles, {new_count} new total")
            time.sleep(0.5)

    print(f"\nDone: {new_count} new articles -> {csv_path}")
    return new_count


if __name__ == "__main__":
    crawl(max_pages=10)
