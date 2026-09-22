"""DingTalk Stream handler background handoff tests."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import dingtalk_stream
from dingtalk_stream import AckMessage

from zme_dingtalk_robot.handler import BotHandler


class FakeService:
    def __init__(self) -> None:
        self.claimed = []
        self.respond_started = asyncio.Event()
        self.release_response = asyncio.Event()

    async def claim_message(self, context) -> bool:
        self.claimed.append(context.message_id)
        return True

    async def respond(self, context) -> str:
        self.respond_started.set()
        await self.release_response.wait()
        return "最终回答"


class FakeRelay:
    def __init__(self) -> None:
        self.messages = []

    async def send(self, context, text: str) -> None:
        self.messages.append((context.conversation_id, text))


class FakeAICard:
    def __init__(
        self,
        card_instance_id: str | None = "card-1",
        finish_error: Exception | None = None,
    ) -> None:
        self.card_instance_id = card_instance_id
        self.finish_error = finish_error
        self.order = None
        self.streamed_markdown = None
        self.finished_markdown = None

    def set_order(self, order: list[str]) -> None:
        self.order = order

    def ai_streaming(self, markdown: str, append: bool = False) -> None:
        self.streamed_markdown = markdown

    def ai_finish(self, markdown: str | None = None, **kwargs) -> None:
        if self.finish_error is not None:
            raise self.finish_error
        self.finished_markdown = markdown


class BotHandlerTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_ack_before_background_codex_finishes(self) -> None:
        service = FakeService()
        relay = FakeRelay()
        handler = BotHandler(service, relay)
        incoming = SimpleNamespace(
            message_id="message-1",
            sender_staff_id="user-1",
            sender_id=None,
            sender_nick="测试用户",
            conversation_id="conversation-1",
            conversation_type="2",
            message_type="text",
            text=SimpleNamespace(content="读取文档"),
            session_webhook="https://example.test/session",
        )
        callback = SimpleNamespace(
            data={},
            headers=SimpleNamespace(message_id="header-message-1"),
        )

        with patch.object(
            dingtalk_stream.ChatbotMessage,
            "from_dict",
            return_value=incoming,
        ):
            status, message = await handler.process(callback)

        self.assertEqual(AckMessage.STATUS_OK, status)
        self.assertEqual("OK", message)
        self.assertFalse(service.respond_started.is_set())

        await service.respond_started.wait()
        self.assertEqual([], relay.messages)
        service.release_response.set()
        await asyncio.gather(*handler._tasks)

        self.assertEqual([("conversation-1", "最终回答")], relay.messages)
        self.assertEqual(["message-1"], service.claimed)

    async def test_ai_card_shows_processing_then_receives_final_answer(self) -> None:
        service = FakeService()
        relay = FakeRelay()
        handler = BotHandler(service, relay)
        handler.dingtalk_client = SimpleNamespace()
        card = FakeAICard()
        incoming = SimpleNamespace(
            message_id="message-1",
            sender_staff_id="user-1",
            sender_id=None,
            sender_nick="测试用户",
            conversation_id="conversation-1",
            conversation_type="2",
            message_type="text",
            text=SimpleNamespace(content="读取文档"),
            session_webhook="https://example.test/session",
        )
        callback = SimpleNamespace(
            data={},
            headers=SimpleNamespace(message_id="header-message-1"),
        )

        with patch.object(
            dingtalk_stream.ChatbotMessage,
            "from_dict",
            return_value=incoming,
        ), patch.object(
            handler,
            "ai_markdown_card_start",
            return_value=card,
        ):
            status, message = await handler.process(callback)
            await service.respond_started.wait()
            self.assertIsNone(card.finished_markdown)
            self.assertEqual([], relay.messages)
            service.release_response.set()
            await asyncio.gather(*handler._tasks)

        self.assertEqual(AckMessage.STATUS_OK, status)
        self.assertEqual("OK", message)
        self.assertEqual(["msgContent"], card.order)
        self.assertEqual("最终回答", card.streamed_markdown)
        self.assertEqual("最终回答", card.finished_markdown)
        self.assertEqual([], relay.messages)

    async def test_unavailable_ai_card_falls_back_to_final_text_only(self) -> None:
        service = FakeService()
        service.release_response.set()
        relay = FakeRelay()
        handler = BotHandler(service, relay)
        handler.dingtalk_client = SimpleNamespace()
        incoming = SimpleNamespace(
            message_id="message-1",
            sender_staff_id="user-1",
            sender_id=None,
            sender_nick="测试用户",
            conversation_id="conversation-1",
            conversation_type="2",
            message_type="text",
            text=SimpleNamespace(content="读取文档"),
            session_webhook="https://example.test/session",
        )
        callback = SimpleNamespace(
            data={},
            headers=SimpleNamespace(message_id="header-message-1"),
        )

        with patch.object(
            dingtalk_stream.ChatbotMessage,
            "from_dict",
            return_value=incoming,
        ), patch.object(
            handler,
            "ai_markdown_card_start",
            return_value=FakeAICard(card_instance_id=None),
        ):
            await handler.process(callback)
            await asyncio.gather(*handler._tasks)

        self.assertEqual([("conversation-1", "最终回答")], relay.messages)

    async def test_ai_card_update_error_falls_back_to_final_text(self) -> None:
        service = FakeService()
        service.release_response.set()
        relay = FakeRelay()
        handler = BotHandler(service, relay)
        handler.dingtalk_client = SimpleNamespace()
        incoming = SimpleNamespace(
            message_id="message-1",
            sender_staff_id="user-1",
            sender_id=None,
            sender_nick="测试用户",
            conversation_id="conversation-1",
            conversation_type="2",
            message_type="text",
            text=SimpleNamespace(content="读取文档"),
            session_webhook="https://example.test/session",
        )
        callback = SimpleNamespace(
            data={},
            headers=SimpleNamespace(message_id="header-message-1"),
        )

        with patch.object(
            dingtalk_stream.ChatbotMessage,
            "from_dict",
            return_value=incoming,
        ), patch.object(
            handler,
            "ai_markdown_card_start",
            return_value=FakeAICard(finish_error=RuntimeError("update failed")),
        ):
            await handler.process(callback)
            await asyncio.gather(*handler._tasks)

        self.assertEqual([("conversation-1", "最终回答")], relay.messages)


if __name__ == "__main__":
    unittest.main()
