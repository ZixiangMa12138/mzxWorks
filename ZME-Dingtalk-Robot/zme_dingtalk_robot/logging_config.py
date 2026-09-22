"""机器人统一日志配置和敏感信息脱敏。"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sys


_LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s "
    "request_id=%(request_id)s %(message)s"
)

_SECRET_PATTERNS = (
    re.compile(
        r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?"
        r"(?:bearer|basic)\s+)[^\s,;\"']+"
    ),
    re.compile(
        r"(?i)(access[_-]?token|refresh[_-]?token|appsecret|"
        r"client[_-]?secret|api[_-]?key|sessionwebhook|webhook[_-]?token|ticket)"
        r"([\"']?\s*[:=]\s*[\"']?)([^\s,;}\"']+)"
    ),
    re.compile(r"(?i)([?&]access_token=)[^&\s]+"),
    re.compile(
        r"(?i)(x-acs-dingtalk-access-token[\"']?\s*[:=]\s*[\"']?)"
        r"[^\s,;\"']+"
    ),
)


class RequestIdFilter(logging.Filter):
    """确保所有日志均具有 request_id 字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True


class SensitiveDataFilter(logging.Filter):
    """对所有日志消息执行凭证级脱敏。"""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = redact_secrets(message)
        record.args = ()
        return True


def configure_logging(
    level_name: str,
    log_file: Path,
    max_bytes: int,
    backup_count: int,
) -> logging.Logger:
    """同时输出到控制台和按大小轮转的文件。"""
    level = getattr(logging, level_name.upper(), logging.INFO)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_LOG_FORMAT)
    filters = (RequestIdFilter(), SensitiveDataFilter())

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    for handler in (console_handler, file_handler):
        for log_filter in filters:
            handler.addFilter(log_filter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(console_handler)
    root.addHandler(file_handler)

    logging.getLogger("websockets").setLevel(logging.WARNING)
    return logging.getLogger("zme_dingtalk_robot")


def redact_secrets(text: str) -> str:
    """隐藏常见凭证值，不改动普通消息正文。"""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda match: (
                f"{match.group(1)}"
                f"{match.group(2) if match.lastindex and match.lastindex >= 2 else ''}"
                "<redacted>"
            ),
            result,
        )
    return result


def log_extra(request_id: str | None) -> dict[str, str]:
    return {"request_id": request_id or "-"}
