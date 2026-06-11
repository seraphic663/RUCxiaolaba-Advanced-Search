"""AI activation, status and search routes."""

from __future__ import annotations


def _require_enabled(handler) -> bool:
    if handler.context.ai_enabled:
        return True
    handler.serve_json({"ok": False, "error": "AI 功能未启用"}, code=503)
    return False


def activate(handler):
    if not _require_enabled(handler):
        return
    if not handler.allow_ai_request(limit=10):
        handler.serve_json(
            {"ok": False, "error": "操作过于频繁，请稍后重试"}, code=429
        )
        return
    try:
        data = handler.read_json()
    except ValueError:
        handler.serve_json({"ok": False, "error": "请求体过大"}, code=413)
        return
    if not isinstance(data, dict):
        handler.serve_json({"ok": False, "error": "请求格式错误"}, code=400)
        return
    status, payload, token = handler.context.ai.activate(
        str(data.get("code", ""))
    )
    if token:
        handler.set_pending_ai_cookie(
            "ai_token", token, handler.context.ai_session_days * 86400
        )
    handler.serve_json(payload, code=status)


def status(handler):
    if not _require_enabled(handler):
        return
    code_hash = handler.ai_user()
    status_code, payload = handler.context.ai.status(code_hash)
    handler.serve_json(payload, code=status_code)


def search(handler):
    if not _require_enabled(handler):
        return
    if not handler.allow_ai_request():
        handler.serve_json(
            {"ok": False, "error": "搜索过于频繁，请稍后重试"}, code=429
        )
        return
    try:
        data = handler.read_json()
    except ValueError:
        handler.serve_json({"ok": False, "error": "请求体过大"}, code=413)
        return
    if not isinstance(data, dict):
        handler.serve_json({"ok": False, "error": "请求体为空"}, code=400)
        return
    is_admin = handler.is_admin()
    code_hash = None if is_admin else handler.ai_user()
    try:
        status_code, payload = handler.context.ai.search(
            str(data.get("query", "")).strip(),
            is_admin=is_admin,
            code_hash=code_hash,
        )
    except Exception as exc:
        print(f"[ai] search failed: {type(exc).__name__}: {exc}")
        status_code, payload = 500, {
            "ok": False,
            "error": "AI 搜索内部异常，请稍后重试",
        }
    handler.serve_json(payload, code=status_code)
