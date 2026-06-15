"""
routers/admin_agent.py  (已加 /think 入口)
"""
from __future__ import annotations
import asyncio
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message
from config import ADMIN_AGENT_ENABLED, OPENAI_API_KEY, OPENAI_BASE_URL
from api.mode_router import route_message, switch_mode, MODEL_MODES, EXIT_CMD
from services.context_service import get_chat_mode, is_owner
from services.reply_service import send_long_text
from utils.logger import setup_logging


def _is_pipeline_model(model: str) -> bool:
    """轻量/快速模型不走 pipeline 拦截器。"""
    return not any(x in str(model).lower() for x in ("lite", "brain", "mini", "flash"))



logger = setup_logging()
router = Router(name="admin_agent")


def _owner_private(message: Message) -> bool:
    if not ADMIN_AGENT_ENABLED:
        return False
    if get_chat_mode(message) != "private":
        return False
    return is_owner(message)


def _make_llm_factory(bot: Bot):
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)

    def factory(model: str, temperature: float, max_tokens: int, json_mode: bool = False):
        async def call(messages: list[dict], system: str) -> str:
            full = [{"role": "system", "content": system}] + messages
            kwargs: dict = dict(model=model, messages=full, temperature=temperature, max_tokens=max_tokens)
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            resp = await client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content or ""
        from utils.llm_interceptor import make_intercepted_llm_call
        _pipeline = _is_pipeline_model(model)
        return make_intercepted_llm_call(call, pipeline_mode=_pipeline, clean_context=True)

    return factory


def _make_emit_progress(bot: Bot, chat_id: int):
    async def emit(kind: str, text: str):
        if text and text.strip():
            await send_long_text(bot, chat_id, text)
    return emit


_MODE_CMDS = ["brain", "task", "deep", "code", "github"]
_EXIT_CMDS = ["exit", "退出", "quit", "q"]
_THINK_CMDS = ["think"]
_ALL_CMDS = _MODE_CMDS + _EXIT_CMDS + _THINK_CMDS


@router.message(Command(commands=_MODE_CMDS))
async def cmd_switch_mode(message: Message, bot: Bot):
    if not _owner_private(message):
        return
    cmd = (message.text or "").split()[0].lstrip("/").lower()
    user_id = str(message.from_user.id if message.from_user else message.chat.id)
    reply = await switch_mode(user_id, cmd)
    await send_long_text(bot, message.chat.id, reply)


@router.message(Command(commands=_EXIT_CMDS))
async def cmd_exit(message: Message, bot: Bot):
    if not _owner_private(message):
        return
    user_id = str(message.from_user.id if message.from_user else message.chat.id)
    reply = await switch_mode(user_id, "exit")
    await send_long_text(bot, message.chat.id, reply)


# ── /think 三层 Agent 入口 ─────────────────────────────────────────────────────
@router.message(Command(commands=_THINK_CMDS))
async def cmd_think(message: Message, bot: Bot):
    if not _owner_private(message):
        return

    # 提取 /think 后面的目标文本
    text = (message.text or "").strip()
    parts = text.split(None, 1)
    goal = parts[1].strip() if len(parts) > 1 else ""

    if not goal:
        await send_long_text(bot, message.chat.id,
            "🧠 **三层 Agent 模式**\n\n用法：`/think <目标描述>`\n\n"
            "例：`/think 扫描 192.168.1.1 的开放端口并检测漏洞`\n"
            "例：`/think 分析 /opt/myapp 的代码安全性`\n\n"
            "架构：大脑层(拆解) → Shannon规划层(方案) → 执行层(无审查执行)")
        return

    await send_long_text(bot, message.chat.id, f"🚀 **启动三层 Agent**\n\n目标：{goal}\n\n⏳ 大脑层分析中…")

    emit = _make_emit_progress(bot, message.chat.id)

    try:
        from core.think_pipeline import run_think_pipeline
        report = await asyncio.wait_for(
            run_think_pipeline(user_goal=goal, emit_progress=emit),
            timeout=600,
        )
        await send_long_text(bot, message.chat.id, f"📋 **最终报告**\n\n{report}")
    except asyncio.TimeoutError:
        await send_long_text(bot, message.chat.id, "⏱ /think 执行超时（>10分钟）")
    except Exception as e:
        logger.exception("think pipeline failed | err=%s", e)
        await send_long_text(bot, message.chat.id, f"❌ /think 出错：{e}")


# ── 消息兜底（owner 私聊所有文字都走 route_message）──────────────────────────
@router.message(F.text)
async def handle_text(message: Message, bot: Bot):
    if not _owner_private(message):
        return

    user_id  = str(message.from_user.id if message.from_user else message.chat.id)
    username = (message.from_user.username or "") if message.from_user else ""
    text     = message.text or ""

    from api.mode_router import _get_state
    state = _get_state(user_id)
    if state.active_mode == "task":
        await bot.send_chat_action(message.chat.id, "typing")
        await send_long_text(bot, message.chat.id, "⚙️ 收到，开始执行任务…")

    llm_factory = _make_llm_factory(bot)
    emit        = _make_emit_progress(bot, message.chat.id)

    try:
        result = await asyncio.wait_for(
            route_message(
                user_id=user_id,
                owner_key=user_id,
                user_message=text,
                llm_call_factory=llm_factory,
                emit_progress=emit,
                username=username,
            ),
            timeout=600,
        )
    except asyncio.TimeoutError:
        try:
            from api.ask_admin_brain_task import _get_fsm, _wb_key
            _fsm = _get_fsm()
            _ctx = await _fsm.load_or_create(str(user_id))
            await _fsm.mark_interrupted(_ctx, reason="Telegram 层 600s 超时")
        except Exception:
            pass
        await send_long_text(bot, message.chat.id,
            "⏱ 任务执行时间较长已暂停（>10分钟）。\n\n"
            "📌 **进度已保存，发送任意消息即可自动恢复继续。**")
        return
    except Exception as e:
        logger.exception("route_message failed | err=%s", e)
        await send_long_text(bot, message.chat.id, f"任务模式出错：{e}")
        return

    if result.reply:
        await send_long_text(bot, message.chat.id, result.reply)
    else:
        logger.warning("route_message returned empty reply | user=%s | mode=%s", user_id, state.active_mode)