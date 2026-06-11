"""Web application assembly and standard-library HTTP transport."""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

import requests  # compatibility: tests and local tools patch server.requests

_requests_compat = requests

from app.ai.client import DeepSeekClient, DeepSeekSettings
from app.ai.policy import (
    evidence_payload,
    normalize_answer,
    sanitize_summary_citations,
    scrub_pii,
    validate_query,
    verify_cited_ids,
)
from app.ai.prompts import build_prompt
from app.config import AppConfig, choose_posts_db
from app.http.router import dispatch
from app.repositories.connections import connect_readonly
from app.repositories.post_repository import PostRepository
from app.repositories.search_repository import bigram_query, fts_query
from app.services.admin_service import AdminService
from app.services.ai_service import AIService
from app.services.auth_service import AdminAuthService
from app.services.search_service import SearchService
from app.services.template_service import TemplateService
from app.repositories.ai_access_repository import AIStore, get_store


SESSION_TTL = 86400
CSRF_TTL = 3600
COMMENT_LIMIT = 500
AI_SESSION_DAYS = 30
AI_MAX_BODY_BYTES = 64 * 1024
AI_RATE_LIMIT = 6
AI_RATE_WINDOW_SECONDS = 60

APP_CONFIG = AppConfig.from_env()
DATA_DIR = str(APP_CONFIG.data_dir)
TEMPLATES_DIR = str(APP_CONFIG.templates_dir)
SQLITE_DB = str(APP_CONFIG.posts_db)
BIGRAM_DB = str(APP_CONFIG.bigram_db or "")
AI_DB_PATH = str(APP_CONFIG.ai_db)
PASSWORD_FILE = str(APP_CONFIG.admin_password_file)
AI_KEY_FILE = str(APP_CONFIG.ai_key_file)
AI_MODEL = APP_CONFIG.ai_model
AI_FALLBACK_MODEL = APP_CONFIG.ai_fallback_model
AI_MODERATION_MODEL = APP_CONFIG.ai_moderation_model
AI_BASE_URL = APP_CONFIG.ai_base_url
AI_MAX_CONCURRENT = APP_CONFIG.ai_max_concurrent
AI_PROMPT_CHAR_LIMIT = APP_CONFIG.ai_prompt_char_limit
AI_CONTEXT_POST_LIMIT = APP_CONFIG.ai_context_post_limit
AI_MAX_OUTPUT_TOKENS = APP_CONFIG.ai_max_output_tokens
AI_REQUEST_TIMEOUT = APP_CONFIG.ai_request_timeout
AI_NETWORK_RETRIES = APP_CONFIG.ai_network_retries
AI_MODERATION_TIMEOUT = APP_CONFIG.ai_moderation_timeout
AI_MODERATION_RETRIES = APP_CONFIG.ai_moderation_retries


def choose_sqlite_db(explicit_path=None):
    return str(choose_posts_db(explicit_path))


def _get_deepseek_key() -> str:
    path = Path(AI_KEY_FILE)
    if path.exists():
        value = path.read_text(encoding="utf-8-sig").strip()
        if value:
            return value
    return os.environ.get("DEEPSEEK_API_KEY", "").strip()


AI_DEEPSEEK_KEY = _get_deepseek_key()
AI_ENABLED = bool(AI_DEEPSEEK_KEY) and APP_CONFIG.ai_enabled_setting != "0"


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
    return connect_readonly(SQLITE_DB, BIGRAM_DB or None)


def _search_service() -> SearchService:
    return SearchService(SQLITE_DB, BIGRAM_DB or None)


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


def _deepseek_client() -> DeepSeekClient:
    return DeepSeekClient(
        DeepSeekSettings(
            api_key=AI_DEEPSEEK_KEY,
            base_url=AI_BASE_URL,
            model=AI_MODEL,
            fallback_model=AI_FALLBACK_MODEL,
            moderation_model=AI_MODERATION_MODEL,
            max_output_tokens=AI_MAX_OUTPUT_TOKENS,
            request_timeout=AI_REQUEST_TIMEOUT,
            network_retries=AI_NETWORK_RETRIES,
            moderation_timeout=AI_MODERATION_TIMEOUT,
            moderation_retries=AI_MODERATION_RETRIES,
        )
    )


_scrub_pii = scrub_pii
_validate_ai_query = validate_query
_verify_cited_ids = verify_cited_ids
_sanitize_summary_citations = sanitize_summary_citations
_normalize_ai_answer = normalize_answer


def _moderate_ai_query(query):
    return _deepseek_client().moderate(query)


def _ai_evidence_payload(retrieved, cited):
    return evidence_payload(
        retrieved,
        cited,
        context_limit=AI_CONTEXT_POST_LIMIT,
    )


def _call_deepseek(system_prompt, user_prompt):
    return _deepseek_client().complete(system_prompt, user_prompt)


