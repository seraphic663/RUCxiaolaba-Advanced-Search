"""AI search contracts tested against the synthetic corpus and local HTTP APIs."""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

from app.domain.ai_search import AiSearchError, validate_api_url
from app.http.server import ApplicationContext, Handler
from app.http.server import ThreadingHTTPServer as AppHTTPServer
from app.integrations.ai_search_client import request_json
from app.repositories.post_repository import PostRepository
from app.services.admin_service import AdminService
from app.services.ai_search_config import AiSearchConfigStore
from app.services.ai_search_service import AiSearchService
from app.services.auth_service import AdminAuthService
from app.services.search_service import SearchService
from app.services.template_service import TemplateService

ROOT = Path(__file__).resolve().parents[1]


def fake_ai_content(messages):
    if messages[-1]["content"] == "只回复 OK":
        return "OK"
    task = messages[0]["content"]
    data = json.loads(messages[-1]["content"])
    if "任务：plan" in task:
        result = {"queries": ["图书馆", "自习"]}
    elif "任务：rank" in task:
        result = {"answerable": True, "ranking": [
            {"post_id": candidate["post_id"], "relevance": 3,
             "evidence_ids": [p["id"] for p in candidate["passages"]], "supports_answer": True}
            for candidate in data["candidates"][:4]
        ]}
    elif "任务：answer" in task:
        passage = next((p for p in data["sources"][0]["passages"] if p["kind"] == "comment"), data["sources"][0]["passages"][0])
        result = {"answerable": True, "claims": [{"text": "有同学分享：" + passage["text"],
                  "evidence": [{"id": passage["id"], "quote": passage["text"]}]}]}
    else:
        result = {"answerable": True, "supported_claim_indices": [c["index"] for c in data["checks"]]}
    return json.dumps(result, ensure_ascii=False)


@pytest.fixture
def config(tmp_path, monkeypatch):
    for key in ("AI_SEARCH_API_KEY", "DEEPSEEK_API_KEY", "AI_SEARCH_BASE_URL", "AI_SEARCH_MODEL", "AI_SEARCH_RETRIEVAL_URL", "AI_SEARCH_RETRIEVAL_KEY"):
        monkeypatch.delenv(key, raising=False)
    return AiSearchConfigStore(tmp_path / "ai.json")


@pytest.fixture
def upstream():
    records = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            records.append((self.path, dict(self.headers), payload))
            content = fake_ai_content(payload["messages"])
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }, ensure_ascii=False).encode())

        def do_GET(self):
            records.append((self.path, dict(self.headers), None))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"results": [
                {"id": "outside", "content": "独立检索接口返回的资料", "real_user_id": "secret-id"},
                {"id": "deleted", "content": "已删除", "source_state": "deleted_or_unavailable"},
            ]}, ensure_ascii=False).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", records
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def service(config, *, client=None):
    return AiSearchService(SearchService(ROOT / "demo/posts.db", ROOT / "demo/bigram_index.db"), config, client=client)


def test_saved_secrets_are_not_returned_and_blank_preserves_key(config):
    result = config.save({"api_key": "private-key", "search_api_key": "search-private"})
    assert result["api_key_set"] and result["search_api_key_set"]
    assert "private" not in json.dumps(result)
    config.save({"api_key": "", "model": "deepseek-flash"})
    assert config.load().api_key == "private-key"
    config.save({"clear_api_key": True})
    assert not config.load().api_key
    assert "private-key" not in repr(config.load())
    assert len(list(config.path.parent.iterdir())) == 1


@pytest.mark.parametrize("url", ["file:///data", "http://example.com/api", "https://user:password@example.com", "https://example.com/#fragment", "https://example.com/?token=secret", "https://example.com:bad", "https://example.com\nheader"])
def test_invalid_endpoints_rejected(url, config):
    with pytest.raises(AiSearchError):
        config.save({"base_url": url})


def test_loopback_and_https_endpoints_supported():
    assert validate_api_url("http://127.0.0.1:1234/v1/") == "http://127.0.0.1:1234/v1"
    assert validate_api_url("https://api.deepseek.com") == "https://api.deepseek.com"


def test_retrieve_only_requires_no_ai_and_keeps_body_comment_scope(config):
    engine = service(config)
    all_results = engine.run({"query": "图书馆", "mode": "retrieve", "scope": "all"})
    body_results = engine.run({"query": "图书馆", "mode": "retrieve", "scope": "content"})
    assert all_results["answer_status"] == "retrieval_only"
    assert all_results["stats"]["ai_calls"] == 0
    assert all_results["sources"]
    assert any(source["comments"] for source in all_results["sources"])
    assert all(not source["comments"] for source in body_results["sources"])
    for source in all_results["sources"]:
        assert "real_user_id" not in source and "show_user_id" not in source
    missing = engine.run({"query": "图书馆"})
    assert missing["answer_status"] == "not_configured"
    assert missing["sources"] and missing["warnings"]


