"""OpenAI/兼容 API 调用封装。

本轮加固重点：
- _extract_json_object 兼容代码块 ```json ... ```、首尾噪声、单引号等异常输出
- _coerce_bool 把字符串 'false'/'False'/'0'/'no'/'否' 等正确解析为 False
- _normalize_result 在空回复时给出合理的 should_reply 推断，不会把空字符串视为 True
- call_openai 主模型失败时尝试 BACKUP_MODEL；BACKUP_MODEL 再失败时也不会再无限递归
- 不会把任何密钥写入日志
"""

import asyncio
import io
import json
import re

import aiofiles
from openai import AsyncOpenAI

from config import BACKUP_MODEL, OPENAI_API_KEY, OPENAI_BASE_URL, TRANSCRIBE_FALLBACK_MODELS, TRANSCRIBE_MODEL
from utils.logger import setup_logging

client = AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
logger = setup_logging()


# ---------- PATCH 3：私聊非 JSON 自然语言上限 ----------
_PRIVATE_RAW_TEXT_MAX_CHARS = 3500


def _truncate_private_raw_text(text: str, max_chars: int = _PRIVATE_RAW_TEXT_MAX_CHARS) -> str:
    """私聊非 JSON 自然语言截断到 max_chars，尽量在末尾标点处优雅断。"""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    marks = ["。", "？", "！", "\n", ".", "?", "!"]
    last = max((cut.rfind(m) for m in marks), default=-1)
    if last >= int(max_chars * 0.6):
        return cut[: last + 1].strip()
    return cut.strip()


# ---------- PATCH 4：同 chat_id 串行锁 ----------
# 弱引用思路只有在 cleanup 路径下才需要；这里 in-process 简单 dict。
_chat_locks: dict[str, asyncio.Lock] = {}


def _get_chat_lock(chat_id) -> asyncio.Lock | None:
    if chat_id is None:
        return None
    key = str(chat_id)
    lock = _chat_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _chat_locks.setdefault(key, lock)
        lock = _chat_locks[key]
    return lock


# ---------- PATCH 4：临时错误判定 + 退避 ----------
def _is_transient_error(err: Exception) -> bool:
    """仅 429 / timeout / 5xx / 连接错误算临时错误。其它错误不重试。"""
    name = err.__class__.__name__.lower()
    if "ratelimit" in name or "timeout" in name or "apiconnection" in name:
        return True
    status = getattr(err, "status_code", None) or getattr(err, "http_status", None)
    if isinstance(status, int):
        if status == 429:
            return True
        if 500 <= status < 600:
            return True
        return False
    msg = str(err).lower()
    if "429" in msg or "rate limit" in msg or "timeout" in msg or "server error" in msg:
        return True
    return False


def fallback_private() -> dict:
    return {"reply_text": "刚才有点卡，你再说一遍。", "sticker_type": None}


def fallback_business() -> dict:
    # business 模式失败时优先静默，不要乱回。
    return {"reply_text": "", "sticker_type": None, "should_reply": False, "risk_note": ""}


def _fallback_for_mode(mode: str) -> dict:
    return fallback_business() if mode == "business" else fallback_private()


_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _extract_json_object(raw: str) -> dict | None:
    """从模型输出里尽力解析出一个 JSON 对象。

    支持：
    - 纯 JSON
    - 代码块 ```json ... ``` 包裹
    - 前后有噪声文本时取第一个 {...} 区间
    """
    text = (raw or "").strip()
    if not text:
        return None
    # 先尝试直接 parse
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else None
    except Exception:
        pass
    # 尝试代码块
    m = _CODE_FENCE_RE.search(text)
    if m:
        inner = m.group(1).strip()
        try:
            result = json.loads(inner)
            if isinstance(result, dict):
                return result
        except Exception:
            pass
    # 取第一个 {...}
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    candidate = match.group(0)
    try:
        result = json.loads(candidate)
        return result if isinstance(result, dict) else None
    except Exception:
        # 最后兜底：把单引号替换为双引号再试一次（仅当看上去像 JSON 时）
        try:
            result = json.loads(candidate.replace("'", '"'))
            return result if isinstance(result, dict) else None
        except Exception:
            return None


