"""Runtime configuration tests."""

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from zme_dingtalk_robot.config import Settings


class SettingsTest(unittest.TestCase):
    def test_loads_codex_gateway_defaults(self) -> None:
        env = {
            "DINGTALK_CLIENT_ID": "client-id",
            "DINGTALK_CLIENT_SECRET": "client-secret",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()

        self.assertEqual(
            Path("data/conversations.db"),
            settings.conversation_database_path,
        )
        self.assertEqual(Path("data/codex-workspace"), settings.codex_workspace)
        self.assertEqual(600.0, settings.codex_timeout_seconds)
        self.assertEqual(2, settings.codex_max_concurrency)
        self.assertEqual(10, settings.max_active_threads)
        self.assertEqual(300.0, settings.conversation_idle_timeout_seconds)
        self.assertEqual(7.0, settings.message_dedup_retention_days)
        self.assertEqual(3_600.0, settings.sqlite_cleanup_interval_seconds)
        self.assertEqual(4_000, settings.max_reply_characters)

    def test_requires_only_dingtalk_credentials(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DINGTALK_CLIENT_ID"):
                Settings.from_env()

    def test_rejects_invalid_positive_values(self) -> None:
        env = {
            "DINGTALK_CLIENT_ID": "client-id",
            "DINGTALK_CLIENT_SECRET": "client-secret",
            "CODEX_MAX_CONCURRENCY": "0",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "CODEX_MAX_CONCURRENCY"):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()
