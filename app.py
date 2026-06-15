import asyncio
import os
from typing import Optional

from aiogram import Bot, Dispatcher

from config import ADMIN_AGENT_ENABLED, TELEGRAM_TOKEN
from db.core import close_db, init_db
from routers.admin_agent import router as admin_agent_router
from routers.shannon_chat import router as shannon_chat_router
from utils.logger import setup_logging

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

logger = setup_logging()


async def _warmup_memory() -> None:
    try:
        from memory.conscious_memory import get_memory
        mem = await get_memory()
        await mem._ensure_ready()
        logger.info("memory warmup done")
    except Exception as e:
        logger.warning("memory warmup failed (non-fatal) | err=%s", e)


async def _shutdown(bot: Optional[Bot], db_inited: bool) -> None:
    if db_inited:
        try:
            await close_db()
        except Exception as e:
            logger.warning("db close failed | err=%s", e)
    if bot is not None:
        try:
            await bot.close()
        except Exception as e:
            logger.warning("bot close failed | err=%s", e)


async def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("缺少 TELEGRAM_TOKEN，无法启动")

    bot: Optional[Bot] = None
    db_inited = False

    try:
        await init_db()
        db_inited = True

        asyncio.create_task(_warmup_memory())

        bot = Bot(token=TELEGRAM_TOKEN)
        dp = Dispatcher()

        if ADMIN_AGENT_ENABLED:
            # shannon_chat 先注册（优先级高），admin_agent 兜底
            dp.include_router(shannon_chat_router)
            dp.include_router(admin_agent_router)

        logger.info("xinxue-bot startup | admin_agent=%s | shannon_chat=enabled", ADMIN_AGENT_ENABLED)

        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("startup/polling failed | err=%s", e)
        raise
    finally:
        await _shutdown(bot, db_inited)


if __name__ == "__main__":
    asyncio.run(main())
