"""核心窗口策略（贝贝 + 阿君）：窗口防爆，但**不换模型**。

四档：
  - normal     : ratio < 0.60      → 不动
  - summarize  : 0.60 <= ratio < 0.85 → 折叠早期消息为摘要（调用方决定怎么摘）
  - truncate   : 0.85 <= ratio < 0.95 → 截断到 CORE_RECENT_MESSAGES_TIGHT 条
  - emergency  : ratio >= 0.95     → 直接出固定短回（贝贝 sanitize；阿君明示状态）

仅做策略层；真正的摘要/截断/发送由调用方做。
"""

from __future__ import annotations

from dataclasses import dataclass

from services.atree_models import (
    CORE_RECENT_MESSAGES_NORMAL,
    CORE_RECENT_MESSAGES_TIGHT,
    WINDOW_EMERGENCY_AT,
    WINDOW_SUMMARIZE_AT,
    WINDOW_TRUNCATE_AT,
)
from services.atree_persona import sanitize_visible_reply

# 估算上限：保守按字符近似 token；约 1.6 chars/token（中英文混合）
# 真实生产可以替换为 tiktoken；这里只要单调可比、不依赖网络。
_DEFAULT_TOKEN_BUDGET = 8000
_CHARS_PER_TOKEN = 1.6


@dataclass
class WindowDecision:
    level: str               # normal / summarize / truncate / emergency
    ratio: float             # 估算占比
    keep_recent: int         # 应保留的最近消息条数
    need_summary: bool       # 是否需要做摘要折叠
    emergency_reply: str     # emergency 时给调用方的兜底短回（贝贝侧已 sanitize）


def estimate_tokens(messages: list[dict] | None) -> int:
    """近似估算消息总 token 量。messages 形如 [{"role": ..., "content": "..."}]"""
    if not messages:
        return 0
    total_chars = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total_chars += len(c)
        elif isinstance(c, list):
            # vision payload：list[{type:text, text:...}, ...]
            for part in c:
                if isinstance(part, dict):
                    t = part.get("text") or ""
                    if isinstance(t, str):
                        total_chars += len(t)
    return int(total_chars / _CHARS_PER_TOKEN)


def _emergency_for_beibei() -> str:
    """贝贝端 emergency：仍像「人」，不暴露后台词；sanitize 保平安。"""
    return sanitize_visible_reply("嗯，我先在这。等一下慢慢说。")


def _emergency_for_owner() -> str:
    """阿君端 emergency：明示状态，不糊弄。"""
    return (
        "当前窗口已满 / 模型异常，我先把上下文压缩一下再继续。"
        "如果着急，告诉我重点，我重新接。"
    )


def decide(
    messages: list[dict] | None,
    *,
    token_budget: int = _DEFAULT_TOKEN_BUDGET,
    for_owner: bool = False,
) -> WindowDecision:
    """根据 messages 估算占比，返回处理决策。

    for_owner=True 时 emergency 用阿君文案；否则用贝贝文案（sanitize 过）。
    """
    used = estimate_tokens(messages)
    ratio = used / token_budget if token_budget > 0 else 0.0
    if ratio >= WINDOW_EMERGENCY_AT:
        return WindowDecision(
            level="emergency",
            ratio=ratio,
            keep_recent=CORE_RECENT_MESSAGES_TIGHT,
            need_summary=True,
            emergency_reply=_emergency_for_owner() if for_owner else _emergency_for_beibei(),
        )
    if ratio >= WINDOW_TRUNCATE_AT:
        return WindowDecision(
            level="truncate",
            ratio=ratio,
            keep_recent=CORE_RECENT_MESSAGES_TIGHT,
            need_summary=True,
            emergency_reply="",
        )
    if ratio >= WINDOW_SUMMARIZE_AT:
        return WindowDecision(
            level="summarize",
            ratio=ratio,
            keep_recent=CORE_RECENT_MESSAGES_NORMAL,
            need_summary=True,
            emergency_reply="",
        )
    return WindowDecision(
        level="normal",
        ratio=ratio,
        keep_recent=CORE_RECENT_MESSAGES_NORMAL,
        need_summary=False,
        emergency_reply="",
    )


def apply(messages: list[dict] | None, decision: WindowDecision) -> list[dict]:
    """按 decision.keep_recent 简单截断；保留首条 system（如有）。

    摘要的真活由 caller 用 LLM 自行完成；这里只做无网络的本地截断兜底，确保不抛错。
    """
    if not messages:
        return []
    head: list[dict] = []
    if messages and messages[0].get("role") == "system":
        head = [messages[0]]
        rest = messages[1:]
    else:
        rest = list(messages)
    if decision.keep_recent <= 0:
        return head
    if len(rest) <= decision.keep_recent:
        return head + rest
    return head + rest[-decision.keep_recent:]
