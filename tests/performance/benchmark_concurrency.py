"""Test concurrent ID scanning speed."""
import requests, urllib3, os, time, threading
urllib3.disable_warnings()

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
with open(os.path.join(DATA_DIR, "config.txt"), encoding="utf-8") as f:
    cookie = f.read().strip().split("=", 1)[1]

def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 MicroMessenger/7.0.20.1781 MiniProgramEnv/Windows WindowsWechat/WMPF",
        "Referer": "https://servicewechat.com/wxe23b94e06f71e89a/141/page-frame.html",
        "Xweb-Xhr": "1", "Accept": "application/json",
    })
    s.cookies.set("ys7_ysxy_session", cookie)
    s.verify = False
    return s

for n_workers in [1, 5, 10, 20]:
    sessions = [make_session() for _ in range(n_workers)]
    ids_to_check = list(range(4985000, 4985000 - 200, -1))

    found = [0]
    idx = [0]
    lock = threading.Lock()

    def worker(sess):
        while True:
            with lock:
                if idx[0] >= len(ids_to_check):
                    break
                i = idx[0]
                idx[0] += 1
            tid = ids_to_check[i]
            try:
                r = sess.get("https://ys.qimiaoyuanfen.com/article/article/info",
                            params={"id": tid}, timeout=5, verify=False)
                data = r.json()
                if data.get("code") == "0000" and data["data"].get("community_id") == "4":
                    with lock:
                        found[0] += 1
            except:
                pass

    t0 = time.time()
    threads = [threading.Thread(target=worker, args=(sessions[i],)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    dt = time.time() - t0
    rate = 200 / dt
    print(f"Workers={n_workers:2d}: {dt:.1f}s, {rate:.0f} IDs/s, found {found[0]} RUC")

print()
ids_total = 4990000 - 3350000
print(f"Step=1 full scan ({ids_total} IDs) estimates:")
for w, rate_guess in [(10, 50), (20, 90), (30, 120)]:
    hours = ids_total / rate_guess / 3600
    print(f"  {w} workers @ {rate_guess}/s: {hours:.1f} hours")
