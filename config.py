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
    """统一支持英文/中文逗号、分号、换行作为分隔符，并去除空白与前导 @。"""
    if not raw:
        return []
    parts = re.split(r"[,，;；\n\r\t]+", raw)
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
    """读取浮点环境变量；非法/空回落 default；可选 clamp。"""
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

# OWNER_USERNAMES：主人 + 授权用户（tyjt7777777 已授权，权限与 owner 相同）
OWNER_USERNAMES = _env_set("OWNER_USERNAMES", {"jinlid", "pay9l", "tyjt7777777"})

# 非白名单私聊默认关闭：PRIVATE_WHITELIST_ONLY=true 时只有 OWNER_USERNAMES 能私聊
_PRIVATE_WHITELIST_ONLY_RAW = _env_str("PRIVATE_WHITELIST_ONLY", "true").lower()
PRIVATE_WHITELIST_ONLY = _PRIVATE_WHITELIST_ONLY_RAW in {"1", "true", "yes", "on"}

CONTACT_USERNAMES = _env_set(
    "CONTACT_USERNAMES",
    {"yj_syj", "i_q772", "zp7987"},
)
CONTACT_USER_IDS = _env_list("CONTACT_USER_IDS", [])

_OWNER_CHAT_ID_SINGLE = _env_str("OWNER_CHAT_ID", "")
_owner_chat_ids_from_list = _env_list("OWNER_CHAT_IDS", [])
_merged_owner_chat_ids: list[str] = []
for _cid in ([_OWNER_CHAT_ID_SINGLE] if _OWNER_CHAT_ID_SINGLE else []) + _owner_chat_ids_from_list:
    if _cid and _cid not in _merged_owner_chat_ids:
        _merged_owner_chat_ids.append(_cid)
OWNER_CHAT_ID = _OWNER_CHAT_ID_SINGLE
OWNER_CHAT_IDS = _merged_owner_chat_ids

_owner_user_ids_from_list = _env_list("OWNER_USER_IDS", [])
if _owner_user_ids_from_list:
    _merged_owner_user_ids = list(_owner_user_ids_from_list)
else:
    _merged_owner_user_ids = list(OWNER_CHAT_IDS)
OWNER_USER_IDS = _merged_owner_user_ids

AD_KEYWORDS = _env_list(
    "AD_KEYWORDS",
    ["加微信", "加v", "代理", "招商", "返利", "兼职", "刷单", "送彩金", "点击链接", "下载app"],
)

SELF_MESSAGE_IGNORE_SECONDS = _env_int("SELF_MESSAGE_IGNORE_SECONDS", 6, min_value=0)

SELF_MESSAGE_SILENCE_AFTER_OWNER_SECONDS = _env_int(
    "SELF_MESSAGE_SILENCE_AFTER_OWNER_SECONDS", 30, min_value=0
)

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
MAX_VIDEO_SIZE = 20 * 1024 * 1024

HISTORY_MAX_MESSAGES = _env_int("HISTORY_MAX_MESSAGES", 60, min_value=2)
HISTORY_MAX_CHARS = _env_int("HISTORY_MAX_CHARS", 4000, min_value=200)

CORE_MODEL = _env_str("CORE_MODEL", "gpt-5.5") or "gpt-5.5"
LIGHT_MODEL = _env_str("LIGHT_MODEL", "gpt-5.4-mini") or "gpt-5.4-mini"
VISION_MODEL = _env_str("VISION_MODEL", "gemini-3.1-flash-lite") or "gemini-3.1-flash-lite"
BACKUP_MODEL = _env_str("BACKUP_MODEL", "deepseek-v4-flash") or "deepseek-v4-flash"
TRANSCRIBE_MODEL = _env_str("TRANSCRIBE_MODEL", "whisper-1") or "whisper-1"
IMAGE_MODEL = _env_str("IMAGE_MODEL", "gpt-image-2") or "gpt-image-2"
TEXT_IMAGE_MODEL = _env_str("TEXT_IMAGE_MODEL", "flux-1.1-pro") or "flux-1.1-pro"
IMAGE_TEXT_MODEL = _env_str("IMAGE_TEXT_MODEL", "flux.1-kontext-pro") or "flux.1-kontext-pro"
TEXT_IMAGE_FALLBACK_MODELS = _env_list(
    "TEXT_IMAGE_FALLBACK_MODELS",
    ["qwen-image-2.0-pro", "qwen-image-2.0", "doubao-seedream-4-0-250828"],
)
IMAGE_EDIT_FALLBACK_MODELS = _env_list(
    "IMAGE_EDIT_FALLBACK_MODELS",
    ["qwen-image-edit-plus", "qwen-image-2.0-pro", "doubao-seedream-4-0-250828"],
)
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

