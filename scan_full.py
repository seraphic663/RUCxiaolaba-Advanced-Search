"""
多线程全量 ID 扫描：4,000,000 → 最新，仅保留 RUC 帖（community_id==4）
每帖直接存完整数据（含评论），断点续扫
"""
import requests
import csv
import json
import os
import time
import threading
import sys
import urllib3

urllib3.disable_warnings()

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CHECKPOINT_FILE = os.path.join(DATA_DIR, ".scan_checkpoint.json")
OUTPUT_CSV = os.path.join(DATA_DIR, "posts_scan.csv")

START_ID = 5000000   # 从上界开始（实际会动态调整到最新帖）
END_ID = 4000000      # 下界
NUM_WORKERS = 10       # 并发线程数

HEADERS_TEMPLATE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "MicroMessenger/7.0.20.1781 MiniProgramEnv/Windows WindowsWechat/WMPF",
    "Referer": "https://servicewechat.com/wxe23b94e06f71e89a/141/page-frame.html",
    "Xweb-Xhr": "1",
    "Accept": "application/json",
}

# ---- 全局状态 ----
lock = threading.Lock()
progress = {"done": 0, "ruc": 0, "exist": 0, "deleted": 0, "missing": 0, "error": 0, "next_id": None}
writer_lock = threading.Lock()


def load_cookie():
    p = os.path.join(DATA_DIR, "config.txt")
    if not os.path.exists(p):
        sys.exit(f"[!] Create {p}")
    for line in open(p, encoding="utf-8"):
        if "ys7_ysxy_session=" in line:
            return line.strip().split("=", 1)[1]
    sys.exit("[!] Cookie not found")


def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, "r") as f:
            return json.load(f)
    return {"last_id": START_ID}


def save_checkpoint(cp):
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(cp, f)


def make_session(cookie):
    s = requests.Session()
    s.headers.update(HEADERS_TEMPLATE)
    s.cookies.set("ys7_ysxy_session", cookie)
    s.verify = False
    return s


def writer_thread_func(write_queue, csv_path, done_event):
    """Single writer thread: drains queue and writes CSV rows."""
    csv.field_size_limit(10 ** 7)
    file_exists = os.path.exists(csv_path)
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "id", "content", "category_name", "user_name",
                "show_user_id", "show_user_head", "real_user_id",
                "create_time", "comment_count", "star_count",
                "trace_count", "views", "hot", "comments_json"
            ])
        while True:
            batch = write_queue.get()
            if batch is None:  # poison pill
                break
            for row in batch:
                writer.writerow(row)
            f.flush()
            write_queue.task_done()
    print("[writer] done.", flush=True)


def worker(worker_id, cookie, start_id, end_id, write_queue):
    """Scan IDs from start_id down to end_id, step = -NUM_WORKERS."""
    sess = make_session(cookie)
    local_batch = []
    tid = start_id - worker_id  # each worker starts at a different offset

    while tid >= end_id:
        try:
            r = sess.get(
                "https://ys.qimiaoyuanfen.com/article/article/info",
                params={"id": tid}, timeout=8, verify=False
            )
            data = r.json()
            code = data.get("code")

            with lock:
                progress["done"] += 1

            if code == "0000":
                inner = data["data"]
                if inner.get("community_id") == "4":
                    # RUC post — collect full row
                    comments = inner.get("comment_list", [])
                    content = f"{(inner.get('title') or '')} {(inner.get('detail') or '')}".strip()
                    row = [
                        str(tid),
                        content,
                        inner.get("category_name", ""),
                        inner.get("show_user_name", ""),
                        inner.get("show_user_id", ""),
                        inner.get("show_user_head", ""),
                        inner.get("real_user_id", 0),
                        inner.get("create_time", ""),
                        inner.get("count_comment", 0),
                        inner.get("count_star", 0),
                        inner.get("count_trace", 0),
                        inner.get("views", 0),
                        inner.get("hot", 0),
                        json.dumps(comments, ensure_ascii=False),
                    ]
                    local_batch.append(row)

                    with lock:
                        progress["ruc"] += 1
                        progress["exist"] += 1
                else:
                    with lock:
                        progress["exist"] += 1

            elif code == "0102":
                msg = data.get("message", "")
                if "下架" in msg:
                    with lock:
                        progress["deleted"] += 1
                else:
                    with lock:
                        progress["missing"] += 1
            else:
                with lock:
                    progress["error"] += 1

        except Exception:
            with lock:
                progress["error"] += 1

        # Flush batch to writer thread periodically
        if len(local_batch) >= 50:
            write_queue.put(local_batch)
            local_batch = []

        # Progress log every 500 per worker
        with lock:
            d = progress["done"]
        if d % 500 == 0 and worker_id == 0:
            pct = (start_id - tid) / (start_id - end_id) * 100
            with lock:
                ruc = progress["ruc"]
                done = progress["done"]
            print(f"  [{done}/{START_ID-end_id} {pct:.0f}%] ruc={ruc} id={tid}", flush=True)

        tid -= NUM_WORKERS

    # Flush remaining
    if local_batch:
        write_queue.put(local_batch)
    print(f"[worker {worker_id}] done. scanned to {tid + NUM_WORKERS}", flush=True)


