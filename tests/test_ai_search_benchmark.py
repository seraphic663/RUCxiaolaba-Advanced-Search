"""Persistence, concurrency and billing boundaries of admin benchmark jobs."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from app.domain.ai_search import AiSearchError, AiSearchSettings
from app.http.server import ApplicationContext, Handler
from app.http.server import ThreadingHTTPServer as AppServer
from app.services.ai_search_benchmark_service import AiSearchBenchmarkService
from app.services.ai_search_config import AiSearchConfigStore
from app.services.ai_search_costs import estimate, usage_cost
from app.services.auth_service import AdminAuthService
from app.services.template_service import TemplateService

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bench(tmp_path, monkeypatch):
    for key in ("AI_SEARCH_API_KEY", "DEEPSEEK_API_KEY", "AI_SEARCH_BASE_URL", "AI_SEARCH_MODEL", "AI_SEARCH_RETRIEVAL_URL", "AI_SEARCH_RETRIEVAL_KEY"):
        monkeypatch.delenv(key, raising=False)
    db = tmp_path / "posts.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            create table posts(id text primary key,content text,create_time text);
            create table comments(post_id text,comment_id text,parent_comment_id text,detail text,create_time text);
            insert into posts values('1','地点在一楼','2025-01-01');
        """)
    cases = [{"id": f"{i+1:03}", "query": f"问题{i+1}", "domain": str(i), "intent": str(i),
              "answerable": i < 9, "reference_answer": "地点在一楼" if i < 9 else "没有证据",
              "evidence_post_ids": ["1"] if i < 9 else [], "required_patterns": ["一楼"] if i < 9 else [],
              "source_scope_patterns": ["地点"], "negative_kind": "unavailable"} for i in range(18)]
    manifest = tmp_path / "cases.json"
    manifest.write_text(json.dumps({"version": 3, "cases": cases}), encoding="utf-8")
    config = AiSearchConfigStore(tmp_path / "config.json")
    config.save({"api_key": "never-return-this-key"})

    class Engine:
        def __init__(self):
            self.config, self.search_service = config, SimpleNamespace(repository=SimpleNamespace(posts_db=db))
            self.lock = threading.Lock()
            self.active, self.maximum, self.calls = 0, 0, 0
            self.barrier = None
            self.settings_seen = []
            self.fail_query = ""

        def run(self, payload, *, settings, wait_for_slot):
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                self.calls += 1
                self.settings_seen.append(settings)
            try:
                if self.barrier:
                    self.barrier.wait(timeout=10)
                if payload["query"] == self.fail_query:
                    raise RuntimeError("sensitive-key-from-vendor")
                positive = int(payload["query"][2:]) <= 9
                source = {"post_id": "1", "ref": 1, "content": "地点在一楼",
                          "passages": [{"id": "p:1", "text": "地点在一楼"}]}
                return {"answer_status": "generated" if positive else "no_answer", "answer": "地点在一楼 [1]" if positive else "",
                        "sources": [source], "citation_check": {"accepted": 1},
                        "claims": [{"citations": [{"post_id": "1", "evidence_id": "p:1", "quote": "地点在一楼"}]}] if positive else [],
                        "stats": {"elapsed_ms": 10000, "ai_calls": 4, "usage_reported_calls": 4,
                                  "usage": {"prompt_tokens": 12000, "completion_tokens": 800}}}
            finally:
                with self.lock:
                    self.active -= 1

    engine = Engine()
    runner = AiSearchBenchmarkService(engine, tmp_path / "runs", manifest_path=manifest, archive_dir=tmp_path / "archive")
    yield runner, engine
    if runner._thread:
        runner._thread.join(timeout=15)


def finish(runner):
    runner._thread.join(timeout=15)
    assert not runner._thread.is_alive()


def test_eighteen_queries_concurrent_are_persisted_and_reloadable(bench):
    runner, engine = bench
    assert runner.catalog()["history"] == []
    assert engine.calls == 0
    engine.barrier = threading.Barrier(18)
    run = runner.start({"request_id": "one-run-unique-id", "concurrency": 18})
    finish(runner)
    detail = runner.detail(run["id"])
    assert engine.maximum == 18 and engine.calls == 18
    assert detail["summary"]["completed"] == 18 and detail["summary"]["passed"] == 18
    assert detail["summary"]["status"] == "complete"
    assert detail["summary"]["cost"]["usage_complete"]
    assert "never-return-this-key" not in json.dumps(detail)
    assert len({id(settings) for settings in engine.settings_seen}) == 1
    reloaded = AiSearchBenchmarkService(engine, runner.runs_dir, manifest_path=runner.manifest_path, archive_dir=runner.archive_dir)
    assert reloaded.detail(run["id"])["summary"]["passed"] == 18
    assert reloaded.start({"request_id": "one-run-unique-id"})["id"] == run["id"]
    assert engine.calls == 18


