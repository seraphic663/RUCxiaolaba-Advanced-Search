"""Web application assembly and standard-library HTTP transport."""

from __future__ import annotations

import argparse
import json
import os
import socket
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

from app.config import AppConfig, choose_posts_db
from app.http.router import dispatch
from app.repositories.connections import connect_readonly
from app.repositories.post_repository import PostRepository
from app.repositories.search_repository import bigram_query, fts_query
from app.services.admin_crawl_service import AdminCrawlService
from app.services.admin_service import AdminService
from app.services.auth_service import AdminAuthService
from app.services.search_service import SearchService
from app.services.template_service import TemplateService

SESSION_TTL = 86400
CSRF_TTL = 3600
COMMENT_LIMIT = 500

APP_CONFIG = AppConfig.from_env()
DATA_DIR = str(APP_CONFIG.data_dir)
TEMPLATES_DIR = str(APP_CONFIG.templates_dir)
SQLITE_DB = str(APP_CONFIG.posts_db)
BIGRAM_DB = str(APP_CONFIG.bigram_db or "")
SYMBOL_DB = str(APP_CONFIG.symbol_db or "")
PASSWORD_FILE = str(APP_CONFIG.admin_password_file)


def choose_sqlite_db(explicit_path=None):
    return str(choose_posts_db(explicit_path))


def get_password() -> str:
    password = os.environ.get("ADMIN_PASSWORD", "").strip()
    if password:
        return password
    path = Path(PASSWORD_FILE)
    if path.exists():
        password = path.read_text(encoding="utf-8").strip()
        if password:
            return password
    raise RuntimeError(
        "admin password missing: set ADMIN_PASSWORD or create "
        "data/admin_password.txt"
    )


def sqlite_connect():
    return connect_readonly(SQLITE_DB, BIGRAM_DB or None, SYMBOL_DB or None)


def _search_service() -> SearchService:
    return SearchService(SQLITE_DB, BIGRAM_DB or None, SYMBOL_DB or None)


def sqlite_overview():
    return PostRepository(SQLITE_DB).overview()


def sqlite_admin_stats_and_rows(limit=40):
    return AdminService(SQLITE_DB).dashboard(limit)


def sqlite_fts_query(keywords):
    return fts_query(keywords)


def sqlite_bigram_query(keyword):
    return bigram_query(keyword)


def sqlite_has_search_index():
    return _search_service().repository.has_search_index()


def sqlite_has_bigram_index():
    return _search_service().repository.has_bigram_index()


def sqlite_has_symbol_index():
    return _search_service().repository.has_symbol_index()


def api_search_sqlite(
    query,
    sort_by,
    page,
    limit,
    category=None,
    date_from=None,
    date_to=None,
    scope="content",
    uid=None,
    uname=None,
    admin=False,
    identity=None,
    admin_fields=None,
    id_match="exact",
    name_match="exact",
):
    return _search_service().search(
        query,
        sort_by,
        page,
        limit,
        category=category,
        date_from=date_from,
        date_to=date_to,
        scope=scope,
        uid=uid,
        uname=uname,
        admin=admin,
        identity=identity,
        admin_fields=admin_fields,
        id_match=id_match,
        name_match=name_match,
    )


def api_categories_sqlite():
    return _search_service().categories()


def api_comments_sqlite(post_id, admin=False):
    return _search_service().comments(post_id, admin=admin)


def render_template(name, **values):
    return TemplateService(TEMPLATES_DIR).render(name, **values)


_compat_auth = AdminAuthService(SESSION_TTL, CSRF_TTL)


def create_session():
    return _compat_auth.create_session()


def is_valid_session(token):
    return _compat_auth.is_valid_session(token)


def create_csrf_token():
    return _compat_auth.create_csrf_token()


def verify_csrf_token(token):
    return _compat_auth.verify_csrf_token(token)


@dataclass
class ApplicationContext:
    posts_db: str
    admin_password: str
    posts: PostRepository
    search: SearchService
    admin: AdminService
    admin_crawl: AdminCrawlService
    auth: AdminAuthService
    templates: TemplateService


def build_context() -> ApplicationContext:
    return ApplicationContext(
        posts_db=SQLITE_DB,
        admin_password=get_password(),
        posts=PostRepository(SQLITE_DB),
        search=SearchService(SQLITE_DB, BIGRAM_DB or None, SYMBOL_DB or None),
        admin=AdminService(SQLITE_DB),
        admin_crawl=AdminCrawlService(
            SQLITE_DB,
            config_path=os.environ.get(
                "CRAWLER_CONFIG",
                str(Path(SQLITE_DB).with_name("config.txt")),
            ),
            bigram_db=BIGRAM_DB or None,
            symbol_db=SYMBOL_DB or None,
        ),
        auth=AdminAuthService(SESSION_TTL, CSRF_TTL),
        templates=TemplateService(TEMPLATES_DIR),
    )


