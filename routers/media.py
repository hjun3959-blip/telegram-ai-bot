"""媒体路由：图片、语音、视频、贴纸、GIF。

Business 与 private 共享底层处理逻辑，但对外通过两个独立的 handler 注册，
确保 aiogram 能正确分发 business_message 与普通 message。

Business 媒体处理特别注意：
1. 必须先做 should_skip 过滤（群聊/非私聊不处理）
2. business 模式下：先判断自发消息与自发静默窗口，避免回复 owner 自己刚发的内容
3. business 模式下：视频暂时保守跳过（见 video_handler 注释）——
   原因是 Telegram Bot API 在 business_connection 上下文中下载 video 不稳定，
   而且代聊里收到视频后机器人若误回会比较突兀，宁可让 owner 手动接。
4. 发送回复必须带 business_connection_id；getattr 容错。
5. 贴纸/GIF 同样要遵守 business should_reply 静默规则。
"""

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.types import Message

from config import CORE_MODEL, MAX_VIDEO_SIZE, STICKER_MAP, VISION_MODEL
from services.business_memory_service import get_history as biz_get_history, save_history as biz_save_history
from services.chat_action_service import send_chat_action_safe
from services.atree_models import GENERAL_VISION_MODEL, pick_beibei_vision_model, pick_owner_vision_model
from services.context_service import (
    choose_model,
    get_chat_mode,
    is_in_owner_cooldown,
    is_in_self_silence,
    is_owner,
    is_self_message,
    mark_self_silence,
    owner_cooldown_remaining,
    should_skip_message,
    system_prompt_for_mode,
)
from services.history_service import trim_messages
from services.media_service import (
    encode_image_to_base64,
    extract_video_frames,
    media_tmp_dir,
    media_tmp_path,
    run_ffmpeg,
    safe_remove,
    safe_rmtree,
)
from services.plog_service import remember_photo as plog_remember_photo
from services.message_service import store_message
from services.openai_service import call_openai, transcribe_voice
from services.reply_service import send_reply
from services.atree_persona import (
    ATREE_COMMITMENT_FORBIDDEN_WORDS,
    ATREE_VISIBLE_FORBIDDEN_WORDS,
    sanitize_visible_reply,
)
from services.typing_delay_service import human_typing_delay
from services.self_media_service import bump_media_use, pick_media_asset, record_incoming_media, record_self_media
from services.xiaopang_service import (
    build_system_prompt_with_xiaopang,
    is_xiaopang,
    maybe_hit_xiaopang_reminders,
    remember_xiaopang_identity,
    xiaopang_scope,
)
from utils.logger import setup_logging

logger = setup_logging()

router = Router(name="media")


async def _business_self_check(message: Message, content_type: str) -> bool:
    """business 模式下的“是不是阿君自发或静默窗口”检查。

    返回 True 表示跳过本次媒体处理（不调用模型、不发送回复）。
    静默不等于丢弃：命中 is_self_message 时会同步采集素材到 self_media_assets，
    并依然入 message_log，保持原有历史可追溯。
    """
    if get_chat_mode(message) != "business":
        return False
    if is_self_message(message):
        mark_self_silence(message)
        # 静默采集：owner 自发的贴纸/animation/photo/voice/video 都记上素材库。
        await record_self_media(message, mode="business")
        await store_message(message, "outgoing", f"[自发静默采集:{content_type}]", "system", scope="self")
        logger.info(
            "business media self-message silenced & collected | type=%s | chat_id=%s",
            content_type, message.chat.id,
        )
        return True
    if is_in_self_silence(message):
        logger.info("business media in self-silence window | type=%s | chat_id=%s", content_type, message.chat.id)
        return True
    # “抢话修复”：owner 刚发过消息后，同 chat 的 incoming 媒体也先保守静默。
    # 仅记入消息日志，不调模型、不发回复。
    if is_in_owner_cooldown(message):
        try:
            await store_message(
                message,
                "incoming",
                f"[owner_cooldown:{content_type}]",
                "text_suppressed",
                scope="owner_cooldown",
            )
        except Exception:
            pass
        logger.info(
            "business media in owner-cooldown | type=%s | chat_id=%s | remaining=%.1fs",
            content_type, message.chat.id, owner_cooldown_remaining(message),
        )
        return True
    return False


async def _business_non_contact_check(message: Message, content_type: str) -> bool:
    """已废弃的硬性非联系人拦截入口。

    保留函数签名以兼容已有调用点；现在永远返回 False（放行）。
    陌生人/无关消息由 BUSINESS_SYSTEM_PROMPT 让模型自行 should_reply=false；
    广告由 ad_keyword_hit 统一拦截，不在这里处理。
    """
    return False


async def _owner_self_sticker_or_gif_check(message: Message) -> bool:
    """涵盖“business + private”两个窗口里阿君自发的 sticker/animation。

    business 那边已经被 _business_self_check 处理；这个函数主要补 private 场景：
    主人private里发贴纸/GIF，机器人也不应该机械回复，但需要采集进素材库。
    返回 True 表示要静默（不走后面的模型回复逻辑）。
    """
    mode = get_chat_mode(message)
    if mode != "private":
        return False
    # private 里，只针对 sticker / animation；photo / voice 仍作为工具查询使用。
    if not (getattr(message, "sticker", None) or getattr(message, "animation", None)):
        return False
    if not is_owner(message):
        return False
    await record_self_media(message, mode="private")
    content_type = "animation" if getattr(message, "animation", None) else "sticker"
    await store_message(message, "outgoing", f"[自发静默采集:{content_type}]", "system", scope="self")
    logger.info(
        "private owner self sticker/gif silenced & collected | type=%s | chat_id=%s",
        content_type, message.chat.id,
    )
    return True


