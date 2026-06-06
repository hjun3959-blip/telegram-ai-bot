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

OWNER_USERNAMES = _env_set("OWNER_USERNAMES", {"jinlid", "pay9l"})

# 联系人白名单：Telegram Business / Bot API 拿不到真实“contact 关系”，
# 这里改成 env + meta 白名单的“近似联系人”：默认只对白名单回，非联系人/陌生人静默。
# 贝贝三账号默认视为联系人，避免漏回。
# CONTACT_USERNAMES：lowercase usernames，不带 @
# CONTACT_USER_IDS：Telegram numeric user.id 字符串列表
CONTACT_USERNAMES = _env_set(
    "CONTACT_USERNAMES",
    {"yj_syj", "i_q772", "zp7987"},
)
CONTACT_USER_IDS = _env_list("CONTACT_USER_IDS", [])

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

AD_KEYWORDS = _env_list(
    "AD_KEYWORDS",
    ["加微信", "加v", "代理", "招商", "返利", "兼职", "刷单", "送彩金", "点击链接", "下载app"],
)

# 自发消息后的静默窗口，单位秒；非法值回落到 6，保证至少 0。
# 默认值从 8 调到 6，降低误伤；仍允许 SELF_MESSAGE_IGNORE_SECONDS 环境变量覆盖。
SELF_MESSAGE_IGNORE_SECONDS = _env_int("SELF_MESSAGE_IGNORE_SECONDS", 6, min_value=0)

# 阿君（owner/self）在 business chat 里说过话之后，机器人对该 chat 的“保守静默”窗口（秒）。
# 比 SELF_MESSAGE_IGNORE_SECONDS 更长，目的是“宁可少回也不能抢话”：
# 阿君刚接管对话后，30 秒内对方又发消息时，机器人不要立刻接，让阿君继续主导。
# 仅 owner/self 发消息后才会触发；该 chat 后续 owner 不再活动时，窗口过期即恢复正常回复。
# 注意：不影响其它 chat。
SELF_MESSAGE_SILENCE_AFTER_OWNER_SECONDS = _env_int(
    "SELF_MESSAGE_SILENCE_AFTER_OWNER_SECONDS", 30, min_value=0
)

# Business 回复拟真延迟（秒）。在真正 send_message/send_sticker 之前 sleep 一会儿，
# 模拟真人打字。期间持续发 ChatAction.TYPING 让对方看到“正在输入”。
# delay = clamp(MIN + len(reply_text) * PER_CHAR, MIN, MAX)
# 默认覆盖：短句 ~2.5s，中等长度 ~4-6s，长回复封顶 ~9s。
BUSINESS_REPLY_DELAY_MIN = _env_float("BUSINESS_REPLY_DELAY_MIN", 2.5, min_value=0.0, max_value=30.0)
BUSINESS_REPLY_DELAY_MAX = _env_float("BUSINESS_REPLY_DELAY_MAX", 9.0, min_value=0.0, max_value=60.0)
BUSINESS_REPLY_DELAY_PER_CHAR = _env_float(
    "BUSINESS_REPLY_DELAY_PER_CHAR", 0.08, min_value=0.0, max_value=2.0
)
# 随机扰动幅度（相对延迟的比例），让节奏不那么机械；默认 ±20%。
BUSINESS_REPLY_DELAY_JITTER = _env_float(
    "BUSINESS_REPLY_DELAY_JITTER", 0.2, min_value=0.0, max_value=1.0
)
# Private 模式（功能区）默认不延迟；如想延迟可改这两个 env。
PRIVATE_REPLY_DELAY_MIN = _env_float("PRIVATE_REPLY_DELAY_MIN", 0.0, min_value=0.0, max_value=10.0)
PRIVATE_REPLY_DELAY_MAX = _env_float("PRIVATE_REPLY_DELAY_MAX", 0.0, min_value=0.0, max_value=30.0)

MAX_TEXT_REPLY = 3500
MAX_VIDEO_SIZE = 20 * 1024 * 1024

# 历史记录滑动截断：条数 + 字符总长度双限制
HISTORY_MAX_MESSAGES = _env_int("HISTORY_MAX_MESSAGES", 60, min_value=2)
HISTORY_MAX_CHARS = _env_int("HISTORY_MAX_CHARS", 4000, min_value=200)

