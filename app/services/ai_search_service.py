"""Public AI search service: request validation, limits and pipeline assembly."""

from __future__ import annotations

import threading
from datetime import datetime
from datetime import time as day_time

from app.domain.ai_search import AiSearchError
from app.integrations.ai_search_client import AiSearchClient
from app.repositories.ai_evidence_repository import AiEvidenceRepository
from app.services.ai_search_config import AiSearchConfigStore
from app.services.ai_search_costs import estimate
from app.services.ai_search_pipeline import ANSWER_PROMPT as ANSWER_PROMPT
from app.services.ai_search_pipeline import PLAN_PROMPT as PLAN_PROMPT
from app.services.ai_search_pipeline import AiSearchPipeline
from app.services.search_service import SearchService

MAX_CONCURRENT_QUERIES = 18


class AiSearchService:
    def __init__(self, search: SearchService, config: AiSearchConfigStore, *, client=None):
        self.search_service = search
        self.config = config
        self.client = client or AiSearchClient()
        self.evidence = AiEvidenceRepository(search.repository.posts_db)
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_QUERIES)

    def estimate(self, payload: dict) -> dict:
        request = self._request(payload)
        settings = self.config.load()
        sample = {} if request["mode"] == "answer" and settings.api_key else {"stats": {"ai_calls": 0}}
        return estimate(settings, [sample])

    def test_connection(self) -> dict:
        if not self._slots.acquire(blocking=False):
            raise AiSearchError("ai_busy", "AI 检索正在处理其他请求，请稍后再试", 429)
        try:
            settings = self.config.load()
            self.client.chat(settings, [{"role": "user", "content": "只回复 OK"}])
            return {"model": settings.model, "message": "AI 接口连接正常"}
        finally:
            self._slots.release()

    @staticmethod
    def _request(payload: dict) -> dict:
        query = payload.get("query", "")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
            raise AiSearchError("invalid_query", "请输入 1–500 字的问题或关键词")
        scope = payload.get("scope", "all")
        mode = payload.get("mode", "answer")
        if scope not in ("content", "all") or mode not in ("answer", "retrieve"):
            raise AiSearchError("invalid_options", "检索范围或模式无效")
        top_k = payload.get("top_k", 6)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 10:
            raise AiSearchError("invalid_limit", "来源数量应在 1–10 之间")
        dates = {}
        for key, end in (("date_from", False), ("date_to", True)):
            value = payload.get(key, "")
            try:
                dates[key] = datetime.combine(datetime.strptime(value, "%Y-%m-%d").date(), day_time.max if end else day_time.min) if value else None
            except (ValueError, TypeError) as exc:
                raise AiSearchError("invalid_date", "日期格式应为 YYYY-MM-DD") from exc
        if dates["date_from"] and dates["date_to"] and dates["date_from"] > dates["date_to"]:
            raise AiSearchError("invalid_date", "开始日期不能晚于结束日期")
        return {"query": query.strip(), "scope": scope, "mode": mode, "top_k": top_k, **dates}

    def run(self, payload: dict, *, settings=None, wait_for_slot=False) -> dict:
        request = self._request(payload)
        if not self._slots.acquire(blocking=wait_for_slot):
            raise AiSearchError("ai_busy", "AI 检索正在处理其他请求，请稍后再试", 429)
        try:
            return self._run(request) if settings is None else AiSearchPipeline(self).run(request, settings=settings)
        finally:
            self._slots.release()

    def _run(self, request: dict) -> dict:
        return AiSearchPipeline(self).run(request)
