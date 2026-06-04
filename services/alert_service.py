from aiogram import Bot

from config import OWNER_CHAT_IDS
from utils.logger import setup_logging

logger = setup_logging()
_alert_sent_cache: set[str] = set()


async def alert_owner(bot: Bot, text: str):
    if not OWNER_CHAT_IDS:
        logger.warning("owner alert skipped: OWNER_CHAT_IDS is empty")
        return None
    for chat_id in OWNER_CHAT_IDS:
        try:
            await bot.send_message(int(chat_id), text)
        except Exception as e:
            logger.exception("owner alert failed | chat_id=%s | err=%s", chat_id, e)
    return None


async def dedup_alert(bot: Bot, key: str, text: str):
    if key in _alert_sent_cache:
        return
    _alert_sent_cache.add(key)
    await alert_owner(bot, text)