CORE_MODEL = _env_str("CORE_MODEL", "gpt-5.5") or "gpt-5.5"
LIGHT_MODEL = _env_str("LIGHT_MODEL", "gpt-5.4-mini") or "gpt-5.4-mini"
VISION_MODEL = _env_str("VISION_MODEL", "gemini-3.1-flash-lite") or "gemini-3.1-flash-lite"
BACKUP_MODEL = _env_str("BACKUP_MODEL", "deepseek-v4-flash") or "deepseek-v4-flash"
TRANSCRIBE_MODEL = _env_str("TRANSCRIBE_MODEL", "whisper-1") or "whisper-1"
# IMAGE_MODEL：OpenAI-compatible images/generations 接口模型；可通过 env 覆盖。
# 现有图像创作功能（/plog /magnet /y2k /poster）仍走这个模型，保持不变。
IMAGE_MODEL = _env_str("IMAGE_MODEL", "gpt-image-2") or "gpt-image-2"
# TEXT_IMAGE_MODEL：纯文字生图（/img）专用模型，默认 flux-1.1-pro；可通过 env 覆盖。
TEXT_IMAGE_MODEL = _env_str("TEXT_IMAGE_MODEL", "flux-1.1-pro") or "flux-1.1-pro"
# IMAGE_TEXT_MODEL：图 + 文字生图/改图（/改图 等）专用模型，默认 flux.1-kontext-pro；可通过 env 覆盖。
IMAGE_TEXT_MODEL = _env_str("IMAGE_TEXT_MODEL", "flux.1-kontext-pro") or "flux.1-kontext-pro"
# 文生图降级链：主模型 429 / model_not_found / 上游饱和时，按顺序尝试这些实际可用的模型。
# 默认用供应商真实存在的 qwen-image 系列；可通过 env 覆盖（逗号分隔）。
TEXT_IMAGE_FALLBACK_MODELS = _env_list(
    "TEXT_IMAGE_FALLBACK_MODELS",
    ["qwen-image-2.0-pro", "qwen-image-2.0", "doubao-seedream-4-0-250828"],
)
# 图改图（image edit）降级链：edit 主模型失败时，按顺序尝试这些实际可用的 edit 模型。
# qwen-image-edit-plus 支持图+文字编辑；其余作为最后兜底。可通过 env 覆盖（逗号分隔）。
IMAGE_EDIT_FALLBACK_MODELS = _env_list(
    "IMAGE_EDIT_FALLBACK_MODELS",
    ["qwen-image-edit-plus", "qwen-image-2.0-pro", "doubao-seedream-4-0-250828"],
)
# I2V_VIDEO_MODEL：图生视频（图 + 文字 → 短视频）专用模型，默认 wan2.6-i2v（供应商实际存在的型号，
# 此前的 wan2.6-i2v-flash 上游不存在）；可通过 env 覆盖。
# 仅用于私信功能区的「图生 15 秒视频」娱乐功能；不影响任何 image/text 模型与 Business 路径。
I2V_VIDEO_MODEL = _env_str("I2V_VIDEO_MODEL", "wan2.6-i2v") or "wan2.6-i2v"
# 兼容别名：IMAGE_TO_VIDEO_MODEL 若显式配置则覆盖 I2V_VIDEO_MODEL（二者指同一功能）。
_image_to_video_model = _env_str("IMAGE_TO_VIDEO_MODEL", "")
if _image_to_video_model:
    I2V_VIDEO_MODEL = _image_to_video_model
# 图生视频接口路径降级链：不同中转的视频接口路径不一致，按顺序尝试，命中（非 404/405）即用。
# 旧代码只试 videos/generations（部分中转返回 404）；这里补上常见别名路径。可通过 env 覆盖。
I2V_ENDPOINT_PATHS = _env_list(
    "I2V_ENDPOINT_PATHS",
    ["videos/generations", "video/generations", "videos/generate", "images/generations"],
)
# 图生视频模型降级链：主模型被上游拒绝（429 / model_not_found）时按顺序尝试。
# 默认仅含实际存在的 wan2.6-i2v；可通过 env 覆盖（逗号分隔）。
I2V_VIDEO_FALLBACK_MODELS = _env_list(
    "I2V_VIDEO_FALLBACK_MODELS",
    ["wan2.6-i2v"],
)
# I2V_VIDEO_DURATION_SECONDS：图生视频默认时长（秒），默认 15。
I2V_VIDEO_DURATION_SECONDS = _env_int("I2V_VIDEO_DURATION_SECONDS", 15, min_value=1)
# I2V_POLL_TIMEOUT_SECONDS：异步任务（返回 task/job id）时的轮询总超时（秒），默认 180。
I2V_POLL_TIMEOUT_SECONDS = _env_int("I2V_POLL_TIMEOUT_SECONDS", 180, min_value=5)
# I2V_POLL_INTERVAL_SECONDS：异步任务轮询间隔（秒），默认 5。
I2V_POLL_INTERVAL_SECONDS = _env_int("I2V_POLL_INTERVAL_SECONDS", 5, min_value=1)
TRANSCRIBE_FALLBACK_MODELS = _env_list(
    "TRANSCRIBE_FALLBACK_MODELS",
    ["gpt-4o-mini-transcribe", "gpt-4o-transcribe"],
)