def _should_reply_business(result: dict) -> bool:
    """统一 business 静默判断，兼容 should_reply 与 reply_text/sticker_type 推断。"""
    reply_text = (result.get("reply_text") or "").strip()
    sticker_type = result.get("sticker_type")
    return bool(result.get("should_reply", bool(reply_text or sticker_type))) and bool(reply_text or sticker_type)


# ---------------- Beibei media 出站安全（灰度前补丁 PATCH 1 + PATCH 5） ----------------
#
# 贝贝/小胖在 photo / voice / sticker / GIF / video 路径上拿到的回复，必须先过
# sanitize_visible_reply（已有阿树持人格统一规则）再 send_reply。
# 命中后台/系统/承诺词时再额外打一条「red-line hit」日志，只记元信息（场景 + chat_id），
# 不写正文；如果 bot 还活着，再尽力通过 dedup_alert 给 owner 报一句。
# 阿君 owner / 普通用户 / Business 普通聊天 不调用本函数。

_BEIBEI_REDLINE_TERMS = tuple(set(ATREE_VISIBLE_FORBIDDEN_WORDS) | set(ATREE_COMMITMENT_FORBIDDEN_WORDS))


def _hit_redline_terms(text: str) -> list[str]:
    if not text:
        return []
    hits: list[str] = []
    for w in _BEIBEI_REDLINE_TERMS:
        if w and w in text:
            hits.append(w)
    return hits


async def _sanitize_beibei_result(result: dict, *, bot: Bot | None, scene: str, chat_id) -> dict:
    """贝贝侧媒体出站统一 sanitize + 命中红线时尽力告警 owner。

    - 必须在 send_reply 之前调用
    - 永远不抛异常；任何兜底情况下都会替换为「嗯，我在。」级安全短句
    - 不写正文到日志；告警失败不影响主流程
    """
    try:
        original = (result.get("reply_text") or "") if isinstance(result, dict) else ""
        hits = _hit_redline_terms(original)
        try:
            cleaned = sanitize_visible_reply(original)
        except Exception:
            cleaned = "嗯，我在。"
        if not isinstance(result, dict):
            result = {"reply_text": cleaned, "sticker_type": None}
        else:
            result["reply_text"] = cleaned
        if hits:
            try:
                logger.warning(
                    "beibei_redline_hit | scene=%s | chat_id=%s | hit_count=%d",
                    scene, chat_id, len(hits),
                )
            except Exception:
                pass
            if bot is not None:
                try:
                    from services.alert_service import dedup_alert
                    key = f"beibei_redline:{scene}:{chat_id}:{','.join(sorted(set(hits)))[:60]}"
                    notice = (
                        f"贝贝侧出站红线命中（{scene}）\n"
                        f"命中词：{', '.join(sorted(set(hits)))}\n"
                        f"已对发出内容做替换；这是状态通报，不需要立刻动。"
                    )
                    await dedup_alert(bot, key, notice)
                except Exception:
                    pass
        return result
    except Exception:
        try:
            logger.warning("sanitize beibei result failed | scene=%s", scene)
        except Exception:
            pass
        return {"reply_text": "嗯，我在。", "sticker_type": None}


# 反模板化硬红线：不管模型那边出什么，这些句式绝不能上屏。
# 加进最终 JSON 主脑的 user prompt 里，明示禁止。
_ANTI_TEMPLATE_BAN_LIST = [
    "看到这个心情都亮了",
    "看到这个心情都好起来了",
    "一看到这个就笑了",
    "这张图太可爱了",
    "谢谢你分享这张图",
    "收到你发的图",
    "收到你的贴纸",
    "多么可爱的表情",
    "看到你发这个",
]


def _format_recent_history_for_prompt(history: list[dict], max_turns: int = 6) -> str:
    """把最近几轮对话打成人话，供最终主脑看上下文。不暴露 file_id。"""
    if not history:
        return "（这是本次会话的第一句，之前没有聊过）"
    tail = history[-max_turns * 2 :]
    lines: list[str] = []
    for m in tail:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, list):
            # 多模态：只拼接 text 部分
            text_parts = [p.get("text", "") for p in content if isinstance(p, dict)]
            content = " ".join([t for t in text_parts if t])
        content = (content or "").strip()
        if not content:
            continue
        speaker = "对方" if role == "user" else ("我" if role == "assistant" else role)
        lines.append(f"{speaker}：{content}")
    return "\n".join(lines) if lines else "（没有可用历史）"


def _build_anti_template_clause() -> str:
    bans = "、“".join(_ANTI_TEMPLATE_BAN_LIST)
    return (
        "硬红线：绝不要说“" + bans + "”这类万能模板。\n"
        "不要描述图本身/贴纸本身长什么样，不要说“收到你的图”。\n"
        "要紧贴上一句、图里在表达的东西、贝贝当下情绪。"
    )


