"""Explicit route table for the standard-library HTTP server."""

from __future__ import annotations

from app.http.routes import admin, public

GET_ROUTES = {
    "/": public.main_page,
    "/api/search": public.search,
    "/api/comments": public.comments,
    "/api/categories": public.categories,
    "/healthz": public.health,
    "/admin": admin.get,
}

POST_ROUTES = {
    "/admin": admin.post,
}


def dispatch(handler, method: str, path: str) -> bool:
    route = (GET_ROUTES if method == "GET" else POST_ROUTES).get(path)
    if route is None:
        return False
    route(handler)
    return True
