"""
Web search for RUC-Xiaolaba — reads SQLite posts DB, provides search API + admin panel.

Features:
  - ThreadingHTTPServer (multi-client concurrent)
  - /api/search?q=...&sort=...&page=...&limit=50
  - /api/comments?id=... (lazy comment loading)
  - Admin panel with CSRF + session auth
  - Random password generation on first run
  - Template-based rendering (templates/*.html)
"""
import json
import os
import secrets
import sqlite3
import time
import threading
import html as _html
import string as _string
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

# ==================== CONFIG ====================

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
def _sqlite_candidate_info(path):
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(path)
        row = conn.execute(
            "select count(*), max(nullif(create_time, '')) from posts"
        ).fetchone()
        conn.close()
        return {
            "path": path,
            "count": int(row[0] or 0),
            "latest": row[1] or "",
            "mtime": os.path.getmtime(path),
        }
    except sqlite3.Error:
        return {
            "path": path,
            "count": 0,
            "latest": "",
            "mtime": os.path.getmtime(path),
        }


def choose_sqlite_db(explicit_path=None):
    if explicit_path:
        return explicit_path
    env_path = os.environ.get("SQLITE_DB")
    if env_path:
        return env_path
    candidates = [os.path.join(DATA_DIR, "posts.db")]
    infos = [info for info in (_sqlite_candidate_info(p) for p in candidates) if info]
    if not infos:
        return candidates[0]
    infos.sort(key=lambda x: (x["latest"], x["mtime"]), reverse=True)
    return infos[0]["path"]


SQLITE_DB = choose_sqlite_db()
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


def _safe_int(value, default=0):
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


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



# ==================== SQLITE BACKEND ====================

def sqlite_connect():
    conn = sqlite3.connect(SQLITE_DB)
    conn.row_factory = sqlite3.Row
    return conn


def sqlite_public_post(row):
    return {
        "id": row["id"],
        "content": row["content"],
        "category": row["category_name"],
        "user": row["user_name"],
        "time": row["create_time"],
        "comments": _safe_int(row["comment_count"]),
        "stars": _safe_int(row["star_count"]),
        "trace": _safe_int(row["trace_count"]),
        "views": _safe_int(row["views"]),
        "hot": _safe_int(row["hot"]),
    }


def sqlite_overview():
    if not os.path.exists(SQLITE_DB):
        return {"total": 0, "earliest": "?", "latest": "?", "crawl_time": "?"}
    with sqlite_connect() as conn:
        row = conn.execute(
            """
            select count(*) as total,
                   min(nullif(create_time, '')) as earliest,
                   max(nullif(create_time, '')) as latest
            from posts
            """
        ).fetchone()
    crawl_time = datetime.fromtimestamp(os.path.getmtime(SQLITE_DB)).strftime("%Y-%m-%d %H:%M")
    return {
        "total": row["total"] if row else 0,
        "earliest": row["earliest"] or "?",
        "latest": row["latest"] or "?",
        "crawl_time": crawl_time,
    }


