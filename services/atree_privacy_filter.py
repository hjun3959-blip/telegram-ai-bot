"""阿树隐私：决定通知里是否转给阿君「贝贝原话」。

默认普通情绪不转原话，只发中性提醒。
高风险 / 危机 / 复合 / 小肥 / 钱可转原话。
"""

from __future__ import annotations

from services.atree_keyword_trigger import AtreeIntent

# 可转原话的 intent 白名单
_FORWARD_ALLOWED_INTENTS = frozenset({
    "crisis_support",
    "reconciliation",
    "relationship_risk",
    "call_xiaofei",
})


def should_forward_original(intent_obj: AtreeIntent | None, *, text: str = "") -> bool:
    """是否在 owner 通知里附带贝贝原话。

    - intent 在 _FORWARD_ALLOWED_INTENTS 内 → 允许
    - 或 forward_original 显式 True（intent 自带标记）→ 允许
    - 其它情况 → 默认不转
    """
    if not intent_obj:
        return False
    if intent_obj.intent in _FORWARD_ALLOWED_INTENTS:
        return True
    return bool(intent_obj.forward_original)


def safe_excerpt(text: str, *, max_chars: int = 80) -> str:
    """要转原话时也别整段贴，截短到 80 字以内。"""
    s = (text or "").strip()
    if not s:
        return ""
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1].rstrip() + "…"
