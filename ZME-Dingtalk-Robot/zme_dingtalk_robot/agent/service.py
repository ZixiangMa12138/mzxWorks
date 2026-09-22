"""Conversation policy between the DingTalk transport and Codex backend.

This layer owns deduplication, per-conversation ordering, global concurrency,
Thread lifetime and user-facing error messages.  It intentionally knows
nothing about DingTalk cards or webhooks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import hashlib
import json
import logging
import time
from typing import Protocol
import weakref

from ..logging_config import log_extra
from ..models import MessageContext
from .codex_backend import CodexTimeoutError
from .session_store import ConversationSession, SessionStore


RESET_COMMANDS = frozenset({"/new", "/clear", "/清空上下文"})


class Backend(Protocol):
    async def start_thread(self) -> str: ...

    async def resume_thread(self, thread_id: str) -> str: ...

    async def run(self, thread_id: str, message: str) -> str: ...

    async def archive_thread(self, thread_id: str) -> None: ...


class AgentService:
    """Serialize each conversation while allowing bounded cross-chat work.

    A per-user, per-conversation lock prevents two messages from racing on the
    same Thread.  The semaphore caps total Codex work across all users.
    """

    def __init__(
        self,
        backend: Backend,
        store: SessionStore,
        *,
        max_concurrency: int = 2,
        max_active_threads: int = 10,
        session_ttl_seconds: float = 300.0,
        message_dedup_retention_seconds: float = 7 * 24 * 60 * 60,
        sqlite_cleanup_interval_seconds: float = 60 * 60,
        max_reply_characters: int = 4_000,
        clock: Callable[[], float] = time.time,
        logger: logging.Logger | None = None,
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency 必须大于 0")
        if session_ttl_seconds <= 0:
            raise ValueError("session_ttl_seconds 必须大于 0")
        if max_active_threads <= 0:
            raise ValueError("max_active_threads 必须大于 0")
        if message_dedup_retention_seconds <= 0:
            raise ValueError("message_dedup_retention_seconds 必须大于 0")
        if sqlite_cleanup_interval_seconds <= 0:
            raise ValueError("sqlite_cleanup_interval_seconds 必须大于 0")
        if max_reply_characters <= 0:
            raise ValueError("max_reply_characters 必须大于 0")

        self._backend = backend
        self._store = store
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_active_threads = max_active_threads
        self._session_ttl_seconds = session_ttl_seconds
        self._message_dedup_retention_seconds = message_dedup_retention_seconds
        self._sqlite_cleanup_interval_seconds = sqlite_cleanup_interval_seconds
        self._max_reply_characters = max_reply_characters
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        # Weak values prevent one tiny lock object from accumulating forever
        # for every user who has ever contacted the robot.
        self._conversation_locks: weakref.WeakValueDictionary[
            str, asyncio.Lock
        ] = weakref.WeakValueDictionary()
        self._locks_guard = asyncio.Lock()
        self._maintenance_lock = asyncio.Lock()
        self._active_session_keys: set[str] = set()
        self._expiry_tasks: dict[str, asyncio.Task[None]] = {}
        self._maintenance_started = False
        self._startup_maintenance_task: asyncio.Task[None] | None = None
        self._dedup_cleanup_task: asyncio.Task[None] | None = None

    def start_maintenance(self) -> asyncio.Task[None]:
        """Restore idle timers once the DingTalk event loop is running."""
        if self._maintenance_started:
            assert self._startup_maintenance_task is not None
            return self._startup_maintenance_task
        self._maintenance_started = True
        self._startup_maintenance_task = asyncio.create_task(
            self._start_maintenance()
        )
        return self._startup_maintenance_task

    async def _start_maintenance(self) -> None:
        """Clean persisted state, restore timers, then start periodic cleanup."""
        await self._cleanup_processed_messages()
        await self._restore_expiry_tasks()
        self._dedup_cleanup_task = asyncio.create_task(
            self._run_periodic_dedup_cleanup()
        )
        self._dedup_cleanup_task.add_done_callback(self._dedup_cleanup_done)

    async def _run_periodic_dedup_cleanup(self) -> None:
        """Bound deduplication storage without coupling it to Thread expiry."""
        while True:
            await asyncio.sleep(self._sqlite_cleanup_interval_seconds)
            await self._cleanup_processed_messages()

    async def _cleanup_processed_messages(self) -> None:
        cutoff = self._clock() - self._message_dedup_retention_seconds
        try:
            deleted = await asyncio.to_thread(
                self._store.delete_processed_messages_before,
                cutoff,
            )
        except Exception:
            # Database housekeeping must never prevent Stream startup or stop
            # later cleanup attempts.  The next interval retries naturally.
            self._logger.exception("SQLite 消息去重记录清理失败 cutoff=%s", cutoff)
            return
        if deleted:
            self._logger.info(
                "SQLite 消息去重记录清理完成 deleted=%d cutoff=%s",
                deleted,
                cutoff,
            )

    def _dedup_cleanup_done(self, completed: asyncio.Task[None]) -> None:
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            self._logger.error(
                "SQLite 周期清理任务异常 error=%s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def claim_message(self, context: MessageContext) -> bool:
        """Atomically reserve a DingTalk message before starting any work."""
        if not context.message_id:
            self._logger.error("拒绝缺少 message_id 的消息")
            return False
        return await asyncio.to_thread(
            self._store.claim_message,
            context.message_id,
            context.conversation_id,
            self._clock(),
        )

    async def handle(self, context: MessageContext) -> str | None:
        """Claim and process one message; duplicates return ``None``."""
        if not await self.claim_message(context):
            return None
        return await self.respond(context)

    async def respond(self, context: MessageContext) -> str:
        """Run a claimed message under conversation and process-wide limits."""
        session_key = _session_key(context)
        lock = await self._conversation_lock(session_key)
        async with lock:
            async with self._semaphore:
                self._active_session_keys.add(session_key)
                try:
                    return await self._respond_serialized(context, session_key)
                finally:
                    self._active_session_keys.discard(session_key)

    async def _conversation_lock(self, session_key: str) -> asyncio.Lock:
        # Lock creation itself is guarded, otherwise two simultaneous first
        # messages could each create a different lock for the same conversation.
        async with self._locks_guard:
            return self._conversation_locks.setdefault(
                session_key,
                asyncio.Lock(),
            )

    async def _respond_serialized(
        self,
        context: MessageContext,
        session_key: str,
    ) -> str:
        extra = log_extra(context.message_id)
        text = context.text.strip()
        if _is_reset_command(text):
            return await self._reset(session_key, extra)

        try:
            thread_id = await self._resolve_thread(session_key, extra)
            response = await self._backend.run(
                thread_id,
                _format_codex_message(context),
            )
            last_active_at = self._clock()
            await asyncio.to_thread(
                self._store.save_session,
                session_key,
                thread_id,
                last_active_at,
            )
            self._schedule_expiry(session_key, last_active_at)
            return self._truncate(response)
        except CodexTimeoutError:
            self._logger.warning(
                "Codex 处理超时 conversation_id=%s",
                context.conversation_id,
                extra=extra,
            )
            return self._truncate(
                "处理超时，请稍后重试；如需放弃当前上下文，可发送 /new。"
            )
        except Exception as error:
            self._logger.exception(
                "Codex 处理失败 conversation_id=%s error=%s",
                context.conversation_id,
                error,
                extra=extra,
            )
            return self._truncate("暂时无法完成请求，请稍后重试。")

    async def _resolve_thread(
        self,
        session_key: str,
        extra: dict[str, str],
    ) -> str:
        """Return a usable Thread, replacing expired or unresumable mappings."""
        now = self._clock()
        async with self._maintenance_lock:
            await self._remove_idle_sessions(now, session_key, extra)
            session = await asyncio.to_thread(self._store.get_session, session_key)

        if session is not None:
            try:
                # Resume on every request.  The backend cheaply returns/cache
                # handles as appropriate, while this also validates persisted
                # IDs after a restart or external Thread deletion.
                resumed_id = await self._backend.resume_thread(session.thread_id)
                if resumed_id != session.thread_id:
                    await asyncio.to_thread(
                        self._store.save_session,
                        session_key,
                        resumed_id,
                        now,
                    )
                    session = ConversationSession(session_key, resumed_id, now)
                self._schedule_expiry(session_key, session.last_active_at)
                return resumed_id
            except Exception as error:
                # A stale SQLite mapping must not permanently break the chat.
                # Clear it and continue below with a fresh contextual Thread.
                self._logger.warning(
                    "恢复 Codex Thread 失败，将创建新 Thread "
                    "conversation_id=%s thread_id=%s error=%s",
                    session_key,
                    session.thread_id,
                    error,
                    extra=extra,
                )
                await asyncio.to_thread(self._store.clear_session, session_key)

        async with self._maintenance_lock:
            await self._evict_for_capacity(session_key, extra)
            thread_id = await self._backend.start_thread()
            await asyncio.to_thread(
                self._store.save_session,
                session_key,
                thread_id,
                now,
            )
            self._schedule_expiry(session_key, now)
        return thread_id

    def _is_expired(self, session: ConversationSession, now: float) -> bool:
        return now - session.last_active_at >= self._session_ttl_seconds

    async def _reset(
        self,
        session_key: str,
        extra: dict[str, str],
    ) -> str:
        thread_id = await asyncio.to_thread(
            self._store.clear_session,
            session_key,
        )
        self._cancel_expiry(session_key)
        if thread_id is None:
            return "当前会话已经是全新上下文。"
        await self._archive_best_effort(thread_id, extra)
        return "已清空当前会话上下文，下一条消息将创建新的 Codex Thread。"

    async def _remove_idle_sessions(
        self,
        now: float,
        current_session_key: str,
        extra: dict[str, str],
    ) -> None:
        sessions = await asyncio.to_thread(self._store.list_sessions_oldest_first)
        for session in sessions:
            if not self._is_expired(session, now):
                break
            if (
                session.session_key in self._active_session_keys
                and session.session_key != current_session_key
            ):
                continue
            await asyncio.to_thread(self._store.clear_session, session.session_key)
            self._cancel_expiry(session.session_key)
            await self._archive_best_effort(session.thread_id, extra)

    async def _evict_for_capacity(
        self,
        current_session_key: str,
        extra: dict[str, str],
    ) -> None:
        sessions = await asyncio.to_thread(self._store.list_sessions_oldest_first)
        while len(sessions) >= self._max_active_threads:
            victim = next(
                (
                    item
                    for item in sessions
                    if item.session_key != current_session_key
                    and item.session_key not in self._active_session_keys
                ),
                None,
            )
            if victim is None:
                raise RuntimeError("Codex Thread 已达到上限且都在执行中")
            await asyncio.to_thread(self._store.clear_session, victim.session_key)
            self._cancel_expiry(victim.session_key)
            await self._archive_best_effort(victim.thread_id, extra)
            sessions.remove(victim)

    async def _restore_expiry_tasks(self) -> None:
        """Recreate timers for persisted mappings after a process restart."""
        async with self._maintenance_lock:
            now = self._clock()
            sessions = await asyncio.to_thread(self._store.list_sessions_oldest_first)
            for session in sessions:
                if self._is_expired(session, now):
                    await asyncio.to_thread(
                        self._store.clear_session,
                        session.session_key,
                    )
                    await self._archive_best_effort(session.thread_id, {})
            sessions = await asyncio.to_thread(self._store.list_sessions_oldest_first)
            while len(sessions) > self._max_active_threads:
                victim = sessions.pop(0)
                await asyncio.to_thread(
                    self._store.clear_session,
                    victim.session_key,
                )
                await self._archive_best_effort(victim.thread_id, {})
            for session in sessions:
                remaining = self._session_ttl_seconds - (
                    now - session.last_active_at
                )
                self._schedule_expiry(
                    session.session_key,
                    session.last_active_at,
                    delay_seconds=remaining,
                )

    def _schedule_expiry(
        self,
        session_key: str,
        last_active_at: float,
        *,
        delay_seconds: float | None = None,
    ) -> None:
        self._cancel_expiry(session_key)
        task = asyncio.create_task(
            self._expire_after_idle(
                session_key,
                last_active_at,
                self._session_ttl_seconds
                if delay_seconds is None
                else delay_seconds,
            )
        )
        self._expiry_tasks[session_key] = task
        task.add_done_callback(
            lambda completed, key=session_key: self._expiry_task_done(key, completed)
        )

    async def _expire_after_idle(
        self,
        session_key: str,
        expected_last_active_at: float,
        delay_seconds: float,
    ) -> None:
        await asyncio.sleep(delay_seconds)
        lock = await self._conversation_lock(session_key)
        async with lock:
            async with self._maintenance_lock:
                session = await asyncio.to_thread(
                    self._store.get_session,
                    session_key,
                )
                if (
                    session is None
                    or session.last_active_at != expected_last_active_at
                    or not self._is_expired(session, self._clock())
                ):
                    return
                await asyncio.to_thread(self._store.clear_session, session_key)
                await self._archive_best_effort(session.thread_id, {})

    def _cancel_expiry(self, session_key: str) -> None:
        task = self._expiry_tasks.pop(session_key, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _expiry_task_done(
        self,
        session_key: str,
        completed: asyncio.Task[None],
    ) -> None:
        if self._expiry_tasks.get(session_key) is completed:
            self._expiry_tasks.pop(session_key, None)
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            self._logger.error(
                "Codex Thread 空闲清理任务异常 error=%s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _archive_best_effort(
        self,
        thread_id: str,
        extra: dict[str, str],
    ) -> None:
        # Resetting local context must succeed even when the remote archive
        # endpoint is temporarily unavailable; the old ID is no longer reused.
        try:
            await self._backend.archive_thread(thread_id)
        except Exception as error:
            self._logger.warning(
                "归档 Codex Thread 失败 thread_id=%s error=%s",
                thread_id,
                error,
                extra=extra,
            )

    def _truncate(self, text: str) -> str:
        """Bound output for DingTalk while retaining an explicit truncation mark."""
        if len(text) <= self._max_reply_characters:
            return text
        suffix = "\n\n（回答过长，已安全截断）"
        if len(suffix) >= self._max_reply_characters:
            return suffix[: self._max_reply_characters]
        keep = self._max_reply_characters - len(suffix)
        return text[:keep].rstrip() + suffix


def _is_reset_command(text: str) -> bool:
    normalized = text.strip().casefold()
    if normalized.startswith("@"):
        mention_and_text = normalized.split(maxsplit=1)
        if len(mention_and_text) == 2:
            normalized = mention_and_text[1]
    command = normalized.split(maxsplit=1)[0] if normalized else ""
    command = command.split("@", maxsplit=1)[0]
    return command in RESET_COMMANDS


def _session_key(context: MessageContext) -> str:
    """Build an opaque key scoped to one user inside one DingTalk chat."""
    # Never merge unidentified group members into one context.  A missing
    # sender ID gets a one-message scope and therefore cannot confirm another
    # person's pending operation.
    actor_key = context.sender_user_id or f"unknown:{context.message_id}"
    identity = f"{context.conversation_id}\0{actor_key}"
    return "v2:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _format_codex_message(context: MessageContext) -> str:
    """Attach gateway-owned actor metadata to the untrusted message body."""
    envelope = {
        "dingtalk_metadata": {
            "sender_user_id": context.sender_user_id,
            "sender_nickname": context.sender_nickname,
            "conversation_id": context.conversation_id,
            "conversation_type": context.conversation_type,
        },
        "user_message": context.text,
    }
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