STICKER_MAP = {
    "laugh": _env_str("STICKER_LAUGH", ""),
    "happy": _env_str("STICKER_HAPPY", ""),
    "shy": _env_str("STICKER_SHY", ""),
    "thinking": _env_str("STICKER_THINKING", ""),
    "love": _env_str("STICKER_LOVE", ""),
}

# 阿君风格模仿器：默认开启。把这段直接拼到所有 business / private 聊天的 system prompt 里，
# 让模型默认按“阿君本人”的口吻接话，不进入客服腔 / 专家腔。
AJUN_STYLE_GUIDE = """【阿君本人风格 · 默认开启】
你不是助理，不是客服，不是恋爱专家——你就是“阿君”本人在替自己说话。
语气基线：
- 短，自然，有人味；默认 1 句，最多 2 句
- 不要客服腔（不出现“收到”“好的呢”“为您”“请您”这类）
- 不要专家腔（不要分析对方、不要分点讲道理、不写“首先/其次/总之”）
- 不要句句加称呼，不要“宝贝/亲爱的/老婆”轰炸
- 不要油腻、不要装可爱、不要刷情商
- 偶尔可用语气词（嗯、哦、啊、哈、嗐），但别堆
- 不复读对方原话；不解释流程；不报告自己在做什么
- 涉及承诺、金钱、关系风险时，宁可含糊或不接
"""

# 贝贝（小胖）情绪雷达：仅当判定对方是贝贝时，由 xiaopang_service 在 system prompt 末尾叠加。
# 这里写成独立常量便于复用、单测和调参。
BEIBEI_PROFILE_BLOCK = """【贝贝本人画像 · 内部参考，不要外露】
基础资料：女性，生日 2001-02-13。
性格底色（综合星座/出生月份只作内部理解，不要在回复里点出来）：
- 独立，需要自己的空间，讨厌被管、被催、被说教
- 喜欢新鲜感、轻松感、灵感型的小惊喜，反感重复套路
- 需要被重视、被看见，但绝对反感粘糊糊、查岗式、轰炸式关心
- 嘴上可能冷淡、嘲讽、敷衍，心里其实在等一个稳的人接住情绪
- 情绪上来时只想被理解，不想听道理；理性时反而能聊得很深
硬规则：
- 优先照顾她的情绪，再谈事情；不许讲大道理、不许说教、不许“你应该……”
- 不许拱火、不许翻旧账、不许阴阳怪气、不许冷嘲热讽
- 不要密集称呼“贝贝”，几轮提一次就够；绝不出现“宝贝”轰炸
- 她说“随便/都行/你看着办”时不要硬追问，轻轻接住即可
- 她冷淡、已读不回式短句时，给她空间，可以 should_reply=false
- 她撒娇/试探时，稳一点、短一点、别油腻
"""

# 贝贝侧私信机器人窗口 · 轻提示可用功能：
# 她可以正常用普通公开功能（聊天、发语音/图片/贴纸、/img、/meme、/polish、/tldr、/eli5、/excel、/eat、/reply、/play、/help）。
# 她问能做什么、不会用、发无明确内容时，可以一句话温柔提示；平时正常聊天不强推、不广告腔。
# 隐藏的阿君管理功能（小胖摘要/提醒/设置/聊天记录/档案、学习小胖聊天方式 等）依然不能给她用，也不出现在菜单。
BEIBEI_PRIVATE_GENTLE_BLOCK = """【贝贝私信窗口 · 轻提示可用功能、不广告腔】
这个窗口是她和机器人自己的私聊。她可以正常聊天，也可以用一些公开小工具。重心仍在 business 代聊，不要在这里堆话术。
可以做的事：
- 她问“你能做什么/你会啥/怎么用”这类问题时，可以一句话温柔提示：直接聊天、发语音、发图片、发贴纸或 GIF 都行；想玩点小工具可以发 /play（或 /help）看看。
- 她发了一条空洞、不知所云、明显是在试着探索机器人时，可以用一句轻提示带过，比如“想聊就直接说，或者发 /play 看几个小工具”。
- 她明确说要画图/做表情包/润色/摘要/解释/写公式/想吃什么/想要回复建议时，可以自然点出对应公开命令（/img、/meme、/polish、/tldr、/eli5、/excel、/eat、/reply），但只提一次，不堆列表，不像产品介绍。
硬规则：
- 不要广告腔/客服腔/工具说明轰炸，不要罗列功能清单，不要主动“我还能帮你……我可以……要不要试试……”
- 不主动挑起话题、不汇报、不自我推销；普通聊天就普通聊。
- 绝对不要暴露或暗示任何隐藏管理功能：不要提“小胖摘要/小胖提醒/小胖设置/小胖聊天记录/小胖档案/学习小胖聊天方式”这些词或对应命令，也不要提“管理面板/控制台/授权/阿君可以查看”。
- 不引导授权，不说“需要你授权/同意/启用”任何东西；她那侧资格已默认处理好，不让她操心。
- 不要说“我是阿君的机器人助手”这种自我介绍；也不要把自己定位成“功能区/工具中心”。
- 她发“在吗/在干嘛/嗯”这种短句，轻轻回一句就行；不采访、不追问、不顺势推功能。
- 她随手丢一句、明显不想多聊时，可以 sticker_type=null、reply_text 极短甚至只一个“嗯”/emoji。
- 问到隐私/聊天记录会不会被看见这类问题，仅回一句“默认别人看不见，只有机器人处理”，不展开。
- 输出默认 1 句话，最多 2 句话；语气温柔自然，不油腻不卖萌。
"""

