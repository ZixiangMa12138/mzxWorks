"""Official SDK adapter tests with a fully fake openai_codex module."""

import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from zme_dingtalk_robot.agent.codex_backend import (
    CodexBackend,
    CodexTimeoutError,
    DEVELOPER_INSTRUCTIONS,
)


class FakeExternalMessage:
    def __init__(self, *, tool_name, namespace, content) -> None:
        self.tool_name = tool_name
        self.namespace = namespace
        self.content = content


class FakeTurn:
    def __init__(self, response: str = "最终回答", delay: float = 0.0) -> None:
        self.id = "turn-1"
        self.response = response
        self.delay = delay
        self.interrupted = False

    async def run(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        return SimpleNamespace(final_response=self.response)

    async def interrupt(self) -> None:
        self.interrupted = True


class FakeThread:
    def __init__(self, thread_id: str, turn: FakeTurn | None = None) -> None:
        self.id = thread_id
        self.turn_object = turn or FakeTurn()
        self.inputs = []

    async def turn(self, value):
        self.inputs.append(value)
        return self.turn_object


class FakeCodexConfig:
    def __init__(self, *, cwd: str) -> None:
        self.cwd = cwd


class FakeAsyncCodex:
    instances = []

    def __init__(self, config) -> None:
        self.config = config
        self.start_kwargs = None
        self.resume_calls = []
        self.archived = []
        self.threads = {}
        self.login_keys = []
        FakeAsyncCodex.instances.append(self)

    async def login_api_key(self, api_key: str) -> None:
        self.login_keys.append(api_key)

    async def thread_start(self, **kwargs):
        self.start_kwargs = kwargs
        thread = FakeThread("thread-1")
        self.threads[thread.id] = thread
        return thread

    async def thread_resume(self, thread_id: str, **kwargs):
        self.resume_calls.append((thread_id, kwargs))
        thread = self.threads.setdefault(thread_id, FakeThread(thread_id))
        return thread

    async def thread_archive(self, thread_id: str) -> None:
        self.archived.append(thread_id)

    async def close(self) -> None:
        return None


def fake_sdk_module() -> ModuleType:
    module = ModuleType("openai_codex")
    module.AsyncCodex = FakeAsyncCodex
    module.CodexConfig = FakeCodexConfig
    module.ExternalMessage = FakeExternalMessage
    module.Sandbox = SimpleNamespace(full_access="full-access")
    return module


class CodexBackendTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeAsyncCodex.instances.clear()
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self._temporary_directory.name) / "workspace"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_starts_persistent_thread_without_hardcoded_model(self) -> None:
        with patch.dict(sys.modules, {"openai_codex": fake_sdk_module()}), patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            backend = CodexBackend(self.workspace)
            thread_id = await backend.start_thread()
            response = await backend.run(thread_id, "读取真实文档")

        client = FakeAsyncCodex.instances[0]
        self.assertEqual("thread-1", thread_id)
        self.assertEqual("最终回答", response)
        self.assertFalse(client.start_kwargs["ephemeral"])
        self.assertNotIn("model", client.start_kwargs)
        self.assertEqual(str(self.workspace.resolve()), client.start_kwargs["cwd"])
        self.assertEqual(
            DEVELOPER_INSTRUCTIONS,
            client.start_kwargs["developer_instructions"],
        )
        message = client.threads[thread_id].inputs[0]
        self.assertEqual("dingtalk_robot", message.tool_name)
        self.assertEqual("读取真实文档", message.content)

    async def test_timeout_interrupts_turn(self) -> None:
        module = fake_sdk_module()
        with patch.dict(sys.modules, {"openai_codex": module}), patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            backend = CodexBackend(self.workspace, timeout_seconds=0.01)
            thread_id = await backend.start_thread()
            client = FakeAsyncCodex.instances[0]
            turn = FakeTurn(delay=0.05)
            client.threads[thread_id].turn_object = turn

            with self.assertRaises(CodexTimeoutError):
                await backend.run(thread_id, "慢请求")

        self.assertTrue(turn.interrupted)


if __name__ == "__main__":
    unittest.main()
