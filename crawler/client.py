"""Remote mini-program API client."""

from __future__ import annotations

from pathlib import Path

import requests
import urllib3

from crawler.automatic_quota import AutomaticQuota, AutomaticQuotaError
from crawler.config import BASE_URL, COMMUNITY_ID, HEADERS

urllib3.disable_warnings()


class AuthenticationExpired(RuntimeError):
    pass


class RemoteAPIError(RuntimeError):
    pass


RATE_LIMIT_MARKERS = (
    "刷的太久",
    "休息一下",
    "操作频繁",
    "稍后再试",
    "访问频繁",
)
SESSION_COOKIE_NAMES = ("ys7_ysxy_session", "ys_ysxy_sess")


def load_cookie(config_path: str | Path) -> str:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"missing cookie config: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        for cookie_name in SESSION_COOKIE_NAMES:
            marker = f"{cookie_name}="
            if marker in line:
                value = line.split(marker, 1)[1].split(";", 1)[0].strip()
                if value:
                    return value
    raise RuntimeError(f"cookie not found in {path}")


class MiniProgramClient:
    def __init__(self, cookie: str, *, lane_id: str = ""):
        self.cookie = cookie
        self.lane_id = str(lane_id or "")
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        for cookie_name in SESSION_COOKIE_NAMES:
            self.session.cookies.set(cookie_name, cookie)
        self.session.verify = False
        self.automatic_quota = AutomaticQuota.from_environment(
            lane_id=self.lane_id
        )
        self.request_count = 0

    def get(
        self,
        path: str,
        params: dict | None = None,
    ) -> tuple[dict | None, str | None]:
        if self.automatic_quota is not None:
            try:
                self.automatic_quota.claim(1)
            except AutomaticQuotaError as exc:
                return None, exc.code
        self.request_count += 1
        try:
            response = self.session.get(
                f"{BASE_URL}{path}",
                params=params,
                timeout=15,
                verify=False,
            )
            payload = response.json()
        except Exception as exc:
            return None, str(exc)
        code = str(payload.get("code") or "")
        if code == "0000":
            return payload.get("data", {}), None
        if code in {"1000", "7001"}:
            return None, "cookie_expired"
        if code == "0102":
            return None, "not_found"
        message = str(payload.get("message", ""))
        if "请先登录" in message:
            return None, "cookie_expired"
        if any(marker in message for marker in RATE_LIMIT_MARKERS):
            return None, f"rate_limited:{message}"
        return None, f"code={code} {message}"

    def list_page(self, endpoint: str, page: int):
        return self.get(
            f"/article/article/{endpoint}",
            {"community_id": COMMUNITY_ID, "page": page},
        )

    def article(self, post_id: str):
        return self.get(
            "/article/article/info",
            {"community_id": COMMUNITY_ID, "id": str(post_id)},
        )

    def search(self, keyword: str, page: int):
        return self.get(
            "/article/article/search",
            {"community_id": COMMUNITY_ID, "search": keyword, "page": page},
        )

    def latest_id(self, *, task_type: str = "") -> int:
        data, error = self.list_page("lists", 1)
        if error:
            raise RemoteAPIError(f"cannot determine latest id: {error}")
        ids = []
        for item in (data or {}).get("list", []):
            try:
                ids.append(int(item.get("id") or 0))
            except (TypeError, ValueError):
                pass
        latest = max(ids, default=0)
        if latest <= 0:
            raise RemoteAPIError("cannot determine latest id from lists page 1")
        return latest
