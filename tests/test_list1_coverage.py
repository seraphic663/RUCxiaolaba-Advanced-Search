import json
from datetime import datetime, timedelta

import pytest

from crawler.id_ledger import ledger_state, record_list_page
from crawler.list_coverage import List1Coverage
from crawler.service import CrawlerService
from storage.post_writer import SQLitePostStore


def articles(first, size=3):
    return [
        {
            "id": str(i),
            "create_time": str(datetime(2026, 11, 20) + timedelta(minutes=i)),
            "detail": "test",
        }
        for i in range(first, first - size, -1)
    ]


class Feed:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def list_page(self, endpoint, page):
        self.requests.append(page)
        result = self.pages.get(page, [])
        return (None, result) if isinstance(result, str) else ({"list": result}, None)


def scan(db, pages, limit=6, monitor=True):
    feed = Feed(pages)
    service = CrawlerService(db_path=db, cookie="test", lock_timeout=2)
    service.client = lambda: feed
    stats = service.discover_queue(
        command="discover-latest", endpoint="lists", since="2026-11-20 00:00:00",
        max_pages=limit, min_pages=1, no_action_page_threshold=1,
        list1_coverage_enabled=monitor,
        old_page_threshold=1, min_delay=0, max_delay=0,
    )
    return stats, feed.requests


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "posts.db"
    with SQLitePostStore(path) as store:
        store.init_schema()
        # An active-list observation makes every row familiar to the ledger.
        # It gives no evidence that two consecutive list1 scans overlap.
        record_list_page(store.conn, run_id="active", endpoint="lists2", page=1,
                         articles=articles(220, 140), baseline=True)
        store.conn.commit()
    return path


@pytest.mark.parametrize("error", ["network down", "source_quota_day_changed"])
def test_restart_joins_partial_scan_and_older_gap_without_new_ids(db, error):
    scan(db, {1: articles(106)}, limit=1)
    partial, requests = scan(db, {1: articles(112), 2: error, 3: articles(106)})
    assert requests == [1, 2]
    assert partial["list1_open_gaps"] == 1

    stats, requests = scan(db, {1: articles(115), 2: articles(112),
                                3: articles(109), 4: articles(106)})
    assert requests == [1, 2, 3, 4]
    assert stats["list1_overlap"] and stats["list1_open_gaps"] == 0
    assert stats["list1_current_head"] == articles(115)[0]["create_time"]


def test_beyond_cap_gap_stays_visible_while_recent_head_advances(db):
    scan(db, {1: articles(100)}, limit=1)
    pages = {i: articles(203 - 3 * i) for i in range(1, 7)}
    first, requests = scan(db, pages)
    assert requests == list(range(1, 7))
    assert first["list1_open_gaps"] == 1
    pages = {i: articles(206 - 3 * i) for i in range(1, 7)}
    second, requests = scan(db, pages)
    assert requests == list(range(1, 7))
    assert second["list1_gaps_beyond_page_limit"] == 1

    third, requests = scan(db, {1: articles(204)})
    assert requests == [1]
    assert third["list1_open_gaps"] == 1
    assert third["list1_current_head"] == articles(204)[0]["create_time"]


def test_rate_limit_preserves_committed_gap_and_previous_anchor(db):
    scan(db, {1: articles(106)}, limit=1)
    with pytest.raises(RuntimeError, match="rate_limited"):
        scan(db, {1: articles(116), 2: "rate_limited:source"})
    with SQLitePostStore(db) as store:
        state = json.loads(ledger_state(store.conn, List1Coverage.key))
    assert "116" in state["head"]["witnesses"]
    assert "106" in state["gaps"][0]["older"]["witnesses"]


def test_one_recurring_id_cannot_join_the_previous_head(db):
    scan(db, {1: articles(106)}, limit=1)
    stats, requests = scan(db, {1: articles(116) + articles(106, 1)}, limit=1)
    assert requests == [1] and not stats["list1_overlap"]
    assert stats["list1_open_gaps"] == 1


def test_joined_first_page_stops_even_when_it_also_discovers_new_ids(db):
    scan(db, {1: articles(106)}, limit=1)
    stats, requests = scan(db, {1: articles(250, 1) + articles(106)})
    assert requests == [1]
    assert stats["ledger_new_ids"] == 1
    assert stats["list1_overlap_stop"] and stats["list1_open_gaps"] == 0


def test_manual_discovery_does_not_change_monitor_anchor(db):
    scan(db, {1: articles(106)}, limit=1)
    manual, _ = scan(db, {1: articles(116)}, limit=1, monitor=False)
    assert "list1_current_head" not in manual
    with SQLitePostStore(db) as store:
        state = json.loads(ledger_state(store.conn, List1Coverage.key))
    assert "106" in state["head"]["witnesses"]


def test_reused_ids_with_changed_creation_times_cannot_prove_overlap(db):
    scan(db, {1: articles(106)}, limit=1)
    changed = articles(106)
    for row, other in zip(changed, articles(120)):
        row["create_time"] = other["create_time"]
    stats, _ = scan(db, {1: changed}, limit=1)
    assert not stats["list1_overlap"]
    assert stats["list1_open_gaps"] == 1


def test_undated_page_stores_ids_but_cannot_bridge_gap(db):
    scan(db, {1: articles(106)}, limit=1)
    scan(db, {1: articles(116)}, limit=1)
    incomplete = articles(113)
    del incomplete[1]["create_time"]
    stats, requests = scan(db, {1: articles(119), 2: incomplete, 3: articles(106)})
    assert requests == [1, 2]
    assert stats["list1_invalid_time"] and stats["errors"] == 1
    assert stats["list1_open_gaps"] > 0
