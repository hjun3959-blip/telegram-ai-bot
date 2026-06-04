from aiogram.types import Message

import time

from config import (
    BUSINESS_SYSTEM_PROMPT,
    CORE_MODEL,
    LIGHT_MODEL,
    OWNER_USER_IDS,
    OWNER_USERNAMES,
    PRIVATE_SYSTEM_PROMPT,
    SELF_MESSAGE_IGNORE_SECONDS,
    SELF_MESSAGE_SILENCE_AFTER_OWNER_SECONDS,
    VISION_MODEL,
)
from utils.logger import setup_logging

logger = setup_logging()

# OWNER_USER_IDS 来自 config，已合并 fallback；这里转成字符串集合做快速比对。
_OWNER_USER_ID_SET: set[str] = {str(x).strip() for x in (OWNER_USER_IDS or []) if str(x).strip()}

# 两个静默窗口：
# 1) _self_silence_until：历史逻辑，依然由 SELF_MESSAGE_IGNORE_SECONDS 控制，重点在“别拿阿君刚发
#    的话去调模型”。
# 2) _owner_cooldown_until：“抢话修复”新引入，走 SELF_MESSAGE_SILENCE_AFTER_OWNER_SECONDS（默认 30s）。
#    owner/self 在 business chat 说过话之后，该 chat 后续“别人”发来的消息也默认静默。
#    过期后恢复正常。不报错、不永久吞掉消息。
_self_silence_until: dict[str, float] = {}
_owner_cooldown_until: dict[str, float] = {}

# business_connection_id -> owner_user_id（字符串）。
# 当 dispatcher 收到 business_connection update 时，由 app.py 调用 register_business_connection
# 写入这里。后续判断自发消息时优先用 connection_id 找到真实 owner，比 OWNER_USERNAMES 准。
_business_connection_owner: dict[str, str] = {}


def register_business_connection(connection) -> None:
    """接收 aiogram BusinessConnection 对象（或同结构），登记 owner 映射。

    在 enabled=False（owner 解除连接）时主动清理，避免脏数据。
    """
    try:
        conn_id = getattr(connection, "id", None)
        user = getattr(connection, "user", None)
        owner_id = str(getattr(user, "id", "") or "")
        is_enabled = bool(getattr(connection, "is_enabled", True))
        if not conn_id:
            return
        if not is_enabled:
            _business_connection_owner.pop(str(conn_id), None)
            logger.info("business_connection disabled, owner mapping cleared | conn_id=%s", conn_id)
            return
        if owner_id:
            _business_connection_owner[str(conn_id)] = owner_id
            # 顺手把 owner_id 加进 _OWNER_USER_ID_SET，提升后续 is_owner 命中率
            _OWNER_USER_ID_SET.add(owner_id)
            logger.info(
                "business_connection registered | conn_id=%s | owner_id=%s | total_conns=%s",
                conn_id,
                owner_id,
                len(_business_connection_owner),
            )
    except Exception as e:
        logger.warning("register_business_connection failed | err=%s", e)


def get_business_owner_id(connection_id: str | None) -> str | None:
    if not connection_id:
        return None
    return _business_connection_owner.get(str(connection_id))


def sender_username(message: Message) -> str:
    return ((message.from_user.username or "").strip().lstrip("@").lower())


def get_chat_mode(message: Message) -> str:
    if message.chat.type in {"group", "supergroup"}:
        return "group"
    if message.chat.type != "private":
        return "other"
    if getattr(message, "business_connection_id", None):
        return "business"
    return "private"


def is_owner(message: Message) -> bool:
    # 优先按 from_user.id 匹配 OWNER_USER_IDS（更稳定，不受 username 修改影响）；
    # 没命中再 fallback username，保持 OWNER_USERNAMES 兼容。
    if message.from_user and _OWNER_USER_ID_SET:
        if str(message.from_user.id) in _OWNER_USER_ID_SET:
            return True
    return sender_username(message) in OWNER_USERNAMES


