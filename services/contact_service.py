"""联系人（contact）白名单服务。

背景：
    Telegram Bot API / Business update 里没有“你是不是我 Telegram 联系人”这种字段，
    所以这里实现一个“近似联系人白名单”：env + meta 表两层兜底。
    默认策略：宁可漏回陌生人，也不要误回非联系人。

判定优先级（任一命中即视为联系人）：
    1. owner 自己 → 不算联系人，由 is_self_message 走另外的静默；调用方应在更早处过滤掉。
    2. 贝贝三账号（XIAOPANG_CANONICAL_USERNAMES）→ 永远视为联系人。
    3. config.CONTACT_USER_IDS 命中 from_user.id
    4. config.CONTACT_USERNAMES 命中 from_user.username（小写、去 @）
    5. meta 表 `contact_user_ids` / `contact_usernames` 里手动添加的条目

API：
    is_contact(message) -> bool
    add_contact(token: str) -> tuple[bool, str]    # token 可以是 username（带不带 @）或纯数字 id
    remove_contact(token: str) -> tuple[bool, str]
    list_contacts() -> str                          # 给 owner 在私信里查看，含来源标签
"""

from __future__ import annotations

import re

from aiogram.types import Message

from config import CONTACT_USER_IDS, CONTACT_USERNAMES
from db.core import execute, fetchone
from services.context_service import sender_username
from utils.logger import setup_logging

logger = setup_logging()

# 贝贝（小胖）三账号默认视为联系人，硬编码一份，避免循环 import。
# 这里和 xiaopang_service.XIAOPANG_CANONICAL_USERNAMES 保持一致。
_BEIBEI_DEFAULT_CONTACT_USERNAMES = frozenset({"yj_syj", "i_q772", "zp7987"})

# meta key
_META_KEY_USERNAMES = "contact_usernames"
_META_KEY_USER_IDS = "contact_user_ids"

_NUMERIC_RE = re.compile(r"^-?\d+$")


def _norm_username(raw: str) -> str:
    return (raw or "").strip().lstrip("@").lower()


# ---------------- meta helpers (复制自 xiaopang_service 风格，避免循环引用) ----------------

async def _meta_get(key: str, default: str = "") -> str:
    row = await fetchone("SELECT value FROM meta WHERE key=?", (key,))
    return row["value"] if row else default


async def _meta_set(key: str, value: str) -> None:
    await execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