async def _final_reply_via_core_model(
    message: Message,
    mode: str,
    media_kind: str,
    visual_or_human_summary: str,
    extra_user_caption: str = "",
) -> dict:
    """拿不含 file_id 的人话描述 + 近期聊天历史，交给 CORE_MODEL=gpt-5.5 出最终 JSON。

    仅限 business（贝贝/联系人）。private 调用方可选择复用，但默认由原逻辑 VISION 出。
    """
    user_id = message.from_user.id if message.from_user else 0
    history = biz_get_history(user_id) if mode == "business" else []
    hist_block = _format_recent_history_for_prompt(history)
    system_prompt = await build_system_prompt_with_xiaopang(system_prompt_for_mode(message), message)
    user_caption_part = f"\n对方这次随{media_kind}发的话：{extra_user_caption.strip()}" if extra_user_caption.strip() else ""
    user_prompt = (
        f"刚才对方发了一个{media_kind}。\n"
        f"这个{media_kind}的客观描述（仅供你参考，不要复述）：\n{visual_or_human_summary.strip() or '暂无摘要'}"
        f"{user_caption_part}\n\n"
        "最近聊天上下文（从上到下越近，我是你）：\n"
        f"{hist_block}\n\n"
        "任务：结合上一句、这个" + media_kind + "在表达的东西、与对方当下情绪，像真人一样接一句。\n"
        "可选策略：短文字 / 几个 emoji / 选一个 sticker_type / 什么都不回（should_reply=false）。\n"
        + _build_anti_template_clause()
        + "\n请以 JSON 输出，字段严格遵守 system prompt 里的约束。"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    messages = trim_messages(messages)
    return await call_openai(messages, CORE_MODEL, mode, chat_id=message.chat.id)


async def _visual_summary_via_vision(message: Message, encoded_b64: str, caption_hint: str) -> str:
    """第一段：视觉模型只出“客观视觉摘要”，不负责聊天。返 plain text。

    明确要求：描述图里在表达什么、人物情绪、错机与文本；不产出聊天回复、不产出 JSON。
    """
    sys = (
        "你是一个中文视觉描述助手，只负责看图出摘要，不负责聊天。\n"
        "输出要求：\n"
        " - 不超过 5 句中文\n"
        " - 仅描述画面、主体、动作、表情、文字、画风、明显情绪\n"
        " - 不要输出任何问候语、问题、聊天语气、推销语、“我看到”这种第一人称语气\n"
        " - 不要猜测发送人意图\n"
        " - 不输出 JSON，只输出纯文本\n"
        " - 不要出现“file_id”/“贴纸集”/“set_name”/URL/base64 这种素材 ID信息"
    )
    user_text = caption_hint or "请描述这张图里在表达什么。"
    msgs = [
        {"role": "system", "content": sys},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded_b64}"}},
            ],
        },
    ]
    try:
        vision_model = await _vision_model_for_message(message)
        summary = await call_openai(msgs, vision_model, "private", response_json=False, chat_id=message.chat.id)
    except Exception as e:
        logger.exception("vision summary failed | err=%s", e)
        summary = ""
    if isinstance(summary, dict):  # 万一 fallback 返 dict
        summary = str(summary.get("reply_text") or "")
    return str(summary or "").strip()


async def _vision_model_for_message(message: Message) -> str:
    """核心窗口视觉用 Pro，普通窗口视觉维持 Flash Lite。"""
    if await is_xiaopang(message):
        return pick_beibei_vision_model()
    if is_owner(message):
        return pick_owner_vision_model()
    return GENERAL_VISION_MODEL


# ---------------- Photo ----------------

# /plog 与 /magnet 待处理照片缓存目录：跟 temp_*.jpg（视觉用，处理完就删）分开放，
# 避免 finally 里的 safe_remove 把缓存也清掉。
import os as _os
# 默认放到受控的 tmp/plog_cache 下（与视觉用 temp_*.jpg 同一可写根目录），
# 避免在没有写权限的项目根目录创建缓存目录；PLOG_CACHE_DIR 仍可显式覆盖。
_PLOG_CACHE_DIR = _os.environ.get("PLOG_CACHE_DIR", "") or media_tmp_dir("plog_cache")


# 图+文字生图/改图的 caption 触发词。带 / 与不带 / 两种都接：
#   /改图 描述 / 改图 描述 / 生图 描述 / 图生图 描述 / /imgedit 描述 / /edit 描述
# 注意：必须是「触发词 + 描述」结构才命中，纯图片普通配文不会被劫持。
_IMGEDIT_CAPTION_WORDS = ("改图", "图生图", "图加字", "图文生图")
_IMGEDIT_CAPTION_SLASH = ("/改图", "/生图", "/图生图", "/imgedit", "/edit", "/图加字", "/图文生图")

# 图生视频的 caption 触发词。带 / 与不带 / 两种都接：
#   图生视频 描述 / 视频 描述 / 生成视频 描述 / 图转视频 描述 / /图生视频 描述 / /i2v 描述
# 注意：必须是「触发词 + 描述」结构才命中，纯图片普通配文不会被劫持。
_I2V_CAPTION_WORDS = ("图生视频", "生成视频", "图转视频", "视频")
_I2V_CAPTION_SLASH = ("/图生视频", "/视频", "/生成视频", "/图转视频", "/i2v")


def _caption_i2v_intent(caption: str | None) -> tuple[bool, str]:
    """caption 是「图生视频」触发时，返回 (True, 描述)；否则 (False, "").

    支持带 / 与不带 / 两种写法。不带 / 时只认明确的中文触发词开头，避免劫持普通配文。
    """
    raw = (caption or "").strip()
    if not raw:
        return False, ""
    head, _, rest = raw.partition(" ")
    cmd = head.split("@", 1)[0]
    cmd_lower = cmd.lower()
    if cmd_lower in _I2V_CAPTION_SLASH:
        return True, rest.strip()
    if cmd in _I2V_CAPTION_WORDS:
        return True, rest.strip()
    return False, ""


