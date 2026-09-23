"""Public page and search API routes."""

from __future__ import annotations

from datetime import datetime, time, timedelta


def _parse_search_date(raw_value: str, *, is_end: bool = False) -> datetime:
    """Parse either a calendar date or the legacy timestamp query format."""
    value = str(raw_value or "").strip()
    if len(value) == 10:
        day = datetime.strptime(value, "%Y-%m-%d").date()
        return datetime.combine(day, time(23, 59, 59) if is_end else time.min)
    return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")


def main_page(handler):
    overview = handler.context.posts.overview()
    content = handler.context.templates.render(
        "main.html",
        TOTAL=overview["total"],
        CRAWL_TIME=overview["crawl_time"] or "?",
        EARLIEST_TIME=overview["earliest"],
        LATEST_TIME=overview["latest"],
    )
    handler.serve_html(content)


def search(handler):
    params, _ = handler.parse_query()
    query = params.get("q", [""])[0].strip()
    sort_by = params.get("sort", ["time"])[0]
    if sort_by not in ("time", "stars", "comments", "score"):
        sort_by = "time"
    try:
        page = max(1, int(params.get("page", ["1"])[0]))
        limit = max(1, min(int(params.get("limit", ["50"])[0]), 200))
        scan_offset = max(0, int(params.get("scan_offset", ["0"])[0]))
        matched_before = max(0, int(params.get("matched_before", ["0"])[0]))
    except ValueError:
        page, limit, scan_offset, matched_before = 1, 50, 0, 0
    cursor_mode = params.get("cursor", ["0"])[0] == "1"
    category = params.get("category", [""])[0].strip() or None
    l2 = params.get("l2", [""])[0].strip() or None
    if l2 not in {
        "fwb", "sex_behavior", "sex_kink", "harassment",
        "gender_view", "romance", "dating", "noise",
    }:
        l2 = None
    uid = params.get("uid", [""])[0].strip() or None
    uname = params.get("uname", [""])[0].strip() or None
    scope = params.get("scope", ["content"])[0]
    if scope not in ("all", "content"):
        scope = "content"

    admin_required = params.get("admin_required", ["0"])[0] == "1"
    admin = handler.is_admin()
    if not admin:
        sort_by = "time"
        # Topic taxonomy is intentionally not exposed on the public search API.
        l2 = None
    if admin_required and not admin:
        handler.serve_json(
            {"ok": False, "error": "管理员登录已失效，请重新登录"},
            code=401,
        )
        return
    admin_fields = None
    id_match = name_match = "exact"
    if admin:
        allowed = {"body", "cmt", "uid", "name", "post"}
        raw_fields = params.get("admin_fields", [""])[0].strip()
        admin_fields = (
            {field for field in raw_fields.split(",") if field in allowed}
            if raw_fields
            else allowed
        )
        if not admin_fields:
            admin_fields = allowed
        id_match = params.get("id_match", ["exact"])[0]
        name_match = params.get("name_match", ["exact"])[0]
        if id_match not in ("exact", "contains"):
            id_match = "exact"
        if name_match not in ("exact", "contains"):
            name_match = "exact"
    identity = params.get("identity", [""])[0].strip()
    if identity not in ("anonymous", "real") or not admin:
        identity = None
    source_state = params.get("source_state", ["all"])[0].strip()
    if not admin or source_state not in ("all", "available", "deleted"):
        source_state = "all"

    date_from = date_to = None
    preset = params.get("date", [""])[0].strip()
    if preset:
        now = datetime.now()
        if preset == "1d":
            date_from = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif preset == "3d":
            date_from = now - timedelta(days=3)
        elif preset == "7d":
            date_from = now - timedelta(days=7)
        elif preset == "30d":
            date_from = now - timedelta(days=30)
    else:
        try:
            raw_from = params.get("from", [""])[0]
            raw_to = params.get("to", [""])[0]
            if raw_from:
                date_from = _parse_search_date(raw_from)
            if raw_to:
                date_to = _parse_search_date(raw_to, is_end=True)
        except ValueError:
            handler.serve_json(
                {"ok": False, "error": "日期格式无效，请使用 YYYY-MM-DD"},
                code=400,
            )
            return
        if date_from and date_to and date_from > date_to:
            handler.serve_json(
                {"ok": False, "error": "开始日期不能晚于结束日期"},
                code=400,
            )
            return

    search_method = (
        handler.context.search.search_cursor
        if cursor_mode
        else handler.context.search.search
    )
    cursor_args = (
        {"scan_offset": scan_offset, "matched_before": matched_before}
        if cursor_mode
        else {}
    )
    result = search_method(
        query,
        sort_by,
        page,
        limit,
        **cursor_args,
        category=category,
        l2=l2,
        date_from=date_from,
        date_to=date_to,
        scope=scope,
        uid=uid,
        uname=uname,
        admin=admin,
        identity=identity,
        source_state=source_state,
        admin_fields=admin_fields,
        id_match=id_match,
        name_match=name_match,
    )
    handler.serve_json(result)


def categories(handler):
    params, _ = handler.parse_query()
    try:
        min_count = max(1, min(int(params.get("min_count", ["200"])[0]), 1000000))
    except ValueError:
        min_count = 200
    handler.serve_json(handler.context.search.categories(min_count=min_count))


def comments(handler):
    params, _ = handler.parse_query()
    post_id = params.get("id", [""])[0]
    if not post_id:
        handler.serve_json({"error": "Missing post id"}, code=400)
        return
    admin_required = params.get("admin_required", ["0"])[0] == "1"
    admin = handler.is_admin()
    if admin_required and not admin:
        handler.serve_json(
            {"ok": False, "error": "管理员登录已失效，请重新登录"},
            code=401,
        )
        return
    result = handler.context.search.comments(
        post_id,
        admin=admin,
    )
    if result is None:
        handler.serve_json({"error": "Post not found"}, code=404)
        return
    handler.serve_json(result)


def health(handler):
    handler.serve_json({"ok": True})
