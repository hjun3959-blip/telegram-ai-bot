"""阿树给阿君的中性提醒文案（不向贝贝暴露）。

- 文案中性：不出现「状态通报 / 真人接管 / 阿树已…」
- 配合 atree_cooldown.should_alert 控制频率
- 配合 atree_privacy_filter.should_forward_original 决定是否带原话
"""

from __future__ import annotations

from services.atree_keyword_trigger import AtreeIntent
from services.atree_privacy_filter import safe_excerpt, should_forward_original


# intent → 一句中性的状态描述（不机械、不出现「状态通报」）
_INTENT_TO_LINE: dict[str, str] = {
    "crisis_support": "贝贝刚才在说撑不住，我先稳了一句。",
    "reconciliation": "贝贝刚才提到复合，我没替你答。",
    "relationship_risk": "贝贝刚说到关系/钱这边，我没替你说话。",
    "call_xiaofei": "贝贝刚在叫小肥。",
    "miss_xiaofei": "贝贝刚才说她想你。",
    "sad": "贝贝刚才有点低落。",
    "tired": "贝贝刚才说累，我先陪着。",
    "annoyed": "贝贝刚才有点烦。",
    "fallback": "贝贝刚发了一条，我先陪着。",
}


def _line_for(intent: str) -> str:
    return _INTENT_TO_LINE.get(intent, _INTENT_TO_LINE["fallback"])


def build_owner_notice(
    intent_obj: AtreeIntent | None,
    *,
    original_text: str = "",
    sender_label: str = "贝贝",
) -> str:
    """生成给阿君看的中性通知短文。

    - 默认只发 intent 中性描述 + 一句「我先回了句短的，没替你说话。」
    - 当 intent 允许 forward_original 时，附原话（截 80 字）

    输出从不被发给贝贝（路由层只走 alert_owner）；但仍刻意避免出现「状态通报」类词。
    """
    line = _line_for(intent_obj.intent if intent_obj else "fallback")
    parts = [line, "我先回了句短的，没替你说话。"]
    if should_forward_original(intent_obj, text=original_text) and original_text:
        excerpt = safe_excerpt(original_text)
        if excerpt:
            parts.append(f"她刚说：{excerpt}")
    return "\n".join(parts)


# 路由层使用：判断是否应该 alert + 生成 dedup key
def should_send_alert(user_id: int, intent_obj: AtreeIntent | None) -> bool:
    """组合 intent.notify_owner + cooldown 判定。"""
    if not intent_obj or not intent_obj.notify_owner:
        return False
    from services.atree_cooldown import should_alert
    return should_alert(user_id, intent_obj.intent, intent_obj.severity)
