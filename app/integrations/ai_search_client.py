"""HTTP adapters for a chat-completions API and an optional search API."""

from __future__ import annotations

import json
import threading

import requests

from app.domain.ai_search import AiSearchError, AiSearchSettings

MAX_RESPONSE_BYTES = 1024 * 1024


def request_json(method: str, url: str, *, key: str = "", requester=None, **kwargs) -> dict:
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    try:
        with (requester or requests.request)(
            method, url, headers=headers, timeout=(5, 45),
            allow_redirects=False, stream=True, **kwargs,
        ) as response:
            if response.status_code != 200:
                status = response.status_code
                message = {
                    401: "接口鉴权失败，请检查 API Key",
                    402: "接口余额不足，请检查账户额度",
                    403: "接口拒绝访问，请检查权限",
                    429: "接口触发限流，请稍后再试",
                }.get(status, f"接口返回 HTTP {status}，请检查地址、模型和服务状态")
                raise AiSearchError("upstream_http_error", message, 502)
            body = bytearray()
            for chunk in response.iter_content(16384):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise AiSearchError("upstream_too_large", "接口返回内容过大", 502)
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ValueError("not an object")
            return result
    except requests.Timeout as exc:
        raise AiSearchError("upstream_timeout", "接口请求超时，请稍后重试", 504) from exc
    except requests.RequestException as exc:
        raise AiSearchError("upstream_unavailable", "无法连接接口，请检查地址和网络", 502) from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise AiSearchError("upstream_invalid_json", "接口未返回有效 JSON", 502) from exc


class AiSearchClient:
    def __init__(self):
        self._thread = threading.local()

    def _request(self, method, url, **kwargs):
        if not hasattr(self._thread, "session"):
            self._thread.session = requests.Session()
        # Reuse TLS connections within a request/benchmark worker. No retry
        # adapter, default credentials or administrator cookies are installed.
        return self._thread.session.request(method, url, **kwargs)

    def chat(self, settings: AiSearchSettings, messages: list[dict], *, structured=False) -> tuple[str, dict]:
        if not settings.api_key:
            raise AiSearchError("ai_not_configured", "请先在接口设置中填写 AI API Key", 409)
        url = settings.base_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        payload = {
            "model": settings.model, "messages": messages, "stream": False,
            "max_tokens": 2400 if structured else 2000,
        }
        if settings.model.startswith("deepseek-"):
            payload["thinking"] = {"type": "disabled"}
        if structured:
            payload["response_format"] = {"type": "json_object"}
        result = request_json("POST", url, key=settings.api_key, requester=self._request, json=payload)
        try:
            choice = result["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip() or choice.get("finish_reason") == "length":
                raise ValueError("empty or truncated content")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AiSearchError("ai_invalid_response", "AI 未返回完整有效的回答", 502) from exc
        usage = result.get("usage", {})
        return content.strip(), usage if isinstance(usage, dict) else {}

    def retrieve(self, settings: AiSearchSettings, query: str, **params) -> dict:
        result = request_json(
            "GET", settings.search_url, key=settings.search_api_key, requester=self._request,
            params={"q": query, **params},
        )
        if result.get("ok") is False or not isinstance(result.get("results"), list):
            raise AiSearchError("search_invalid_response", "检索接口须返回含 results 数组的 JSON", 502)
        return result
