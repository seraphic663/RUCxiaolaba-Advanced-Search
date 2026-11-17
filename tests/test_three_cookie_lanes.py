"""Three fixed sessions share scheduling but retain independent limits and pauses."""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from crawler.automatic_quota import (
    AUTOMATIC_QUOTA_DATE_ENV,
    AUTOMATIC_QUOTA_KIND_ENV,
    AutomaticQuota,
    AutomaticQuotaError,
)
from crawler.client import MiniProgramClient
from crawler.config import default_cookie_config
from crawler.cookie_pool import load_cookie_pool_specs
from jobs import scheduler


@pytest.fixture
def three_lanes(tmp_path, monkeypatch):
    example = Path(__file__).resolve().parents[1] / "data" / "cookie_pool.example.json"
    pool = json.loads(example.read_text(encoding="utf-8"))
    for lane in pool["lanes"]:
        config = tmp_path / lane["config"]
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(f"ys_ysxy_sess=test-{lane['id']}\n", encoding="utf-8")
    pool_path = tmp_path / "cookie_pool.json"
    pool_path.write_text(json.dumps(pool), encoding="utf-8")
    monkeypatch.setattr(scheduler, "COOKIE_POOL_PATH", str(pool_path))
    monkeypatch.setattr(scheduler, "COOKIE_POOL_LANE", "")
    monkeypatch.setattr(scheduler, "PARALLEL_LANES_ENABLED", False)
    monkeypatch.setattr(scheduler, "QUOTA_PATH", tmp_path / "quota.json")
    monkeypatch.setattr(scheduler, "QUOTA_HISTORY_PATH", tmp_path / "history.jsonl")
    monkeypatch.setattr(scheduler, "PAUSE_PATH", tmp_path / "pause.json")
    at = datetime.fromisoformat("2026-11-17T01:00:00+08:00")
    monkeypatch.setattr(scheduler, "beijing_now", lambda: at)
    monkeypatch.setattr(scheduler, "now_wall", lambda: at.timestamp())
    return pool_path


def test_numbered_default_and_legacy_fallback(tmp_path):
    canonical = tmp_path / "cookies" / "cookie_01.txt"
    legacy = tmp_path / "config.txt"
    assert default_cookie_config(tmp_path) == canonical
    legacy.write_text("legacy", encoding="utf-8")
    assert default_cookie_config(tmp_path) == legacy
    canonical.parent.mkdir()
    canonical.write_text("canonical", encoding="utf-8")
    assert default_cookie_config(tmp_path) == canonical


def test_third_lane_shares_detail_windows_and_release_profile(three_lanes):
    specs = load_cookie_pool_specs(three_lanes)
    assert {spec.lane_id for spec in specs} == {"new", "old", "third"}
    assert len({spec.config_path for spec in specs}) == 3
    assert scheduler.cookie_pool_budget("detail") == 1500
    assert scheduler.detail_quota_release_steps("third") == scheduler.detail_quota_release_steps("new")
    for lane in ("new", "old", "third"):
        assert scheduler.remaining_budget("detail", scheduler.load_quota(), lane_id=lane) == 400


def test_all_three_lanes_are_selected_by_actual_usage(three_lanes, monkeypatch):
    quota = scheduler.load_quota()
    monkeypatch.setattr(scheduler, "load_quota", lambda: quota)
    for job, key in (("trickle_fill", "detail_calls"), ("discover_new", "new_list_calls")):
        selected = []
        for _ in range(3):
            lane = scheduler.job_lane_id(job)
            selected.append(lane)
            quota["cookie_lanes"][lane][key] = 25
        assert selected == ["new", "old", "third"]


def test_three_lanes_stop_at_400_then_500_without_resetting_existing_counts(
    three_lanes, monkeypatch,
):
    quota = scheduler.load_quota()
    lanes = ("new", "old", "third")
    for clock, prior, target in (("01:00", 399, 400), ("21:00", 499, 500)):
        at = datetime.fromisoformat(f"2026-11-17T{clock}:00+08:00")
        monkeypatch.setattr(scheduler, "beijing_now", lambda: at)
        quota["detail_calls"] = prior * 3
        for lane in lanes:
            quota["cookie_lanes"][lane]["detail_calls"] = prior
        scheduler.save_quota(quota)
        for lane in lanes:
            result = AutomaticQuota("detail", lane_id=lane).claim()
            assert result["lane_used"] == target
            with pytest.raises(AutomaticQuotaError, match="exhausted"):
                AutomaticQuota("detail", lane_id=lane).claim()
        quota = scheduler.load_quota()
        assert quota["detail_calls"] == target * 3
        assert all(quota["cookie_lanes"][lane]["detail_calls"] == target for lane in lanes)


def test_third_lane_pause_does_not_consume_or_reset_other_lanes(three_lanes):
    scheduler.save_pause(
        reason="cookie_expired", job="trickle_fill", seconds=3600,
        detail="test", lane_id="third",
    )
    with pytest.raises(AutomaticQuotaError, match="paused"):
        AutomaticQuota("detail", lane_id="third").claim()
    for lane in ("new", "old"):
        assert AutomaticQuota("detail", lane_id=lane).claim()["lane_used"] == 1
    quota = scheduler.load_quota()
    assert quota["detail_calls"] == 2
    assert quota["cookie_lanes"]["third"]["detail_calls"] == 0


