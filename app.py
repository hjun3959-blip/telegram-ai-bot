import asyncio
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.types import BusinessConnection

from config import (
    ADMIN_AGENT_ENABLED,
    OWNER_MENU_ENABLED,
    OXAPAY_WEBHOOK_ENABLED,
    TELEGRAM_TOKEN,
)
from db.core import close_db, init_db
from routers.admin_agent import router as admin_agent_router
from routers.business import router as business_router
from routers.media import router as media_router
from routers.mingli import router as mingli_router
from routers.owner_menu import router as owner_menu_router
from routers.private import router as private_router
from routers.rstory import router as rstory_router
from services.automation_scheduler import AutomationScheduler
from services.context_service import register_business_connection
from services.daily_joke_scheduler import DailyJokeScheduler
from services.rstory_store import close_store as close_rstory_store
from services.rstory_store import init_store as init_rstory_store
from services.rstory_webhook import start_webhook_server, stop_webhook_server
from utils.logger import setup_logging

logger = setup_logging()


async def _on_business_connection(connection: BusinessConnection) -> None:
    """Handle BusinessConnection updates and register the connection."""
    try:
        await register_business_connection(connection)
    except Exception as e:
        conn_id = getattr(connection, "id", None)
        logger.exception(
            "register_business_connection failed | connection_id=%s | err=%s",
            conn_id,
            e,
        )


async def _shutdown(
    bot: Optional[Bot],
    webhook_runner: Optional[object],
    db_inited: bool,
    rstory_inited: bool,
) -> None:
    """Unified resource cleanup to avoid repetition across error/normal paths."""
    cleanup_tasks = [
        (stop_webhook_server(webhook_runner), "webhook"),
    ]
    if db_inited:
        cleanup_tasks.append((close_db(), "db"))
    if rstory_inited:
        cleanup_tasks.append((close_rstory_store(), "rstory"))
    if bot is not None:
        cleanup_tasks.append((bot.close(), "bot"))

    for coro, name in cleanup_tasks:
        try:
            await coro
        except Exception as e:
            logger.warning("%s cleanup failed | err=%s", name, e)


async def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("缺少 TELEGRAM_TOKEN 环境变量，无法启动")

    bot: Optional[Bot] = None
    webhook_runner = None
    db_inited = False
    rstory_inited = False
    daily_joke_scheduler: Optional[DailyJokeScheduler] = None
    automation_scheduler: Optional[AutomationScheduler] = None

    try:
        await init_db()
        db_inited = True

        await init_rstory_store()
        rstory_inited = True

        bot = Bot(token=TELEGRAM_TOKEN)
        dp = Dispatcher()

        dp.business_connection()(_on_business_connection)

        # 命理路由优先于其他私信路由，避免 FSM 状态被拦截
        dp.include_router(mingli_router)

        dp.include_router(rstory_router)

        if ADMIN_AGENT_ENABLED:
            dp.include_router(admin_agent_router)

        if OWNER_MENU_ENABLED:
            dp.include_router(owner_menu_router)

        dp.include_router(private_router)
        dp.include_router(business_router)
        dp.include_router(media_router)

        logger.info(
            "bot startup | routers=mingli,rstory,%s%sprivate,business,media",
            "admin_agent," if ADMIN_AGENT_ENABLED else "",
            "owner_menu," if OWNER_MENU_ENABLED else "",
        )

        daily_joke_scheduler = DailyJokeScheduler(bot)
        daily_joke_scheduler.start()
        
        automation_scheduler = AutomationScheduler()
        automation_scheduler.start()

        if OXAPAY_WEBHOOK_ENABLED:
            try:
                webhook_runner = await start_webhook_server()
            except Exception as e:
                logger.exception("oxapay webhook server start failed | err=%s", e)
                raise

        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        except asyncio.CancelledError:
            logger.debug("polling cancelled")
            raise
        except Exception as e:
            logger.exception("polling failed | err=%s", e)
            raise
        finally:
            if daily_joke_scheduler is not None:
                try:
                    await daily_joke_scheduler.stop()
                except Exception as e:
                    logger.warning("daily_joke_scheduler stop failed | err=%s", e)
            if automation_scheduler is not None:
                try:
                    await automation_scheduler.stop()
                except Exception as e:
                    logger.warning("automation_scheduler stop failed | err=%s", e)

    finally:
        await _shutdown(bot, webhook_runner, db_inited, rstory_inited)


if __name__ == "__main__":
    asyncio.run(main())
