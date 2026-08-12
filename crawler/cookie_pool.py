"""Fixed cookie lanes and single-process request routing.

The pool deliberately stores references to local cookie files, never cookie
values.  A :class:`CookiePoolClient` still sends one HTTP request at a time;
it only chooses which already-authorized lane owns that request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


COOKIE_KINDS = ("new_list", "active_list", "detail", "probe")
_KIND_ALIASES = {
    "new_list": ("new_list", "new_list_calls", "daily_new_list_budget"),
    "active_list": (
        "active_list",
        "active_list_calls",
        "daily_active_list_budget",
    ),
    "detail": ("detail", "detail_calls", "daily_detail_budget"),
    "probe": ("probe", "probe_calls", "daily_probe_budget"),
}


@dataclass(frozen=True)
class CookieLaneSpec:
    """Public metadata for one fixed authenticated session lane."""

    lane_id: str
    config_path: Path
    daily_budgets: dict[str, int]
    weight: int = 1

    def budget(self, kind: str) -> int:
        return max(0, int(self.daily_budgets.get(kind, 0) or 0))


def _positive_int(value: object, *, field: str, lane_id: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"cookie lane {lane_id!r} has invalid {field}") from exc
    if number < 0:
        raise ValueError(f"cookie lane {lane_id!r} has negative {field}")
    return number


def _lane_entries(payload: object) -> list[dict]:
    if isinstance(payload, dict) and "lanes" in payload:
        payload = payload["lanes"]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        entries: list[dict] = []
        for lane_id, item in payload.items():
            if not isinstance(item, dict):
                continue
            entries.append({"id": lane_id, **item})
        return entries
    raise ValueError("cookie pool must contain a 'lanes' list or object")


def _parse_budgets(item: dict, lane_id: str) -> dict[str, int]:
    raw = item.get("daily_budgets")
    if raw is None:
        raw = item.get("budgets")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"cookie lane {lane_id!r} daily_budgets must be an object")
    budgets: dict[str, int] = {}
    for kind in COOKIE_KINDS:
        value = None
        for key in _KIND_ALIASES[kind]:
            if key in raw:
                value = raw[key]
                break
            if key in item:
                value = item[key]
                break
        budgets[kind] = _positive_int(
            0 if value is None else value,
            field=f"daily_budgets.{kind}",
            lane_id=lane_id,
        )
    if not any(budgets.values()):
        raise ValueError(f"cookie lane {lane_id!r} must have a positive budget")
    return budgets


def load_cookie_pool_specs(path: str | Path) -> tuple[CookieLaneSpec, ...]:
    """Load lane metadata from JSON without reading any cookie value."""

    pool_path = Path(path)
    if not pool_path.exists():
        raise FileNotFoundError(f"missing cookie pool config: {pool_path}")
    try:
        payload = json.loads(pool_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid cookie pool JSON: {pool_path}") from exc

    specs: list[CookieLaneSpec] = []
    seen: set[str] = set()
    for item in _lane_entries(payload):
        lane_id = str(item.get("id") or item.get("name") or "").strip()
        if not lane_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in lane_id):
            raise ValueError(f"invalid cookie lane id: {lane_id!r}")
        if lane_id in seen:
            raise ValueError(f"duplicate cookie lane id: {lane_id!r}")
        seen.add(lane_id)
        config_text = str(item.get("config") or item.get("config_path") or "").strip()
        if not config_text:
            raise ValueError(f"cookie lane {lane_id!r} is missing config")
        config_path = Path(config_text)
        if not config_path.is_absolute():
            config_path = pool_path.parent / config_path
        weight = _positive_int(item.get("weight", 1), field="weight", lane_id=lane_id)
        specs.append(
            CookieLaneSpec(
                lane_id=lane_id,
                config_path=config_path,
                daily_budgets=_parse_budgets(item, lane_id),
                weight=max(1, weight),
            )
        )
    if not specs:
        raise ValueError(f"cookie pool has no lanes: {pool_path}")
    return tuple(specs)


class CookiePoolClient:
    """Route one sequential crawler through a fixed pool of clients.

    A source-level rate-limit or expired-session error is returned immediately
    so the existing scheduler fuse can pause the crawler.  Only local quota
    exhaustion is eligible for another configured lane.
    """

    def __init__(
        self,
        specs: tuple[CookieLaneSpec, ...] | list[CookieLaneSpec],
        *,
        client_factory: Callable[[str, str], object] | None = None,
    ):
        self.specs = tuple(specs)
        if not self.specs:
            raise ValueError("cookie pool must contain at least one lane")
        self._client_factory = client_factory
        self._clients: dict[str, object] = {}
        self._disabled: set[str] = set()
        self._lane_request_counts = {spec.lane_id: 0 for spec in self.specs}
        self.last_lane_id = ""
        self.last_error = ""

    @classmethod
    def from_file(cls, path: str | Path) -> "CookiePoolClient":
        specs = load_cookie_pool_specs(path)

        def factory(cookie: str, lane_id: str):
            from crawler.client import MiniProgramClient

            return MiniProgramClient(cookie, lane_id=lane_id)

        clients = cls(specs, client_factory=factory)
        clients._pool_path = Path(path)
        return clients

    @property
    def lane_ids(self) -> tuple[str, ...]:
        return tuple(spec.lane_id for spec in self.specs)

    @property
    def lane_request_counts(self) -> dict[str, int]:
        return dict(self._lane_request_counts)

    @property
    def request_count(self) -> int:
        return sum(self._lane_request_counts.values())

    def _client(self, spec: CookieLaneSpec):
        client = self._clients.get(spec.lane_id)
        if client is not None:
            return client
        from crawler.client import load_cookie

        if self._client_factory is None:
            from crawler.client import MiniProgramClient

            client = MiniProgramClient(
                load_cookie(spec.config_path),
                lane_id=spec.lane_id,
            )
        else:
            client = self._client_factory(
                load_cookie(spec.config_path),
                spec.lane_id,
            )
        self._clients[spec.lane_id] = client
        return client

    @staticmethod
    def _kind(path: str) -> str:
        if "/article/article/info" in path:
            return "detail"
        if "/article/article/lists2" in path:
            return "active_list"
        if "/article/article/lists" in path or "/article/article/search" in path:
            return "new_list"
        return "detail"

    def _ordered_specs(self, kind: str) -> list[CookieLaneSpec]:
        candidates = [
            spec
            for spec in self.specs
            if (
                spec.lane_id not in self._disabled
                and spec.budget(kind) > 0
                and self._lane_request_counts[spec.lane_id] < spec.budget(kind)
            )
        ]
        candidates.sort(
            key=lambda spec: (
                self._lane_request_counts[spec.lane_id] / max(1, spec.budget(kind)),
                -spec.weight,
                self.lane_ids.index(spec.lane_id),
            )
        )
        return candidates

    def get(self, path: str, params: dict | None = None):
        kind = self._kind(path)
        self.last_lane_id = ""
        candidates = self._ordered_specs(kind)
        if not candidates:
            self.last_error = f"source_quota_budget_exhausted:{kind}"
            return None, self.last_error
        last_error = "source_quota_budget_exhausted"
        for spec in candidates:
            client = self._client(spec)
            before = int(getattr(client, "request_count", 0) or 0)
            data, error = client.get(path, params)
            after = int(getattr(client, "request_count", 0) or 0)
            self._lane_request_counts[spec.lane_id] += max(0, after - before)
            self.last_lane_id = spec.lane_id
            self.last_error = str(error or "")
            if not error:
                return data, None
            if str(error).startswith("rate_limited:"):
                return None, error
            if str(error).startswith("source_quota_"):
                self._disabled.add(spec.lane_id)
                last_error = str(error)
                continue
            if error == "cookie_expired":
                return None, error
            return data, error
        self.last_error = last_error
        return None, last_error

    def list_page(self, endpoint: str, page: int):
        return self.get(
            f"/article/article/{endpoint}",
            {"community_id": 4, "page": page},
        )

    def article(self, post_id: str):
        return self.get(
            "/article/article/info",
            {"community_id": 4, "id": str(post_id)},
        )

    def search(self, keyword: str, page: int):
        return self.get(
            "/article/article/search",
            {"community_id": 4, "search": keyword, "page": page},
        )

    def latest_id(self) -> int:
        data, error = self.list_page("lists", 1)
        if error:
            raise RuntimeError(f"cannot determine latest id: {error}")
        ids = []
        for item in (data or {}).get("list", []):
            try:
                ids.append(int(item.get("id") or 0))
            except (TypeError, ValueError):
                pass
        latest = max(ids, default=0)
        if latest <= 0:
            raise RuntimeError("cannot determine latest id from lists page 1")
        return latest

    def close(self) -> None:
        for client in self._clients.values():
            session = getattr(client, "session", None)
            close = getattr(session, "close", None)
            if close:
                close()
