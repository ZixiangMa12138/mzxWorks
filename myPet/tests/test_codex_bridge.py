from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_bridge import CodexBridge, derive_pet_state  # noqa: E402


class DerivePetStateTests(unittest.TestCase):
    def test_waiting_has_highest_priority(self) -> None:
        threads = [
            {"status": {"type": "active"}},
            {"status": {"type": "active", "activeFlags": ["waitingOnApproval"]}},
        ]
        self.assertEqual(derive_pet_state(threads), "waiting")

    def test_active_maps_to_working(self) -> None:
        self.assertEqual(derive_pet_state([{"status": {"type": "active"}}]), "working")

    def test_error_maps_to_error(self) -> None:
        self.assertEqual(derive_pet_state([{"status": {"type": "systemError"}}]), "error")

    def test_unloaded_threads_are_idle(self) -> None:
        threads = [{"status": {"type": "notLoaded"}}, {"status": {"type": "idle"}}]
        self.assertEqual(derive_pet_state(threads), "idle")


class RolloutFallbackTests(unittest.TestCase):
    def test_task_lifecycle_maps_to_working_then_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory)
            log = sessions / "rollout.jsonl"
            log.write_text(
                '{"type":"event_msg","payload":{"type":"task_started"}}\n',
                encoding="utf-8",
            )
            bridge = CodexBridge(lambda _update: None, sessions / "manual.json")
            bridge._sessions_dir = sessions
            self.assertEqual(bridge._poll_rollout_log("no socket").state, "working")

            with log.open("a", encoding="utf-8") as handle:
                handle.write('{"type":"event_msg","payload":{"type":"task_complete"}}\n')
            self.assertEqual(bridge._poll_rollout_log("no socket").state, "success")


if __name__ == "__main__":
    unittest.main()
