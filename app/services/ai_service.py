"""AI search orchestration independent from HTTP transport."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from app.ai.client import DeepSeekClient
from app.ai.policy import evidence_payload, normalize_answer, validate_query
from app.ai.prompts import build_prompt
from app.ai.retriever import retrieve_ai
from app.repositories.ai_access_repository import AIStore


class AIService:
    def __init__(
        self,
        *,
        posts_db: str | Path,
        store: AIStore,
        client: DeepSeekClient,
        context_limit: int,
        prompt_char_limit: int,
        max_concurrent: int = 1,
    ):
        self.posts_db = Path(posts_db)
        self.store = store
        self.client = client
        self.context_limit = context_limit
        self.prompt_char_limit = prompt_char_limit
        self._semaphore = threading.BoundedSemaphore(max_concurrent)

    def activate(self, code: str) -> tuple[int, dict, str | None]:
        normalized = code.strip().upper()
        if not normalized:
            return 400, {"ok": False, "error": "请输入邀请码"}, None
        ok, result = self.store.activate(normalized)
        if not ok:
            return 403, {"ok": False, "error": "邀请码无效或已禁用"}, None
        token = result
        status = self.store.get_status(self.store.hash_code(normalized))
        return (
            200,
            {
                "ok": True,
                "remaining": status["remaining"],
                "daily_quota": status["daily_quota"],
            },
            token,
        )

    def status(self, code_hash: str | None) -> tuple[int, dict]:
        if not code_hash:
            return 401, {
                "ok": False,
                "error": "未激活或会话已过期，请重新输入邀请码",
            }
        return 200, {"ok": True, **self.store.get_status(code_hash)}

    def search(
        self,
        query: str,
        *,
        is_admin: bool,
        code_hash: str | None,
    ) -> tuple[int, dict]:
        if not query.strip():
            return 400, {"ok": False, "error": "请输入搜索内容"}
        if not is_admin:
            if not code_hash:
                return 401, {
                    "ok": False,
                    "error": "请先激活邀请码或登录管理面板",
                }
            valid, reason = validate_query(query)
            if not valid:
                return 400, {"ok": False, "error": f"抱歉：{reason}"}
            allowed, reason = self.client.moderate(query)
            if not allowed:
                return 400, {"ok": False, "error": f"抱歉：{reason}"}

        reserved = False
        if not is_admin and code_hash:
            ok, result = self.store.reserve_quota(code_hash)
            if not ok:
                if result == "quota_exceeded":
                    return 429, {
                        "ok": False,
                        "error": "今日 AI 搜索次数已用完，请明天再试",
                    }
                return 403, {"ok": False, "error": "邀请码已失效"}
            reserved = True

        if not self._semaphore.acquire(timeout=30):
            if reserved and code_hash:
                self.store.release_quota(code_hash)
            return 503, {"ok": False, "error": "AI 服务繁忙，请稍后重试"}

        started = time.time()
        try:
            retrieved = retrieve_ai(query, self.posts_db, limit=20)
            context = retrieved[: self.context_limit]
            allowed_ids = {str(item["post"]["id"]) for item in context}
            if not retrieved:
                if reserved and code_hash:
                    self.store.release_quota(code_hash)
                    reserved = False
                return 200, {
                    "ok": True,
                    "summary": "抱歉，在论坛数据库中未找到相关帖子。请尝试更换关键词。",
                    "cited": [],
                    "retrieved_count": 0,
                }

            system_prompt, user_prompt = build_prompt(
                query,
                retrieved,
                context_limit=self.context_limit,
                char_limit=self.prompt_char_limit,
            )
            parsed, error, input_tokens, output_tokens = self.client.complete(
                system_prompt, user_prompt
            )
            if error:
                if reserved and code_hash:
                    self.store.release_quota(code_hash)
                    reserved = False
                return 502, {"ok": False, "error": f"AI 服务异常: {error}"}

            answer, cited = normalize_answer(parsed or {}, allowed_ids)
            stats, posts = evidence_payload(
                retrieved,
                cited,
                context_limit=self.context_limit,
            )
            summary_parts = [answer["overview"]]
            summary_parts.extend(
                f"{item['title']}：{item['detail']}"
                for item in answer["findings"]
            )
            if answer["caveat"]:
                summary_parts.append(answer["caveat"])
            response = {
                "ok": True,
                "summary": "\n\n".join(
                    part for part in summary_parts if part
                ),
                "answer": answer,
                "cited": cited,
                "evidence_stats": stats,
                "evidence_posts": posts,
                "retrieved_count": len(retrieved),
                "elapsed_s": round(time.time() - started, 2),
            }
            if is_admin:
                response["_debug"] = {
                    "tokens_in": input_tokens,
                    "tokens_out": output_tokens,
                    "evidence_stats": stats,
                }
            elif code_hash:
                status = self.store.get_status(code_hash)
                response["remaining"] = status["remaining"]
                response["daily_quota"] = status["daily_quota"]
            return 200, response
        except Exception:
            if reserved and code_hash:
                self.store.release_quota(code_hash)
            raise
        finally:
            self._semaphore.release()
