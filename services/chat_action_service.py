"""Chat Action 发送封装。

目的：
- 在调用 LLM 前给对方一个“正在输入/上传/录音”的拟真状态
- 兼容 Telegram Business：能透传 business_connection_id 时透传，否则降级不抛
- aiogram 版本不支持 business_connection_id 参数时 try/except 降级
- 任何失败都吞掉，不阻断主流程

用法：
    from aiogram.enums import ChatAction
    await send_chat_action_safe(bot, chat_id, ChatAction.TYPING, business_connection_id=...)
"""

from __future__ import annotations

from aiogram import Bot

from utils.logger import setup_logging

logger = setup_logging()


async def send_chat_action_safe(
    bot: Bot,
    chat_id: int,
    action: str,
    business_connection_id: str | None = None,
) -> None:
    """发送 chat action；任何异常都吞掉。

    优先尝试带 business_connection_id 的调用；如果 aiogram 版本不支持该参数（TypeError），
    自动降级为不带 business_connection_id 的调用。其它异常仅记日志。
    """
    if not action:
        return
    try:
        if business_connection_id:
            try:
                await bot.send_chat_action(
                    chat_id=chat_id,
                    action=action,
                    business_connection_id=business_connection_id,
                )
                return
            except TypeError:
                # 老版本 aiogram 不支持 business_connection_id 关键字
                logger.debug("send_chat_action does not support business_connection_id, falling back")
        await bot.send_chat_action(chat_id=chat_id, action=action)
    except Exception as e:
        # business 上下文里不带 business_connection_id 调用可能被 Telegram 拒，吞掉即可
        logger.debug("send_chat_action failed | chat_id=%s | action=%s | err=%s", chat_id, action, e)
