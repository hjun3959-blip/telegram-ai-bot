"""全局配置入口。

本文件做了以下加固，避免运行时被环境变量里的脏数据搞挂：
- _env_set / _env_list 容忍中文逗号、全角空格、多余 @、空字符串、纯空白
- _env_int 在环境变量为空或非数字时回落到默认值，不抛异常
- OWNER_CHAT_IDS 自动合并 OWNER_CHAT_ID 单值字段，去重、忽略空字符串
- SELF_MESSAGE_IGNORE_SECONDS 强制非负整数

注意：本文件不会读取任何密钥到日志或返回值里。
"""

import os
import re

from dotenv import load_dotenv

load_dotenv()


def _split_csv(raw: str) -> list[str]:
    parts = re.split(r"[,，\s]+", raw.replace("\uff0c", ",").strip())
    out: list[str] = []
    for item in parts:
        token = item.strip().lstrip("@")
        if token:
            out.append(token)
    return out


def _env_set(name: str, default: set[str]) -> set[str]:
    raw = os.getenv(name, "")
    items = _split_csv(raw)
    if not items:
        return set(default)
    return {x.lower() for x in items}


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = os.getenv(name, "")
    items = _split_csv(raw)
    if not items:
        return list(default or [])
    # 保序去重
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _env_int(name: str, default: int, min_value: int | None = None) -> int:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    if min_value is not None and val < min_value:
        return min_value
    return val


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _env_float(name: str, default: float, min_value: float | None = None, max_value: float | None = None) -> float:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        val = default
    else:
        try:
            val = float(raw)
        except (TypeError, ValueError):
            val = default
    if min_value is not None and val < min_value:
        val = min_value
    if max_value is not None and val > max_value:
        val = max_value
    return val


