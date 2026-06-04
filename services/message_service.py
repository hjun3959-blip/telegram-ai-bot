import json
from datetime import datetime

from aiogram.types import Message

from db.core import execute
from services.context_service import get_chat_mode
from utils.logger import setup_logging

logger = setup_logging()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def store_message(message: Message, direction: str, content_text: str, content_type: str, scope: str = "default"):
    payload = json.dumps(
        {
            "chat_id": str(message.chat.id),
            "user_id": str(message.from_user.id),
            "username": message.from_user.username,
            "mode": get_chat_mode(message),
            "chat_type": message.chat.type,
            "message_id": str(message.message_id),
            "business_connection_id": str(getattr(message, "business_connection_id", "") or ""),
            "caption": getattr(message, "caption", None),
        },
        ensure_ascii=False,
    )
    logger.info(
        "message_log insert | direction=%s | mode=%s | chat_id=%s | user_id=%s | content_type=%s",
        direction,
        get_chat_mode(message),
        str(message.chat.id),
        str(message.from_user.id),
        content_type,
    )
    await execute(
        """
        INSERT INTO message_log(ts, scope, chat_id, user_id, username, direction, mode, content_type, content_text, raw_json)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_str(),
            scope,
            str(message.chat.id),
            str(message.from_user.id),
            (message.from_user.username or ""),
            direction,
            get_chat_mode(message),
            content_type,
            content_text,
            payload,
        ),
    )

