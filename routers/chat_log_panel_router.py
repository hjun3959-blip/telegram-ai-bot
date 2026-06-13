"""全局聊天记录采集面板路由（仅限 owner，唯一性保护）。

入口命令：/采集记录  /chatlog  /全部记录
callback_data 前缀：chatlog:

Owner 唯一性保护：
  - is_owner() 检查：只有 OWNER_USERNAMES / OWNER_CHAT_IDS 用户能触发，其余静默。
  - 私信 only：Business 窗口、群组不触发。
  - 功能完全独立，不污染 private 路由逻辑。
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from services.context_service import get_chat_mode, is_owner
from services.chat_log_panel_service import (
    USER_BTN_PAGE_SIZE,
    build_panel_keyboard,
    build_user_log_keyboard,
    count_known_scopes,
    count_user_log,
    format_panel_header,
    format_user_log_text,
    get_user_log,
    list_known_scopes,
)
from utils.logger import setup_logging

logger = setup_logging()
router = Router(name="chat_log_panel")

# ─────────────────────────────────────────────────────────────
# 工具：把服务层返回的二维 list[dict] 组装成 InlineKeyboardMarkup
# ─────────────────────────────────────────────────────────────

def _build_markup(rows: list[list[dict]]) -> InlineKeyboardMarkup:
    kb: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=btn["text"], callback_data=btn["callback_data"]) for btn in row]
        for row in rows
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


# ─────────────────────────────────────────────────────────────
# 命令入口：/采集记录  /chatlog  /全部记录
# ─────────────────────────────────────────────────────────────

@router.message(
    Command(commands=["采集记录", "chatlog", "全部记录"]),
)
async def cmd_chatlog(message: Message) -> None:
    # 唯一性门禁：非 owner 或非私信，静默退出
    if get_chat_mode(message) != "private" or not is_owner(message):
        return

    await _send_panel(message.bot, message.chat.id, page=0)


# ─────────────────────────────────────────────────────────────
# Callback 分发：chatlog:panel:<page> | chatlog:user:<scope>:<chat_id>:<offset> | chatlog:close
# ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("chatlog:"))
async def cb_chatlog(cq: CallbackQuery) -> None:
    # 唯一性门禁
    if not is_owner(cq.message) and not _cq_is_owner(cq):
        await cq.answer("仅限 owner。", show_alert=True)
        return

    data = cq.data or ""
    parts = data.split(":")
    # parts[0] = "chatlog"
    action = parts[1] if len(parts) > 1 else ""

    if action == "close":
        try:
            await cq.message.delete()
        except Exception:
            await cq.answer("已关闭。")
        return

    if action == "panel":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await _edit_panel(cq, page)
        return

    if action == "user":
        # chatlog:user:<scope>:<chat_id>:<offset>
        if len(parts) < 6:
            await cq.answer("参数错误。", show_alert=True)
            return
        scope = parts[2]
        chat_id = parts[3]
        offset = int(parts[4]) if parts[4].isdigit() else 0
        await _show_user_log(cq, scope, chat_id, offset)
        return

    await cq.answer()


# ─────────────────────────────────────────────────────────────
# 内部：渲染面板（首次发送 or 编辑）
# ─────────────────────────────────────────────────────────────

async def _send_panel(bot: Bot, chat_id: int, page: int = 0) -> None:
    total_users = await count_known_scopes()
    offset = page * USER_BTN_PAGE_SIZE
    scopes = await list_known_scopes(offset=offset)
    total_pages = max(1, (total_users + USER_BTN_PAGE_SIZE - 1) // USER_BTN_PAGE_SIZE)

    header = format_panel_header(total_users, page, total_pages)
    kb_rows = build_panel_keyboard(scopes, page, total_users)
    markup = _build_markup(kb_rows)

    await bot.send_message(chat_id, header, reply_markup=markup)


async def _edit_panel(cq: CallbackQuery, page: int) -> None:
    total_users = await count_known_scopes()
    offset = page * USER_BTN_PAGE_SIZE
    scopes = await list_known_scopes(offset=offset)
    total_pages = max(1, (total_users + USER_BTN_PAGE_SIZE - 1) // USER_BTN_PAGE_SIZE)

    header = format_panel_header(total_users, page, total_pages)
    kb_rows = build_panel_keyboard(scopes, page, total_users)
    markup = _build_markup(kb_rows)

    try:
        await cq.message.edit_text(header, reply_markup=markup)
    except Exception:
        pass
    await cq.answer()


async def _show_user_log(
    cq: CallbackQuery,
    scope: str,
    chat_id: str,
    offset: int,
) -> None:
    total = await count_user_log(scope, chat_id)
    rows = await get_user_log(scope, chat_id, offset=offset)
    text = format_user_log_text(scope, rows, offset, total)

    # 翻页键盘里的 panel_page 默认 0（从用户详情返回列表首页）
    kb_rows = build_user_log_keyboard(scope, chat_id, offset, total, panel_page=0)
    markup = _build_markup(kb_rows)

    try:
        await cq.message.edit_text(text, reply_markup=markup)
    except Exception:
        await cq.message.answer(text, reply_markup=markup)
    await cq.answer()


# ─────────────────────────────────────────────────────────────
# 工具：从 CallbackQuery 判定是否 owner
# ─────────────────────────────────────────────────────────────

def _cq_is_owner(cq: CallbackQuery) -> bool:
    """从 CallbackQuery 的 from_user 判断是否 owner，防止伪造 message 对象。"""
    from config import OWNER_USERNAMES, OWNER_USER_IDS

    user = cq.from_user
    if not user:
        return False
    username = (user.username or "").lower().lstrip("@")
    if username in {u.lower() for u in OWNER_USERNAMES}:
        return True
    if str(user.id) in OWNER_USER_IDS:
        return True
    return False
