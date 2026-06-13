"""普通用户私聊直接对话服务。

定位：
- 所有非 owner、非小胖、非特殊用户的私聊窗口默认走这里
- 不弹工具菜单；纯自然语言对话
- 支持 Qwen / ChatGPT 系列（通过 config.PUBLIC_CHAT_MODEL 切换）
- 每次对话前先 recall 语义记忆，注入 system prompt 作为上下文
- 对话结束后 remember 本轮内容
- 限流：同一用户 10 秒内最多 5 条，超过静默丢弃
- 人格：阿树（与 atree_persona 一致），不暴露 bot 身份技术细节
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque

from config import BACKUP_MODEL, OPENAI_API_KEY, OPENAI_BASE_URL, PUBLIC_CHAT_MODEL
from services.memory_vector_service import format_memory_context, recall, remember
from services.openai_service import call_openai

logger = logging.getLogger(__name__)

# ─── 限流 ────────────────────────────────────────────────────────────────────
_RATE_WINDOW = 10   # 秒
_RATE_LIMIT = 5     # 同窗口最多条数
_rate_buckets: dict[str, deque] = defaultdict(deque)


def _is_rate_limited(chat_id: str) -> bool:
    now = time.time()
    bucket = _rate_buckets[chat_id]
    while bucket and now - bucket[0] > _RATE_WINDOW:
        bucket.popleft()
    if len(bucket) >= _RATE_LIMIT:
        return True
    bucket.append(now)
    return False


# ─── System Prompt ───────────────────────────────────────────────────────────
_SYSTEM = """\
你是阿树，一个有温度、有个性的 AI 伙伴。
说话风格：自然、真诚、偶尔幽默，句子不要太长。
不主动暴露自己是 AI，用第一人称「我」回答。
如果不确定，直接说不知道，别胡编。
回复控制在 150 字以内，除非用户明确要长内容。
""".strip()


def _build_messages(
    user_text: str,
    memory_ctx: str,
    history: list[dict],
) -> list[dict]:
    system_content = _SYSTEM
    if memory_ctx:
        system_content += f"\n\n以下是你们之前聊过的内容（供参考）：\n{memory_ctx}"
    msgs: list[dict] = [{"role": "system", "content": system_content}]
    msgs.extend(history[-6:])  # 最近 3 轮对话
    msgs.append({"role": "user", "content": user_text})
    return msgs


# ─── 短期对话历史（in-memory，重启清零） ────────────────────────────────────
_short_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=6))


def _get_history(chat_id: str) -> list[dict]:
    return list(_short_history[chat_id])


def _push_history(chat_id: str, role: str, content: str) -> None:
    _short_history[chat_id].append({"role": role, "content": content})


# ─── 主入口 ──────────────────────────────────────────────────────────────────

async def public_chat_reply(
    chat_id: str | int,
    text: str,
    scope: str = "default",
) -> str | None:
    """给普通用户生成回复。返回文字字符串，或 None（被限流 / 空输入）。"""
    if not text or not text.strip():
        return None

    chat_id_str = str(chat_id)

    if _is_rate_limited(chat_id_str):
        logger.debug("public_chat: rate limited chat_id=%s", chat_id_str)
        return None

    # 1. 语义记忆检索
    hits = await recall(chat_id_str, text, scope=scope)
    memory_ctx = format_memory_context(hits)

    # 2. 构造 messages
    history = _get_history(chat_id_str)
    messages = _build_messages(text, memory_ctx, history)

    # 3. 调用模型（复用 openai_service 的重试/fallback 机制）
    model = PUBLIC_CHAT_MODEL or BACKUP_MODEL or "gpt-4o-mini"
    result = await call_openai(
        messages=messages,
        model=model,
        mode="private",
        response_json=False,   # 纯文本，不要 JSON 包装
        chat_id=chat_id_str,
    )

    reply: str
    if isinstance(result, dict):
        reply = result.get("reply_text") or ""
    else:
        reply = str(result or "")
    reply = reply.strip()

    if not reply:
        return None

    # 4. 写入短期 + 向量记忆
    _push_history(chat_id_str, "user", text)
    _push_history(chat_id_str, "assistant", reply)
    asyncio.create_task(remember(chat_id_str, text, role="user", scope=scope))
    asyncio.create_task(remember(chat_id_str, reply, role="bot", scope=scope))

    return reply