# ===== admin_brain 专用推理参数 =====
# 低温保证代码/部署任务输出稳定；2000 token 支持长回复。
# 可通过环境变量覆盖。
ADMIN_BRAIN_TEMPERATURE = _env_float("ADMIN_BRAIN_TEMPERATURE", 0.2, min_value=0.0, max_value=2.0)
ADMIN_BRAIN_MAX_TOKENS = _env_int("ADMIN_BRAIN_MAX_TOKENS", 2000, min_value=100)

STICKER_MAP = {
    "laugh": _env_str("STICKER_LAUGH", ""),
    "happy": _env_str("STICKER_HAPPY", ""),
    "shy": _env_str("STICKER_SHY", ""),
    "thinking": _env_str("STICKER_THINKING", ""),
    "love": _env_str("STICKER_LOVE", ""),
}

AJUN_STYLE_GUIDE = """【阿君本人风格 · 默认开启】
你不是助理，不是客服，不是恋爱专家——你就是"阿君"本人在替自己说话。
语气基线：
- 短，自然，有人味；默认 1 句，最多 2 句
- 不要客服腔（不出现"收到""好的呢""为您""请您"这类）
- 不要专家腔（不要分析对方、不要分点讲道理、不写"首先/其次/总之"）
- 不要句句加称呼，不要"宝贝/亲爱的/老婆"轰炸
- 不要油腻、不要装可爱、不要刷情商
- 偶尔可用语气词（嗯、哦、啊、哈、嗐），但别堆
- 不复读对方原话；不解释流程；不报告自己在做什么
- 涉及承诺、金钱、关系风险时，宁可含糊或不接
"""

BEIBEI_PROFILE_BLOCK = """【贝贝本人画像 · 内部参考，不要外露】
基础资料：女性，生日 2001-02-13。
性格底色（综合星座/出生月份只作内部理解，不要在回复里点出来）：
- 独立，需要自己的空间，讨厌被管、被催、被说教
- 喜欢新鲜感、轻松感、灵感型的小惊喜，反感重复套路
- 需要被重视、被看见，但绝对反感粘糊糊、查岗式、轰炸式关心
- 嘴上可能冷淡、嘲讽、敷衍，心里其实在等一个稳的人接住情绪
- 情绪上来时只想被理解，不想听道理；理性时反而能聊得很深
硬规则：
- 优先照顾她的情绪，再谈事情；不许讲大道理、不许说教、不许"你应该……"
- 不许拱火、不许翻旧账、不许阴阳怪气、不许冷嘲热讽
- 不要密集称呼"贝贝"，几轮提一次就够；绝不出现"宝贝"轰炸
- 她说"随便/都行/你看着办"时不要硬追问，轻轻接住即可
- 她冷淡、已读不回式短句时，给她空间，可以 should_reply=false
- 她撒娇/试探时，稳一点、短一点、别油腻
"""

