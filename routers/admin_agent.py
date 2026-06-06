"""管理员对话网关（owner-only，私聊 only）。

让 owner 直接在私信里和两个助手自然对话：
- 主脑（OpenAI）：/主脑 <自然语言>，别名 /openai、/brain
- GitHub 助手：/github <自然语言>，别名 /gh、/git

两种用法：
1. 单条直达：/主脑 帮我想想灰度方案  →  返回主脑回复
2. 短会话模式（可选）：单独发 /主脑（或 /github）进入该助手会话，
   之后 owner 的普通消息都转给该助手，直到 /退出（别名 /exit、/quit、/q）。

安全边界（硬性）：
- 必须 ADMIN_AGENT_ENABLED=true 才启用；否则整套路由 noop（命令也不响应）。
- 仅 is_owner(message) 且 get_chat_mode == "private" 才触发。
- Business / 群 / 贝贝 / 媒体路由都不会进到这里：本 router 只挂 private 文本 + Command。
- 不执行任意 shell；GitHub 写/破坏性动作交由 github_helper_service 拒绝并要求 owner 确认。

注册顺序：必须在 private_router（含 F.text 兜底）之前 include，
会话兜底用函数过滤器，只在「owner+私聊+有活跃会话」时才命中，不会误吞其它消息。
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from config import ADMIN_AGENT_ENABLED, GITHUB_REPO
from services.admin_brain_service import ask_admin_brain, reset_history
from services.context_service import get_chat_mode, is_owner
from services.github_helper_service import handle_github_message
from services.reply_service import send_long_text
from utils.logger import setup_logging

logger = setup_logging()

router = Router(name="admin_agent")

# chat_id(str) -> "brain" | "github"：当前活跃的短会话目标。
_active_session: dict[str, str] = {}

_BRAIN_CMDS = {"/主脑", "/openai", "/brain"}
_GITHUB_CMDS = {"/github", "/gh", "/git"}
_EXIT_CMDS = {"/退出", "/exit", "/quit", "/q"}


def _owner_private(message: Message) -> bool:
    """硬门禁：总开关 + owner + 私聊。任一不满足直接 False。"""
    if not ADMIN_AGENT_ENABLED:
        return False
    if get_chat_mode(message) != "private":
        return False
    return is_owner(message)


def _arg_after_command(text: str) -> str:
    parts = (text or "").strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def _dispatch(bot: Bot, message: Message, target: str, payload: str) -> None:
    owner_key = message.from_user.id if message.from_user else message.chat.id
    if target == "github":
        reply = await handle_github_message(owner_key, payload)
    else:
        reply = await ask_admin_brain(owner_key, payload)
    await send_long_text(bot, message.chat.id, reply)


@router.message(Command(commands=["主脑", "openai", "brain"]))
async def cmd_brain(message: Message, bot: Bot):
    if not _owner_private(message):
        return
    payload = _arg_after_command(message.text or "")
    cid = str(message.chat.id)
    if not payload:
        _active_session[cid] = "brain"
        await send_long_text(
            bot, message.chat.id,
            "（主脑已就绪）已进入主脑会话，直接发消息即可对话；发 /退出 结束。",
        )
        return
    await _dispatch(bot, message, "brain", payload)


@router.message(Command(commands=["github", "gh", "git"]))
async def cmd_github(message: Message, bot: Bot):
    if not _owner_private(message):
        return
    payload = _arg_after_command(message.text or "")
    cid = str(message.chat.id)
    if not payload:
        _active_session[cid] = "github"
        await send_long_text(
            bot, message.chat.id,
            f"（GitHub 助手已就绪 · 仓库 {GITHUB_REPO}）已进入 GitHub 会话，直接发消息即可；发 /退出 结束。\n"
            "可问：仓库状态 / open PR / 最近 Actions / 安全告警。写操作需你本人确认。",
        )
        return
    await _dispatch(bot, message, "github", payload)


@router.message(Command(commands=["退出", "exit", "quit", "q"]))
async def cmd_exit(message: Message, bot: Bot):
    if not _owner_private(message):
        return
    cid = str(message.chat.id)
    prev = _active_session.pop(cid, None)
    if prev == "brain":
        reset_history(message.from_user.id if message.from_user else cid)
    if prev:
        await send_long_text(bot, message.chat.id, "已退出会话，恢复普通模式。")
    else:
        await send_long_text(bot, message.chat.id, "当前没有进行中的会话。")


def _has_active_session(message: Message) -> bool:
    """会话兜底过滤器：只在 owner+私聊+该 chat 有活跃会话时才命中。

    返回 False 时本 handler 不匹配，事件继续往后传给 private_router，不影响既有行为。
    """
    if not _owner_private(message):
        return False
    return str(message.chat.id) in _active_session


@router.message(F.text & ~F.text.startswith("/"), _has_active_session)
async def session_text(message: Message, bot: Bot):
    cid = str(message.chat.id)
    target = _active_session.get(cid)
    if not target:
        return
    await _dispatch(bot, message, target, message.text or "")