_FALSE_STRS = {"false", "0", "no", "n", "off", "none", "null", "否", "不", "假"}
_TRUE_STRS = {"true", "1", "yes", "y", "on", "是", "对", "真"}


def _coerce_bool(value, default: bool) -> bool:
    """把模型返回的各种类型都规范成 bool。

    关键修复：字符串 'false'/'False'/'0' 之前会被 bool() 当成 True；
    这里显式按字符串语义判断。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if not token:
            return default
        if token in _FALSE_STRS:
            return False
        if token in _TRUE_STRS:
            return True
        return default
    return bool(value)


def _normalize_sticker(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token:
        return None
    if token.lower() in {"null", "none", "false"}:
        return None
    return token


def _normalize_result(data: dict | None, mode: str) -> dict:
    if not isinstance(data, dict):
        return _fallback_for_mode(mode)
    reply_text = str(data.get("reply_text") or "").strip()
    sticker_type = _normalize_sticker(data.get("sticker_type"))
    if mode == "business":
        has_content = bool(reply_text or sticker_type)
        # 如果字段缺失，应回退到“有内容才回”
        if "should_reply" in data:
            should_reply = _coerce_bool(data.get("should_reply"), default=has_content)
        else:
            should_reply = has_content
        # 没有任何内容时强制静默，不让模型用 should_reply=true 但空内容触发奇怪发送
        if not has_content:
            should_reply = False
        risk_note = str(data.get("risk_note") or "").strip()
        return {
            "reply_text": reply_text,
            "sticker_type": sticker_type,
            "should_reply": should_reply,
            "risk_note": risk_note,
        }
    return {"reply_text": reply_text, "sticker_type": sticker_type}


async def _do_chat(model: str, messages: list, mode: str, response_json: bool = True):
    """底层调用。

    response_json=True（默认）：要求 JSON，返回 规范化后的 dict
    response_json=False：不要求 JSON，返回 plain text。适用于“视觉摘要”这种
    中间分析步骤。调用方拿到 plain text 后再拼给最终 JSON 主脑。
    """
    kwargs = dict(
        model=model,
        messages=messages,
        temperature=0.6 if mode == "business" else 0.8,
        max_tokens=500 if mode == "business" else 900,
    )
    if response_json:
        kwargs["response_format"] = {"type": "json_object"}
    response = await client.chat.completions.create(**kwargs)
    raw = response.choices[0].message.content or ""
    if not response_json:
        return raw.strip()
    parsed = _extract_json_object(raw)
    if parsed is None:
        raw_text = raw.strip()
        logger.warning(
            "OpenAI invalid JSON | mode=%s | model=%s | raw_len=%s",
            mode,
            model,
            len(raw),
        )
        # Claude/OpenAI-compatible routes can ignore response_format and return a
        # perfectly usable natural-language reply. In private windows we do not
        # need should_reply/risk_note, so preserve the answer instead of turning
        # it into the annoying "刚才有点卡" fallback. Beibei-specific callers
        # still sanitize the visible reply after this function returns.
        if mode == "private" and raw_text:
            # PATCH 3：私聊非 JSON 自然语言截断到 3500，避免 Telegram 4096 上限 + 防止日志/DB 爆炸
            truncated = _truncate_private_raw_text(raw_text, _PRIVATE_RAW_TEXT_MAX_CHARS)
            return _normalize_result({"reply_text": truncated, "sticker_type": None}, mode)
        raise ValueError(f"model returned non-json response: {model}")
    return _normalize_result(parsed, mode)


async def _do_chat_with_retry(model: str, messages: list, mode: str, response_json: bool):
    """PATCH 4：对单个 model 加上临时错误指数退避（2 / 4 / 8 秒，最多 3 次重试）。

    非临时错误（鉴权 / 400 / JSON 解析）一次性 raise，由上层走 BACKUP_MODEL 或 fallback。
    """
    backoffs = (2, 4, 8)
    last_err: Exception | None = None
    for attempt in range(len(backoffs) + 1):
        try:
            return await _do_chat(model, messages, mode, response_json=response_json)
        except Exception as e:
            last_err = e
            if attempt >= len(backoffs):
                raise
            if not _is_transient_error(e):
                raise
            wait = backoffs[attempt]
            logger.warning(
                "OpenAI transient error, retrying | mode=%s | model=%s | attempt=%d | wait=%ds | err_type=%s",
                mode, model, attempt + 1, wait, type(e).__name__,
            )
            await asyncio.sleep(wait)
    if last_err is not None:
        raise last_err
    return _fallback_for_mode(mode) if response_json else ""


async def call_openai(
    messages: list,
    model: str,
    mode: str,
    response_json: bool = True,
    chat_id: str | int | None = None,
):
    """主调用入口。

    response_json=True（默认）：返回 dict（含 reply_text 等字段）
    response_json=False：返回 plain text（视觉摘要 / 中间分析专用）

    PATCH 4：
      - 新增可选 chat_id：同一 chat_id 串行执行；None 时不加锁，保持原行为
      - 临时错误（429/timeout/5xx/连接）走 2/4/8 秒指数退避，最多 3 次

    主模型失败时仍尝试 BACKUP_MODEL；BACKUP_MODEL 再失败回 mode 的 fallback dict。
    日志只打 mode/model/err_type，**不写正文 / prompt / raw_text**。
    """
    lock = _get_chat_lock(chat_id)

    async def _runner():
        try:
            return await _do_chat_with_retry(model, messages, mode, response_json)
        except Exception as e:
            logger.exception(
                "OpenAI call failed | mode=%s | model=%s | json=%s | err_type=%s",
                mode, model, response_json, type(e).__name__,
            )
        if model != BACKUP_MODEL and BACKUP_MODEL:
            try:
                return await _do_chat_with_retry(BACKUP_MODEL, messages, mode, response_json)
            except Exception as backup_err:
                logger.exception(
                    "Backup OpenAI call failed | mode=%s | backup=%s | json=%s | err_type=%s",
                    mode, BACKUP_MODEL, response_json, type(backup_err).__name__,
                )
        return _fallback_for_mode(mode) if response_json else ""

    if lock is None:
        return await _runner()
    async with lock:
        return await _runner()


async def transcribe_voice(file_path: str) -> str:
    """语音转文字。主模型与 fallback 列表逐个尝试，任何一个成功即返回。"""
    try:
        async with aiofiles.open(file_path, "rb") as audio_file:
            audio_bytes = await audio_file.read()
    except Exception as e:
        logger.exception("Voice file read failed | file=%s | err=%s", file_path, e)
        return ""

    models: list[str] = []
    for model in [TRANSCRIBE_MODEL, *TRANSCRIBE_FALLBACK_MODELS]:
        if model and model not in models:
            models.append(model)

    for model in models:
        try:
            audio_io = io.BytesIO(audio_bytes)
            audio_io.name = "voice.mp3"
            transcript = await client.audio.transcriptions.create(
                model=model,
                file=audio_io,
                prompt="普通话，粤语，四川话，上海话，闽南语，简体中文。",
            )
            text = getattr(transcript, "text", "") or ""
            if text.strip():
                logger.info("Voice transcription ok | file=%s | model=%s | len=%s", file_path, model, len(text))
                return text
        except Exception as e:
            logger.exception("Voice transcription failed | file=%s | model=%s | err=%s", file_path, model, e)
    return ""
