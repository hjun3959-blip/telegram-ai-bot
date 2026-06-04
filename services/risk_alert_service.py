"""贝贝高风险/冷回/深夜低落 — 给真人（阿君）静默告警。

只在以下两种情形使用：
1) Business 真实代聊里收到贝贝的消息（business mode + is_xiaopang）
2) 贝贝在私信 bot 窗口里发消息（private mode + is_xiaopang）

规则（FINAL SPEC）：
A. 高风险关键词命中（如：我们 / 分开 / 回来 / 以后 / 现实 / 钱 / 稳定 / 不信任 /
   别回了 / 别烦我 / 不想继续）→ 给真人通知，附 1-2 句候选安全回复。
   * Business 路径：触发 `should_bot_stay_safe()` 信号，让上层把回复降级到
     `HIGH_RISK_SAFE_REPLY` 这一句话，避免模型即兴长回复。
B. 连续 3 次冷回（嗯 / 好 / 哦 / 行 / 随便 / 。/ … / ...）→ 提示真人减少自动回复。
   per-user 简单状态，超过窗口（10 分钟）重置。
C. 深夜（默认 23:00 后，本地 Asia/Hong_Kong）出现「烦/哭/睡不着/难受/委屈/累」等
   关键词 → 给真人通知，建议真人出现。

所有 alert 都用 `dedup_alert`，避免短时间内重复打扰。
告警永远只通知 OWNER_CHAT_IDS；贝贝看不到。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from services.alert_service import dedup_alert
from utils.logger import setup_logging

logger = setup_logging()


# ============== 关键词与文案 ==============

# 高风险关键词：关系/承诺/现实压力/拒绝继续
HIGH_RISK_KEYWORDS = (
    "我们", "分开", "分手", "回来",
    "以后", "现实",
    "钱", "稳定", "不信任",
    "别回了", "别烦我", "不想继续",
    "不想我们", "结束",
)

# 冷回判定：完全等于其中一个字符串（去空格后）才算「冷回」，避免「嗯嗯今天怎么样」误命中
COLD_REPLY_LITERALS = frozenset({
    "嗯", "好", "哦", "行", "随便",
    "。", "…", "...", "....", ".....",
    "..", "。。", "。。。",
    "嗯嗯", "好的",
    "🙂", "🙃",
})

# 深夜低落关键词
NIGHT_LOW_KEYWORDS = ("烦", "哭", "睡不着", "难受", "委屈", "累")
# 深夜阈值
NIGHT_HOUR_THRESHOLD = 23  # >= 23 点

# 通用 dedup 窗口
_DEDUP_WINDOW_SECONDS = 30 * 60

# 模块级常量：保留兜底文案给极端技术失败时用（默认不会主动用）
# 业务路径不再用它替换 gpt-5.5 输出。
HIGH_RISK_SAFE_REPLY = "好，我不逼你。你先缓缓，我在。"

# 高风险告警文案（FINAL: status-only；不要求阿君几分钟内回复，不暗示 bot 在等他）
HIGH_RISK_OWNER_ALERT_TEXT = (
    "贝贝消息触发高风险关键词，当前可能涉及现实压力/关系不安全感。\n"
    "仅为状态通报；机器人仍在用 gpt-5.5 正常陪她。"
)

# 冷回 3 次告警（status-only）
COLD_REPLY_OWNER_ALERT_TEXT = (
    "贝贝最近一段消息偏冷淡、防御感较高。\n"
    "仅为状态通报；机器人仍在正常陪她。"
)

# 深夜低落告警（status-only）
NIGHT_LOW_OWNER_ALERT_TEXT = (
    "贝贝深夜情绪偏低，可能需要安全感。\n"
    "仅为状态通报；机器人仍在用 gpt-5.5 正常陪她。"
)


# ============== 冷回连击状态 ==============

@dataclass
class _ColdStreak:
    count: int
    last_at: float


_COLD_STREAK_TTL = 10 * 60  # 10 分钟窗
_cold_streaks: dict[int, _ColdStreak] = {}


def _bump_cold_streak(user_id: int) -> int:
    """text 命中冷回时累加；返回累计次数。超时（10 分钟）自动重置为 1。"""
    now = time.time()
    cur = _cold_streaks.get(user_id)
    if cur is None or now - cur.last_at > _COLD_STREAK_TTL:
        cur = _ColdStreak(count=1, last_at=now)
    else:
        cur = _ColdStreak(count=cur.count + 1, last_at=now)
    _cold_streaks[user_id] = cur
    return cur.count


def _reset_cold_streak(user_id: int) -> None:
    _cold_streaks.pop(user_id, None)


def _is_cold_reply(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    # 去掉末尾的 ?！。, 不变化语义太多；不做太激进的清洗
    return s in COLD_REPLY_LITERALS


# ============== 检测器 ==============

def hits_high_risk_keywords(text: str) -> list[str]:
    """返回命中关键词列表（去重）。空表示未命中。"""
    s = (text or "")
    out: list[str] = []
    if not s.strip():
        return out
    for k in HIGH_RISK_KEYWORDS:
        if k in s and k not in out:
            out.append(k)
    return out


def hits_night_low_keywords(text: str) -> list[str]:
    s = (text or "")
    out: list[str] = []
    if not s.strip():
        return out
    for k in NIGHT_LOW_KEYWORDS:
        if k in s and k not in out:
            out.append(k)
    return out


def is_deep_night(now: datetime | None = None, tz: str = "Asia/Hong_Kong") -> bool:
    """是否处于深夜（>= NIGHT_HOUR_THRESHOLD 或 < 5）。
    >= 23 或 < 5 都算「深夜段」。
    """
    try:
        n = now or datetime.now(ZoneInfo(tz))
    except Exception:
        n = datetime.now()
    h = n.hour
    return h >= NIGHT_HOUR_THRESHOLD or h < 5


# ============== 给路由层调用的主入口 ==============

@dataclass
class RiskCheckResult:
    """风控判定结果。

    FINAL（更新版）：不再有 safe_reply_only 兜底；business 路径仍走 gpt-5.5，
    通过 `context_for_model` 把风险/状态喂给模型，让模型自己用安抚口吻短回复。
    safe_reply_only 字段保留为 False，仅作向后兼容，不再触发任何降级。
    """
    high_risk_hits: list[str]
    cold_streak: int
    night_low_hits: list[str]
    safe_reply_only: bool  # 始终 False（保留字段名以兼容老调用方）
    context_for_model: str  # 给 gpt-5.5 用的「内部状态摘要」；空串表示无风险信号


def _build_context_for_model(
    *,
    high_risk_hits: list[str],
    cold_streak: int,
    night_low_hits: list[str],
) -> str:
    """把风险信号变成一段内部 system context（中文，简短），让 gpt-5.5 在回贝贝时
    自动调到「短、稳、克制、不讲道理、不许承诺、不要长分析」的安抚模式。
    返回空串表示无信号，调用方可不附加。
    """
    parts: list[str] = []
    if high_risk_hits:
        words = "、".join(high_risk_hits[:4])
        parts.append(
            f"【内部状态 · 仅供你参考，不要外露】她刚才的话触发了高风险关键词（{words}），"
            "可能涉及现实压力 / 关系不安全感。请用最短、最稳、不讲道理、不许下任何承诺"
            "（钱/未来/见面）的方式接住情绪；不要分析、不要复述这些关键词。"
        )
    if cold_streak >= 3:
        parts.append(
            "【内部状态】她最近连续在冷回（嗯/好/哦 类），可能心累或防御。请给空间："
            "极短一句或一个表情；可以 should_reply=false。不要追问、不要『怎么了』。"
        )
    if night_low_hits:
        words = "、".join(night_low_hits[:3])
        parts.append(
            f"【内部状态】现在是深夜，她提到{words}，情绪可能偏低。请用安静、温和、"
            "不催不追问的口吻接一句；优先安抚不解决问题。"
        )
    return "\n\n".join(parts)


async def check_and_alert(
    bot: Bot,
    *,
    user_id: int,
    sender_label: str,
    text: str,
    is_business: bool,
    tz: str = "Asia/Hong_Kong",
) -> RiskCheckResult:
    """对一条贝贝的消息做风控判定 + 必要时给真人 dedup 状态通报。

    FINAL（更新版）：
    - 不再有「替换模型回复」的硬路径；business 仍走 gpt-5.5
    - 给真人的 alert 是「状态通报」，不指挥真人回复
    - 返回的 context_for_model 给 business 路由层在 system prompt 上追加用
    """
    hits_hr = hits_high_risk_keywords(text)
    hits_nl_raw = hits_night_low_keywords(text)
    deep_night = is_deep_night(tz=tz)
    hits_nl = hits_nl_raw if deep_night else []

    # 冷回累计：冷回时增计，否则重置
    cold_count = 0
    if _is_cold_reply(text):
        cold_count = _bump_cold_streak(user_id)
    else:
        _reset_cold_streak(user_id)

    # ---- 告警 ----
    bucket = int(time.time() // _DEDUP_WINDOW_SECONDS)

    if hits_hr:
        key = f"hr::{sender_label}::{bucket}"
        try:
            words = "、".join(hits_hr[:4])
            await dedup_alert(
                bot, key,
                HIGH_RISK_OWNER_ALERT_TEXT + f"\n命中：{words}",
            )
        except Exception as e:
            logger.warning("high-risk alert failed | err=%s", e)

    if cold_count >= 3:
        key = f"cold::{sender_label}::{bucket}"
        try:
            await dedup_alert(bot, key, COLD_REPLY_OWNER_ALERT_TEXT)
        except Exception as e:
            logger.warning("cold alert failed | err=%s", e)
        # 触发过一次告警后清零，避免每条都告警
        _reset_cold_streak(user_id)

    if hits_nl:
        key = f"nightlow::{sender_label}::{bucket}"
        try:
            words = "、".join(hits_nl[:3])
            await dedup_alert(
                bot, key,
                NIGHT_LOW_OWNER_ALERT_TEXT + f"\n命中：{words}",
            )
        except Exception as e:
            logger.warning("night-low alert failed | err=%s", e)

    return RiskCheckResult(
        high_risk_hits=hits_hr,
        cold_streak=cold_count,
        night_low_hits=hits_nl,
        safe_reply_only=False,  # FINAL: 永远不走硬替换
        context_for_model=_build_context_for_model(
            high_risk_hits=hits_hr,
            cold_streak=cold_count,
            night_low_hits=hits_nl,
        ),
    )
