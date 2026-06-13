"""OpenAI/兼容 API 调用封装。

加固要点：
- _MODE_PARAMS dict 统一管理各 mode 的 temperature / max_tokens
- admin_brain mode: temperature=0.2, max_tokens=2000，适合代码/部署任务
- _extract_json_object 兼容代码块、首尾噪声、单引号等异常输出
- _coerce_bool 把字符串 'false'/'0'/'no' 等正确解析为 False
- _normalize_result 在空回复时给出合理的 should_reply 推断
- call_openai 主模型失败时尝试 BACKUP_MODEL
"""

import asyncio
import io
import json
import re

import aiofiles
from openai import AsyncOpenAI

from config import (
    ADMIN_BRAIN_MAX_TOKENS,
    ADMIN_BRAIN_TEMPERATURE,
    BACKUP_MODEL,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    TRANSCRIBE_FALLBACK_MODELS,
    TRANSCRIBE_MODEL,
)
from utils.logger import setup_logging

client = AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
logger = setup_logging()

_PRIVATE_RAW_TEXT_MAX_CHARS = 3500

# 各 mode 采样参数。admin_brain 低温保证代码任务稳定输出，token 放大支持长回复。
_MODE_PARAMS: dict[str, dict] = {
    "business":    {"temperature": 0.6, "max_tokens": 500},
    "private":     {"temperature": 0.8, "max_tokens": 900},
    "admin_brain": {"temperature": ADMIN_BRAIN_TEMPERATURE, "max_tokens": ADMIN_BRAIN_MAX_TOKENS},
}
_DEFAULT_PARAMS = {"temperature": 0.8, "max_tokens": 900}


def _truncate_private_raw_text(text: str, max_chars: int = _PRIVATE_RAW_TEXT_MAX_CHARS) -> str:
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


def _is_transient_error(err: Exception) -> bool:
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
    return {"reply_text": "", "sticker_type": None, "should_reply": False, "risk_note": ""}


def _fallback_for_mode(mode: str) -> dict:
    return fallback_business() if mode == "business" else fallback_private()


_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _extract_json_object(raw: str) -> dict | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else None
    except Exception:
        pass
    m = _CODE_FENCE_RE.search(text)
    if m:
        inner = m.group(1).strip()
        try:
            result = json.loads(inner)
            if isinstance(result, dict):
                return result
        except Exception:
            pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    candidate = match.group(0)
    try:
        result = json.loads(candidate)
        return result if isinstance(result, dict) else None
    except Exception:
        try:
            result = json.loads(candidate.replace("'", '"'))
            return result if isinstance(result, dict) else None
        except Exception:
            return None


_FALSE_STRS = {"false", "0", "no", "n", "off", "none", "null", "否", "不", "假"}
_TRUE_STRS = {"true", "1", "yes", "y", "on", "是", "对", "真"}


def _coerce_bool(value, default: bool) -> bool:
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
    if not token or token.lower() in {"null", "none", "false"}:
        return None
    return token


def _normalize_result(data: dict | None, mode: str) -> dict:
    if not isinstance(data, dict):
        return _fallback_for_mode(mode)
    reply_text = str(data.get("reply_text") or "").strip()
    sticker_type = _normalize_sticker(data.get("sticker_type"))
    if mode == "business":
        has_content = bool(reply_text or sticker_type)
        if "should_reply" in data:
            should_reply = _coerce_bool(data.get("should_reply"), default=has_content)
        else:
            should_reply = has_content
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
    params = _MODE_PARAMS.get(mode, _DEFAULT_PARAMS)
    kwargs = dict(
        model=model,
        messages=messages,
        temperature=params["temperature"],
        max_tokens=params["max_tokens"],
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
            mode, model, len(raw),
        )
        if mode in ("private", "admin_brain") and raw_text:
            truncated = _truncate_private_raw_text(raw_text, _PRIVATE_RAW_TEXT_MAX_CHARS)
            return _normalize_result({"reply_text": truncated, "sticker_type": None}, mode)
        raise ValueError(f"model returned non-json response: {model}")
    return _normalize_result(parsed, mode)


async def _do_chat_with_retry(model: str, messages: list, mode: str, response_json: bool):
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
    """主调用入口。mode: business / private / admin_brain"""
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
