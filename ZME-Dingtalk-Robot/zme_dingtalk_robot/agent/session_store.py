"""SQLite persistence for DingTalk conversation and Codex thread mappings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3


@dataclass(frozen=True, slots=True)
class ConversationSession:
    session_key: str
    thread_id: str
    last_active_at: float


class SessionStore:
    """Persist only Thread mappings, deduplication keys, and timestamps.

    Message bodies are intentionally absent from the schema.  Codex owns the
    transcript; the legacy ``conversation_id`` column now stores an opaque,
    user-scoped session key so existing databases need no destructive migration.
    """

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.resolve()
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        # Each call gets a short-lived connection.  Store methods run via
        # asyncio.to_thread(), and sharing one sqlite connection across worker
        # threads would violate sqlite3's default thread-safety contract.
        connection = sqlite3.connect(self._database_path, timeout=5.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            # WAL permits readers and the single writer to make progress with
            # less contention when different DingTalk conversations run at once.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_sessions (
                    conversation_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    last_active_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    processed_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_processed_messages_time
                ON processed_messages(processed_at);
                """
            )

    def claim_message(
        self,
        message_id: str,
        conversation_id: str,
        processed_at: float,
    ) -> bool:
        """Atomically claim a message ID; return false for a duplicate."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO processed_messages(
                    message_id, conversation_id, processed_at
                ) VALUES (?, ?, ?)
                """,
                (message_id, conversation_id, processed_at),
            )
            # The primary key plus INSERT OR IGNORE makes retries idempotent,
            # including duplicate deliveries racing in separate tasks.
            return cursor.rowcount == 1

    def get_session(self, session_key: str) -> ConversationSession | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT conversation_id, thread_id, last_active_at
                FROM conversation_sessions
                WHERE conversation_id = ?
                """,
                (session_key,),
            ).fetchone()
        if row is None:
            return None
        return ConversationSession(
            session_key=str(row[0]),
            thread_id=str(row[1]),
            last_active_at=float(row[2]),
        )

    def save_session(
        self,
        session_key: str,
        thread_id: str,
        last_active_at: float,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_sessions(
                    conversation_id, thread_id, last_active_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    last_active_at = excluded.last_active_at
                """,
                (session_key, thread_id, last_active_at),
            )

    def clear_session(self, session_key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT thread_id FROM conversation_sessions
                WHERE conversation_id = ?
                """,
                (session_key,),
            ).fetchone()
            connection.execute(
                "DELETE FROM conversation_sessions WHERE conversation_id = ?",
                (session_key,),
            )
        return str(row[0]) if row is not None else None

    def list_sessions_oldest_first(self) -> list[ConversationSession]:
        """Return all mappings in least-recently-active order."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT conversation_id, thread_id, last_active_at
                FROM conversation_sessions
                ORDER BY last_active_at ASC, conversation_id ASC
                """
            ).fetchall()
        return [
            ConversationSession(
                session_key=str(row[0]),
                thread_id=str(row[1]),
                last_active_at=float(row[2]),
            )
            for row in rows
        ]
