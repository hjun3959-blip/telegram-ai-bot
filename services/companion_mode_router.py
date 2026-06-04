"""贝贝陪伴 · 模式路由（P0）。

按 design spec：7 个主模式 + 优先级规则。
规则守门为主：快速、可测、可解释；不依赖 LLM 二次分类，先把第一版稳住。

模式：
- risk_support：高风险，最高优先级
- repair_gentle：误会/委屈/阴阳风险
- comfort_hold：低落/累/烦/委屈/想哭
- serious_answer：明确认真提问
- playful_light：撒娇/玩笑/逗
- space_respect：冷淡/敷衍/短回避
- presence_soft：默认轻在场
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Iterable

from services.risk_alert_service import (
    NIGHT_LOW_KEYWORDS,
    hits_high_risk_keywords,
    is_deep_night,
)


# 模式枚举（字符串常量，便于序列化与日志）
MODE_PRESENCE_SOFT = "presence_soft"
MODE_COMFORT_HOLD = "comfort_hold"
MODE_PLAYFUL_LIGHT = "playful_light"
MODE_SERIOUS_ANSWER = "serious_answer"
MODE_SPACE_RESPECT = "space_respect"
MODE_REPAIR_GENTLE = "repair_gentle"
MODE_RISK_SUPPORT = "risk_support"

ALL_MODES = (
    MODE_RISK_SUPPORT,
    MODE_REPAIR_GENTLE,
    MODE_COMFORT_HOLD,
    MODE_SERIOUS_ANSWER,
    MODE_PLAYFUL_LIGHT,
    MODE_SPACE_RESPECT,
    MODE_PRESENCE_SOFT,
)


# ---- 关键词词库（短小，可调）----

# 低落/累
COMFORT_KEYWORDS = (
    "好累", "累了", "累死", "累爆", "好烦", "烦死", "不想动", "不想说话",
    "今天很差", "今天不行", "今天好差", "心累", "难受", "想哭", "委屈",
    "撑不住", "崩溃", "失眠",
)

# 撒娇/亲昵/玩笑
PLAYFUL_KEYWORDS = (
    "哼", "哼哼", "嘿嘿", "嘻嘻", "啦啦", "想你了～", "亲一个", "抱一下",
    "陪我", "夸夸我", "哄我", "夸我", "你说嘛", "宝宝",
)

# 误会/阴阳/repair
REPAIR_KEYWORDS = (
    "随便你", "你都不懂", "算了你", "不用了", "你忙吧", "你忙你的",
    "无所谓", "呵", "呵呵", "你说什么都对", "你高兴就好",
)

# 序号问句 / 决策问题（serious 信号）
QUESTION_HINT_KEYWORDS = (
    "你怎么看", "为什么", "你说我该", "我该不该", "怎么办", "应不应该",
    "建议", "怎么处理", "你觉得",
)

# 冷淡/敷衍（强匹配：消息本体几乎只剩这些）
COLD_REPLY_LITERALS = frozenset({
    "嗯", "好", "哦", "行", "随便", "无所谓",
    "。", "…", "...", "....", ".....", "..", "。。", "。。。",
    "嗯嗯", "好的", "知道了",
    "🙂", "🙃",
})

# 「想哭/委屈/想被偏爱」等 hint（不一定 high-intensity，但要进 comfort_hold）
LIGHT_COMFORT_HINTS = ("想哭", "委屈", "心疼自己", "好崩")


# ---- 会话级状态：ask_budget / template cooldown / nickname cooldown ----

@dataclass
class _SessionState:
    """会话级控制：连续追问熔断、称呼冷却、模板冷却。"""
    ask_budget: int = 1
    last_question_at: float = 0.0
    last_nickname_at: float = 0.0
    last_template_at: dict[str, float] = field(default_factory=dict)
    last_mode: str = MODE_PRESENCE_SOFT
    last_mode_at: float = 0.0


# user_id -> state；in-memory，dies on restart（acceptable，TTL 不必显式 evict
# 单 user 占用极小；进程重启即重置）。
_session_states: dict[int, _SessionState] = {}


def get_session_state(user_id: int) -> _SessionState:
    if user_id not in _session_states:
        _session_states[user_id] = _SessionState()
    return _session_states[user_id]


def reset_session(user_id: int) -> None:
    _session_states.pop(user_id, None)


# ---- 特征抽取 ----

@dataclass
class MessageFeatures:
    text: str = ""
    text_normed: str = ""
    length: int = 0
    is_question: bool = False
    is_cold_reply: bool = False
    has_emoji_only: bool = False
    is_short: bool = False
    deep_night: bool = False
    # 信号分数（0-1，粗粒度）
    coldness_score: float = 0.0
    sadness_score: float = 0.0
    playfulness_score: float = 0.0
    repair_risk_score: float = 0.0
    risk_score: float = 0.0
    expects_answer_score: float = 0.0


_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF"
    "\U00002700-\U000027BF"
    "]+"
)


def _norm(text: str) -> str:
    s = (text or "").strip()
    return s.replace("？", "?").replace("！", "!").replace(" ", "").lower()


def _is_only_emoji(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    stripped = _EMOJI_PATTERN.sub("", s).strip()
    # 只剩空 or 仅剩少量符号
    return len(stripped) == 0


def extract_features(text: str, *, tz: str = "Asia/Hong_Kong") -> MessageFeatures:
    f = MessageFeatures()
    f.text = text or ""
    f.text_normed = _norm(text)
    f.length = len(f.text.strip())
    f.is_short = f.length <= 6
    f.deep_night = is_deep_night(tz=tz)
    f.has_emoji_only = _is_only_emoji(text)
    f.is_cold_reply = bool(f.text_normed) and f.text_normed in COLD_REPLY_LITERALS

    # 问句
    f.is_question = bool(f.text) and (
        "?" in f.text or any(k in f.text for k in QUESTION_HINT_KEYWORDS)
    )
    f.expects_answer_score = 0.8 if f.is_question else 0.1

    # 高风险
    hr = hits_high_risk_keywords(text)
    f.risk_score = min(1.0, 0.4 + 0.2 * len(hr)) if hr else 0.0

    # 修复信号
    rp = sum(1 for k in REPAIR_KEYWORDS if k in f.text)
    f.repair_risk_score = min(1.0, 0.3 * rp)

    # 低落
    sad_hits = sum(1 for k in COMFORT_KEYWORDS if k in f.text)
    light_hits = sum(1 for k in LIGHT_COMFORT_HINTS if k in f.text)
    # 深夜命中夜间低落词也加权
    night_hits = 0
    if f.deep_night:
        night_hits = sum(1 for k in NIGHT_LOW_KEYWORDS if k in f.text)
    f.sadness_score = min(1.0, 0.35 * sad_hits + 0.2 * light_hits + 0.15 * night_hits)

    # 撒娇
    play_hits = sum(1 for k in PLAYFUL_KEYWORDS if k in f.text)
    f.playfulness_score = min(1.0, 0.3 * play_hits)

    # 冷淡：只有「严格字面冷回」才算 space_respect 信号；
    # 普通短句（如「今天去吃饭」）不应被判定为冷淡。
    if f.is_cold_reply:
        f.coldness_score = 0.9
    else:
        f.coldness_score = 0.0

    return f


# ---- 分类（规则守门）----

@dataclass
class ClassificationResult:
    mode: str
    intensity: float          # 0-1
    should_ask_question: bool
    needs_ajun_alert: bool
    allow_emoji: bool
    allow_sticker: bool
    allow_gif: bool
    reason: str
    features: MessageFeatures
    repair_streak: int = 0    # 连续 repair 轮数（>=2 触发 alert）
    comfort_streak: int = 0   # 连续 comfort_hold 高强度轮数（>=3 触发 alert）


def _decide_mode(f: MessageFeatures) -> tuple[str, float, str]:
    """按优先级决定主模式。返回 (mode, intensity, reason)。"""
    if f.risk_score >= 0.4:
        return MODE_RISK_SUPPORT, max(0.6, f.risk_score), "命中高风险关键词"
    if f.repair_risk_score >= 0.3:
        return MODE_REPAIR_GENTLE, max(0.5, f.repair_risk_score), "命中误会/阴阳/委屈信号"
    if f.sadness_score >= 0.3:
        # 低落 + 同时有明确问句 → 仍是 comfort_hold（spec：先接住，再回答）
        return MODE_COMFORT_HOLD, f.sadness_score, "低落/累/烦/想哭"
    if f.is_question and f.expects_answer_score >= 0.6:
        return MODE_SERIOUS_ANSWER, 0.7, "明确认真提问"
    if f.coldness_score >= 0.5:
        return MODE_SPACE_RESPECT, f.coldness_score, "冷淡/敷衍/留白"
    if f.playfulness_score >= 0.3:
        return MODE_PLAYFUL_LIGHT, f.playfulness_score, "撒娇/玩笑/逗"
    # 仅 emoji/sticker 短消息：默认 presence_soft，不分析
    return MODE_PRESENCE_SOFT, 0.3, "默认轻在场"


def classify(
    user_id: int,
    text: str,
    *,
    tz: str = "Asia/Hong_Kong",
    media_kind: str = "text",  # "text"|"sticker"|"gif"|"voice"|"photo"|"emoji_only"
) -> ClassificationResult:
    """对一条贝贝消息做模式分类 + 是否提醒阿君的决策。"""
    f = extract_features(text, tz=tz)
    state = get_session_state(user_id)

    # 媒体类型修正：纯 emoji/sticker/GIF/voice 当作短消息处理；不让模型上分析腔
    if media_kind in ("sticker", "gif"):
        f.is_short = True
    if f.has_emoji_only or media_kind == "emoji_only":
        f.is_short = True

    mode, intensity, reason = _decide_mode(f)

    # 连续相同模式累加
    repair_streak = 0
    comfort_streak = 0
    if state.last_mode == MODE_REPAIR_GENTLE:
        repair_streak = 1
    if state.last_mode == MODE_COMFORT_HOLD:
        comfort_streak = 1
    if mode == MODE_REPAIR_GENTLE:
        repair_streak += 1
    if mode == MODE_COMFORT_HOLD and intensity >= 0.5:
        comfort_streak += 1

    # 追问预算：冷淡 / repair / 高风险默认不允许追问
    ask_allowed = state.ask_budget > 0
    if mode in (MODE_SPACE_RESPECT, MODE_REPAIR_GENTLE, MODE_RISK_SUPPORT):
        ask_allowed = False
    if mode == MODE_COMFORT_HOLD:
        # spec：低落但没展开时，最多一次轻问
        ask_allowed = ask_allowed and state.ask_budget > 0

    # 是否提醒阿君（spec §7.2）
    needs_alert = False
    if mode == MODE_RISK_SUPPORT:
        needs_alert = True
    elif mode == MODE_REPAIR_GENTLE and repair_streak >= 2:
        needs_alert = True
    elif mode == MODE_COMFORT_HOLD and comfort_streak >= 3 and intensity >= 0.55:
        needs_alert = True

    # 媒体策略（spec §6）
    allow_emoji = mode in (MODE_PRESENCE_SOFT, MODE_PLAYFUL_LIGHT)
    allow_sticker = (
        mode == MODE_PLAYFUL_LIGHT
        and media_kind in ("text", "sticker")
    )
    # GIF 默认关闭；只有连续 playful_light 才允许
    allow_gif = (
        mode == MODE_PLAYFUL_LIGHT
        and state.last_mode == MODE_PLAYFUL_LIGHT
        and not f.deep_night
    )

    return ClassificationResult(
        mode=mode,
        intensity=intensity,
        should_ask_question=ask_allowed,
        needs_ajun_alert=needs_alert,
        allow_emoji=allow_emoji,
        allow_sticker=allow_sticker,
        allow_gif=allow_gif,
        reason=reason,
        features=f,
        repair_streak=repair_streak,
        comfort_streak=comfort_streak,
    )


def record_after_reply(user_id: int, classification: ClassificationResult, reply_text: str) -> None:
    """回复发出后更新 ask_budget、nickname_cooldown、last_mode。"""
    state = get_session_state(user_id)
    now = time.time()

    # ask_budget
    if reply_text and ("?" in reply_text or "？" in reply_text):
        # 机器人问句 → 减预算
        state.ask_budget = max(0, state.ask_budget - 1)
        state.last_question_at = now
    elif classification.features.length > 18:
        # 用户给了长内容 → 重置 budget
        state.ask_budget = 1

    # nickname：粗略检测「宝宝/小胖/贝贝」
    if any(n in (reply_text or "") for n in ("宝宝", "小胖", "贝贝")):
        state.last_nickname_at = now

    state.last_mode = classification.mode
    state.last_mode_at = now


def nickname_allowed(user_id: int, mode: str) -> bool:
    """称呼冷却：默认 3 轮间隔；serious/repair/risk 拉到 5 轮间隔。这里用时间近似。"""
    state = get_session_state(user_id)
    now = time.time()
    elapsed = now - state.last_nickname_at
    # 3 轮 ≈ 6 分钟；5 轮 ≈ 10 分钟（近似）
    threshold = 600.0 if mode in (MODE_SERIOUS_ANSWER, MODE_REPAIR_GENTLE, MODE_RISK_SUPPORT) else 360.0
    return elapsed >= threshold


def template_allowed(user_id: int, template_key: str, *, cooldown_seconds: float = 6 * 3600) -> bool:
    """同一短句模板冷却。"""
    state = get_session_state(user_id)
    last = state.last_template_at.get(template_key, 0.0)
    return (time.time() - last) >= cooldown_seconds


def record_template_used(user_id: int, template_key: str) -> None:
    state = get_session_state(user_id)
    state.last_template_at[template_key] = time.time()
