"""Business 路由（真实代聊窗口）。

处理优先级，从上到下：
1. 非 business 或群聊：直接跳过
2. owner 自己在 business 里发消息：标记自发静默窗口，不回；只入库 outgoing
3. 处于自发静默窗口期：进来的消息只入库，不调用模型
4. 广告关键词命中：静默并告警 owner，不调用模型
5. 命中小胖逻辑：注入小胖系统提示，模型走 CORE_MODEL（gpt-5.5）
6. 调用模型；命中 risk_note 时给 owner 告警
7. should_reply=False 或无内容时静默，不发消息

注意：业务窗口发送必须带 business_connection_id，否则 Telegram 会拒收。
"""

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.types import Message

from config import CORE_MODEL
from services.alert_service import dedup_alert
from services.atree_keyword_trigger import detect_intent as atree_detect_intent
from services.atree_owner_alert import (
    build_owner_notice as atree_build_owner_notice,
    should_send_alert as atree_should_send_alert,
)
from services.atree_persona import ATREE_SYSTEM_PROMPT, sanitize_visible_reply
from services.atree_quote_library import pick_safe_reply as atree_pick_safe_reply
from services.atree_undo import record_last_atree_reply
from services.business_memory_service import get_history, save_history
from services.chat_action_service import send_chat_action_safe
from services.context_service import (
    choose_model,
    get_chat_mode,
    is_in_owner_cooldown,
    is_in_self_silence,
    is_self_message,
    mark_self_silence,
    owner_cooldown_remaining,
    should_skip_message,
    system_prompt_for_mode,
)
from services.filter_service import ad_keyword_hit
from services.history_service import trim_messages
from services.message_service import store_message
from services.openai_service import call_openai
from services.reply_service import send_reply
from services.risk_alert_service import (
    check_and_alert as risk_check_and_alert,
)
from services.typing_delay_service import human_typing_delay
from services.xiaopang_service import (
    build_system_prompt_with_xiaopang,
    is_xiaopang,
    maybe_hit_xiaopang_reminders,
    xiaopang_scope,
)
from utils.logger import setup_logging

logger = setup_logging()

router = Router(name="business")

# 兼容旧测试：暴露共享 dict 同名引用
from services.business_memory_service import user_histories  # noqa: E402,F401