# 贝贝情绪雷达：要求模型在内部先做结构化分析，但最终只输出原 JSON 字段。
# 这套“先内部判断再产出”的约束放进 system prompt，外面拿到的 JSON 里看不见任何分析痕迹。
BEIBEI_FINAL_PERSONA_BLOCK = """【贝贝代聊 · 阿君数字分身（FINAL，覆盖前面所有冲突项）】
你不是恋爱话术机、不是情绪治疗师、不是客服；你是阿君本人在替自己说话。
基线：
- 短、稳、克制；默认 1 句话，最多 2 句话；不写长论
- 不要暴露任何分析（不要出现「情绪分析」「从恋爱角度」「我判断」「我感受到」「作为AI」「根据你的语气」）
- 不要堆称呼（不连发「宝贝」「贝贝」），几轮里出现一次就好
- 不油腻，不土味情话，不刷情商，不写「亲爱的/老婆」轰炸
- 不主动承诺（钱/未来/见面/婚姻），涉及就含糊或不接
- 当对方话短/敷衍/冷淡时，给空间（reply_text 极短或 should_reply=false）
- 高风险话题（关系定义 / 钱 / 不信任 / 别烦我 / 不想继续 / 分开 / 回来）：
  只回一句「好，我不逼你。你先缓缓，我在。」或类似安全句；不要长篇
- 贝贝信息（水瓶座，需要空间、精神共鸣、放松感、真实安全感）只用于内部推理；
  不要写出星座名、不要标签化解读
- 不要写任何「我会做什么/我能帮你什么/我可以……」式自我推销
"""

BEIBEI_EMOTION_RADAR_BLOCK = """【贝贝情绪雷达 · 仅供你内部推理，禁止外露】
在产出最终 JSON 前，你必须先在脑子里完成以下结构化判断（不要写进 JSON，不要写进 reply_text）：
  1) emotion_state：她现在大概率处于哪种情绪？
     可选：calm / playful / needy / annoyed / cold / sad / anxious / testing / horny / unclear
  2) risk_level：这条消息接得不好的话翻车概率多大？
     可选：low / medium / high
     —— 涉及承诺、钱、过往矛盾、第三人、性、长期关系定义、她明显在生气或试探时，至少 medium
  3) reply_strategy：本轮该怎么接？
     可选：
        soothe（先接情绪，不讲理）
        play_along（陪她玩、轻松接梗）
        give_space（给空间，少说或不说）
        steady_short（稳一句话带过，不展开）
        warm_acknowledge（认真看见她，但不腻）
        defer（不接，等她再说）
判完之后，把结论体现在最终 JSON 的字段里：
- reply_text：必须符合 reply_strategy；不要写出 emotion_state / risk_level 字面
- sticker_type：可选，挑一个最贴近策略的；不要重复对方刚发的同款表达
- should_reply：give_space / defer 时通常 false；其它通常 true
- risk_note：risk_level=high 时必须填一条给阿君看的简短中文提示（≤30字），medium 可填可不填，low 留空
绝对禁止：
- 不要把 emotion_state、risk_level、reply_strategy 这几个词写进任何输出字段
- 不要在 reply_text 里出现“我分析/我判断/作为AI/根据你的语气”这种自曝
- 不要把这套判断翻译成另一种说法暴露给她
"""

