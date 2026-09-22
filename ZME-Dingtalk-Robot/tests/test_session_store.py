"""SQLite conversation session tests."""

from pathlib import Path
import tempfile
import unittest

from zme_dingtalk_robot.agent.session_store import SessionStore


class SessionStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "conversations.db"
        self.store = SessionStore(self.database_path)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_persists_and_restores_thread_mapping(self) -> None:
        self.store.save_session("conversation-1", "thread-1", 100.0)

        restored = SessionStore(self.database_path).get_session("conversation-1")

        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual("thread-1", restored.thread_id)
        self.assertEqual("conversation-1", restored.session_key)
        self.assertEqual(100.0, restored.last_active_at)

    def test_lists_sessions_oldest_first(self) -> None:
        self.store.save_session("session-new", "thread-new", 200.0)
        self.store.save_session("session-old", "thread-old", 100.0)

        sessions = self.store.list_sessions_oldest_first()

        self.assertEqual(
            ["session-old", "session-new"],
            [session.session_key for session in sessions],
        )

    def test_claims_message_id_only_once(self) -> None:
        first = self.store.claim_message("message-1", "conversation-1", 100.0)
        duplicate = self.store.claim_message("message-1", "conversation-1", 101.0)

        self.assertTrue(first)
        self.assertFalse(duplicate)

    def test_clear_returns_previous_thread(self) -> None:
        self.store.save_session("conversation-1", "thread-1", 100.0)

        thread_id = self.store.clear_session("conversation-1")

        self.assertEqual("thread-1", thread_id)
        self.assertIsNone(self.store.get_session("conversation-1"))


if __name__ == "__main__":
    unittest.main()
