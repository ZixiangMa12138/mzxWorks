"""Conversation orchestration tests using a fake Codex backend."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from zme_dingtalk_robot.agent.codex_backend import CodexTimeoutError
from zme_dingtalk_robot.agent.service import AgentService, _session_key
from zme_dingtalk_robot.agent.session_store import SessionStore
from zme_dingtalk_robot.models import MessageContext


def make_context(
    message_id: str,
    conversation_id: str = "conversation-1",
    text: str = "查询项目进度",
    sender_user_id: str = "user-1",
) -> MessageContext:
    return MessageContext(
        text=text,
        message_id=message_id,
        sender_user_id=sender_user_id,
        sender_nickname="测试用户",
        conversation_id=conversation_id,
        conversation_type="2",
        raw_message=None,
    )


class FakeCodexBackend:
    def __init__(self) -> None:
        self.next_thread = 1
        self.started: list[str] = []
        self.resumed: list[str] = []
        self.runs: list[tuple[str, str]] = []
        self.archived: list[str] = []
        self.resume_failures: set[str] = set()
        self.run_error: Exception | None = None
        self.run_delay = 0.0
        self.active = 0
        self.max_active = 0
        self.active_by_thread: dict[str, int] = {}
        self.max_active_by_thread: dict[str, int] = {}

    async def start_thread(self) -> str:
        thread_id = f"thread-{self.next_thread}"
        self.next_thread += 1
        self.started.append(thread_id)
        return thread_id

    async def resume_thread(self, thread_id: str) -> str:
        self.resumed.append(thread_id)
        if thread_id in self.resume_failures:
            raise RuntimeError("thread missing")
        return thread_id

    async def run(self, thread_id: str, message: str) -> str:
        self.runs.append((thread_id, message))
        user_message = json.loads(message)["user_message"]
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        current = self.active_by_thread.get(thread_id, 0) + 1
        self.active_by_thread[thread_id] = current
        self.max_active_by_thread[thread_id] = max(
            self.max_active_by_thread.get(thread_id, 0),
            current,
        )
        try:
            if self.run_delay:
                await asyncio.sleep(self.run_delay)
            if self.run_error is not None:
                raise self.run_error
            return f"Codex:{user_message}"
        finally:
            self.active -= 1
            self.active_by_thread[thread_id] -= 1

    async def archive_thread(self, thread_id: str) -> None:
        self.archived.append(thread_id)


class AgentServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self._temporary_directory.name) / "conversations.db"
        self.store = SessionStore(database_path)
        self.backend = FakeCodexBackend()
        self.now = 1_000.0
        self.service = AgentService(
            self.backend,
            self.store,
            clock=lambda: self.now,
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_same_conversation_reuses_thread(self) -> None:
        first = await self.service.handle(make_context("message-1"))
        second = await self.service.handle(make_context("message-2", text="继续"))

        self.assertEqual("Codex:查询项目进度", first)
        self.assertEqual("Codex:继续", second)
        self.assertEqual(["thread-1"], self.backend.started)
        self.assertEqual(["thread-1"], self.backend.resumed)
        self.assertEqual(
            ["查询项目进度", "继续"],
            [json.loads(message)["user_message"] for _, message in self.backend.runs],
        )

    async def test_users_in_same_group_have_independent_threads(self) -> None:
        await self.service.handle(
            make_context("message-1", text="创建待办", sender_user_id="user-a")
        )
        await self.service.handle(
            make_context("message-2", text="确认", sender_user_id="user-b")
        )

        self.assertEqual(["thread-1", "thread-2"], self.backend.started)
        first = json.loads(self.backend.runs[0][1])
        second = json.loads(self.backend.runs[1][1])
        self.assertEqual("user-a", first["dingtalk_metadata"]["sender_user_id"])
        self.assertEqual("user-b", second["dingtalk_metadata"]["sender_user_id"])

    async def test_different_conversations_do_not_share_thread(self) -> None:
        await self.service.handle(make_context("message-1", "conversation-1"))
        await self.service.handle(make_context("message-2", "conversation-2"))

        self.assertEqual(["thread-1", "thread-2"], self.backend.started)
        self.assertEqual("thread-1", self.backend.runs[0][0])
        self.assertEqual("thread-2", self.backend.runs[1][0])

    async def test_resume_failure_creates_new_thread(self) -> None:
        context = make_context("message-1")
        session_key = _session_key(context)
        self.store.save_session(session_key, "missing-thread", self.now)
        self.backend.resume_failures.add("missing-thread")

        response = await self.service.handle(context)

        self.assertEqual("Codex:查询项目进度", response)
        self.assertEqual(["missing-thread"], self.backend.resumed)
        self.assertEqual(["thread-1"], self.backend.started)
        restored = self.store.get_session(session_key)
        assert restored is not None
        self.assertEqual("thread-1", restored.thread_id)

    async def test_expired_mapping_is_archived_and_replaced(self) -> None:
        context = make_context("message-1")
        self.store.save_session(_session_key(context), "old-thread", self.now)
        self.now += 301

        await self.service.handle(context)

        self.assertEqual(["old-thread"], self.backend.archived)
        self.assertEqual(["thread-1"], self.backend.started)

    async def test_reset_commands_clear_mapping_and_archive_thread(self) -> None:
        for index, command in enumerate(
            ("/new", "/clear", "/清空上下文", "/new@ZmeRobot", "@小未 /new"),
            start=1,
        ):
            with self.subTest(command=command):
                thread_id = f"old-thread-{index}"
                context = make_context(f"message-{index}", text=command)
                session_key = _session_key(context)
                self.store.save_session(session_key, thread_id, self.now)

                response = await self.service.handle(context)

                self.assertIn("已清空", response or "")
                self.assertEqual(thread_id, self.backend.archived[-1])
                self.assertIsNone(self.store.get_session(session_key))
        self.assertFalse(self.backend.runs)

    async def test_duplicate_message_is_not_processed(self) -> None:
        first = await self.service.handle(make_context("message-1"))
        duplicate = await self.service.handle(make_context("message-1"))

        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)
        self.assertEqual(1, len(self.backend.runs))

    async def test_reply_is_safely_truncated(self) -> None:
        service = AgentService(
            self.backend,
            self.store,
            max_reply_characters=30,
            clock=lambda: self.now,
        )
        original_run = self.backend.run

        async def long_run(thread_id: str, message: str) -> str:
            await original_run(thread_id, message)
            return "甲" * 100

        self.backend.run = long_run  # type: ignore[method-assign]

        response = await service.handle(make_context("message-1"))

        assert response is not None
        self.assertLessEqual(len(response), 30)
        self.assertTrue(response.endswith("（回答过长，已安全截断）"))

    async def test_timeout_and_exception_return_friendly_messages(self) -> None:
        self.backend.run_error = CodexTimeoutError("timeout")
        timeout_response = await self.service.handle(make_context("message-1"))

        self.backend.run_error = RuntimeError("boom")
        error_response = await self.service.handle(make_context("message-2"))

        self.assertIn("处理超时", timeout_response or "")
        self.assertIn("暂时无法完成请求", error_response or "")

    async def test_same_conversation_is_serial_and_different_chats_are_parallel(self) -> None:
        self.backend.run_delay = 0.03
        await asyncio.gather(
            self.service.handle(make_context("message-1", "conversation-1")),
            self.service.handle(make_context("message-2", "conversation-1")),
            self.service.handle(make_context("message-3", "conversation-2")),
        )

        for maximum in self.backend.max_active_by_thread.values():
            self.assertEqual(1, maximum)
        self.assertEqual(2, self.backend.max_active)

    async def test_evicts_oldest_thread_when_capacity_is_reached(self) -> None:
        service = AgentService(
            self.backend,
            self.store,
            max_active_threads=2,
            clock=lambda: self.now,
        )
        for index, user_id in enumerate(("user-a", "user-b", "user-c"), start=1):
            self.now += 1
            await service.handle(
                make_context(f"message-{index}", sender_user_id=user_id)
            )

        self.assertEqual(["thread-1"], self.backend.archived)
        sessions = self.store.list_sessions_oldest_first()
        self.assertEqual(2, len(sessions))
        self.assertEqual({"thread-2", "thread-3"}, {item.thread_id for item in sessions})

    async def test_idle_thread_is_archived_automatically(self) -> None:
        service = AgentService(
            self.backend,
            self.store,
            session_ttl_seconds=0.01,
        )
        await service.handle(make_context("message-1"))
        await asyncio.sleep(0.03)

        self.assertEqual(["thread-1"], self.backend.archived)
        self.assertEqual([], self.store.list_sessions_oldest_first())

    async def test_startup_prunes_expired_and_excess_threads(self) -> None:
        self.store.save_session("expired", "thread-expired", 600.0)
        for index in range(11):
            self.store.save_session(
                f"session-{index}",
                f"thread-{index}",
                900.0 + index,
            )
        service = AgentService(
            self.backend,
            self.store,
            max_active_threads=10,
            session_ttl_seconds=300.0,
            clock=lambda: self.now,
        )

        await service.start_maintenance()

        self.assertEqual(
            {"thread-expired", "thread-0"},
            set(self.backend.archived),
        )
        self.assertEqual(10, len(self.store.list_sessions_oldest_first()))


if __name__ == "__main__":
    unittest.main()
