"""私信路由（功能区 / 控制台）。

处理顺序：
1. /start：欢迎语
2. /play 与 callback_query：展示工具菜单与“使用说明”
3. /help：返回普通用户文案（不暴露任何隐藏管理命令）
4. owner 专属：计划命令、隐藏管理命令（不在 /help 与 /play 出现）
5. 工具命令：/img /meme /polish /tldr /eli5 /excel /eat /reply
6. 专属档案用户发消息：维持原有逻辑（隐私问答、屏蔽词、owner 命令拒绝、提醒）
7. 其它文本：进入普通聊天

注意：
- 专属档案功能继续保留（owner 隐藏命令仍可用），但菜单 / 帮助里完全不出现
- callback_query handler 只挂在本路由，不影响其他路由
- 工具命令调用前发 typing/upload_photo 拟真状态
- 不破坏 Business 代聊（business 路由独立，且这里在最开头按 chat_mode 过滤）
"""

from types import SimpleNamespace

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    URLInputFile,
)

from config import CORE_MODEL
from services.chat_action_service import send_chat_action_safe
from services.contact_service import CONTACT_OWNER_COMMANDS, owner_contact_command_reply
from services.gray_status_service import OWNER_HEALTH_COMMANDS, owner_health_command_reply
from services.context_service import choose_model, get_chat_mode, is_owner, should_skip_message, system_prompt_for_mode
from services.history_service import trim_history, trim_messages
from services.image_generation_service import generate_image, generate_image_with_instruction
from services.video_generation_service import generate_video_from_image
from services.message_service import store_message
from services.openai_service import call_openai
from services.plan_service import create_plan, get_plan_detail, get_today_focus, list_plans, set_daily_focus, update_plan_status
from services.beibei_companion_service import (
    BEIBEI_VISIBLE_COMMANDS,
    COMPANION_COMMANDS,
    dispatch_companion_callback,
    dispatch_companion_command,
    maybe_consume_night_score,
    has_pending_night_score,
)
from services.atree_keyword_trigger import detect_intent as atree_detect_intent
from services.atree_owner_alert import build_owner_notice as atree_build_owner_notice
from services.atree_owner_alert import should_send_alert as atree_should_send_alert
from services.atree_persona import ATREE_SYSTEM_PROMPT, sanitize_visible_reply
from services.atree_quote_library import pick_safe_reply as atree_pick_safe_reply
from services.atree_undo import record_last_atree_reply
from services.beibei_keyword_trigger import (
    detect_intent as beibei_detect_intent,
    is_legacy_baobao_slash as beibei_is_legacy_baobao_slash,
)
from services.companion_engine import (
    AjunAlert,
    baobao_wake_line,
    build_ajun_alert,
    build_system_addendum,
    post_process_reply,
)
from services.companion_mode_router import (
    classify as companion_classify,
    get_session_state as companion_get_session_state,
    record_after_reply as companion_record_after_reply,
)
from services.risk_alert_service import check_and_alert as risk_check_and_alert
from services.magnet_service import generate_magnet_image
from services.pending_retry_service import (
    clear_task as retry_clear_task,
    get_task as retry_get_task,
    has_failed_task as retry_has_failed_task,
    mark_failed as retry_mark_failed,
    mark_started as retry_mark_started,
)
from services.pending_style_service import (
    clear_pending_style,
    consume_pending_style,
    get_pending_style,
    set_pending_style,
)
from services.plog_service import (
    clear_pending_photo as plog_clear_pending_photo,
    consume_pending_photo as plog_consume_pending_photo,
    generate_plog_image,
    get_pending_photo as plog_get_pending_photo,
)
from services.poster_service import generate_poster_image
from services.reply_service import send_long_text, send_reply
from services.tool_command_service import build_meme_prompt, run_text_tool
from services.copywriting_service import extract_signals, optimize_copy
from services.y2k_service import generate_y2k_image
from utils.logger import setup_logging

logger = setup_logging()

from services.xiaopang_service import (
    XIAOPANG_OWNER_COMMANDS,
    build_system_prompt_with_xiaopang,
    handle_xiaopang_private_setting,
    is_xiaopang,
    maybe_hit_xiaopang_reminders,
    owner_xiaopang_command_reply,
    remember_xiaopang_identity,
    xiaopang_block_owner_command_for_private,
    xiaopang_blocklist_hit,
    xiaopang_fixed_privacy_reply,
    xiaopang_scope,
)

router = Router(name="private")
user_histories: dict[int, list] = {}


# ====== 私信功能区菜单：首页 + 三个二级菜单，不暴露任何隐藏管理命令 ======
#
# 二次收口原则：
# - 首页只展示 4 个大入口（做点图 / 好玩一下 / 小工具 / 怎么用），不直接铺 13 个按钮
# - 每个大入口点开后，展开对应的二级菜单（带「返回首页」）
# - 二级菜单里的具体功能按钮只显示简短用法/示例，不直接触发生成
# - /start /play 都进入首页；贝贝侧用更软的文案

# --- 首页文案 ---
HOME_MENU_HEADER = "✨ 私信功能区\n点下面的大入口看具体功能～"
HOME_MENU_FOOTER = "直接发文字、图片、语音都可以，我会自己判断要干嘛。"
PLAY_MENU_TEXT = f"{HOME_MENU_HEADER}\n\n{HOME_MENU_FOOTER}"

# 贝贝侧首页：P0 spec（关键词触发版）—— 不要按钮宫格、不要列命令、不要提示任何 / 命令。
# 自然关键词（如「在吗 / 抱抱 / 想你 / 烦死了 / 晚安 / 不想说」）会被关键词触发器接住；
# 普通文本走 gpt-5.5 自然陪伴。
BEIBEI_HOME_MENU_HEADER = "嗯，我在。"
BEIBEI_HOME_MENU_FOOTER = "你直接说就行。发文字、语音、图片、表情都可以。"
BEIBEI_PLAY_MENU_TEXT = f"{BEIBEI_HOME_MENU_HEADER}\n\n{BEIBEI_HOME_MENU_FOOTER}"

# /help 文案：明确说 /play /start 都会进首页
HELP_TEXT = (
    "私信功能区使用说明：\n"
    "• 发 /play 或 /start，会自动弹出 4 个大入口（做点图 / 好玩一下 / 小工具 / 怎么用）\n"
    "• 点大入口进入二级菜单；点具体功能会看到用法和示例\n"
    "• 直接发文字、图片、语音也可以聊天，我会自己判断\n\n"
    f"{PLAY_MENU_TEXT}"
)
BEIBEI_HELP_TEXT = BEIBEI_PLAY_MENU_TEXT


# --- 二级菜单标题 ---
SUB_MAKE_IMAGE_TITLE = "📸 做点图（图像创作 + 直接出图）"
SUB_FUN_TITLE = "🎀 好玩一下（轻娱乐）"
SUB_TOOLS_TITLE = "🧰 小工具（办公/文字）"

# 二级菜单底部说明：图像类提醒先发照片
SUB_MAKE_IMAGE_HINT = "—— 图像类记得先发一张照片再发命令；点按钮看具体用法"
SUB_FUN_HINT = "—— 点按钮看用法，或直接发对应命令"
SUB_TOOLS_HINT = "—— 点按钮看用法，或直接发对应命令"


# 「怎么用」按钮展开后的详细文案（保留原 HOW_TO_USE_TEXT 名以兼容外部引用）
HOW_TO_USE_TEXT = (
    "使用说明：\n"
    "1) 发 /start 或 /play → 看到 4 个大入口（做点图 / 好玩一下 / 小工具 / 怎么用）\n"
    "2) 进入二级菜单 → 点具体功能 → 看到「这个功能怎么用」的简短说明与示例\n"
    "3) 直接发命令也行：\n"
    "   📸 图像（先发一张照片）：/plog、/magnet（别名 /fridge）、/y2k、/poster（别名 /starposter）\n"
    "   🎨 直接出图：/img 描述、/meme 梗\n"
    "   🖼️ 图+文字改图：发照片配文「改图 描述」/「生图 描述」，或发照片后再发 /改图 描述\n"
    "   🎬 图生15秒视频：发照片配文「图生视频 描述」/「视频 描述」，或发照片后再发 /图生视频 描述\n"
    "   🍜 好玩：/eat 状态、/reply 对方的话、/eli5 概念\n"
    "   🧰 小工具：/excel 需求、/tldr 长文、/polish 文本\n\n"
    "发命令后，机器人会先回一条状态提示，再给最终结果；不会让你干等不知道在不在做。"
)


# 工具命令列表（小写、不带斜杠），用于路由分发
_TEXT_TOOLS = {"polish", "tldr", "eli5", "excel", "eat", "reply", "copyfix"}
_IMAGE_TOOLS = {"img", "meme"}
# 图像创作类（需要先发照片）：/plog, /magnet (+/fridge), /y2k, /poster (+/starposter),
# 「图 + 文字生图/改图」/imgedit（中文别名 /改图 /生图 /图生图，英文别名 /edit），
# 以及新增的「图生 15 秒视频」/i2v（中文别名 /图生视频 /视频 /生成视频 /图转视频）。
_PHOTO_TOOLS = {"plog", "magnet", "fridge", "y2k", "poster", "starposter", "imgedit", "i2v"}

# 图+文字生图/改图的命令别名 → 统一规范成 "imgedit"。
# 中文命令（/改图 等）Telegram 不会当成原生 command，但用户能直接输入文本，这里手动识别。
_IMGEDIT_ALIASES = {"imgedit", "edit", "改图", "生图", "图生图", "图加字", "图文生图"}

# 图生视频的命令别名 → 统一规范成 "i2v"。中文命令同样靠手动识别。
_I2V_ALIASES = {"i2v", "图生视频", "视频", "生成视频", "图转视频"}

# 文案优化的命令别名 → 统一规范成 "copyfix"。/文案优化 等中文命令 Telegram 不当原生 command，
# 但用户能直接输入文本，这里手动识别。
_COPYFIX_ALIASES = {"copyfix", "文案优化", "优化文案", "文案", "改文案"}


def _normalize_tool_name(name: str) -> str:
    """把命令别名规范成内部 tool 名。处理图+文字生图/改图、图生视频、文案优化的多别名。"""
    if name in _IMGEDIT_ALIASES:
        return "imgedit"
    if name in _I2V_ALIASES:
        return "i2v"
    if name in _COPYFIX_ALIASES:
        return "copyfix"
    return name


