import sqlite3

import pytest

from crawler.service import CrawlerService
from storage.post_writer import SQLitePostStore


def articles(*ids, refresh="2026-11-24 12:00:00", comments=0):
    return [{"id": str(i), "create_time": "2026-11-24 08:00:00",
             "update_time": "2026-11-24 12:00:00", "refresh_time": refresh,
             "count_comment": comments} for i in ids]


@pytest.fixture
def scan(tmp_path):
    db = tmp_path / "posts.db"
    with SQLitePostStore(db) as store:
        store.init_schema()

    def run(pages, *, round_id="", cap=18, deadline=None):
        calls = []

        class Client:
            def list_page(self, endpoint, page):
                calls.append(page)
                result = pages.get(page, [])
                if isinstance(result, str):
                    return None, result
                return {"list": result}, None

        service = CrawlerService(db_path=db, cookie="test", lock_timeout=2)
        service.client = Client
        stats = service.discover_queue(
            command="discover-active", endpoint="lists2", since="2026-11-24 00:00:00",
            old_page_threshold=1, min_pages=3, max_pages=cap, min_delay=0, max_delay=0,
            active_coverage=True, active_round_id=round_id, yield_at_monotonic=deadline,
        )
        return calls, stats

    return db, run


def test_repeat_uses_event_keys_and_persists_changed_page(scan):
    db, run = scan
    a = articles(101, 102)
    b = articles(101, 102, refresh="2026-11-24 12:01:00")
    calls, stats = run({1: a, 2: b, 3: b})
    assert calls == [1, 2, 3]
    assert stats["stop_reason"] == "repeat"
    assert not stats["coverage_complete"]
    assert stats["last_page"]["page"] == 3
    with sqlite3.connect(db) as conn:
        assert conn.execute("select count(*) from list2_observation_log").fetchone()[0] == 4


def test_two_quiet_overlapping_pages_and_minimum_three(scan):
    _, run = scan
    pages = {i: articles(i*10, i*10+1, i*10+2) for i in range(1, 7)}
    run(pages)
    # Refresh-only activity is kept in the event log, and has no effect on the
    # continuation signal when the other two events still verify overlap.
    pages[3][0]["refresh_time"] = "changed"
    calls, stats = run(pages)
    assert calls == [1, 2, 3]
    assert stats["coverage_complete"] and stats["previous_overlap"]
    assert stats["last_page"]["new_events"] == 1
    assert stats["last_page"]["comment_growth"] == 0


def test_comment_growth_resets_quiet_streak(scan):
    _, run = scan
    pages = {i: articles(i*10, i*10+1, i*10+2) for i in range(1, 7)}
    run(pages)
    pages[3][0]["count_comment"] = 1
    calls, stats = run(pages)
    assert calls == [1, 2, 3, 4, 5]
    assert stats["stop_reason"] == "overlap"


def test_one_sticky_event_cannot_stop_depth(scan):
    _, run = scan
    pages = {i: articles(100, i) for i in range(1, 7)}
    run(pages)
    for batch in pages.values():
        batch[1]["refresh_time"] = "changed"
    calls, stats = run(pages, cap=6)
    assert calls == [1, 2, 3, 4, 5, 6]
    assert stats["stop_reason"] == "cap" and not stats["coverage_complete"]


def test_failed_page_leaves_incomplete_coverage(scan):
    _, run = scan
    calls, stats = run({1: articles(1, 2), 2: "network down", 3: articles(3, 4)})
    assert calls == [1, 2]
    assert stats["errors"] == 1 and stats["round_pages"] == 1
    assert stats["stop_reason"] == "error" and not stats["coverage_complete"]


def test_resume_rechecks_boundary_and_enforces_shared_budget(scan):
    db, run = scan
    pages = {i: articles(2*i, 2*i+1) for i in range(1, 8)}
    calls, stats = run(pages, cap=4, round_id="same", deadline=0)
    assert calls == [1] and stats["stop_reason"] == "list1_due"
    with sqlite3.connect(db) as conn:
        assert conn.execute("select value from ledger_state where key='lists2_baseline_ready'").fetchone()[0] == "0"
    calls, stats = run(pages, cap=18, round_id="same")
    assert calls == [1, 2, 3]
    assert stats["round_page_attempts"] == 4 and stats["stop_reason"] == "cap"
    assert stats["ledger_baseline"]
    with sqlite3.connect(db) as conn:
        runs = conn.execute("select distinct run_id from list2_observation_log").fetchall()
        assert runs == [("discover-active:same",)]
        assert conn.execute("select value from ledger_state where key='lists2_baseline_ready'").fetchone()[0] == "1"
    calls, stats = run(pages, cap=18, round_id="same")
    assert calls == [] and stats["round_page_attempts"] == 4


def test_resume_with_shifted_boundary_restarts_from_head(scan):
    _, run = scan
    pages = {i: articles(2*i, 2*i+1) for i in range(1, 8)}
    run(pages, cap=5, round_id="same", deadline=0)
    pages[1] = articles(100, 101)
    calls, stats = run(pages, cap=5, round_id="same")
    assert calls == [1, 1, 2, 3]
    assert stats["round_page_attempts"] == 5 and not stats["coverage_complete"]


def test_shared_prefix_alone_cannot_verify_resume(scan):
    _, run = scan
    run({1: articles(1, 2, 3, 4, 5, 6)}, cap=3, round_id="fixed", deadline=0)
    calls, stats = run({1: articles(1, 2, 3, 7, 8, 9)}, cap=3, round_id="fixed")
    assert calls == [1, 1] and stats["boundary_restarts"] == 1
    assert not stats["coverage_complete"]
