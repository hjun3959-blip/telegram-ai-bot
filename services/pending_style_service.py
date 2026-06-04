"""Pending style selection store（私信功能区）。

当用户在私信里点了某个图片/娱乐功能的「风格按钮」时，把 (tool, style) 暂存到
这里；下一条消息（照片或描述）到达时自动消费，触发对应生成。这样用户不用再复制
命令、不用再敲风格词。

设计要点：
- key = user_id（int）。同一用户后来的选择覆盖前一条。
- TTL 默认 10 分钟；超时自动失效。
- 容量上限 256，超出时 LRU 淘汰最早的一条。
- 仅 in-memory；进程重启就丢，避免长期误触发。
- 不读写任何密钥；不依赖 Telegram/OpenAI。
- 仅 private 路由使用；business 路由不会调用。
"""

from __future__ import annotations

import time
from dataclasses import dataclass


_PENDING_TTL_SECONDS = 10 * 60
_PENDING_MAX_USERS = 256


@dataclass
class PendingStyle:
    user_id: int
    tool: str
    style: str
    created_at: float


_store: dict[int, PendingStyle] = {}


def _now() -> float:
    return time.time()


def _evict_expired() -> None:
    cutoff = _now() - _PENDING_TTL_SECONDS
    expired = [uid for uid, p in _store.items() if p.created_at < cutoff]
    for uid in expired:
        _store.pop(uid, None)


def set_pending_style(user_id: int, tool: str, style: str) -> None:
    """暂存用户最近一次的风格选择。同一用户重复调用覆盖。"""
    if not user_id or not tool or not style:
        return
    _evict_expired()
    if len(_store) >= _PENDING_MAX_USERS and user_id not in _store:
        try:
            oldest = min(_store, key=lambda u: _store[u].created_at)
            _store.pop(oldest, None)
        except Exception:
            pass
    _store[user_id] = PendingStyle(
        user_id=user_id,
        tool=tool,
        style=style,
        created_at=_now(),
    )


def get_pending_style(user_id: int) -> PendingStyle | None:
    """读取但不弹出。过期返回 None。"""
    if not user_id:
        return None
    _evict_expired()
    return _store.get(user_id)


def consume_pending_style(user_id: int) -> PendingStyle | None:
    """读取并弹出。过期返回 None。"""
    if not user_id:
        return None
    _evict_expired()
    return _store.pop(user_id, None)


def clear_pending_style(user_id: int) -> None:
    _store.pop(user_id, None)


def pending_size() -> int:
    """测试/调试用。"""
    _evict_expired()
    return len(_store)
