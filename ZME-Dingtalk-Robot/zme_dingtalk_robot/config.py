"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少必需环境变量：{name}")
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} 必须是整数") from error
    if value <= 0:
        raise RuntimeError(f"{name} 必须大于 0")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} 必须是数字") from error
    if value <= 0:
        raise RuntimeError(f"{name} 必须大于 0")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    dingtalk_client_id: str
    dingtalk_client_secret: str
    conversation_database_path: Path = Path("data/conversations.db")
    codex_workspace: Path = Path("data/codex-workspace")
    codex_timeout_seconds: float = 600.0
    codex_max_concurrency: int = 2
    max_active_threads: int = 10
    conversation_idle_timeout_seconds: float = 300.0
    message_dedup_retention_days: float = 7.0
    sqlite_cleanup_interval_seconds: float = 3_600.0
    max_reply_characters: int = 4_000
    dingtalk_reply_timeout_seconds: float = 20.0
    log_level: str = "INFO"
    log_file: Path = Path("logs/robot.log")
    log_max_bytes: int = 10_485_760
    log_backup_count: int = 5

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            dingtalk_client_id=_required("DINGTALK_CLIENT_ID"),
            dingtalk_client_secret=_required("DINGTALK_CLIENT_SECRET"),
            conversation_database_path=Path(
                os.getenv("CONVERSATION_DATABASE_PATH", "data/conversations.db")
            ).expanduser(),
            codex_workspace=Path(
                os.getenv("CODEX_WORKSPACE", "data/codex-workspace")
            ).expanduser(),
            codex_timeout_seconds=_positive_float("CODEX_TIMEOUT_SECONDS", 600.0),
            codex_max_concurrency=_positive_int("CODEX_MAX_CONCURRENCY", 2),
            max_active_threads=_positive_int("MAX_ACTIVE_THREADS", 10),
            conversation_idle_timeout_seconds=_positive_float(
                "CONVERSATION_IDLE_TIMEOUT_SECONDS",
                300.0,
            ),
            message_dedup_retention_days=_positive_float(
                "MESSAGE_DEDUP_RETENTION_DAYS",
                7.0,
            ),
            sqlite_cleanup_interval_seconds=_positive_float(
                "SQLITE_CLEANUP_INTERVAL_SECONDS",
                3_600.0,
            ),
            max_reply_characters=_positive_int("MAX_REPLY_CHARACTERS", 4_000),
            dingtalk_reply_timeout_seconds=_positive_float(
                "DINGTALK_REPLY_TIMEOUT_SECONDS",
                20.0,
            ),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            log_file=Path(os.getenv("LOG_FILE", "logs/robot.log")).expanduser(),
            log_max_bytes=_positive_int("LOG_MAX_BYTES", 10_485_760),
            log_backup_count=_positive_int("LOG_BACKUP_COUNT", 5),
        )