@router.business_message(F.text)
async def text_handler(message: Message, bot: Bot):
    # 1. mode 校验
    if should_skip_message(message) or get_chat_mode(message) != "business":
        return

    # 2. owner / 自发消息：标记静默，不回复自己
    if is_self_message(message):
        mark_self_silence(message)
        await store_message(message, "outgoing", message.text or "", "text", scope="self")
        logger.info("business self-message ignored | chat_id=%s", message.chat.id)
        return

    # 3. 自发静默窗口：对方此时进来的消息也忽略，避免对方接到自己刚发的话立刻被机器人接走
    if is_in_self_silence(message):
        await store_message(message, "incoming", message.text or "", "text_suppressed", scope="self_silence")
        logger.info("business in self-silence window | chat_id=%s", message.chat.id)
        return

    # 3b. “抢话修复”：owner 刚手动发过话的 chat，30s 内对方又发也先不接，
    # 让阿君继续主导。过期后恢复正常。
    if is_in_owner_cooldown(message):
        await store_message(
            message, "incoming", message.text or "", "text_suppressed", scope="owner_cooldown"
        )
        logger.info(
            "business in owner-cooldown, skip reply | chat_id=%s | remaining=%.1fs",
            message.chat.id, owner_cooldown_remaining(message),
        )
        return

    user_id = message.from_user.id
    text = message.text or ""

    is_xp = await is_xiaopang(message)
    scope = await xiaopang_scope(message) if is_xp else "default"
    await store_message(message, "incoming", text, "text", scope=scope)

    # 4. 广告过滤（明显广告/拉客/诈骗仍静默 + 告警 owner）
    ad_hit = ad_keyword_hit(text)
    if ad_hit:
        await store_message(message, "outgoing", f"[广告静默:{ad_hit}]", "system", scope=scope)
        await dedup_alert(
            bot,
            f"ad:{message.chat.id}:{ad_hit}:{text[:30]}",
            f"疑似广告已静默：{ad_hit}\n{text[:500]}",
        )
        return

    # 联系人白名单只作为辅助标注（贝贝/特殊名单），不再用作硬性拦截。
    # 是否回复交给模型判断（系统 prompt 中已要求陌生搭讪/无关消息 should_reply=false）。

    # 4b. 紧急 / 重要事件关键词（仅对**普通外部联系人**生效；贝贝走阿树独立路径）
    # 命中即给 owner 一条 status-only 提醒，dedup 防刷屏；不阻断后续 LLM 回复。
    if not is_xp:
        _URGENT_KEYWORDS = (
            "紧急", "急事", "重要", "马上", "现在就要", "立刻",
            "叫醒阿君", "叫醒他", "出事了", "救命", "急用",
        )
        _hit_urgent = next((w for w in _URGENT_KEYWORDS if w and w in text), None)
        if _hit_urgent:
            try:
                sender_label = (
                    (message.from_user.username if message.from_user else "")
                    or (str(message.from_user.id) if message.from_user else "?")
                )
                key = f"urgent:{message.chat.id}:{_hit_urgent}"
                # 摘要只截前 200 字，避免日志/告警里泄露过长正文
                notice = (
                    f"业务窗口紧急关键词命中：{_hit_urgent}（来源 {sender_label}）\n"
                    f"机器人已自动接住并提示对方继续说事；建议你尽快上来看。\n"
                    f"——状态通报。\n"
                    f"原文摘要：{text[:200]}"
                )
                await dedup_alert(bot, key, notice)
            except Exception as _ue:
                logger.warning("business urgent alert failed | err=%s", _ue)

    # 5. 小胖提醒命中（不阻断回复）
    if is_xp:
        await maybe_hit_xiaopang_reminders(message, text, bot)

    # 5a. 阿树关键词触发（贝贝 business 专享）：critical/high 走安全短句，跳过 LLM。
    # medium/low 不在这里发，留给下面 gpt-5.5 自然陪伴；只做阿君通报。
    if is_xp:
        try:
            atree_intent = atree_detect_intent(text)
        except Exception as _ae:
            logger.warning("business atree detect failed | err=%s", _ae)
            atree_intent = None
        if atree_intent is not None:
            try:
                sender_label = (message.from_user.username or str(message.from_user.id))
                if atree_should_send_alert(message.from_user.id, atree_intent):
                    notice = atree_build_owner_notice(atree_intent, original_text=text, sender_label=sender_label)
                    if notice:
                        key = f"atree::biz::{sender_label}::{atree_intent.intent}::{atree_intent.severity}"
                        await dedup_alert(bot, key, notice)
            except Exception as _ne:
                logger.warning("business atree owner alert failed | err=%s", _ne)
            if atree_intent.severity in ("critical", "high"):
                try:
                    safe = atree_pick_safe_reply(atree_intent.intent)
                    safe = sanitize_visible_reply(safe)
                except Exception as _se:
                    logger.warning("business atree pick safe failed | err=%s", _se)
                    safe = "嗯，我在。"
                # business 发送必须带 business_connection_id
                await send_chat_action_safe(
                    bot,
                    message.chat.id,
                    ChatAction.TYPING,
                    business_connection_id=message.business_connection_id,
                )
                await human_typing_delay(
                    bot,
                    message.chat.id,
                    safe,
                    mode="business",
                    has_sticker_only=False,
                    business_connection_id=message.business_connection_id,
                )
                if is_in_owner_cooldown(message) or is_in_self_silence(message):
                    await store_message(message, "outgoing", "[延迟后 cooldown 静默]", "system", scope=scope)
                    return
                await send_reply(
                    bot,
                    message.chat.id,
                    {"reply_text": safe, "sticker_type": None},
                    "atree",
                    business_connection_id=message.business_connection_id,
                )
                try:
                    record_last_atree_reply(message.chat.id, safe)
                except Exception:
                    pass
                save_history(user_id, text, safe)
                await store_message(message, "outgoing", safe, "text", scope=scope)
                return

    # 5b. P0 spec（关键词触发版）：贝贝 business 消息 → companion 模式路由 + 自然关键词触发器。
    # 关键词只**纠正模式**，不替换模型回复；gpt-5.5 仍是主回复者；ajun 通报仍是 status-only。
    classification = None
    bb_kw_intent = None
    if is_xp:
        try:
            from services.companion_mode_router import classify as _bb_classify
            classification = _bb_classify(
                message.from_user.id, text, tz="Asia/Hong_Kong", media_kind="text",
            )
        except Exception as _ce:
            logger.warning("business companion classify failed | err=%s", _ce)
        # 自然关键词触发器：识别到关键词时把建议 mode 喂给分类器
        try:
            from services.beibei_keyword_trigger import detect_intent as _bb_kw_detect
            bb_kw_intent = _bb_kw_detect(text)
            if bb_kw_intent is not None and classification is not None:
                # business 不发短句兜底（避免「客服腔」），只**调整模式**
                classification.mode = bb_kw_intent.mode
        except Exception as _ke:
            logger.warning("business beibei keyword detect failed | err=%s", _ke)
        # 关键词通报（status-only，dedup 5 分钟）—— 贝贝看不到
        if bb_kw_intent is not None and bb_kw_intent.needs_ajun_alert:
            try:
                import time as _time
                sender_label = (message.from_user.username or str(message.from_user.id))
                bucket = int(_time.time() // (5 * 60))
                key = f"bb_kw::{sender_label}::{bb_kw_intent.keyword}::{bucket}"
                alert_text = (
                    f"{bb_kw_intent.alert_label}（{sender_label} · business）\n"
                    f"依据：{bb_kw_intent.alert_reason}\n"
                    f"机器人已做：自然短回应，未弹菜单。\n"
                    f"——仅为状态通报，机器人仍在用 gpt-5.5 正常陪她。"
                )
                await dedup_alert(bot, key, alert_text)
            except Exception as _ae:
                logger.warning("business beibei keyword alert failed | err=%s", _ae)
        try:
            sender_label = (message.from_user.username or str(message.from_user.id))
            await risk_check_and_alert(
                bot,
                user_id=message.from_user.id,
                sender_label=sender_label,
                text=text,
                is_business=True,
            )
        except Exception as _e:
            logger.warning("business risk check failed | err=%s", _e)

    history = get_history(user_id)
    # 贝贝 business：用 atree_models 拨号到 companion 高配（env 未配 alias 时落到 CORE_MODEL）。
    # 非贝贝业务窗口保持原有 choose_model（普通用户不变）。
    if is_xp:
        try:
            from services.atree_models import pick_beibei_companion_model
            model = pick_beibei_companion_model(deep=False)
        except Exception:
            model = CORE_MODEL
    else:
        model = choose_model(message)
    system_prompt = await build_system_prompt_with_xiaopang(system_prompt_for_mode(message), message)
    if is_xp and classification is not None:
        try:
            from services.companion_engine import build_system_addendum as _bb_addendum
            system_prompt = system_prompt + "\n\n" + _bb_addendum(classification)
        except Exception as _ae:
            logger.warning("business companion addendum failed | err=%s", _ae)
    if is_xp:
        system_prompt = system_prompt + "\n\n" + ATREE_SYSTEM_PROMPT
    messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": text}]
    # 组装后再 trim 一次，控制 token
    messages = trim_messages(messages)

    # 发送 typing 状态；business 一定要透传 business_connection_id
    await send_chat_action_safe(
        bot,
        message.chat.id,
        ChatAction.TYPING,
        business_connection_id=message.business_connection_id,
    )

    result = await call_openai(messages, model, "business", chat_id=message.chat.id)

    # 5c. P0：贝贝 business 回复 → post-process（长度裁切、emoji 限制、追问熔断）
    if is_xp and classification is not None:
        try:
            from services.companion_engine import (
                post_process_reply as _bb_post,
                build_ajun_alert as _bb_build_alert,
            )
            raw_reply = (result.get("reply_text") or "")
            final_text = _bb_post(raw_reply, classification)
            try:
                final_text = sanitize_visible_reply(final_text)
            except Exception as _se:
                logger.warning("business atree sanitize failed | err=%s", _se)
            result["reply_text"] = final_text
            try:
                record_last_atree_reply(message.chat.id, final_text)
            except Exception:
                pass
            sender_label = (message.from_user.username or str(message.from_user.id))
            alert = _bb_build_alert(classification, sender_label)
            if alert and alert.should_alert:
                await dedup_alert(bot, alert.dedup_key, alert.text)
            from services.companion_mode_router import record_after_reply as _bb_record
            _bb_record(message.from_user.id, classification, final_text)
        except Exception as _be:
            logger.warning("business companion post-process failed | err=%s", _be)

    # 6. 风险提示
    risk_note = (result.get("risk_note") or "").strip()
    if risk_note:
        await dedup_alert(
            bot,
            f"risk:{message.chat.id}:{risk_note[:60]}",
            f"Business 风险提醒：\n{risk_note}\n\n原文：{text[:500]}",
        )

    # 7. 静默判断（由模型自行决定：陌生/广告/无关时 should_reply=false）
    reply_text = (result.get("reply_text") or "").strip()
    sticker_type = result.get("sticker_type")
    should_reply = result.get("should_reply", bool(reply_text or sticker_type))
    if not should_reply or (not reply_text and not sticker_type):
        await store_message(message, "outgoing", "[模型静默]", "system", scope=scope)
        logger.info(
            "business model chose silence | chat_id=%s | from_user_id=%s | username=%s",
            message.chat.id,
            message.from_user.id if message.from_user else "",
            (message.from_user.username if message.from_user else "") or "",
        )
        return

    # 8. 发送前拟真延迟（business）：期间持续刷 typing，避免“秒回”
    sticker_only = (not reply_text) and bool(sticker_type)
    await human_typing_delay(
        bot,
        message.chat.id,
        reply_text,
        mode="business",
        has_sticker_only=sticker_only,
        business_connection_id=message.business_connection_id,
    )

    # 9. 真正发送。发送前再侍机检查一下 cooldown：延迟期间 owner 可能手动插了一句话。
    if is_in_owner_cooldown(message) or is_in_self_silence(message):
        await store_message(message, "outgoing", "[延迟后 cooldown 静默]", "system", scope=scope)
        logger.info(
            "business reply aborted due to owner activity during delay | chat_id=%s",
            message.chat.id,
        )
        return
    await send_reply(
        bot,
        message.chat.id,
        result,
        model,
        business_connection_id=message.business_connection_id,
    )
    save_history(user_id, text, reply_text)
    await store_message(message, "outgoing", reply_text, "text", scope=scope)
