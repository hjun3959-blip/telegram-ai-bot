"""管理员主脑（OpenAI）对话服务。

只服务 owner 的私人控制台。与普通私聊 / Business 完全隔离：
- 用独立的 ADMIN_BRAIN_SYSTEM_PROMPT；
- 走 call_openai(..., response_json=False) 拿纯自然语言（不强制 JSON），
  避免污染普通聊天那套 reply_text/sticker JSON 协议；
- 维护一小段 per-owner 的 in-process 对话历史，让连续对话有上下文。

不写任何密钥到日志，也不持久化对话内容（进程级内存，重启即清）。
"""

from __future__ import annotations

from collections import deque

from config import ADMIN_BRAIN_SYSTEM_PROMPT, CORE_MODEL
from services.openai_service import call_openai
from utils.logger import setup_logging

logger = setup_logging()

# 每个 owner 保留最近 N 轮（user+assistant 算 2 条）对话，限制 token / 内存占用。
_MAX_TURNS = 12
_history: dict[str, deque] = {}


def reset_history(owner_key: str | int) -> None:
    """退出会话或 owner 主动重置时清空历史。"""
    _history.pop(str(owner_key), None)


def _get_history(owner_key: str) -> deque:
    dq = _history.get(owner_key)
    if dq is None:
        dq = deque(maxlen=_MAX_TURNS)
        _history[owner_key] = dq
    return dq


async def ask_admin_brain(owner_key: str | int, user_text: str) -> str:
    """把 owner 的自然语言指令交给主脑，返回自然语言回复。

    owner_key：用于隔离不同 owner 的对话历史（一般是 user_id）。
    失败时返回一句友好的中文提示，不抛异常给上层 handler。
    """
    text = (user_text or "").strip()
    if not text:
        return "（主脑）你想聊点什么？直接把指令发我就行。"

    key = str(owner_key)
    history = _get_history(key)

    messages = [{"role": "system", "content": ADMIN_BRAIN_SYSTEM_PROMPT}]
    messages.extend(list(history))
    messages.append({"role": "user", "content": text})

    try:
        reply = await call_openai(
            messages,
            model=CORE_MODEL,
            mode="private",
            response_json=False,
            chat_id=f"admin_brain:{key}",
        )
    except Exception as e:
        logger.exception("admin brain call failed | err_type=%s", type(e).__name__)
        return "（主脑）刚才调用模型出错了，稍后再试一次。"

    reply = (reply or "").strip()
    if not reply:
        return "（主脑）我没接住，换个说法再发一次？"

    # 记录这一轮，供后续对话使用
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    return reply
