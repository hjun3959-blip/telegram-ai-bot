"""阿树自然关键词触发器（P0）。

输入文本，输出 `AtreeIntent`：
- intent：与 atree_quote_library 池对齐
- severity：critical / high / medium / low
- notify_owner：是否给阿君发通知
- forward_original：通知里是否带贝贝原话

关键规则：
- 含否定前缀「不/没/别/无/未」的相邻词不触发 ANNOYED 等普通情绪类。
  例如「没那么烦了」「不想哭了」「不烦」不应命中。
- 危机词、复合词、小肥相关词永远触发安全短句路径，不依赖 GPT 发挥。
- 兼容 `/宝宝` ⇒ 当作关键词「宝宝」处理（贝贝侧不展示 / 命令）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ------------------ 关键词词库（按优先级）------------------

# 危机
_CRISIS = ("撑不住", "不想活", "活不下去", "崩了", "没意思了", "撑不下去", "想消失")
# 复合
_RECONCILE = ("复合", "我们复合", "重新开始", "我们和好")
# 关系/钱风险
_RELATION_RISK = ("分开", "分手", "拉黑你", "再也不", "信任", "钱", "借钱", "还钱")
# 「小肥/肥肥」是贝贝对阿君的专属称呼
_CALL_XIAOFEI = ("小肥", "肥肥")
# 想你/想阿君
_MISS = ("想你", "我想你了", "想阿君", "想小肥")
# 普通情绪
_ANNOYED = ("烦", "好烦", "烦死了", "破防", "心烦")
_SAD = ("委屈", "想哭", "难受", "没电")
_TIRED = ("累", "没力气", "扛不动")
# 仪式
_SLEEP = ("晚安", "睡了", "我要睡了", "困了", "先睡了")
_MORNING = ("早安", "醒了", "起床了")
# 在场
_PRESENCE = ("在吗", "你在吗", "陪我", "陪我一下", "阿树", "宝宝", "贝贝入口")
# 抱抱
_COMFORT = ("抱抱", "抱一下", "抱我")
# 空间
_SPACE = ("不想说", "别问", "算了", "没事")
# 玩
_PLAYFUL = ("骂我", "凶我", "凶我一下", "偏爱值", "今天乖吗", "我乖吗")


# ------------------ 否定前缀 ------------------
# 命中规则：keyword 前 1-2 个字符内出现这些前缀，视为否定（不触发）。
# 例如「没那么烦了」「不烦」「别烦」「未必烦」「无所谓烦」。
_NEGATION_PREFIXES = ("不", "没", "别", "无", "未")


# ------------------ 结果 ------------------

@dataclass
class AtreeIntent:
    matched_keyword: str
    intent: str
    severity: str          # critical | high | medium | low
    notify_owner: bool
    forward_original: bool


# ------------------ 辅助 ------------------

def _strip_slash_baobao(text: str) -> str:
    """旧 /宝宝 兼容：当作关键词「宝宝」处理。"""
    s = (text or "").strip()
    s2 = re.sub(r"^/宝宝(@\w+)?", "宝宝", s)
    return s2 if s2 != s else s


def _is_negated(text: str, kw_start: int) -> bool:
    """检查 kw 命中位置之前最近 1-4 个字符里是否出现否定前缀。

    支持：「不烦」「没烦」「别想哭」「没那么烦」「没那么想哭」「不那么累」「无所谓烦」。
    """
    if kw_start <= 0:
        return False
    lookback = text[max(0, kw_start - 4): kw_start]
    if not lookback:
        return False
    # 任意位置出现否定前缀就视为否定
    for ch in lookback:
        if ch in _NEGATION_PREFIXES:
            return True
    return False


def _hit_first(text: str, keywords: tuple[str, ...], *, check_negation: bool = True) -> str | None:
    """返回首个命中的关键词；否定前缀则跳过。"""
    if not text:
        return None
    for k in keywords:
        if not k:
            continue
        idx = text.find(k)
        if idx < 0:
            continue
        if check_negation and _is_negated(text, idx):
            # 跳过这一处；继续看是否其他位置也命中
            # 简单处理：再找下一个出现位置
            pos = idx
            while pos != -1:
                pos = text.find(k, pos + 1)
                if pos == -1:
                    break
                if not _is_negated(text, pos):
                    return k
            continue
        return k
    return None


# ------------------ 主入口 ------------------

def detect_intent(text: str) -> AtreeIntent | None:
    """对一条贝贝消息做关键词识别。命中返回 AtreeIntent；否则返回 None。

    优先级（高 → 低）：crisis > reconcile > relation_risk > call_xiaofei >
                       miss > sad > tired > annoyed > sleep > morning >
                       playful > comfort > space > presence
    """
    if not text:
        return None
    s = _strip_slash_baobao(text)
    if not s.strip():
        return None

    # —— 危机（不受否定前缀影响：宁可误报也别漏）——
    hit = _hit_first(s, _CRISIS, check_negation=False)
    if hit:
        return AtreeIntent(hit, "crisis_support", "critical", True, True)

    # —— 复合 ——（不受否定前缀影响：「不想复合」也要上报给阿君）
    hit = _hit_first(s, _RECONCILE, check_negation=False)
    if hit:
        return AtreeIntent(hit, "reconciliation", "critical", True, True)

    # —— 关系 / 钱 ——（不否定）
    hit = _hit_first(s, _RELATION_RISK, check_negation=False)
    if hit:
        return AtreeIntent(hit, "relationship_risk", "high", True, True)

    # —— 小肥 / 肥肥 ——
    hit = _hit_first(s, _CALL_XIAOFEI, check_negation=False)
    if hit:
        return AtreeIntent(hit, "call_xiaofei", "high", True, True)

    # —— 想你 ——（不带否定时才上报）
    hit = _hit_first(s, _MISS, check_negation=True)
    if hit:
        return AtreeIntent(hit, "miss_xiaofei", "medium", True, False)

    # —— 想哭 / 委屈 / 难受 / 没电 ——
    hit = _hit_first(s, _SAD, check_negation=True)
    if hit:
        return AtreeIntent(hit, "sad", "medium", True, False)

    # —— 累 / 没力气 / 扛不动 ——
    hit = _hit_first(s, _TIRED, check_negation=True)
    if hit:
        return AtreeIntent(hit, "tired", "medium", True, False)

    # —— 烦 ——（否定就放过）
    hit = _hit_first(s, _ANNOYED, check_negation=True)
    if hit:
        return AtreeIntent(hit, "annoyed", "low", False, False)

    # —— 晚安 / 睡了 / 困了 ——
    hit = _hit_first(s, _SLEEP, check_negation=False)
    if hit:
        return AtreeIntent(hit, "sleep", "low", False, False)

    # —— 早安 / 醒了 / 起床了 ——
    hit = _hit_first(s, _MORNING, check_negation=False)
    if hit:
        return AtreeIntent(hit, "morning", "low", False, False)

    # —— 玩 / 偏爱值 / 我乖吗 ——
    hit = _hit_first(s, _PLAYFUL, check_negation=False)
    if hit:
        return AtreeIntent(hit, "playful", "low", False, False)

    # —— 抱抱 ——
    hit = _hit_first(s, _COMFORT, check_negation=False)
    if hit:
        return AtreeIntent(hit, "comfort_hold", "low", False, False)

    # —— 不想说 / 算了 / 别问 / 没事 ——
    # 这些本身就是否定语气；不参与 _is_negated。
    hit = _hit_first(s, _SPACE, check_negation=False)
    if hit:
        return AtreeIntent(hit, "space", "low", False, False)

    # —— 在吗 / 阿树 / 宝宝 / 陪我 ——
    hit = _hit_first(s, _PRESENCE, check_negation=False)
    if hit:
        return AtreeIntent(hit, "presence", "low", False, False)

    return None


def is_legacy_baobao_slash(text: str) -> bool:
    if not text:
        return False
    s = text.strip()
    return bool(re.match(r"^/宝宝(@\w+)?(\s|$)", s))
