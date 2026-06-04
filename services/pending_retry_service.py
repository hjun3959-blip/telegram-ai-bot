"""图像任务「最近一次失败」记账（in-memory，TTL 30 分钟）。

用法：
- 图像 runner（/plog /magnet /y2k /poster /img /meme）在调用失败/超时时，
  调 set_failed_task(user_id, tool, style) 记下「上次干到一半挂了」。
- 用户问「图片呢」/ 发 /继续 / 点「再试一次」时，路由可以读到这条记录，
  给清晰回答 / 触发重试。
- 成功落屏后用 clear_task 清掉，避免误以为还在挂。
- TTL 30 分钟；超时自动失效。

字段：
- tool：plog/magnet/y2k/poster/img/meme 之一
- style：用户上次选的中文风格短词（可为空）
- status：'pending'（正在做，但尚未成功落屏）/ 'failed'（明确失败）
- failed_reason：可选，给用户看的中文摘要
- created_at：UTC epoch seconds
"""

from __future__ import annotations

import time
from dataclasses import dataclass

_TTL_SECONDS = 30 * 60
_MAX_USERS = 512


@dataclass
class RetryTask:
    user_id: int
    tool: str
    style: str
    status: str  # 'pending' | 'failed'
    failed_reason: str | None
    created_at: float


_store: dict[int, RetryTask] = {}


def _now() -> float:
    return time.time()


def _evict_expired() -> None:
    cutoff = _now() - _TTL_SECONDS
    expired = [uid for uid, t in _store.items() if t.created_at < cutoff]
    for uid in expired:
        _store.pop(uid, None)


def mark_started(user_id: int, tool: str, style: str = "") -> None:
    """生成开始时记一条 status='pending'。"""
    if not user_id or not tool:
        return
    _evict_expired()
    if len(_store) >= _MAX_USERS and user_id not in _store:
        try:
            oldest = min(_store, key=lambda u: _store[u].created_at)
            _store.pop(oldest, None)
        except Exception:
            pass
    _store[user_id] = RetryTask(
        user_id=user_id,
        tool=tool,
        style=(style or "").strip(),
        status="pending",
        failed_reason=None,
        created_at=_now(),
    )


def mark_failed(user_id: int, tool: str, style: str = "", reason: str | None = None) -> None:
    """生成失败/超时/落屏失败时升级为 status='failed'。"""
    if not user_id or not tool:
        return
    _evict_expired()
    _store[user_id] = RetryTask(
        user_id=user_id,
        tool=tool,
        style=(style or "").strip(),
        status="failed",
        failed_reason=(reason or None),
        created_at=_now(),
    )


def get_task(user_id: int) -> RetryTask | None:
    if not user_id:
        return None
    _evict_expired()
    return _store.get(user_id)


def clear_task(user_id: int) -> None:
    _store.pop(user_id, None)


def has_failed_task(user_id: int) -> bool:
    t = get_task(user_id)
    return bool(t and t.status == "failed")


def size() -> int:
    _evict_expired()
    return len(_store)
