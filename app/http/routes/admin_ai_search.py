"""Administrator-only AI search, configuration and connectivity routes."""

from __future__ import annotations

import json

from app.domain.ai_search import AiSearchError


def page(handler):
    if not handler.is_admin():
        handler.send_response(302)
        handler.send_header("Location", "/admin")
        handler.end_headers()
        return
    handler.serve_html(handler.context.templates.render(
        "admin_ai_search.html", ADMIN_CSRF_TOKEN=handler.context.auth.create_csrf_token()
    ), no_store=True)


def _reply(handler, result: dict, status: int = 200):
    handler.serve_json({
        **result, "csrf_token": handler.context.auth.create_csrf_token(),
    }, status, no_store=True)


def _error(handler, error: AiSearchError):
    _reply(handler, {"ok": False, "error": error.code, "message": str(error)}, error.status)


def _authenticate(handler, *, csrf=False):
    if not handler.is_admin():
        _error(handler, AiSearchError("admin_required", "管理员登录已失效，请重新登录", 401))
        return False
    if csrf and not handler.context.auth.verify_csrf_token(handler.headers.get("X-CSRF-Token", "")):
        _error(handler, AiSearchError("csrf_invalid", "操作令牌已过期，请重试", 403))
        return False
    return True


def _service(handler):
    service = handler.context.ai_search
    if service is None:
        raise AiSearchError("ai_unavailable", "AI 检索服务尚未初始化", 503)
    return service


def _body(handler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if length < 0 or length > 16384:
            raise ValueError("size")
        payload = json.loads(handler.read_body(max_bytes=16384).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("object required")
        return payload
    except (ValueError, UnicodeDecodeError) as exc:
        raise AiSearchError("invalid_json", "请求须为不超过 16 KiB 的 JSON 对象") from exc


def settings(handler):
    if not _authenticate(handler):
        return
    try:
        _reply(handler, {"ok": True, "settings": _service(handler).config.load().public()})
    except AiSearchError as exc:
        _error(handler, exc)


def _post(handler, action):
    if not _authenticate(handler, csrf=True):
        return
    try:
        payload = _body(handler)
        service = _service(handler)
        if action == "settings":
            result = {"settings": service.config.save(payload)}
        elif action == "test":
            result = service.test_connection()
        elif action == "estimate":
            result = {"estimate": service.estimate(payload)}
        else:
            result = service.run(payload)
        _reply(handler, {"ok": True, **result})
    except AiSearchError as exc:
        _error(handler, exc)


def save_settings(handler):
    _post(handler, "settings")


def test_connection(handler):
    _post(handler, "test")


def search(handler):
    _post(handler, "search")


def estimate(handler):
    _post(handler, "estimate")