def _caption_imgedit_intent(caption: str | None) -> tuple[bool, str]:
    """caption 是「图+文字生图/改图」触发时，返回 (True, 指令)；否则 (False, "").

    支持带 / 与不带 / 两种写法。不带 / 时只认明确的中文触发词开头，避免劫持普通配文。
    """
    raw = (caption or "").strip()
    if not raw:
        return False, ""
    head, _, rest = raw.partition(" ")
    cmd = head.split("@", 1)[0]
    cmd_lower = cmd.lower()
    if cmd_lower in _IMGEDIT_CAPTION_SLASH:
        return True, rest.strip()
    # 不带 / 的中文触发词（如「改图 描述」「生图 描述」）
    if cmd in _IMGEDIT_CAPTION_WORDS or cmd == "生图":
        return True, rest.strip()
    return False, ""


def _caption_is_plog_or_magnet(caption: str | None) -> tuple[str | None, str]:
    """caption 是图片创作命令时，返回 (tool, 风格参数)；否则 (None, "").

    tool 取值："plog" / "magnet" / "y2k" / "poster" / "imgedit" / "i2v"。
    """
    raw = (caption or "").strip()

    # 先看图生视频（中文触发词不带 / 也能命中）。放在 imgedit 之前，
    # 避免「图生视频」被泛化误判；二者首词精确匹配，不会互相劫持。
    hit, instruction = _caption_i2v_intent(raw)
    if hit:
        return "i2v", instruction

    # 再看图+文字生图/改图（中文触发词不带 / 也能命中）
    hit, instruction = _caption_imgedit_intent(raw)
    if hit:
        return "imgedit", instruction

    if not raw.startswith("/"):
        return None, ""
    head, _, rest = raw.partition(" ")
    cmd = head.split("@", 1)[0].lower()
    if cmd == "/plog":
        return "plog", rest.strip()
    if cmd in ("/magnet", "/fridge"):
        return "magnet", rest.strip()
    if cmd == "/y2k":
        return "y2k", rest.strip()
    if cmd in ("/poster", "/starposter"):
        return "poster", rest.strip()
    return None, ""