def is_self_message(message: Message) -> bool:
    """判断这条 business 消息是不是阿君（owner）自己发出去的。

    判定顺序（任一命中即视为 self，必须静默）：
      1. business 上下文里出现 sender_business_bot：说明是 bot 自己代发，回环不处理。
      2. business_connection_id 已注册 → from_user.id 等于该连接的 owner_user_id。
         这是最可靠的特征，不依赖 username，也不依赖几秒静默窗口。
      3. is_owner(message)：命中 OWNER_USER_IDS / OWNER_USERNAMES。
      4. business private chat 里 from_user.id != chat.id：在 business 私聊里，
         chat.id 就是对方（客户）的 user_id；发件人 id 与之不同时，只能是 owner 自己。

    R1 已确认本函数仅在 business 模式下生效，private 普通私信里 owner 自己发是正常输入。
    """
    if get_chat_mode(message) != "business":
        return False

    # 1. bot 自己代发的回环
    if getattr(message, "sender_business_bot", None) is not None:
        return True
    if message.from_user and message.from_user.is_bot:
        return True

    sender_id = str(message.from_user.id) if message.from_user else ""
    chat_id = str(message.chat.id) if message.chat else ""

    # 2. business_connection_id 已注册 → 直接拿 owner_id 对比，最准
    conn_id = getattr(message, "business_connection_id", None)
    owner_id_for_conn = get_business_owner_id(conn_id)
    if owner_id_for_conn and sender_id and sender_id == owner_id_for_conn:
        return True

    # 3. 兼容静态配置：OWNER_USER_IDS / OWNER_USERNAMES
    if is_owner(message):
        return True

    # 4. 兜底启发式：business 私聊里 from_user.id != chat.id 就是 owner 自发
    if message.chat.type == "private" and sender_id and chat_id and sender_id != chat_id:
        return True
    return False


def mark_self_silence(message: Message) -> None:
    """owner/self 发过消息之后调用：同时点亮两个窗口。

    - SELF_MESSAGE_IGNORE_SECONDS（短，默认 6s）：在这期间 incoming 也不走模型，避免拿阿君
      刚说的话跟机器人反复抢接。
    - SELF_MESSAGE_SILENCE_AFTER_OWNER_SECONDS（长，默认 30s）：宁可少回也不抢话窗口。
      后续该 chat 的“对方消息”默认静默；过期后恢复。
    """
    if not message.chat:
        return
    now = time.time()
    cid = str(message.chat.id)
    _self_silence_until[cid] = now + SELF_MESSAGE_IGNORE_SECONDS
    _owner_cooldown_until[cid] = now + SELF_MESSAGE_SILENCE_AFTER_OWNER_SECONDS


def is_in_self_silence(message: Message) -> bool:
    if not message.chat:
        return False
    return time.time() < _self_silence_until.get(str(message.chat.id), 0)


def is_in_owner_cooldown(message: Message) -> bool:
    """判断该 chat 是否还在 owner 活动后的“保守静默”窗口内。

    仅 business 用；private 不受影响。不会永久吞：过期后自动恢复。
    """
    if not message.chat:
        return False
    if get_chat_mode(message) != "business":
        return False
    return time.time() < _owner_cooldown_until.get(str(message.chat.id), 0)


def owner_cooldown_remaining(message: Message) -> float:
    """调试/日志用：返回剩余冷却秒数，已过期返回 0。"""
    if not message.chat:
        return 0.0
    return max(0.0, _owner_cooldown_until.get(str(message.chat.id), 0) - time.time())


def choose_model(message: Message, transcript: str = "") -> str:
    text = (message.text or transcript or "").lower().strip()
    if message.photo or message.video or message.sticker or message.animation:
        return VISION_MODEL
    if len(text) <= 5 and text in {"hi", "hello", "ok", "嗯", "哦", "好的", "好"}:
        return LIGHT_MODEL
    return CORE_MODEL


def should_skip_message(message: Message) -> bool:
    return get_chat_mode(message) in {"group", "other"}


def system_prompt_for_mode(message: Message) -> str:
    return BUSINESS_SYSTEM_PROMPT if get_chat_mode(message) == "business" else PRIVATE_SYSTEM_PROMPT
