from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.public_access_policy import DEFAULT_PUBLIC_ACCESS, PublicAccessPolicy


class PublicAccessPolicyTest(unittest.TestCase):
    def test_defaults_are_safe_and_updates_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".public_access_settings.json"
            policy = PublicAccessPolicy(path)
            self.assertEqual(policy.load(), DEFAULT_PUBLIC_ACCESS)

            saved = policy.save({"deleted_records": True, "body_search": False})
            self.assertFalse(saved["body_search"])
            self.assertTrue(saved["deleted_records"])
            self.assertEqual(
                PublicAccessPolicy(path).load(),
                {
                    **DEFAULT_PUBLIC_ACCESS,
                    "body_search": False,
                    "deleted_records": True,
                },
            )

    def test_invalid_policy_update_is_rejected_without_changing_saved_values(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = PublicAccessPolicy(Path(directory) / "policy.json")
            saved = policy.save({"comments_view": False})
            with self.assertRaises(ValueError):
                policy.save({"comments_view": "false"})
            with self.assertRaises(ValueError):
                policy.save({"show_real_user_ids": True})
            self.assertEqual(policy.load(), saved)


if __name__ == "__main__":
    unittest.main()
