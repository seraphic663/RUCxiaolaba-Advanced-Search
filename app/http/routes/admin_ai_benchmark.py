"""Authenticated benchmark history, estimates and explicit paid retests."""

from urllib.parse import parse_qs, urlsplit

from app.domain.ai_search import AiSearchError
from app.http.routes.admin_ai_search import _authenticate, _body, _error, _reply


def _service(handler):
    service = handler.context.ai_benchmark
    if service is None:
        raise AiSearchError("benchmark_unavailable", "Bench 服务尚未初始化", 503)
    return service


def page(handler):
    if not handler.is_admin():
        handler.send_response(302)
        handler.send_header("Location", "/admin")
        handler.end_headers()
        return
    handler.serve_html(handler.context.templates.render(
        "admin_ai_benchmark.html", ADMIN_CSRF_TOKEN=handler.context.auth.create_csrf_token()
    ), no_store=True)


def get(handler):
    if not _authenticate(handler):
        return
    try:
        run_id = parse_qs(urlsplit(handler.path).query).get("run_id", [""])[0]
        result = _service(handler).detail(run_id) if run_id else _service(handler).catalog()
        _reply(handler, {"ok": True, **result})
    except AiSearchError as exc:
        _error(handler, exc)


def _post(handler, action):
    if not _authenticate(handler, csrf=True):
        return
    try:
        payload = _body(handler)
        service = _service(handler)
        result = {"run": service.start(payload)} if action == "run" else {"estimate": service.estimate(payload)}
        _reply(handler, {"ok": True, **result}, 202 if action == "run" else 200)
    except AiSearchError as exc:
        _error(handler, exc)


def start(handler):
    _post(handler, "run")


def estimate(handler):
    _post(handler, "estimate")
