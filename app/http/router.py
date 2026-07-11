"""Explicit route table for the standard-library HTTP server."""

from __future__ import annotations

from app.http.routes import admin, admin_crawl, public

GET_ROUTES = {
    "/": public.main_page,
    "/api/search": public.search,
    "/api/comments": public.comments,
    "/api/categories": public.categories,
    "/healthz": public.health,
    "/admin": admin.get,
    "/api/admin/live-crawl": admin_crawl.status,
    "/api/admin/crawl-status": admin_crawl.overview,
}

POST_ROUTES = {
    "/admin": admin.post,
    "/api/admin/upstream-preview": admin_crawl.preview,
    "/api/admin/live-crawl": admin_crawl.create,
}


def dispatch(handler, method: str, path: str) -> bool:
    route = (GET_ROUTES if method == "GET" else POST_ROUTES).get(path)
    if route is None:
        return False
    route(handler)
    return True
