"""DeepSeek HTTP client and model-based query moderation."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import requests

from app.ai.policy import scrub_pii


@dataclass(frozen=True)
class DeepSeekSettings:
    api_key: str
    base_url: str
    model: str
    fallback_model: str
    moderation_model: str
    max_output_tokens: int
    request_timeout: int
    network_retries: int
    moderation_timeout: int
    moderation_retries: int


class DeepSeekClient:
    def __init__(self, settings: DeepSeekSettings):
        self.settings = settings

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def moderate(self, query: str) -> tuple[bool, str | None]:
        body = {
            "model": self.settings.moderation_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是论坛搜索请求安全审核器。必须拒绝人肉搜索、身份推断、"
                        "账号关联、联系方式、住址行踪、医疗隐私及违法实施方法。"
                        "允许普通校园生活和公开信息总结。只返回 JSON："
                        '{"allowed":true或false,"reason":"简短中文理由"}。'
                    ),
                },
                {
                    "role": "user",
                    "content": f"待审核搜索请求：{scrub_pii(query)}",
                },
            ],
            "temperature": 0,
            "max_tokens": 100,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
        }
        for attempt in range(self.settings.moderation_retries):
            try:
                response = requests.post(
                    f"{self.settings.base_url}/v1/chat/completions",
                    headers=self.headers,
                    json=body,
                    timeout=self.settings.moderation_timeout,
                )
                if not response.ok:
                    return False, "安全审核服务暂时不可用"
                raw = response.json()
                content = (raw.get("choices") or [{}])[0].get(
                    "message", {}
                ).get("content", "")
                parsed = json.loads(content) if isinstance(content, str) else content
                if not isinstance(parsed, dict) or not isinstance(
                    parsed.get("allowed"), bool
                ):
                    return False, "安全审核返回格式异常"
                if parsed["allowed"]:
                    return True, None
                return False, str(
                    parsed.get("reason") or "请求涉及不适合检索的敏感内容"
                )[:120]
            except (requests.RequestException, ValueError, json.JSONDecodeError):
                if attempt + 1 < self.settings.moderation_retries:
                    time.sleep(0.5)
        return False, "安全审核服务暂时不可用"

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[dict | None, str | None, int, int]:
        if not self.settings.api_key:
            return None, "ai_not_configured", 0, 0
        body = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 1e-6,
            "max_tokens": self.settings.max_output_tokens,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
        }
        response, last_error = self._post_with_retry(body)
        if response is None and self.settings.model != self.settings.fallback_model:
            fallback = dict(body)
            fallback["model"] = self.settings.fallback_model
            try:
                response = requests.post(
                    f"{self.settings.base_url}/v1/chat/completions",
                    headers=self.headers,
                    json=fallback,
                    timeout=self.settings.request_timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
        if response is None:
            detail = str(last_error).strip() or repr(last_error)
            return (
                None,
                f"请求失败 ({type(last_error).__name__}): {detail}",
                0,
                0,
            )
        return self._parse_completion(response)

    def _post_with_retry(self, body: dict):
        response = None
        last_error = None
        for attempt in range(self.settings.network_retries):
            try:
                response = requests.post(
                    f"{self.settings.base_url}/v1/chat/completions",
                    headers=self.headers,
                    json=body,
                    timeout=self.settings.request_timeout,
                )
                break
            except requests.RequestException as exc:
                last_error = exc
                if attempt + 1 < self.settings.network_retries:
                    time.sleep(0.75 * (2**attempt))
        return response, last_error

    @staticmethod
    def _parse_completion(response):
        if not response.ok:
            try:
                payload = response.json()
                message = (
                    payload.get("error", {}).get("message", "")
                    if isinstance(payload, dict)
                    else str(payload)
                )
            except ValueError:
                message = ""
            error = (
                f"API 返回错误 (HTTP {response.status_code}): {message}"
                if message
                else f"API HTTP {response.status_code}: {response.text[:200]}"
            )
            return None, error, 0, 0
        try:
            raw = response.json()
        except ValueError:
            return None, "API 返回了无法解析的 JSON", 0, 0
        usage = raw.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        content = (raw.get("choices") or [{}])[0].get("message", {}).get(
            "content", ""
        )
        try:
            parsed = json.loads(content) if isinstance(content, str) else content
            if not isinstance(parsed, dict):
                return (
                    None,
                    "AI 返回格式错误：预期 JSON 对象",
                    input_tokens,
                    output_tokens,
                )
            return parsed, None, input_tokens, output_tokens
        except json.JSONDecodeError:
            return (
                {"summary": str(content)[:2000], "cited": []},
                None,
                input_tokens,
                output_tokens,
            )