def test_window_planning_respects_lane_hard_cap(three_lanes, monkeypatch):
    pool = json.loads(three_lanes.read_text(encoding="utf-8"))
    pool["lanes"][2]["daily_budgets"]["detail"] = 5
    three_lanes.write_text(json.dumps(pool), encoding="utf-8")
    monkeypatch.setattr(scheduler, "COOKIE_POOL_LANE", "third")
    args, _ = scheduler.prepare_job("trickle_fill")
    assert int(args[args.index("--limit") + 1]) == 5


@pytest.mark.parametrize("clock,target", [
    ("00:00:00", 0), ("00:59:59", 0), ("01:00:00", 400),
    ("20:59:59", 400), ("21:00:00", 500), ("23:59:59", 500),
])
def test_fixed_window_boundaries_never_use_legacy_midnight_release(
    three_lanes, monkeypatch, clock, target,
):
    at = datetime.fromisoformat(f"2026-11-17T{clock}+08:00")
    monkeypatch.setattr(scheduler, "beijing_now", lambda: at)
    quota = scheduler.load_quota()
    for lane in ("new", "old", "third"):
        assert scheduler.remaining_budget("detail", quota, lane_id=lane) == target
    assert scheduler.remaining_budget("detail", quota) == target * 3


def test_today_keeps_300_used_and_only_releases_remaining_100(
    three_lanes, monkeypatch,
):
    at = datetime.fromisoformat("2026-11-17T09:30:00+08:00")
    monkeypatch.setattr(scheduler, "beijing_now", lambda: at)
    quota = scheduler.load_quota()
    quota["detail_calls"] = 900
    for lane in ("new", "old", "third"):
        quota["cookie_lanes"][lane]["detail_calls"] = 300
    scheduler.save_quota(quota)
    for lane in ("new", "old", "third"):
        assert scheduler.remaining_budget("detail", scheduler.load_quota(), lane_id=lane) == 100
        monkeypatch.setattr(scheduler, "COOKIE_POOL_LANE", lane)
        args, _ = scheduler.prepare_job("trickle_fill")
        assert int(args[args.index("--limit") + 1]) == 25
        assert AutomaticQuota("detail", lane_id=lane).claim()["lane_used"] == 301
    quota = scheduler.load_quota()
    assert quota["detail_calls"] == 903
    assert all(lane["detail_calls"] == 301 for lane in quota["cookie_lanes"].values())
    assert not scheduler.QUOTA_HISTORY_PATH.exists()


@pytest.mark.parametrize("lane", ["new", "old", "third"])
def test_old_task_cannot_send_http_after_midnight_or_next_window(
    three_lanes, monkeypatch, lane,
):
    at = datetime.fromisoformat("2026-11-17T23:59:59+08:00")
    monkeypatch.setattr(scheduler, "beijing_now", lambda: at)
    quota = scheduler.load_quota()
    quota["detail_calls"] = 1497
    for item in quota["cookie_lanes"].values():
        item["detail_calls"] = 499
    scheduler.save_quota(quota)
    monkeypatch.setenv(AUTOMATIC_QUOTA_KIND_ENV, "detail")
    monkeypatch.setenv(AUTOMATIC_QUOTA_DATE_ENV, "2026-11-17")
    old = MiniProgramClient("test-cookie", lane_id=lane)
    response = Mock()
    response.json.return_value = {"code": "0000", "data": {"id": "test"}}
    old.session.get = Mock(return_value=response)
    assert old.article("test")[1] is None
    assert scheduler.load_quota()["cookie_lanes"][lane]["detail_calls"] == 500

    at = datetime.fromisoformat("2026-11-18T00:00:00+08:00")
    old.session.get.reset_mock()
    assert old.article("test")[1] == "source_quota_day_changed"
    old.session.get.assert_not_called()
    assert scheduler.load_quota()["detail_calls"] == 0
    history = [json.loads(line) for line in scheduler.QUOTA_HISTORY_PATH.read_text().splitlines()]
    assert history[-1]["date"] == "2026-11-17"
    assert history[-1]["detail_calls"] == 1498

    monkeypatch.setenv(AUTOMATIC_QUOTA_DATE_ENV, "2026-11-18")
    fresh = MiniProgramClient("test-cookie", lane_id=lane)
    fresh.session.get = Mock(return_value=response)
    assert fresh.article("test")[1] == "source_quota_window_locked"
    fresh.session.get.assert_not_called()

    at = datetime.fromisoformat("2026-11-18T01:00:00+08:00")
    assert old.article("test")[1] == "source_quota_day_changed"
    old.session.get.assert_not_called()
    assert fresh.article("test")[1] is None
    assert fresh.request_count == 1
    assert scheduler.load_quota()["detail_calls"] == 1


def test_child_delayed_until_next_day_cannot_make_its_first_request(
    three_lanes, monkeypatch,
):
    at = datetime.fromisoformat("2026-11-18T01:00:00+08:00")
    monkeypatch.setattr(scheduler, "beijing_now", lambda: at)
    monkeypatch.setenv(AUTOMATIC_QUOTA_KIND_ENV, "detail")
    monkeypatch.setenv(AUTOMATIC_QUOTA_DATE_ENV, "2026-11-17")
    client = MiniProgramClient("test-cookie", lane_id="third")
    client.session.get = Mock()
    assert client.article("test")[1] == "source_quota_day_changed"
    client.session.get.assert_not_called()
    assert client.request_count == 0
    assert scheduler.load_quota()["detail_calls"] == 0
