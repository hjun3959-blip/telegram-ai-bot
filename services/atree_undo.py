"""阿树「最近一条回复」记录 + 撤回提示（P0 stub）。

只做 in-memory 状态：阿树自己发给贝贝的最近一条短句，存 (chat_id, text, ts)。
- record_last_atree_reply(chat_id, text)
- get_last_atree_reply(chat_id) -> dict|None
- clear_last_atree_reply(chat_id)

第一版**不真撤回 Telegram 消息**（避免误操作），仅给 owner 调试用：知道阿树最后说了
什么，必要时阿君自己用 Telegram 客户端撤回。
"""

from __future__ import annotations

import time

# chat_id -> {"text": str, "ts": float}
_last_replies: dict[int, dict] = {}

_TTL_SECONDS = 30 * 60


def record_last_atree_reply(chat_id: int, text: str) -> None:
    if not chat_id:
        return
    _last_replies[int(chat_id)] = {"text": text or "", "ts": time.time()}


def get_last_atree_reply(chat_id: int) -> dict | None:
    rec = _last_replies.get(int(chat_id)) if chat_id else None
    if not rec:
        return None
    if time.time() - rec["ts"] > _TTL_SECONDS:
        _last_replies.pop(int(chat_id), None)
        return None
    return dict(rec)


def clear_last_atree_reply(chat_id: int) -> None:
    _last_replies.pop(int(chat_id), None)


def reset() -> None:
    _last_replies.clear()
