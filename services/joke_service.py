"""每天一个笑话（daily joke）服务层。

职责：
- 按配置 source mode 拉取/生成一条中文笑话
- 由 LIGHT_MODEL 润色：短、好笑、自然、不低俗、不冒犯、不出 emoji 堆
- 失败兜底：返回一句温和的「今日笑话开小差了」文案
- 不读密钥；复用 services.openai_service.client / call_openai
- 不爬全网；网络源仅消费配置中显式给出的 JSON 接口列表
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from config import (
    DAILY_JOKE_NETWORK_URLS,
    DAILY_JOKE_SOURCE_MODE,
    LIGHT_MODEL,
)
from services.openai_service import call_openai
from utils.logger import setup_logging

logger = setup_logging()


_AI_FALLBACK = "今日笑话开小差了，明天再继续。"

# 笑话润色系统提示词：保持笑点，但用人话短句、不低俗、不冒犯。
_POLISH_SYSTEM = (
    "你是一个中文段子手。任务：把用户提供的笑话改写成一条短、好笑、自然的中文段子，"
    "适合微信/Telegram 私信里发给朋友。\n"
    "硬要求：\n"
    "1) 必须用中文；2) 一两段、合计不超过 80 字；3) 不低俗、不歧视、不冒犯、不出政治敏感；\n"
    "4) 不要堆 emoji；最多一个；5) 不要加引号、不要加“今日笑话：”这种前缀；\n"
    "6) 直接输出最终段子文本即可，不要解释、不要列表、不要分行说明。"
)

# 当没有原始素材时，由 AI 直接原创一条
_AI_GENERATE_SYSTEM = (
    "你是一个中文段子手。任务：原创一条短、好笑、自然的中文段子，"
    "适合微信/Telegram 私信里发给朋友。\n"
    "硬要求：\n"
    "1) 必须用中文；2) 一两段、合计不超过 80 字；3) 不低俗、不歧视、不冒犯、不政治敏感；\n"
    "4) 不要堆 emoji；最多一个；5) 不要加引号、不要加“今日笑话：”这种前缀；\n"
    "6) 直接输出最终段子文本即可，不要解释、不要列表、不要分行说明。\n"
    "题材偏好：生活、职场、宠物、社畜、感情小尴尬、谐音梗；避开擦边、辱骂、宗教、政治。"
)


def _extract_text_from_json(payload: Any) -> str | None:
    """从常见笑话 API JSON 里抽出第一条文本。容错：尝试若干常见字段路径。"""
    if not payload:
        return None
    # 直接是字符串
    if isinstance(payload, str):
        s = payload.strip()
        return s or None
    if isinstance(payload, list):
        for item in payload:
            t = _extract_text_from_json(item)
            if t:
                return t
        return None
    if isinstance(payload, dict):
        # 常见键
        for key in ("content", "joke", "text", "body", "data", "result"):
            if key in payload:
                t = _extract_text_from_json(payload[key])
                if t:
                    return t
    return None


async def _fetch_url_text(url: str, timeout: float = 5.0) -> str | None:
    """阻塞拉取一个 JSON/Text 接口，超时 5 秒；放到线程池里跑避免阻塞 event loop。

    严格的容错：网络错、超时、非 200、JSON 解析失败都吞掉返 None。
    """
    def _do() -> str | None:
        req = urllib_request.Request(url, headers={"User-Agent": "tg-joke-bot/1.0"})
        try:
            with urllib_request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                raw = resp.read()
                if not raw:
                    return None
                text = raw.decode("utf-8", errors="ignore")
        except (urllib_error.URLError, urllib_error.HTTPError, TimeoutError, OSError) as e:
            logger.info("joke fetch failed | url=%s | err=%s", url, e)
            return None
        # 先尝试 JSON
        try:
            payload = json.loads(text)
            joke = _extract_text_from_json(payload)
            if joke:
                return joke.strip()
        except Exception:
            pass
        # 不是 JSON 就当 plain text，截断 400 字
        snippet = text.strip()[:400]
        return snippet or None

    try:
        return await asyncio.get_running_loop().run_in_executor(None, _do)
    except Exception as e:
        logger.warning("joke fetch loop error | url=%s | err=%s", url, e)
        return None


async def fetch_joke_from_network(urls: list[str] | None = None) -> str | None:
    """逐个尝试配置的 URL，返回第一条非空文本。"""
    targets = list(urls or DAILY_JOKE_NETWORK_URLS or [])
    if not targets:
        return None
    random.shuffle(targets)  # 避免每次都打第一个源
    for url in targets:
        text = await _fetch_url_text(url)
        if text:
            return text
    return None


async def _polish_with_model(raw_text: str) -> str | None:
    """让 LIGHT_MODEL 把抓到的原始段子润色到最终格式；失败返 None。"""
    text = (raw_text or "").strip()
    if not text:
        return None
    messages = [
        {"role": "system", "content": _POLISH_SYSTEM},
        {"role": "user", "content": text[:800]},
    ]
    try:
        result = await call_openai(messages, LIGHT_MODEL, "private", response_json=False)
    except Exception as e:
        logger.warning("joke polish failed | err=%s", e)
        return None
    if isinstance(result, str):
        s = result.strip()
        return s or None
    return None


async def _ai_generate_joke() -> str | None:
    """让 LIGHT_MODEL 直接原创一条；失败返 None。"""
    messages = [
        {"role": "system", "content": _AI_GENERATE_SYSTEM},
        {"role": "user", "content": "请直接写一条今天的中文段子。"},
    ]
    try:
        result = await call_openai(messages, LIGHT_MODEL, "private", response_json=False)
    except Exception as e:
        logger.warning("joke ai generate failed | err=%s", e)
        return None
    if isinstance(result, str):
        s = result.strip()
        return s or None
    return None


def _sanitize(text: str) -> str:
    """最终落屏前的兜底清洗：去掉常见前缀、过长截断、去重多余空行。"""
    s = (text or "").strip()
    # 去常见前缀
    for prefix in ("今日笑话：", "今日笑话:", "笑话：", "笑话:", "段子："):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
    # 去包裹引号
    if len(s) >= 2 and s[0] in "“\"'《" and s[-1] in "”\"'》":
        s = s[1:-1].strip()
    # 截到 200 字（润色规则是 80，留点 buffer）
    if len(s) > 200:
        s = s[:200].rstrip() + "…"
    return s


async def get_daily_joke(
    *,
    source_mode: str | None = None,
    network_urls: list[str] | None = None,
) -> str:
    """对外主入口：拿一条最终可发的中文笑话。

    source_mode：
      - mixed（默认）：先抓网络；抓不到回落 AI 原创
      - network：仅抓网络；失败返兜底文案
      - ai：跳过抓取，直接 AI 原创
    """
    mode = (source_mode or DAILY_JOKE_SOURCE_MODE or "mixed").lower()
    if mode not in {"mixed", "network", "ai"}:
        mode = "mixed"

    raw: str | None = None
    if mode in {"network", "mixed"}:
        raw = await fetch_joke_from_network(network_urls)

    # network 模式下：抓到就润色，没抓到也不 fallback AI
    if mode == "network":
        if not raw:
            logger.info("daily joke: network mode but no source returned text")
            return _AI_FALLBACK
        polished = await _polish_with_model(raw) or raw
        return _sanitize(polished) or _AI_FALLBACK

    # ai 模式：直接生成
    if mode == "ai" or not raw:
        ai = await _ai_generate_joke()
        if not ai:
            return _AI_FALLBACK
        return _sanitize(ai) or _AI_FALLBACK

    # mixed 且抓到了原始素材：润色
    polished = await _polish_with_model(raw)
    if polished:
        return _sanitize(polished) or _AI_FALLBACK
    # 润色失败：用原始素材清洗后直接用
    return _sanitize(raw) or _AI_FALLBACK
