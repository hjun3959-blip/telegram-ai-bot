"""Owner 私聊功能按钮菜单（owner-only 控制台 UI）。

变更：
- OWNER_MENU_ENABLED 默认 true，菜单始终可用
- 新增「小胖管理」区块按钮：摘要/聊天记录/设置/档案
- 修复 bazi 入口兼容性
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import GITHUB_REPO, OWNER_MENU_ENABLED
from services.context_service import get_chat_mode, is_owner
from services.gray_status_service import owner_health_command_reply
from services.pending_style_service import set_pending_style
from services.plan_service import get_today_focus, list_plans
from services.reply_service import send_long_text
from utils.logger import setup_logging

logger = setup_logging()

router = Router(name="owner_menu")

_MENU_TITLE = "🛠️ 私信控制台（仅你可见）\n挑一个功能点一下："

_MENU_HELP = (
    "🛠️ 私信控制台用法\n"
    "- 主脑：进入会话后直接发消息问 OpenAI 主脑；发 /退出 结束。\n"
    "- GitHub：进入会话后直接问 GitHub 助手。\n"
    "- 好玩一下：娱乐/出图/改图/视频按钮菜单。\n"
    "- 文案优化：点完把文案发来即可。\n"
    "- 计划 / 今日焦点 / 神算子：点一下直接出结果。\n"
    "- 八字命理：点一下进入八字推算。\n"
    "- 小胖管理：摘要/聊天记录/设置/档案（仅 owner 可用）。\n"
    "随时发 /菜单 或 /功能 重新打开控制台。"
)


def _owner_private_msg(message: Message) -> bool:
    if not OWNER_MENU_ENABLED:
        return False
    if get_chat_mode(message) != "private":
        return False
    return is_owner(message)


def _owner_private_cb(query: CallbackQuery) -> bool:
    if not OWNER_MENU_ENABLED:
        return False
    msg = query.message
    if not msg or get_chat_mode(msg) != "private":
        return False
    user = query.from_user
    if not user:
        return False
    return is_owner(_OwnerProbe(user))


class _OwnerProbe:
    def __init__(self, user):
        self.from_user = user
        self.chat = None


def _build_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🧠 主脑", callback_data="ownmenu:brain"),
                InlineKeyboardButton(text="🐙 GitHub", callback_data="ownmenu:github"),
            ],
            [
                InlineKeyboardButton(text="🎨 出图/改图/视频", callback_data="home:make_image"),
            ],
            [
                InlineKeyboardButton(text="🎲 好玩一下", callback_data="home:fun"),
                InlineKeyboardButton(text="🧰 文本工具", callback_data="home:tools"),
            ],
            [
                InlineKeyboardButton(text="📝 文案优化", callback_data="ownmenu:copyfix"),
                InlineKeyboardButton(text="🗂️ 计划", callback_data="ownmenu:plans"),
            ],
            [
                InlineKeyboardButton(text="🎯 今日焦点", callback_data="ownmenu:today"),
                InlineKeyboardButton(text="🔮 神算子", callback_data="ownmenu:health"),
            ],
            [
                InlineKeyboardButton(text="🎴 八字命理", callback_data="ownmenu:bazi"),
            ],
            # ===== 小胖管理区块 =====
            [
                InlineKeyboardButton(text="💬 小胖摘要", callback_data="ownmenu:xp_summary"),
                InlineKeyboardButton(text="📋 小胖记录", callback_data="ownmenu:xp_log"),
            ],
            [
                InlineKeyboardButton(text="⚙️ 小胖设置", callback_data="ownmenu:xp_settings"),
                InlineKeyboardButton(text="📁 小胖档案", callback_data="ownmenu:xp_profile"),
            ],
            [
                InlineKeyboardButton(text="❓ 用法", callback_data="ownmenu:help"),
            ],
        ]
    )


def _build_home_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ 回控制台", callback_data="ownmenu:home")]]
    )


async def _send_menu(message: Message, *, greeting: str | None = None) -> None:
    text = f"{greeting.strip()}\n\n{_MENU_TITLE}" if (greeting and greeting.strip()) else _MENU_TITLE
    await message.answer(text, reply_markup=_build_menu_keyboard())


@router.message(Command(commands=["菜单", "功能", "控制台", "menu", "panel"]), _owner_private_msg)
async def cmd_menu(message: Message):
    await _send_menu(message)


@router.message(CommandStart(), _owner_private_msg)
async def owner_start(message: Message):
    await _send_menu(message, greeting="你好阿君～这是你的私信控制台。")


@router.callback_query(F.data.startswith("ownmenu:"))
async def owner_menu_callback(query: CallbackQuery, bot: Bot, state: FSMContext):
    if not _owner_private_cb(query):
        try:
            await query.answer()
        except Exception:
            pass
        return

    data = query.data or ""
    key = data.split(":", 1)[1] if ":" in data else ""
    msg = query.message
    if not msg:
        try:
            await query.answer()
        except Exception:
            pass
        return
    chat_id = msg.chat.id

    try:
        await query.answer()
    except Exception:
        pass

    if key == "home":
        await msg.answer(_MENU_TITLE, reply_markup=_build_menu_keyboard())
        return

    if key == "help":
        await send_long_text(bot, chat_id, _MENU_HELP)
        return

    if key in {"brain", "github"}:
        try:
            from routers import admin_agent
        except Exception:
            admin_agent = None
        if admin_agent is None:
            await send_long_text(bot, chat_id, "（控制台）这个入口暂时不可用，稍后再试。")
            return
        admin_agent._active_session[str(chat_id)] = "brain" if key == "brain" else "github"
        if key == "brain":
            await send_long_text(bot, chat_id, "🧠 已进入主脑会话，直接发消息即可对话；发 /退出 结束。")
        else:
            await send_long_text(
                bot, chat_id,
                f"🐙 已进入 GitHub 会话（仓库 {GITHUB_REPO}），直接发消息即可；发 /退出 结束。",
            )
        return

    if key == "copyfix":
        if query.from_user:
            set_pending_style(query.from_user.id, "copyfix", "频道发布")
        await send_long_text(bot, chat_id, "📝 文案优化已就绪：把要优化的文案直接发我就行。")
        return

    if key == "plans":
        try:
            reply = await list_plans()
        except Exception as e:
            logger.warning("owner_menu list_plans failed | err=%s", e)
            reply = "计划列表暂时拉不出来，稍后再试。"
        await send_long_text(bot, chat_id, reply or "还没有计划。")
        return

    if key == "today":
        try:
            reply = await get_today_focus()
        except Exception as e:
            logger.warning("owner_menu get_today_focus failed | err=%s", e)
            reply = "今日焦点暂时拉不出来，稍后再试。"
        await send_long_text(bot, chat_id, reply or "今天还没设焦点。")
        return

    if key == "health":
        try:
            reply = await owner_health_command_reply("/健康检查")
        except Exception as e:
            logger.warning("owner_menu health failed | err=%s", e)
            reply = "🔮 神算子暂时生成失败，请看后台日志。"
        await send_long_text(bot, chat_id, reply or "🔮 神算子无返回。")
        return

    if key == "bazi":
        try:
            from routers.mingli import BaziStates, _gender_kb
            await state.clear()
            await state.set_state(BaziStates.ask_gender)
            await bot.send_message(
                chat_id,
                "🔮 *八字命理解读*\n\n请先告诉我你的性别：",
                parse_mode="Markdown",
                reply_markup=_gender_kb(),
            )
        except Exception as e:
            logger.warning("owner_menu bazi failed | err=%s", e)
            await send_long_text(bot, chat_id, "八字入口暂时不可用，请直接发 /八字。")
        return

    # ===== 小胖管理 =====
    if key in {"xp_summary", "xp_log", "xp_settings", "xp_profile"}:
        try:
            from services.xiaopang_service import (
                build_xiaopang_summary_text,
                get_latest_daily_summary,
                get_xiaopang_chat_archive,
                get_xiaopang_profile_text,
                xiaopang_owner_settings_text,
            )
            if key == "xp_summary":
                reply = await get_latest_daily_summary()
            elif key == "xp_log":
                reply = await get_xiaopang_chat_archive(limit=30)
            elif key == "xp_settings":
                reply = await xiaopang_owner_settings_text()
            else:  # xp_profile
                reply = await get_xiaopang_profile_text()
        except Exception as e:
            logger.warning("owner_menu xp_%s failed | err=%s", key, e)
            reply = f"小胖 {key} 暂时拉不出来，稍后再试。"
        await send_long_text(bot, chat_id, reply)
        return

    await msg.answer("（控制台）未知操作。", reply_markup=_build_home_button())
