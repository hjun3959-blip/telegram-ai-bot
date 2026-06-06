"""Owner 私聊功能按钮菜单（owner-only 控制台 UI）。

把 owner 平时要敲命令才能用的功能，做成 Telegram inline 按钮，集中在「私信控制台」里：
- /菜单、/功能 弹出主菜单；owner 私聊里发 /start 也会在欢迎语下方带一个「打开控制台」入口。
- 主菜单按钮覆盖：主脑(OpenAI) / GitHub 助手 / 好玩一下(娱乐&图像&文本工具) /
  文案优化 / 计划 / 今日焦点 / 健康检查 / 帮助。
- 图像/视频/娱乐/文本类功能不在这里重复实现，而是直接复用 private 路由里已有的
  「好玩一下」首页（home:* / play:* / stylepick:* 回调），避免两套键盘逻辑漂移。

点击行为（callback，前缀统一 ownmenu:）：
- ownmenu:brain   → 进入主脑会话（复用 admin_agent 的 _active_session），下一条普通消息转主脑。
- ownmenu:github  → 进入 GitHub 助手会话，下一条普通消息转 GitHub 助手。
- ownmenu:copyfix → 登记 copyfix pending，提示 owner 直接把文案/贴纸/GIF 发来即可优化。
- ownmenu:plans   → 直接列出计划（plan_service）。
- ownmenu:today   → 直接返回今日焦点。
- ownmenu:health  → 直接返回 owner 健康检查。
- ownmenu:help    → 控制台用法说明。
- ownmenu:home    → 回主菜单。
- 「好玩一下」相关入口用 home:make_image / home:fun / home:tools 回调，落到 private 路由处理。

安全边界（硬性）：
- 必须 OWNER_MENU_ENABLED=true（默认跟随 ADMIN_AGENT_ENABLED）才启用；否则整套菜单 noop。
- 仅 is_owner(message) 且 get_chat_mode == "private" 才触发，回调里也复核同一门禁。
- Business / 群 / 普通用户 / 贝贝(小胖) / 媒体路由都进不来：本 router 只挂 owner 私聊文本命令 + ownmenu: 回调。
- 不执行任意 shell；主脑/GitHub 写操作的硬门禁仍在各自 service 里。

注册顺序：必须在 private_router（含 F.text 兜底）之前 include，命令与会话兜底才能优先命中。
ownmenu: 之外的回调（home:* 等）不被本 router 匹配，会继续传给 private_router，不影响既有行为。
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
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
    "- 主脑：进入会话后，直接发消息就是问 OpenAI 主脑；发 /退出 结束。\n"
    "- GitHub：进入会话后，直接发消息就是问 GitHub 助手（只读优先，写操作要你本人确认）。\n"
    "- 好玩一下：打开娱乐/出图/改图/图生视频/文本工具的按钮菜单。\n"
    "- 文案优化：点完直接把文案（也可带贴纸/GIF）发来即可。\n"
    "- 计划 / 今日焦点 / 健康检查：点一下直接出结果。\n"
    "随时发 /菜单 或 /功能 重新打开控制台。"
)


def _owner_private_msg(message: Message) -> bool:
    """硬门禁：总开关 + 私聊 + owner。任一不满足直接 False。"""
    if not OWNER_MENU_ENABLED:
        return False
    if get_chat_mode(message) != "private":
        return False
    return is_owner(message)


def _owner_private_cb(query: CallbackQuery) -> bool:
    """回调门禁：复用消息门禁逻辑，外加 from_user 必须是 owner。

    CallbackQuery.message 是 bot 自己发的菜单消息，其 from_user 是 bot；
    真正点按钮的人在 query.from_user。所以这里基于 query.from_user 单独判定 owner，
    chat 类型仍按 message.chat 判（必须 private）。
    """
    if not OWNER_MENU_ENABLED:
        return False
    msg = query.message
    if not msg or get_chat_mode(msg) != "private":
        return False
    user = query.from_user
    if not user:
        return False
    # 复用 is_owner：构造一个最小 message-like，让它读 from_user。
    return is_owner(_OwnerProbe(user))


class _OwnerProbe:
    """给 is_owner 用的最小载体：只暴露 from_user / chat。"""

    def __init__(self, user):
        self.from_user = user
        self.chat = None


def _build_menu_keyboard() -> InlineKeyboardMarkup:
    """主菜单键盘。owner 专属功能用 ownmenu:* 回调；娱乐/出图复用 private 的 home:* 回调。"""
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
                InlineKeyboardButton(text="🩺 健康检查", callback_data="ownmenu:health"),
            ],
            [
                InlineKeyboardButton(text="❓ 用法", callback_data="ownmenu:help"),
            ],
        ]
    )


def _build_home_button() -> InlineKeyboardMarkup:
    """单按钮：回主菜单。"""
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
    """owner 私聊 /start：在欢迎语下方直接弹控制台。

    门禁作为 handler 过滤器（而非函数内 early-return）：非 owner / 非私聊 / 关闭时本 handler
    不匹配，/start 事件继续传给 private_router 的 start_handler，保持既有安全行为不被吞掉。
    """
    await _send_menu(message, greeting="你好阿君～这是你的私信控制台。")


@router.callback_query(F.data.startswith("ownmenu:"))
async def owner_menu_callback(query: CallbackQuery, bot: Bot):
    if not _owner_private_cb(query):
        # 非 owner 点到（理论上不会，菜单只发给 owner）：静默 ack，不泄露功能。
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
    owner_key = query.from_user.id if query.from_user else chat_id

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
        # 复用 admin_agent 的短会话机制：之后 owner 的普通文本由 admin_agent.session_text 接管。
        try:
            from routers import admin_agent
        except Exception:
            admin_agent = None
        if admin_agent is None:
            await send_long_text(bot, chat_id, "（控制台）这个入口暂时不可用，稍后再试。")
            return
        admin_agent._active_session[str(chat_id)] = "brain" if key == "brain" else "github"
        if key == "brain":
            await send_long_text(
                bot, chat_id,
                "🧠 已进入主脑会话，直接发消息即可对话；发 /退出 结束。",
            )
        else:
            await send_long_text(
                bot, chat_id,
                f"🐙 已进入 GitHub 会话（仓库 {GITHUB_REPO}），直接发消息即可；发 /退出 结束。\n"
                "可问：仓库状态 / open PR / 最近 Actions / 安全告警。写操作需你本人确认。",
            )
        return

    if key == "copyfix":
        # 登记 copyfix pending：owner 下一条文本会被 private 的 _maybe_consume_pending_for_text 当作待优化文案。
        if query.from_user:
            set_pending_style(query.from_user.id, "copyfix", "频道发布")
        await send_long_text(
            bot, chat_id,
            "📝 文案优化已就绪：把要优化的频道/广告文案直接发我就行（emoji、贴纸、GIF 也会读进去）。",
        )
        return

    if key == "plans":
        try:
            reply = await list_plans()
        except Exception as e:
            logger.warning("owner_menu list_plans failed | err=%s", e)
            reply = "计划列表暂时拉不出来，稍后再试。"
        await send_long_text(bot, chat_id, reply or "还没有计划。发 /新计划 标题 可以建一个。")
        return

    if key == "today":
        try:
            reply = await get_today_focus()
        except Exception as e:
            logger.warning("owner_menu get_today_focus failed | err=%s", e)
            reply = "今日焦点暂时拉不出来，稍后再试。"
        await send_long_text(bot, chat_id, reply or "今天还没设焦点。发 /设置今日焦点 内容 设一个。")
        return

    if key == "health":
        try:
            reply = await owner_health_command_reply("/健康检查")
        except Exception as e:
            logger.warning("owner_menu health failed | err=%s", e)
            reply = "🩺 健康检查暂时生成失败，请看后台日志。"
        await send_long_text(bot, chat_id, reply or "🩺 健康检查无返回。")
        return

    # 未知 ownmenu:* 键：给个回主菜单的兜底。
    await msg.answer("（控制台）未知操作。", reply_markup=_build_home_button())
