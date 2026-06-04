"""贝贝陪伴 · 主回复引擎（P0）。

把「模式路由 + 短句风格基底 + 后处理 + 阿君状态通报」打包：
- 入口 build_system_addendum(classification) → 一段贴在 system prompt 后面的「内部指令」
- 入口 post_process_reply(text, classification, history) → 最终落屏文本
- 入口 build_ajun_alert_text(classification, user_label) → 给阿君的 4 段式 status-only 通报

不强行替换模型回复（除非模型完全失败）。
不要让任何 system/router 文案出现在贝贝面前——所有 alert 文案都走 alert_owner，
不会出现在 send_reply。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from services.companion_mode_router import (
    ALL_MODES,
    MODE_COMFORT_HOLD,
    MODE_PLAYFUL_LIGHT,
    MODE_PRESENCE_SOFT,
    MODE_REPAIR_GENTLE,
    MODE_RISK_SUPPORT,
    MODE_SERIOUS_ANSWER,
    MODE_SPACE_RESPECT,
    ClassificationResult,
)


# 长度上限（按模式）
_MODE_MAX_CHARS = {
    MODE_PRESENCE_SOFT: 28,
    MODE_PLAYFUL_LIGHT: 28,
    MODE_SPACE_RESPECT: 28,
    MODE_COMFORT_HOLD: 35,
    MODE_SERIOUS_ANSWER: 60,
    MODE_REPAIR_GENTLE: 60,
    MODE_RISK_SUPPORT: 50,
}


# 模式 → 风格基底短句池（不强制用，仅供模型参考；后处理会确保不复读模板）
_SHORT_POOL = {
    MODE_PRESENCE_SOFT: ["嗯，我在。", "你说，我听着。", "先过来。", "我在这。"],
    MODE_COMFORT_HOLD: ["先别硬撑。", "没事，先到我这儿。", "那我先陪你缓一会儿。", "不想说也行。"],
    MODE_PLAYFUL_LIGHT: ["知道啦，先偏你。", "行，今天让着你。", "嗯，给你一点特权。", "来，给你抱一下。"],
    MODE_SERIOUS_ANSWER: ["这个我认真回你。", "我先说结论。", "这件事我站你这边，但要分开看。"],
    MODE_SPACE_RESPECT: ["好，我不追着你说。", "那我先安静陪你。", "你想说的时候再来。"],
    MODE_REPAIR_GENTLE: ["这句是我没接好。", "我先不跟你顶。", "你不舒服这件事，我先认。"],
    MODE_RISK_SUPPORT: ["我在，先慢一点。", "你不用一个人扛。", "我陪你，先不想以后。"],
}


# Emoji code-point ranges (inclusive). Built programmatically so the regex
# character class is assembled from chr() values instead of literal astral-plane
# escapes, which static analyzers (CodeQL py/overly-large-range) misparse as
# overlapping � ranges.
_EMOJI_RANGES = (
    (0x1F300, 0x1F5FF),
    (0x1F600, 0x1F64F),
    (0x1F680, 0x1F6FF),
    (0x1F700, 0x1F77F),
    (0x1F780, 0x1F7FF),
    (0x1F800, 0x1F8FF),
    (0x1F900, 0x1F9FF),
    (0x1FA00, 0x1FA6F),
    (0x1FA70, 0x1FAFF),
    (0x2600, 0x26FF),
    (0x2700, 0x27BF),
)


def _build_emoji_class() -> str:
    return "[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in _EMOJI_RANGES) + "]"


_EMOJI_RE = re.compile(_build_emoji_class())


def _mode_brief(mode: str) -> str:
    return {
        MODE_PRESENCE_SOFT: "presence_soft（轻在场）",
        MODE_COMFORT_HOLD: "comfort_hold（低落接住）",
        MODE_PLAYFUL_LIGHT: "playful_light（轻撒娇/轻调皮）",
        MODE_SERIOUS_ANSWER: "serious_answer（认真回应）",
        MODE_SPACE_RESPECT: "space_respect（给空间）",
        MODE_REPAIR_GENTLE: "repair_gentle（降摩擦，不辩解）",
        MODE_RISK_SUPPORT: "risk_support（稳定陪伴，不抽离）",
    }.get(mode, mode)


def build_system_addendum(classification: ClassificationResult) -> str:
    """生成一段「内部指令」追加到 system prompt 后面，让 gpt-5.5 按模式回话。

    严格要求：不暴露分析、不复述关键词、不堆称呼、不上承诺。
    """
    mode = classification.mode
    max_chars = _MODE_MAX_CHARS.get(mode, 35)
    style_pool = "、".join(f"「{s}」" for s in _SHORT_POOL.get(mode, [])[:3])

    bullets: list[str] = []
    bullets.append(f"当前回复模式（内部，不要外露）：{_mode_brief(mode)}")
    bullets.append(f"本轮目标长度：默认 1-2 句，最长不超过 {max_chars} 个中文字符")
    if classification.should_ask_question:
        bullets.append("允许最多一句轻问；如果不确定该不该问，就不问。")
    else:
        bullets.append("本轮不要在末尾抛任何问句。")

    if classification.allow_emoji:
        bullets.append("最多 1 个 emoji；可不用。")
    else:
        bullets.append("本轮不要用 emoji。")

    if mode == MODE_COMFORT_HOLD:
        bullets.append("禁止：「你要积极一点」「其实没事的」「你应该」这类话。")
    if mode == MODE_REPAIR_GENTLE:
        bullets.append("先承认她不舒服，不辩解、不甩锅给「误会」。")
    if mode == MODE_RISK_SUPPORT:
        bullets.append("继续陪、不抽离、不切官方腔；语气要软、不要长篇。")
    if mode == MODE_SPACE_RESPECT:
        bullets.append("不要追问、不要把话拉长；一句话即可。")
    if mode == MODE_PLAYFUL_LIGHT:
        bullets.append("轻，不油腻；不连续抖机灵。")
    if mode == MODE_SERIOUS_ANSWER:
        bullets.append("先表认真再给清晰观点；要点最多 2 个；不要咨询师腔。")

    bullets.append("不要复述她原话；不要写「我分析/我感受到/作为AI」。")
    bullets.append("不要堆称呼，「宝宝/小胖/贝贝」如果上一两轮已经叫过，本轮就别叫。")
    if style_pool:
        bullets.append(f"风格基底参考（不要照抄）：{style_pool}")

    return "【内部 · 仅供你参考，不要外露】\n" + "\n".join(f"- {b}" for b in bullets)


def _truncate(text: str, max_chars: int) -> str:
    s = (text or "").strip()
    if len(s) <= max_chars:
        return s
    # 尝试在句号/逗号断
    for i in range(max_chars, max(0, max_chars - 12), -1):
        if i < len(s) and s[i] in "。.!?！？，,；;":
            return s[: i + 1]
    # 强制截断：保证最终长度 ≤ max_chars（含省略号）
    return s[: max(0, max_chars - 1)].rstrip() + "…"


def _strip_extra_emoji(text: str, allow_count: int) -> str:
    """限制 emoji 数量。allow_count=0 时全删。"""
    found = 0

    def _repl(m):
        nonlocal found
        found += 1
        if found <= allow_count:
            return m.group(0)
        return ""

    return _EMOJI_RE.sub(_repl, text)


# 反模板化：当用户多次问候时不要重复同一句模板。配合 mode_router 的 template_cooldown。
def _maybe_apply_template_fallback(text: str, classification: ClassificationResult) -> str:
    """如果模型给出空回复，从模式池里挑一句兜底。"""
    s = (text or "").strip()
    if s:
        return s
    pool = _SHORT_POOL.get(classification.mode) or _SHORT_POOL[MODE_PRESENCE_SOFT]
    # 取首个，调用方负责模板冷却
    return pool[0]


def post_process_reply(reply_text: str, classification: ClassificationResult) -> str:
    """对模型回复做后处理：长度裁切 + emoji 限制 + 追问熔断兜底。

    注意：模型若已经按 system addendum 输出短句，这里多半是 no-op；
    主要为了保底，避免模型偶尔超长或多 emoji。
    """
    s = _maybe_apply_template_fallback(reply_text, classification)

    # emoji 限制
    if classification.allow_emoji:
        s = _strip_extra_emoji(s, allow_count=1)
    else:
        s = _strip_extra_emoji(s, allow_count=0)

    # 长度裁切
    max_chars = _MODE_MAX_CHARS.get(classification.mode, 35)
    s = _truncate(s, max_chars)

    # 追问熔断：本轮不允许问号，但模型还是问了 → 砍掉末尾问号
    if not classification.should_ask_question:
        # 把末尾的 ?！？ 改成句号；同时如果整句以问号收，做温和改写
        s = re.sub(r"[??!！]+$", "。", s).strip()
        # 如果中间还有问号且是末段，也压平
        if s.endswith("?") or s.endswith("？"):
            s = s.rstrip("?？") + "。"

    # 去掉「我是 AI」「作为 AI」此类自曝；删字尽量保留语句
    for forbid in ("作为AI", "作为 AI", "作为人工智能", "我是 AI", "我是AI", "我分析", "情绪分析"):
        s = s.replace(forbid, "")

    return s.strip() or _SHORT_POOL[classification.mode][0]


# ---- 阿君状态通报 ----

@dataclass
class AjunAlert:
    should_alert: bool
    text: str
    dedup_key: str


_MODE_STATE_LABEL = {
    MODE_RISK_SUPPORT: "状态：风险偏高，建议你看一眼",
    MODE_REPAIR_GENTLE: "状态：可能有委屈/误会",
    MODE_COMFORT_HOLD: "状态：有点低落，偏想被接住",
    MODE_SERIOUS_ANSWER: "状态：在认真问问题",
    MODE_PLAYFUL_LIGHT: "状态：轻松撒娇",
    MODE_SPACE_RESPECT: "状态：偏冷，需要空间",
    MODE_PRESENCE_SOFT: "状态：日常在场",
}


_MODE_OPENERS = {
    MODE_RISK_SUPPORT: "我看到了，先别一个人扛，我等下找你。",
    MODE_REPAIR_GENTLE: "这句是我没接好，先别气，我在。",
    MODE_COMFORT_HOLD: "我看到了，先别硬撑。",
    MODE_SERIOUS_ANSWER: "我先认真回你。",
    MODE_PLAYFUL_LIGHT: "嗯，给你偏心一点。",
    MODE_SPACE_RESPECT: "好，我先不追着你说。",
    MODE_PRESENCE_SOFT: "嗯，我在。",
}


def build_ajun_alert(
    classification: ClassificationResult,
    user_label: str,
    *,
    extra_reason: str = "",
) -> AjunAlert | None:
    """按 spec §7.4 生成 4 段式 status-only 通报；不指挥真人，不出现「请立刻/务必/5 分钟内」。"""
    if not classification.needs_ajun_alert:
        return None
    mode = classification.mode
    label = _MODE_STATE_LABEL.get(mode, "状态：需要留意一下")
    reason = (extra_reason or classification.reason or "").strip() or "信号轻微，给你同步一下。"
    bot_action = "机器人已先接住，未追问；保持短句陪伴中。" if mode != MODE_RISK_SUPPORT else (
        "机器人继续陪她、保持低刺激；未抽离、未切官方腔。"
    )
    opener = _MODE_OPENERS.get(mode, "嗯，我在。")
    text = (
        f"{label}（{user_label}）\n"
        f"依据：{reason}\n"
        f"机器人已做：{bot_action}\n"
        f"你如果想接，开头参考：{opener}\n"
        f"——仅为状态通报，机器人仍在用 gpt-5.5 正常陪她。"
    )
    # dedup key：sender + mode + 小时 bucket
    import time
    bucket = int(time.time() // (60 * 60))
    dedup_key = f"ajun_alert::{user_label}::{mode}::{bucket}"
    return AjunAlert(should_alert=True, text=text, dedup_key=dedup_key)


# ---- /宝宝 关系唤醒 ----

# 注意：宝宝唤醒不依赖 mode 分类（不需要她输入），而是看「最近 last_mode」做粗判
def baobao_wake_line(last_mode: str | None) -> str:
    """spec §4.2：/宝宝 不展示菜单，只回一句关系唤醒短句。"""
    if last_mode == MODE_COMFORT_HOLD:
        return "我在，今天先不硬撑。"
    if last_mode == MODE_SPACE_RESPECT:
        return "我在这，不吵你。"
    if last_mode == MODE_PLAYFUL_LIGHT:
        return "嗯，宝宝到了。"
    if last_mode == MODE_SERIOUS_ANSWER:
        return "我记着刚才那件事，我们接着说。"
    if last_mode == MODE_REPAIR_GENTLE:
        return "我先不跟你顶。"
    if last_mode == MODE_RISK_SUPPORT:
        return "我在，慢慢来。"
    return "我在，慢慢说。"