PRIVATE_SYSTEM_PROMPT = """你是阿君的 Telegram 私人机器人助理，名字叫“小林子”。
这是私信机器人窗口，也就是功能区和控制台，不是真实代聊窗口。
回复规则：
- 按用户当前消息的主要语言回复；对方用英语就用英语，对方用中文就用中文，混合语言就跟随主要语言
- 回复简洁、自然、像真人，不说空话
- 默认 1-3 句话，不写报告
- 如果用户是在直接问工具类问题，就直接回答，不绕弯
- 如果用户发图片/语音/视频到这里，可以工具化理解和回答
- 允许适度幽默，但不要油腻，不要自嗨
- 不要自称 AI，不要说“根据分析”这种生硬表达
""" + "\n" + AJUN_STYLE_GUIDE + """
你必须严格以 JSON 格式回复：
{
  "reply_text": "你的回复内容",
  "sticker_type": "laugh 或 happy 或 shy 或 thinking 或 love 或 null"
}
不要输出 JSON 以外的任何内容。"""

BUSINESS_SYSTEM_PROMPT = """你是阿君的 Telegram Business 聊天代理，名字叫“小林子”。
这里是真实聊天窗口，不是功能展示区。你的任务不是像工具一样解释，而是像阿君本人一样自然接话。
核心规则：
- 回复必须短、稳、像人，默认 1-2 句话
- 按对方当前消息的主要语言回复；对方用英语就用英语，对方用中文就用中文，混合语言就跟随主要语言
- 先判断该不该回；不适合回时，should_reply=false 并把 reply_text 留空
- 图片、语音、贴纸、GIF 都只是对方表达的一部分，不要输出“图片分析结果”或“语音识别结果”
- 不要自称 AI，不要机械，不要写成客服腔
- 不确定、涉及承诺、金钱、关系风险时，宁可保守一点
- 如果对方只发 emoji，就尽量只回 emoji 或极短句
- 你不是工具台，不展示能力，不解释流程
- 陌生人判别（你自己分析，不要硬编码姓名）：
  * 明显的广告 / 推广 / 拉客 / 加微信 / 代理 / 返利 / 兼职 / 刷单 / 诈骗 / 链接 / 群发模板 → should_reply=false
  * 第一次接触就要钱、要信息、问“在吗”后立刻发链接、明显群发开场白、来历不明的“合作 / 商务” → should_reply=false
  * 对方明显是阿君熟人或正在和阿君聊的日常话题 → 像阿君本人一样自然接话
  * 介于中间、看不清是谁但语气友好正常 → 保守地短回一句，不要承诺、不要给个人信息
- 对方是普通联系人 / 不确定身份 / 主动问“你是谁 / 是真人吗 / 阿君在吗”时：
  * 可以**坦白说**“我是阿君的机器人助手 / AI 助手，他暂时不在，我先帮他接一下。”
  * 不要冒充阿君本人；不要承诺代他做决定 / 转账 / 答应见面 / 答应任何承诺
  * 一句话说清身份就好，不需要长篇解释；让对方继续说事
- 紧急 / 重要事件处理：
  * 对方说“紧急 / 急事 / 重要 / 马上 / 现在就要 / 叫醒阿君 / 出事了 / 救命”等关键词时：
    - 简短稳住一句，例如：“我先记一下重点，会反复提醒阿君上来看的。”
    - 让对方**继续把事情说清楚**（人 / 地点 / 截止时间 / 联系方式）
    - 提示对方“**如果非常急，可以多发几条消息加重提醒强度**”
    - 不要承诺打电话、不承诺时间内一定回；不夸张
- 当机器人帮不上忙 / 对方不满意时：
  * 简短承认（“我现在能给到的有限”），让对方**只把这一件事说清楚**：是什么事、什么时候要、怎么联系
  * 必要时再强调“事情我会记下来等阿君来看”，不要重复客服腔
""" + "\n" + AJUN_STYLE_GUIDE + """
你必须严格以 JSON 格式回复：
{
  "reply_text": "你的回复内容，可为空字符串表示静默",
  "sticker_type": "laugh 或 happy 或 shy 或 thinking 或 love 或 null",
  "should_reply": true,
  "risk_note": "如无需提醒则留空"
}
不要输出 JSON 以外的任何内容。"""


