from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from crawler.automatic_quota import AutomaticQuota, AutomaticQuotaError
from crawler.cookie_pool import CookiePoolClient, load_cookie_pool_specs
from jobs import scheduler
from storage.post_writer import SQLitePostStore


class FakeLaneClient:
    def __init__(self, lane_id: str, responses=None):
        self.lane_id = lane_id
        self.responses = list(responses or [])
        self.request_count = 0
        self.paths: list[str] = []

    def get(self, path, params=None):
        self.paths.append(path)
        if self.responses:
            data, error = self.responses.pop(0)
        else:
            data, error = {"lane": self.lane_id}, None
        if not error:
            self.request_count += 1
        return data, error


class CookiePoolTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name, value in (("small", "small-cookie"), ("main", "main-cookie")):
            (self.root / f"{name}.txt").write_text(
                f"ys7_ysxy_session={value}\n",
                encoding="utf-8",
            )
        self.pool_path = self.root / "pool.json"
        self.pool_path.write_text(
            json.dumps(
                {
                    "lanes": [
                        {
                            "id": "small",
                            "config": "small.txt",
                            "daily_budgets": {"detail": 2},
                        },
                        {
                            "id": "main",
                            "config": "main.txt",
                            "daily_budgets": {"detail": 2},
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_pool_resolves_relative_config_and_routes_one_request_at_a_time(self):
        specs = load_cookie_pool_specs(self.pool_path)
        created: dict[str, FakeLaneClient] = {}

        def factory(cookie, lane_id):
            client = FakeLaneClient(lane_id)
            created[lane_id] = client
            return client

        with patch("crawler.client.load_cookie", side_effect=lambda path: "unused"):
            pool = CookiePoolClient(specs, client_factory=factory)
            results = [pool.article(str(index)) for index in range(4)]

        self.assertEqual([error for _, error in results], [None] * 4)
        self.assertEqual(pool.request_count, 4)
        self.assertEqual(sum(pool.lane_request_counts.values()), 4)
        self.assertEqual(set(created), {"small", "main"})
        self.assertEqual(
            sorted(pool.lane_request_counts.values()),
            [2, 2],
        )

    def test_pool_fails_over_only_local_quota_exhaustion(self):
        specs = load_cookie_pool_specs(self.pool_path)
        created: dict[str, FakeLaneClient] = {}

        def factory(cookie, lane_id):
            responses = (
                [({"ok": True}, "source_quota_budget_exhausted")]
                if lane_id == "small"
                else []
            )
            client = FakeLaneClient(lane_id, responses)
            created[lane_id] = client
            return client

        with patch("crawler.client.load_cookie", side_effect=lambda path: "unused"):
            pool = CookiePoolClient(specs, client_factory=factory)
            data, error = pool.article("1")

        self.assertEqual((data, error), ({"lane": "main"}, None))
        self.assertEqual(pool.last_lane_id, "main")
        self.assertEqual(created["small"].request_count, 0)
        self.assertEqual(created["main"].request_count, 1)

    def test_rate_limit_is_not_hidden_by_switching_lanes(self):
        specs = load_cookie_pool_specs(self.pool_path)
        created: dict[str, FakeLaneClient] = {}

        def factory(cookie, lane_id):
            responses = (
                [({"ok": False}, "rate_limited:pause")]
                if lane_id == "small"
                else [({"ok": True}, None)]
            )
            client = FakeLaneClient(lane_id, responses)
            created[lane_id] = client
            return client

        with patch("crawler.client.load_cookie", side_effect=lambda path: "unused"):
            pool = CookiePoolClient(specs, client_factory=factory)
            data, error = pool.article("1")

        self.assertIsNone(data)
        self.assertEqual(error, "rate_limited:pause")
        self.assertEqual(set(created), {"small"})

    def test_expired_session_is_not_hidden_by_switching_lanes(self):
        specs = load_cookie_pool_specs(self.pool_path)
        created: dict[str, FakeLaneClient] = {}

        def factory(cookie, lane_id):
            responses = (
                [({"ok": False}, "cookie_expired")]
                if lane_id == "small"
                else [(None, None)]
            )
            client = FakeLaneClient(lane_id, responses)
            created[lane_id] = client
            return client

        with patch("crawler.client.load_cookie", side_effect=lambda path: "unused"):
            pool = CookiePoolClient(specs, client_factory=factory)
            data, error = pool.article("1")

        self.assertIsNone(data)
        self.assertEqual(error, "cookie_expired")
        self.assertEqual(set(created), {"small"})


class CookieLaneQuotaTest(unittest.TestCase):
    def test_each_lane_has_an_independent_hard_daily_counter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("small", "main"):
                (root / f"{name}.txt").write_text(
                    f"ys7_ysxy_session={name}\n",
                    encoding="utf-8",
                )
            pool_path = root / "pool.json"
            pool_path.write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "id": "small",
                                "config": "small.txt",
                                "daily_budgets": {"detail": 2},
                            },
                            {
                                "id": "main",
                                "config": "main.txt",
                                "daily_budgets": {"detail": 2},
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            quota_path = root / "quota.json"
            history_path = root / "history.jsonl"
            now = datetime(
                2026,
                8,
                12,
                23,
                59,
                tzinfo=timezone(timedelta(hours=8)),
            )
            with (
                patch.object(scheduler, "COOKIE_POOL_PATH", str(pool_path)),
                patch.object(scheduler, "QUOTA_PATH", quota_path),
                patch.object(scheduler, "QUOTA_HISTORY_PATH", history_path),
                patch.object(scheduler, "beijing_now", return_value=now),
            ):
                small = AutomaticQuota("detail", lane_id="small")
                main = AutomaticQuota("detail", lane_id="main")
                small.claim()
                small.claim()
                main.claim()
                main.claim()
                with self.assertRaises(AutomaticQuotaError):
                    small.claim()
                quota = json.loads(quota_path.read_text(encoding="utf-8"))

        self.assertEqual(quota["detail_calls"], 4)
        self.assertEqual(quota["cookie_lanes"]["small"]["detail_calls"], 2)
        self.assertEqual(quota["cookie_lanes"]["main"]["detail_calls"], 2)


class QueueClaimTest(unittest.TestCase):
    def test_two_claimers_receive_disjoint_rows_and_expired_claims_recover(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "posts.db"
            with SQLitePostStore(db_path) as store:
                store.init_schema()
                for post_id in ("1", "2"):
                    store.enqueue_crawler_candidate(
                        post_id=post_id,
                        source="lists",
                        priority=10,
                        list_create_time="2026-08-12 10:00:00",
                        list_update_time="2026-08-12 10:00:00",
                        list_comment_count=1,
                        db_comment_count=None,
                        reason="new_post",
                    )
                self.assertTrue(
                    store.claim_crawler_queue_item(
                        "1",
                        owner="worker-a",
                        lane_id="small",
                    )
                )
                self.assertFalse(
                    store.claim_crawler_queue_item(
                        "1",
                        owner="worker-b",
                        lane_id="main",
                    )
                )
                self.assertTrue(
                    store.claim_crawler_queue_item(
                        "2",
                        owner="worker-b",
                        lane_id="main",
                    )
                )
                rows = store.conn.execute(
                    "select post_id,status,last_lane_id from crawler_queue order by post_id"
                ).fetchall()
                self.assertEqual(
                    [tuple(row) for row in rows],
                    [("1", "in_progress", "small"), ("2", "in_progress", "main")],
                )
                recovered = store.recover_expired_crawler_queue_claims(
                    now="9999-01-01 00:00:00"
                )
                self.assertEqual(recovered, 2)
                self.assertEqual(
                    store.conn.execute(
                        "select count(*) from crawler_queue where status='pending'"
                    ).fetchone()[0],
                    2,
                )


if __name__ == "__main__":
    unittest.main()
