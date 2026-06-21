"""
Test RUC-Xiaolaba API endpoints. Reads cookie from data/config.txt .
Usage: python test_api.py
"""
import os

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://ys.qimiaoyuanfen.com"
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def load_cookie():
    """Load session cookie from data/config.txt"""
    config_path = os.path.join(DATA_DIR, "config.txt")
    if not os.path.exists(config_path):
        print("[!] data/config.txt not found.")
        print("    Create it with: ys7_ysxy_session=YOUR_SESSION_COOKIE")
        return None
    for line in open(config_path):
        if "ys7_ysxy_session=" in line:
            return line.strip().split("=", 1)[1]
    return None


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "MicroMessenger/7.0.20.1781 MiniProgramEnv/Windows WindowsWechat/WMPF"
    ),
    "Referer": "https://servicewechat.com/wxe23b94e06f71e89a/141/page-frame.html",
    "Xweb-Xhr": "1",
    "Accept": "application/json",
}


def api(session, method, path, **kwargs):
    url = BASE + path
    resp = session.request(method, url, **kwargs)
    print("  [%s] %s -> %d" % (method, path, resp.status_code))
    try:
        data = resp.json()
        code = data.get("code", "?")
        if code == "0000":
            inner = data.get("data", {})
            if isinstance(inner, dict):
                print("    OK  keys: %s" % list(inner.keys())[:8])
            elif isinstance(inner, list):
                print("    OK  list: %d items" % len(inner))
            else:
                print("    OK  value: %s" % str(inner)[:80])
        elif code == "1000":
            print("    AUTH FAILED - cookie expired. Update data/config.txt")
        else:
            print("    CODE=%s msg=%s" % (code, data.get("message", "")))
        return data
    except Exception:
        print("    RAW: %s" % resp.text[:200])
        return None


def main():
    cookie = load_cookie()
    if not cookie:
        return

    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.set("ys7_ysxy_session", cookie)
    session.verify = False

    print("=" * 50)
    print("  Testing RUC-Xiaolaba API")
    print("=" * 50)

    print("\n[1] Community Info")
    api(session, "GET", "/base/community/info?community_id=4")

    print("\n[2] Article List (homepage)")
    r = api(session, "GET", "/article/article/lists2?community_id=4&page=1")
    if r and r.get("code") == "0000":
        for a in r["data"].get("list", [])[:5]:
            text = ((a.get("title") or "") + " " + (a.get("detail") or ""))[:100]
            print("    [#%s] [%s] %s" % (a.get("id"), a.get("category_name", ""), text))

    print("\n[3] Hot Articles")
    r = api(session, "GET", "/article/article/datehot?community_id=4&page=1")
    if r and r.get("code") == "0000":
        for a in r["data"].get("list", [])[:3]:
            text = ((a.get("detail") or ""))[:80]
            print("    [#%s] %s" % (a.get("id"), text))

    print("\nDone!")


if __name__ == "__main__":
    main()