def _build_play_keyboard(*, owner: bool = False) -> InlineKeyboardMarkup:
    """首页大入口按钮：做点图 / 好玩一下 / 算一算 / 小工具 / 怎么用。

    所有面向所有人的 callback_data 都以 `home:` 开头，全部是安全的公开功能，
    不会暴露任何隐藏管理入口。

    owner=True 时（且必须是 owner 私聊，由调用方门禁保证）额外追加一行
    「🛠️ 控制台」，callback_data=`ownmenu:home`，复用 owner_menu 路由里的
    私信控制台（主脑 / GitHub / 神算子 等管理按钮）。owner_menu 回调里会再次复核
    OWNER_MENU_ENABLED + owner + 私聊，Business / 群 / 普通用户 / 贝贝都进不来。

    保留 `_build_play_keyboard` 名称是为了兼容外部引用（旧 smoke test）。
    """
    rows = [
        [InlineKeyboardButton(text="📸 做点图", callback_data="home:make_image")],
        [InlineKeyboardButton(text="🎀 好玩一下", callback_data="home:fun")],
        [InlineKeyboardButton(text="🔮 八字命理", callback_data="home:bazi")],
        [InlineKeyboardButton(text="🧰 小工具", callback_data="home:tools")],
        [InlineKeyboardButton(text="📖 怎么用", callback_data="home:howto")],
    ]
    if owner:
        # 仅 owner 私聊：把管理控制台挂在最下面，复用 ownmenu: 回调（自带门禁）
        rows.append([InlineKeyboardButton(text="🛠️ 控制台", callback_data="ownmenu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# 别名：方便阅读
def _build_home_keyboard(*, owner: bool = False) -> InlineKeyboardMarkup:
    return _build_play_keyboard(owner=owner)


def _row_back_home() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="⬅️ 返回首页", callback_data="home:back")]


def _build_make_image_keyboard() -> InlineKeyboardMarkup:
    """二级菜单：做点图。"""
    rows = [
        [InlineKeyboardButton(text="📔 生活小报机 /plog", callback_data="play:plog")],
        [InlineKeyboardButton(text="🧊 冰箱贴小画报 /magnet", callback_data="play:magnet")],
        [InlineKeyboardButton(text="💿 Y2K甜酷拼贴 /y2k", callback_data="play:y2k")],
        [InlineKeyboardButton(text="🌟 明星感海报 /poster", callback_data="play:poster")],
        [InlineKeyboardButton(text="🎨 AI画画搭子 /img", callback_data="play:img")],
        [InlineKeyboardButton(text="🖼️ 图+文字改图 /改图", callback_data="play:imgedit")],
        [InlineKeyboardButton(text="🎬 图生15秒视频 /图生视频", callback_data="play:i2v")],
        [InlineKeyboardButton(text="😂 表情包捣蛋鬼 /meme", callback_data="play:meme")],
        _row_back_home(),
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_fun_keyboard() -> InlineKeyboardMarkup:
    """二级菜单：好玩一下。"""
    rows = [
        [InlineKeyboardButton(text="🍜 今天吃点啥 /eat", callback_data="play:eat")],
        [InlineKeyboardButton(text="💬 神回复救场 /reply", callback_data="play:reply")],
        [InlineKeyboardButton(text="🧸 五岁也能懂 /eli5", callback_data="play:eli5")],
        _row_back_home(),
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_tools_keyboard() -> InlineKeyboardMarkup:
    """二级菜单：小工具。"""
    rows = [
        [InlineKeyboardButton(text="📊 表格公式小抄 /excel", callback_data="play:excel")],
        [InlineKeyboardButton(text="✂️ 长话变短 /tldr", callback_data="play:tldr")],
        [InlineKeyboardButton(text="🪄 润色变好听 /polish", callback_data="play:polish")],
        [InlineKeyboardButton(text="📝 文案优化 /文案优化", callback_data="play:copyfix")],
        _row_back_home(),
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_back_home_keyboard() -> InlineKeyboardMarkup:
    """单按钮：返回首页（用在 howto 与功能用法说明气泡里）。"""
    return InlineKeyboardMarkup(inline_keyboard=[_row_back_home()])


def _build_retry_keyboard() -> InlineKeyboardMarkup:
    """失败后给用户的「再试一次 / 返回首页」按钮。"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔁 再试一次", callback_data="home:retry_image"),
            InlineKeyboardButton(text="🏠 返回首页", callback_data="home:back"),
        ],
    ])


async def _send_text_with_keyboard(bot: Bot, chat_id: int, text: str, kb: InlineKeyboardMarkup) -> None:
    """带 InlineKeyboard 的状态/失败提示。失败吞掉，不抛。"""
    try:
        await bot.send_message(chat_id, text, reply_markup=kb)
    except Exception as e:
        logger.warning("send_text_with_keyboard failed | chat_id=%s | err=%s", chat_id, e)


# 按钮点击后展示的人味用法说明 + 示例。
_TOOL_HINTS = {
    "img": "🎨 AI画画搭子：发 /img 加你想画的内容\n例如：/img 一只穿西装的柴犬",
    "imgedit": (
        "🖼️ 图+文字改图：先发一张照片，配文写要怎么改\n"
        "例如：发照片 + 配文「改图 戴上墨镜」/「生图 换成夜景霓虹风」\n"
        "也可以发照片后再发：/改图 加一顶圣诞帽"
    ),
    "i2v": (
        "🎬 图生15秒视频：先发一张照片，配文写要它怎么动\n"
        "例如：发照片 + 配文「图生视频 镜头缓缓推近，头发随风轻轻飘」\n"
        "也可以发照片后再发：/图生视频 加点电影感运镜\n"
        "默认出 15 秒，渲染会比出图久一点，请稍等～"
    ),
    "meme": "😂 表情包捣蛋鬼：发 /meme 加你想做的梗\n例如：/meme 周一打工人崩溃",
    "plog": (
        "📔 生活小报机：先发一张照片，再发 /plog 加风格\n"
        "例如：/plog 可爱手账风 / /plog 甜点小报 / /plog 韩系治愈\n"
        "想要 Q 版分身手账照：/plog q版（或 /plog chibi、/plog 手账照）"
    ),
    "magnet": (
        "🧊 冰箱贴小画报：先发一张照片，再发 /magnet 加地点\n"
        "例如：/magnet 巴黎 2025.10\n"
        "（命令别名 /fridge 也可用）"
    ),
    "fridge": (
        "🧊 冰箱贴小画报（/magnet 的别名）：先发一张照片，再发 /fridge 加地点\n"
        "例如：/fridge 京都 2025.04"
    ),
    "y2k": (
        "💿 Y2K甜酷拼贴：先发一张照片，再发 /y2k\n"
        "例如：/y2k 粉色少女拼贴 / /y2k 韩系甜酷"
    ),
    "poster": (
        "🌟 明星感海报：先发一张照片，再发 /poster 加风格\n"
        "例如：/poster 甜酷复古 / /poster 赛博朋克霓虹\n"
        "（命令别名 /starposter 也可用）"
    ),
    "starposter": (
        "🌟 明星感海报（/poster 的别名）：先发一张照片，再发 /starposter 加风格\n"
        "例如：/starposter 甜酷复古"
    ),
    "eat": "🍜 今天吃点啥：发 /eat 加你的状态或偏好\n例如：/eat 今天降温想吃热的",
    "reply": "💬 神回复救场：发 /reply 加对方说的话\n例如：/reply 在干嘛？（会给你三种回复风格）",
    "eli5": "🧸 五岁也能懂：发 /eli5 加你想被解释的概念\n例如：/eli5 量子纠缠",
    "excel": "📊 表格公式小抄：发 /excel 加你的表格需求\n例如：/excel A 列求和但跳过空值",
    "polish": "🪄 润色变好听：发 /polish 加你想润色的文字\n例如：/polish 我今天没空",
    "tldr": "✂️ 长话变短：发 /tldr 加你要摘要的长文本，会给你 3-5 条要点",
    "copyfix": (
        "📝 文案优化：把广告/频道文案发给我（用 /文案优化 加内容，或直接发命令后再发文案）\n"
        "我会保留你的原意和语气，帮你改得更清楚、更好转化、更适合发 Telegram 频道。\n"
        "文案里的 emoji、表情、贴纸我都会读进去，不会把活泼的语气改没。\n"
        "例如：/文案优化 🔥今日上新！全场五折，手慢无～"
    ),
}


# ====== 命令反馈状态文案（P0）：先回一句让用户知道在做事 ======
STATUS_TEXT_TOOL = "收到，我在整理，马上给你。"
STATUS_IMAGE_TOOL = "收到，我在帮你出图，第一版会稍微等一下。"
STATUS_NEED_PHOTO_PLOG = "这一步要先发一张照片，我收到就继续。\n例如：/plog 可爱手账风 / /plog q版"
STATUS_NEED_PHOTO_MAGNET = "这一步要先发一张照片，我收到就继续。\n例如：/magnet 巴黎 2025.10（别名 /fridge 也行）"
STATUS_NEED_PHOTO_Y2K = "这一步要先发一张照片，我收到就继续。\n例如：/y2k 粉色少女拼贴"
STATUS_NEED_PHOTO_POSTER = "这一步要先发一张照片，我收到就继续。\n例如：/poster 甜酷复古（别名 /starposter 也行）"
STATUS_NEED_PHOTO_IMGEDIT = (
    "这一步要先发一张照片，我收到就继续。\n"
    "你可以直接发照片，配文写：改图 戴上墨镜（或 /改图 戴上墨镜）"
)
STATUS_IMGEDIT_NEED_INSTRUCTION = (
    "想怎么改这张图？发一句指令我就动手。\n"
    "例如：改图 换成夜景霓虹风 / 改图 加一顶圣诞帽"
)
STATUS_NEED_PHOTO_I2V = (
    "这一步要先发一张照片，我收到就继续。\n"
    "你可以直接发照片，配文写：图生视频 镜头缓缓推近（或 /图生视频 镜头缓缓推近）"
)
STATUS_I2V_NEED_INSTRUCTION = (
    "想让这张图怎么动？发一句描述我就开做。\n"
    "例如：图生视频 头发随风飘、镜头缓缓推近 / 图生视频 加点电影感运镜"
)
STATUS_I2V_TOOL = "收到，我在帮你把这张图做成视频，渲染会比出图久一点，请稍等～"

# 图像接口失败/超时时的诚实文案；告诉用户「照片/风格我都记着，发 /继续 或点按钮就再试一次」。
STATUS_IMAGE_FAILED_TPL = (
    "图像接口这一次{reason_clause}没出来😣 你的照片和风格我都记着，\n"
    "直接发 /继续 或点下面的「再试一次」就再发一遍。"
)


def _format_failed_text(reason: str | None = None) -> str:
    rc = ""
    if reason and reason.strip():
        rc = f"出了点问题（{reason.strip()}），"
    else:
        rc = "超时或出了点问题，"
    return STATUS_IMAGE_FAILED_TPL.format(reason_clause=rc)


STATUS_RETRY_NO_TASK = "暂时没有要重试的图片任务。先点一个风格或发命令开始吧～"
STATUS_RETRY_NEED_PHOTO = "要重试这张图片任务，得有照片。先发一张照片给我，再发 /继续。"
STATUS_RETRY_STARTING = "好，我用之前的照片和「{style}」再试一次。"


# ====== 风格子菜单：图片/娱乐功能按钮下挂的风格选择 ======
#
# 每个 tool 一份风格列表（中文短词）。点风格按钮只显示「示例命令 + 怎么用」，
# 不直接调用生成。callback_data 形如 stylepick:<tool>:<idx>，避免把中文塞进 callback。
#
# - need_photo=True 的功能：示例命令格式「先发照片 → /tool 风格」
# - need_photo=False（/img /meme）：示例命令带额外描述「/tool 风格 你的描述」
_STYLE_PRESETS: dict[str, dict] = {
    "plog": {
        "title": "📔 生活小报机｜选个风格",
        "need_photo": True,
        "example_extra": "",  # 风格词后面不强制再补描述
        "styles": [
            "奶油手账", "情侣纸条", "打卡日记", "旅行小册", "晴天底片",
            "夜闪直拍", "咖啡约会", "宅家软片", "朋友抓拍", "周末写真",
        ],
    },
    "magnet": {
        "title": "🧊 冰箱贴小画报｜选个风格",
        "need_photo": True,
        "example_extra": "",
        "styles": [
            "爆点封面", "甜酷拼贴", "杂志大片", "糖纸梦幻",
            "爱豆氛围", "霓虹卡片", "头像精修", "贴纸爆改",
        ],
    },
    "y2k": {
        "title": "💿 Y2K甜酷拼贴｜选个风格",
        "need_photo": True,
        "example_extra": "",
        "styles": [
            "千禧糖果", "镜面银潮", "辣妹碟片", "果冻闪粉", "网格未来",
            "心动电波", "像素甜酷", "荧光夜色", "泡泡塑感", "星光刊面",
        ],
    },
    "poster": {
        "title": "🌟 明星感海报｜选个风格",
        "need_photo": True,
        "example_extra": "",
        "styles": [
            "青春影报", "旅行海报", "应援海报", "港风海报", "法刊封面",
            "贴纸海报", "展陈海报", "校园海报", "纪念日海报", "单人主视觉",
            "生日主报", "明星封面",
        ],
    },
    "img": {
        "title": "🎨 AI画画搭子｜选个风格",
        "need_photo": False,
        "example_extra": " 一只小猫坐在窗边",
        "styles": [
            "自然好看", "软萌头像", "社交头像", "氛围写真", "种草分享",
            "情侣日常", "旅行出片", "聚会合拍", "城市底片", "高级刊面",
            "甜酷潮流", "拼贴相册", "宠物治愈", "夜闪大片", "星味写真",
        ],
    },
    "meme": {
        "title": "😂 表情包捣蛋鬼｜选个风格",
        "need_photo": False,
        "example_extra": " 我真的会谢",
        "styles": [
            "呆萌反应", "崩溃抓狂", "恋爱小剧场", "职场小剧场", "萌宠代言",
            "阴阳文学", "贴字表情", "变形喜剧", "尴尬现场", "甜妹撒娇",
        ],
    },
    # ----- 好玩一下：文本工具的风格子菜单（kind=text，复用 pending 机制走 _send_text_tool） -----
    "eat": {
        "kind": "text",
        "title": "🍜 今天吃点啥｜选个口味",
        "need_photo": False,
        "input_prompt": "状态/食材",
        "example_extra": " 今天降温想吃热的",
        "styles": [
            "省脑快选", "深夜治愈", "清淡不腻", "重口过瘾",
            "约会氛围", "打工人续命", "冰箱清仓", "甜品快乐",
        ],
    },
    "reply": {
        "kind": "text",
        "title": "💬 神回复救场｜选个语气",
        "need_photo": False,
        "input_prompt": "对方说的话",
        "example_extra": " 在干嘛？",
        "styles": [
            "高情商", "幽默化解", "专业坚定", "温柔拒绝", "阴阳不冒犯",
            "朋友口吻", "恋爱撒娇", "冷静止损", "客户沟通", "领导汇报",
        ],
    },
    "eli5": {
        "kind": "text",
        "title": "🧸 五岁也能懂｜选个讲法",
        "need_photo": False,
        "input_prompt": "概念",
        "example_extra": " 量子纠缠",
        "styles": [
            "五岁能懂", "生活比喻", "故事讲法", "同事速懂", "老板速览",
            "一句话版", "例子拆解", "反向解释",
        ],
    },
}


# 把 (tool, style_key) 反查需要的元信息封装在 helper 里
def _get_style_preset(tool: str) -> dict | None:
    return _STYLE_PRESETS.get(tool)


def _resolve_style_name(tool: str, idx: int) -> str | None:
    preset = _STYLE_PRESETS.get(tool)
    if not preset:
        return None
    styles = preset.get("styles", [])
    if 0 <= idx < len(styles):
        return styles[idx]
    return None


def _build_style_picker_keyboard(tool: str) -> InlineKeyboardMarkup | None:
    """风格子菜单键盘：每行 2 个风格按钮 + 底部返回按钮。

    callback_data 用 stylepick:<tool>:<idx> 索引方式，避免中文塞 callback。
    底部返回按钮按 tool 所属二级菜单分组：
      - 图片/图像娱乐（kind != text）回「做点图」
      - 文本（kind=text，eat/reply/eli5）回「好玩一下」
    """
    preset = _STYLE_PRESETS.get(tool)
    if not preset:
        return None
    styles = preset.get("styles", [])
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for idx, name in enumerate(styles):
        btn = InlineKeyboardButton(text=name, callback_data=f"stylepick:{tool}:{idx}")
        pair.append(btn)
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    # 底部双返回：按 kind 选不同的上级菜单
    if preset.get("kind") == "text":
        rows.append([
            InlineKeyboardButton(text="⬅️ 返回好玩一下", callback_data="home:fun"),
            InlineKeyboardButton(text="🏠 返回首页", callback_data="home:back"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(text="⬅️ 返回做点图", callback_data="home:make_image"),
            InlineKeyboardButton(text="🏠 返回首页", callback_data="home:back"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_back_to_style_keyboard(tool: str) -> InlineKeyboardMarkup:
    """风格说明气泡尾部按钮：返回该工具风格菜单 + 返回首页。"""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ 返回风格", callback_data=f"style:{tool}"),
        InlineKeyboardButton(text="🏠 返回首页", callback_data="home:back"),
    ]])


def _style_kind(tool: str) -> str:
    """返回 tool 的 kind：text（文本工具）或 image（图片/图像娱乐工具，默认）。"""
    preset = _STYLE_PRESETS.get(tool) or {}
    return preset.get("kind", "image")


def _style_usage_text(tool: str, style_name: str) -> str:
    """根据 tool 的 kind / need_photo 生成「下一步要发什么」的简短说明。

    与新版直接生成流程配套：
    - image + need_photo=True：选了风格但没有照片 → 提示「发照片后我直接继续」
    - image + need_photo=False（/img /meme）：选了风格但没描述 → 提示「把描述发给我就行」
    - text（/eat /reply /eli5）：选了风格但没输入 → 提示「把内容发给我就行」（用 input_prompt 描述输入）
    成功直接生成/执行时不再使用这段文案（runner 自己回状态）。
    """
    preset = _STYLE_PRESETS.get(tool) or {}
    kind = preset.get("kind", "image")
    need_photo = preset.get("need_photo", False)
    if kind == "text":
        # 文本工具：等用户下一条文本作为输入
        input_prompt = preset.get("input_prompt", "内容")
        # 不同 tool 的对外动作动词不一样
        verbs = {"eat": "推", "reply": "整理回复思路", "eli5": "讲清楚"}
        verb = verbs.get(tool, "整理")
        return (
            f"想走「{style_name}」的话，把{input_prompt}发给我就行。\n"
            f"你下一条文字我会按「{style_name}」帮你{verb}。"
        )
    if need_photo:
        return (
            f"这一步要先发一张照片。\n"
            f"我已经记住「{style_name}」了，你发照片后我直接继续。"
        )
    # /img /meme：等用户下一条描述
    return (
        f"想做「{style_name}」的话，把画面描述发给我就行。\n"
        f"你下一条文字我会当成描述，按「{style_name}」帮你出图。"
    )


# 直接生成时回给用户的「开始处理」状态文案
def _style_start_text(style_name: str, tool: str | None = None) -> str:
    """根据 tool 区分用语：图片类用「帮你出图」；文本类用「整理/推/讲清楚」。"""
    kind = _style_kind(tool) if tool else "image"
    if kind == "text":
        verbs = {"eat": "推", "reply": "整理回复思路", "eli5": "讲清楚"}
        verb = verbs.get(tool, "整理")
        return f"收到，我按「{style_name}」帮你{verb}，马上给你。"
    return f"收到，我按「{style_name}」帮你出图，第一版会稍微等一下。"


def get_history(user_id: int) -> list:
    return user_histories.get(user_id, [])


def save_history(user_id: int, user_content: str, assistant_reply: str):
    history = user_histories.get(user_id, [])
    history.append({"role": "user", "content": user_content})
    history.append({"role": "assistant", "content": assistant_reply})
    user_histories[user_id] = trim_history(history)


async def handle_owner_plan_command(text: str) -> str | None:
    raw = (text or "").strip()
    if raw.startswith("/新计划"):
        title = raw.replace("/新计划", "", 1).strip()
        if not title:
            return "用法：/新计划 计划标题"
        plan_id = await create_plan(title)
        return f"已创建计划 #{plan_id}：{title}"
    if raw.startswith("/计划列表"):
        return await list_plans()
    if raw.startswith("/计划详情"):
        arg = raw.replace("/计划详情", "", 1).strip()
        if not arg.isdigit():
            return "用法：/计划详情 计划ID"
        return await get_plan_detail(int(arg))
    if raw.startswith("/计划完成"):
        arg = raw.replace("/计划完成", "", 1).strip()
        if not arg.isdigit():
            return "用法：/计划完成 计划ID"
        return await update_plan_status(int(arg), "done")
    if raw.startswith("/计划暂停"):
        arg = raw.replace("/计划暂停", "", 1).strip()
        if not arg.isdigit():
            return "用法：/计划暂停 计划ID"
        return await update_plan_status(int(arg), "paused")
    if raw.startswith("/今天计划"):
        return await get_today_focus()
    if raw.startswith("/设置今日焦点"):
        content = raw.replace("/设置今日焦点", "", 1).strip()
        if not content:
            return "用法：/设置今日焦点 今天最重要的事"
        return await set_daily_focus(content)
    return None


def _strip_command(text: str, cmd_with_slash: str) -> str:
    """从消息文本里去掉命令前缀，保留参数。"""
    raw = (text or "").strip()
    # 兼容 /cmd@bot_username 形式
    head, _, rest = raw.partition(" ")
    if "@" in head:
        head_main = head.split("@", 1)[0]
    else:
        head_main = head
    if head_main.lower() == cmd_with_slash.lower():
        return rest.strip()
    # 兜底
    if raw.lower().startswith(cmd_with_slash.lower()):
        return raw[len(cmd_with_slash):].strip()
    return raw


async def _send_image_tool(bot: Bot, message: Message, tool: str, arg: str, *, silent_status: bool = False) -> None:
    """处理 /img 与 /meme：先回状态提示，再生成图片并发送；失败给自然中文。

    silent_status：调用方已发过风格感知文案时跳过通用 STATUS_IMAGE_TOOL，避免双状态。
    失败/超时时记 retry_task，让 /继续 能重试；这里把 (arg) 作为「风格」存进去（已含用户描述），
    /继续 时直接走 /img 或 /meme 同样的 arg。
    """
    arg = (arg or "").strip()
    user_id = message.from_user.id if message.from_user else 0
    chat_id = message.chat.id
    if not arg:
        await send_long_text(bot, chat_id, _TOOL_HINTS[tool])
        return

    if not silent_status:
        await send_long_text(bot, chat_id, STATUS_IMAGE_TOOL)
    await send_chat_action_safe(bot, chat_id, ChatAction.UPLOAD_PHOTO)

    if tool == "meme":
        prompt = build_meme_prompt(arg)
    else:
        prompt = arg

    retry_mark_started(user_id, tool, arg)
    try:
        result = await generate_image(prompt)
    except Exception as e:
        logger.exception("_send_image_tool generate crashed | tool=%s | uid=%s | err=%s", tool, user_id, e)
        result = {"ok": False, "error": "图像接口出错"}

    if not result.get("ok"):
        err = (result.get("error") or "").strip() or None
        retry_mark_failed(user_id, tool, arg, reason=err)
        await _send_text_with_keyboard(bot, chat_id, _format_failed_text(err), _build_retry_keyboard())
        return

    try:
        if result.get("url"):
            photo = URLInputFile(result["url"])
            await bot.send_photo(chat_id=chat_id, photo=photo)
            retry_clear_task(user_id)
            return
        if result.get("data"):
            photo = BufferedInputFile(result["data"], filename="image.png")
            await bot.send_photo(chat_id=chat_id, photo=photo)
            retry_clear_task(user_id)
            return
        # 返回里既没 url 也没 data：当失败处理
        retry_mark_failed(user_id, tool, arg, reason="图片为空")
        await _send_text_with_keyboard(bot, chat_id, _format_failed_text("图片为空"), _build_retry_keyboard())
    except Exception as e:
        # 落屏失败：保留 retry_task
        logger.warning("_send_image_tool send_photo failed | tool=%s | uid=%s | err=%s", tool, user_id, e)
        retry_mark_failed(user_id, tool, arg, reason="图片发送失败")
        await _send_text_with_keyboard(bot, chat_id, _format_failed_text("图片发送失败"), _build_retry_keyboard())


# 当底层只能做 text-to-image（image edit 接口不可用）时给用户的诚实说明。
# 不要假装能严格保脸/保姿势。
T2I_FALLBACK_NOTE = (
    "ℹ️ 提示：当前图像接口只能做风格参考、不支持严格图像编辑（img2img），\n"
    "所以无法保证原脸、原姿势完全不变；如果需要严格保留原图，需要切到支持图像编辑的接口。"
)


async def _maybe_send_fallback_note(bot: Bot, chat_id: int, result: dict) -> None:
    """如果底层降级到 text-to-image，附一句诚实说明。"""
    if result.get("fallback_to_text2image"):
        try:
            await send_long_text(bot, chat_id, T2I_FALLBACK_NOTE)
        except Exception:
            pass


async def _send_photo_to_chat(bot: Bot, chat_id: int, result: dict, fallback_msg: str) -> bool:
    """将 image_generation 返回的 url/data 发送为 Telegram 照片。发成功返 True。"""
    try:
        if result.get("url"):
            await bot.send_photo(chat_id=chat_id, photo=URLInputFile(result["url"]))
            return True
        if result.get("data"):
            await bot.send_photo(
                chat_id=chat_id,
                photo=BufferedInputFile(result["data"], filename="image.png"),
            )
            return True
    except Exception:
        pass
    await send_long_text(bot, chat_id, fallback_msg)
    return False


async def run_plog_for_user(bot: Bot, message: Message, style_arg: str, *, silent_status: bool = False) -> None:
    """执行 /plog：AI大头贴+生活小报。仅私信调用。

    使用最近一张待处理照片（由 media 路由存入 plog_service 缓存）作为参考图。
    成功落屏才清缓存；失败/超时则保留缓存 + 设置 retry_task，用户可发 /继续 重试。

    silent_status=True 表示调用方已经发过了风格感知的「开始处理」状态文案，
    本函数就不再额外发通用 STATUS_IMAGE_TOOL，避免连发两条状态消息。
    """
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0

    pending = plog_get_pending_photo(user_id)
    if not pending or not pending.file_path:
        await send_long_text(bot, chat_id, STATUS_NEED_PHOTO_PLOG)
        return

    if not silent_status:
        await send_long_text(bot, chat_id, STATUS_IMAGE_TOOL)
    await send_chat_action_safe(bot, chat_id, ChatAction.UPLOAD_PHOTO)

    is_xp = False
    try:
        is_xp = await is_xiaopang(message)
    except Exception:
        pass

    # 不消费照片，只读：失败后 /继续 能直接复用同一张图
    ref_path = pending.file_path
    caption = pending.caption

    # 记一条「正在做」便于「图片呢」状态查询
    retry_mark_started(user_id, "plog", style_arg or "")

    try:
        result = await generate_plog_image(
            style_raw=style_arg,
            reference_path=ref_path,
            beibei=is_xp,
            user_caption=caption,
        )
    except Exception as e:
        logger.exception("run_plog generate crashed | uid=%s | err=%s", user_id, e)
        result = {"ok": False, "error": "图像接口出错"}

    if not result.get("ok"):
        err = (result.get("error") or "").strip() or None
        retry_mark_failed(user_id, "plog", style_arg or "", reason=err)
        await _send_text_with_keyboard(bot, chat_id, _format_failed_text(err), _build_retry_keyboard())
        # 不清照片、不清 pending_style，让 /继续 直接重试
        logger.info("run_plog failed kept-photo | uid=%s | err=%s", user_id, err)
        return

    sent = await _send_photo_to_chat(bot, chat_id, result, "图片生成完了但发不出去，等下再试")
    if sent:
        await _maybe_send_fallback_note(bot, chat_id, result)
        plog_clear_pending_photo(user_id)
        retry_clear_task(user_id)
        logger.info("run_plog success cleaned-photo | uid=%s | style=%s", user_id, style_arg)
    else:
        # 落屏失败 ≠ 生成失败：照片可能已生成但发送失败，给重试机会
        retry_mark_failed(user_id, "plog", style_arg or "", reason="图片发送失败")
        await _send_text_with_keyboard(bot, chat_id, _format_failed_text("图片发送失败"), _build_retry_keyboard())
        logger.warning("run_plog send_photo failed but generation ok | uid=%s", user_id)


async def run_magnet_for_user(bot: Bot, message: Message, raw_arg: str, *, silent_status: bool = False) -> None:
    """执行 /magnet：AI冰箱贴海报。仅私信调用。与 /plog 是两个完全独立的功能。
    复用同一个“最近一张照片”缓存（与 /plog 共享），但 prompt / 菜单名 / 提示都独立。
    silent_status：见 run_plog_for_user。
    成功才清照片；失败保留照片 + 记 retry_task，便于 /继续 重试。
    """
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0

    pending = plog_get_pending_photo(user_id)
    if not pending or not pending.file_path:
        await send_long_text(bot, chat_id, STATUS_NEED_PHOTO_MAGNET)
        return

    if not silent_status:
        await send_long_text(bot, chat_id, STATUS_IMAGE_TOOL)
    await send_chat_action_safe(bot, chat_id, ChatAction.UPLOAD_PHOTO)

    is_xp = False
    try:
        is_xp = await is_xiaopang(message)
    except Exception:
        pass

    ref_path = pending.file_path
    caption = pending.caption

    retry_mark_started(user_id, "magnet", raw_arg or "")
    try:
        result = await generate_magnet_image(
            raw_arg=raw_arg,
            reference_path=ref_path,
            beibei=is_xp,
            user_caption=caption,
        )
    except Exception as e:
        logger.exception("run_magnet generate crashed | uid=%s | err=%s", user_id, e)
        result = {"ok": False, "error": "图像接口出错"}

    if not result.get("ok"):
        err = (result.get("error") or "").strip() or None
        retry_mark_failed(user_id, "magnet", raw_arg or "", reason=err)
        await _send_text_with_keyboard(bot, chat_id, _format_failed_text(err), _build_retry_keyboard())
        logger.info("run_magnet failed kept-photo | uid=%s | err=%s", user_id, err)
        return

    sent = await _send_photo_to_chat(bot, chat_id, result, "图片生成完了但发不出去，等下再试")
    if sent:
        await _maybe_send_fallback_note(bot, chat_id, result)
        plog_clear_pending_photo(user_id)
        retry_clear_task(user_id)
    else:
        retry_mark_failed(user_id, "magnet", raw_arg or "", reason="图片发送失败")
        await _send_text_with_keyboard(bot, chat_id, _format_failed_text("图片发送失败"), _build_retry_keyboard())


async def run_y2k_for_user(bot: Bot, message: Message, raw_arg: str, *, silent_status: bool = False) -> None:
    """执行 /y2k：Y2K 拼贴海报。仅私信。复用最近一张照片缓存。
    成功才清照片；失败保留照片 + 记 retry_task。
    silent_status：见 run_plog_for_user。
    """
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0

    pending = plog_get_pending_photo(user_id)
    if not pending or not pending.file_path:
        await send_long_text(bot, chat_id, STATUS_NEED_PHOTO_Y2K)
        return

    if not silent_status:
        await send_long_text(bot, chat_id, STATUS_IMAGE_TOOL)
    await send_chat_action_safe(bot, chat_id, ChatAction.UPLOAD_PHOTO)

    is_xp = False
    try:
        is_xp = await is_xiaopang(message)
    except Exception:
        pass

    ref_path = pending.file_path
    caption = pending.caption

    retry_mark_started(user_id, "y2k", raw_arg or "")
    try:
        result = await generate_y2k_image(
            raw_arg=raw_arg,
            reference_path=ref_path,
            beibei=is_xp,
            user_caption=caption,
        )
    except Exception as e:
        logger.exception("run_y2k generate crashed | uid=%s | err=%s", user_id, e)
        result = {"ok": False, "error": "图像接口出错"}

    if not result.get("ok"):
        err = (result.get("error") or "").strip() or None
        retry_mark_failed(user_id, "y2k", raw_arg or "", reason=err)
        await _send_text_with_keyboard(bot, chat_id, _format_failed_text(err), _build_retry_keyboard())
        logger.info("run_y2k failed kept-photo | uid=%s | err=%s", user_id, err)
        return

    sent = await _send_photo_to_chat(bot, chat_id, result, "图片生成完了但发不出去，等下再试")
    if sent:
        await _maybe_send_fallback_note(bot, chat_id, result)
        plog_clear_pending_photo(user_id)
        retry_clear_task(user_id)
    else:
        retry_mark_failed(user_id, "y2k", raw_arg or "", reason="图片发送失败")
        await _send_text_with_keyboard(bot, chat_id, _format_failed_text("图片发送失败"), _build_retry_keyboard())


async def run_poster_for_user(bot: Bot, message: Message, raw_arg: str, *, silent_status: bool = False) -> None:
    """执行 /poster /starposter：明星拼贴海报。仅私信。复用最近一张照片缓存。
    成功才清照片；失败保留照片 + 记 retry_task。
    silent_status：见 run_plog_for_user。
    """
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0

    pending = plog_get_pending_photo(user_id)
    if not pending or not pending.file_path:
        await send_long_text(bot, chat_id, STATUS_NEED_PHOTO_POSTER)
        return

    if not silent_status:
        await send_long_text(bot, chat_id, STATUS_IMAGE_TOOL)
    await send_chat_action_safe(bot, chat_id, ChatAction.UPLOAD_PHOTO)

    is_xp = False
    try:
        is_xp = await is_xiaopang(message)
    except Exception:
        pass

    ref_path = pending.file_path
    caption = pending.caption

    retry_mark_started(user_id, "poster", raw_arg or "")
    try:
        result = await generate_poster_image(
            raw_arg=raw_arg,
            reference_path=ref_path,
            beibei=is_xp,
            user_caption=caption,
        )
    except Exception as e:
        logger.exception("run_poster generate crashed | uid=%s | err=%s", user_id, e)
        result = {"ok": False, "error": "图像接口出错"}

    if not result.get("ok"):
        err = (result.get("error") or "").strip() or None
        retry_mark_failed(user_id, "poster", raw_arg or "", reason=err)
        await _send_text_with_keyboard(bot, chat_id, _format_failed_text(err), _build_retry_keyboard())
        logger.info("run_poster failed kept-photo | uid=%s | err=%s", user_id, err)
        return

    sent = await _send_photo_to_chat(bot, chat_id, result, "图片生成完了但发不出去，等下再试")
    if sent:
        await _maybe_send_fallback_note(bot, chat_id, result)
        plog_clear_pending_photo(user_id)
        retry_clear_task(user_id)
    else:
        retry_mark_failed(user_id, "poster", raw_arg or "", reason="图片发送失败")
        await _send_text_with_keyboard(bot, chat_id, _format_failed_text("图片发送失败"), _build_retry_keyboard())


async def run_imgedit_for_user(bot: Bot, message: Message, instruction: str, *, silent_status: bool = False) -> None:
    """执行「图 + 文字生图/改图」（/改图 /生图 /图生图 /imgedit）。仅私信。

    用最近一张待处理照片（media 路由缓存）作参考图，按用户指令调用 IMAGE_TEXT_MODEL
    （默认 flux.1-kontext-pro）做图像编辑；中转不支持 image edit 时由底层服务降级到
    纯文字生图（仍用 IMAGE_TEXT_MODEL）并附诚实说明。

    成功落屏才清照片；失败/超时保留照片 + 记 retry_task，用户可发 /继续 重试。
    silent_status：见 run_plog_for_user。
    """
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0

    instruction = (instruction or "").strip()

    pending = plog_get_pending_photo(user_id)
    if not pending or not pending.file_path:
        await send_long_text(bot, chat_id, STATUS_NEED_PHOTO_IMGEDIT)
        return
    if not instruction:
        await send_long_text(bot, chat_id, STATUS_IMGEDIT_NEED_INSTRUCTION)
        return

    if not silent_status:
        await send_long_text(bot, chat_id, STATUS_IMAGE_TOOL)
    await send_chat_action_safe(bot, chat_id, ChatAction.UPLOAD_PHOTO)

    ref_path = pending.file_path

    retry_mark_started(user_id, "imgedit", instruction)
    try:
        result = await generate_image_with_instruction(
            instruction=instruction,
            reference_path=ref_path,
        )
    except Exception as e:
        logger.exception("run_imgedit generate crashed | uid=%s | err=%s", user_id, e)
        result = {"ok": False, "error": "图像接口出错"}

    if not result.get("ok"):
        err = (result.get("error") or "").strip() or None
        retry_mark_failed(user_id, "imgedit", instruction, reason=err)
        await _send_text_with_keyboard(bot, chat_id, _format_failed_text(err), _build_retry_keyboard())
        logger.info("run_imgedit failed kept-photo | uid=%s | err=%s", user_id, err)
        return

    sent = await _send_photo_to_chat(bot, chat_id, result, "图片生成完了但发不出去，等下再试")
    if sent:
        await _maybe_send_fallback_note(bot, chat_id, result)
        plog_clear_pending_photo(user_id)
        retry_clear_task(user_id)
    else:
        retry_mark_failed(user_id, "imgedit", instruction, reason="图片发送失败")
        await _send_text_with_keyboard(bot, chat_id, _format_failed_text("图片发送失败"), _build_retry_keyboard())


# 图生视频失败/超时时的诚实文案：照片与描述都记着，可发 /继续 或点「再试一次」重试。
STATUS_VIDEO_FAILED_TPL = (
    "视频接口这一次{reason_clause}没出来😣 你的照片和描述我都记着，\n"
    "直接发 /继续 或点下面的「再试一次」就再发一遍。"
)


def _format_video_failed_text(reason: str | None = None) -> str:
    if reason and reason.strip():
        rc = f"出了点问题（{reason.strip()}），"
    else:
        rc = "超时或出了点问题，"
    return STATUS_VIDEO_FAILED_TPL.format(reason_clause=rc)


async def _send_video_to_chat(bot: Bot, chat_id: int, result: dict, fallback_msg: str) -> bool:
    """把视频服务返回的 url/data 发成 Telegram 视频。发成功返 True，否则发 fallback 文案返 False。"""
    try:
        if result.get("url"):
            await bot.send_video(chat_id=chat_id, video=URLInputFile(result["url"]))
            return True
        if result.get("data"):
            await bot.send_video(
                chat_id=chat_id,
                video=BufferedInputFile(result["data"], filename="video.mp4"),
            )
            return True
    except Exception as e:
        logger.warning("send_video failed | chat_id=%s | err=%s", chat_id, e)
        # 视频发送失败时尝试退化成 document（部分客户端/编码更稳）
        try:
            if result.get("data"):
                await bot.send_document(
                    chat_id=chat_id,
                    document=BufferedInputFile(result["data"], filename="video.mp4"),
                )
                return True
        except Exception as e2:
            logger.warning("send_document fallback failed | chat_id=%s | err=%s", chat_id, e2)
    await send_long_text(bot, chat_id, fallback_msg)
    return False


async def run_i2v_for_user(bot: Bot, message: Message, instruction: str, *, silent_status: bool = False) -> None:
    """执行「图生 15 秒视频」（/图生视频 /视频 /生成视频 /图转视频 /i2v）。仅私信。

    用最近一张待处理照片（media 路由缓存）作参考图，按用户描述调用 I2V_VIDEO_MODEL
    （默认 wan2.6-i2v-flash）出一段默认 15 秒的短视频。上游不支持视频接口时由底层
    服务返回 ok=False + 简短中文文案，这里走失败分支，不暴露后端细节。

    成功落屏才清照片；失败/超时保留照片 + 记 retry_task，用户可发 /继续 重试。
    silent_status：见 run_plog_for_user。
    """
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0

    instruction = (instruction or "").strip()

    pending = plog_get_pending_photo(user_id)
    if not pending or not pending.file_path:
        await send_long_text(bot, chat_id, STATUS_NEED_PHOTO_I2V)
        return
    if not instruction:
        await send_long_text(bot, chat_id, STATUS_I2V_NEED_INSTRUCTION)
        return

    if not silent_status:
        await send_long_text(bot, chat_id, STATUS_I2V_TOOL)
    await send_chat_action_safe(bot, chat_id, ChatAction.UPLOAD_VIDEO)

    ref_path = pending.file_path

    retry_mark_started(user_id, "i2v", instruction)
    try:
        result = await generate_video_from_image(
            prompt=instruction,
            reference_path=ref_path,
        )
    except Exception as e:
        logger.exception("run_i2v generate crashed | uid=%s | err=%s", user_id, e)
        result = {"ok": False, "error": "视频接口出错"}

    if not result.get("ok"):
        err = (result.get("error") or "").strip() or None
        retry_mark_failed(user_id, "i2v", instruction, reason=err)
        await _send_text_with_keyboard(bot, chat_id, _format_video_failed_text(err), _build_retry_keyboard())
        logger.info("run_i2v failed kept-photo | uid=%s", user_id)
        return

    sent = await _send_video_to_chat(bot, chat_id, result, "视频生成完了但发不出去，等下再试")
    if sent:
        plog_clear_pending_photo(user_id)
        retry_clear_task(user_id)
    else:
        retry_mark_failed(user_id, "i2v", instruction, reason="视频发送失败")
        await _send_text_with_keyboard(bot, chat_id, _format_video_failed_text("视频发送失败"), _build_retry_keyboard())


async def _send_text_tool(bot: Bot, message: Message, tool: str, arg: str, *, silent_status: bool = False) -> None:
    """处理文字类工具命令。先回状态提示，再给最终结果。

    silent_status：调用方已发过风格感知文案时跳过通用 STATUS_TEXT_TOOL。
    copyfix（文案优化）走独立 service，并把文案里的 emoji/自定义 emoji/贴纸占位等
    表达信号拼进 prompt；其它工具继续走 run_text_tool。
    """
    arg = (arg or "").strip()
    if not arg:
        # copyfix 无内容：除了发用法说明，还登记一个 pending，
        # 这样用户接着发文案（或先发贴纸/GIF）时能被识别为「文案优化」流程。
        if tool == "copyfix" and message.from_user:
            set_pending_style(message.from_user.id, "copyfix", "频道发布")
        await send_long_text(bot, message.chat.id, _TOOL_HINTS[tool])
        return
    # 用户明确提供了参数（显式调用），清除之前设置的 pending 状态
    if tool == "copyfix" and message.from_user:
        clear_pending_style(message.from_user.id)
    # P0：先回一句状态提示（除非调用方已发过风格感知文案）
    if not silent_status:
        await send_long_text(bot, message.chat.id, STATUS_TEXT_TOOL)
    await send_chat_action_safe(bot, message.chat.id, ChatAction.TYPING)
    if tool == "copyfix":
        signals = extract_signals(arg, entities=getattr(message, "entities", None))
        text = await optimize_copy(arg, signals)
    else:
        text = await run_text_tool(tool, arg)
    await send_long_text(bot, message.chat.id, text)


_PENDING_TEXT_CONSUMABLE_TOOLS = {"img", "meme", "eat", "reply", "eli5", "copyfix"}


async def _maybe_consume_pending_for_text(bot: Bot, message: Message, text: str) -> bool:
    """如果该用户有 /img /meme /eat /reply /eli5 的 pending 风格，且这条文本不是命令，
    就直接把文本当作输入触发。

    - img/meme：调 _send_image_tool（生成图片）
    - eat/reply/eli5：调 _send_text_tool（生成文字结果）
    返回 True 表示已经消费 + 触发，调用方应 return；否则返回 False。
    """
    if not message or not message.from_user:
        return False
    raw = (text or "").strip()
    if not raw:
        return False
    # 命令自己就是显式调用，让用户重选/重写：不消费 pending
    if raw.startswith("/"):
        return False
    user_id = message.from_user.id
    pending = get_pending_style(user_id)
    if not pending:
        return False
    if pending.tool not in _PENDING_TEXT_CONSUMABLE_TOOLS:
        return False
    # 消费 pending（不论成功失败都消掉，避免误反复触发）
    consume_pending_style(user_id)
    chat_id = message.chat.id
    style = pending.style
    tool = pending.tool

    # copyfix（文案优化）：style 只是占位标记，不拼进文案；把这条文本当作待优化文案直接处理。
    if tool == "copyfix":
        try:
            await send_long_text(bot, chat_id, STATUS_TEXT_TOOL)
        except Exception:
            pass
        try:
            await _send_text_tool(bot, message, "copyfix", raw, silent_status=True)
        except Exception:
            pass
        return True

    # 风格感知状态提示（按 tool 分图片/文本两种）
    try:
        await send_long_text(bot, chat_id, _style_start_text(style, tool))
    except Exception:
        pass
    # 把文本当作输入：组装成 "<style> <user_text>" 作为 tool 的 arg
    arg = f"{style} {raw}".strip()
    try:
        if _style_kind(tool) == "text":
            await _send_text_tool(bot, message, tool, arg, silent_status=True)
        else:
            await _send_image_tool(bot, message, tool, arg, silent_status=True)
    except Exception:
        # 下游已有兜底；这层只是 belt-and-braces
        pass
    return True


def _detect_tool_command(text: str) -> tuple[str | None, str]:
    """识别工具命令；返回 (tool_name 或 None, 参数)。仅匹配私信菜单工具。"""
    raw = (text or "").lstrip()
    if not raw.startswith("/"):
        return None, ""
    head, _, rest = raw.partition(" ")
    cmd = head.split("@", 1)[0].lower()
    if not cmd.startswith("/"):
        return None, ""
    name = _normalize_tool_name(cmd[1:])
    if name in _TEXT_TOOLS or name in _IMAGE_TOOLS or name in _PHOTO_TOOLS:
        return name, rest.strip()
    return None, ""


async def _send_entertainment_menu(message: Message, *, greeting: str | None = None) -> None:
    """发送私信功能区首页（4 个大入口）。

    greeting 是放在菜单前面的一句欢迎语，可选；不传时只发菜单本体。
    贝贝侧使用更温柔的文案。Business 模式由调用方负责过滤。
    """
    is_xp = False
    try:
        is_xp = await is_xiaopang(message)
    except Exception:
        pass
    body = BEIBEI_PLAY_MENU_TEXT if is_xp else PLAY_MENU_TEXT
    text = f"{greeting.strip()}\n\n{body}" if (greeting and greeting.strip()) else body
    # 仅 owner 私聊（且非贝贝）追加控制台入口；其余用户只看安全公开功能
    owner = (not is_xp) and is_owner(message)
    await message.answer(text, reply_markup=_build_home_keyboard(owner=owner))


async def _retry_last_image_task(bot: Bot, message: Message) -> bool:
    """根据 retry_task 重跑上一个图像生成任务。返回 True 表示已尝试触发，
    False 表示没有任务可重试（调用方应给「没任务可重试」提示）。"""
    if not message or not message.from_user:
        return False
    user_id = message.from_user.id
    chat_id = message.chat.id
    task = retry_get_task(user_id)
    if not task:
        return False
    tool = task.tool
    style = task.style or ""

    # need_photo 类工具：要保证 plog 缓存里还有照片
    if tool in {"plog", "magnet", "y2k", "poster", "imgedit", "i2v"}:
        cached = plog_get_pending_photo(user_id)
        if not cached or not cached.file_path:
            try:
                await bot.send_message(chat_id, STATUS_RETRY_NEED_PHOTO)
            except Exception:
                pass
            return True
        # 风格感知状态
        try:
            await bot.send_message(chat_id, STATUS_RETRY_STARTING.format(style=style or tool))
        except Exception:
            pass
        if tool == "plog":
            await run_plog_for_user(bot, message, style, silent_status=True)
        elif tool == "magnet":
            await run_magnet_for_user(bot, message, style, silent_status=True)
        elif tool == "y2k":
            await run_y2k_for_user(bot, message, style, silent_status=True)
        elif tool == "poster":
            await run_poster_for_user(bot, message, style, silent_status=True)
        elif tool == "imgedit":
            await run_imgedit_for_user(bot, message, style, silent_status=True)
        elif tool == "i2v":
            await run_i2v_for_user(bot, message, style, silent_status=True)
        return True

    # /img /meme：不需要照片，arg 在 retry_task.style 里
    if tool in {"img", "meme"}:
        try:
            await bot.send_message(chat_id, STATUS_RETRY_STARTING.format(style=style or tool))
        except Exception:
            pass
        await _send_image_tool(bot, message, tool, style or "", silent_status=True)
        return True

    return False


def _looks_like_image_status_question(text: str) -> bool:
    """简单识别「图片呢 / 图呢 / 图片 / 出图了没 / 怎么没图 / 图片出来了吗」类问题。"""
    if not text:
        return False
    s = text.strip().replace("？", "?").replace(" ", "").lower()
    if not s:
        return False
    # 太长（>30）就不当成简短的状态询问
    if len(s) > 30:
        return False
    patterns = (
        "图片呢", "图呢", "图片?", "图?",
        "我的图", "我的图片", "图片到了吗", "图片出来了吗",
        "图出来了吗", "出图了没", "出图了吗", "怎么没图",
        "怎么没出图", "图哪去了", "图片哪去了",
        "图还有吗", "图没了", "图片没了",
    )
    for p in patterns:
        if p in s:
            return True
    return False


@router.message(Command("继续"))
async def retry_command_handler(message: Message, bot: Bot):
    """/继续：基于 pending_retry_service 重跑上一个图像任务。仅 private。"""
    if get_chat_mode(message) != "private":
        return
    if should_skip_message(message):
        return
    ok = await _retry_last_image_task(bot, message)
    if not ok:
        await send_long_text(bot, message.chat.id, STATUS_RETRY_NO_TASK)


@router.message(CommandStart())
async def start_handler(message: Message):
    if get_chat_mode(message) != "private":
        return
    if should_skip_message(message):
        return
    # 贝贝侧 P0：不要按钮宫格、不要列功能；只一句关系唤醒（阿树开场池）
    if await is_xiaopang(message):
        try:
            opening = atree_pick_safe_reply("opening")
        except Exception:
            opening = "嗯，我在。"
        await message.answer(sanitize_visible_reply(opening))
        return
    # 普通用户：人味欢迎 + 首页 4 个大入口（保留，工具入口）
    await _send_entertainment_menu(
        message,
        greeting="你好呀～我是小林子。挑一个想做的点一下就行～",
    )


@router.message(Command("play"))
async def play_handler(message: Message):
    if get_chat_mode(message) != "private":
        return
    if should_skip_message(message):
        return
    # 贝贝侧 P0：/play 不弹娱乐菜单，仍给关系唤醒（阿树开场池）
    if await is_xiaopang(message):
        try:
            opening = atree_pick_safe_reply("opening")
        except Exception:
            opening = "嗯，我在。"
        await message.answer(sanitize_visible_reply(opening))
        return
    # 普通用户：娱乐功能菜单本体
    await _send_entertainment_menu(message)


@router.message(Command("help"))
async def help_handler(message: Message):
    if get_chat_mode(message) != "private":
        return
    if should_skip_message(message):
        return
    # 贝贝侧：温柔，不出现隐藏管理词汇
    if await is_xiaopang(message):
        await send_long_text(message.bot, message.chat.id, BEIBEI_HELP_TEXT)
        return
    # /help 文案不暴露任何隐藏管理命令；明确告知 /play /start 会自动弹娱乐菜单
    await send_long_text(message.bot, message.chat.id, HELP_TEXT)


async def _safe_cb_ack(query: CallbackQuery, text: str | None = None) -> None:
    try:
        if text:
            await query.answer(text)
        else:
            await query.answer()
    except Exception:
        pass


def _cb_is_owner(query: CallbackQuery) -> bool:
    """回调里复核 owner：用 from_user + 回调所在私聊 chat 拼一个最小 message-like 探针。

    仅当 chat 是 private（非 business / 群）且 from_user 命中 owner 时返回 True。
    Business 回调的 message.chat.type 仍是 private，但 business_connection_id 非空，
    这里据此区分；普通用户 / 贝贝命中不了 OWNER_USER_IDS / OWNER_USERNAMES。
    """
    msg = query.message
    user = query.from_user
    if not msg or not user:
        return False
    probe = SimpleNamespace(
        from_user=user,
        chat=msg.chat,
        business_connection_id=getattr(msg, "business_connection_id", None),
    )
    if get_chat_mode(probe) != "private":
        return False
    return is_owner(probe)


@router.callback_query(F.data.startswith("home:"))
async def home_callback(query: CallbackQuery, state: FSMContext):
    """首页大入口 + 返回首页 + howto + 八字命理 的统一回调。

    home:make_image / home:fun / home:tools  →  弹对应二级菜单
    home:howto                               →  弹「怎么用」详细文案
    home:bazi                                →  进入八字命理 FSM（安全公开功能）
    home:back                                →  回到首页（owner 私聊额外带控制台入口）
    """
    data = query.data or ""
    key = data.split(":", 1)[1] if ":" in data else ""
    msg = query.message
    if not msg:
        await _safe_cb_ack(query)
        return

    if key == "make_image":
        await _safe_cb_ack(query)
        await msg.answer(
            f"{SUB_MAKE_IMAGE_TITLE}\n{SUB_MAKE_IMAGE_HINT}",
            reply_markup=_build_make_image_keyboard(),
        )
        return
    if key == "fun":
        await _safe_cb_ack(query)
        await msg.answer(
            f"{SUB_FUN_TITLE}\n{SUB_FUN_HINT}",
            reply_markup=_build_fun_keyboard(),
        )
        return
    if key == "tools":
        await _safe_cb_ack(query)
        await msg.answer(
            f"{SUB_TOOLS_TITLE}\n{SUB_TOOLS_HINT}",
            reply_markup=_build_tools_keyboard(),
        )
        return
    if key == "howto":
        await _safe_cb_ack(query)
        await msg.answer(HOW_TO_USE_TEXT, reply_markup=_build_back_home_keyboard())
        return
    if key == "bazi":
        # 🔮 八字命理：安全公开功能，复用 mingli FSM（任何用户私聊都可用）
        await _safe_cb_ack(query)
        try:
            from routers.mingli import BaziStates, _gender_kb
            await state.clear()
            await state.set_state(BaziStates.ask_gender)
            await msg.answer(
                "🔮 *八字命理解读*\n\n请先告诉我你的性别：",
                parse_mode="Markdown",
                reply_markup=_gender_kb(),
            )
        except Exception as e:
            logger.warning("home:bazi start failed | err=%s", e)
            await msg.answer("🔮 八字命理暂时打不开，稍后再试。", reply_markup=_build_back_home_keyboard())
        return
    if key == "back":
        await _safe_cb_ack(query)
        # 回首页：用首页文案 + 首页键盘（不再加 greeting，避免重复）
        is_xp = False
        try:
            is_xp = await is_xiaopang(query.message)
        except Exception:
            pass
        body = BEIBEI_PLAY_MENU_TEXT if is_xp else PLAY_MENU_TEXT
        owner = (not is_xp) and _cb_is_owner(query)
        await msg.answer(body, reply_markup=_build_home_keyboard(owner=owner))
        return
    if key == "retry_image":
        # 「🔁 再试一次」：从 retry_task 取出上次失败的 tool+style，重跑一次
        await _safe_cb_ack(query)
        bot = (
            getattr(query, "bot", None)
            or getattr(msg, "bot", None)
        )
        # 把 callback 消息当作上下文 message：from_user / chat 都来自它
        ctx = SimpleNamespace(
            chat=msg.chat,
            from_user=query.from_user,
            bot=bot,
        )
        ok = await _retry_last_image_task(bot, ctx)
        if not ok:
            try:
                await msg.answer(STATUS_RETRY_NO_TASK)
            except Exception:
                pass
        return

    # 未知 home:* 键
    await _safe_cb_ack(query, "未知操作")


@router.callback_query(F.data.startswith("play:"))
async def play_callback(query: CallbackQuery):
    """二级菜单里具体功能按钮的回调。

    - 命中 _STYLE_PRESETS（图片/娱乐图像 6 个功能）→ 打开「风格子菜单」
    - 其它（fridge / starposter / 文本工具的 hint 入口）→ 显示用法说明
    所有路径都不直接调用生成接口。
    """
    data = query.data or ""
    key = data.split(":", 1)[1] if ":" in data else ""

    # 兼容旧路径：play:howto 仍然有效，等价 home:howto
    if key == "howto":
        await _safe_cb_ack(query)
        if query.message:
            await query.message.answer(HOW_TO_USE_TEXT, reply_markup=_build_back_home_keyboard())
        return

    # 命中风格预设：弹风格子菜单
    if key in _STYLE_PRESETS:
        preset = _STYLE_PRESETS[key]
        await _safe_cb_ack(query)
        if query.message:
            kb = _build_style_picker_keyboard(key)
            await query.message.answer(preset["title"], reply_markup=kb)
        return

    hint = _TOOL_HINTS.get(key)
    if not hint:
        await _safe_cb_ack(query, "未知操作")
        return

    await _safe_cb_ack(query)
    if query.message:
        await query.message.answer(hint, reply_markup=_build_back_home_keyboard())


@router.callback_query(F.data.startswith("style:"))
async def style_menu_callback(query: CallbackQuery):
    """「⬅️ 返回风格」按钮：从风格说明气泡回到该工具的风格子菜单。"""
    data = query.data or ""
    tool = data.split(":", 1)[1] if ":" in data else ""
    preset = _STYLE_PRESETS.get(tool)
    if not preset:
        await _safe_cb_ack(query, "未知操作")
        return
    await _safe_cb_ack(query)
    if query.message:
        await query.message.answer(preset["title"], reply_markup=_build_style_picker_keyboard(tool))


@router.callback_query(F.data.startswith("stylepick:"))
async def style_pick_callback(query: CallbackQuery):
    """点风格按钮：

    - need_photo=True 工具：
      * 已有最近照片缓存 → 直接调对应 runner 生成（带风格感知状态文案）
      * 没有缓存 → set pending，提示「我记住了风格，发照片后我继续」
    - need_photo=False（/img /meme）：set pending，提示「下一条文字会作为描述」
    所有路径都不在按钮点击里直接对外暴露技术错误；失败由 runner 给自然中文。
    """
    data = query.data or ""
    parts = data.split(":", 2)
    if len(parts) < 3:
        await _safe_cb_ack(query, "未知操作")
        return
    _, tool, idx_str = parts
    try:
        idx = int(idx_str)
    except ValueError:
        await _safe_cb_ack(query, "未知操作")
        return
    name = _resolve_style_name(tool, idx)
    if not name:
        await _safe_cb_ack(query, "未知操作")
        return

    preset = _STYLE_PRESETS.get(tool) or {}
    need_photo = preset.get("need_photo", False)
    msg = query.message
    if not msg:
        await _safe_cb_ack(query)
        return

    # 注意：plog 缓存按 from_user.id 索引，所以这里也用 from_user.id 以保持一致
    user_id = (
        getattr(getattr(query, "from_user", None), "id", None)
        or getattr(getattr(msg, "chat", None), "id", None)
        or 0
    )

    # need_photo 类：能直接生成就直接生成；不行就 set pending
    if need_photo:
        # 用户已有照片缓存 → 直接触发对应 runner
        cached = plog_get_pending_photo(user_id) if user_id else None
        if cached and cached.file_path:
            await _safe_cb_ack(query)
            # 风格感知状态文案
            try:
                await msg.answer(_style_start_text(name, tool))
            except Exception:
                pass
            # 清掉可能残留的 pending（已经在生成了）
            clear_pending_style(user_id)
            # bot 从 query / msg 多个位置兜底取（aiogram CallbackQuery 默认从 context 注入）
            bot = (
                getattr(query, "bot", None)
                or getattr(msg, "bot", None)
                or getattr(getattr(msg, "chat", None), "bot", None)
            )
            # 调对应 runner；style 作为 raw_arg/style_raw 传入
            try:
                if tool == "plog":
                    await run_plog_for_user(bot, msg, name, silent_status=True)
                elif tool == "magnet":
                    await run_magnet_for_user(bot, msg, name, silent_status=True)
                elif tool == "y2k":
                    await run_y2k_for_user(bot, msg, name, silent_status=True)
                elif tool == "poster":
                    await run_poster_for_user(bot, msg, name, silent_status=True)
            except Exception:
                # runner 内部已经有 try/except 兜底；这层只是为了别把异常抛回 aiogram
                pass
            return

        # 没缓存：set pending
        if user_id:
            set_pending_style(user_id, tool, name)
        await _safe_cb_ack(query)
        await msg.answer(
            _style_usage_text(tool, name),
            reply_markup=_build_back_to_style_keyboard(tool),
        )
        return

    # /img /meme：始终 set pending，等用户下一条文字作为描述
    if user_id:
        set_pending_style(user_id, tool, name)
    await _safe_cb_ack(query)
    await msg.answer(
        _style_usage_text(tool, name),
        reply_markup=_build_back_to_style_keyboard(tool),
    )


@router.callback_query(F.data.startswith("bb:"))
async def beibei_companion_callback(query: CallbackQuery, bot: Bot):
    """贝贝陪伴菜单按钮回调。仅 private 模式：Business 不会触发这套 callback，因为
    Business 不会出现 /宝宝 命令路径，且 callback_data 也不会被 Business 路由订阅。"""
    try:
        await dispatch_companion_callback(bot, query)
    except Exception as e:
        logger.warning("bb callback dispatch crashed | err=%s", e)
        try:
            await query.answer()
        except Exception:
            pass


async def _run_beibei_keyword_intent(bot: Bot, message: Message, text: str, intent) -> None:
    """处理贝贝侧自然关键词触发：
    - 若 intent.short_reply 非空：直接发短句（不走 LLM），更新 last_mode；
      若 intent.needs_ajun_alert：用 dedup_alert 给阿君 status-only 通报
    - 否则：把 intent.mode 注入 system addendum，让 gpt-5.5 在该模式下自然回复
    """
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0
    # 任何路径都先更新 session 的 last_mode，方便后续 /宝宝 关系唤醒挑词
    try:
        st = companion_get_session_state(user_id) if user_id else None
        if st is not None:
            st.last_mode = intent.mode
    except Exception:
        pass

    # status-only 通报阿君（不会出现在贝贝侧）
    if intent.needs_ajun_alert:
        try:
            from services.alert_service import dedup_alert as _dedup_alert
            import time as _time
            sender_label = (
                (message.from_user.username if message.from_user else "")
                or (str(message.from_user.id) if message.from_user else "?")
            )
            bucket = int(_time.time() // (5 * 60))
            key = f"bb_kw::{sender_label}::{intent.keyword}::{bucket}"
            alert_text = (
                f"{intent.alert_label}（{sender_label}）\n"
                f"依据：{intent.alert_reason}\n"
                f"机器人已做：自然短回应，未弹菜单。\n"
                f"——仅为状态通报，机器人仍在用 gpt-5.5 正常陪她。"
            )
            await _dedup_alert(bot, key, alert_text)
        except Exception as _ae:
            logger.warning("beibei keyword ajun alert failed | err=%s", _ae)

    # 短句直接回（关键词触发的关系感短句）
    if intent.short_reply:
        try:
            await bot.send_message(chat_id, intent.short_reply)
        except Exception as _se:
            logger.warning("beibei keyword short_reply failed | err=%s", _se)
        return

    # 没短句：走 gpt-5.5 自然回复 + 模式 addendum
    try:
        classification = companion_classify(user_id, text, tz="Asia/Hong_Kong", media_kind="text")
        # 用关键词建议的 mode 覆盖（更稳）
        try:
            classification.mode = intent.mode
        except Exception:
            pass
        system_prompt = await build_system_prompt_with_xiaopang(system_prompt_for_mode(message), message)
        system_prompt = system_prompt + "\n\n" + build_system_addendum(classification)
        history = get_history(user_id)
        messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": text}]
        messages = trim_messages(messages)
        await send_chat_action_safe(bot, chat_id, ChatAction.TYPING)
        result = await call_openai(messages, CORE_MODEL, "private", chat_id=chat_id)
        raw = (result or {}).get("reply_text", "") or ""
        final_text = post_process_reply(raw, classification)
        result = dict(result or {})
        result["reply_text"] = final_text
        await send_reply(bot, chat_id, result, CORE_MODEL)
        save_history(user_id, text, final_text)
        try:
            companion_record_after_reply(user_id, classification, final_text)
        except Exception:
            pass
    except Exception as _le:
        logger.warning("beibei keyword LLM path failed | err=%s", _le)


async def _run_atree_for_beibei(bot: Bot, message: Message, text: str) -> bool:
    """阿树（贝贝侧）主入口：识别 → 安全短句回复 → 必要时通知阿君。

    返回 True 表示已经回应了贝贝（调用方应 return）。返回 False 表示当前文本没命中
    Atree 关键词，调用方可继续走自然陪伴流程。

    所有贝贝可见文本统一过 sanitize_visible_reply()。
    """
    if not message or not message.from_user:
        return False
    chat_id = message.chat.id
    user_id = message.from_user.id
    try:
        intent_obj = atree_detect_intent(text)
    except Exception as e:
        logger.warning("atree detect crashed | err=%s", e)
        return False
    if intent_obj is None:
        return False

    # 1) 给贝贝的回复：永远从安全池里挑一条，过 sanitize
    try:
        raw_reply = atree_pick_safe_reply(intent_obj.intent)
    except Exception as e:
        logger.warning("atree pick safe reply failed | err=%s", e)
        raw_reply = "嗯，我在。"
    safe = sanitize_visible_reply(raw_reply, max_sentences=2, max_chars=80)
    try:
        await bot.send_message(chat_id, safe)
        record_last_atree_reply(chat_id, safe)
    except Exception as e:
        logger.warning("atree send to beibei failed | err=%s", e)

    # 2) 通知阿君（独立通道，不出现在贝贝侧）
    try:
        if atree_should_send_alert(user_id, intent_obj):
            notice = atree_build_owner_notice(intent_obj, original_text=text)
            from services.alert_service import alert_owner
            await alert_owner(bot, notice)
    except Exception as e:
        logger.warning("atree owner alert failed | err=%s", e)
    return True


async def _maybe_answer_image_status(bot: Bot, message: Message, text: str) -> bool:
    """若文本像「图片呢」类问题且存在 retry_task，给状态回答；返回 True 表示已处理。

    优先级：在工具命令检测之前；命令以 / 开头则不命中（用户在显式发命令）。
    """
    if not message or not message.from_user:
        return False
    raw = (text or "").strip()
    if not raw or raw.startswith("/"):
        return False
    if not _looks_like_image_status_question(raw):
        return False
    user_id = message.from_user.id
    task = retry_get_task(user_id)
    if not task:
        return False
    chat_id = message.chat.id
    style_show = task.style or task.tool
    if task.status == "failed":
        reason = task.failed_reason or "超时或接口出错"
        await _send_text_with_keyboard(
            bot,
            chat_id,
            f"上一次「{style_show}」没出来（{reason}）。\n照片/参数我都记着，发 /继续 或点下面的「再试一次」就再发一遍。",
            _build_retry_keyboard(),
        )
        return True
    if task.status == "pending":
        await send_long_text(
            bot,
            chat_id,
            f"「{style_show}」还在出图，第一版稍等一下；如果太久没有，发 /继续 再试一次。",
        )
        return True
    return False


@router.message(F.text)
async def text_handler(message: Message, bot: Bot):
    if should_skip_message(message) or get_chat_mode(message) != "private":
        return
    user_id = message.from_user.id
    text = message.text or ""

    # 「图片呢」类状态询问优先于聊天回复 —— 不让模型答「我没收到图片」之类
    if await _maybe_answer_image_status(bot, message, text):
        return

    # 贝贝陪伴模块（P0 spec gating）：
    # - 贝贝本人：仅可见 /宝宝（关系唤醒），其它命令不响应（走普通陪伴流程）
    # - owner（阿君预览/调试）：仍可用全套命令
    # - 陌生人：完全不响应陪伴命令
    _is_xp_early = False
    try:
        _is_xp_early = await is_xiaopang(message)
    except Exception:
        pass
    _is_owner_early = is_owner(message)

    head_cmd = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    head_cmd = head_cmd.split("@", 1)[0]  # 兼容 /宝宝@botusername

    # 1) 晚安等分数：仅 owner 仍走旧逻辑（owner 预览时使用）；贝贝走自然陪伴
    if _is_owner_early and await maybe_consume_night_score(bot, message, text):
        return

    # 2) 命令分发：贝贝只允许 /宝宝（且 /宝宝 走关键词路径，不再弹菜单）；owner 可走全集
    if head_cmd in COMPANION_COMMANDS:
        if _is_xp_early:
            # 贝贝发任何 slash 陪伴命令一律走关键词路径（不弹菜单、不分发到老 handler）
            # /宝宝 由 detect_intent 兼容处理为「宝宝」关键词
            pass
        elif _is_owner_early:
            await dispatch_companion_command(bot, message, head_cmd)
            return

    # 2b) 贝贝侧：先走「阿树」自然关键词路径（关键词命中 → 安全短句 + 必要时通报阿君）
    if _is_xp_early:
        try:
            if await _run_atree_for_beibei(bot, message, text):
                return
        except Exception as _ae:
            logger.warning("atree beibei route crashed | err=%s", _ae)

    if is_owner(message):
        plan_reply = await handle_owner_plan_command(text)
        if plan_reply:
            await send_long_text(bot, message.chat.id, plan_reply)
            return
        cmd = text.strip().split(maxsplit=1)[0]
        # 隐藏 owner 命令仍然可用，但不会在 /play /help 出现
        if cmd in XIAOPANG_OWNER_COMMANDS:
            reply = await owner_xiaopang_command_reply(text)
            await send_long_text(bot, message.chat.id, reply)
            return
        # 联系人白名单维护（隐藏，不出现在菜单）：
        #   /联系人列表、/添加联系人 xxx、/删除联系人 xxx
        if cmd in CONTACT_OWNER_COMMANDS:
            contact_reply = await owner_contact_command_reply(text)
            if contact_reply is not None:
                await send_long_text(bot, message.chat.id, contact_reply)
                return
        # owner 灰度观测命令（隐藏，不出现在 /play /help）：
        #   /健康检查 - 服务/DB/secrets/调度器/模型路由
        #   /灰度状态 - 当天 incoming/outgoing/静默桶/媒体计数/贝贝消息数
        if cmd in OWNER_HEALTH_COMMANDS:
            health_reply = await owner_health_command_reply(text)
            if health_reply is not None:
                await send_long_text(bot, message.chat.id, health_reply)
                return

    # 贝贝（专属档案用户）判定。
    # 公开小工具现在允许贝贝使用，但隐藏的 owner 管理命令依然要先拦下。
    is_xp = await is_xiaopang(message)

    if is_xp:
        await remember_xiaopang_identity(message)
        scope = await xiaopang_scope(message)
        await store_message(message, "incoming", text, "text", scope=scope)
        await maybe_hit_xiaopang_reminders(message, text, bot)

        # FINAL SPEC: private 模式下也跑一次风控（不降级回复，只告警）。
        # 走 dedup_alert，避免短时间内重复打扰真人。
        try:
            sender_label = (message.from_user.username or str(message.from_user.id))
            await risk_check_and_alert(
                bot,
                user_id=message.from_user.id,
                sender_label=sender_label,
                text=text,
                is_business=False,
            )
        except Exception as _e:
            logger.warning("private risk check failed | err=%s", _e)

        # 先拦隐藏 owner 命令（/小胖设置 等），避免被她意外触发
        block_reply = await xiaopang_block_owner_command_for_private(message, text)
        if block_reply:
            await send_long_text(bot, message.chat.id, block_reply)
            await store_message(message, "outgoing", block_reply, "system_cmd", scope=scope)
            return

        # 先检查 pending 风格（/img /meme）：贝贝侧同样可消费，公开工具
        if await _maybe_consume_pending_for_text(bot, message, text):
            await store_message(message, "outgoing", "[pending 风格消费]", "system_cmd", scope=scope)
            return

        # 公开工具命令：现在允许贝贝用 /img /meme /plog /magnet /polish /tldr /eli5 /excel /eat /reply
        tool, arg = _detect_tool_command(text)
        if tool in _IMAGE_TOOLS:
            await _send_image_tool(bot, message, tool, arg)
            await store_message(message, "outgoing", f"[公开工具 /{tool}]", "system_cmd", scope=scope)
            return
        if tool == "plog":
            await run_plog_for_user(bot, message, arg)
            await store_message(message, "outgoing", "[公开工具 /plog]", "system_cmd", scope=scope)
            return
        if tool in ("magnet", "fridge"):
            await run_magnet_for_user(bot, message, arg)
            await store_message(message, "outgoing", "[公开工具 /magnet]", "system_cmd", scope=scope)
            return
        if tool == "y2k":
            await run_y2k_for_user(bot, message, arg)
            await store_message(message, "outgoing", "[公开工具 /y2k]", "system_cmd", scope=scope)
            return
        if tool in ("poster", "starposter"):
            await run_poster_for_user(bot, message, arg)
            await store_message(message, "outgoing", "[公开工具 /poster]", "system_cmd", scope=scope)
            return
        if tool == "imgedit":
            await run_imgedit_for_user(bot, message, arg)
            await store_message(message, "outgoing", "[公开工具 /改图]", "system_cmd", scope=scope)
            return
        if tool == "i2v":
            await run_i2v_for_user(bot, message, arg)
            await store_message(message, "outgoing", "[公开工具 /图生视频]", "system_cmd", scope=scope)
            return
        if tool in _TEXT_TOOLS:
            await _send_text_tool(bot, message, tool, arg)
            await store_message(message, "outgoing", f"[公开工具 /{tool}]", "system_cmd", scope=scope)
            return

        setting_reply = await handle_xiaopang_private_setting(message, text)
        if setting_reply:
            await send_long_text(bot, message.chat.id, setting_reply)
            await store_message(message, "outgoing", setting_reply, "system_cmd", scope=scope)
            return

        privacy_reply = await xiaopang_fixed_privacy_reply(message, text)
        if privacy_reply:
            await send_long_text(bot, message.chat.id, privacy_reply)
            await store_message(message, "outgoing", privacy_reply, "system_cmd", scope=scope)
            return

        hit = await xiaopang_blocklist_hit(text)
        if hit:
            quiet_reply = {"reply_text": "这个我先不接，换个话题聊。", "sticker_type": None}
            await send_reply(bot, message.chat.id, quiet_reply, "blocklist")
            await store_message(message, "outgoing", quiet_reply["reply_text"], "system_cmd", scope=scope)
            return
    else:
        # 普通用户：先检查是否有 /img /meme 的 pending 风格，能消费就直接走生成
        if await _maybe_consume_pending_for_text(bot, message, text):
            return
        # 公开工具命令分发
        tool, arg = _detect_tool_command(text)
        if tool in _IMAGE_TOOLS:
            await _send_image_tool(bot, message, tool, arg)
            return
        if tool == "plog":
            await run_plog_for_user(bot, message, arg)
            return
        if tool in ("magnet", "fridge"):
            await run_magnet_for_user(bot, message, arg)
            return
        if tool == "y2k":
            await run_y2k_for_user(bot, message, arg)
            return
        if tool in ("poster", "starposter"):
            await run_poster_for_user(bot, message, arg)
            return
        if tool == "imgedit":
            await run_imgedit_for_user(bot, message, arg)
            return
        if tool == "i2v":
            await run_i2v_for_user(bot, message, arg)
            return
        if tool in _TEXT_TOOLS:
            await _send_text_tool(bot, message, tool, arg)
            return
        scope = "default"
        await store_message(message, "incoming", text, "text", scope=scope)

    history = get_history(user_id)
    # 双核心同级高配（贝贝 = 阿君）：贝贝/owner 走 atree_models resolver；普通用户继续 CORE_MODEL。
    _is_owner_now = False
    try:
        _is_owner_now = is_owner(message)
    except Exception:
        pass
    if is_xp:
        try:
            from services.atree_models import pick_beibei_companion_model
            model = pick_beibei_companion_model(deep=False)
        except Exception:
            model = CORE_MODEL
    elif _is_owner_now:
        try:
            from services.atree_models import pick_owner_default_model
            model = pick_owner_default_model(deep=False)
        except Exception:
            model = CORE_MODEL
    else:
        # 普通用户/陌生人：保留旧配置 CORE_MODEL（spec 要求普通窗口不动）。
        model = CORE_MODEL
    system_prompt = await build_system_prompt_with_xiaopang(system_prompt_for_mode(message), message)

    classification = None
    if is_xp:
        try:
            classification = companion_classify(
                user_id, text, tz="Asia/Hong_Kong", media_kind="text",
            )
            system_prompt = system_prompt + "\n\n" + build_system_addendum(classification)
        except Exception as _ce:
            logger.warning("private companion classify failed | err=%s", _ce)
        # 注入阿树人设（贝贝看见的所有非命令输出都受此约束）
        system_prompt = system_prompt + "\n\n" + ATREE_SYSTEM_PROMPT

    messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": text}]
    messages = trim_messages(messages)
    await send_chat_action_safe(bot, message.chat.id, ChatAction.TYPING)
    result = await call_openai(messages, model, "private", chat_id=message.chat.id)
    raw_reply = (result or {}).get("reply_text", "") or ""

    # 贝贝侧：post-process（长度裁切 / emoji 限制 / 追问熔断 / 兜底）
    if is_xp and classification is not None:
        try:
            final_text = post_process_reply(raw_reply, classification)
        except Exception as _pe:
            logger.warning("private companion post_process failed | err=%s", _pe)
            final_text = raw_reply
        # 阿树最终安全过滤：句子/字符上限、禁词兜底
        try:
            final_text = sanitize_visible_reply(final_text)
        except Exception as _se:
            logger.warning("private atree sanitize failed | err=%s", _se)
        result = dict(result or {})
        result["reply_text"] = final_text
        try:
            record_last_atree_reply(message.chat.id, final_text)
        except Exception:
            pass
        # 阿君状态通报（status-only 4 段式），只在 needs_ajun_alert 时发
        try:
            sender_label = (
                (message.from_user.username if message.from_user else "")
                or (str(message.from_user.id) if message.from_user else "?")
            )
            alert = build_ajun_alert(classification, sender_label)
            if alert and alert.should_alert:
                from services.alert_service import dedup_alert as _dedup_alert
                await _dedup_alert(bot, alert.dedup_key, alert.text)
        except Exception as _ae:
            logger.warning("private companion ajun alert failed | err=%s", _ae)
        # 更新会话级状态（ask_budget / nickname cooldown / last_mode）
        try:
            companion_record_after_reply(user_id, classification, final_text)
        except Exception:
            pass

    await send_reply(bot, message.chat.id, result, model)
    save_history(user_id, text, result.get("reply_text", ""))
    await store_message(message, "outgoing", result.get("reply_text", ""), "text", scope=scope)