async def _meta_list_get(key: str) -> list[str]:
    raw = await _meta_get(key, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


async def _meta_list_save(key: str, items: list[str]) -> None:
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        k = x.strip()
        if not k:
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    await _meta_set(key, ",".join(out))


# ---------------- 判定 ----------------

async def get_meta_contact_usernames() -> set[str]:
    return {_norm_username(x) for x in await _meta_list_get(_META_KEY_USERNAMES) if x}


async def get_meta_contact_user_ids() -> set[str]:
    return {x.strip() for x in await _meta_list_get(_META_KEY_USER_IDS) if x.strip()}


async def is_contact(message: Message) -> bool:
    """判断这条 incoming business 消息的发件人是不是"联系人"。

    只看 from_user：不查 chat_id，避免群聊误判。groups 由上游 should_skip_message 过滤掉。
    """
    if not message or not message.from_user:
        return False
    username = sender_username(message)
    sender_id = str(message.from_user.id) if message.from_user.id else ""

    # 1. 贝贝三账号默认联系人
    if username and username in _BEIBEI_DEFAULT_CONTACT_USERNAMES:
        return True

    # 2. env CONTACT_USER_IDS
    if sender_id and sender_id in set(CONTACT_USER_IDS):
        return True

    # 3. env CONTACT_USERNAMES
    if username and username in CONTACT_USERNAMES:
        return True

    # 4. meta 白名单
    if sender_id and sender_id in await get_meta_contact_user_ids():
        return True
    if username and username in await get_meta_contact_usernames():
        return True

    return False


# ---------------- owner 维护命令 ----------------

async def add_contact(token: str) -> tuple[bool, str]:
    """owner 隐藏命令使用：/添加联系人 token。

    token 可以是：
      - 纯数字（Telegram user.id）→ 进 contact_user_ids
      - 字符串（可带 @）→ 进 contact_usernames（lowercase）
    """
    raw = (token or "").strip()
    if not raw:
        return False, "用法：/添加联系人 username 或 user_id"
    if _NUMERIC_RE.match(raw):
        ids = await _meta_list_get(_META_KEY_USER_IDS)
        if raw in ids:
            return False, f"user_id {raw} 已在联系人白名单里。"
        ids.append(raw)
        await _meta_list_save(_META_KEY_USER_IDS, ids)
        logger.info("contact whitelist add | user_id=%s", raw)
        return True, f"已添加 user_id {raw} 到联系人白名单。"
    # username
    name = _norm_username(raw)
    if not name:
        return False, "用法：/添加联系人 username 或 user_id"
    names = await _meta_list_get(_META_KEY_USERNAMES)
    lower_names = {_norm_username(x) for x in names}
    if name in lower_names:
        return False, f"@{name} 已在联系人白名单里。"
    names.append(name)
    await _meta_list_save(_META_KEY_USERNAMES, names)
    logger.info("contact whitelist add | username=%s", name)
    return True, f"已添加 @{name} 到联系人白名单。"


async def remove_contact(token: str) -> tuple[bool, str]:
    raw = (token or "").strip()
    if not raw:
        return False, "用法：/删除联系人 username 或 user_id"
    if _NUMERIC_RE.match(raw):
        ids = await _meta_list_get(_META_KEY_USER_IDS)
        if raw not in ids:
            return False, f"user_id {raw} 不在 meta 白名单里。"
        ids.remove(raw)
        await _meta_list_save(_META_KEY_USER_IDS, ids)
        logger.info("contact whitelist remove | user_id=%s", raw)
        return True, f"已从联系人白名单移除 user_id {raw}。"
    name = _norm_username(raw)
    if not name:
        return False, "用法：/删除联系人 username 或 user_id"
    names = await _meta_list_get(_META_KEY_USERNAMES)
    new_names = [x for x in names if _norm_username(x) != name]
    if len(new_names) == len(names):
        return False, f"@{name} 不在 meta 白名单里。"
    await _meta_list_save(_META_KEY_USERNAMES, new_names)
    logger.info("contact whitelist remove | username=%s", name)
    return True, f"已从联系人白名单移除 @{name}。"


async def list_contacts_text() -> str:
    """给 owner 在私信里看：列出 env 与 meta 两层白名单。

    贝贝三账号默认在 env 之外硬编码兜底，这里也展示一行，避免 owner 困惑"怎么没看见贝贝？"。
    """
    meta_names = sorted(await _meta_list_get(_META_KEY_USERNAMES))
    meta_ids = sorted(await _meta_list_get(_META_KEY_USER_IDS))
    env_names = sorted(CONTACT_USERNAMES)
    env_ids = sorted(CONTACT_USER_IDS)
    default_names = sorted(_BEIBEI_DEFAULT_CONTACT_USERNAMES)
    lines = ["联系人白名单"]
    lines.append(f"默认（贝贝三账号）：{', '.join('@' + x for x in default_names)}")
    lines.append(f"env CONTACT_USERNAMES：{', '.join('@' + x for x in env_names) if env_names else '无'}")
    lines.append(f"env CONTACT_USER_IDS：{', '.join(env_ids) if env_ids else '无'}")
    lines.append(f"meta usernames：{', '.join('@' + x for x in meta_names) if meta_names else '无'}")
    lines.append(f"meta user_ids：{', '.join(meta_ids) if meta_ids else '无'}")
    lines.append("")
    lines.append("管理：/添加联系人 username_or_id、/删除联系人 username_or_id")
    return "\n".join(lines)


# owner 私信里的隐藏命令集合，private 路由用来分发。/play /help 不展示。
CONTACT_OWNER_COMMANDS = {"/联系人列表", "/添加联系人", "/删除联系人"}


async def owner_contact_command_reply(text: str) -> str | None:
    """处理 owner 的联系人维护命令。返回 None 表示不是这套命令。"""
    raw = (text or "").strip()
    if not raw:
        return None
    head, _, rest = raw.partition(" ")
    head = head.strip()
    arg = rest.strip()
    if head == "/联系人列表":
        return await list_contacts_text()
    if head == "/添加联系人":
        ok, msg = await add_contact(arg)
        return msg
    if head == "/删除联系人":
        ok, msg = await remove_contact(arg)
        return msg
    return None
