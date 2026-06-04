"""阿树通知去重（按 severity）。

低/中：30 分钟同种 intent 内不重复
高：5 分钟内不重复
critical：60 秒内不重复
"""

from __future__ import annotations

import time

# severity → TTL（秒）
_TTL_BY_SEVERITY: dict[str, int] = {
    "critical": 60,
    "high": 5 * 60,
    "medium": 30 * 60,
    "low": 30 * 60,
}


_DEFAULT_TTL = 30 * 60


def _ttl_for(severity: str) -> int:
    return _TTL_BY_SEVERITY.get((severity or "").lower(), _DEFAULT_TTL)


# user_id -> intent -> last_at
_last_alert_at: dict[int, dict[str, float]] = {}


def should_alert(user_id: int, intent: str, severity: str) -> bool:
    """是否允许这一次发通知。"""
    if not user_id or not intent:
        return False
    now = time.time()
    by_intent = _last_alert_at.get(user_id) or {}
    last = by_intent.get(intent, 0.0)
    if now - last < _ttl_for(severity):
        return False
    by_intent[intent] = now
    _last_alert_at[user_id] = by_intent
    return True


def reset(user_id: int | None = None) -> None:
    """测试/调试用。user_id=None 时清掉所有。"""
    if user_id is None:
        _last_alert_at.clear()
        return
    _last_alert_at.pop(user_id, None)
