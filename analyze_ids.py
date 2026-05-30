"""ID 区间分析：每 10 万一块，探测时间范围"""
import requests, urllib3, os, time, sys
urllib3.disable_warnings()

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
with open(os.path.join(DATA_DIR, "config.txt"), encoding="utf-8") as f:
    cookie = f.read().strip().split("=", 1)[1]

s = requests.Session()
s.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 MicroMessenger/7.0.20.1781 MiniProgramEnv/Windows WindowsWechat/WMPF",
    "Referer": "https://servicewechat.com/wxe23b94e06f71e89a/141/page-frame.html",
    "Xweb-Xhr": "1", "Accept": "application/json",
})
s.cookies.set("ys7_ysxy_session", cookie)
s.verify = False

LOWER = 3300000
UPPER = 5000000
BLOCK = 100000

print(f"ID range: {LOWER} ~ {UPPER} ({(UPPER-LOWER)//BLOCK} blocks of {BLOCK})")
print()

# Phase 1: find time at each 100k boundary
print("=== Block boundary times ===")
boundaries = list(range(UPPER, LOWER - BLOCK, -BLOCK))
time_at = {}

for b in boundaries:
    # Search around the boundary for the nearest RUC post
    found = None
    for offset in range(0, 500, 1):
        for direction in [0, -1, 1, -2, 2]:  # search around the exact boundary
            tid = b + direction * (offset if offset > 0 else 0)
            if tid < LOWER or tid > UPPER:
                continue
            try:
                r = s.get(f"https://ys.qimiaoyuanfen.com/article/article/info?id={tid}", timeout=5, verify=False)
                data = r.json()
                if data.get("code") == "0000" and data["data"].get("community_id") == "4":
                    found = (tid, data["data"].get("create_time", "?")[:16])
                    break
            except:
                pass
            time.sleep(0.02)
        if found:
            break
        time.sleep(0.05)

    if found:
        time_at[b] = found
        print(f"  {b:>8}: ID {found[0]}  {found[1]}", flush=True)
    else:
        print(f"  {b:>8}: no RUC post found nearby", flush=True)
    time.sleep(0.1)

# Phase 2: density sample per block (100 IDs each)
print()
print("=== Block density (100-ID sample per block) ===")

for i in range(len(boundaries) - 1):
    hi_b = boundaries[i]
    lo_b = boundaries[i + 1]
    mid = (hi_b + lo_b) // 2

    ruc = 0
    exist = 0
    for tid in range(mid + 50, mid - 50, -1):
        try:
            r = s.get(f"https://ys.qimiaoyuanfen.com/article/article/info?id={tid}", timeout=5, verify=False)
            data = r.json()
            if data.get("code") == "0000":
                exist += 1
                if data["data"].get("community_id") == "4":
                    ruc += 1
        except:
            pass
        time.sleep(0.03)

    hi_t = time_at.get(hi_b, ("?", "?"))[1] if hi_b in time_at else "?"
    lo_t = time_at.get(lo_b, ("?", "?"))[1] if lo_b in time_at else "?"
    pct = ruc / 100 * 100 if exist > 0 else 0
    est = int(BLOCK * pct / 100)
    print(f"  {lo_b}-{hi_b}: {ruc}/100 RUC ({pct:.0f}%)  est ~{est} posts  {lo_t} ~ {hi_t}", flush=True)

print()
print("Done.", flush=True)
