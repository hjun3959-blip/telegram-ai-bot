"""统一发送层。

加固重点：
- send_long_text 分片更稳：长度优先按 MAX_TEXT_REPLY 切，遇到换行优先在换行处切，并避免死循环
- send_long_text 在 business_connection_id 为 None 时也能正常工作
- send_reply 在 sticker_type 对应 file_id 为空字符串时不会触发 API 报错
- send_reply 把 business_connection_id 透传给 send_sticker
- 任何发送异常都记录日志，但不向上抛出，避免单条消息把整个事件循环炸掉
"""

from aiogram import Bot

from config import MAX_TEXT_REPLY, STICKER_MAP
from utils.logger import setup_logging

logger = setup_logging()


def clean_reply_text(text: str) -> str:
    text = (text or "").strip()
    fixes = {
        "晚安渠": "晚安，早点休息",
        "晚安去": "晚安，早点休息",
        "早点休息渠": "早点休息",
        "早点睡渠": "早点睡",
    }
    for bad, good in fixes.items():
        text = text.replace(bad, good)
    return text.replace("。。", "。").replace("，，", "，").replace("，。", "。")


def _split_chunks(text: str, max_len: int) -> list[str]:
    """安全切片：保证每次切出的 chunk 长度 > 0 且 <= max_len，不会死循环。"""
    if max_len <= 0:
        return [text] if text else []
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        chunk = remaining[:max_len]
        # 优先在换行处切；至少留 500 字符避免太碎
        split_at = chunk.rfind("\n")
        if split_at > 500:
            chunk = chunk[:split_at]
        # 保底：万一 chunk 被切空，强制使用 max_len
        if not chunk:
            chunk = remaining[:max_len]
        chunks.append(chunk)
        remaining = remaining[len(chunk):].lstrip("\n")
    return chunks


async def send_long_text(
    bot: Bot,
    chat_id: int,
    text: str,
    business_connection_id: str | None = None,
):
    text = clean_reply_text(text)
    if not text:
        return
    chunks = _split_chunks(text, MAX_TEXT_REPLY)
    kwargs = {}
    if business_connection_id:
        kwargs["business_connection_id"] = business_connection_id
    for chunk in chunks:
        try:
            await bot.send_message(chat_id, chunk, **kwargs)
        except Exception as e:
            logger.exception("send_message failed | chat_id=%s | len=%s | err=%s", chat_id, len(chunk), e)
            # 一段失败时不再继续发剩余段，避免把残缺消息推完
            return


async def send_reply(
    bot: Bot,
    chat_id: int,
    result: dict,
    model_used: str,
    business_connection_id: str | None = None,
    sticker_file_id_override: str | None = None,
):
    """统一发送出口。

    sticker_file_id_override：斗图弹药库从历史素材中预选出一个 file_id 时可传入，
    此时不再查 STICKER_MAP。调用方需自行保证 reuse_in_same_turn=false（已在
    pick_media_asset 里靠 exclude_file_unique_id 实现）。
    """
    text = clean_reply_text((result or {}).get("reply_text") or "")
    sticker_type = (result or {}).get("sticker_type")

    if text:
        await send_long_text(bot, chat_id, text, business_connection_id=business_connection_id)

    # 贴纸保护：优先用调用方传入的 override；否则查 STICKER_MAP。
    # STICKER_MAP 为空是允许的——不会报错，只是不发贴纸。
    sticker_file_id = (sticker_file_id_override or "").strip()
    if not sticker_file_id and isinstance(sticker_type, str) and sticker_type and sticker_type.lower() not in {"null", "none"}:
        sticker_file_id = (STICKER_MAP.get(sticker_type) or "").strip()

    if sticker_file_id:
        kwargs = {}
        if business_connection_id:
            kwargs["business_connection_id"] = business_connection_id
        try:
            await bot.send_sticker(chat_id, sticker_file_id, **kwargs)
        except Exception as e:
            logger.warning("sticker send failed | sticker=%s | err=%s", sticker_type, e)
    elif sticker_type and not sticker_file_id:
        # 模型选了类型但没有可用 file_id，直接忽略不报错
        logger.debug("sticker_type set but file_id empty | type=%s", sticker_type)

    logger.info(
        "reply sent | model=%s | text_len=%s | sticker=%s | chat_id=%s | business=%s",
        model_used,
        len(text),
        sticker_type,
        chat_id,
        bool(business_connection_id),
    )