def sqlite_admin_stats_and_rows(limit=40):
    if not os.path.exists(SQLITE_DB):
        return {
            "total": 0,
            "unique_users": 0,
            "multi": 0,
            "total_comments": 0,
            "unique_commenters": 0,
            "user_rows": '<div class="no-data">SQLite DB not found.</div>',
        }

    with sqlite_connect() as conn:
        stats = conn.execute(
            """
            select
              (select count(*) from posts) as total,
              (select count(distinct show_user_id) from posts where show_user_id != '') as unique_users,
              (select count(*) from (
                   select show_user_id from posts where show_user_id != ''
                   group by show_user_id having count(*) >= 2
               )) as multi,
              (select count(*) from comments) as total_comments
            """
        ).fetchone()
        users = conn.execute(
            """
            select show_user_id, max(user_name) as user_name, count(*) as post_count,
                   group_concat(distinct category_name) as categories
            from posts
            where show_user_id != ''
            group by show_user_id
            order by post_count desc
            limit ?
            """,
            (limit,),
        ).fetchall()
        user_ids = [u["show_user_id"] for u in users]
        posts_by_uid = {uid: [] for uid in user_ids}
        if user_ids:
            placeholders = ",".join("?" for _ in user_ids)
            post_rows = conn.execute(
                f"""
                select show_user_id, id, content, category_name, create_time, star_count, comment_count
                from posts
                where show_user_id in ({placeholders})
                order by show_user_id, create_time desc, cast(id as integer) desc
                """,
                user_ids,
            ).fetchall()
            for post in post_rows:
                bucket = posts_by_uid.get(post["show_user_id"])
                if bucket is not None and len(bucket) < 12:
                    bucket.append(post)

        rows = []
        for user in users:
            uid = user["show_user_id"]
            posts = posts_by_uid.get(uid, [])
            name = _html.escape(user["user_name"] or "?")
            cats = ", ".join((user["categories"] or "").split(",")[:5])
            cats_html = _html.escape(cats)
            detail_parts = []
            for p in posts:
                content = _html.escape((p["content"] or "")[:300])
                detail_parts.append(
                    '<div class="post-item">'
                    '<div class="post-meta-row">'
                    f'<span class="post-cat">[{_html.escape(p["category_name"] or "?")}]</span> '
                    f'<span class="post-id">#{_html.escape(p["id"])}</span> '
                    f'<span class="post-time">{_html.escape((p["create_time"] or "")[:19])}</span> '
                    f'<span style="color:#666;font-size:0.8em;">L{p["star_count"]} C{p["comment_count"]}</span>'
                    '</div>'
                    f'<div class="post-content">{content}</div>'
                    '</div>'
                )
            rows.append(
                '<div>'
                f'<div class="user-row" onclick="toggleUser(\'{_html.escape(uid, quote=True)}\')" data-text="{_html.escape(uid + " " + (user["user_name"] or "") + " " + cats, quote=True)}">'
                f'<div><span class="uid">ID:{_html.escape(uid)}</span><span class="uname">{name}</span><span class="cats">{cats_html}</span></div>'
                f'<span class="count">{user["post_count"]} post(s)</span>'
                '</div>'
                f'<div class="user-detail" id="detail-{_html.escape(uid, quote=True)}">{"".join(detail_parts)}</div>'
                '</div>'
            )

    return {
        "total": stats["total"],
        "unique_users": stats["unique_users"],
        "multi": stats["multi"],
        "total_comments": stats["total_comments"],
        "unique_commenters": "按需检索",
        "user_rows": "\n".join(rows) if rows else '<div class="no-data">No data with show_user_id found.</div>',
    }


def sqlite_has_search_index():
    if not os.path.exists(SQLITE_DB):
        return False
    with sqlite_connect() as conn:
        row = conn.execute("select 1 from sqlite_master where name = 'search_index' and type = 'table'").fetchone()
    return row is not None


def sqlite_fts_query(keywords):
    # SQLite trigram FTS does not match 1-2 character CJK queries. Keep LIKE fallback for those.
    if not keywords or any(len(kw) < 3 for kw in keywords):
        return None
    quoted = []
    for kw in keywords:
        safe = kw.replace('"', '""')
        quoted.append(f'"{safe}"')
    return " AND ".join(quoted)


def sqlite_search_where(query, category=None, date_from=None, date_to=None, scope="content",
                        uid=None, uname=None, admin=False, use_fts=False,
                        identity=None, admin_fields=None):
    clauses = []
    args = []
    keywords = (query or "").lower().split()
    fields = admin_fields or {"body", "cmt", "uid", "name"}
    fts_query = sqlite_fts_query(keywords) if scope == "all" and use_fts and not admin else None
    if fts_query:
        clauses.append("p.id in (select post_id from search_index where body match ?)")
        args.append(fts_query)
    else:
        for kw in keywords:
            like = f"%{kw}%"
            if admin:
                field_clauses = []
                if "body" in fields:
                    field_clauses.append("(lower(p.content) like ? or p.id like ?)")
                    args.extend([like, like])
                if "cmt" in fields:
                    field_clauses.append("p.id in (select post_id from comments where lower(detail) like ?)")
                    args.append(like)
                if "uid" in fields:
                    field_clauses.append(
                        "("
                        "p.show_user_id like ? or p.real_user_id like ? or "
                        "p.id in (select post_id from comments where show_user_id like ? or real_user_id like ? or reply_show_user_id like ?)"
                        ")"
                    )
                    args.extend([like, like, like, like, like])
                if "name" in fields:
                    field_clauses.append(
                        "("
                        "lower(p.user_name) like ? or "
                        "p.id in (select post_id from comments where lower(show_user_name) like ? or lower(reply_show_user_name) like ?)"
                        ")"
                    )
                    args.extend([like, like, like])
                clauses.append("(" + " or ".join(field_clauses or ["0"]) + ")")
            elif scope == "all":
                clauses.append(
                    "p.id in ("
                    "select id from posts where lower(content) like ? or id like ? "
                    "union select post_id from comments where lower(detail) like ?"
                    ")"
                )
                args.extend([like, like, like])
            else:
                clauses.append("(lower(p.content) like ? or p.id like ?)")
                args.extend([like, like])
    if category:
        clauses.append("p.category_name = ?")
        args.append(category)
    if date_from:
        clauses.append("p.create_time >= ?")
        args.append(date_from.strftime("%Y-%m-%d %H:%M:%S"))
    if date_to:
        clauses.append("p.create_time <= ?")
        args.append(date_to.strftime("%Y-%m-%d %H:%M:%S"))
    if admin and uid:
        clauses.append("p.show_user_id = ?")
        args.append(uid)
    if admin and uname:
        clauses.append("lower(p.user_name) like ?")
        args.append(f"%{uname.lower()}%")
    if admin and identity == "anonymous":
        clauses.append("(p.real_user_id = '' or p.real_user_id = '0')")
    if admin and identity == "real":
        clauses.append("(p.real_user_id != '' and p.real_user_id != '0')")
    return (" where " + " and ".join(clauses)) if clauses else "", args


