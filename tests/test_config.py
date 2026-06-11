"""Application path selection tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config


class BigramConfigTest(unittest.TestCase):
    def test_local_default_is_auto_detected_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bigram_index.db"
            path.touch()
            with (
                patch.object(config, "DEFAULT_BIGRAM_DB", path),
                patch.dict(os.environ, {}, clear=True),
            ):
                self.assertEqual(config.choose_bigram_db(), path)

    def test_missing_local_default_falls_back_to_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bigram_index.db"
            with (
                patch.object(config, "DEFAULT_BIGRAM_DB", path),
                patch.dict(os.environ, {}, clear=True),
            ):
                self.assertIsNone(config.choose_bigram_db())

    def test_environment_overrides_local_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "bigram_index.db"
            remote = Path(tmp) / "railway.db"
            local.touch()
            with (
                patch.object(config, "DEFAULT_BIGRAM_DB", local),
                patch.dict(os.environ, {"BIGRAM_DB": str(remote)}, clear=True),
            ):
                self.assertEqual(config.choose_bigram_db(), remote)


if __name__ == "__main__":
    unittest.main()