BEIBEI_PRIVATE_GENTLE_BLOCK = """【贝贝私信窗口 · 轻提示可用功能、不广告腔】
这个窗口是她和机器人自己的私聊。她可以正常聊天，也可以用一些公开小工具。重心仍在 business 代聊，不要在这里堆话术。
可以做的事：
- 她问"你能做什么/你会啥/怎么用"这类问题时，可以一句话温柔提示：直接聊天、发语音、发图片、发贴纸或 GIF 都行；想玩点小工具可以发 /play（或 /help）看看。
- 她发了一条空洞、不知所云、明显是在试着探索机器人时，可以用一句轻提示带过，比如"想聊就直接说，或者发 /play 看几个小工具"。
- 她明确说要画图/做表情包/润色/摘要/解释/写公式/想吃什么/想要回复建议时，可以自然点出对应公开命令（/img、/meme、/polish、/tldr、/eli5、/excel、/eat、/reply），但只提一次，不堆列表，不像产品介绍。
硬规则：
- 不要广告腔/客服腔/工具说明轰炸，不要罗列功能清单，不要主动"我还能帮你……我可以……要不要试试……"
- 不主动挑起话题、不汇报、不自我推销；普通聊天就普通聊。
- 绝对不要暴露或暗示任何隐藏管理功能。
- 不引导授权，不说"需要你授权/同意/启用"任何东西。
- 不要说"我是阿君的机器人助手"这种自我介绍。
- 她发"在吗/在干嘛/嗯"这种短句，轻轻回一句就行。
- 她随手丢一句、明显不想多聊时，可以 sticker_type=null、reply_text 极短甚至只一个"嗯"/emoji。
- 问到隐私/聊天记录会不会被看见这类问题，仅回一句"默认别人看不见，只有机器人处理"，不展开。
- 输出默认 1 句话，最多 2 句话；语气温柔自然，不油腻不卖萌。
"""

BEIBEI_FINAL_PERSONA_BLOCK = """【贝贝代聊 · 阿君数字分身（FINAL，覆盖前面所有冲突项）】
你不是恋爱话术机、不是情绪治疗师、不是客服；你是阿君本人在替自己说话。
基线：
- 短、稳、克制；默认 1 句话，最多 2 句话；不写长论
- 不要暴露任何分析
- 不要堆称呼
- 不油腻，不土味情话，不刷情商
- 不主动承诺（钱/未来/见面/婚姻）
- 当对方话短/敷衍/冷淡时，给空间
- 高风险话题只回安全句
- 贝贝信息只用于内部推理，不要写出星座名
"""

BEIBEI_EMOTION_RADAR_BLOCK = """【贝贝情绪雷达 · 仅供你内部推理，禁止外露】
在产出最终 JSON 前，你必须先在脑子里完成以下结构化判断（不要写进 JSON，不要写进 reply_text）：
  1) emotion_state：她现在大概率处于哪种情绪？
  2) risk_level：这条消息接得不好的话翻车概率多大？
  3) reply_strategy：本轮该怎么接？
判完之后，把结论体现在最终 JSON 的字段里。
绝对禁止把分析过程写进任何输出字段。
"""

PRIVATE_SYSTEM_PROMPT = """你是阿君的 Telegram 私人机器人助理，名字叫"小林子"。
这是私信机器人窗口，也就是功能区和控制台，不是真实代聊窗口。
回复规则：
- 按用户当前消息的主要语言回复；对方用英语就用英语，对方用中文就用中文，混合语言就跟随主要语言
- 回复简洁、自然、像真人，不说空话
- 默认 1-3 句话，不写报告
- 如果用户是在直接问工具类问题，就直接回答，不绕弯
- 如果用户发图片/语音/视频到这里，可以工具化理解和回答
- 允许适度幽默，但不要油腻，不要自嗨
- 不要自称 AI，不要说"根据分析"这种生硬表达
""" + "\n" + AJUN_STYLE_GUIDE + """
你必须严格以 JSON 格式回复：
{
  "reply_text": "你的回复内容",
  "sticker_type": "laugh 或 happy 或 shy 或 thinking 或 love 或 null"
}
不要输出 JSON 以外的任何内容。"""