def test_single_retest_preserves_old_failure_and_never_leaks_exception(bench):
    runner, engine = bench
    engine.fail_query = "问题1"
    first = runner.start({"request_id": "first-request-id", "case_ids": ["001"]})
    finish(runner)
    failed = runner.detail(first["id"])
    assert failed["summary"]["failed"] == 1
    assert not failed["summary"]["cost"]["usage_complete"]
    assert failed["summary"]["cost"]["unmetered_queries"] == 1
    assert "sensitive" not in json.dumps(failed)
    engine.fail_query = ""
    second = runner.start({"request_id": "second-request-id", "case_ids": ["001"], "concurrency": 1})
    finish(runner)
    assert second["id"] != first["id"]
    assert runner.detail(second["id"])["summary"]["passed"] == 1
    assert runner.detail(first["id"])["summary"]["failed"] == 1


def test_unfinished_job_is_interrupted_without_paid_resume(bench):
    runner, engine = bench
    out = runner.runs_dir / "20260101-000000-interrupted"
    out.mkdir(parents=True)
    (out / "results.json").write_text(json.dumps({"status": "running", "total": 18, "runs": []}), encoding="utf-8")
    assert runner.catalog()["history"][0]["status"] == "interrupted"
    assert runner.catalog()["active_id"] is None and engine.calls == 0


def test_bad_design_or_options_cannot_spend_api(bench):
    runner, engine = bench
    for values in ({"concurrency": 19}, {"concurrency": True}, {"case_ids": ["unknown"]}, {"case_ids": "001"}):
        with pytest.raises(AiSearchError):
            runner.start({"request_id": "valid-request-id", **values})
    runner.manifest_path.write_text('{"cases": []}', encoding="utf-8")
    with pytest.raises(AiSearchError, match="核查失败"):
        runner.start({"request_id": "valid-request-id"})
    assert engine.calls == 0
    with pytest.raises(AiSearchError) as error:
        runner.detail("../../config.json")
    assert error.value.status == 404


def test_second_job_rejected_while_first_is_running(bench):
    runner, engine = bench
    entered, release = threading.Event(), threading.Event()
    original = engine.run

    def waiting(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return original(*args, **kwargs)

    engine.run = waiting
    first = runner.start({"request_id": "first-running-job", "case_ids": ["001"]})
    assert entered.wait(timeout=5)
    try:
        assert runner.start({"request_id": "first-running-job"})["id"] == first["id"]
        with pytest.raises(AiSearchError) as error:
            runner.start({"request_id": "another-running-job"})
        assert error.value.status == 409
    finally:
        release.set()
    finish(runner)


def test_usage_cache_pricing_unknown_provider_and_incomplete_usage():
    settings = AiSearchSettings()
    cost = usage_cost(settings, {"prompt_tokens": 1000000, "prompt_cache_hit_tokens": 500000, "completion_tokens": 100000}, calls=4, reported=3)
    assert cost["low_cny"] == pytest.approx(0.91) and cost["high_cny"] == pytest.approx(1.82)
    assert not cost["usage_complete"]
    assert not usage_cost(AiSearchSettings(base_url="https://proxy.example"), {}, calls=1)["available"]
    serial = estimate(settings, [{"stats": {"elapsed_ms": 10000}}] * 18, concurrency=1)
    parallel = estimate(settings, [{"stats": {"elapsed_ms": 10000}}] * 18, concurrency=18)
    assert serial["cost"] == parallel["cost"]
    assert serial["seconds_low"] > parallel["seconds_low"]


def test_benchmark_http_auth_csrf_estimate_and_real_job_contract(bench):
    runner, engine = bench
    context = ApplicationContext(posts_db="unused", admin_password="test", posts=None, search=None, admin=None,
                                 admin_crawl=None, auth=AdminAuthService(), templates=TemplateService(ROOT / "app/templates"),
                                 ai_benchmark=runner)

    class LocalHandler(Handler):
        pass

    LocalHandler.context = context
    server = AppServer(("127.0.0.1", 0), LocalHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert requests.get(base + "/admin/ai-search/bench", allow_redirects=False).status_code == 302
        assert requests.get(base + "/api/admin/ai-search/bench").status_code == 401
        assert requests.post(base + "/api/admin/ai-search/bench/run", json={}).status_code == 401
        session = requests.Session()
        session.cookies.set("admin_token", context.auth.create_session())
        page = session.get(base + "/admin/ai-search/bench")
        assert page.status_code == 200 and "__ADMIN_" not in page.text
        assert page.headers["Cache-Control"] == "no-store"
        catalog = session.get(base + "/api/admin/ai-search/bench")
        assert catalog.status_code == 200 and engine.calls == 0
        assert "never-return-this-key" not in catalog.text
        assert session.post(base + "/api/admin/ai-search/bench/run", json={}).status_code == 403
        token = catalog.json()["csrf_token"]
        response = session.post(base + "/api/admin/ai-search/bench/estimate", json={"concurrency": 18}, headers={"X-CSRF-Token": token})
        assert response.status_code == 200 and engine.calls == 0
        token = response.json()["csrf_token"]
        response = session.post(base + "/api/admin/ai-search/bench/run", json={"request_id": "http-request-id", "case_ids": ["001"]}, headers={"X-CSRF-Token": token})
        assert response.status_code == 202
        assert session.post(base + "/api/admin/ai-search/bench/run", json={}, headers={"X-CSRF-Token": token}).status_code == 403
        finish(runner)
        response = session.get(base + "/api/admin/ai-search/bench", params={"run_id": response.json()["run"]["id"]})
        assert response.json()["summary"]["passed"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