def test_real_http_chat_contract_rewrite_fusion_citations_and_usage(config, upstream):
    base, records = upstream
    config.save({"base_url": base + "/v1", "api_key": "test-key"})
    result = service(config).run({"query": "图书馆什么时候开放？", "top_k": 4})
    assert result["answer_status"] == "generated"
    assert result["queries"][0] == "图书馆什么时候开放？"
    assert len(result["queries"]) == 3
    assert result["stats"]["ai_calls"] == 4
    assert result["stats"]["usage"]["total_tokens"] == 60
    assert len({source["post_id"] for source in result["sources"]}) == len(result["sources"])
    assert records[0][0] == "/v1/chat/completions"
    assert records[0][1]["Authorization"] == "Bearer test-key"
    assert records[0][2]["model"] == "deepseek-flash"
    assert records[0][2]["thinking"] == {"type": "disabled"}
    evidence = json.loads(records[1][2]["messages"][-1]["content"])["candidates"]
    assert evidence and any(any(p["kind"] == "comment" for p in source["passages"]) for source in evidence)
    assert "real_user_id" not in json.dumps(evidence)
    assert "show_user_id" not in json.dumps(evidence)
    assert "show_user_name" not in json.dumps(evidence)
    assert result["claims"][0]["citations"][0]["post_id"] in {source["post_id"] for source in result["sources"]}
    assert result["citation_check"]["accepted"] == 1


def test_external_search_contract_and_no_local_identity_or_comments(config, upstream):
    base, records = upstream
    config.save({"search_url": base + "/search", "search_api_key": "search-key"})
    result = service(config).run({"query": "♀找", "mode": "retrieve"})
    assert len(result["sources"]) == 1
    assert result["sources"][0]["post_id"] == "outside"
    assert not result["sources"][0]["comments"]
    assert "secret-id" not in json.dumps(result)
    assert "q=%E2%99%80%E6%89%BE" in records[0][0]
    assert records[0][1]["Authorization"] == "Bearer search-key"
    assert "Cookie" not in records[0][1]


@pytest.mark.parametrize("failure_at,reply", [(1, ""), (2, ""), (2, "没有引用的回答"), (2, "不存在的引用 [999]")])
def test_failed_ai_never_loses_retrieved_sources(config, failure_at, reply):
    config.save({"api_key": "test-key"})

    class Client:
        calls = 0

        def chat(self, *args, **kwargs):
            self.calls += 1
            if self.calls == failure_at:
                if not reply:
                    raise AiSearchError("upstream_timeout", "接口请求超时", 504)
                return reply, {}
            return '{"queries":["图书馆"]}', {}

    result = service(config, client=Client()).run({"query": "图书馆"})
    assert result["sources"]
    assert result["answer_status"] in {"failed", "unverified"}
    assert not result["answer"] and result["warnings"]
    assert result["stats"]["ai_calls"] == failure_at


def test_no_results_and_empty_or_invalid_input_never_calls_ai(config, upstream):
    base, records = upstream
    config.save({"base_url": base, "api_key": "test-key"})
    engine = service(config)
    result = engine.run({"query": "绝对不存在的资料", "mode": "retrieve"})
    assert result["answer_status"] == "no_results" and not result["answer"]
    for payload in ({"query": ""}, {"query": "x", "top_k": 11}, {"query": "x", "scope": "uid"}, {"query": "x", "date_from": "bad"}, {"query": "x", "date_from": "2026-11-05", "date_to": "2026-01-01"}):
        with pytest.raises(AiSearchError):
            engine.run(payload)
    assert records == []


def test_empty_recall_skips_answer_generation(config):
    config.save({"api_key": "test-key"})

    class Client:
        calls = 0

        def chat(self, *args, **kwargs):
            self.calls += 1
            assert kwargs.get("structured") is True
            return '{"queries":["绝对不存在的资料"]}', {}

    client = Client()
    result = service(config, client=client).run({"query": "绝对不存在的资料"})
    assert result["answer_status"] == "no_results"
    assert not result["sources"] and not result["answer"]
    assert client.calls == 1


