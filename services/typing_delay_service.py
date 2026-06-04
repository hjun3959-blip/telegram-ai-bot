"""人性化输入延迟。

目的：在真正 send_message/send_sticker 之前 sleep 一会儿，模拟真人打字节奏，
解决“机器人回得太快”的问题。期间持续刷 ChatAction.TYPING，让对方看到“正在输入”。

设计要点：
- delay = clamp(MIN + len(reply_text) * PER_CHAR + jitter, MIN, MAX)
- jitter 是 ±BUSINESS_REPLY_DELAY_JITTER * delay 的随机扰动，让节奏不机械
- 仅 business 模式默认延迟；private（功能区）默认 0
- 期间 typing action 每 4.5s 续一次（Telegram typing 状态约 5s 过期）
- 任何延迟相关异常都吞掉，不影响主流程
- delay==0 时直接 return，零开销
- 纯函数 compute_human_delay 可单测，不依赖事件循环
"""

from __future__ import annotations

import asyncio
import random

from aiogram import Bot
from aiogram.enums import ChatAction

from config import (
    BUSINESS_REPLY_DELAY_JITTER,
    BUSINESS_REPLY_DELAY_MAX,
    BUSINESS_REPLY_DELAY_MIN,
    BUSINESS_REPLY_DELAY_PER_CHAR,
    PRIVATE_REPLY_DELAY_MAX,
    PRIVATE_REPLY_DELAY_MIN,
)
from services.chat_action_service import send_chat_action_safe
from utils.logger import setup_logging

logger = setup_logging()

# typing 状态过期约 5s；4.5s 续一次更稳。
_TYPING_REFRESH_INTERVAL = 4.5


def compute_human_delay(
    text_length: int,
    *,
    mode: str = "business",
    has_sticker_only: bool = False,
    rng: random.Random | None = None,
) -> float:
    """根据回复文本长度 + 场景计算延迟（秒）。

    参数:
        text_length: 即将发送的 reply_text 字符数；仅贴纸时传 0，并设 has_sticker_only=True。
        mode: "business" / "private"。private 默认 0 延迟。
        has_sticker_only: True 表示只发贴纸，没文字；用更短的范围。
        rng: 注入 random 实例，便于单测确定性。
    """
    if mode != "business":
        # private 功能区：默认 0，可由 PRIVATE_REPLY_DELAY_* 打开
        lo, hi = PRIVATE_REPLY_DELAY_MIN, PRIVATE_REPLY_DELAY_MAX
        if hi <= 0:
            return 0.0
        base = max(lo, min(hi, lo + max(0, text_length) * BUSINESS_REPLY_DELAY_PER_CHAR))
        return base

    lo = BUSINESS_REPLY_DELAY_MIN
    hi = BUSINESS_REPLY_DELAY_MAX
    if hi <= 0:
        return 0.0
    # 只发贴纸时上限收紧到 ~3s，且不按字符数线性增长
    if has_sticker_only:
        sticker_hi = min(hi, max(lo, 3.0))
        base = max(lo, min(sticker_hi, lo + 0.5))
    else:
        base = lo + max(0, text_length) * BUSINESS_REPLY_DELAY_PER_CHAR
        if base < lo:
            base = lo
        if base > hi:
            base = hi
    # jitter
    if BUSINESS_REPLY_DELAY_JITTER > 0:
        r = rng or random
        jitter = base * BUSINESS_REPLY_DELAY_JITTER
        base = base + r.uniform(-jitter, jitter)
    # 最终再 clamp 一次，防止 jitter 把它推出范围
    if base < 0:
        base = 0.0
    if base > hi:
        base = hi
    if base < lo and not has_sticker_only:
        base = lo
    return base


async def human_typing_delay(
    bot: Bot,
    chat_id: int,
    reply_text: str,
    *,
    mode: str = "business",
    has_sticker_only: bool = False,
    business_connection_id: str | None = None,
) -> None:
    """睡眠拟真延迟；期间持续刷 typing 状态。

    任何异常都吞掉。delay 计算见 compute_human_delay。
    """
    try:
        delay = compute_human_delay(
            len((reply_text or "")),
            mode=mode,
            has_sticker_only=has_sticker_only,
        )
        if delay <= 0:
            return
        # 把延迟拆成 typing-refresh 周期
        remaining = delay
        while remaining > 0:
            await send_chat_action_safe(
                bot,
                chat_id,
                ChatAction.TYPING,
                business_connection_id=business_connection_id,
            )
            chunk = min(_TYPING_REFRESH_INTERVAL, remaining)
            await asyncio.sleep(chunk)
            remaining -= chunk
        logger.debug(
            "human_typing_delay done | mode=%s | len=%s | delay=%.2fs | chat_id=%s",
            mode, len(reply_text or ""), delay, chat_id,
        )
    except Exception as e:
        # 拟真层失败绝不能阻断主流程
        logger.debug("human_typing_delay swallowed | err=%s", e)
