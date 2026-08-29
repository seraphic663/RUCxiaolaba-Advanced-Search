"""mitmproxy addon for locally inspecting mini-program API requests."""
import json
import os
import re
from pathlib import Path

from mitmproxy import ctx, http

# Patterns that look like mini-program API calls
API_PATTERNS = [
    r"api\.weixin\.qq\.com",
    r"\.myqcloud\.com",
    r"/cgi-bin/",
    r"mp\.weixin\.qq\.com",
    r"/api/",
    r"api\.",
    r"\.api\.",
]

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captured_requests.jsonl")
COOKIE_FILE = Path(__file__).resolve().parents[2] / "data" / "config_small.txt"
COOKIE_HOST = "ys.qimiaoyuanfen.com"

SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
}


def redacted_headers(headers) -> dict:
    """Copy headers without persisting credentials to the local JSONL log."""
    return {
        str(name): "<redacted>"
        if str(name).lower() in SENSITIVE_HEADERS
        else str(value)
        for name, value in dict(headers).items()
    }


def save_session_cookie(flow: http.HTTPFlow) -> bool:
    """Save only the own-session cookie from the configured upstream host."""
    if flow.request.pretty_host.lower() != COOKIE_HOST:
        return False
    try:
        cookie = flow.request.cookies.get("ys7_ysxy_session")
    except Exception:
        cookie = None
    if not cookie:
        return False

    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = COOKIE_FILE.with_name(COOKIE_FILE.name + ".tmp")
    temporary.write_text(
        f"ys7_ysxy_session={cookie}\n",
        encoding="utf-8",
    )
    os.replace(temporary, COOKIE_FILE)
    return True


def is_api_request(flow: http.HTTPFlow) -> bool:
    """Return True if this looks like a mini-program API call."""
    host = flow.request.pretty_host
    path = flow.request.path

    # Skip static resources
    if re.search(r"\.(js|css|png|jpg|jpeg|gif|svg|woff2?|ttf|map)(\?|$)", path):
        # Still capture if it's from a known API domain
        if not any(re.search(p, host) for p in [r"api\.", r"/cgi-bin/"]):
            return False

    # Skip metrics/analytics/telemetry
    skip_domains = [
        "pingfore.qq.com", "pingtcss.qq.com", "report.qqweb.qq.com",
        "szmg.qq.com", "tpstelemetry.tencent.com", "beacon.qq.com",
        "h.trace.qq.com", "oth.str.beacon.qq.com",
        "edge.log.zhiyan.tencent-cloud.net",
    ]
    if host in skip_domains:
        return False

    return True


def request(flow: http.HTTPFlow) -> None:
    if not is_api_request(flow):
        return

    if save_session_cookie(flow):
        ctx.log.info(f"[capture] saved own session cookie to {COOKIE_FILE}")

    method = flow.request.method
    url = flow.request.pretty_url
    host = flow.request.pretty_host
    path = flow.request.pretty_path

    req_body = ""
    if flow.request.content:
        try:
            req_body = flow.request.content.decode("utf-8", errors="replace")
        except Exception:
            req_body = str(flow.request.content)

    entry = {
        "method": method,
        "url": url,
        "host": host,
        "path": path,
        "req_headers": redacted_headers(flow.request.headers),
        "req_body": req_body[:2000],
    }

    ctx.log.info(f"[API] {method} {url}")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def response(flow: http.HTTPFlow) -> None:
    if not is_api_request(flow):
        return

    url = flow.request.pretty_url
    status = flow.response.status_code
    content_type = flow.response.headers.get("content-type", "")

    resp_body = ""
    if flow.response.content:
        try:
            resp_body = flow.response.content.decode("utf-8", errors="replace")
        except Exception:
            resp_body = f"<binary {len(flow.response.content)} bytes>"

    entry = {
        "url": url,
        "status": status,
        "content_type": content_type,
        "resp_body": resp_body[:5000],
    }

    ctx.log.info(f"[API] {status} <- {url}")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