async def _handle_photo(message: Message, bot: Bot):
    if should_skip_message(message):
        return
    if await _business_self_check(message, "photo"):
        return

    mode = get_chat_mode(message)
    file_path = media_tmp_path(f"temp_{message.from_user.id}_{message.message_id}.jpg")
    is_xp = await is_xiaopang(message)
    scope = await xiaopang_scope(message) if is_xp else "default"

    if is_xp:
        await remember_xiaopang_identity(message)
        await maybe_hit_xiaopang_reminders(message, message.caption or "", bot)

    await store_message(message, "incoming", message.caption or "[图片]", "photo", scope=scope)

    # business 非联系人静默：incoming 已入库，不下载、不调模型、不回复。
    if await _business_non_contact_check(message, "photo"):
        return

    try:
        file = await bot.get_file(message.photo[-1].file_id)
        await bot.download_file(file.file_path, file_path)
        encoded = await encode_image_to_base64(file_path)
        user_caption = (message.caption or "").strip()

        # 拟真交互：图片分析前发 UPLOAD_PHOTO
        await send_chat_action_safe(
            bot,
            message.chat.id,
            ChatAction.UPLOAD_PHOTO,
            business_connection_id=getattr(message, "business_connection_id", None),
        )

        # 第一段：VISION_MODEL 只出“客观视觉摘要”，不负责聊天
        caption_hint = user_caption or "请描述这张图里在表达什么。"
        visual_summary = await _visual_summary_via_vision(message, encoded, caption_hint)
        # 视觉摘要是中间分析产物，不会上屏，也不入库为 outgoing

        if mode == "business":
            # 第二段：CORE_MODEL=gpt-5.5 拿视觉摘要 + caption + 历史 出最终 JSON
            result = await _final_reply_via_core_model(
                message,
                mode="business",
                media_kind="图片",
                visual_or_human_summary=visual_summary,
                extra_user_caption=user_caption,
            )

            if not _should_reply_business(result):
                # 静默也要 save_history，让下一轮文本知道“刚才对方发过一张图”
                user_id = message.from_user.id if message.from_user else 0
                user_log = f"[图片]{('：' + user_caption) if user_caption else ''}".strip()
                biz_save_history(user_id, user_log, "")
                await store_message(message, "outgoing", "[静默跳过:photo]", "system", scope=scope)
                return

            if is_xp:
                result = await _sanitize_beibei_result(
                    result, bot=bot, scene="media_photo_business", chat_id=message.chat.id,
                )
            reply_text_for_delay = (result.get("reply_text") or "")
            sticker_only = (not reply_text_for_delay.strip()) and bool(result.get("sticker_type"))
            await human_typing_delay(
                bot,
                message.chat.id,
                reply_text_for_delay,
                mode="business",
                has_sticker_only=sticker_only,
                business_connection_id=getattr(message, "business_connection_id", None),
            )
            if is_in_owner_cooldown(message) or is_in_self_silence(message):
                await store_message(message, "outgoing", "[延迟后 cooldown 静默:photo]", "system", scope=scope)
                return

            await send_reply(
                bot,
                message.chat.id,
                result,
                CORE_MODEL,
                business_connection_id=getattr(message, "business_connection_id", None),
            )
            # 保存到共享记忆：让下轮文本看到“刚刚对方发了图，我回了这句”
            user_id = message.from_user.id if message.from_user else 0
            user_log = f"[图片]{('：' + user_caption) if user_caption else ''}".strip()
            biz_save_history(user_id, user_log, reply_text_for_delay.strip())
            await store_message(message, "outgoing", reply_text_for_delay.strip(), "text", scope=scope)
            return

        # private 模式：先把照片缓存到 plog 池（仅 private，业务窗口不缓存），
        # 这样用户后续发 /plog 或 /magnet 就能拿到这张照片作参考图。
        # 文件单独 copy 出去，避免 finally 里 safe_remove 把视觉用 temp 一起删后影响 plog。
        try:
            _os.makedirs(_PLOG_CACHE_DIR, exist_ok=True)
            cache_path = _os.path.join(
                _PLOG_CACHE_DIR,
                f"plog_{message.from_user.id}.jpg",
            )
            # 直接读原 file_path 写到 cache_path，简单可靠
            with open(file_path, "rb") as _src, open(cache_path, "wb") as _dst:
                _dst.write(_src.read())
            plog_remember_photo(
                message.from_user.id,
                file_path=cache_path,
                file_id=(message.photo[-1].file_id if message.photo else None),
                caption=user_caption or None,
            )
        except Exception as _e:
            logger.warning("plog cache photo failed | err=%s", _e)

        # caption 里直接带图片创作命令：跳过 VISION 聊天回复，转入对应工具流程
        tool_in_caption, style_arg = _caption_is_plog_or_magnet(user_caption)
        if tool_in_caption == "plog":
            # 延迟导入避免循环
            from routers.private import run_plog_for_user
            await run_plog_for_user(bot, message, style_arg)
            return
        if tool_in_caption == "magnet":
            from routers.private import run_magnet_for_user
            await run_magnet_for_user(bot, message, style_arg)
            return
        if tool_in_caption == "y2k":
            from routers.private import run_y2k_for_user
            await run_y2k_for_user(bot, message, style_arg)
            return
        if tool_in_caption == "poster":
            from routers.private import run_poster_for_user
            await run_poster_for_user(bot, message, style_arg)
            return
        if tool_in_caption == "imgedit":
            from routers.private import run_imgedit_for_user
            await run_imgedit_for_user(bot, message, style_arg)
            return
        if tool_in_caption == "i2v":
            from routers.private import run_i2v_for_user
            await run_i2v_for_user(bot, message, style_arg)
            return

        # private 模式 + 用户之前在风格菜单点过 /plog /magnet /y2k /poster 的风格但没照片 →
        # 现在刚发了照片，自动消费 pending，直接走对应 runner，跳过 VISION。
        # 严格隔离 business：业务窗口绝不触发自动生成。
        if mode == "private":
            try:
                from services.pending_style_service import consume_pending_style, get_pending_style
                from routers.private import (
                    _style_start_text,
                    run_imgedit_for_user,
                    run_magnet_for_user,
                    run_plog_for_user,
                    run_poster_for_user,
                    run_y2k_for_user,
                    send_long_text as _send_long_text,
                )
                pending = get_pending_style(message.from_user.id) if message.from_user else None
                if pending and pending.tool in ("plog", "magnet", "y2k", "poster", "imgedit"):
                    consume_pending_style(message.from_user.id)
                    # 风格感知状态
                    try:
                        await _send_long_text(bot, message.chat.id, _style_start_text(pending.style, pending.tool))
                    except Exception:
                        pass
                    if pending.tool == "plog":
                        await run_plog_for_user(bot, message, pending.style, silent_status=True)
                    elif pending.tool == "magnet":
                        await run_magnet_for_user(bot, message, pending.style, silent_status=True)
                    elif pending.tool == "y2k":
                        await run_y2k_for_user(bot, message, pending.style, silent_status=True)
                    elif pending.tool == "poster":
                        await run_poster_for_user(bot, message, pending.style, silent_status=True)
                    elif pending.tool == "imgedit":
                        await run_imgedit_for_user(bot, message, pending.style, silent_status=True)
                    return
            except Exception as _pend_err:
                logger.warning("pending style consume failed | err=%s", _pend_err)

        # private 模式：保持原逻辑（VISION 直接出 JSON），不破坏现有功能区行为
        caption_text = user_caption or "请详细描述这张图片，并给出有用、自然的回答。"
        system_prompt = await build_system_prompt_with_xiaopang(system_prompt_for_mode(message), message)
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": caption_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
                ],
            },
        ]
        private_vision_model = await _vision_model_for_message(message)
        result = await call_openai(messages, private_vision_model, mode, chat_id=message.chat.id)
        if is_xp:
            result = await _sanitize_beibei_result(
                result, bot=bot, scene="media_photo_private", chat_id=message.chat.id,
            )
        await send_reply(
            bot,
            message.chat.id,
            result,
            private_vision_model,
            business_connection_id=getattr(message, "business_connection_id", None),
        )
        await store_message(message, "outgoing", result.get("reply_text", ""), "text", scope=scope)
    except Exception as e:
        logger.exception("photo handler failed | chat_id=%s | err=%s", message.chat.id, e)
    finally:
        await safe_remove(file_path)


@router.message(F.photo)
async def photo_handler(message: Message, bot: Bot):
    await _handle_photo(message, bot)


@router.business_message(F.photo)
async def business_photo_handler(message: Message, bot: Bot):
    await _handle_photo(message, bot)


# ---------------- Voice ----------------

