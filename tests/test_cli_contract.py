import unittest

from crawler.cli import build_parser
from jobs.scheduler import classify_error, job_args


class CLIContractTest(unittest.TestCase):
    def test_new_and_canonical_latest_commands_match(self):
        parser = build_parser()
        old = parser.parse_args(["new"])
        new = parser.parse_args(["sync-latest"])
        self.assertEqual(old.endpoint, new.endpoint)
        self.assertIs(old.func, new.func)

    def test_phase1_and_scan_id_range_are_both_supported(self):
        parser = build_parser()
        old = parser.parse_args(["phase1", "--start-id", "1", "--end-id", "2"])
        new = parser.parse_args(
            ["scan-id-range", "--start-id", "1", "--end-id", "2"]
        )
        self.assertIs(old.func, new.func)
        self.assertEqual((old.start_id, old.end_id), (1, 2))

    def test_scheduler_uses_valid_canonical_commands(self):
        parser = build_parser()
        for job_name in (
            "new",
            "refresh",
            "backfill",
            "phase1",
            "discover_new",
            "discover_active",
            "trickle_fill",
        ):
            parsed = parser.parse_args(
                [
                    *job_args(job_name),
                    "--db-path",
                    "data/posts.db",
                    "--config",
                    "data/config.txt",
                ]
            )
            self.assertTrue(callable(parsed.func))

    def test_scheduler_classifies_crawler_fuses(self):
        self.assertEqual(
            classify_error("[crawler] error: rate_limited:今天刷的太久了"),
            "rate_limited",
        )
        self.assertEqual(
            classify_error("[crawler] error: cookie_expired"),
            "cookie_expired",
        )
        self.assertEqual(classify_error("[crawler] error: not_found"), "")


if __name__ == "__main__":
    unittest.main()
