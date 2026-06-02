"""Fetch details for all posts in list CSV that don't have details yet."""
import requests, csv, json, os, time, random, urllib3
urllib3.disable_warnings()
csv.field_size_limit(10**7)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LIST_CSV = os.path.join(DATA_DIR, "posts_danger_list.csv")
DETAIL_CSV = os.path.join(DATA_DIR, "posts_danger.csv")
CHECKPOINT = os.path.join(DATA_DIR, ".detail_checkpoint.json")

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

all_ids = set()
with open(LIST_CSV, encoding="utf-8") as f:
    for r in csv.DictReader(f):
        all_ids.add(r["id"])

detail_ids = set()
if os.path.exists(DETAIL_CSV):
    with open(DETAIL_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            detail_ids.add(r["id"])

to_fetch = sorted([aid for aid in all_ids if aid not in detail_ids], key=int)

done = set()
if os.path.exists(CHECKPOINT):
    with open(CHECKPOINT, encoding="utf-8") as f:
        done = set(json.load(f))
    to_fetch = [aid for aid in to_fetch if aid not in done]

print(f"List: {len(all_ids)}  Detail: {len(detail_ids)}  Done: {len(done)}  Remaining: {len(to_fetch)}", flush=True)

if not to_fetch:
    print("All done!", flush=True)
    if os.path.exists(CHECKPOINT):
        os.remove(CHECKPOINT)
    exit()

detail_exists = os.path.exists(DETAIL_CSV)
fetched, errors, last_save = 0, 0, time.time()

with open(DETAIL_CSV, "a", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    if not detail_exists:
        writer.writerow(["id","content","category_name","user_name","show_user_id","show_user_head","real_user_id","create_time","comment_count","star_count","trace_count","views","hot","comments_json"])

    for i, aid in enumerate(to_fetch):
        time.sleep(random.uniform(0.8, 2.0))
        try:
            r = s.get("https://ys.qimiaoyuanfen.com/article/article/info",
                      params={"community_id": 4, "id": aid}, timeout=15, verify=False)
            data = r.json()
            if data.get("code") != "0000":
                errors += 1
                if errors > 10:
                    print(f"Too many errors ({errors})", flush=True)
                    break
                if data.get("code") == "1000":
                    print("COOKIE EXPIRED!", flush=True)
                    break
                continue
            errors = 0
            inner = data["data"]
            comments = inner.get("comment_list", [])
            content = f"{(inner.get('title') or '')} {(inner.get('detail') or '')}".strip()
            writer.writerow([
                aid, content,
                inner.get("category_name",""), inner.get("show_user_name",""),
                inner.get("show_user_id",""), inner.get("show_user_head",""),
                inner.get("real_user_id",0), inner.get("create_time",""),
                inner.get("count_comment",0), inner.get("count_star",0),
                inner.get("count_trace",0), inner.get("views",0),
                inner.get("hot",0), json.dumps(comments, ensure_ascii=False),
            ])
            f.flush()
            done.add(aid)
            fetched += 1
        except Exception as e:
            errors += 1
            continue

        if (i+1) % 30 == 0 or (time.time() - last_save) > 30:
            with open(CHECKPOINT, "w", encoding="utf-8") as cf:
                json.dump(list(done), cf)
            last_save = time.time()
            pct = (i+1)/len(to_fetch)*100
            eta = (len(to_fetch)-i-1) * 1.4 / 60
            print(f"[{i+1}/{len(to_fetch)} {pct:.0f}% ETA {eta:.0f}m] #{aid} c={len(comments)}", flush=True)

with open(CHECKPOINT, "w", encoding="utf-8") as cf:
    json.dump(list(done), cf)
print(f"Done! Fetched {fetched}. Total: {len(detail_ids) + len(done)}", flush=True)
if errors == 0 and os.path.exists(CHECKPOINT):
    os.remove(CHECKPOINT)
