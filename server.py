"""
Web search for RUC-Xiaolaba — reads posts CSV, provides search API + admin panel.

Features:
  - ThreadingHTTPServer (multi-client concurrent)
  - Memory cache with auto-reload on CSV change
  - /api/search?q=...&sort=...&page=...&limit=50
  - /api/comments?id=... (lazy comment loading)
  - Admin panel with CSRF + session auth
  - Random password generation on first run
  - Template-based rendering (templates/*.html)
"""
import json
import csv
import os
import secrets
import time
import threading
import string as _string
from datetime import datetime, timedelta
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

# ==================== CONFIG ====================

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
CSV_SCAN = os.path.join(DATA_DIR, "posts_scan.csv")
CSV_DANGER = os.path.join(DATA_DIR, "posts_danger.csv")
CSV_LEGACY = os.path.join(DATA_DIR, "posts_full.csv")
PASSWORD_FILE = os.path.join(DATA_DIR, "admin_password.txt")
CHECKIN_FILE = os.path.join(DATA_DIR, "checkin_count.json")

SESSION_TTL = 86400   # 24 hours
CSRF_TTL = 3600       # 1 hour
COMMENT_LIMIT = 500   # max comments to return per post

# ==================== THREAD-SAFE STATE ====================

_state_lock = threading.Lock()
_checkin_lock = threading.Lock()
_admin_sessions = {}   # token -> expiry (unix timestamp)
_csrf_tokens = {}      # token -> expiry (unix timestamp)

_cache = {
    "posts": None,        # list of post dicts
    "post_index": None,   # id -> post dict (for fast lookup)
    "mtime": 0.0,         # CSV file mtime when cached
    "csv_path": None,     # which CSV was loaded
}

# ==================== PASSWORD ====================

def get_password():
    """Return admin password, generating a random one on first run."""
    if os.path.exists(PASSWORD_FILE):
        with open(PASSWORD_FILE, "r", encoding="utf-8") as f:
            pwd = f.read().strip()
            if pwd:
                return pwd
    # Generate and persist a random password
    pwd = ''.join(secrets.choice(_string.ascii_letters + _string.digits) for _ in range(16))
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PASSWORD_FILE, "w", encoding="utf-8") as f:
        f.write(pwd)
    print(f"\n[!] ========================================")
    print(f"[!]  生成随机管理员密码: {pwd}")
    print(f"[!]  已保存至: {PASSWORD_FILE}")
    print(f"[!] ========================================\n")
    return pwd


# ==================== CHECK-IN COUNT ====================