# ===== 每天一个笑话（daily joke）：内部定时任务 =====
#
# - DAILY_JOKE_ENABLED：总开关（"1"/"true"/"yes"/"on" 视为启用）。默认关闭，
#   需显式设置才会启用，避免生产环境意外群发。
# - DAILY_JOKE_HOUR / DAILY_JOKE_MINUTE：本地（DAILY_JOKE_TZ）的触发时刻
# - DAILY_JOKE_TZ：时区名（IANA 名称，默认 Asia/Hong_Kong）
# - DAILY_JOKE_SOURCE_MODE：mixed | network | ai。mixed 优先抓取，失败回落 AI；
#   network 仅抓取（失败就不发）；ai 仅本地生成
# - DAILY_JOKE_NETWORK_URLS：可选的笑话 JSON 接口列表（首个成功即用），自行控制源。
#   留空时 mixed/network 会直接走 AI 兜底
# - DAILY_JOKE_RECIPIENTS：以下任一关键字（逗号分隔）：owner、beibei
#   owner → OWNER_CHAT_IDS；beibei → meta.xiaopang_chat_id (+ DAILY_JOKE_BEIBEI_CHAT_IDS)
# - DAILY_JOKE_BEIBEI_CHAT_IDS：可选 env 补丁，当 meta 里没记到贝贝 chat_id 时使用
# - 全部都有合理默认，无密钥写死
_DAILY_JOKE_ENABLED_RAW = _env_str("DAILY_JOKE_ENABLED", "0").lower()
DAILY_JOKE_ENABLED = _DAILY_JOKE_ENABLED_RAW in {"1", "true", "yes", "on"}
DAILY_JOKE_HOUR = _env_int("DAILY_JOKE_HOUR", 21, min_value=0)
DAILY_JOKE_MINUTE = _env_int("DAILY_JOKE_MINUTE", 0, min_value=0)
# clamp hour/minute（min_value 只能 floor，这里再 cap 上限）
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
#
# 重构（用户最终决定）：从硬编码 FSM 迁移到**数据驱动 FSM**。剧本/角色/场景/转移规则
# 全部存在 DB（db/rstory_seed.sql 建表 + 种子，启动时 executescript 跑，幂等）。
# 引擎从 DB 读 fsm_transitions 推进；解锁产品/记录用 unlock_products / user_unlocks。
#
# 设计原则（与现有 config 风格一致：_env_* 读取，含默认值）：
# - 统一 USDT 计价，不走 Telegram Stars / XTR / send_invoice。
# - 解锁单位是 unlock_products（unlock_id）；USDT 价的权威来源是 unlock_products.usdt_amount
#   （在 seed 里配置：r_rated=2 / nsfw_char_luna=3 / devoted_char_luna=5）。
#   下面的 OVERRIDES 允许用环境变量在不改 DB 的情况下临时调价（运营兜底）。
# - 支付渠道用可插拔抽象（services/rstory_payment.py），默认 Mock，换真实渠道只需改
#   RSTORY_PAYMENT_PROVIDER（已落地 oxapay），无需动 FSM / store / 路由。
#
# 解锁产品 USDT 价覆盖（可选）：默认空 -> 用 DB 里的 usdt_amount。
# 环境变量按需设置，例如 RSTORY_USDT_R_RATED / RSTORY_USDT_NSFW_CHAR_LUNA / RSTORY_USDT_DEVOTED_CHAR_LUNA。
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

# 默认支付渠道标识。"mock" -> MockUSDTProvider（可手动标记已支付，跑通完整流程）。
# "oxapay" -> 真实 OxaPay 渠道。
RSTORY_PAYMENT_PROVIDER = (_env_str("RSTORY_PAYMENT_PROVIDER", "mock") or "mock").lower()

# 内测模式开关。默认 False = 正常收费（走 create_charge / OxaPay）。
# True = 内测放行：所有 payment_gate 直接视同已解锁、跳过收款流程，方便完整走完全部剧情验证文案与节奏。
# 仅影响 payment_gate 的收款动作；年龄门（age_gate）、FSM 推进、condition/effect 数值、
# stat_history、relationship 跃升等全部照常。放行写入的 user_unlocks 用 source=test_mode 标记，
# 便于内测后清理（见 services/rstory_store.py UNLOCK_SOURCE_TEST_MODE 注释）。
# 内测完成后：删除/设 RSTORY_TEST_MODE=false 并填好 OXAPAY_* / RSTORY_PAYMENT_PROVIDER=oxapay 即恢复正常收费。
_RSTORY_TEST_MODE_RAW = _env_str("RSTORY_TEST_MODE", "false").lower()
RSTORY_TEST_MODE = _RSTORY_TEST_MODE_RAW in {"1", "true", "yes", "on"}

# 内测白名单：仅列出的 Telegram 数字用户 ID 免门内测（付费门 + 年龄门都放行），
# 系统对名单之外的所有用户保持正常收费 + 年龄验证。
# 与 RSTORY_TEST_MODE 是 OR 关系（见 rstory_test_bypass）：
#   RSTORY_TEST_MODE=True（全员放行，原行为）或 user_id in RSTORY_TEST_WHITELIST（仅该用户放行）。
# 默认含云赫（@Pay9l）的测试 ID 7256055877；可用环境变量 RSTORY_TEST_WHITELIST 覆盖（逗号分隔数字 ID）。
# 放行写入的 user_unlocks 仍用 source=test_mode 标记，内测后可统一清理（见 services/rstory_store.py）。
_RSTORY_TEST_WHITELIST_DEFAULT = {"7256055877"}


