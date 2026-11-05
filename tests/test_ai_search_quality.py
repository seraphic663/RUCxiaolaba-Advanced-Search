"""Source-grounding regressions, using a tiny synthetic forum."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.domain.ai_search import AiSearchError, calendar_context
from app.repositories.ai_evidence_repository import AiEvidenceRepository
from app.services.ai_answer_grounding import grounded_claims, render_claims
from app.services.ai_search_config import AiSearchConfigStore
from app.services.ai_search_retrieval import diverse_documents, queries_from_plan
from app.services.ai_search_service import AiSearchService
from app.services.search_service import SearchService
from storage.bigram_index import build_bigram_index


def test_concurrent_dataset_info_scans_once_and_invalidates_after_write(tmp_path, monkeypatch):
    db = tmp_path / "metadata.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("create table posts(id text,create_time text); insert into posts values('1','2025-01-01');")
    repository = AiEvidenceRepository(db)
    original = repository._dataset_info_uncached
    scans = []

    def scan():
        scans.append(1)
        return original()

    monkeypatch.setattr(repository, "_dataset_info_uncached", scan)
    with ThreadPoolExecutor(max_workers=18) as pool:
        results = list(pool.map(lambda _: repository.dataset_info(), range(18)))
    assert len(scans) == 1 and all(result["latest_post_time"] == "2025-01-01" for result in results)
    with sqlite3.connect(db) as conn:
        conn.execute("insert into posts values('2','2026-01-01')")
    assert repository.dataset_info()["latest_post_time"] == "2026-01-01"
    assert len(scans) == 2
    with sqlite3.connect(db) as conn:
        conn.execute("pragma journal_mode=wal")
        repository.dataset_info()
        previous_stat = db.stat().st_mtime_ns
        conn.execute("insert into posts values('3','2027-01-01')")
        conn.commit()
        assert db.stat().st_mtime_ns == previous_stat
        assert repository.dataset_info()["latest_post_time"] == "2027-01-01"


def test_short_numeric_reply_keeps_its_parent_question(tmp_path):
    path = tmp_path / "posts.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            create table posts(id text primary key, content text);
            create table comments(row_key text, comment_id text, post_id text,
              parent_comment_id text, detail text, create_time text);
            insert into posts values ('1','图书馆早上几点开门？');
            insert into comments values ('1:c1','c1','1','','7：00','2026-01-01');
            insert into comments values ('1:c2','c2','1','','谢谢','2026-01-02');
        """)
    selected = AiEvidenceRepository(path).comments("1", ["图书馆", "开门"])
    assert any(comment["content"] == "7：00" for comment in selected)


def test_alternative_facets_do_not_require_all_words_in_one_and_query():
    queries = queries_from_plan("图书馆开放时间", {"queries": ["图书馆 开门 关门", "图书馆 开馆 几点"]})
    assert "图书馆 开门" in queries and "图书馆 关门" in queries
    assert "图书馆 开馆" in queries and "图书馆 几点" in queries
    assert queries[0] == "图书馆开放时间" and len(queries) <= 8


def evidence_sources():
    return [
        {"post_id": "A", "ref": 1, "passages": [{"id": "c:A:1", "text": "7：00", "kind": "comment", "time": "2025-09-04"}]},
        {"post_id": "B", "ref": 2, "passages": [{"id": "c:B:2", "text": "22点闭馆", "kind": "comment", "time": "2026-03-02"}]},
    ]


@pytest.mark.parametrize("citation", [
    {"id": "c:A:1", "quote": "22点闭馆"},
    {"id": "c:nonexistent:1", "quote": "7：00"},
    {"id": "c:B:2", "quote": "不在原文的说法"},
])
def test_wrong_post_or_invented_quote_cannot_produce_an_answer(citation):
    claims, rejected = grounded_claims({"claims": [{"text": "某条说法", "evidence": [citation]}]}, evidence_sources())
    assert not claims and rejected == 1


def test_citation_number_and_ownership_are_assigned_by_server():
    claims, rejected = grounded_claims({"claims": [{"text": "历史回复给出的开门时间是7点。", "evidence": [{"id": "c:A:1", "quote": "7:00"}]}]}, evidence_sources())
    assert rejected == 0
    assert claims[0]["citations"][0]["post_id"] == "A"
    assert render_claims(claims).endswith("[1]")


def test_repeated_long_posts_do_not_take_multiple_source_slots():
    body = "寒假留校互助群，分享图书馆开放时间和食堂营业信息。" * 5
    documents = [{"post_id": str(i), "content": body, "comments": [], "duplicate_post_ids": []} for i in (1, 2, 3)]
    selected, removed = diverse_documents(documents)
    assert len(selected) == 1 and removed == 2
    assert selected[0]["duplicate_post_ids"] == ["2", "3"]


def test_duplicate_questions_with_conflicting_substantive_replies_are_kept():
    body = "关于图书馆开放时间的详细问题。" * 10
    docs = [
        {"post_id": "1", "content": body, "comments": [{"content": "明确回复每天早上七点开门。"}], "duplicate_post_ids": []},
        {"post_id": "2", "content": body, "comments": [{"content": "明确回复最近改成八点开门。"}], "duplicate_post_ids": []},
    ]
    assert len(diverse_documents(docs)[0]) == 2