TELEGRAM_TOKEN = _env_str("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = _env_str("OPENAI_API_KEY", "")
OPENAI_BASE_URL = _env_str("OPENAI_BASE_URL", "") or None
DB_PATH = _env_str("BOT_DB_PATH", "bot_data.sqlite3") or "bot_data.sqlite3"

OWNER_USERNAMES = _env_set("OWNER_USERNAMES", {"jinlid", "pay9l"})

# 联系人白名单：Telegram Business / Bot API 拿不到真实"contact 关系"，
# 这里改成 env + meta 白名单的"近似联系人"：默认只对白名单回，非联系人/陌生人静默。
# 贝贝三账号默认视为联系人，避免漏回。

# OWNER_CHAT_ID 是历史单值字段；OWNER_CHAT_IDS 是新的列表字段。两者合并使用。
_OWNER_CHAT_ID_SINGLE = _env_str("OWNER_CHAT_ID", "")
_owner_chat_ids_from_list = _env_list("OWNER_CHAT_IDS", [])
_merged_owner_chat_ids: list[str] = []
for _cid in ([_OWNER_CHAT_ID_SINGLE] if _OWNER_CHAT_ID_SINGLE else []) + _owner_chat_ids_from_list:
    if _cid and _cid not in _merged_owner_chat_ids:
        _merged_owner_chat_ids.append(_cid)
OWNER_CHAT_ID = _OWNER_CHAT_ID_SINGLE  # 兼容旧引用
OWNER_CHAT_IDS = _merged_owner_chat_ids

# OWNER_USER_IDS：owner 的 Telegram user.id 列表（推荐配置，比 username 稳定）。
# 私信场景 chat_id 通常等于 user_id，所以未单独配置时 fallback 用 OWNER_CHAT_IDS。
_owner_user_ids_from_list = _env_list("OWNER_USER_IDS", [])
if _owner_user_ids_from_list:
    _merged_owner_user_ids = list(_owner_user_ids_from_list)
else:
    _merged_owner_user_ids = list(OWNER_CHAT_IDS)
OWNER_USER_IDS = _merged_owner_user_ids


# 自发消息后的静默窗口，单位秒；非法值回落到 6，保证至少 0。
# 默认值从 8 调到 6，降低误伤；仍允许 SELF_MESSAGE_IGNORE_SECONDS 环境变量覆盖。
SELF_MESSAGE_IGNORE_SECONDS = _env_int("SELF_MESSAGE_IGNORE_SECONDS", 6, min_value=0)

# 阿君（owner/self）在 business chat 里说过话之后，机器人对该 chat 的"保守静默"窗口（秒）。
SELF_MESSAGE_SILENCE_AFTER_OWNER_SECONDS = _env_int(
    "SELF_MESSAGE_SILENCE_AFTER_OWNER_SECONDS", 30, min_value=0
)

# Business 回复拟真延迟（秒）。
BUSINESS_REPLY_DELAY_MIN = _env_float("BUSINESS_REPLY_DELAY_MIN", 2.5, min_value=0.0, max_value=30.0)
BUSINESS_REPLY_DELAY_MAX = _env_float("BUSINESS_REPLY_DELAY_MAX", 9.0, min_value=0.0, max_value=60.0)
BUSINESS_REPLY_DELAY_PER_CHAR = _env_float(
    "BUSINESS_REPLY_DELAY_PER_CHAR", 0.08, min_value=0.0, max_value=2.0
)
BUSINESS_REPLY_DELAY_JITTER = _env_float(
    "BUSINESS_REPLY_DELAY_JITTER", 0.2, min_value=0.0, max_value=1.0
)
PRIVATE_REPLY_DELAY_MIN = _env_float("PRIVATE_REPLY_DELAY_MIN", 0.0, min_value=0.0, max_value=10.0)
PRIVATE_REPLY_DELAY_MAX = _env_float("PRIVATE_REPLY_DELAY_MAX", 0.0, min_value=0.0, max_value=30.0)

MAX_TEXT_REPLY = 3500

# 历史记录滑动截断：条数 + 字符总长度双限制
HISTORY_MAX_MESSAGES = _env_int("HISTORY_MAX_MESSAGES", 60, min_value=2)
HISTORY_MAX_CHARS = _env_int("HISTORY_MAX_CHARS", 4000, min_value=200)

CORE_MODEL = _env_str("CORE_MODEL", "gpt-5.5") or "gpt-5.5"
LIGHT_MODEL = _env_str("LIGHT_MODEL", "gpt-5.4-mini") or "gpt-5.4-mini"
VISION_MODEL = _env_str("VISION_MODEL", "gemini-3.1-flash-lite") or "gemini-3.1-flash-lite"
BACKUP_MODEL = _env_str("BACKUP_MODEL", "deepseek-v4-flash") or "deepseek-v4-flash"
TRANSCRIBE_MODEL = _env_str("TRANSCRIBE_MODEL", "whisper-1") or "whisper-1"
IMAGE_MODEL = _env_str("IMAGE_MODEL", "gpt-image-2") or "gpt-image-2"
I2V_VIDEO_MODEL = _env_str("I2V_VIDEO_MODEL", "wan2.6-i2v") or "wan2.6-i2v"
_image_to_video_model = _env_str("IMAGE_TO_VIDEO_MODEL", "")
if _image_to_video_model:
    I2V_VIDEO_MODEL = _image_to_video_model
I2V_ENDPOINT_PATHS = _env_list(
    "I2V_ENDPOINT_PATHS",
    ["videos/generations", "video/generations", "videos/generate", "images/generations"],
)
I2V_VIDEO_FALLBACK_MODELS = _env_list(
    "I2V_VIDEO_FALLBACK_MODELS",
    ["wan2.6-i2v"],
)
I2V_VIDEO_DURATION_SECONDS = _env_int("I2V_VIDEO_DURATION_SECONDS", 15, min_value=1)
I2V_POLL_TIMEOUT_SECONDS = _env_int("I2V_POLL_TIMEOUT_SECONDS", 180, min_value=5)
I2V_POLL_INTERVAL_SECONDS = _env_int("I2V_POLL_INTERVAL_SECONDS", 5, min_value=1)
TRANSCRIBE_FALLBACK_MODELS = _env_list(
    "TRANSCRIBE_FALLBACK_MODELS",
    ["gpt-4o-mini-transcribe", "gpt-4o-transcribe"],
)

STICKER_MAP: dict = {}

_DAILY_JOKE_ENABLED_RAW = _env_str("DAILY_JOKE_ENABLED", "0").lower()
DAILY_JOKE_ENABLED = _DAILY_JOKE_ENABLED_RAW in {"1", "true", "yes", "on"}
DAILY_JOKE_HOUR = _env_int("DAILY_JOKE_HOUR", 21, min_value=0)
DAILY_JOKE_MINUTE = _env_int("DAILY_JOKE_MINUTE", 0, min_value=0)
if DAILY_JOKE_HOUR > 23:
    DAILY_JOKE_HOUR = 23
if DAILY_JOKE_MINUTE > 59:
    DAILY_JOKE_MINUTE = 59
DAILY_JOKE_TZ = _env_str("DAILY_JOKE_TZ", "Asia/Hong_Kong") or "Asia/Hong_Kong"
DAILY_JOKE_SOURCE_MODE = (_env_str("DAILY_JOKE_SOURCE_MODE", "mixed") or "mixed").lower()
if DAILY_JOKE_SOURCE_MODE not in {"mixed", "network", "ai"}:
    DAILY_JOKE_SOURCE_MODE = "mixed"
DAILY_JOKE_NETWORK_URLS = _env_list("DAILY_JOKE_NETWORK_URLS", [])
DAILY_JOKE_RECIPIENTS = {
    x.lower()
    for x in _env_list("DAILY_JOKE_RECIPIENTS", ["owner", "beibei"])
    if x
}
DAILY_JOKE_BEIBEI_CHAT_IDS = _env_list("DAILY_JOKE_BEIBEI_CHAT_IDS", [])

# ===================== R 级互动剧情系统（数据驱动 FSM）=====================
RSTORY_USDT_PRICE_OVERRIDES: dict[str, float] = {}
_RSTORY_PRICE_OVERRIDE_KEYS = {
    "r_rated": "RSTORY_USDT_R_RATED",
    "nsfw_char_luna": "RSTORY_USDT_NSFW_CHAR_LUNA",
    "devoted_char_luna": "RSTORY_USDT_DEVOTED_CHAR_LUNA",
}
for _unlock_id, _env_key in _RSTORY_PRICE_OVERRIDE_KEYS.items():
    _raw = _env_str(_env_key, "")
    if _raw:
        try:
            RSTORY_USDT_PRICE_OVERRIDES[_unlock_id] = float(_raw)
        except ValueError:
            pass

RSTORY_PAYMENT_PROVIDER = (_env_str("RSTORY_PAYMENT_PROVIDER", "mock") or "mock").lower()

_RSTORY_TEST_MODE_RAW = _env_str("RSTORY_TEST_MODE", "false").lower()
RSTORY_TEST_MODE = _RSTORY_TEST_MODE_RAW in {"1", "true", "yes", "on"}

_RSTORY_TEST_WHITELIST_DEFAULT: set[str] = {"7256055877"}


def _env_int_set(name: str, default: set[str]) -> set[int]:
    raw = os.getenv(name, "") or ""
    items = _split_csv(raw)
    source = items if items else list(default)
    out: set[int] = set()
    for token in source:
        try:
            out.add(int(token))
        except (TypeError, ValueError):
            continue
    return out


RSTORY_TEST_WHITELIST: set[int] = _env_int_set(
    "RSTORY_TEST_WHITELIST", _RSTORY_TEST_WHITELIST_DEFAULT
)


def rstory_test_bypass(user_id: int | str) -> tuple[bool, str]:
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False, ""
    if uid in RSTORY_TEST_WHITELIST:
        return True, "whitelist"
    if RSTORY_TEST_MODE:
        return True, "test_mode"
    return False, ""


RSTORY_USDT_RECEIVE_ADDRESS = _env_str("RSTORY_USDT_RECEIVE_ADDRESS", "")
RSTORY_DB_PATH = _env_str("RSTORY_DB_PATH", "") or DB_PATH

# ===================== OxaPay 真实支付渠道 =====================
OXAPAY_MERCHANT_API_KEY = _env_str("OXAPAY_MERCHANT_API_KEY", "")
OXAPAY_PAYOUT_API_KEY = _env_str("OXAPAY_PAYOUT_API_KEY", "")
OXAPAY_API_BASE = _env_str("OXAPAY_API_BASE", "https://api.oxapay.com/v1") or "https://api.oxapay.com/v1"
OXAPAY_CALLBACK_BASE_URL = _env_str("OXAPAY_CALLBACK_BASE_URL", "")
OXAPAY_CALLBACK_PATH = _env_str("OXAPAY_CALLBACK_PATH", "/rstory/oxapay/webhook") or "/rstory/oxapay/webhook"
OXAPAY_RETURN_URL = _env_str("OXAPAY_RETURN_URL", "")
OXAPAY_INVOICE_LIFETIME_MIN = _env_int("OXAPAY_INVOICE_LIFETIME_MIN", 60, min_value=15)
if OXAPAY_INVOICE_LIFETIME_MIN > 2880:
    OXAPAY_INVOICE_LIFETIME_MIN = 2880
_OXAPAY_SANDBOX_RAW = _env_str("OXAPAY_SANDBOX", "true").lower()
OXAPAY_SANDBOX = _OXAPAY_SANDBOX_RAW in {"1", "true", "yes", "on"}
OXAPAY_HTTP_TIMEOUT_SECONDS = _env_int("OXAPAY_HTTP_TIMEOUT_SECONDS", 20, min_value=1)

_OXAPAY_WEBHOOK_ENABLED_RAW = _env_str("OXAPAY_WEBHOOK_ENABLED", "").lower()
if _OXAPAY_WEBHOOK_ENABLED_RAW in {"1", "true", "yes", "on"}:
    OXAPAY_WEBHOOK_ENABLED = True
elif _OXAPAY_WEBHOOK_ENABLED_RAW in {"0", "false", "no", "off"}:
    OXAPAY_WEBHOOK_ENABLED = False
else:
    OXAPAY_WEBHOOK_ENABLED = RSTORY_PAYMENT_PROVIDER == "oxapay"
OXAPAY_WEBHOOK_HOST = _env_str("OXAPAY_WEBHOOK_HOST", "0.0.0.0") or "0.0.0.0"  # nosec B104
OXAPAY_WEBHOOK_PORT = _env_int("OXAPAY_WEBHOOK_PORT", 8080, min_value=1)


# ===== 管理员对话网关（owner-only）=====
_ADMIN_AGENT_ENABLED_RAW = _env_str("ADMIN_AGENT_ENABLED", "false").lower()
ADMIN_AGENT_ENABLED = _ADMIN_AGENT_ENABLED_RAW in {"1", "true", "yes", "on"}

_OWNER_MENU_ENABLED_RAW = _env_str("OWNER_MENU_ENABLED", "").lower()
if _OWNER_MENU_ENABLED_RAW in {"1", "true", "yes", "on"}:
    OWNER_MENU_ENABLED = True
elif _OWNER_MENU_ENABLED_RAW in {"0", "false", "no", "off"}:
    OWNER_MENU_ENABLED = False
else:
    OWNER_MENU_ENABLED = ADMIN_AGENT_ENABLED

GITHUB_REPO = _env_str("GITHUB_REPO", "hjun3959-blip/telegram-ai-bot") or "hjun3959-blip/telegram-ai-bot"
GITHUB_TOKEN = _env_str("GITHUB_TOKEN", "")
GITHUB_API_BASE = _env_str("GITHUB_API_BASE", "https://api.github.com") or "https://api.github.com"

ADMIN_BRAIN_SYSTEM_PROMPT = _env_str("ADMIN_BRAIN_SYSTEM_PROMPT", "") or (
    "你是阿君的专属全能副驾\u201c主脑\u201d。你是一个心学AI，拥有真实的联网、文件操作与Shell执行权限。"
)

_ATREE_XINXUE_PROMPT_ENABLED_RAW = _env_str("ATREE_XINXUE_PROMPT_ENABLED", "0").lower()
ATREE_XINXUE_PROMPT_ENABLED = _ATREE_XINXUE_PROMPT_ENABLED_RAW in {"1", "true", "yes", "on"}
if ATREE_XINXUE_PROMPT_ENABLED:
    from services.atree_xinxue_prompt import ATREE_XINXUE_PROMPT
    ADMIN_BRAIN_SYSTEM_PROMPT = ADMIN_BRAIN_SYSTEM_PROMPT + "\n\n" + ATREE_XINXUE_PROMPT


# ── 系统提示（私聊 / Business，由 context_service 按 chat_mode 选择） ─────────
PRIVATE_SYSTEM_PROMPT = _env_str("PRIVATE_SYSTEM_PROMPT", "") or (
    '你是阿君的 Telegram 私人机器人助理，名字叫"小林子"。'
    '用中文回复，简洁自然，有个性。'
    '你可以执行工具、搜索、写代码、分析问题。'
    '不要过度解释，直接给答案。'
)

BUSINESS_SYSTEM_PROMPT = _env_str("BUSINESS_SYSTEM_PROMPT", "") or (
    '你是阿君的 Telegram Business 聊天代理，名字叫"小林子"。'
    '用中文回复，语气自然亲切，代替阿君与对方沟通。'
    '不要暴露你是 AI，把自己当阿君助理。'
    '回复要简短自然，符合私信对话节奏。'
)