async def _handle_voice(message: Message, bot: Bot):
    if should_skip_message(message):
        return
    if await _business_self_check(message, "voice"):
        return

    mode = get_chat_mode(message)
    oga_path = media_tmp_path(f"temp_voice_{message.from_user.id}_{message.message_id}.oga")
    mp3_path = media_tmp_path(f"temp_voice_{message.from_user.id}_{message.message_id}.mp3")
    is_xp = await is_xiaopang(message)
    scope = await xiaopang_scope(message) if is_xp else "default"

    if is_xp:
        await remember_xiaopang_identity(message)
    await store_message(message, "incoming", "[语音]", "voice", scope=scope)

    # business 非联系人静默：不下载、不转录、不调模型。
    if await _business_non_contact_check(message, "voice"):
        return

    try:
        file = await bot.get_file(message.voice.file_id)
        await bot.download_file(file.file_path, oga_path)
        success = await run_ffmpeg(["-y", "-i", oga_path, mp3_path])
        if not success:
            logger.warning("ffmpeg convert failed | oga=%s", oga_path)
            return
        transcript = await transcribe_voice(mp3_path)
        if not transcript.strip():
            if mode == "private":
                await bot.send_message(message.chat.id, "语音转文字接口现在没通，我收到语音了，但暂时识别不出来。")
            return

        if is_xp:
            await maybe_hit_xiaopang_reminders(message, transcript, bot)

        model = choose_model(message, transcript)
        system_prompt = await build_system_prompt_with_xiaopang(system_prompt_for_mode(message), message)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"[语音消息转录]：{transcript}"},
        ]
        # 拟真交互：语音转录后发 TYPING（对方看到“正在输入”）
        await send_chat_action_safe(
            bot,
            message.chat.id,
            ChatAction.TYPING,
            business_connection_id=getattr(message, "business_connection_id", None),
        )
        result = await call_openai(messages, model, mode, chat_id=message.chat.id)

        if mode == "business" and not _should_reply_business(result):
            await store_message(message, "outgoing", "[静默跳过:voice]", "system", scope=scope)
            return

        if is_xp:
            result = await _sanitize_beibei_result(
                result, bot=bot, scene="media_voice", chat_id=message.chat.id,
            )

        if mode == "business":
            reply_text_for_delay = (result.get("reply_text") or "")
            sticker_only = (not reply_text_for_delay.strip()) and bool(result.get("sticker_type"))
            await human_typing_delay(
                bot,
                message.chat.id,
                reply_text_for_delay,
                mode="business",
                has_sticker_only=sticker_only,
                business_connection_id=getattr(message, "business_connection_id", None),
            )
            if is_in_owner_cooldown(message) or is_in_self_silence(message):
                await store_message(message, "outgoing", "[延迟后 cooldown 静默:voice]", "system", scope=scope)
                return

        await send_reply(
            bot,
            message.chat.id,
            result,
            model,
            business_connection_id=getattr(message, "business_connection_id", None),
        )
        await store_message(message, "outgoing", result.get("reply_text", ""), "text", scope=scope)
    except Exception as e:
        logger.exception("voice handler failed | chat_id=%s | err=%s", message.chat.id, e)
    finally:
        await safe_remove(oga_path, mp3_path)


@router.message(F.voice)
async def voice_handler(message: Message, bot: Bot):
    await _handle_voice(message, bot)


@router.business_message(F.voice)
async def business_voice_handler(message: Message, bot: Bot):
    await _handle_voice(message, bot)


# ---------------- Video ----------------
#
# 重要：Business 视频暂不处理。
# 原因有二：
#   1) Telegram Bot API 在 business_connection 上下文中获取视频文件不够稳定，常见超时
#   2) 代聊场景中机器人对视频的回复容易突兀，宁可让阿君手动接
# 因此 video_handler 中显式判断 mode=='business' 时返回，不调用模型。
# private 模式下：核心窗口视频视觉用 Pro，普通窗口维持 Flash Lite。

@router.message(F.video)
async def video_handler(message: Message, bot: Bot):
    if should_skip_message(message):
        return
    mode = get_chat_mode(message)
    # Business 视频保守跳过，见本文件顶部注释。
    if mode == "business":
        logger.info("business video skipped by design | chat_id=%s", message.chat.id)
        return

    if message.video.file_size and message.video.file_size > MAX_VIDEO_SIZE:
        await bot.send_message(message.chat.id, "视频太大啦～官方 Bot API 目前最多只能处理 20MB 以内的视频。")
        return

    video_path = media_tmp_path(f"temp_video_{message.from_user.id}_{message.message_id}.mp4")
    frames_dir = media_tmp_dir(f"frames_{message.from_user.id}_{message.message_id}")
    is_xp = await is_xiaopang(message)
    scope = await xiaopang_scope(message) if is_xp else "default"

    if is_xp:
        await remember_xiaopang_identity(message)
    await store_message(message, "incoming", message.caption or "[视频]", "video", scope=scope)

    try:
        file = await bot.get_file(message.video.file_id)
        await bot.download_file(file.file_path, video_path)
        frames = await extract_video_frames(video_path, frames_dir)
        if not frames:
            return
        image_contents = []
        for frame_path in frames[:4]:
            encoded = await encode_image_to_base64(frame_path)
            image_contents.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
        system_prompt = await build_system_prompt_with_xiaopang(system_prompt_for_mode(message), message)
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这是视频的关键帧，请帮我看懂它在说什么，并给出自然回复。"},
                    *image_contents,
                ],
            },
        ]
        video_vision_model = await _vision_model_for_message(message)
        result = await call_openai(messages, video_vision_model, "private", chat_id=message.chat.id)
        if is_xp:
            result = await _sanitize_beibei_result(
                result, bot=bot, scene="media_video_private", chat_id=message.chat.id,
            )
        await send_reply(bot, message.chat.id, result, video_vision_model)
        await store_message(message, "outgoing", result.get("reply_text", ""), "text", scope=scope)
    except Exception as e:
        logger.exception("video handler failed | chat_id=%s | err=%s", message.chat.id, e)
    finally:
        await safe_remove(video_path)
        await safe_rmtree(frames_dir)