def _read_checkin_count_unlocked():
    if not os.path.exists(CHECKIN_FILE):
        return 0
    try:
        with open(CHECKIN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return max(0, int(data.get("count", 0)))
    except Exception:
        return 0


def get_checkin_count():
    with _checkin_lock:
        return _read_checkin_count_unlocked()


def increment_checkin_count():
    with _checkin_lock:
        count = _read_checkin_count_unlocked() + 1
        os.makedirs(DATA_DIR, exist_ok=True)
        payload = {
            "count": count,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(CHECKIN_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return count


# ==================== DATA LOADING & CACHING ====================

def load_posts():
    """Read CSV, deduplicate, parse comments, build index. Returns (posts, crawl_time, has_danger, csv_path)."""
    csv_path = CSV_SCAN if os.path.exists(CSV_SCAN) else (CSV_DANGER if os.path.exists(CSV_DANGER) else CSV_LEGACY)
    if not os.path.exists(csv_path):
        return [], None, None, None

    csv.field_size_limit(10 ** 7)
    seen = set()
    posts = []
    has_danger = (csv_path in (CSV_DANGER, CSV_SCAN))

    with open(csv_path, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            aid = row.get("id", "")
            if not aid or aid in seen:
                continue
            seen.add(aid)

            # Parse comment JSON
            cmts_raw = row.get("comments_json", "[]")
            try:
                comment_list = json.loads(cmts_raw)
            except Exception:
                comment_list = []

            post = {
                "id": aid,
                "content": row.get("content", ""),
                "category": row.get("category_name", ""),
                "user": row.get("user_name", ""),
                "time": row.get("create_time", ""),
                "comments": int(row.get("comment_count", 0)),
                "stars": int(row.get("star_count", 0)),
                "trace": int(row.get("trace_count", 0)),
                "views": int(row.get("views", 0)),
                "hot": int(row.get("hot", 0)),
                "comment_list": comment_list,
            }

            if has_danger:
                post["show_user_id"] = row.get("show_user_id", "")
                post["show_user_head"] = row.get("show_user_head", "")
                post["real_user_id"] = row.get("real_user_id", "0")

            # Pre-compute search text: content-only + full (with comments)
            post["_search_content"] = (post["content"] + " #" + aid).lower()
            search_parts = [post["content"], "#" + aid]
            for c in comment_list:
                search_parts.append(c.get("detail", "") or "")
                for nr in c.get("reply_comment_list", []):
                    search_parts.append(nr.get("detail", "") or "")
            post["_search_all"] = " ".join(search_parts).lower()

            # Pre-parse time for fast date filtering
            t = post["time"]
            if t:
                try:
                    post["_time_dt"] = datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    post["_time_dt"] = None
            else:
                post["_time_dt"] = None

            posts.append(post)

    posts.sort(key=lambda x: int(x["id"]), reverse=True)
    crawl_time = datetime.fromtimestamp(os.path.getmtime(csv_path)).strftime("%Y-%m-%d %H:%M")
    return posts, crawl_time, has_danger, csv_path


def refresh_cache():
    """Reload data if CSV has changed (or first load). Thread-safe."""
    csv_path = CSV_SCAN if os.path.exists(CSV_SCAN) else (CSV_DANGER if os.path.exists(CSV_DANGER) else CSV_LEGACY)
    if not os.path.exists(csv_path):
        with _state_lock:
            _cache["posts"] = []
            _cache["post_index"] = {}
            _cache["mtime"] = 0
            _cache["csv_path"] = None
        return

    mtime = os.path.getmtime(csv_path)
    with _state_lock:
        if _cache["posts"] is not None and mtime <= _cache["mtime"]:
            return  # cache still fresh

    # Load outside lock to avoid holding it during I/O
    posts, crawl_time, has_danger, actual_path = load_posts()
    post_index = {p["id"]: p for p in posts}

    with _state_lock:
        _cache["posts"] = posts
        _cache["post_index"] = post_index
        _cache["mtime"] = mtime
        _cache["csv_path"] = actual_path
        _cache["crawl_time"] = crawl_time
        _cache["has_danger"] = has_danger


def get_cached_data():
    """Return (posts, crawl_time, has_danger, csv_path) from cache, refreshing if needed."""
    refresh_cache()
    with _state_lock:
        return (
            _cache["posts"],
            _cache.get("crawl_time", "?"),
            _cache.get("has_danger", False),
            _cache.get("csv_path", "?"),
        )


def get_post_by_id(pid):
    """Look up a single post by ID. Returns None if not found."""
    with _state_lock:
        idx = _cache.get("post_index", {})
    return idx.get(pid)


# ==================== SESSION & CSRF ====================

def _cleanup_expired():
    """Remove expired sessions and CSRF tokens. Call periodically."""
    now = time.time()
    with _state_lock:
        expired_sessions = [t for t, exp in _admin_sessions.items() if exp < now]
        for t in expired_sessions:
            del _admin_sessions[t]
        expired_csrf = [t for t, exp in _csrf_tokens.items() if exp < now]
        for t in expired_csrf:
            del _csrf_tokens[t]


def create_session():
    """Create a new admin session token. Returns token string."""
    token = secrets.token_hex(32)
    with _state_lock:
        _admin_sessions[token] = time.time() + SESSION_TTL
    return token


def is_valid_session(token):
    """Check if an admin session token is valid and not expired."""
    if not token:
        return False
    _cleanup_expired()
    with _state_lock:
        expiry = _admin_sessions.get(token)
    return expiry is not None and expiry > time.time()


def create_csrf_token():
    """Generate a CSRF token for the login form."""
    token = secrets.token_hex(16)
    with _state_lock:
        _csrf_tokens[token] = time.time() + CSRF_TTL
    return token


def verify_csrf_token(token):
    """Verify and consume a CSRF token. Returns True if valid."""
    if not token:
        return False
    _cleanup_expired()
    with _state_lock:
        expiry = _csrf_tokens.pop(token, None)
    return expiry is not None and expiry > time.time()


# ==================== ADMIN DATA ====================

def count_stats(posts):
    """Compute admin statistics from posts with show_user_id."""
    uid_counts = defaultdict(int)
    commenter_ids = set()
    total_comments = 0
    for p in posts:
        uid = p.get("show_user_id", "")
        if uid:
            uid_counts[uid] += 1
        for c in p.get("comment_list", []):
            total_comments += 1
            cu = c.get("show_user_id", "")
            if cu:
                commenter_ids.add(cu)
            for nr in c.get("reply_comment_list", []):
                total_comments += 1
                nu = nr.get("show_user_id", "")
                if nu:
                    commenter_ids.add(nu)
    multi = sum(1 for c in uid_counts.values() if c >= 2)
    return len(uid_counts), multi, total_comments, len(commenter_ids)


def build_user_rows(posts):
    """Build HTML for user grouping in admin dashboard."""
    user_posts = defaultdict(list)
    user_names = {}
    user_heads = {}
    for p in posts:
        uid = p.get("show_user_id", "")
        if not uid:
            continue
        user_posts[uid].append(p)
        user_names[uid] = p.get("user", "?")
        user_heads[uid] = p.get("show_user_head", "")

    rows = []
    for uid in sorted(user_posts.keys(), key=lambda u: -len(user_posts[u])):
        p_list = user_posts[uid]
        name = user_names.get(uid, "?")
        cats = sorted(set(p.get("category", "?") for p in p_list))
        cats_str = ", ".join(cats[:5])
        if len(cats) > 5:
            cats_str += f" +{len(cats) - 5}"

        detail_parts = []
        for p in p_list:
            content = p.get("content", "")[:300].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            detail_parts.append(
                '<div class="post-item">'
                f'<div class="post-meta-row">'
                f'<span class="post-cat">[{p.get("category", "?")}]</span> '
                f'<span class="post-id">#{p.get("id", "?")}</span> '
                f'<span class="post-time">{p.get("time", "?")}</span> '
                f'<span style="color:#666;font-size:0.8em;">L{p.get("stars", 0)} C{p.get("comments", 0)}</span>'
                f'</div>'
                f'<div class="post-content">{content}</div>'
                f'</div>'
            )
            for c in p.get("comment_list", []):
                c_uid = c.get("show_user_id", "")
                if c_uid and c_uid != uid:
                    c_detail = (c.get("detail", "") or "")[:120].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    detail_parts.append(
                        f'<div class="comment-item">'
                        f'<div class="cmt-from">← {c.get("show_user_name", "?")} (ID:{c_uid})</div>'
                        f'<div class="cmt-text">{c_detail}</div>'
                        f'</div>'
                    )
                for nr in c.get("reply_comment_list", []):
                    nr_uid = nr.get("show_user_id", "")
                    if nr_uid and nr_uid != uid:
                        nr_detail = (nr.get("detail", "") or "")[:120].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        reply_to = nr.get("reply_show_user_id", "")
                        arrow = "→ " + nr.get("reply_show_user_name", "?") if reply_to == uid else "← "
                        detail_parts.append(
                            f'<div class="comment-item">'
                            f'<div class="cmt-from">{arrow} {nr.get("show_user_name", "?")} (ID:{nr_uid})</div>'
                            f'<div class="cmt-text">{nr_detail}</div>'
                            f'</div>'
                        )

        detail_html = "".join(detail_parts)
        data_text = f"{uid} {name} {cats_str}"
        head_url = user_heads.get(uid, "")
        img_tag = ""
        if head_url:
            img_tag = f'<img src="{head_url}" style="width:24px;height:24px;border-radius:50%;margin-right:8px;vertical-align:middle;" onerror="this.style.display=\'none\'">'

        rows.append(
            '<div>'
            f'<div class="user-row" onclick="toggleUser(\'{uid}\')" data-text="{data_text}">'
            f'<div>{img_tag}<span class="uid">ID:{uid}</span><span class="uname">{name}</span><span class="cats">{cats_str}</span></div>'
            f'<span class="count">{len(p_list)} post(s)</span>'
            f'</div>'
            f'<div class="user-detail" id="detail-{uid}">{detail_html}</div>'
            f'</div>'
        )

    return "\n".join(rows) if rows else '<div class="no-data">No data with show_user_id found.</div>'


# ==================== TEMPLATE RENDERING ====================

def _read_template(name):
    """Read an HTML template file. Returns content or None if missing."""
    path = os.path.join(TEMPLATES_DIR, name)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return None


def render_template(name, **kwargs):
    """Read template and replace __PLACEHOLDER__ values."""
    html = _read_template(name)
    if html is None:
        return f"<html><body><h1>Error</h1><p>Template '{name}' not found in {TEMPLATES_DIR}</p></body></html>"
    for key, value in kwargs.items():
        html = html.replace(f"__{key}__", str(value))
    return html


# ==================== API HANDLERS ====================

def _safe_post(post):
    """Return a copy of post without sensitive/internal fields for public API."""
    return {
        "id": post["id"],
        "content": post["content"],
        "category": post.get("category", ""),
        "user": post.get("user", ""),
        "time": post.get("time", ""),
        "comments": post.get("comments", 0),
        "stars": post.get("stars", 0),
        "trace": post.get("trace", 0),
        "views": post.get("views", 0),
        "hot": post.get("hot", 0),
    }


def _admin_post(post):
    """Return post with ALL fields for admin API."""
    p = _safe_post(post)
    p["show_user_id"] = post.get("show_user_id", "")
    p["real_user_id"] = post.get("real_user_id", "0")
    p["comment_list"] = post.get("comment_list", [])
    return p


def api_search(query, sort_by, page, limit, category=None, date_from=None, date_to=None,
               scope="all", uid=None, uname=None, admin=False):
    """Search posts with optional filters."""
    posts, _, _, _ = get_cached_data()

    # ---- Keyword filter ----
    if query:
        keywords = query.lower().split()
        search_field = "_search_content" if scope == "content" else "_search_all"
        results = []
        for p in posts:
            search_text = p.get(search_field, "")
            # Also search show_user_id and show_user_name when admin mode
            if admin:
                search_text += " " + p.get("show_user_id", "")
                search_text += " " + p.get("user", "")
            if all(kw in search_text for kw in keywords):
                results.append(p)
    else:
        results = list(posts)

    # ---- UID filter ----
    if uid:
        results = [p for p in results if p.get("show_user_id", "") == uid]

    # ---- User name filter ----
    if uname:
        uname_lower = uname.lower()
        results = [p for p in results if uname_lower in p.get("user", "").lower()]

    # ---- Category filter ----
    if category:
        results = [p for p in results if p.get("category", "") == category]

    # ---- Time range filter ----
    if date_from or date_to:
        filtered = []
        for p in results:
            dt = p.get("_time_dt")
            if dt is None:
                continue  # skip posts with unparseable time
            if date_from and dt < date_from:
                continue
            if date_to and dt > date_to:
                continue
            filtered.append(p)
        results = filtered

    # ---- Sort ----
    if sort_by == "stars":
        results.sort(key=lambda p: (-p.get("stars", 0), -int(p["id"])))
    elif sort_by == "views":
        results.sort(key=lambda p: (-p.get("views", 0), -int(p["id"])))
    elif sort_by == "hot":
        results.sort(key=lambda p: (-p.get("hot", 0), -int(p["id"])))
    # Default (time): already sorted by id desc

    # ---- Paginate ----
    total = len(results)
    total_pages = max(1, (total + limit - 1) // limit)
    page = max(1, min(page, total_pages))
    start = (page - 1) * limit
    page_results = results[start:start + limit]

    return {
        "total": total,
        "page": page,
        "page_size": limit,
        "total_pages": total_pages,
        "results": [_admin_post(p) if admin else _safe_post(p) for p in page_results],
    }


def api_categories():
    """Return sorted list of categories with >= 3 posts (filter noise)."""
    posts, _, _, _ = get_cached_data()
    from collections import Counter
    cnt = Counter(p.get("category", "") for p in posts if p.get("category"))
    # Only show categories with enough posts to be useful (>= 5)
    cats = sorted(c for c, n in cnt.items() if n >= 5)
    return {"categories": cats}


def api_comments(post_id):
    """Return comments for a single post."""
    post = get_post_by_id(post_id)
    if post is None:
        return None
    comment_list = post.get("comment_list", [])
    # Truncate if too many comments
    if len(comment_list) > COMMENT_LIMIT:
        comment_list = comment_list[:COMMENT_LIMIT]
    return {
        "post_id": post_id,
        "comment_count": len(post.get("comment_list", [])),
        "comment_list": comment_list,
    }


# ==================== HTTP REQUEST HANDLER ====================

class Handler(BaseHTTPRequestHandler):
    """HTTP request handler with routing."""

    # ---- Cookie helpers ----
    def _set_cookie(self, name, value, max_age=SESSION_TTL):
        self.send_header("Set-Cookie", f"{name}={value}; Path=/; HttpOnly; Max-Age={max_age}")

    def _get_cookie(self, name):
        cookie_hdr = self.headers.get("Cookie", "")
        for item in cookie_hdr.split(";"):
            item = item.strip()
            if "=" in item:
                k, v = item.split("=", 1)
                if k == name:
                    return v
        return None

    def _is_admin(self):
        token = self._get_cookie("admin_token")
        return is_valid_session(token)

    # ---- Response helpers ----
    def _serve_html(self, html, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _serve_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    # ---- Query param parsing ----
    def _parse_query(self):
        parsed = urlparse(self.path)
        return parse_qs(parsed.query), parsed.path

    # ---- Main page ----
    def _handle_main(self):
        posts, crawl_time, has_danger, csv_path = get_cached_data()
        if posts:
            latest_time = max((p["time"] for p in posts), default="?")
            earliest_time = min((p["time"] for p in posts), default="?")
        else:
            latest_time = earliest_time = "?"

        html = render_template(
            "main.html",
            TOTAL=len(posts),
            CRAWL_TIME=crawl_time or "?",
            EARLIEST_TIME=earliest_time,
            LATEST_TIME=latest_time,
        )
        self._serve_html(html)

    # ---- Admin GET ----
    def _handle_admin_get(self):
        params, _ = self._parse_query()

        # Logout
        if "logout" in params:
            self.send_response(302)
            self._set_cookie("admin_token", "deleted", max_age=1)
            self.send_header("Location", "/admin")
            self.end_headers()
            return

        # Not logged in → show login form
        if not self._is_admin():
            csrf = create_csrf_token()
            html = render_template("admin_login.html", CSRF_TOKEN=csrf, ERROR="")
            self._serve_html(html)
            return

        # Logged in → show dashboard
        self._serve_admin_dashboard()

    # ---- Admin POST (login) ----
    def _handle_admin_post(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8", errors="replace")
        params = parse_qs(body)
        password = params.get("password", [""])[0]
        csrf = params.get("csrf_token", [""])[0]

        if not verify_csrf_token(csrf):
            new_csrf = create_csrf_token()
            html = render_template("admin_login.html", CSRF_TOKEN=new_csrf, ERROR='<div class="error">表单已过期，请重新输入</div>')
            self._serve_html(html)
            return

        if password == get_password():
            token = create_session()
            self.send_response(302)
            self._set_cookie("admin_token", token)
            self.send_header("Location", "/admin")
            self.end_headers()
        else:
            new_csrf = create_csrf_token()
            html = render_template("admin_login.html", CSRF_TOKEN=new_csrf, ERROR='<div class="error">密码错误</div>')
            self._serve_html(html)

    def _serve_admin_dashboard(self):
        posts, _crawl_time, has_danger, csv_path = get_cached_data()
        unique_users, multi, total_comments, unique_commenters = count_stats(posts)

        if has_danger:
            user_rows = build_user_rows(posts)
        else:
            user_rows = '<div class="no-data">当前是旧版CSV，缺少 show_user_id 字段。<br><br>请运行 spider_danger.py 采集完整数据。</div>'

        html = render_template(
            "admin_dashboard.html",
            CSV_SOURCE=os.path.basename(csv_path) if csv_path else "?",
            TOTAL=len(posts),
            UNIQUE_USERS=unique_users,
            MULTI=multi,
            TOTAL_COMMENTS=total_comments,
            UNIQUE_COMMENTERS=unique_commenters,
            DANGER="完整数据(含ID)" if has_danger else "旧版数据(无ID)",
            USER_ROWS=user_rows,
        )
        self._serve_html(html)

    # ---- API: search ----
    def _handle_api_search(self):
        params, _ = self._parse_query()
        q = params.get("q", [""])[0].strip()
        sort_by = params.get("sort", ["time"])[0]
        if sort_by not in ("time", "stars", "views", "hot"):
            sort_by = "time"
        try:
            page = int(params.get("page", ["1"])[0])
            limit = int(params.get("limit", ["50"])[0])
        except ValueError:
            page, limit = 1, 50
        page = max(1, page)
        limit = max(1, min(limit, 200))

        # --- Optional filters ---
        category = params.get("category", [""])[0].strip() or None
        uid = params.get("uid", [""])[0].strip() or None
        uname = params.get("uname", [""])[0].strip() or None
        scope = params.get("scope", ["content"])[0]
        if scope not in ("all", "content"):
            scope = "content"
        admin = self._is_admin()

        # Time range: accept "date" preset or explicit "from"/"to" timestamps
        date_from = date_to = None
        date_preset = params.get("date", [""])[0].strip()
        if date_preset:
            now = datetime.now()
            if date_preset == "1d":
                date_from = now.replace(hour=0, minute=0, second=0, microsecond=0)
            elif date_preset == "3d":
                date_from = now - timedelta(days=3)
            elif date_preset == "7d":
                date_from = now - timedelta(days=7)
            elif date_preset == "30d":
                date_from = now - timedelta(days=30)
        else:
            # Explicit from/to
            try:
                from_str = params.get("from", [""])[0]
                if from_str:
                    date_from = datetime.strptime(from_str[:19], "%Y-%m-%d %H:%M:%S")
                to_str = params.get("to", [""])[0]
                if to_str:
                    date_to = datetime.strptime(to_str[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

        result = api_search(q, sort_by, page, limit,
                            category=category, date_from=date_from, date_to=date_to,
                            scope=scope, uid=uid, uname=uname, admin=admin)
        self._serve_json(result)

    # ---- API: feedback ----
    def _handle_api_feedback(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
            name = (data.get("name") or "").strip()[:50]
            message = (data.get("message") or "").strip()[:2000]
            if not message:
                self._serve_json({"ok": False, "error": "message required"}, 400)
                return
        except json.JSONDecodeError:
            self._serve_json({"ok": False, "error": "invalid json"}, 400)
            return

        feedback_file = os.path.join(DATA_DIR, "feedback.jsonl")
        with open(feedback_file, "a", encoding="utf-8") as f:
            json.dump({"name": name, "message": message, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False)
            f.write("\n")
        self._serve_json({"ok": True})

    # ---- API: categories ----
    def _handle_api_categories(self):
        self._serve_json(api_categories())

    # ---- API: comments ----
    def _handle_api_comments(self):
        params, _ = self._parse_query()
        post_id = params.get("id", [""])[0]

        if not post_id:
            self._serve_json({"error": "Missing post id"}, code=400)
            return

        result = api_comments(post_id)
        if result is None:
            self._serve_json({"error": "Post not found"}, code=404)
            return

        self._serve_json(result)

    # ---- Healthcheck ----
    def _handle_healthcheck(self):
        self._serve_json({"ok": True})

    # ---- API: check-in ----
    def _handle_api_checkin_get(self):
        self._serve_json({"count": get_checkin_count()})

    def _handle_api_checkin_post(self):
        self._serve_json({"count": increment_checkin_count()})

    # ---- ROUTING ----
    def do_GET(self):
        _, path = self._parse_query()

        if path == "/api/search":
            self._handle_api_search()
        elif path == "/api/comments":
            self._handle_api_comments()
        elif path == "/api/categories":
            self._handle_api_categories()
        elif path == "/api/checkin":
            self._handle_api_checkin_get()
        elif path == "/healthz":
            self._handle_healthcheck()
        elif path == "/admin":
            self._handle_admin_get()
        elif path == "/" or path == "":
            self._handle_main()
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"404 Not Found")

    def do_POST(self):
        _, path = self._parse_query()

        if path == "/admin":
            self._handle_admin_post()
        elif path == "/api/checkin":
            self._handle_api_checkin_post()
        elif path == "/api/feedback":
            self._handle_api_feedback()
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"404 Not Found")

    def log_message(self, fmt, *args):
        # Cleaner log format: [timestamp] method path status
        print(f"[{self.address_string()}] {args[0]} {args[1]} {args[2]}")


# ==================== THREADING SERVER ====================

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTPServer with thread-per-request concurrency."""
    daemon_threads = True


# ==================== MAIN ====================

if __name__ == "__main__":
    import socket

    port = int(os.environ.get("PORT", 8080))
    HOST = os.environ.get("HOST", "0.0.0.0")

    # Pre-load password (triggers generation if needed)
    pwd = get_password()

    # Pre-load data cache
    print("[init] Loading data...")
    refresh_cache()
    posts, crawl_time, has_danger, csv_path = get_cached_data()
    print(f"[init] {len(posts)} posts loaded from {os.path.basename(csv_path) if csv_path else 'N/A'}")

    local_ip = socket.gethostbyname(socket.gethostname())
    print(f"\n  RUC小喇叭 搜索服务已启动")
    print(f"  本地:    http://127.0.0.1:{port}")
    print(f"  局域网:  http://{local_ip}:{port}")
    print(f"  Admin:   http://127.0.0.1:{port}/admin")
    if not os.path.exists(PASSWORD_FILE) or os.path.getsize(PASSWORD_FILE) < 8:
        print(f"  管理员密码: {pwd}")
    print()

    ThreadingHTTPServer((HOST, port), Handler).serve_forever()
