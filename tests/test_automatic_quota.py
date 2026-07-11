import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from crawler.automatic_quota import AutomaticQuota, AutomaticQuotaError
from crawler.client import MiniProgramClient
from jobs import scheduler


class AutomaticQuotaTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.quota_path = root / ".crawler_quota.json"
        self.history_path = root / ".crawler_quota_history.jsonl"
        self.pause_path = root / ".crawler_pause.json"
        self.now = datetime(
            2026,
            7,
            11,
            23,
            59,
            tzinfo=timezone(timedelta(hours=8)),
        )
        self.patches = [
            patch.object(scheduler, "QUOTA_PATH", self.quota_path),
            patch.object(scheduler, "QUOTA_HISTORY_PATH", self.history_path),
            patch.object(scheduler, "PAUSE_PATH", self.pause_path),
            patch.object(scheduler, "beijing_now", side_effect=lambda: self.now),
            patch.object(scheduler, "configured_source_budget", return_value=690),
            patch.object(scheduler, "configured_admin_budget", return_value=30),
            patch.object(scheduler, "adaptive_source_budget", return_value=690),
            patch.object(scheduler, "adaptive_scale", return_value=1.0),
            patch.object(scheduler, "daily_budget", return_value=3),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def _quota(self):
        return json.loads(self.quota_path.read_text(encoding="utf-8"))

    def test_claim_counts_only_each_actual_request(self):
        quota = AutomaticQuota("detail")
        quota.claim()
        quota.claim()
        self.assertEqual(self._quota()["detail_calls"], 2)

        # A restarted worker continues from the persisted actual count. There
        # is no batch reservation to lose when a deployment interrupts it.
        AutomaticQuota("detail").claim()
        self.assertEqual(self._quota()["detail_calls"], 3)
        with self.assertRaises(AutomaticQuotaError) as caught:
            quota.claim()
        self.assertEqual(caught.exception.code, "source_quota_budget_exhausted")
        self.assertEqual(self._quota()["detail_calls"], 3)

    def test_concurrent_claims_never_exceed_released_budget(self):
        with patch.object(scheduler, "daily_budget", return_value=7):

            def claim_once(_index):
                try:
                    AutomaticQuota("detail").claim()
                    return True
                except AutomaticQuotaError:
                    return False

            with ThreadPoolExecutor(max_workers=10) as pool:
                outcomes = list(pool.map(claim_once, range(20)))
        self.assertEqual(sum(outcomes), 7)
        self.assertEqual(self._quota()["detail_calls"], 7)

    def test_midnight_rollover_does_not_consume_locked_new_day(self):
        quota = AutomaticQuota("detail")
        quota.claim()
        self.assertEqual(self._quota()["date"], "2026-07-11")

        self.now = self.now + timedelta(minutes=2)
        with self.assertRaises(AutomaticQuotaError) as caught:
            quota.claim()
        self.assertEqual(caught.exception.code, "source_quota_window_locked")
        current = self._quota()
        self.assertEqual(current["date"], "2026-07-12")
        self.assertEqual(current["detail_calls"], 0)
        history = self.history_path.read_text(encoding="utf-8")
        self.assertIn('"reason": "day_rollover"', history)

    def test_prepare_job_only_crops_limit_without_reserving(self):
        self.quota_path.write_text(
            json.dumps(
                {
                    "date": "2026-07-11",
                    "new_list_calls": 1,
                    "active_list_calls": 0,
                    "detail_calls": 0,
                    "probe_calls": 0,
                }
            ),
            encoding="utf-8",
        )
        with patch.object(
            scheduler,
            "job_args",
            return_value=["discover-latest", "--max-pages", "7"],
        ):
            args, note = scheduler.prepare_job("discover_new")
        self.assertEqual(args[-1], "2")
        self.assertIn("planned_max=2", note)
        self.assertEqual(self._quota()["new_list_calls"], 1)

    def test_client_does_not_send_http_when_local_quota_rejects(self):
        client = MiniProgramClient("cookie")
        client.automatic_quota = Mock()
        client.automatic_quota.claim.side_effect = AutomaticQuotaError(
            "source_quota_window_locked",
            "locked",
        )
        client.session.get = Mock()

        data, error = client.list_page("lists", 1)

        self.assertIsNone(data)
        self.assertEqual(error, "source_quota_window_locked")
        self.assertEqual(client.request_count, 0)
        client.session.get.assert_not_called()

    def test_client_counts_attempted_http_after_claim(self):
        client = MiniProgramClient("cookie")
        client.automatic_quota = Mock()
        response = Mock()
        response.json.return_value = {"code": "0000", "data": {"list": []}}
        client.session.get = Mock(return_value=response)

        data, error = client.list_page("lists", 1)

        self.assertEqual(data, {"list": []})
        self.assertIsNone(error)
        self.assertEqual(client.request_count, 1)
        client.automatic_quota.claim.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