class Handler(BaseHTTPRequestHandler):
    context: ApplicationContext

    def set_cookie(self, name, value, max_age=SESSION_TTL):
        self.send_header(
            "Set-Cookie",
            f"{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}",
        )

    def get_cookie(self, name):
        for item in self.headers.get("Cookie", "").split(";"):
            if "=" in item:
                key, value = item.strip().split("=", 1)
                if key == name:
                    return value
        return None

    def is_admin(self):
        return self.context.auth.is_valid_session(self.get_cookie("admin_token"))

    def client_ip(self):
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",", 1)[0]
        return forwarded.strip() or self.client_address[0]

    def serve_html(self, content, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def serve_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def parse_query(self):
        parsed = urlparse(self.path)
        return parse_qs(parsed.query), parsed.path

    def read_body(self, max_bytes=None):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if max_bytes is not None and length > max_bytes:
            raise ValueError("request_too_large")
        return self.rfile.read(length) if length else b""

    def _dispatch(self, method):
        _, path = self.parse_query()
        try:
            if dispatch(self, method, path):
                return
        except (BrokenPipeError, ConnectionResetError):
            # The client or reverse proxy gave up before a slow response was
            # written. The request is already over; do not emit a traceback or
            # attempt a second response on the closed socket.
            print(f"[disconnect] {method} {path}", flush=True)
            return
        except Exception as exc:
            print(f"[error] {method} {path}: {exc}", flush=True)
            traceback.print_exc()
            try:
                self.serve_json(
                    {"ok": False, "error": "服务器处理请求失败，请稍后重试"},
                    code=500,
                )
            except (BrokenPipeError, ConnectionResetError):
                print(f"[disconnect] {method} {path} while reporting error", flush=True)
            return
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"404 Not Found")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, fmt, *args):
        parts = " ".join(str(value) for value in args[:3]) if args else ""
        print(f"[{self.address_string()}] {parts}")


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main(argv=None):
    global SQLITE_DB, BIGRAM_DB, SYMBOL_DB, APP_CONFIG
    parser = argparse.ArgumentParser(description="Run RUC Xiaolaba search server")
    parser.add_argument(
        "--db",
        action="store_true",
        help="accepted for compatibility; SQLite is always used",
    )
    parser.add_argument("--sqlite-db", default=None, help="SQLite DB path")
    parser.add_argument(
        "--bigram-db",
        default=None,
        help=(
            "sidecar bigram FTS database; defaults to BIGRAM_DB_PATH, "
            "BIGRAM_DB, or data/bigram_index.db when present"
        ),
    )
    parser.add_argument(
        "--symbol-db",
        default=None,
        help=(
            "sidecar symbol database; defaults to SYMBOL_INDEX_DB_PATH, "
            "SYMBOL_INDEX_DB, or data/symbol_index.db when present"
        ),
    )
    parser.add_argument("--port", type=int, default=APP_CONFIG.port)
    parser.add_argument("--host", default=APP_CONFIG.host)
    args = parser.parse_args(argv)

    APP_CONFIG = AppConfig.from_env(
        posts_db=args.sqlite_db,
        bigram_db=args.bigram_db,
        symbol_db=args.symbol_db,
    )
    SQLITE_DB = str(APP_CONFIG.posts_db)
    BIGRAM_DB = str(APP_CONFIG.bigram_db or "")
    SYMBOL_DB = str(APP_CONFIG.symbol_db or "")
    Handler.context = build_context()

    overview = sqlite_overview()
    print(
        f"[init] SQLite backend: {overview['total']} posts from "
        f"{Path(SQLITE_DB).resolve()} (latest={overview['latest']})"
    )
    local_ip = socket.gethostbyname(socket.gethostname())
    print(f"  Local:   http://127.0.0.1:{args.port}")
    print(f"  LAN:     http://{local_ip}:{args.port}")
    print(f"  Admin:   http://127.0.0.1:{args.port}/admin")
    print(f"  Backend: sqlite ({SQLITE_DB})")
    if BIGRAM_DB:
        status = "ready" if sqlite_has_bigram_index() else "invalid"
        print(f"  Search:  bigram sidecar ({BIGRAM_DB}, {status})")
    else:
        print("  Search:  LIKE fallback (data/bigram_index.db not found)")
    if SYMBOL_DB:
        status = "ready" if sqlite_has_symbol_index() else "invalid"
        print(f"  Search:  symbol sidecar ({SYMBOL_DB}, {status})")
    else:
        print("  Search:  symbol fallback (data/symbol_index.db not found)")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
