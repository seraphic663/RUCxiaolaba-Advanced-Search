"""Administrator login and dashboard routes."""

from __future__ import annotations

import json
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
        ADMIN_CSRF_TOKEN=csrf,
    )
    handler.serve_html(content)


def crawl_page(handler):
    tool_page(handler, "admin_crawl.html")


def reports_page(handler):
    tool_page(handler, "admin_reports.html")


def tool_page(handler, template):
    if not handler.is_admin():
        handler.send_response(302)
        handler.send_header("Location", "/admin")
        handler.end_headers()
        return
    handler.serve_html(handler.context.templates.render(
        template, ADMIN_CSRF_TOKEN=handler.context.auth.create_csrf_token()
    ))


def report_data(handler):
    if not handler.is_admin():
        handler.serve_json({"ok": False, "error": "管理员登录已失效，请重新登录"}, 401)
        return
    params, _ = handler.parse_query()
    try:
        result = handler.context.admin.reports.report(params.get("from", [""])[0], params.get("to", [""])[0])
    except ValueError as exc:
        handler.serve_json({"ok": False, "error": str(exc)}, 400)
        return
    handler.serve_json(result)


def public_access_settings(handler):
    if not handler.is_admin():
        handler.serve_json(
            {"ok": False, "error": "管理员登录已失效，请重新登录"},
            code=401,
            no_store=True,
        )
        return
    handler.serve_json(
        {
            "ok": True,
            "settings": handler.context.public_access.load(),
            "csrf_token": handler.context.auth.create_csrf_token(),
        },
        no_store=True,
    )


def save_public_access_settings(handler):
    if not handler.is_admin():
        handler.serve_json(
            {"ok": False, "error": "管理员登录已失效，请重新登录"},
            code=401,
            no_store=True,
        )
        return
    if not handler.context.auth.verify_csrf_token(
        handler.headers.get("X-CSRF-Token", "")
    ):
        handler.serve_json(
            {
                "ok": False,
                "error": "操作令牌已过期，请重试",
                "csrf_token": handler.context.auth.create_csrf_token(),
            },
            code=403,
            no_store=True,
        )
        return
    try:
        payload = json.loads(handler.read_body(max_bytes=4096).decode("utf-8"))
        settings = handler.context.public_access.save(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        handler.serve_json(
            {
                "ok": False,
                "error": str(exc) or "权限设置格式无效",
                "csrf_token": handler.context.auth.create_csrf_token(),
            },
            code=400,
            no_store=True,
        )
        return
    handler.serve_json(
        {
            "ok": True,
            "settings": settings,
            "csrf_token": handler.context.auth.create_csrf_token(),
        },
        no_store=True,
    )