def api_search_sqlite(query, sort_by, page, limit, category=None, date_from=None, date_to=None,
                      scope="content", uid=None, uname=None, admin=False,
                      identity=None, admin_fields=None):
    if not os.path.exists(SQLITE_DB):
        return {"total": 0, "page": 1, "page_size": limit, "total_pages": 1, "results": []}

    order_map = {
        "time": "p.create_time desc, p.id desc",
        "stars": "p.star_count desc, cast(p.id as integer) desc",
        "views": "p.views desc, cast(p.id as integer) desc",
        "hot": "p.hot desc, cast(p.id as integer) desc",
    }
    order_by = order_map.get(sort_by, order_map["time"])
    use_fts = scope == "all" and bool(query) and sqlite_has_search_index()
    where_sql, args = sqlite_search_where(
        query, category, date_from, date_to, scope, uid, uname, admin,
        use_fts=use_fts, identity=identity, admin_fields=admin_fields
    )

    with sqlite_connect() as conn:
        total = conn.execute(f"select count(*) from posts p{where_sql}", args).fetchone()[0]
        total_pages = max(1, (total + limit - 1) // limit)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * limit
        rows = conn.execute(
            f"""
            select p.id, p.content, p.category_name, p.user_name, p.create_time,
                   p.comment_count, p.star_count, p.trace_count, p.views, p.hot,
                   p.show_user_id, p.real_user_id
            from posts p
            {where_sql}
            order by {order_by}
            limit ? offset ?
            """,
            args + [limit, offset],
        ).fetchall()

    results = []
    for row in rows:
        item = sqlite_public_post(row)
        if admin:
            item["show_user_id"] = row["show_user_id"]
            item["real_user_id"] = row["real_user_id"]
        results.append(item)
    return {"total": total, "page": page, "page_size": limit, "total_pages": total_pages, "results": results}


def api_categories_sqlite():
    if not os.path.exists(SQLITE_DB):
        return {"categories": []}
    with sqlite_connect() as conn:
        rows = conn.execute(
            """
            select category_name
            from posts
            where category_name != ''
            group by category_name
            having count(*) >= 5
            order by category_name
            """
        ).fetchall()
    return {"categories": [r["category_name"] for r in rows]}


def public_comment_from_row(row, include_sensitive=False):
    item = {
        "detail": row["detail"],
        "show_user_name": row["show_user_name"],
        "create_time": row["create_time"],
        "is_publisher": row["is_publisher"],
        "reply_show_user_name": row["reply_show_user_name"],
        "reply_comment_list": [],
    }
    if include_sensitive:
        item["show_user_id"] = row["show_user_id"]
        item["real_user_id"] = row["real_user_id"]
        item["reply_show_user_id"] = row["reply_show_user_id"]
    return item


def api_comments_sqlite(post_id, admin=False):
    if not os.path.exists(SQLITE_DB):
        return None
    with sqlite_connect() as conn:
        post = conn.execute("select comment_count from posts where id = ?", (post_id,)).fetchone()
        if post is None:
            return None
        rows = conn.execute(
            """
            select comment_id, parent_comment_id, detail, show_user_name, create_time,
                   show_user_id, real_user_id, is_publisher, reply_show_user_name, reply_show_user_id
            from comments
            where post_id = ?
            order by create_time, row_key
            limit ?
            """,
            (post_id, COMMENT_LIMIT * 3),
        ).fetchall()

    top = []
    by_id = {}
    for row in rows:
        item = public_comment_from_row(row, include_sensitive=admin)
        if row["parent_comment_id"]:
            parent = by_id.get(row["parent_comment_id"])
            if parent is not None:
                parent["reply_comment_list"].append(item)
            else:
                top.append(item)
        else:
            top.append(item)
            by_id[row["comment_id"]] = item
    return {"post_id": post_id, "comment_count": post["comment_count"], "comment_list": top[:COMMENT_LIMIT]}


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
        overview = sqlite_overview()
        html = render_template(
            "main.html",
            TOTAL=overview["total"],
            CRAWL_TIME=overview["crawl_time"] or "?",
            EARLIEST_TIME=overview["earliest"],
            LATEST_TIME=overview["latest"],
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
        stats = sqlite_admin_stats_and_rows()
        html = render_template(
            "admin_dashboard.html",
            CSV_SOURCE=f"SQLite · {os.path.basename(SQLITE_DB)}",
            TOTAL=stats["total"],
            UNIQUE_USERS=stats["unique_users"],
            MULTI=stats["multi"],
            TOTAL_COMMENTS=stats["total_comments"],
            UNIQUE_COMMENTERS=stats["unique_commenters"],
            DANGER="SQLite 数据(含ID)",
            USER_ROWS=stats["user_rows"],
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
        admin_fields = None
        if admin:
            raw_fields = params.get("admin_fields", [""])[0].strip()
            allowed_fields = {"body", "cmt", "uid", "name"}
            if raw_fields:
                admin_fields = {f for f in raw_fields.split(",") if f in allowed_fields}
            else:
                admin_fields = {"body", "cmt", "uid", "name"}
            if not admin_fields:
                admin_fields = allowed_fields
        identity = params.get("identity", [""])[0].strip()
        if identity not in ("", "anonymous", "real"):
            identity = ""
        if not admin:
            identity = ""

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

        result = api_search_sqlite(
            q, sort_by, page, limit,
            category=category, date_from=date_from, date_to=date_to,
            scope=scope, uid=uid, uname=uname, admin=admin,
            identity=identity or None, admin_fields=admin_fields,
        )
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
        self._serve_json(api_categories_sqlite())

    # ---- API: comments ----
    def _handle_api_comments(self):
        params, _ = self._parse_query()
        post_id = params.get("id", [""])[0]

        if not post_id:
            self._serve_json({"error": "Missing post id"}, code=400)
            return

        result = api_comments_sqlite(post_id, admin=self._is_admin())
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
    import argparse
    import socket

    parser = argparse.ArgumentParser(description="Run RUC Xiaolaba search server")
    parser.add_argument("--db", action="store_true", help="accepted for compatibility; SQLite is always used")
    parser.add_argument("--sqlite-db", default=None, help="SQLite DB path")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    args = parser.parse_args()

    SQLITE_DB = choose_sqlite_db(args.sqlite_db)

    port = args.port
    HOST = args.host

    # Pre-load password (triggers generation if needed)
    pwd = get_password()

    overview = sqlite_overview()
    print(
        f"[init] SQLite backend: {overview['total']} posts from {os.path.abspath(SQLITE_DB)} "
        f"(latest={overview['latest']})"
    )

    local_ip = socket.gethostbyname(socket.gethostname())
    print(f"\n  RUC小喇叭 搜索服务已启动")
    print(f"  本地:    http://127.0.0.1:{port}")
    print(f"  局域网:  http://{local_ip}:{port}")
    print(f"  Admin:   http://127.0.0.1:{port}/admin")
    print(f"  Backend: sqlite ({SQLITE_DB})")
    if not os.path.exists(PASSWORD_FILE) or os.path.getsize(PASSWORD_FILE) < 8:
        print(f"  管理员密码: {pwd}")
    print()

    ThreadingHTTPServer((HOST, port), Handler).serve_forever()
