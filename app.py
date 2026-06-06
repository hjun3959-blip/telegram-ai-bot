import asyncio

from aiogram import Bot, Dispatcher
from aiogram.types import BusinessConnection

from config import ADMIN_AGENT_ENABLED, OXAPAY_WEBHOOK_ENABLED, TELEGRAM_TOKEN
from db.core import close_db, init_db
from routers.admin_agent import router as admin_agent_router
from routers.business import router as business_router
from routers.media import router as media_router
from routers.private import router as private_router
from routers.rstory import router as rstory_router
from services.context_service import register_business_connection
from services.daily_joke_scheduler import DailyJokeScheduler
from services.rstory_store import close_store as close_rstory_store
from services.rstory_store import init_store as init_rstory_store
from services.rstory_webhook import start_webhook_server, stop_webhook_server
from utils.logger import setup_logging

logger = setup_logging()


async def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("缺少 TELEGRAM_TOKEN 环境变量")
    await init_db()
    # 剧情系统独立存储：自包含建表，初始化与主库分开（互不影响）。
    await init_rstory_store()
    bot = Bot(token=TELEGRAM_TOKEN)
    dp = Dispatcher()

    # business_connection update：owner 启用/更新/解除 Business 绑定时会推送到这里。
    # 记录 connection_id -> owner_user_id 映射，后续 business_message 里用它作为
    # “当前消息是不是阿君自发”的权威依据，不再仅靠几秒静默窗口。
    @dp.business_connection()
    async def _on_business_connection(connection: BusinessConnection):
        register_business_connection(connection)

    # rstory 先于 private 注册：private 有 F.text 兜底 handler，会吞掉 /rstory；
    # rstory 用 Command/CallbackQuery 精确过滤，放前面优先命中，不影响其它命令。
    dp.include_router(rstory_router)
    # 管理员对话网关：owner-only + 私聊 only。必须在 private（含 F.text 兜底）之前注册，
    # 命令与「有活跃会话时的文本兜底」才能优先命中；ADMIN_AGENT_ENABLED=False 时其 handler
    # 内部硬门禁直接 return，等价 noop，不影响既有路由。
    if ADMIN_AGENT_ENABLED:
        dp.include_router(admin_agent_router)
    dp.include_router(private_router)
    dp.include_router(business_router)
    dp.include_router(media_router)
    logger.info(
        "bot startup | routers=rstory,%sprivate,business,media",
        "admin_agent," if ADMIN_AGENT_ENABLED else "",
    )

    # 每天一个笑话：内部定时任务。不阻塞 polling；shutdown 时 await stop()。
    # config 里 DAILY_JOKE_ENABLED=False 时 start() 直接 noop。
    daily_joke_scheduler = DailyJokeScheduler(bot)
    daily_joke_scheduler.start()

    # OxaPay 支付 Webhook：polling 没有现成 HTTP server，按需附带起一个最小 aiohttp server。
    # 仅当 RSTORY_PAYMENT_PROVIDER=oxapay（或 OXAPAY_WEBHOOK_ENABLED 强制）时启用。
    webhook_runner = None
    if OXAPAY_WEBHOOK_ENABLED:
        try:
            webhook_runner = await start_webhook_server()
        except Exception as e:
            logger.exception("oxapay webhook server start failed | err=%s", e)

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
        # 关闭 OxaPay Webhook server（若启用）
        try:
            await stop_webhook_server(webhook_runner)
        except Exception as e:
            logger.warning("stop_webhook_server failed in finally | err=%s", e)
        # polling 结束后优雅关闭全局 DB 连接
        try:
            await close_db()
        except Exception as e:
            logger.warning("close_db failed in finally | err=%s", e)
        # 关闭剧情系统独立存储连接
        try:
            await close_rstory_store()
        except Exception as e:
            logger.warning("close_rstory_store failed in finally | err=%s", e)


if __name__ == "__main__":
    asyncio.run(main())
