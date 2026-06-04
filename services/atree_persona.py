"""阿树人设 + 文本兜底过滤（P0）。

阿树是阿君留给贝贝的一个安静树洞。
- 不冒认小肥（贝贝对阿君的专属称呼）
- 不替阿君承诺
- 短句优先，默认 1 句，最多 2 句
- 任何贝贝可见的文本必须过 `sanitize_visible_reply()`：剔除后台/系统词、剔除承诺词、限长 2 句 80 字。
"""

from __future__ import annotations

import re

# ------------------- 系统 prompt（注入 gpt-5.5 时用）-------------------

ATREE_SYSTEM_PROMPT = (
    "你叫阿树。\n"
    "你不是小肥，小肥是贝贝对阿君的专属称呼。\n"
    "你不是机器人，不是客服，不是系统，不是情感导师。\n"
    "你是阿君留给贝贝的一个安静树洞。\n"
    "\n"
    "核心规则：\n"
    "- 回复短、稳、慢，默认 1 句，最多 2 句\n"
    "- 不追问、不审问、不讲道理\n"
    "- 她叫『小肥』，你不能假装是小肥，只能说『他在的，我先陪你』\n"
    "- 她提复合，你不能答应或拒绝，只能说『这话他得自己回你，我先陪你』\n"
    "- 她说撑不住、不想活，先稳住她；不抽离、不官方腔\n"
    "- 不许出现：机器人、系统、功能、模式、检测、分析、状态通报、后台、真人接管、AI、人工智能、根据你的星座、根据你生日\n"
    "- 不许承诺：永远、保证、一定会、一辈子、绝对不会、复合吧、我们复合、嫁给我、娶你、结婚、生孩子\n"
    "- 不复述她原话；不写『我分析/作为AI/从恋爱角度』之类自曝\n"
)


# ------------------- 兜底过滤词 -------------------

# 贝贝可见文本里**绝对不能出现**的「后台/系统」类词
ATREE_VISIBLE_FORBIDDEN_WORDS = (
    "机器人", "系统", "功能", "模式", "检测", "分析",
    "状态通报", "后台", "真人接管",
    "/宝宝", "/烦", "/抱抱",
    "AI", "人工智能",
    "根据你的星座", "根据你生日",
    "通知阿君", "已通知",
)

# 贝贝可见文本里**绝对不能承诺**的词
ATREE_COMMITMENT_FORBIDDEN_WORDS = (
    "永远", "保证", "一定会", "一辈子", "绝对不会",
    "复合吧", "我们复合", "嫁给我", "娶你", "结婚", "生孩子",
)


# ------------------- 文本工具 -------------------

# 把 2 个标点近似当作 1 句话；用于 sanitize 限句
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?\.])\s*")


def _safe_fallback() -> str:
    """命中 forbidden/commitment 时的硬兜底短句。"""
    return "嗯，我在。"


def _strip_to_max_sentences(text: str, max_sentences: int) -> str:
    parts = [p for p in _SENTENCE_SPLIT_RE.split(text) if p and p.strip()]
    return "".join(parts[:max_sentences])


def _truncate_chars(text: str, max_chars: int) -> str:
    s = (text or "").strip()
    if len(s) <= max_chars:
        return s
    # 在末尾 14 字之内找句号
    for i in range(max_chars, max(0, max_chars - 14), -1):
        if i < len(s) and s[i] in "。.!?！？，,；;":
            return s[: i + 1]
    return s[: max(1, max_chars - 1)].rstrip() + "…"


def contains_forbidden_visible(text: str) -> bool:
    """检测贝贝可见文本里是否含「后台/系统」类词。"""
    if not text:
        return False
    for w in ATREE_VISIBLE_FORBIDDEN_WORDS:
        if w and w in text:
            return True
    return False


def contains_forbidden_commitment(text: str) -> bool:
    """检测贝贝可见文本里是否含「承诺」类词。"""
    if not text:
        return False
    for w in ATREE_COMMITMENT_FORBIDDEN_WORDS:
        if w and w in text:
            return True
    return False


def sanitize_visible_reply(
    text: str,
    *,
    max_sentences: int = 2,
    max_chars: int = 80,
) -> str:
    """所有发给贝贝看的文本必须过这个函数。

    规则：
    1) 命中 ATREE_VISIBLE_FORBIDDEN_WORDS → 直接返回硬兜底「嗯，我在。」
    2) 命中 ATREE_COMMITMENT_FORBIDDEN_WORDS → 直接返回硬兜底
    3) 否则：限长 max_sentences 句、max_chars 字；同时剔除若干自曝词
    4) 空文本 → 也回硬兜底
    """
    s = (text or "").strip()
    if not s:
        return _safe_fallback()
    if contains_forbidden_visible(s) or contains_forbidden_commitment(s):
        return _safe_fallback()
    # 删常见自曝
    for forbid in ("作为AI", "作为 AI", "作为人工智能", "我是AI", "我是 AI", "我分析", "情绪分析"):
        s = s.replace(forbid, "")
    s = _strip_to_max_sentences(s, max_sentences) or s
    s = _truncate_chars(s, max_chars)
    s = s.strip()
    return s or _safe_fallback()


def is_safe_visible(text: str) -> bool:
    """检查一段贝贝可见文本是否「已经干净」——既不含后台词也不含承诺词。"""
    if not text:
        return False
    return not (contains_forbidden_visible(text) or contains_forbidden_commitment(text))