@pytest.mark.parametrize("left,right", [("7：00", "8：00"), ("可以", "不可以")])
def test_deduplication_preserves_conflicting_short_answers(left, right):
    body = "同一个较长的图书馆开放与使用问题。" * 8
    docs = [{"post_id": str(i), "content": body, "comments": [{"content": reply}], "duplicate_post_ids": []}
            for i, reply in enumerate((left, right), 1)]
    assert len(diverse_documents(docs)[0]) == 2


def test_related_question_without_answer_is_not_answerable(tmp_path):
    config = AiSearchConfigStore(tmp_path / "ai.json")
    config.save({"api_key": "fake"})

    class Client:
        calls = 0

        def chat(self, settings, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return '{"queries":["图书馆"]}', {}
            if "任务：answer" in messages[0]["content"]:
                return '{"answerable":false,"claims":[]}', {}
            assert "任务：rank" in messages[0]["content"]
            data = json.loads(messages[-1]["content"])
            candidate = data["candidates"][0]
            return json.dumps({"answerable": False, "ranking": [{"post_id": candidate["post_id"], "relevance": 2, "evidence_ids": [], "supports_answer": False}]}), {}

    root = Path(__file__).resolve().parents[1]
    client = Client()
    result = AiSearchService(SearchService(root / "demo/posts.db", root / "demo/bigram_index.db"), config, client=client).run({"query": "法学院图书馆几点开放？"})
    assert result["sources"] and result["answer_status"] == "no_answer"
    assert not result["answer"] and not result["claims"]
    assert client.calls == 3


@pytest.mark.parametrize("reject_stage", ["quote", "semantic", "clarification"])
def test_unsupported_claim_or_missing_personal_context_never_becomes_answer(tmp_path, reject_stage):
    config = AiSearchConfigStore(tmp_path / "ai.json")
    config.save({"api_key": "fake"})

    class Client:
        calls = 0

        def chat(self, settings, messages, **kwargs):
            self.calls += 1
            task = messages[0]["content"]
            data = json.loads(messages[-1]["content"])
            if "任务：plan" in task:
                response = {"queries": ["图书馆"], "requires_clarification": reject_stage == "clarification"}
            elif "任务：rank" in task:
                candidate = data["candidates"][0]
                response = {"answerable": True, "ranking": [{"post_id": candidate["post_id"], "relevance": 3,
                    "evidence_ids": [p["id"] for p in candidate["passages"]], "supports_answer": True}]}
            elif "任务：answer" in task:
                p = data["sources"][0]["passages"][0]
                response = {"answerable": True, "claims": [{"text": "这是未经支持的个人安排。", "evidence": [{
                    "id": p["id"], "quote": "原文中不存在的片段" if reject_stage == "quote" else p["text"]}]}]}
            else:
                assert data["checks"][0]["evidence"][0]["source_question"]
                response = {"answerable": False, "supported_claim_indices": []}
            return json.dumps(response, ensure_ascii=False), {}

    root = Path(__file__).resolve().parents[1]
    client = Client()
    result = AiSearchService(SearchService(root / "demo/posts.db", root / "demo/bigram_index.db"), config, client=client).run({"query": "图书馆"})
    assert not result["answer"] and not result["claims"]
    assert result["sources"]
    assert result["answer_status"] in {"unverified", "no_answer"}
    assert client.calls == {"quote": 3, "semantic": 4, "clarification": 2}[reject_stage]


def test_failed_or_blocked_request_is_not_a_successful_negative_benchmark():
    from tools.benchmarks.benchmark_ai_search import evaluate

    case = {"answerable": False}
    for status in ("error", "failed", "unverified", "not_configured"):
        assert not evaluate(case, {"answer_status": status, "answer": ""})["passed"]
    assert evaluate(case, {"answer_status": "no_answer", "answer": ""})["passed"]
    assert not evaluate(case, {"answer_status": "no_answer", "answer": "编造的答案"})["passed"]


def test_unsegmented_question_recovers_existing_entity_without_answer_hints(tmp_path):
    db = tmp_path / "posts.db"
    sidecar = tmp_path / "bigram.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            create table posts(id text primary key, content text, category_name text,
              user_name text, show_user_id text, real_user_id text, create_time text,
              comment_count integer, star_count integer, trace_count integer);
            create table comments(row_key text, comment_id text, post_id text, parent_comment_id text,
              detail text, show_user_name text, show_user_id text, real_user_id text,
              reply_show_user_name text, reply_show_user_id text, is_publisher integer, create_time text);
            insert into posts values ('9001','校园卡搞丢了，在哪里补办？','日常','','','','2026-01-01',1,0,0);
            insert into comments values ('9001:c1','c1','9001','','综合服务中心的自助机','','','','','',0,'2026-01-01');
        """)
    build_bigram_index(db, sidecar)
    result = AiSearchService(SearchService(db, sidecar), AiSearchConfigStore(tmp_path / "ai.json")).run({
        "query": "实体校园卡补办", "mode": "retrieve"})
    assert [source["post_id"] for source in result["sources"]] == ["9001"]
    assert result["stats"]["ai_calls"] == 0
    assert result["stats"]["retrieval_calls"] <= 8
    assert any(c["content"] == "综合服务中心的自助机" for c in result["sources"][0]["comments"])


def test_missing_or_invalid_optional_sidecar_does_not_break_anchor_fallback(tmp_path):
    db, invalid = tmp_path / "posts.db", tmp_path / "invalid.db"
    with sqlite3.connect(db):
        pass
    with sqlite3.connect(invalid):
        pass
    assert AiEvidenceRepository(db).anchor_candidates("实体校园卡补办", invalid) == []


def test_planner_network_failure_is_never_relabelled_as_no_results(tmp_path):
    class BrokenClient:
        def chat(self, *args, **kwargs):
            raise AiSearchError("upstream_unavailable", "接口连接失败", 502)

    root = Path(__file__).resolve().parents[1]
    config = AiSearchConfigStore(tmp_path / "ai.json")
    config.save({"api_key": "fake"})
    result = AiSearchService(SearchService(root / "demo/posts.db", root / "demo/bigram_index.db"), config, client=BrokenClient()).run({"query": "UNSEEN-TOKEN-7F29"})
    assert result["answer_status"] == "failed"
    assert not result["sources"] and not result["answer"]
    assert result["stats"]["pipeline_errors"] == [{"stage": "查询理解", "code": "upstream_unavailable"}]


def test_uncertain_source_is_not_presented_as_confirmed_even_if_model_drops_hedge():
    sources = [{"post_id": "A", "ref": 1, "passages": [{"id": "c:A:1", "text": "新中关？校内也有花店呀", "kind": "comment", "time": "2025-03-01"}]}]
    claims, rejected = grounded_claims({"claims": [{"text": "有用户称新中关有花店。", "evidence": [{"id": "c:A:1", "quote": "新中关？校内也有花店呀"}]}]}, sources)
    assert rejected == 0 and claims[0]["citations"][0]["uncertain"]
    assert "仅作线索" in claims[0]["text"]


def test_rank_answerability_false_is_advisory_when_later_evidence_checks_succeed(tmp_path):
    config = AiSearchConfigStore(tmp_path / "ai.json")
    config.save({"api_key": "fake"})

    class Client:
        def chat(self, settings, messages, **kwargs):
            task, data = messages[0]["content"], json.loads(messages[-1]["content"])
            if "任务：plan" in task:
                response = {"queries": ["图书馆"]}
            elif "任务：rank" in task:
                d = data["candidates"][0]
                response = {"answerable": False, "ranking": [{"post_id": d["post_id"], "relevance": 2,
                    "evidence_ids": [p["id"] for p in d["passages"]], "supports_answer": False}]}
            elif "任务：answer" in task:
                p = data["sources"][0]["passages"][-1]
                response = {"answerable": True, "claims": [{"text": "有用户回复：" + p["text"],
                    "evidence": [{"id": p["id"], "quote": p["text"]}]}]}
            else:
                response = {"answerable": True, "supported_claim_indices": [0]}
            return json.dumps(response, ensure_ascii=False), {}

    root = Path(__file__).resolve().parents[1]
    result = AiSearchService(SearchService(root / "demo/posts.db", root / "demo/bigram_index.db"), config, client=Client()).run({"query": "图书馆相关资料"})
    assert result["answer_status"] == "generated" and result["claims"]
    assert result["stats"]["ai_calls"] == 4


def test_relative_weekend_dates_use_program_calendar_not_model_guess():
    calendar = calendar_context("2025-05-30 19:48:36")
    assert calendar["source_weekday"] == "周五"
    assert calendar["明天"] == "2025-05-31"
    assert {row["weekday"]: row["date"] for row in calendar["following_days"]}["周日"] == "2025-06-01"


@pytest.mark.parametrize("query", [
    "2026年11月学校游泳馆官方收费标准是多少？",
    "2026年11月5日图书馆官方公布的准确开放时间是什么？",
    "明德楼自助打印机现在排队还有几个人？",
])
def test_historical_corpus_cannot_invent_official_or_realtime_answers(tmp_path, query):
    class NeverCall:
        def chat(self, *args, **kwargs):
            raise AssertionError("A model cannot add a missing official/live data source")

    root = Path(__file__).resolve().parents[1]
    config = AiSearchConfigStore(tmp_path / "ai.json")
    config.save({"api_key": "fake"})
    result = AiSearchService(SearchService(root / "demo/posts.db", root / "demo/bigram_index.db"), config, client=NeverCall()).run({"query": query})
    assert result["answer_status"] == "no_answer" and not result["answer"] and not result["claims"]
    assert result["abstention_reason"] in {"no_current_official_source", "no_live_observations"}
    assert result["stats"]["ai_calls"] == 0