def test_upstream_timeout_is_sanitized(monkeypatch):
    def timeout(*args, **kwargs):
        raise requests.Timeout("key=do-not-leak")

    monkeypatch.setattr(requests, "request", timeout)
    with pytest.raises(AiSearchError, match="超时") as error:
        request_json("POST", "https://example.com", key="secret")
    assert "do-not-leak" not in str(error.value)


@pytest.mark.parametrize("status,body,code", [(429, b'secret-upstream', 'upstream_http_error'), (401, b'secret-key', 'upstream_http_error'), (302, b'redirect', 'upstream_http_error'), (200, b'not-json', 'upstream_invalid_json'), (200, b'[]', 'upstream_invalid_json'), (200, b'x' * (1024 * 1024 + 1), 'upstream_too_large')])
def test_upstream_errors_are_bounded_sanitized_and_not_retried(monkeypatch, status, body, code):
    calls = []

    class Response:
        status_code = status

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_content(self, size):
            yield body

    def request(*args, **kwargs):
        calls.append(kwargs)
        return Response()

    monkeypatch.setattr(requests, "request", request)
    with pytest.raises(AiSearchError) as error:
        request_json("GET", "https://example.com", key="secret")
    assert error.value.code == code
    assert "secret" not in str(error.value)
    assert len(calls) == 1 and calls[0]["allow_redirects"] is False


@pytest.fixture
def app_server(config, upstream):
    base, records = upstream
    config.save({"base_url": base, "api_key": "do-not-return"})
    search = SearchService(ROOT / "demo/posts.db", ROOT / "demo/bigram_index.db")
    context = ApplicationContext(
        posts_db=str(ROOT / "demo/posts.db"), admin_password="test-only",
        posts=PostRepository(ROOT / "demo/posts.db"), search=search,
        admin=AdminService(ROOT / "demo/posts.db"), admin_crawl=None,
        auth=AdminAuthService(), templates=TemplateService(ROOT / "app/templates"),
        ai_search=AiSearchService(search, config),
    )

    class LocalHandler(Handler):
        pass

    LocalHandler.context = context
    server = AppHTTPServer(("127.0.0.1", 0), LocalHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", context, records
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def test_http_admin_auth_csrf_key_redaction_and_cited_answer(app_server):
    base, context, records = app_server
    for path in ("/api/admin/ai-search/settings",):
        assert requests.get(base + path).status_code == 401
    assert requests.post(base + "/api/admin/ai-search", json={"query": "图书馆"}).status_code == 401
    assert requests.get(base + "/admin/ai-search", allow_redirects=False).status_code == 302
    assert records == []
    session = requests.Session()
    html = session.get(base + "/admin").text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    response = session.post(base + "/admin", data={"password": "test-only", "csrf_token": csrf}, allow_redirects=False)
    assert response.status_code == 302
    page = session.get(base + "/admin/ai-search")
    assert page.status_code == 200 and "__ADMIN_" not in page.text
    assert page.headers["Cache-Control"] == "no-store"
    assert "do-not-return" not in page.text
    settings = session.get(base + "/api/admin/ai-search/settings")
    assert settings.json()["settings"]["api_key_set"]
    assert "do-not-return" not in settings.text
    assert session.post(base + "/api/admin/ai-search", json={"query": "图书馆"}).status_code == 403
    token = context.auth.create_csrf_token()
    response = session.post(base + "/api/admin/ai-search", headers={"X-CSRF-Token": token}, json={"query": "图书馆"})
    assert response.status_code == 200 and response.json()["answer_status"] == "generated"
    assert response.headers["Cache-Control"] == "no-store"
    assert session.post(base + "/api/admin/ai-search", headers={"X-CSRF-Token": token}, json={"query": "图书馆"}).status_code == 403
    assert "AI 检索" not in requests.get(base + "/").text


def test_http_invalid_json_and_settings_csrf(app_server):
    base, context, _ = app_server
    session = requests.Session()
    session.cookies.set("admin_token", context.auth.create_session())
    assert session.post(base + "/api/admin/ai-search/settings", json={"api_key": "bad"}).status_code == 403
    for body in ("[]", "{bad", "x" * 17000):
        response = session.post(base + "/api/admin/ai-search", headers={"X-CSRF-Token": context.auth.create_csrf_token()}, data=body)
        assert response.status_code == 400


def test_up_to_eighteen_requests_can_be_in_flight(config):
    engine = service(config)
    for _ in range(18):
        assert engine._slots.acquire(blocking=False)
    try:
        with pytest.raises(AiSearchError) as error:
            engine.run({"query": "图书馆"})
        assert error.value.status == 429
    finally:
        for _ in range(18):
            engine._slots.release()