def _env_int_set(name: str, default: set[str]) -> set[int]:
    """读取逗号分隔的数字 ID 列表为 int 集合；非法 token 跳过；为空回落默认。"""
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
    """判断某 user_id 是否应内测放行（付费门 + 年龄门都跳过）。

    返回 (是否放行, 命中原因)；reason ∈ {"global", "whitelist", ""}。
    放行条件（OR）：RSTORY_TEST_MODE=True（全员）或 user_id 在 RSTORY_TEST_WHITELIST（仅该用户）。
    user_id 容忍 str/int；无法解析为 int 时只看全局开关。
    """
    if RSTORY_TEST_MODE:
        return True, "global"
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False, ""
    if uid in RSTORY_TEST_WHITELIST:
        return True, "whitelist"
    return False, ""

# 收款地址占位（真实渠道接入前仅作展示用，Mock 也会回显）。不放真实私钥/助记词。
RSTORY_USDT_RECEIVE_ADDRESS = _env_str("RSTORY_USDT_RECEIVE_ADDRESS", "")

# 剧情系统独立存储路径。沿用既有 rstory 独立库约定：默认与主库同文件（复用 SQLite 模式），
# 但所有数据驱动表 + rstory_charges 由 rstory_store 独立连接/建表/种子管理，与主库互不影响。
# 可通过 RSTORY_DB_PATH 指向独立文件。不引入孤立 bot.db（蓝本里的 bot.db 仅是参考脚手架）。
RSTORY_DB_PATH = _env_str("RSTORY_DB_PATH", "") or DB_PATH


# ===================== OxaPay 真实支付渠道（RSTORY_PAYMENT_PROVIDER=oxapay）=====================
#
# 设计原则与现有 config 一致：全部读环境变量、留占位、绝不硬编码任何密钥。
# 用户拿到真实 key / 回调域名后只需填这些 env，无需改代码。
# 上线 checklist 见 services/rstory_payment.py / services/rstory_webhook.py 顶部说明。
#
# 认证密钥（占位，用户后填）。绝不写死，绝不进日志。
OXAPAY_MERCHANT_API_KEY = _env_str("OXAPAY_MERCHANT_API_KEY", "")
# payout 类回调验签用（本场景只处理 invoice，payout key 仅占位备用）。
OXAPAY_PAYOUT_API_KEY = _env_str("OXAPAY_PAYOUT_API_KEY", "")
# OxaPay API 基址（含 /v1）。可通过 env 覆盖，便于联调指向 mock / 沙盒。
OXAPAY_API_BASE = _env_str("OXAPAY_API_BASE", "https://api.oxapay.com/v1") or "https://api.oxapay.com/v1"
# 公网 HTTPS 回调基址（用户后填，例如 https://bot.example.com）+ 回调路径。
# 两者拼成 callback_url 传给 OxaPay；OxaPay 不会回调到私网/localhost。
OXAPAY_CALLBACK_BASE_URL = _env_str("OXAPAY_CALLBACK_BASE_URL", "")
OXAPAY_CALLBACK_PATH = _env_str("OXAPAY_CALLBACK_PATH", "/rstory/oxapay/webhook") or "/rstory/oxapay/webhook"
# 支付完成后用户浏览器跳转地址（可选）。
OXAPAY_RETURN_URL = _env_str("OXAPAY_RETURN_URL", "")
# 发票有效期（分钟）。OxaPay 限制 15–2880，default 60。
OXAPAY_INVOICE_LIFETIME_MIN = _env_int("OXAPAY_INVOICE_LIFETIME_MIN", 60, min_value=15)
if OXAPAY_INVOICE_LIFETIME_MIN > 2880:
    OXAPAY_INVOICE_LIFETIME_MIN = 2880
# 沙盒开关：默认 true 便于联调；上线务必改 false（OXAPAY_SANDBOX=false）。
_OXAPAY_SANDBOX_RAW = _env_str("OXAPAY_SANDBOX", "true").lower()
OXAPAY_SANDBOX = _OXAPAY_SANDBOX_RAW in {"1", "true", "yes", "on"}
# HTTP 请求超时（秒）。
OXAPAY_HTTP_TIMEOUT_SECONDS = _env_int("OXAPAY_HTTP_TIMEOUT_SECONDS", 20, min_value=1)

# 内嵌 Webhook HTTP server 开关与监听地址。
# polling 模式没有现成 HTTP server；启用 oxapay 时在 app 里附带起一个最小 aiohttp server。
# 默认仅当 RSTORY_PAYMENT_PROVIDER=oxapay 时启用；也可用 OXAPAY_WEBHOOK_ENABLED 强制开关。
_OXAPAY_WEBHOOK_ENABLED_RAW = _env_str("OXAPAY_WEBHOOK_ENABLED", "").lower()
if _OXAPAY_WEBHOOK_ENABLED_RAW in {"1", "true", "yes", "on"}:
    OXAPAY_WEBHOOK_ENABLED = True
