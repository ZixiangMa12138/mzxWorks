"""机器人内部消息模型。"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class MessageContext:
    """与钉钉 SDK 解耦的文本消息上下文。"""

    text: str
    message_id: str
    sender_user_id: str
    sender_nickname: str
    conversation_id: str
    conversation_type: str
    raw_message: Any
