"""Contracts shared by AI search orchestration and API adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit


class AiSearchError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def validate_api_url(value: str, *, optional: bool = False) -> str:
    value = value.strip().rstrip("/")
    if optional and not value:
        return ""
    try:
        parsed = urlsplit(value)
        valid_port = parsed.port is None or 0 < parsed.port <= 65535
    except ValueError:
        valid_port = False
    else:
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            parsed.hostname and valid_port and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
            and (parsed.scheme == "https" or (parsed.scheme == "http" and local))
        ):
            return value
    raise AiSearchError("invalid_endpoint", "接口地址应为 HTTPS URL（本机接口可用 HTTP），不能含账号、查询参数或片段")


@dataclass(frozen=True)
class AiSearchSettings:
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    api_key: str = field(default="", repr=False)
    search_url: str = ""
    search_api_key: str = field(default="", repr=False)

    def public(self) -> dict:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key_set": bool(self.api_key),
            "search_url": self.search_url,
            "search_api_key_set": bool(self.search_api_key),
        }


def excerpt(text: str, terms: list[str], limit: int = 1200) -> str:
    text = str(text or "")
    positions = [text.casefold().find(term.casefold()) for term in terms if term]
    positions = [position for position in positions if position >= 0]
    start = max(0, min(positions, default=0) - limit // 4)
    return ("…" if start else "") + text[start:start + limit] + ("…" if len(text) > start + limit else "")


def calendar_context(timestamp: str) -> dict:
    """Calendar facts for relative-date evidence; never a new opening rule."""
    try:
        day = datetime.fromisoformat(timestamp[:19]).date()
    except (ValueError, TypeError):
        return {}
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return {"source_date": day.isoformat(), "source_weekday": names[day.weekday()],
            "明天": (day + timedelta(days=1)).isoformat(),
            "后天": (day + timedelta(days=2)).isoformat(),
            "following_days": [{"date": (day + timedelta(days=offset)).isoformat(),
                                "weekday": names[(day.weekday() + offset) % 7]} for offset in range(7)]}


def missing_live_evidence_reason(question: str, latest_post_time: str) -> str:
    """Hard limits of this historical, unofficial corpus, independent of LLM votes."""
    current = bool(re.search(r"现在|此刻|实时|当前|今天|最新", question))
    if current and re.search(r"排队|还有几个人|还剩|库存|余量|剩余|实时人数", question):
        return "no_live_observations"
    requested = None
    match = re.search(r"(20\d{2})[-年](\d{1,2})(?:[-月](\d{1,2})日?)?", question)
    if match:
        try:
            requested = datetime(int(match[1]), int(match[2]), int(match[3] or 1)).date().isoformat()
        except ValueError:
            pass
    if "官方" in question and (current or (requested and (not latest_post_time or requested > latest_post_time[:10]))):
        return "no_current_official_source"
    return ""
