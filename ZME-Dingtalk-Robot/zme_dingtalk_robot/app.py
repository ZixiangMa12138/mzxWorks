"""Application assembly for the DingTalk-to-Codex gateway."""

import asyncio
import logging

import dingtalk_stream

from .agent.codex_backend import CodexBackend
from .agent.service import AgentService
from .agent.session_store import SessionStore
from .config import Settings
from .handler import BotHandler
from .logging_config import configure_logging
from .relay import SessionWebhookRelay


def run(settings: Settings) -> None:
    """Assemble dependencies and continuously run the Stream connection.

    The SDK opens an outbound persistent connection to DingTalk.  No inbound
    HTTP server is created by this application.
    """
    configure_logging(
        settings.log_level,
        settings.log_file,
        settings.log_max_bytes,
        settings.log_backup_count,
    )
    logger = logging.getLogger(__name__)
    logger.info(
        "应用初始化 database=%s codex_workspace=%s max_concurrency=%d",
        settings.conversation_database_path,
        settings.codex_workspace,
        settings.codex_max_concurrency,
    )

    # Dependency direction is transport -> policy -> backend/store.  Keeping
    # these objects separate lets unit tests replace all external boundaries.
    backend = CodexBackend(
        workspace=settings.codex_workspace,
        timeout_seconds=settings.codex_timeout_seconds,
        logger=logging.getLogger("zme_dingtalk_robot.codex"),
    )
    service = AgentService(
        backend,
        SessionStore(settings.conversation_database_path),
        max_concurrency=settings.codex_max_concurrency,
        max_active_threads=settings.max_active_threads,
        session_ttl_seconds=settings.conversation_idle_timeout_seconds,
        max_reply_characters=settings.max_reply_characters,
        logger=logging.getLogger("zme_dingtalk_robot.agent"),
    )
    relay = SessionWebhookRelay(
        timeout_seconds=settings.dingtalk_reply_timeout_seconds,
        logger=logging.getLogger("zme_dingtalk_robot.relay"),
    )

    credential = dingtalk_stream.Credential(
        settings.dingtalk_client_id,
        settings.dingtalk_client_secret,
    )
    client = dingtalk_stream.DingTalkStreamClient(credential, logger=logger)
    # Chatbot callbacks are acknowledged by BotHandler immediately; long Codex
    # turns therefore do not block or time out the Stream delivery callback.
    client.register_callback_handler(
        dingtalk_stream.ChatbotMessage.TOPIC,
        BotHandler(
            service,
            relay,
            logger=logging.getLogger("zme_dingtalk_robot.handler"),
        ),
    )
    logger.info("正在启动 ZME 钉钉机器人 Stream 客户端")
    try:
        client.start_forever()
    finally:
        # start_forever owns the main event loop; use a short shutdown loop to
        # release the Codex SDK's process/subprocess resources afterward.
        asyncio.run(backend.close())
