"""Explicit route table for the standard-library HTTP server."""

from __future__ import annotations

from app.http.routes import admin, admin_ai_search, admin_crawl, public

GET_ROUTES = {
    "/": public.main_page,
    "/favicon.svg": public.favicon,
    "/api/search": public.search,
    "/api/comments": public.comments,
    "/api/categories": public.categories,
    "/healthz": public.health,
    "/admin": admin.get,
    "/admin/reports": admin.reports_page,
    "/admin/crawl": admin.crawl_page,
    "/admin/ai-search": admin_ai_search.page,
    "/api/admin/ai-search/settings": admin_ai_search.settings,
    "/api/admin/crawler-report": admin.report_data,
    "/api/admin/live-crawl": admin_crawl.status,
    "/api/admin/crawl-status": admin_crawl.overview,
}

POST_ROUTES = {
    "/admin": admin.post,
    "/api/admin/ai-search": admin_ai_search.search,
    "/api/admin/ai-search/settings": admin_ai_search.save_settings,
    "/api/admin/ai-search/test": admin_ai_search.test_connection,
    "/api/admin/ai-search/estimate": admin_ai_search.estimate,
    "/api/admin/upstream-preview": admin_crawl.preview,
    "/api/admin/live-crawl": admin_crawl.create,
}


def dispatch(handler, method: str, path: str) -> bool:
    route = (GET_ROUTES if method == "GET" else POST_ROUTES).get(path)
    if route is None:
        return False
    route(handler)
    return True
