"""文案优化服务（私信功能区 · /文案优化）。

把用户贴来的广告 / 频道文案优化成更适合 Telegram 频道发布的版本：
保留原意与语气，提升清晰度、转化力、排版与合规性。

设计要点：
- 复用 services.openai_service 里的 AsyncOpenAI client，密钥来自 config，不读取 .env
- 不强制 JSON，直接走 chat.completions 拿纯文本
- 主模型失败时回落到 BACKUP_MODEL；都失败时返回简短中文错误
- 识别文案里的表情信号（emoji / Telegram 自定义 emoji 实体 / 贴纸占位 / 用户前后发来的
  贴纸 / GIF），把这份「表达意图」拼进 prompt，而不是当噪音忽略掉
- 不会把任何密钥写入日志
- 仅供 private 路由使用；不影响 Business 代聊与贝贝/阿树隐藏陪伴行为
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from config import BACKUP_MODEL, CORE_MODEL
from services.openai_service import client
from utils.logger import setup_logging

logger = setup_logging()


COPYFIX_SYSTEM = (
    "你是 Telegram 频道文案优化助手。用户会贴来一段广告 / 频道推广文案，"
    "你把它改写成更适合在 Telegram 频道发布的版本。\n"
    "硬性原则：\n"
    "- 保留原意、卖点和原作者的语气，不要换风格、不要凭空加事实或数据\n"
    "- 提升清晰度：去掉啰嗦和重复，让信息一眼能读懂\n"
    "- 提升转化：开头抓人，结尾给清楚的行动指引（如「点击下方」「私信领取」），但别夸大\n"
    "- 优化排版：适合手机阅读，善用换行、短段落、要点列表；可保留或合理使用 emoji 做视觉锚点\n"
    "- 合规：不要承诺收益保证、不要绝对化用语、不要明显违规或诱导词，触及风险点时用更稳妥的表达替换\n"
    "- 跟随原文主要语言（中文/英文）\n"
    "输出格式：\n"
    "- 直接给出优化后的频道文案正文，可直接复制粘贴发布\n"
    "- 不要解释你改了什么，不要加「优化后：」之类前缀，只输出文案本身"
)


# Unicode emoji 粗粒度匹配：覆盖常见表情/符号/旗帜区段。够用即可，不追求完备。
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # 杂项符号与象形 / 补充符号
    "\U00002600-\U000027BF"  # 杂项符号 + dingbats
    "\U0001F1E6-\U0001F1FF"  # 区域指示符（旗帜）
    "\U0000FE00-\U0000FE0F"  # 变体选择符
    "\U00002190-\U000021FF"  # 箭头
    "\U00002B00-\U00002BFF"  # 杂项符号与箭头
    "]"
)

# 文案里常见的贴纸 / 表情占位写法，例如 [贴纸] [sticker] :smile: (笑)
_STICKER_PLACEHOLDER_PATTERN = re.compile(
    r"(\[[^\]\n]{0,20}?(?:贴纸|表情|sticker|emoji|gif)[^\]\n]{0,20}?\])"
    r"|(:[a-z0-9_+\-]{2,40}:)",
    re.IGNORECASE,
)


@dataclass
class ExpressiveSignals:
    """文案 / 附带媒体里的表情类信号汇总。"""

    emojis: list[str] = field(default_factory=list)
    custom_emoji_count: int = 0
    placeholders: list[str] = field(default_factory=list)
    sticker_descs: list[str] = field(default_factory=list)

    def has_any(self) -> bool:
        return bool(self.emojis or self.custom_emoji_count or self.placeholders or self.sticker_descs)

    def to_prompt_hint(self) -> str:
        """把信号拼成一段给模型的「表达意图」说明；无信号时返回空串。"""
        if not self.has_any():
            return ""
        parts: list[str] = []
        if self.emojis:
            uniq = list(dict.fromkeys(self.emojis))
            shown = " ".join(uniq[:20])
            parts.append(f"原文里用到的 emoji：{shown}")
        if self.custom_emoji_count:
            parts.append(f"原文里有 {self.custom_emoji_count} 个 Telegram 自定义 emoji（custom_emoji 实体）")
        if self.placeholders:
            uniq_ph = list(dict.fromkeys(self.placeholders))
            parts.append("原文里有表情/贴纸占位：" + "、".join(uniq_ph[:10]))
        if self.sticker_descs:
            parts.append("用户还附带发了表情媒体：" + "；".join(self.sticker_descs[:5]))
        body = "\n".join(f"- {p}" for p in parts)
        return (
            "表达意图提示（请把这份情绪/活泼度融入优化后的文案，"
            "可保留或合理替换 emoji，不要直接删光表情让文案变得干巴巴）：\n" + body
        )


def extract_signals(text: str, *, entities=None, sticker_descs=None) -> ExpressiveSignals:
    """从文案文本（可选 Telegram 实体、附带贴纸描述）里抽取表情类信号。

    - text：文案正文
    - entities：message.entities（用于识别 custom_emoji 实体），可为 None
    - sticker_descs：调用方提供的「用户前后还发了贴纸/GIF」的人话描述列表，可为 None
    """
    text = text or ""
    emojis = _EMOJI_PATTERN.findall(text)
    placeholders_raw = _STICKER_PLACEHOLDER_PATTERN.findall(text)
    placeholders = [grp for tup in placeholders_raw for grp in tup if grp]

    custom_emoji_count = 0
    for ent in entities or []:
        ent_type = getattr(ent, "type", None)
        if ent_type == "custom_emoji" or ent_type == "CUSTOM_EMOJI":
            custom_emoji_count += 1

    return ExpressiveSignals(
        emojis=emojis,
        custom_emoji_count=custom_emoji_count,
        placeholders=placeholders,
        sticker_descs=list(sticker_descs or []),
    )


async def _chat_text(system_prompt: str, user_content: str, model: str) -> str:
    """走 chat.completions 拿纯文本，不强制 JSON。"""
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.6,
        max_tokens=900,
    )
    raw = response.choices[0].message.content or ""
    return raw.strip()


def _build_user_content(copy_text: str, signals: ExpressiveSignals) -> str:
    """把文案正文 + 表达意图提示拼成发给模型的 user content。"""
    copy_text = (copy_text or "").strip()
    hint = signals.to_prompt_hint()
    if hint:
        return f"待优化文案：\n{copy_text}\n\n{hint}"
    return f"待优化文案：\n{copy_text}"


async def optimize_copy(copy_text: str, signals: ExpressiveSignals | None = None) -> str:
    """优化文案主入口。失败时返回简短错误文案，不抛异常。"""
    copy_text = (copy_text or "").strip()
    if not copy_text:
        return "把你想优化的频道/广告文案发给我就行，我会帮你改得更清楚、更好转化～"

    signals = signals or ExpressiveSignals()
    user_content = _build_user_content(copy_text, signals)

    try:
        text = await _chat_text(COPYFIX_SYSTEM, user_content, CORE_MODEL)
        if text:
            return text
    except Exception as e:
        logger.exception("Copyfix failed | model=%s | err=%s", CORE_MODEL, e)

    if BACKUP_MODEL and BACKUP_MODEL != CORE_MODEL:
        try:
            text = await _chat_text(COPYFIX_SYSTEM, user_content, BACKUP_MODEL)
            if text:
                return text
        except Exception as e:
            logger.exception("Copyfix backup failed | backup=%s | err=%s", BACKUP_MODEL, e)

    return "刚才优化的时候有点卡，稍后再把文案发我一次试试。"
