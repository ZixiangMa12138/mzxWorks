"""DingTalk Stream transport with immediate ACK and background processing.

The Stream callback and the visible robot reply are separate operations.  The
callback must return quickly so DingTalk does not retry delivery; Codex work
continues in a tracked asyncio Task and later updates an AI card (or falls back
to the original message's sessionWebhook).
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
import logging
from typing import Any, Protocol

import dingtalk_stream
from dingtalk_stream import AckMessage

from .agent.service import AgentService
from .logging_config import log_extra
from .models import MessageContext


class Relay(Protocol):
    async def send(self, context: MessageContext, text: str) -> None: ...


class AICard(Protocol):
    card_instance_id: str | None

    def set_order(self, order: list[str]) -> None: ...

    def ai_streaming(self, markdown: str, append: bool = False) -> None: ...

    def ai_finish(self, markdown: str | None = None, **kwargs: Any) -> None: ...


class BotHandler(dingtalk_stream.ChatbotHandler):
    """Convert Stream callbacks and hand accepted text to Codex asynchronously."""

    def __init__(
        self,
        service: AgentService,
        relay: Relay,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._relay = relay
        self._logger = logger or logging.getLogger(__name__)
        self._tasks: set[asyncio.Task[None]] = set()

    def pre_start(self) -> None:
        """Start persisted Thread expiry timers inside the SDK event loop."""
        super().pre_start()
        self._service.start_maintenance()

    async def process(
        self,
        callback: dingtalk_stream.CallbackMessage,
    ) -> tuple[str, str]:
        """Parse one callback, schedule business work, and ACK immediately.

        Returning ``("OK", "OK")`` acknowledges transport receipt only.  It
        does not mean Codex has finished or that a visible reply was delivered.
        """
        message = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
        message_id = message.message_id or callback.headers.message_id or ""
        sender_user_id = message.sender_staff_id or message.sender_id or ""
        # Group callbacks provide conversation_id.  The synthetic single-chat
        # key keeps direct messages isolated when that field is absent.
        conversation_id = message.conversation_id or f"single:{sender_user_id}"
        context = MessageContext(
            text=(
                message.text.content.strip()
                if message.message_type == "text" and message.text is not None
                else ""
            ),
            message_id=message_id,
            sender_user_id=sender_user_id,
            sender_nickname=message.sender_nick or "",
            conversation_id=conversation_id,
            conversation_type=message.conversation_type or "",
            raw_message=message,
        )
        extra = log_extra(message_id)

        if message.message_type != "text" or message.text is None:
            self._logger.info(
                "收到非文本消息 message_type=%s conversation_id=%s",
                message.message_type,
                conversation_id,
                extra=extra,
            )
            self._spawn(
                self._send_safely(
                    context,
                    "当前仅支持文本消息。",
                    "非文本提示回复失败",
                )
            )
            return AckMessage.STATUS_OK, "OK"

        self._logger.info(
            "收到文本消息 sender_user_id=%s conversation_id=%s content=%r",
            sender_user_id,
            conversation_id,
            context.text,
            extra=extra,
        )
        self._spawn(self._process_in_background(context))
        return AckMessage.STATUS_OK, "OK"

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        # Keep a strong reference until completion.  It also gives tests and
        # shutdown diagnostics visibility into outstanding background work.
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._logger.error(
                "后台消息任务异常",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _process_in_background(self, context: MessageContext) -> None:
        """Deduplicate, show processing state, call Codex, and deliver output."""
        extra = log_extra(context.message_id)
        if not await self._service.claim_message(context):
            self._logger.info(
                "忽略重复消息 conversation_id=%s",
                context.conversation_id,
                extra=extra,
            )
            return

        # Creating the card before Codex starts gives the user an immediate,
        # client-rendered processing indicator without sending a text message.
        card = await self._start_ai_card(context)
        response = await self._service.respond(context)
        if card is not None:
            await self._finish_ai_card(context, card, response)
        else:
            await self._send_safely(context, response, "最终结果回复失败")

    async def _start_ai_card(self, context: MessageContext) -> AICard | None:
        """Create the SDK's processing card, or return None for text fallback."""
        if self.dingtalk_client is None:
            return None
        try:
            # dingtalk-stream's card helper performs blocking HTTP requests.
            # Move it off the event loop so other Stream callbacks can ACK.
            card = await asyncio.to_thread(
                self.ai_markdown_card_start,
                context.raw_message,
            )
        except Exception as error:
            self._logger.warning(
                "AI 卡片创建失败，将仅发送最终文本 "
                "conversation_id=%s error=%s",
                context.conversation_id,
                error,
                extra=log_extra(context.message_id),
            )
            return None

        if not getattr(card, "card_instance_id", None):
            self._logger.warning(
                "AI 卡片不可用，将仅发送最终文本 conversation_id=%s",
                context.conversation_id,
                extra=log_extra(context.message_id),
            )
            return None
        # The generic SDK template also advertises empty title/image/slider
        # sections.  Restricting the order prevents those placeholders from
        # appearing around the Markdown answer.
        card.set_order(["msgContent"])
        return card

    async def _finish_ai_card(
        self,
        context: MessageContext,
        card: AICard,
        response: str,
    ) -> None:
        """Replace processing state with the final Markdown answer.

        The SDK template expects the lifecycle start -> streaming -> finish.
        ``append=False`` sends the complete answer in one update because Codex
        currently returns only its final response, not token deltas.
        """
        try:
            await asyncio.to_thread(
                card.ai_streaming,
                markdown=response,
                append=False,
            )
            await asyncio.to_thread(card.ai_finish, markdown=response)
            self._logger.info(
                "AI 卡片最终结果更新已提交 response_chars=%d",
                len(response),
                extra=log_extra(context.message_id),
            )
        except Exception as error:
            self._logger.exception(
                "AI 卡片最终更新异常，将补发最终文本 "
                "conversation_id=%s error=%s",
                context.conversation_id,
                error,
                extra=log_extra(context.message_id),
            )
            await self._send_safely(context, response, "最终结果补发失败")

    async def _send_safely(
        self,
        context: MessageContext,
        text: str,
        failure_message: str,
    ) -> None:
        try:
            await self._relay.send(context, text)
        except Exception as error:
            self._logger.exception(
                "%s conversation_id=%s error=%s",
                failure_message,
                context.conversation_id,
                error,
                extra=log_extra(context.message_id),
            )
