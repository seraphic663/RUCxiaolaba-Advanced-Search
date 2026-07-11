"""Authenticated APIs for upstream preview and administrator live crawling."""

from __future__ import annotations

import json

from app.services.admin_crawl_service import AdminCrawlError


def _error(handler, code: int, error: str, message: str):
    handler.serve_json(
        {
            "ok": False,
            "error": error,
            "message": message,
            "csrf_token": handler.context.auth.create_csrf_token(),
        },
        code,
    )


def _authenticate(handler) -> bool:
    if not handler.is_admin():
        _error(handler, 401, "admin_required", "管理员登录已失效，请重新登录")
        return False
    token = handler.headers.get("X-CSRF-Token", "")
    if not handler.context.auth.verify_csrf_token(token):
        _error(handler, 403, "csrf_invalid", "操作令牌已过期，请重试")
        return False
    return True


def _json_body(handler) -> dict:
    try:
        payload = json.loads(handler.read_body(max_bytes=16_384).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AdminCrawlError("invalid_json", "请求内容不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise AdminCrawlError("invalid_json", "请求内容必须是 JSON 对象")
    return payload


def preview(handler):
    if not _authenticate(handler):
        return
    try:
        payload = _json_body(handler)
        result = handler.context.admin_crawl.preview(
            payload.get("source", ""),
            payload.get("query", ""),
            payload.get("pages", 1),
        )
    except AdminCrawlError as exc:
        _error(handler, exc.http_status, exc.code, str(exc))
        return
    result.update(
        ok=True,
        csrf_token=handler.context.auth.create_csrf_token(),
    )
    handler.serve_json(result)


def create(handler):
    if not _authenticate(handler):
        return
    try:
        payload = _json_body(handler)
        result = handler.context.admin_crawl.create_job(
            str(payload.get("preview_id") or ""),
            list(payload.get("selected_ids") or []),
            str(payload.get("strategy") or "smart"),
        )
    except AdminCrawlError as exc:
        _error(handler, exc.http_status, exc.code, str(exc))
        return
    handler.serve_json(
        {
            "ok": True,
            "job": result,
            "csrf_token": handler.context.auth.create_csrf_token(),
        },
        202,
    )


def status(handler):
    if not handler.is_admin():
        _error(handler, 401, "admin_required", "管理员登录已失效，请重新登录")
        return
    params, _ = handler.parse_query()
    job_id = params.get("id", [""])[0]
    try:
        result = handler.context.admin_crawl.get_job(job_id)
    except AdminCrawlError as exc:
        _error(handler, exc.http_status, exc.code, str(exc))
        return
    handler.serve_json({"ok": True, "job": result})
