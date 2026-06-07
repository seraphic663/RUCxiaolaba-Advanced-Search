import re
import tempfile
import unittest
from pathlib import Path

from storage.ai_store import AIStore


class AIStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AIStore(Path(self.tmp.name) / "ai.db")
        self.store.init_schema()

    def tearDown(self):
        self.tmp.cleanup()

    def test_generated_code_has_64_bits_and_activates(self):
        code = self.store.generate_codes(count=1, daily_quota=2)[0]
        self.assertRegex(
            code,
            re.compile(r"^XLB-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}$"),
        )

        ok, session = self.store.activate(code)
        self.assertTrue(ok)
        self.assertEqual(self.store.validate_session(session), self.store.hash_code(code))

    def test_disabling_code_invalidates_existing_session(self):
        code = self.store.generate_codes(count=1)[0]
        ok, session = self.store.activate(code)
        self.assertTrue(ok)

        prefix = self.store.hash_code(code)[:16]
        self.assertTrue(self.store.set_active(prefix, False))
        self.assertIsNone(self.store.validate_session(session))

    def test_daily_and_total_quota_are_enforced_and_releasable(self):
        code = self.store.generate_codes(
            count=1, daily_quota=3, max_quota=2
        )[0]
        code_hash = self.store.hash_code(code)

        self.assertEqual(self.store.reserve_quota(code_hash), (True, 1))
        self.assertEqual(self.store.reserve_quota(code_hash), (True, 2))
        self.assertEqual(self.store.reserve_quota(code_hash), (False, "quota_exceeded"))
        self.assertEqual(self.store.get_status(code_hash)["remaining"], 0)

        self.store.release_quota(code_hash)
        self.assertEqual(self.store.get_status(code_hash)["remaining"], 1)
        self.assertEqual(self.store.reserve_quota(code_hash), (True, 2))

    def test_management_rejects_ambiguous_short_prefix(self):
        self.store.generate_codes(count=2)
        self.assertFalse(self.store.set_active("a", False))


if __name__ == "__main__":
    unittest.main()
