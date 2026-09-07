"""mitmproxy addon for locally inspecting mini-program API requests.

The addon captures only the session cookie from the configured upstream host.
It first writes a temporary candidate and promotes it to the local config only
after an intercepted API response reports the normal success code.  This lets
the PowerShell wrapper stop automatically without making an extra upstream
request just to validate the credential.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from mitmproxy import ctx, http

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captured_requests.jsonl")
DATA_DIR = Path(__file__).resolve().parents[2] / "data"
COOKIE_FILE = Path(
    os.environ.get("RUC_CAPTURE_COOKIE_FILE", str(DATA_DIR / "config_small.txt"))
)
CANDIDATE_FILE = Path(
    os.environ.get(
        "RUC_CAPTURE_CANDIDATE_FILE",
        str(COOKIE_FILE.with_name(COOKIE_FILE.name + ".candidate")),
    )
)
STATUS_FILE = Path(
    os.environ.get("RUC_CAPTURE_STATUS_FILE", str(DATA_DIR / "capture_status.json"))
)
COOKIE_HOST = "ys.qimiaoyuanfen.com"
COOKIE_NAMES = ("ys7_ysxy_session", "ys_ysxy_sess")
FLOW_COOKIE_METADATA = "ruc_capture_session_cookie"
_COOKIE_DIAGNOSTIC_REPORTED = False

SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
}

SKIP_DOMAINS = {
    "pingfore.qq.com",
    "pingtcss.qq.com",
    "report.qqweb.qq.com",
    "szmg.qq.com",
    "tpstelemetry.tencent.com",
    "beacon.qq.com",
    "h.trace.qq.com",
    "oth.str.beacon.qq.com",
    "edge.log.zhiyan.tencent-cloud.net",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _write_status(state: str, **extra) -> None:
    """Write capture state without including the cookie value."""

    payload = {
        "state": state,
        "updated_at": _now(),
        "cookie_file": str(COOKIE_FILE),
    }
    cookie_label = os.environ.get("RUC_CAPTURE_LABEL", "").strip()
    if cookie_label:
        payload["cookie_label"] = cookie_label
    payload.update(extra)
    try:
        _atomic_write(STATUS_FILE, json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception as exc:
        ctx.log.error(f"[capture] cannot write status file: {type(exc).__name__}")


def redacted_headers(headers) -> dict:
    """Copy headers without persisting credentials to the local JSONL log."""

    return {
        str(name): "<redacted>"
        if str(name).lower() in SENSITIVE_HEADERS
        else str(value)
        for name, value in dict(headers).items()
    }


def _session_cookie(flow: http.HTTPFlow) -> tuple[str, str]:
    if flow.request.pretty_host.lower() != COOKIE_HOST:
        return "", ""
    try:
        for cookie_name in COOKIE_NAMES:
            cookie = str(flow.request.cookies.get(cookie_name) or "").strip()
            if cookie:
                return cookie_name, cookie
    except Exception:
        pass

    # Some WMPF/mitmproxy combinations do not populate the parsed cookie
    # view even though the raw Cookie header is present.
    for raw_cookie in _header_values(flow.request.headers, "cookie"):
        for cookie_name in COOKIE_NAMES:
            match = re.search(
                rf"(?:^|;)\s*{re.escape(cookie_name)}\s*=\s*([^;]*)",
                raw_cookie,
            )
            if match and match.group(1).strip():
                return cookie_name, match.group(1).strip()
    return "", ""


def _header_values(headers, header_name: str) -> list[str]:
    values: list[str] = []
    try:
        for name, value in headers.items():
            if str(name).lower() == header_name.lower():
                values.append(str(value))
    except Exception:
        try:
            value = headers.get(header_name, "")
            if value:
                values.append(str(value))
        except Exception:
            pass
    return values


def _cookie_names(raw_headers: list[str]) -> list[str]:
    names: set[str] = set()
    for raw_header in raw_headers:
        for item in raw_header.split(";"):
            name, separator, _ = item.strip().partition("=")
            if separator and name.strip():
                names.add(name.strip())
    return sorted(names)


def _response_session_cookie(flow: http.HTTPFlow) -> tuple[str, str]:
    if flow.request.pretty_host.lower() != COOKIE_HOST:
        return "", ""
    for raw_cookie in _header_values(flow.response.headers, "set-cookie"):
        for cookie_name in COOKIE_NAMES:
            match = re.search(
                rf"(?:^|;)\s*{re.escape(cookie_name)}\s*=\s*([^;]*)",
                raw_cookie,
            )
            if match and match.group(1).strip():
                return cookie_name, match.group(1).strip()
    return "", ""


def _request_path(flow: http.HTTPFlow) -> str:
    """Return a path across mitmproxy versions."""

    pretty_path = getattr(flow.request, "pretty_path", None)
    if pretty_path:
        return str(pretty_path)
    return str(getattr(flow.request, "path", "") or "").split("?", 1)[0]


def _remember_flow_cookie(flow: http.HTTPFlow, cookie_name: str, cookie: str) -> None:
    metadata = getattr(flow, "metadata", None)
    if isinstance(metadata, dict):
        metadata[FLOW_COOKIE_METADATA] = {"name": cookie_name, "value": cookie}


def _flow_cookie(flow: http.HTTPFlow) -> tuple[str, str]:
    metadata = getattr(flow, "metadata", None)
    if not isinstance(metadata, dict):
        return "", ""
    value = metadata.get(FLOW_COOKIE_METADATA)
    if isinstance(value, dict):
        return str(value.get("name") or ""), str(value.get("value") or "")
    return "", str(value or "")


def save_session_cookie(flow: http.HTTPFlow) -> bool:
    """Save a candidate from the own upstream host, pending response validation."""

    global _COOKIE_DIAGNOSTIC_REPORTED
    cookie_name, cookie = _session_cookie(flow)
    if not cookie:
        if not _COOKIE_DIAGNOSTIC_REPORTED:
            names = _cookie_names(_header_values(flow.request.headers, "cookie"))
            if names:
                ctx.log.info(
                    "[capture] target Cookie names only: " + ",".join(names)
                )
            _COOKIE_DIAGNOSTIC_REPORTED = True
        return False

    _remember_flow_cookie(flow, cookie_name, cookie)
    _atomic_write(CANDIDATE_FILE, f"{cookie_name}={cookie}\n")
    _write_status(
        "captured",
        captured_at=_now(),
        host=flow.request.pretty_host,
        path=_request_path(flow),
        cookie_name=cookie_name,
        validated=False,
    )
    return True


def _response_text(flow: http.HTTPFlow) -> str:
    content = getattr(flow.response, "content", b"")
    if not content:
        return ""
    try:
        return content.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _success_code(response_text: str) -> bool:
    if not response_text:
        return False
    try:
        payload = json.loads(response_text)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        return str(payload.get("code", "")).strip() in {"0000", "0"}
    return bool(re.search(r'"code"\s*:\s*"?(?:0000|0)"?', response_text))


def _response_code(response_text: str) -> str:
    try:
        payload = json.loads(response_text)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict) and payload.get("code") is not None:
        return str(payload.get("code"))
    match = re.search(r'"code"\s*:\s*"?([^",}\s]+)', response_text or "")
    return match.group(1) if match else ""


def _promote_candidate(cookie_name: str, cookie: str) -> bool:
    """Promote only the candidate belonging to the successful flow."""

    try:
        candidate = CANDIDATE_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    expected = f"{cookie_name}={cookie}"
    if expected not in candidate:
        return False
    _atomic_write(COOKIE_FILE, expected + "\n")
    try:
        CANDIDATE_FILE.unlink()
    except FileNotFoundError:
        pass
    return True


def is_api_request(flow: http.HTTPFlow) -> bool:
    """Return True if this looks like a mini-program API call."""

    host = str(flow.request.pretty_host or "").lower()
    if host != COOKIE_HOST:
        return False
    path = flow.request.path
    if re.search(r"\.(js|css|png|jpg|jpeg|gif|svg|woff2?|ttf|map)(\?|$)", path):
        if not any(re.search(pattern, host) for pattern in (r"api\.", r"/cgi-bin/")):
            return False
    return host not in SKIP_DOMAINS


def request(flow: http.HTTPFlow) -> None:
    if not is_api_request(flow):
        return

    if save_session_cookie(flow):
        ctx.log.info("[capture] detected own session candidate; waiting for API validation")

    req_body = ""
    if flow.request.content:
        try:
            req_body = flow.request.content.decode("utf-8", errors="replace")
        except Exception:
            req_body = str(flow.request.content)

    entry = {
        "method": flow.request.method,
        "url": flow.request.pretty_url,
        "host": flow.request.pretty_host,
        "path": _request_path(flow),
        "req_headers": redacted_headers(flow.request.headers),
        "req_body": req_body[:2000],
    }
    ctx.log.info(f"[API] {flow.request.method} {flow.request.pretty_url}")
    with open(LOG_FILE, "a", encoding="utf-8") as output:
        output.write(json.dumps(entry, ensure_ascii=False) + "\n")


def response(flow: http.HTTPFlow) -> None:
    if not is_api_request(flow):
        return

    response_text = _response_text(flow)
    status = flow.response.status_code
    cookie_name, cookie = _flow_cookie(flow)
    if not cookie:
        cookie_name, cookie = _response_session_cookie(flow)
        if cookie:
            _remember_flow_cookie(flow, cookie_name, cookie)
            _atomic_write(CANDIDATE_FILE, f"{cookie_name}={cookie}\n")
    if cookie and flow.request.pretty_host.lower() == COOKIE_HOST:
        if _success_code(response_text):
            if _promote_candidate(cookie_name, cookie):
                _write_status(
                    "validated",
                    captured_at=_now(),
                    validated_at=_now(),
                    validated=True,
                    validation_method="observed_api_response",
                    host=flow.request.pretty_host,
                    path=_request_path(flow),
                    cookie_name=cookie_name,
                    response_status=status,
                )
                ctx.log.info("[capture] own session validated and saved locally")
        elif _response_code(response_text) or status >= 400:
            _write_status(
                "rejected",
                captured_at=_now(),
                validated=False,
                host=flow.request.pretty_host,
                path=_request_path(flow),
                response_status=status,
                response_code=_response_code(response_text),
            )
            ctx.log.info("[capture] session candidate was rejected by upstream")

    entry = {
        "url": flow.request.pretty_url,
        "status": status,
        "content_type": flow.response.headers.get("content-type", ""),
        "resp_body": response_text[:5000] if response_text else f"<binary {len(flow.response.content)} bytes>",
    }
    ctx.log.info(f"[API] {status} <- {flow.request.pretty_url}")
    with open(LOG_FILE, "a", encoding="utf-8") as output:
        output.write(json.dumps(entry, ensure_ascii=False) + "\n")
