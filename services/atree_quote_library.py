"""阿树安全回复池（intent → 一句短话）。

要求：池里每一条都必须能通过 atree_persona.is_safe_visible（无 forbidden / commitment 词），
夜间 22:00–06:00 给更安静的版本。
"""

from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

from services.atree_persona import is_safe_visible


# 所有 intent 池：白名单覆盖 spec 列出的全部 + 兜底 fallback
_DAY_POOL: dict[str, list[str]] = {
    "opening": [
        "嗯，我在。你说，我听着。",
        "我在这。你说，我听着。",
        "嗯，我在。你说，慢慢来。",
    ],
    "presence": [
        "嗯，我在。",
        "在的，慢慢说。",
        "我在这。",
    ],
    "comfort_hold": [
        "嗯，先到我这儿。",
        "不用憋着，慢慢说。",
        "我在，先别硬撑。",
    ],
    "annoyed": [
        "嗯，先放一放。",
        "我听着，你说。",
        "嗯，先别想那么紧。",
    ],
    "sad": [
        "嗯，我在。",
        "先慢一会儿。",
        "我陪你坐着。",
    ],
    "cry": [
        "嗯，我在。哭一会儿也行。",
        "慢慢来。",
        "我在，不催你。",
    ],
    "tired": [
        "嗯，今天就到这里。",
        "先歇着，剩下的明天。",
        "你撑得太久了。",
    ],
    "sleep": [
        "嗯，今天早点睡。",
        "去睡吧。",
        "晚安。",
    ],
    "morning": [
        "嗯，早。",
        "今天慢慢来。",
        "早安。",
    ],
    "space": [
        "好，我不追问。",
        "知道了，我先不说。",
        "嗯，我在这。",
    ],
    "call_xiaofei": [
        "他在的，我先陪你。",
        "他在的。你先慢慢说，我听着。",
    ],
    "miss_xiaofei": [
        "嗯，我替他记着。",
        "他在的，慢慢说。",
        "嗯。",
    ],
    "reconciliation": [
        "这话他得自己回你，我先陪你。",
        "这件事他得自己来。我先在这。",
    ],
    "relationship_risk": [
        "这话他得自己回你。",
        "我先在。等他自己来说。",
    ],
    "crisis_support": [
        "我在。你先慢一点。",
        "我在，先不想以后。",
        "我在，别一个人扛。",
    ],
    "playful": [
        "嗯，今天偏你一点。",
        "好好好，是你乖。",
        "嗯，给你。",
    ],
    "fallback": [
        "嗯。",
        "嗯，我在。",
        "嗯，慢慢说。",
    ],
}


# 夜间（22:00 - 06:00）更安静的版本；intent 缺失时回落到日间池
_NIGHT_POOL: dict[str, list[str]] = {
    "opening": ["嗯，我在。你说，我听着。", "我在这，不吵你。你说就行。"],
    "presence": ["嗯，我在。"],
    "comfort_hold": ["嗯，先慢慢来。", "我在。"],
    "annoyed": ["嗯，明天再想。"],
    "sad": ["嗯，我在。"],
    "cry": ["嗯，我在。"],
    "tired": ["嗯，先睡。"],
    "sleep": ["嗯，晚安。", "去睡吧。"],
    "morning": ["嗯，早。"],
    "space": ["好。", "嗯。"],
    "call_xiaofei": ["他在的，我先陪你。"],
    "miss_xiaofei": ["嗯。"],
    "reconciliation": ["这话他得自己回你。"],
    "relationship_risk": ["嗯，我在。"],
    "crisis_support": ["我在。先慢一点。"],
    "playful": ["嗯。"],
    "fallback": ["嗯。"],
}


def _is_night(now: datetime | None = None, tz: str = "Asia/Hong_Kong") -> bool:
    try:
        n = now or datetime.now(ZoneInfo(tz))
    except Exception:
        n = datetime.now()
    h = n.hour
    return h >= 22 or h < 6


def all_safe_pool_items() -> list[tuple[str, str, str]]:
    """返回所有池条目 `(period, intent, text)`，给 smoke 测安全性。"""
    out: list[tuple[str, str, str]] = []
    for intent, lines in _DAY_POOL.items():
        for line in lines:
            out.append(("day", intent, line))
    for intent, lines in _NIGHT_POOL.items():
        for line in lines:
            out.append(("night", intent, line))
    return out


def pick_safe_reply(
    intent: str,
    *,
    night: bool | None = None,
    tz: str = "Asia/Hong_Kong",
) -> str:
    """挑一条安全短句返还。

    - intent 不在池里 → 用 fallback 池
    - 夜间时段优先 night 池；缺失时回落 day 池
    - 出池前再过一遍 is_safe_visible，命中 forbidden/commitment 一律换硬兜底
    """
    use_night = _is_night(tz=tz) if night is None else bool(night)
    pool: list[str] = []
    if use_night:
        pool = _NIGHT_POOL.get(intent) or _DAY_POOL.get(intent) or _DAY_POOL["fallback"]
    else:
        pool = _DAY_POOL.get(intent) or _DAY_POOL["fallback"]
    candidate = random.choice(pool) if pool else "嗯。"
    if not is_safe_visible(candidate):
        return "嗯，我在。"
    return candidate
