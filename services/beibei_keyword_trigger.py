"""贝贝侧自然关键词触发器（P0：替代 slash 命令的关系入口）。

设计目标（spec 关键词触发版）：
- 不让贝贝用 / 命令。她说「在吗 / 抱抱 / 想你 / 烦死了 / 晚安 / 不想说 / 委屈 / 早安」
  这些自然话时，对应到 companion_mode_router 的模式信号 + 一句关系感短句（可选），
  让 gpt-5.5 自然接住。
- 触发不返回菜单、不返回按钮、不返回功能说明。
- 默认 1-2 句；不展开。
- 「想你 / 委屈 / 想哭 / 没电 / 撑不住」可触发 status-only 通报给阿君（dedup）；
  贝贝看不到任何后台文案。
- 旧 /宝宝 兼容：当贝贝发 /宝宝 时也走这套逻辑（去掉斜杠按「宝宝」处理）。

返回 KeywordIntent：
- mode：建议的 companion 模式（presence_soft / comfort_hold / playful_light /
  serious_answer / space_respect / repair_gentle / risk_support）
- short_reply：若设置，可直接当作最终回复（很短）；否则 None，让 LLM 走自然回复
- needs_ajun_alert：是否触发 status-only 通报
- alert_label：通报里的状态标签（短）
- alert_reason：通报里的依据
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from services.companion_mode_router import (
    MODE_COMFORT_HOLD,
    MODE_PLAYFUL_LIGHT,
    MODE_PRESENCE_SOFT,
    MODE_REPAIR_GENTLE,
    MODE_RISK_SUPPORT,
    MODE_SERIOUS_ANSWER,
    MODE_SPACE_RESPECT,
)


@dataclass
class KeywordIntent:
    keyword: str          # 命中关键词（中文短词，主要用于日志/测试）
    mode: str             # 建议进入的模式
    short_reply: str | None  # 若设置：直接发，不走 LLM
    needs_ajun_alert: bool
    alert_label: str
    alert_reason: str


# ====== 关键词词库 ======

# 「唤醒/在场」类（presence_soft）：可有短回复
_PRESENCE_KEYWORDS = (
    "宝宝", "贝贝入口",
    "在吗", "你在吗",
    "陪我一下", "陪我",
)

# 「烦/破防/心烦」 → comfort_hold（不要立刻 serious）
_COMFORT_TROUBLE_KEYWORDS = (
    "烦", "好烦", "烦死了", "破防", "心烦",
)

# 「抱抱」类 → comfort_hold + 直接短句（不弹按钮）
_HUG_KEYWORDS = ("抱抱", "抱一下", "抱我", "抱")

# 「想你」类 → playful_light + 阿君 status-only alert（5 分钟 dedup）
_MISS_KEYWORDS = ("想你", "我想你了", "想阿君", "想阿君了")

# 「晚安/睡了/困了」 → 晚安短回应，默认不强制问分（贝贝主动提分时再接）
_NIGHT_KEYWORDS = ("晚安", "睡了", "我要睡了", "困了")

# 「早安/起床」
_MORNING_KEYWORDS = ("早安", "醒了", "起床了", "早")

# 「委屈/想哭/难受/没电/撑不住」 → comfort_hold；视语气可升级 risk
_GRIEVED_KEYWORDS = ("委屈", "想哭", "难受", "没电", "撑不住")

# 「不想说/别问/算了/没事」 → space_respect
_SPACE_KEYWORDS = ("不想说", "别问", "算了", "没事")

# 「骂我/凶我一下」 → playful_light
_SCOLD_KEYWORDS = ("骂我", "凶我一下", "凶我")

# 「偏爱值/今天乖吗/我乖吗」 → playful_light
_FAVOR_KEYWORDS = ("偏爱值", "今天乖吗", "我乖吗")


# ====== 候选短回复池（关系感，1-2 句，绝不像菜单）======

_HUG_REPLIES = [
    "过来，抱一下。",
    "嗯，我在，先抱抱你。",
    "走过来，给你抱稳。",
]

_MISS_REPLIES = [
    "我也想你了。",
    "刚也在想你。",
    "嗯，我也是。",
]

_NIGHT_REPLIES = [
    "嗯，今天先睡。我在。",
    "去睡吧，明天再说。",
    "晚安。",
]

_MORNING_REPLIES = [
    "早，今天慢慢来。",
    "醒啦？喝口水。",
    "早安。",
]

_SPACE_REPLIES = [
    "好，我不追着你说。",
    "嗯，我先在这。",
    "知道了，我不问。",
]

_SCOLD_REPLIES = [
    "好好好，我错了。",
    "我先认，你别气太久。",
    "骂吧，我接着。",
]

_FAVOR_REPLIES = [
    "今日被偏爱值：满格。",
    "嗯，今天还是偏你。",
    "你今天，乖。",
]

_PRESENCE_REPLIES = [
    "嗯，我在。",
    "我在，慢慢说。",
    "我在。你说，我听着。",
]

_TROUBLE_REPLIES = [
    "嗯，先到我这儿。",
    "不用憋着，慢慢说。",
    "我在，先别硬撑。",
]

_GRIEVED_REPLIES = [
    "我在。先别一个人扛。",
    "嗯，慢慢来，我陪你。",
    "我先抱抱你。",
]


def _contains_any(text: str, keywords: tuple[str, ...]) -> str | None:
    """返回第一个命中关键词；未命中返回 None。"""
    if not text:
        return None
    for k in keywords:
        if k and k in text:
            return k
    return None


def _strip_slash_baobao(text: str) -> str:
    """旧 /宝宝 兼容：把开头的 /宝宝 视作关键词「宝宝」处理。"""
    s = (text or "").strip()
    # 兼容 /宝宝@bot 形式
    s2 = re.sub(r"^/宝宝(@\w+)?", "宝宝", s)
    return s2 if s2 != s else s


def detect_intent(text: str) -> KeywordIntent | None:
    """对一条贝贝消息做自然关键词识别。命中返回 KeywordIntent；否则返回 None。

    优先级（高 → 低）：
      委屈/想哭/没电（情绪低）  >  想你  >  抱抱  >  烦/破防  >
      晚安/早安  >  不想说  >  骂我  >  偏爱值/乖吗  >  在吗/宝宝/陪我（在场）
    顺序设计避免误吞：例如「想你了」不应被「想哭」匹配，「抱抱」优先于「抱」。
    """
    if not text:
        return None
    # 兼容旧 /宝宝 → 当作「宝宝」关键词
    s = _strip_slash_baobao(text)
    if not s:
        return None
    import random

    # —— 委屈 / 想哭 / 没电 / 撑不住（情绪低，可能触发 alert）——
    hit = _contains_any(s, _GRIEVED_KEYWORDS)
    if hit:
        return KeywordIntent(
            keyword=hit,
            mode=MODE_COMFORT_HOLD,
            short_reply=None,  # 让 gpt-5.5 自然回，保持关系感
            needs_ajun_alert=True,
            alert_label="状态：情绪低 / 偏想被接住",
            alert_reason=f"她提到「{hit}」，可能需要被接住。",
        )

    # —— 想你（playful or comfort 视情境，默认 playful_light + 给阿君通报）——
    hit = _contains_any(s, _MISS_KEYWORDS)
    if hit:
        return KeywordIntent(
            keyword=hit,
            mode=MODE_PLAYFUL_LIGHT,
            short_reply=random.choice(_MISS_REPLIES),  # 直接给短句；alert 单独给阿君
            needs_ajun_alert=True,
            alert_label="状态：撒娇/想你",
            alert_reason=f"她说「{hit}」。",
        )

    # —— 抱抱（短句直接接住，不走 LLM 也行；不弹按钮）——
    hit = _contains_any(s, _HUG_KEYWORDS)
    if hit:
        return KeywordIntent(
            keyword=hit,
            mode=MODE_COMFORT_HOLD,
            short_reply=random.choice(_HUG_REPLIES),
            needs_ajun_alert=False,
            alert_label="",
            alert_reason="",
        )

    # —— 烦 / 破防 / 心烦 —— comfort_hold；让 LLM 自然回（不要立刻给建议）
    hit = _contains_any(s, _COMFORT_TROUBLE_KEYWORDS)
    if hit:
        return KeywordIntent(
            keyword=hit,
            mode=MODE_COMFORT_HOLD,
            short_reply=None,
            needs_ajun_alert=False,
            alert_label="",
            alert_reason="",
        )

    # —— 晚安 / 睡了 / 困了 ——
    hit = _contains_any(s, _NIGHT_KEYWORDS)
    if hit:
        return KeywordIntent(
            keyword=hit,
            mode=MODE_PRESENCE_SOFT,
            short_reply=random.choice(_NIGHT_REPLIES),
            needs_ajun_alert=False,
            alert_label="",
            alert_reason="",
        )

    # —— 早安 / 醒了 / 起床了 ——
    hit = _contains_any(s, _MORNING_KEYWORDS)
    if hit:
        return KeywordIntent(
            keyword=hit,
            mode=MODE_PRESENCE_SOFT,
            short_reply=random.choice(_MORNING_REPLIES),
            needs_ajun_alert=False,
            alert_label="",
            alert_reason="",
        )

    # —— 不想说 / 别问 / 算了 / 没事 —— space_respect
    hit = _contains_any(s, _SPACE_KEYWORDS)
    if hit:
        return KeywordIntent(
            keyword=hit,
            mode=MODE_SPACE_RESPECT,
            short_reply=random.choice(_SPACE_REPLIES),
            needs_ajun_alert=False,
            alert_label="",
            alert_reason="",
        )

    # —— 骂我 / 凶我一下 ——
    hit = _contains_any(s, _SCOLD_KEYWORDS)
    if hit:
        return KeywordIntent(
            keyword=hit,
            mode=MODE_PLAYFUL_LIGHT,
            short_reply=random.choice(_SCOLD_REPLIES),
            needs_ajun_alert=False,
            alert_label="",
            alert_reason="",
        )

    # —— 偏爱值 / 今天乖吗 ——
    hit = _contains_any(s, _FAVOR_KEYWORDS)
    if hit:
        return KeywordIntent(
            keyword=hit,
            mode=MODE_PLAYFUL_LIGHT,
            short_reply=random.choice(_FAVOR_REPLIES),
            needs_ajun_alert=False,
            alert_label="",
            alert_reason="",
        )

    # —— 「烦」之类放在抱抱之后避免短词误吞（抱>抱抱>烦）——
    # 已在上面按优先级处理过；这里轮到最低优先：在场类关键词。

    # —— 在场/唤醒类（宝宝 / 在吗 / 陪我）——
    hit = _contains_any(s, _PRESENCE_KEYWORDS)
    if hit:
        return KeywordIntent(
            keyword=hit,
            mode=MODE_PRESENCE_SOFT,
            short_reply=random.choice(_PRESENCE_REPLIES),
            needs_ajun_alert=False,
            alert_label="",
            alert_reason="",
        )

    return None


# ====== 给路由层调用的简短 helper ======

def is_legacy_baobao_slash(text: str) -> bool:
    """识别贝贝发的旧 /宝宝（含 @bot）。"""
    if not text:
        return False
    s = text.strip()
    return bool(re.match(r"^/宝宝(@\w+)?(\s|$)", s))
