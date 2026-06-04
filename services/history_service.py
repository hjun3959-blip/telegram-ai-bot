"""历史记录滑动截断工具。

设计目标：
- 既限制条数，又限制总字符数，避免 token 过大
- 不做摘要，只做轻量保留最近上下文
- 不破坏调用方对 list[dict] 的使用方式
- content 可能是 str 或 list（多模态），统一估算长度

两个入口：
- trim_history(history)：在写回 user_histories 时调用
- trim_messages(messages)：在调用 OpenAI 前对完整 messages（含 system / 当前 user）再 trim 一次

注意：system 与当前轮 user 永远保留，被截掉的只是中间历史。
"""

from __future__ import annotations

from typing import Any

from config import HISTORY_MAX_CHARS, HISTORY_MAX_MESSAGES


def _content_len(content: Any) -> int:
    """估算消息 content 的字符数，多模态时只统计文本部分。"""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                t = part.get("text")
                if isinstance(t, str):
                    total += len(t)
                # image_url 等不计入字符数
            elif isinstance(part, str):
                total += len(part)
        return total
    # 兜底
    try:
        return len(str(content))
    except Exception:
        return 0


def trim_history(
    history: list[dict],
    max_messages: int | None = None,
    max_chars: int | None = None,
) -> list[dict]:
    """对 user_histories 内的对话历史做双限制截断。

    - 先按条数尾部保留
    - 再从尾部往前累加字符数，超出预算就丢前面的
    - 返回新 list；不修改入参
    """
    if not history:
        return []
    max_msgs = max_messages if max_messages is not None else HISTORY_MAX_MESSAGES
    max_cs = max_chars if max_chars is not None else HISTORY_MAX_CHARS

    # 条数截断（保留最近）
    trimmed = list(history[-max_msgs:]) if max_msgs > 0 else list(history)

    if max_cs <= 0 or not trimmed:
        return trimmed

    # 字符截断（从尾往前累积）
    total = 0
    keep_from_end: list[dict] = []
    for msg in reversed(trimmed):
        clen = _content_len(msg.get("content"))
        if keep_from_end and total + clen > max_cs:
            break
        keep_from_end.append(msg)
        total += clen
    keep_from_end.reverse()
    return keep_from_end


def trim_messages(
    messages: list[dict],
    max_messages: int | None = None,
    max_chars: int | None = None,
) -> list[dict]:
    """在送给 OpenAI 前再 trim 一次：保留首个 system 与最后一条 user，截中间历史。

    - 不破坏 system 与当前轮 user
    - 中间的 user/assistant 走 trim_history 规则
    """
    if not messages:
        return []
    max_msgs = max_messages if max_messages is not None else HISTORY_MAX_MESSAGES
    max_cs = max_chars if max_chars is not None else HISTORY_MAX_CHARS

    system_msgs: list[dict] = []
    middle: list[dict] = []
    tail_user: list[dict] = []

    # 提取首个连续的 system 段
    idx = 0
    while idx < len(messages) and messages[idx].get("role") == "system":
        system_msgs.append(messages[idx])
        idx += 1

    # 末尾若是 user，则单独保留
    end = len(messages)
    if end > idx and messages[end - 1].get("role") == "user":
        tail_user = [messages[end - 1]]
        end -= 1

    middle = messages[idx:end]

    # 系统 + 末尾 user 已占用的字符
    fixed_chars = sum(_content_len(m.get("content")) for m in system_msgs + tail_user)
    remaining_budget = max(0, max_cs - fixed_chars) if max_cs > 0 else 0

    # 中间最多保留多少条
    remain_msg_budget = max(0, max_msgs - len(system_msgs) - len(tail_user)) if max_msgs > 0 else len(middle)

    trimmed_middle = trim_history(middle, max_messages=remain_msg_budget, max_chars=remaining_budget)
    return system_msgs + trimmed_middle + tail_user