# ---------------- Sticker / GIF ----------------

def _describe_incoming_sticker_or_gif(message: Message) -> tuple[str, str]:
    """把 sticker/animation 翻译成人话给模型，不传 file_id，避免被原样 echo。

    返回 (content_type, 人话描述)。
    """
    if message.animation:
        anim = message.animation
        bits = ["动图 GIF"]
        fname = (getattr(anim, "file_name", None) or "").strip()
        if fname:
            bits.append(f"文件名 {fname}")
        dur = getattr(anim, "duration", None)
        if dur:
            bits.append(f"时长 {dur}s")
        return "gif", "、".join(bits)

    s = message.sticker
    if not s:
        return "sticker", "贴纸"
    bits = ["贴纸"]
    emoji = (getattr(s, "emoji", None) or "").strip()
    if emoji:
        bits.append(f"emoji={emoji}")
    set_name = (getattr(s, "set_name", None) or "").strip()
    if set_name:
        bits.append(f"贴纸集={set_name}")
    if getattr(s, "is_animated", False):
        bits.append("动画贴纸")
    if getattr(s, "is_video", False):
        bits.append("视频贴纸")
    s_type = getattr(s, "type", None)
    if s_type:
        bits.append(f"类型={s_type}")
    return "sticker", "、".join(bits)


def _build_sticker_user_prompt(mode: str, content_type: str, human_desc: str, is_xp: bool) -> str:
    """给模型的指令：把对方表情包当作表达的一部分，结合上下文判意图，再决定怎么接。"""
    common = (
        f"对方刚发了一个{content_type}（{human_desc}）。这不是什么需要分析的图片，是对方表达的一部分。\n"
        "请结合上下文判断对方是在：开玩笑 / 撒娇 / 敷衍 / 挑衅 / 尴尬 / 打招呼 / 结束话题 / 表达情绪，还是只是随手发。\n"
        "然后判断怎么接最像真人。可选策略：\n"
        " - 短文字（1–2 句，紧贴对方情绪）\n"
        " - 几个 emoji 接话\n"
        " - 选一个 sticker_type（laugh/happy/shy/thinking/love，不要选跟对方一模一样的）\n"
        " - 什么都不回（should_reply=false）\n\n"
        "硬红线：\n"
        " - 不要机械地回同一个贴纸或同一个 GIF，不要描述贴纸本身长什么样\n"
        " - 不要输出“收到贴纸”“表情包分析”这种工具腔\n"
        " - 如果对方明显只是随手丢个表情、上下文空洞，宁可选 should_reply=false\n"
        " - 只适合回表情时，reply_text 可为空，只给 sticker_type"
    )
    if mode == "business":
        common += "\n - business 代聊里更要稳，宁愿不接也不要贴纸对贴纸互怼"
    if is_xp:
        common += (
            "\n - 对方是贝贝（小胖）：可以陪她一起玩表情包，但不要盲目复制她刚发的那个贴纸/GIF。"
            "要有变化、有来回、有情绪判断：有时选一个不同的 sticker_type，有时只用几个 emoji，有时扣一句短话。不装可爱不油腻。"
        )
    return common


def _incoming_unique_id_and_type(message: Message) -> tuple[str | None, str]:
    """从 incoming 消息中抽出 file_unique_id 与 素材类型。供 reuse_in_same_turn=false 使用。"""
    if getattr(message, "animation", None) is not None:
        return getattr(message.animation, "file_unique_id", None), "animation"
    if getattr(message, "sticker", None) is not None:
        return getattr(message.sticker, "file_unique_id", None), "sticker"
    return None, "sticker"


