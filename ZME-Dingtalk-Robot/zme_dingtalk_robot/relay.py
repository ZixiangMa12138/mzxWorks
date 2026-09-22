"""Reply to the originating DingTalk conversation through sessionWebhook."""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.request import Request, urlopen

from .logging_config import log_extra
from .models import MessageContext


class SessionWebhookRelay:
    """Send a plain-text reply through the webhook attached to one callback.

    This is the compatibility/failure path when AI cards cannot be created or
    updated.  A sessionWebhook belongs to the originating message and should
    not be persisted or reused as a general-purpose bot webhook.
    """
    def __init__(
        self,
        timeout_seconds: float = 20.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._logger = logger or logging.getLogger(__name__)

    async def send(self, context: MessageContext, text: str) -> None:
        """POST text to the source conversation without blocking asyncio."""
        message = context.raw_message
        webhook = getattr(message, "session_webhook", None)
        if not isinstance(webhook, str) or not webhook.startswith("https://"):
            raise RuntimeError("消息缺少有效的 HTTPS sessionWebhook")

        payload = {
            "msgtype": "text",
            "text": {"content": text},
            "at": {"atUserIds": [context.sender_user_id]},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            webhook,
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
            method="POST",
        )
        # urllib is synchronous; run it in a worker so Stream reception and
        # other conversations continue while DingTalk responds.
        await asyncio.to_thread(self._post, request)
        self._logger.info(
            "sessionWebhook 回复成功 response_chars=%d",
            len(text),
            extra=log_extra(context.message_id),
        )

    def _post(self, request: Request) -> None:
        with urlopen(request, timeout=self._timeout_seconds) as response:
            response.read()
