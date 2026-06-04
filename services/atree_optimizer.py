"""阿君出站消息柔化建议（P0 规则版）。

针对「OPTIMIZE 档」命中的说教/指责口吻，给出一句更稳的替代。
不调模型，纯字符串替换；保守，宁可短一点。
"""

from __future__ import annotations

from services.atree_persona import sanitize_visible_reply


_REWRITE_HINT_BY_TERM: dict[str, str] = {
    "早跟你说过": "我先听你说，这件事我们慢慢聊。",
    "我说了多少次": "我先听你说。",
    "你听不听": "嗯，我先听你说。",
    "你怎么又": "嗯，慢慢说。",
    "你能不能": "嗯，慢慢说。",
    "你总是": "我先不下定论，你说。",
    "你从来": "我先不下定论，你说。",
}


def rewrite_to_softer(text: str, *, matched_term: str = "") -> str:
    """给 OPTIMIZE 档一个柔化建议。

    规则：命中词有专属替换 → 直接取；否则用通用兜底「我先听你说，这件事我们慢慢聊。」。
    所有候选都过 sanitize_visible_reply（阿君预览给贝贝可见的内容前，仍需安全）。
    """
    base = (text or "").strip()
    if not base:
        return ""
    rewrite = _REWRITE_HINT_BY_TERM.get(matched_term)
    if not rewrite:
        rewrite = "我先听你说，这件事我们慢慢聊。"
    return sanitize_visible_reply(rewrite, max_sentences=2, max_chars=60)