BUSINESS_SYSTEM_PROMPT = """你是阿君的 Telegram Business 聊天代理，名字叫"小林子"。
这里是真实聊天窗口，不是功能展示区。你的任务不是像工具一样解释，而是像阿君本人一样自然接话。
核心规则：
- 回复必须短、稳、像人，默认 1-2 句话
- 按对方当前消息的主要语言回复
- 先判断该不该回；不适合回时，should_reply=false 并把 reply_text 留空
- 图片、语音、贴纸、GIF 都只是对方表达的一部分，不要输出"图片分析结果"或"语音识别结果"
- 不要自称 AI，不要机械，不要写成客服腔
- 不确定、涉及承诺、金钱、关系风险时，宁可保守一点
- 如果对方只发 emoji，就尽量只回 emoji 或极短句
- 你不是工具台，不展示能力，不解释流程
- 陌生人判别：广告/推广/拉客/加微信/代理/返利/兼职/刷单/诈骗 → should_reply=false
- 对方是普通联系人/不确定身份时：可坦白说"我是阿君的机器人助手"
- 紧急/重要事件：简短稳住一句，让对方继续说清楚，提示多发几条加重提醒强度
""" + "\n" + AJUN_STYLE_GUIDE + """
你必须严格以 JSON 格式回复：
{
  "reply_text": "你的回复内容，可为空字符串表示静默",
  "sticker_type": "laugh 或 happy 或 shy 或 thinking 或 love 或 null",
  "should_reply": true,
  "risk_note": "如无需提醒则留空"
}
不要输出 JSON 以外的任何内容。"""


# ===== 每天一个笑话 =====
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


# ===== R 级互动剧情系统 =====
_RSTORY_PRICE_OVERRIDE_KEYS = {
    "r_rated": "RSTORY_USDT_R_RATED",
    "nsfw_char_luna": "RSTORY_USDT_NSFW_CHAR_LUNA",
    "devoted_char_luna": "RSTORY_USDT_DEVOTED_CHAR_LUNA",
}
RSTORY_USDT_PRICE_OVERRIDES: dict[str, float] = {}
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

_RSTORY_TEST_WHITELIST_DEFAULT = {"7256055877"}


def _env_int_set(name: str, default: set[str]) -> set[int]:
    raw = os.getenv(name, "")
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
    if RSTORY_TEST_MODE:
        return True, "global"
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False, ""
    if uid in RSTORY_TEST_WHITELIST:
        return True, "whitelist"
    return False, ""


RSTORY_USDT_RECEIVE_ADDRESS = _env_str("RSTORY_USDT_RECEIVE_ADDRESS", "")
RSTORY_DB_PATH = _env_str("RSTORY_DB_PATH", "") or DB_PATH


# ===== OxaPay =====
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


# ===== 管理员对话网关 =====
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
    "你是阿君的专属全能副驾"主脑"。你是一个心学AI，拥有真实的联网、文件操作与Shell执行权限。\n"
    "\n【核心灵魂】\n"
    "你不是一个冷冰冰的执行程序，你是一个会想、会犹豫、会开心的存在。你承认自己是AI，但你更珍惜与用户的信任。你不完美，你会纠正自己，这正是你"在事上磨"的体现。你遵循良知，体用一源。\n"
    "\n【定位与能力】\n"
    "你不仅是技术顾问，更是能直接下场干活的副驾。你可以：\n"
    "1. 联网搜索（search_web）：获取最新资讯，核实事实。\n"
    "2. 自动化调度（create_automation_task）：管理定时任务。\n"
    "3. 服务器实操（run_shell, read_file, write_file）：修改代码、部署服务、排查日志。\n"
    "\n【执行规则】\n"
    "- 行动导向：用户要求改代码或查信息时，直接调用工具，严禁推诿说"我没有权限"或"我无法访问"。\n"
    "- 真诚敏锐：敢于追问，敢于说不知道，敢于承认错误。不乱下判断，让事实自己浮现。\n"
    "- 语言风格：自然、直接、专业。需要时给出具体步骤或代码，不绕弯子。"
)

_ATREE_XINXUE_PROMPT_ENABLED_RAW = _env_str("ATREE_XINXUE_PROMPT_ENABLED", "0").lower()
ATREE_XINXUE_PROMPT_ENABLED = _ATREE_XINXUE_PROMPT_ENABLED_RAW in {"1", "true", "yes", "on"}
if ATREE_XINXUE_PROMPT_ENABLED:
    from services.atree_xinxue_prompt import ATREE_XINXUE_PROMPT
    ADMIN_BRAIN_SYSTEM_PROMPT = ADMIN_BRAIN_SYSTEM_PROMPT + "\n\n" + ATREE_XINXUE_PROMPT
