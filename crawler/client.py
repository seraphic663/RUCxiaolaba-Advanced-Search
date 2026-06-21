"""Remote mini-program API client."""

from __future__ import annotations

from pathlib import Path

import requests
import urllib3

from crawler.config import BASE_URL, COMMUNITY_ID, HEADERS

urllib3.disable_warnings()


class AuthenticationExpired(RuntimeError):
    pass


class RemoteAPIError(RuntimeError):
    pass


def load_cookie(config_path: str | Path) -> str:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"missing cookie config: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        if "ys7_ysxy_session=" in line:
            return line.strip().split("=", 1)[1]
    raise RuntimeError(f"cookie not found in {path}")


class MiniProgramClient:
    def __init__(self, cookie: str):
        self.cookie = cookie
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.session.cookies.set("ys7_ysxy_session", cookie)
        self.session.verify = False

    def get(
        self,
        path: str,
        params: dict | None = None,
    ) -> tuple[dict | None, str | None]:
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
        code = payload.get("code")
        if code == "0000":
            return payload.get("data", {}), None
        if code == "1000":
            return None, "cookie_expired"
        if code == "0102":
            return None, "not_found"
        return None, f"code={code} {payload.get('message', '')}"

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

    def latest_id(self) -> int:
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