def main():
    global END_ID
    os.makedirs(DATA_DIR, exist_ok=True)
    cookie = load_cookie()
    cp = load_checkpoint()

    start_id = cp["last_id"]
    end_id = END_ID  # local copy, may be modified on interrupt
    print(f"Range: {start_id} -> {end_id}")
    print(f"Total IDs: {start_id - end_id:,}")
    print(f"Workers: {NUM_WORKERS}")
    if cp.get("last_id", START_ID) < START_ID:
        print(f"Resuming from checkpoint: last_id={start_id}")
    print()

    # Estimate
    est_seconds = (start_id - end_id) / 70  # ~70 IDs/s with 10 workers
    print(f"Estimated: {est_seconds/60:.0f} min ({est_seconds/3600:.1f} h)")
    print()

    # Start writer thread + queue
    from queue import Queue
    write_queue = Queue(maxsize=100)
    done_event = threading.Event()
    writer_thread = threading.Thread(target=writer_thread_func, args=(write_queue, OUTPUT_CSV, done_event))
    writer_thread.start()

    # Start worker threads
    threads = []
    t0 = time.time()
    for w in range(NUM_WORKERS):
        t = threading.Thread(target=worker, args=(w, cookie, start_id, end_id, write_queue))
        t.start()
        threads.append(t)

    # Progress monitor
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(30)
            with lock:
                d = progress["done"]
                r = progress["ruc"]
                total = start_id - end_id
                elapsed = time.time() - t0
                rate = d / elapsed if elapsed > 0 else 0
                eta = (total - d) / rate / 60 if rate > 0 else 0
                next_id_est = start_id - d
            print(f"[MONITOR] {d}/{total} ({d/total*100:.1f}%) ruc={r} "
                  f"rate={rate:.0f}/s eta={eta:.0f}m next_id~{next_id_est}", flush=True)
            # Auto-save checkpoint
            cp["last_id"] = start_id - d
            save_checkpoint(cp)

    except KeyboardInterrupt:
        print("\n[!] Interrupted. Saving checkpoint...", flush=True)
        with lock:
            cp["last_id"] = start_id - progress["done"]
        save_checkpoint(cp)
        print(f"Saved at ID {cp['last_id']}. Resume: python scan_full.py", flush=True)
        write_queue.put(None)
        sys.exit(0)

    # All workers done
    for t in threads:
        t.join()

    # Signal writer to finish
    write_queue.put(None)
    writer_thread.join()

    elapsed = time.time() - t0
    with lock:
        ruc = progress["ruc"]
        done = progress["done"]
    print(f"\n=== DONE ===")
    print(f"Scanned: {done} IDs in {elapsed/60:.0f} min ({done/elapsed:.0f} IDs/s)")
    print(f"RUC posts saved: {ruc}")
    print(f"Output: {OUTPUT_CSV}")

    # Clean checkpoint
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)


if __name__ == "__main__":
    csv.field_size_limit(10 ** 7)
    main()
