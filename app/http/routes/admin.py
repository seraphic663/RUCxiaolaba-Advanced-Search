"""Administrator login and dashboard routes."""

from __future__ import annotations

import os
from urllib.parse import parse_qs


def get(handler):
    params, _ = handler.parse_query()
    if "logout" in params:
        handler.send_response(302)
        handler.set_cookie("admin_token", "deleted", max_age=1)
        handler.send_header("Location", "/admin")
        handler.end_headers()
        return
    if not handler.is_admin():
        csrf = handler.context.auth.create_csrf_token()
        content = handler.context.templates.render(
            "admin_login.html", CSRF_TOKEN=csrf, ERROR=""
        )
        handler.serve_html(content)
        return
    dashboard(handler)


def post(handler):
    body = handler.read_body().decode("utf-8", errors="replace")
    params = parse_qs(body)
    password = params.get("password", [""])[0]
    csrf = params.get("csrf_token", [""])[0]
    if not handler.context.auth.verify_csrf_token(csrf):
        token = handler.context.auth.create_csrf_token()
        content = handler.context.templates.render(
            "admin_login.html",
            CSRF_TOKEN=token,
            ERROR='<div class="error">表单已过期，请重新输入</div>',
        )
        handler.serve_html(content)
        return
    if password == handler.context.admin_password:
        token = handler.context.auth.create_session()
        handler.send_response(302)
        handler.set_cookie("admin_token", token)
        handler.send_header("Location", "/admin")
        handler.end_headers()
        return
    token = handler.context.auth.create_csrf_token()
    content = handler.context.templates.render(
        "admin_login.html",
        CSRF_TOKEN=token,
        ERROR='<div class="error">密码错误</div>',
    )
    handler.serve_html(content)


def dashboard(handler):
    stats = handler.context.admin.dashboard()
    csrf = handler.context.auth.create_csrf_token()
    content = handler.context.templates.render(
        "admin_dashboard.html",
        CSV_SOURCE=f"SQLite · {os.path.basename(handler.context.posts_db)}",
        TOTAL=stats["total"],
        UNIQUE_USERS=stats["unique_users"],
        MULTI=stats["multi"],
        TOTAL_COMMENTS=stats["total_comments"],
        UNIQUE_COMMENTERS=stats["unique_commenters"],
        DANGER="SQLite 数据(含ID)",
        USER_ROWS=stats["user_rows"],
        ADMIN_CSRF_TOKEN=csrf,
    )
    handler.serve_html(content)