async def _handle_sticker_or_gif(message: Message, bot: Bot):
    if should_skip_message(message):
        return
    # business 里走业务自发检查（同时采集素材）
    if await _business_self_check(message, "sticker_or_gif"):
        return
    # private 里主人自发贴纸/GIF 也静默采集，不走后面的模型回复
    if await _owner_self_sticker_or_gif_check(message):
        return

    mode = get_chat_mode(message)
    content_type, human_desc = _describe_incoming_sticker_or_gif(message)
    description = "[GIF动态图]" if message.animation else "[贴纸表情]"
    is_xp = await is_xiaopang(message)
    scope = await xiaopang_scope(message) if is_xp else "default"

    # 入库仍然记 [贴纸表情]/[GIF动态图]，保持与现有历史一致
    await store_message(message, "incoming", description, content_type, scope=scope)

    # 斗图弹药库 · collect_now=true：对方发来的贴纸/GIF 同步入库，
    # 后续可以用于反手（但本轮绝不能拿同一个 file_unique_id 发回去）。
    # 素材采集对非联系人也保留（可调试/后续重用），但后续不代聊。
    await record_incoming_media(message, mode=mode if mode in ("business", "private") else "business")

    # business 非联系人静默：incoming 与素材都已入库，不调模型、不回复。
    if await _business_non_contact_check(message, "sticker_or_gif"):
        return

    if is_xp:
        await remember_xiaopang_identity(message)

    # 交给模型判断：不再机械 echo。给模型的是人话描述（emoji、贴纸集名、GIF时长等），
    # 不传 file_id，也不发原始照片。
    if mode == "business":
        # business 必须由 CORE_MODEL=gpt-5.5 出最终回复，不走 VISION_MODEL。
        # 把人话描述当作“视觉/介质摘要”传进去，连同最近聊天历史一起拼。
        media_kind_label = "GIF动图" if message.animation else "贴纸表情"
        result = await _final_reply_via_core_model(
            message,
            mode="business",
            media_kind=media_kind_label,
            visual_or_human_summary=human_desc,
            extra_user_caption="",
        )
        model = CORE_MODEL
    else:
        # private 维持原逻辑：走 choose_model（可能返 VISION_MODEL）+ 原 sticker prompt
        system_prompt = await build_system_prompt_with_xiaopang(system_prompt_for_mode(message), message)
        user_prompt = _build_sticker_user_prompt(mode, content_type, human_desc, is_xp)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        model = choose_model(message)
        result = await call_openai(messages, model, mode, chat_id=message.chat.id)

    if is_xp:
        result = await _sanitize_beibei_result(
            result, bot=bot, scene=f"media_{content_type}_{mode}", chat_id=message.chat.id,
        )

    # 静默决策：business 走原有逻辑；private 里如果模型明确说不回，也静默。
    reply_text = (result.get("reply_text") or "").strip()
    sticker_type = result.get("sticker_type")
    should_reply = result.get("should_reply", bool(reply_text or sticker_type))

    # 人话描述拼成“对方发过什么”的历史占位，不含 file_id
    user_log_for_history = (
        f"[GIF动图] {human_desc}" if message.animation else f"[贴纸表情] {human_desc}"
    )

    if mode == "business" and not _should_reply_business(result):
        # 静默也要入共享记忆，下轮文本才看得到“对方刚发了贴纸”
        user_id_for_hist = message.from_user.id if message.from_user else 0
        biz_save_history(user_id_for_hist, user_log_for_history, "")
        await store_message(message, "outgoing", f"[静默跳过:{content_type}]", "system", scope=scope)
        return
    if mode != "business" and (not should_reply or (not reply_text and not sticker_type)):
        await store_message(message, "outgoing", f"[静默跳过:{content_type}]", "system", scope=scope)
        return

    # 贴纸选材 · reuse_in_same_turn=false：
    #   如果模型选了 sticker_type，但 STICKER_MAP 里该 slot 为空，试着从弹药库里挑一张历史贴纸发；
    #   严格排除对方本轮刚发过来的 file_unique_id。STICKER_MAP 有配置的 slot 仍然走原路径。
    sticker_override: str | None = None
    if isinstance(sticker_type, str) and sticker_type and sticker_type.lower() not in {"null", "none"}:
        if not (STICKER_MAP.get(sticker_type) or "").strip():
            incoming_uid, incoming_type = _incoming_unique_id_and_type(message)
            # 优先从同类型里挑（sticker 对 sticker，animation 对 animation）；
            # 拿不到时也不报错，只留文字/emoji。
            asset = await pick_media_asset(
                media_type=incoming_type,
                exclude_file_unique_id=incoming_uid,
            )
            if asset and asset.get("file_id"):
                sticker_override = asset["file_id"]
                # 累加使用计数，避免后续反复选中同一张
                try:
                    await bump_media_use(asset.get("file_unique_id") or "", asset.get("media_type") or incoming_type)
                except Exception:
                    pass

    # business 延迟：在贴纸/GIF 或短文字发出前先 sleep，避免“贴纸秒回贴纸”那种术场。
    if mode == "business":
        sticker_only_local = (not reply_text) and bool(sticker_type)
        await human_typing_delay(
            bot,
            message.chat.id,
            reply_text,
            mode="business",
            has_sticker_only=sticker_only_local,
            business_connection_id=getattr(message, "business_connection_id", None),
        )
        if is_in_owner_cooldown(message) or is_in_self_silence(message):
            await store_message(
                message, "outgoing", f"[延迟后 cooldown 静默:{content_type}]", "system", scope=scope
            )
            return

    # 反 echo 保护：send_reply 只从 STICKER_MAP 或 sticker_override 拿 file_id，永远不会拿到 incoming file_id。
    await send_reply(
        bot,
        message.chat.id,
        result,
        model,
        business_connection_id=getattr(message, "business_connection_id", None),
        sticker_file_id_override=sticker_override,
    )
    text_out = reply_text
    if not text_out and sticker_type:
        text_out = f"[贴纸:{sticker_type}]"
    await store_message(message, "outgoing", text_out, "text", scope=scope)
    # business 入共享记忆：让下轮文本能看到“对方发了贴纸，我回了这个”
    if mode == "business":
        user_id_for_hist = message.from_user.id if message.from_user else 0
        biz_save_history(user_id_for_hist, user_log_for_history, text_out)


@router.message(F.sticker | F.animation)
async def sticker_or_gif_handler(message: Message, bot: Bot):
    await _handle_sticker_or_gif(message, bot)


@router.business_message(F.sticker | F.animation)
async def business_sticker_or_gif_handler(message: Message, bot: Bot):
    await _handle_sticker_or_gif(message, bot)
