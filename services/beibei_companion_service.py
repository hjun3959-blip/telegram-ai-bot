"""贝贝（小胖）私信陪伴模块 — 最终版（FINAL SPEC）。

作用范围：仅 private 模式，且仅对「贝贝（小胖）+ 阿君本人预览」生效。
对陌生人 / 其他用户：所有公开陪伴命令都不响应（由调用方的 gating 实现）。

基调：阿君的数字分身，不是恋爱话术机、不是情绪治疗师。
短、稳、克制、有人味，不油腻、不土味、不堆称呼、不上承诺（钱/未来/见面）。
情绪雷达只用于内部判断，绝不外露。

任何字段（数据、文案、按钮 callback_data）都不暴露任何 xiaopang owner 隐藏命令。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from services.alert_service import alert_owner, dedup_alert
from services.reply_service import send_long_text
from utils.logger import setup_logging

logger = setup_logging()


# ============================================================
# 文案（按 FINAL SPEC 直接落地，尽量贴近用户给定的句子）
# ============================================================

# ── /宝宝 主菜单 ────────────────────────────────────────────
MENU_HEADER = "我在。今天想让我陪你做什么？"

# ── /烦 一级分支 ────────────────────────────────────────────
TROUBLE_REPLIES: dict[str, str] = {
    "工作": "事情再多也不是一下做完的。先告诉我最卡你的那件事。",
    "情绪": "今天发生了哪件让你不舒服的小事？",
    "关系": "如果是关于我们，别憋着。你慢慢说，我听着。",
    "钱": "压力是真的，但它只是阶段，不是结局。你不用一个人扛。",
    "心累": "那今天先别讲道理。先歇一会，我陪你。",
    "不想说": "好，那就先不说。我陪你安静一会。",
}

# ── /抱抱 ───────────────────────────────────────────────────
HUG_LINES = [
    "抱住你。今天就让我先抱一会儿。",
    "过来，我抱抱你。不用解释，先稳一会儿。",
    "走过来，给你一个稳稳的抱抱。",
    "心定下来一点，我在这。",
    "我在这里，不走。先抱着你。",
]

# ── /哄我 三风格 ────────────────────────────────────────────
SOOTHE_GENTLE = [
    "宝宝，过来。今天不用强，撒娇也可以。",
    "我在。今天你只管放松，剩下的我顶。",
    "嗯，看见你了。先靠一下，不用想太多。",
]
SOOTHE_FIRM = [
    "看着我，没事的。这事我兜得住。",
    "别怕。先把心放下来，剩下的我处理。",
    "我说过会保护你，那就一定做到。",
]
SOOTHE_PLAYFUL = [
    "好啦好啦，谁惹我家宝宝啦，我去揍他～",
    "笨蛋宝宝，今天的额度不开心已经用完了，剩下的只能开心。",
    "来，眼睛闭一下，我变魔术——好啦，世界变好了。",
]

# ── /想你 回复（给贝贝看）─────────────────────────────────
MISS_REPLIES = [
    "我也想你了，刚要找你。",
    "我也想你。今天有点慢，但我没忘。",
    "我刚也在想你。",
    "想你了，等下就跟你说话。",
    "我也是。心里一直留着你的位置。",
    "嗯，我在想你。",
]

# /想你 → 给阿君（真人）静默状态通报。
# FINAL（更新版）：仅为状态通报；不要求阿君几分钟内回复，不暗示 bot 在等他、也不让他出现替手。
# bot 仍在用 gpt-5.5 正常陪她。
MISS_OWNER_ALERT_TEXT = "贝贝刚刚点了 /想你。仅为状态通报；机器人仍在正常陪她。"

# ── /晚安 分段 ─────────────────────────────────────────────
NIGHT_LOW_REPLY = (
    "嗯，今天确实没那么轻松。\n"
    "不用勉强自己，先睡觉，明天的事明天再算。\n"
    "我在。晚安。"
)
NIGHT_MID_REPLY = (
    "今天平稳就够了。\n"
    "睡前别再翻手机太久，闭眼想一件让你嘴角动一下的小事。\n"
    "晚安。"
)
NIGHT_HIGH_REPLY = (
    "这个分数我很喜欢。\n"
    "明天也想看到你这样的脸。\n"
    "晚安，做个好梦。"
)
NIGHT_NOSCORE_REPLY = "那就不打分。今天辛苦了，先睡觉。"

# ── /早安 ───────────────────────────────────────────────────
MORNING_LINES = [
    "早安，今天慢慢来。",
    "早，今天先把心情打理好。",
    "醒了？喝口水，再慢慢起。",
    "早上好，先深呼吸一下，今天我陪你。",
    "早，今天我会一直在。",
]

# ── /偏爱值 ────────────────────────────────────────────────
FAVOR_BUFFS = [
    "宇宙数据显示，今天适合被抱着说话。",
    "解锁今日「免内耗」buff：今天的事没做错，是事情没顺。",
    "解锁「黏黏怪」buff：今天我会主动多找你一句。",
    "解锁「免吵架卡」一张：今天不许有理也凶你。",
    "解锁「优先级 0」buff：你说先做的事我先做。",
]

# ── /委屈 ───────────────────────────────────────────────────
GRIEVED_LINES = [
    "我知道你委屈，我在。",
    "你委屈的时候，我也会心疼。",
    "不用解释，我先抱抱你。",
]
GRIEVED_OWNER_ALERT_TEXT = (
    "贝贝刚刚点了 /委屈，当前情绪偏委屈。\n"
    "仅为状态通报；机器人仍在用 gpt-5.5 正常陪她。"
)

# ── /想哭 ───────────────────────────────────────────────────
CRY_LINES = [
    "想哭就哭一下，不用憋。",
    "我不催你，慢慢来。",
    "我在。你哭，我陪着。",
]
CRY_OWNER_ALERT_TEXT = (
    "贝贝刚刚点了 /想哭，当前情绪低。\n"
    "仅为状态通报；机器人仍在用 gpt-5.5 正常陪她。"
)

# ── /骂我 ───────────────────────────────────────────────────
SCOLD_ME_LINES = [
    "好好好，我错了，我先认。说，要我怎么哄？",
    "骂吧骂吧，我都接住。但我也心疼你今天不开心。",
    "我不躲。但骂完了，你要让我抱抱你才行。",
]

# ── /在哪 ───────────────────────────────────────────────────
WHERE_LINES = [
    "在处理事情，看到你消息了，等下回你。",
    "在路上，回头就跟你说。",
    "正在忙完，不会让你等太久。",
    "心里一直在你这边。",
    "看到了，先说一声『我在』。",
]

# ── 其它单命令 ─────────────────────────────────────────────
# /没电
NO_BATTERY_LINE = "好，那就不说话。我也不走。"
# /撑一下
HOLD_ON_LINE = "我撑一下。你也撑一下。一起到明天。"
# /不想说
NO_TALK_LINE = "好，那就先不说。我陪你安静一会。"
# /台阶
STAIRS_LINE = "给你台阶下：今天是我的错，先抱我一下。"
# /今天我乖吗
WELL_BEHAVED_LINE = "嗯，今天很乖。比我乖。"
# /我怕
SCARED_LINE = "别怕。我在。"
# /想被偏爱
WANT_FAVOR_LINE = "你本来就被偏爱着。今天也是。"
# /不开心但不想哄
SAD_NO_SOOTHE_LINE = "好，那不哄。坐着陪你就行。"

# ── /今天像不像我（隐藏小命令）─────────────────────────────
# 只返回 STYLE_LIBRARY 里的一句短话，不解释。
STYLE_LIBRARY = [
    "先缓缓", "别硬顶", "我在", "慢慢说", "不急", "我听着",
    "可以委屈", "别自己吓自己", "今天先到这里", "你先照顾好自己",
    "这事我来处理", "你不用替我扛", "先吃饭", "早点休息",
    "我不吵你", "抱一下",
]

# ── 高风险关键词 → 安全短回复 ──────────────────────────────
HIGH_RISK_SAFE_REPLY = "好，我不逼你。你先缓缓，我在。"


# ============================================================
# 内存状态：晚安等分
# ============================================================

@dataclass
class _PendingNightScore:
    user_id: int
    created_at: float


_PENDING_SCORE_TTL = 30 * 60
_pending_scores: dict[int, _PendingNightScore] = {}


def _set_pending_night_score(user_id: int) -> None:
    _pending_scores[user_id] = _PendingNightScore(user_id=user_id, created_at=time.time())


def _consume_pending_night_score(user_id: int) -> bool:
    p = _pending_scores.get(user_id)
    if not p:
        return False
    if time.time() - p.created_at > _PENDING_SCORE_TTL:
        _pending_scores.pop(user_id, None)
        return False
    _pending_scores.pop(user_id, None)
    return True


def has_pending_night_score(user_id: int) -> bool:
    p = _pending_scores.get(user_id)
    if not p:
        return False
    if time.time() - p.created_at > _PENDING_SCORE_TTL:
        _pending_scores.pop(user_id, None)
        return False
    return True


# ============================================================
# 键盘
# ============================================================

def build_baobao_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="💭 说说", callback_data="bb:talk"),
            InlineKeyboardButton(text="🫂 抱抱", callback_data="bb:hug"),
        ],
        [
            InlineKeyboardButton(text="😔 我烦", callback_data="bb:trouble"),
            InlineKeyboardButton(text="💤 晚安", callback_data="bb:night"),
        ],
        [
            InlineKeyboardButton(text="❤️ 想你", callback_data="bb:miss"),
            InlineKeyboardButton(text="🎲 随机惊喜", callback_data="bb:surprise"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_trouble_keyboard() -> InlineKeyboardMarkup:
    """/烦 第一层选项。
    FINAL SPEC：「是工作、情绪、关系，还是单纯烦？」+ 钱/现实压力 + 不想说。
    """
    rows = [
        [
            InlineKeyboardButton(text="工作", callback_data="bb:trouble_pick:工作"),
            InlineKeyboardButton(text="情绪", callback_data="bb:trouble_pick:情绪"),
        ],
        [
            InlineKeyboardButton(text="关系", callback_data="bb:trouble_pick:关系"),
            InlineKeyboardButton(text="钱·现实压力", callback_data="bb:trouble_pick:钱"),
        ],
        [
            InlineKeyboardButton(text="单纯心累", callback_data="bb:trouble_pick:心累"),
            InlineKeyboardButton(text="不想说", callback_data="bb:trouble_pick:不想说"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_soothe_keyboard() -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="温柔版", callback_data="bb:soothe:gentle"),
        InlineKeyboardButton(text="坚定版", callback_data="bb:soothe:firm"),
        InlineKeyboardButton(text="逗你版", callback_data="bb:soothe:playful"),
    ]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============================================================
# 命令处理
# ============================================================

# 仅给「贝贝」可见的公开命令集合（FINAL P0：只剩 /宝宝，不再有菜单）。
# 其它陪伴命令（/烦 /抱抱 …）仅 owner 在私信预览/调试时可用，不暴露给贝贝。
BEIBEI_VISIBLE_COMMANDS: frozenset[str] = frozenset({"/宝宝"})


async def handle_baobao(bot: Bot, message: Message) -> None:
    """/宝宝 P0：不再展示菜单，按最近模式回一句关系唤醒短句。"""
    from services.companion_engine import baobao_wake_line
    from services.companion_mode_router import get_session_state
    user_id = message.from_user.id if message.from_user else 0
    last_mode = get_session_state(user_id).last_mode if user_id else None
    await bot.send_message(message.chat.id, baobao_wake_line(last_mode))


async def handle_baobao_legacy_menu(bot: Bot, message: Message) -> None:
    """旧菜单版本，仅 owner 预览/调试时使用；不挂到 dispatch_companion_command。"""
    await bot.send_message(message.chat.id, MENU_HEADER, reply_markup=build_baobao_menu_keyboard())


async def handle_trouble_first_layer(bot: Bot, message: Message) -> None:
    """/烦：FINAL SPEC 「是工作、情绪、关系，还是单纯烦？」"""
    await bot.send_message(
        message.chat.id,
        "是工作、情绪、关系，还是单纯烦？",
        reply_markup=build_trouble_keyboard(),
    )


async def handle_trouble_pick(bot: Bot, chat_id: int, kind: str) -> None:
    reply = TROUBLE_REPLIES.get(kind) or TROUBLE_REPLIES["不想说"]
    await bot.send_message(chat_id, reply)


async def handle_hug(bot: Bot, message: Message) -> None:
    await bot.send_message(message.chat.id, random.choice(HUG_LINES))


async def handle_soothe_menu(bot: Bot, message: Message) -> None:
    await bot.send_message(message.chat.id, "今天想要哪种哄？", reply_markup=build_soothe_keyboard())


async def handle_soothe_pick(bot: Bot, chat_id: int, kind: str) -> None:
    if kind == "gentle":
        pool = SOOTHE_GENTLE
    elif kind == "firm":
        pool = SOOTHE_FIRM
    elif kind == "playful":
        pool = SOOTHE_PLAYFUL
    else:
        pool = SOOTHE_GENTLE
    await bot.send_message(chat_id, random.choice(pool))


async def handle_miss(bot: Bot, message: Message) -> None:
    """/想你：贝贝看回应；同时静默通知真人（5 分钟窗 dedup）。"""
    await bot.send_message(message.chat.id, random.choice(MISS_REPLIES))
    sender = (message.from_user.username or str(message.from_user.id)) if message.from_user else "?"
    bucket = int(time.time() // 300)
    key = f"miss::{sender}::{bucket}"
    try:
        await dedup_alert(bot, key, MISS_OWNER_ALERT_TEXT)
    except Exception as e:
        logger.warning("miss alert failed | err=%s", e)


async def handle_night_ask_score(bot: Bot, message: Message) -> None:
    """/晚安：先问「今天开心值几分？1 到 10。」FINAL SPEC 原文。"""
    user_id = message.from_user.id if message.from_user else 0
    if user_id:
        _set_pending_night_score(user_id)
    await bot.send_message(message.chat.id, "今天开心值几分？1 到 10。")


def _parse_score(text: str) -> int | None:
    s = (text or "").strip()
    if not s:
        return None
    num = ""
    for ch in s:
        if ch.isdigit():
            num += ch
            if len(num) >= 2:
                break
        elif num:
            break
    if not num:
        return None
    try:
        n = int(num)
    except ValueError:
        return None
    if 1 <= n <= 10:
        return n
    return None


def _looks_like_noscore(text: str) -> bool:
    """识别「不想打分 / 不打分 / 算了 / 不想算 / 别算了」类拒绝。"""
    s = (text or "").strip().replace(" ", "")
    if not s:
        return False
    keywords = ("不打分", "不想打分", "不算了", "算了", "别算了", "不想算", "懒得打分", "不评分", "不想评分")
    return any(k in s for k in keywords)


async def maybe_consume_night_score(bot: Bot, message: Message, text: str) -> bool:
    """处于晚安等分状态时，消费下一条文字。

    分支：
    - 命中 1-10 → 按 1-4 / 5-7 / 8-10 给安抚回复 + 晚安
    - 命中「不想打分/算了…」→ 「那就不打分。今天辛苦了，先睡觉。」+ 清状态
    - 其它非数字 → 让用户重发，**不消费**状态
    """
    if not message or not message.from_user:
        return False
    user_id = message.from_user.id
    if not has_pending_night_score(user_id):
        return False
    chat_id = message.chat.id

    # 不打分路径
    if _looks_like_noscore(text):
        _consume_pending_night_score(user_id)
        await bot.send_message(chat_id, NIGHT_NOSCORE_REPLY)
        return True

    score = _parse_score(text)
    if score is None:
        # 让用户重发，不清状态
        await bot.send_message(chat_id, "我等一个 1-10 的数字就行～不想打分也可以直接说「不打分」。")
        return True

    _consume_pending_night_score(user_id)
    if 1 <= score <= 4:
        body = NIGHT_LOW_REPLY
    elif 5 <= score <= 7:
        body = NIGHT_MID_REPLY
    else:
        body = NIGHT_HIGH_REPLY
    await bot.send_message(chat_id, body)
    return True


async def handle_morning(bot: Bot, message: Message) -> None:
    await bot.send_message(message.chat.id, random.choice(MORNING_LINES))


async def handle_favor_level(bot: Bot, message: Message) -> None:
    """/偏爱值：随机 88-100 + 一句 buff。FINAL SPEC：今日被偏爱值：XX% + 例如「宇宙数据显示，今天适合被抱着说话。」"""
    pct = random.randint(88, 100)
    buff = random.choice(FAVOR_BUFFS)
    await bot.send_message(message.chat.id, f"今日被偏爱值：{pct}%\n{buff}")


async def handle_grieved(bot: Bot, message: Message) -> None:
    """/委屈：给贝贝一句安抚 + 给阿君真人通知 + 候选回复。dedup 30 分钟。"""
    await bot.send_message(message.chat.id, random.choice(GRIEVED_LINES))
    sender = (message.from_user.username or str(message.from_user.id)) if message.from_user else "?"
    bucket = int(time.time() // 1800)
    key = f"grieved::{sender}::{bucket}"
    try:
        await dedup_alert(bot, key, GRIEVED_OWNER_ALERT_TEXT)
    except Exception as e:
        logger.warning("grieved alert failed | err=%s", e)


async def handle_cry(bot: Bot, message: Message) -> None:
    """/想哭：候选短安抚 + owner 通知 + 候选回复。dedup 30 分钟。"""
    await bot.send_message(message.chat.id, random.choice(CRY_LINES))
    sender = (message.from_user.username or str(message.from_user.id)) if message.from_user else "?"
    bucket = int(time.time() // 1800)
    key = f"cry::{sender}::{bucket}"
    try:
        await dedup_alert(bot, key, CRY_OWNER_ALERT_TEXT)
    except Exception as e:
        logger.warning("cry alert failed | err=%s", e)


async def handle_scold_me(bot: Bot, message: Message) -> None:
    await bot.send_message(message.chat.id, random.choice(SCOLD_ME_LINES))


async def handle_remember(bot: Bot, message: Message) -> None:
    """/记得：只引用 meta.xiaopang:memories 真实条目；为空时如实告知，绝不编造。

    格式：【回忆 NN】<内容>（NN 为 01-99，按条目顺序定）。
    """
    chat_id = message.chat.id
    try:
        from services.xiaopang_service import meta_list_get
        items = await meta_list_get("xiaopang:memories")
    except Exception as e:
        logger.warning("remember meta read failed | err=%s", e)
        items = []
    items = [x.strip() for x in (items or []) if x and x.strip()]
    if not items:
        await bot.send_message(
            chat_id,
            "「记得」这部分还没攒到真实记忆。等阿君之后给我加上，他记下的我才会留着。",
        )
        return
    # 真实条目：随机选一条；编号按它在列表里的下标 + 1
    idx = random.randrange(len(items))
    item = items[idx]
    no = f"{idx + 1:02d}"
    await bot.send_message(chat_id, f"【回忆 {no}】{item}")


async def handle_where(bot: Bot, message: Message) -> None:
    await bot.send_message(message.chat.id, random.choice(WHERE_LINES))


async def handle_no_battery(bot: Bot, message: Message) -> None:
    await bot.send_message(message.chat.id, NO_BATTERY_LINE)


async def handle_hold_on(bot: Bot, message: Message) -> None:
    await bot.send_message(message.chat.id, HOLD_ON_LINE)


async def handle_no_talk(bot: Bot, message: Message) -> None:
    await bot.send_message(message.chat.id, NO_TALK_LINE)


async def handle_stairs(bot: Bot, message: Message) -> None:
    await bot.send_message(message.chat.id, STAIRS_LINE)


async def handle_well_behaved(bot: Bot, message: Message) -> None:
    await bot.send_message(message.chat.id, WELL_BEHAVED_LINE)


async def handle_scared(bot: Bot, message: Message) -> None:
    await bot.send_message(message.chat.id, SCARED_LINE)


async def handle_want_favor(bot: Bot, message: Message) -> None:
    await bot.send_message(message.chat.id, WANT_FAVOR_LINE)


async def handle_sad_no_soothe(bot: Bot, message: Message) -> None:
    await bot.send_message(message.chat.id, SAD_NO_SOOTHE_LINE)


async def handle_today_like_me(bot: Bot, message: Message) -> None:
    """/今天像不像我：从 STYLE_LIBRARY 抽一句短话，**不解释**。"""
    await bot.send_message(message.chat.id, random.choice(STYLE_LIBRARY))


async def handle_surprise(bot: Bot, message: Message) -> None:
    """随机惊喜：从抱抱 / 偏爱值 / 早安 / 逗你版 / 在哪 里挑一个。"""
    funcs = [
        handle_hug,
        handle_favor_level,
        handle_morning,
        lambda b, m: handle_soothe_pick(b, m.chat.id, "playful"),
        handle_where,
    ]
    fn = random.choice(funcs)
    await fn(bot, message)  # type: ignore[arg-type]


# ============================================================
# 命令分发表
# ============================================================

# 命令 → 内部 key
COMPANION_COMMANDS: dict[str, str] = {
    "/宝宝": "baobao",
    "/烦": "trouble",
    "/抱抱": "hug",
    "/哄我": "soothe",
    "/想你": "miss",
    "/晚安": "night",
    "/早安": "morning",
    "/偏爱值": "favor",
    "/委屈": "grieved",
    "/想哭": "cry",
    "/骂我": "scold",
    "/记得": "remember",
    "/在哪": "where",
    "/没电": "no_battery",
    "/撑一下": "hold_on",
    "/不想说": "no_talk",
    "/台阶": "stairs",
    "/今天我乖吗": "well_behaved",
    "/我怕": "scared",
    "/想被偏爱": "want_favor",
    "/不开心但不想哄": "sad_no_soothe",
    "/今天像不像我": "today_like_me",
}


_HANDLERS = {
    "baobao": handle_baobao,
    "trouble": handle_trouble_first_layer,
    "hug": handle_hug,
    "soothe": handle_soothe_menu,
    "miss": handle_miss,
    "night": handle_night_ask_score,
    "morning": handle_morning,
    "favor": handle_favor_level,
    "grieved": handle_grieved,
    "cry": handle_cry,
    "scold": handle_scold_me,
    "remember": handle_remember,
    "where": handle_where,
    "no_battery": handle_no_battery,
    "hold_on": handle_hold_on,
    "no_talk": handle_no_talk,
    "stairs": handle_stairs,
    "well_behaved": handle_well_behaved,
    "scared": handle_scared,
    "want_favor": handle_want_favor,
    "sad_no_soothe": handle_sad_no_soothe,
    "today_like_me": handle_today_like_me,
}


async def dispatch_companion_command(bot: Bot, message: Message, cmd: str) -> bool:
    """命中 COMPANION_COMMANDS 则执行；失败兜底回一句温和文案，绝不抛。"""
    key = COMPANION_COMMANDS.get(cmd)
    if not key:
        return False
    fn = _HANDLERS.get(key)
    if not fn:
        return False
    try:
        await fn(bot, message)
    except Exception as e:
        logger.exception("companion dispatch failed | cmd=%s | err=%s", cmd, e)
        try:
            await send_long_text(bot, message.chat.id, "我有点没接住，等下再发一次试试。")
        except Exception:
            pass
    return True


# ============================================================
# Callback dispatch（bb:*）
# ============================================================

async def dispatch_companion_callback(bot: Bot, query) -> bool:
    data = getattr(query, "data", "") or ""
    if not data.startswith("bb:"):
        return False
    msg = getattr(query, "message", None)
    if not msg:
        try:
            await query.answer()
        except Exception:
            pass
        return True
    chat_id = msg.chat.id
    rest = data[3:]
    try:
        if rest == "talk":
            await bot.send_message(chat_id, "嗯，我在。你说，我听着。")
        elif rest == "hug":
            await bot.send_message(chat_id, random.choice(HUG_LINES))
        elif rest == "trouble":
            await bot.send_message(chat_id, "是工作、情绪、关系，还是单纯烦？", reply_markup=build_trouble_keyboard())
        elif rest.startswith("trouble_pick:"):
            kind = rest.split(":", 1)[1]
            await handle_trouble_pick(bot, chat_id, kind)
        elif rest == "night":
            user_id = query.from_user.id if getattr(query, "from_user", None) else 0
            if user_id:
                _set_pending_night_score(user_id)
            await bot.send_message(chat_id, "今天开心值几分？1 到 10。")
        elif rest == "miss":
            await bot.send_message(chat_id, random.choice(MISS_REPLIES))
            sender = (query.from_user.username or str(query.from_user.id)) if getattr(query, "from_user", None) else "?"
            bucket = int(time.time() // 300)
            key = f"miss::{sender}::{bucket}"
            try:
                await dedup_alert(bot, key, MISS_OWNER_ALERT_TEXT)
            except Exception as e:
                logger.warning("miss callback alert failed | err=%s", e)
        elif rest == "surprise":
            # 简化：直接挑几路常见兜底
            r = random.choice(["hug", "favor", "morning", "playful", "where"])
            if r == "hug":
                await bot.send_message(chat_id, random.choice(HUG_LINES))
            elif r == "favor":
                pct = random.randint(88, 100)
                await bot.send_message(chat_id, f"今日被偏爱值：{pct}%\n{random.choice(FAVOR_BUFFS)}")
            elif r == "morning":
                await bot.send_message(chat_id, random.choice(MORNING_LINES))
            elif r == "where":
                await bot.send_message(chat_id, random.choice(WHERE_LINES))
            else:
                await bot.send_message(chat_id, random.choice(SOOTHE_PLAYFUL))
        elif rest.startswith("soothe:"):
            kind = rest.split(":", 1)[1]
            await handle_soothe_pick(bot, chat_id, kind)
        else:
            try:
                await query.answer("未知操作")
                return True
            except Exception:
                return True
        try:
            await query.answer()
        except Exception:
            pass
    except Exception as e:
        logger.exception("companion callback failed | data=%s | err=%s", data, e)
    return True


# ============================================================
# 安全：对外暴露给 owner 主动发的 alert（不带 dedup，用于路由层自定义触发）
# ============================================================

async def send_owner_alert(bot: Bot, text: str, *, dedup_key: str | None = None) -> None:
    """通过 alert_service 给真人发提示。可选 dedup_key 走 dedup_alert。"""
    if dedup_key:
        await dedup_alert(bot, dedup_key, text)
    else:
        await alert_owner(bot, text)
