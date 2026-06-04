import asyncio

from aiogram import Bot, Dispatcher
from aiogram.types import BusinessConnection

from config import TELEGRAM_TOKEN
from db.core import close_db, init_db
from routers.business import router as business_router
from routers.media import router as media_router
from routers.private import router as private_router
from services.context_service import register_business_connection
from services.daily_joke_scheduler import DailyJokeScheduler
from utils.logger import setup_logging

logger = setup_logging()


async def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("缺少 TELEGRAM_TOKEN 环境变量")
    await init_db()
    bot = Bot(token=TELEGRAM_TOKEN)
    dp = Dispatcher()

    # business_connection update：owner 启用/更新/解除 Business 绑定时会推送到这里。
    # 记录 connection_id -> owner_user_id 映射，后续 business_message 里用它作为
    # “当前消息是不是阿君自发”的权威依据，不再仅靠几秒静默窗口。
    @dp.business_connection()
    async def _on_business_connection(connection: BusinessConnection):
        register_business_connection(connection)

    dp.include_router(private_router)
    dp.include_router(business_router)
    dp.include_router(media_router)
    logger.info("bot startup | routers=private,business,media")

    # 每天一个笑话：内部定时任务。不阻塞 polling；shutdown 时 await stop()。
    # config 里 DAILY_JOKE_ENABLED=False 时 start() 直接 noop。
    daily_joke_scheduler = DailyJokeScheduler(bot)
    daily_joke_scheduler.start()

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception as e:
        logger.exception("polling failed | err=%s", e)
        raise
    finally:
        # 先停定时任务，避免 polling 退出后还想用 bot 发消息
        try:
            await daily_joke_scheduler.stop()
        except Exception as e:
            logger.warning("daily_joke_scheduler stop failed in finally | err=%s", e)
        # polling 结束后优雅关闭全局 DB 连接
        try:
            await close_db()
        except Exception as e:
            logger.warning("close_db failed in finally | err=%s", e)


if __name__ == "__main__":
    asyncio.run(main())
