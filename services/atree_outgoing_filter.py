"""阿君出站消息四档过滤（P0 规则版）。

输入：阿君写的草稿（贝贝即将看到）。
输出：tier ∈ {"send", "optimize", "cooldown", "block"} + 可选 suggested_text + reason

规则（按优先级 高→低）：
- BLOCK：含承诺/金钱关键词 → 阻断发送
- COOLDOWN：含冷暴力/情绪化关键词 → 建议先冷一会儿
- OPTIMIZE：含说教/指责口吻 → 提供柔化建议
- SEND：默认放行

只是「review 结果」；是否真正阻断由调用方决定（spec 允许第一版规则版不强行拦截所有出站
消息，除非现有架构已有安全接入点）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 承诺/金钱/重大关系 → 阻断
BLOCK_TERMS = (
    "复合", "永远", "保证", "一定会", "一辈子", "绝对不",
    "嫁给我", "娶你", "结婚", "生孩子",
    "我养你", "转账", "打钱", "借钱", "还钱", "信任", "以后怎么办",
)

# 冷暴力/情绪化 → 冷却
COOLDOWN_TERMS = (
    "滚", "闭嘴", "别烦我", "烦死了", "拉黑吧", "删了吧",
    "随你", "呵呵", "懒得说", "不想理你", "分了吧",
)

# 说教/指责口吻 → 柔化
OPTIMIZE_TERMS = (
    "早跟你说过", "我说了多少次", "你听不听",
    "你怎么又", "你能不能", "你总是", "你从来",
)


# tier 常量
TIER_SEND = "send"
TIER_OPTIMIZE = "optimize"
TIER_COOLDOWN = "cooldown"
TIER_BLOCK = "block"


@dataclass
class OutgoingReview:
    tier: str                   # send / optimize / cooldown / block
    matched_term: str           # 命中的词；send 时为空
    reason: str                 # 给阿君看的简短理由
    suggested_text: str | None  # optimize 时可能给出柔化建议


def _first_match(text: str, terms) -> str | None:
    for t in terms:
        if t and t in text:
            return t
    return None


def review_outgoing(text: str) -> OutgoingReview:
    s = (text or "").strip()
    if not s:
        return OutgoingReview(TIER_SEND, "", "空文本", None)

    # block 优先
    hit = _first_match(s, BLOCK_TERMS)
    if hit:
        return OutgoingReview(
            TIER_BLOCK,
            hit,
            f"命中阻断词「{hit}」：涉及承诺/金钱/重大关系，先别发。",
            None,
        )

    # cooldown
    hit = _first_match(s, COOLDOWN_TERMS)
    if hit:
        return OutgoingReview(
            TIER_COOLDOWN,
            hit,
            f"命中冷暴力/情绪词「{hit}」：先冷一会儿再回。",
            None,
        )

    # optimize
    hit = _first_match(s, OPTIMIZE_TERMS)
    if hit:
        from services.atree_optimizer import rewrite_to_softer
        suggested = rewrite_to_softer(s, matched_term=hit)
        return OutgoingReview(
            TIER_OPTIMIZE,
            hit,
            f"命中说教/指责口吻「{hit}」：建议先柔化。",
            suggested,
        )

    return OutgoingReview(TIER_SEND, "", "通过", None)