def _build_ai_prompt(query, retrieved):
    return build_prompt(
        query,
        retrieved,
        context_limit=AI_CONTEXT_POST_LIMIT,
        char_limit=AI_PROMPT_CHAR_LIMIT,
    )


_ai_store: AIStore | None = None


def _get_ai_store() -> AIStore:
    global _ai_store
    if _ai_store is None:
        _ai_store = get_store(AI_DB_PATH)
    return _ai_store


@dataclass
class ApplicationContext:
    posts_db: str
    admin_password: str
    posts: PostRepository
    search: SearchService
    admin: AdminService
    auth: AdminAuthService
    templates: TemplateService
    ai: AIService | None
    ai_enabled: bool
    ai_store: AIStore | None
    ai_session_days: int = AI_SESSION_DAYS
    ai_max_body_bytes: int = AI_MAX_BODY_BYTES
    ai_rate_limit: int = AI_RATE_LIMIT
    ai_rate_window: int = AI_RATE_WINDOW_SECONDS


def build_context() -> ApplicationContext:
    store = _get_ai_store() if AI_ENABLED else None
    ai = (
        AIService(
            posts_db=SQLITE_DB,
            store=store,
            client=_deepseek_client(),
            context_limit=AI_CONTEXT_POST_LIMIT,
            prompt_char_limit=AI_PROMPT_CHAR_LIMIT,
            max_concurrent=AI_MAX_CONCURRENT,
        )
        if store
        else None
    )
    return ApplicationContext(
        posts_db=SQLITE_DB,
        admin_password=get_password(),
        posts=PostRepository(SQLITE_DB),
        search=SearchService(SQLITE_DB, BIGRAM_DB or None),
        admin=AdminService(SQLITE_DB),
        auth=AdminAuthService(SESSION_TTL, CSRF_TTL),
        templates=TemplateService(TEMPLATES_DIR),
        ai=ai,
        ai_enabled=AI_ENABLED,
        ai_store=store,
    )


class Handler(BaseHTTPRequestHandler):
    context: ApplicationContext
    _rate_lock = threading.Lock()
    _rate_events: dict[str, list[float]] = {}

    def set_cookie(self, name, value, max_age=SESSION_TTL):
        self.send_header(
            "Set-Cookie", f"{name}={value}; Path=/; HttpOnly; Max-Age={max_age}"
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

    def ai_user(self):
        token = self.get_cookie("ai_token")
        if not token or not self.context.ai_store:
            return None
        return self.context.ai_store.validate_session(token)

    def set_pending_ai_cookie(self, name, value, max_age):
        cookie = f"{name}={value}; Path=/; HttpOnly; Max-Age={max_age}"
        host = self.headers.get("Host") or ""
        if "localhost" not in host and not host.startswith("127.0.0.1"):
            cookie += "; Secure"
        self._pending_ai_cookie = cookie + "; SameSite=Lax"

    def client_ip(self):
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",", 1)[0]
        return forwarded.strip() or self.client_address[0]

    def allow_ai_request(self, limit=None):
        now = time.time()
        cutoff = now - self.context.ai_rate_window
        ip = self.client_ip()
        limit = limit or self.context.ai_rate_limit
        with self._rate_lock:
            events = [
                event for event in self._rate_events.get(ip, []) if event > cutoff
            ]
            if len(events) >= limit:
                self._rate_events[ip] = events
                return False
            events.append(now)
            self._rate_events[ip] = events
        return True

    def serve_html(self, content, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def serve_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        pending = getattr(self, "_pending_ai_cookie", None)
        if pending:
            self.send_header("Set-Cookie", pending)
            self._pending_ai_cookie = None
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

    def read_json(self):
        body = self.read_body(self.context.ai_max_body_bytes)
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None

    def _dispatch(self, method):
        _, path = self.parse_query()
        if dispatch(self, method, path):
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
    global SQLITE_DB, BIGRAM_DB, APP_CONFIG
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
    parser.add_argument("--port", type=int, default=APP_CONFIG.port)
    parser.add_argument("--host", default=APP_CONFIG.host)
    args = parser.parse_args(argv)

    APP_CONFIG = AppConfig.from_env(
        posts_db=args.sqlite_db,
        bigram_db=args.bigram_db,
    )
    SQLITE_DB = str(APP_CONFIG.posts_db)
    BIGRAM_DB = str(APP_CONFIG.bigram_db or "")
    Handler.context = build_context()

    overview = sqlite_overview()
    print(
        f"[init] SQLite backend: {overview['total']} posts from "
        f"{Path(SQLITE_DB).resolve()} (latest={overview['latest']})"
    )
    if AI_ENABLED and Handler.context.ai_store:
        stats = Handler.context.ai_store.get_stats()
        print(
            f"[init] AI enabled: model={AI_MODEL}, "
            f"invite_codes={stats['total_codes']}, "
            f"active_sessions={stats['active_sessions']}, "
            f"max_concurrent={AI_MAX_CONCURRENT}"
        )
    else:
        print("[init] AI disabled")
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
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