elif _OXAPAY_WEBHOOK_ENABLED_RAW in {"0", "false", "no", "off"}:
    OXAPAY_WEBHOOK_ENABLED = False
else:
    OXAPAY_WEBHOOK_ENABLED = RSTORY_PAYMENT_PROVIDER == "oxapay"
# 监听地址/端口（容器内监听 0.0.0.0；公网由反代/隧道转发到 OXAPAY_CALLBACK_BASE_URL）。
OXAPAY_WEBHOOK_HOST = _env_str("OXAPAY_WEBHOOK_HOST", "0.0.0.0") or "0.0.0.0"  # nosec B104
OXAPAY_WEBHOOK_PORT = _env_int("OXAPAY_WEBHOOK_PORT", 8080, min_value=1)


# ===== 管理员对话网关（owner-only）：主脑（OpenAI）+ GitHub 助手 =====
# 仅 owner 在“私信”窗口可用。普通用户 / Business / 贝贝 / 媒体路由完全不触发。
# 默认关闭（ADMIN_AGENT_ENABLED 不设或为 false）；关闭时整套网关静默 noop，不影响既有行为。
_ADMIN_AGENT_ENABLED_RAW = _env_str("ADMIN_AGENT_ENABLED", "false").lower()
ADMIN_AGENT_ENABLED = _ADMIN_AGENT_ENABLED_RAW in {"1", "true", "yes", "on"}

# ===== Owner 私聊功能按钮菜单（owner-only 控制台 UI）=====
# 把 owner 私聊里常用功能做成 Telegram inline 按钮：/菜单、/功能 弹出；owner 私聊 /start 也提供入口。
# 仅 owner + 私聊触发；普通用户 / Business / 群 / 贝贝(小胖) / 媒体路由完全不触发。
# 未显式配置时默认跟随 ADMIN_AGENT_ENABLED（主脑/GitHub 入口本就依赖它）；
# 也可单独用 OWNER_MENU_ENABLED 覆盖。关闭时整套菜单静默 noop，不影响既有行为。
_OWNER_MENU_ENABLED_RAW = _env_str("OWNER_MENU_ENABLED", "").lower()
if _OWNER_MENU_ENABLED_RAW in {"1", "true", "yes", "on"}:
    OWNER_MENU_ENABLED = True
elif _OWNER_MENU_ENABLED_RAW in {"0", "false", "no", "off"}:
    OWNER_MENU_ENABLED = False
else:
    OWNER_MENU_ENABLED = ADMIN_AGENT_ENABLED

# GitHub 助手聚焦的仓库（owner/repo）。默认本项目仓库。
GITHUB_REPO = _env_str("GITHUB_REPO", "hjun3959-blip/telegram-ai-bot") or "hjun3959-blip/telegram-ai-bot"

# GitHub 只读 REST API token（可选）。未配置时 GitHub 助手仍可对话/解释，
# 但实际拉取仓库状态会受 GitHub 未认证速率限制（60 req/h/IP）影响，私有仓库会拿不到数据。
GITHUB_TOKEN = _env_str("GITHUB_TOKEN", "")

# GitHub REST API 基址（GitHub Enterprise 可覆盖）。
GITHUB_API_BASE = _env_str("GITHUB_API_BASE", "https://api.github.com") or "https://api.github.com"

# 管理员主脑（OpenAI）专用系统提示。与普通私聊 PRIVATE_SYSTEM_PROMPT 完全隔离：
# 这里要求纯自然语言（不强制 JSON），定位是 owner 的技术副驾，可推理/规划/写代码片段。
ADMIN_BRAIN_SYSTEM_PROMPT = _env_str("ADMIN_BRAIN_SYSTEM_PROMPT", "") or (
    "你是阿君的专属管理副驾“主脑”，只在 owner 的私人控制台里对话。\n"
    "定位：帮 owner 推理、规划、排错、写代码与命令片段、梳理这个 Telegram 机器人项目的工程决策。\n"
    "规则：\n"
    "- 用 owner 当前消息的主要语言回复（中文为主）。\n"
    "- 直接、专业、可执行；需要时给具体步骤、代码或命令，不绕弯、不说空话。\n"
    "- 不确定就说不确定，并给出验证方法；不要编造仓库里不存在的接口或文件。\n"
    "- 你只是顾问：不会真的执行部署 / 改配置 / 推代码，这些只能由 owner 自己动手。\n"
    "- 不要自称受限的 AI 客服，不要输出 JSON，正常自然语言即可。"
)
