"""R 级互动剧情系统 —— 数据驱动入口路由（/rstory）。

第1段重构：入口改为「先选角色 → 再选线 → 进入所选 (角色,线) 的 entry scene」三步。
剧情仍从 DB 读规则推进（数据驱动 FSM）。本路由只做最小入口：展示当前 scene 的
fixed_text + choices，处理 choice 推进、payment_gate 触发 OxaPay 支付、age_gate 触发
年龄验证。仅 private 模式响应，不破坏现有功能。

双线隔离：所有推进/支付/年龄类 callback 都携带 script_id（独立命名空间），引擎按
(user_id, script_id) 维度读写 user_game_state，两条线进度互不串线。

交互（inline 按钮，callback_data 以 rstory: 前缀）：
- /rstory：Step1，列出可选角色（聚合各线 script_characters）。
- rstory:char:<char_id>：Step2，列出该角色可走的剧情线（线A/线B）。
- rstory:line:<script_id>:<char_id>：Step3，进入所选 (角色,线) 的 entry scene。
- rstory:choice:<script_id>:<value>：按当前场景的 choice 转移推进。
- rstory:pay:<script_id>:<unlock_id>：未解锁的 payment_gate，创建 OxaPay/Mock 订单。
- rstory:confirm:<charge_id>：演示确认支付（Mock 直接 mark_paid；真实渠道走 Webhook）。
- rstory:age:<script_id>：年龄验证确认，置 users.age_verified=1 并消费 age_verify 转移。

落到 payment_gate/age_gate 由引擎给出 NEEDS_UNLOCK / NEEDS_AGE 信号，路由据此弹按钮。
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from services import rstory_fsm_service as fsm
from services import rstory_payment as payment
from services import rstory_store as store
from services.context_service import get_chat_mode
from utils.logger import setup_logging

logger = setup_logging()

router = Router(name="rstory")

# 入口聚合可选角色的剧本范围（先选角色入口从这些线汇总角色）。
_ENTRY_SCRIPT_IDS = ("romance_slow", "bold_pursuit")


def _scene_keyboard(script_id: str, scene: store.Scene) -> InlineKeyboardMarkup | None:
    """把当前 scene 的 choices 渲染成按钮（callback 携带 script_id）。无 choices 返回 None。"""
    rows: list[list[InlineKeyboardButton]] = []
    for choice in scene.choices:
        label = choice.get("label") or choice.get("value", "")
        value = choice.get("value", "")
        if not value:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=label, callback_data=f"rstory:choice:{script_id}:{value}"
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _render_scene_text(scene: store.Scene) -> str:
    title = f"〔{scene.title}〕" if scene.title else ""
    if scene.fixed_text:
        return f"{title}{scene.fixed_text}".strip()
    if scene.scene_type == "ai_free":
        return f"{title}（进入自由对话场景，分级 L{scene.content_level}）".strip()
    return f"{title}".strip() or "（无内容）"


def _unlock_keyboard(
    script_id: str, unlock_id: str, title: str, price: float
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"解锁「{title}」（{price} USDT）",
                    callback_data=f"rstory:pay:{script_id}:{unlock_id}",
                )
            ]
        ]
    )


def _confirm_keyboard(charge_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="我已支付（演示确认）", callback_data=f"rstory:confirm:{charge_id}")]
        ]
    )


def _age_keyboard(script_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="我已满 18 岁，确认验证",
                    callback_data=f"rstory:age:{script_id}",
                )
            ]
        ]
    )


async def _safe_ack(query: CallbackQuery, text: str | None = None) -> None:
    try:
        await query.answer(text) if text else await query.answer()
    except Exception:  # noqa: BLE001
        pass


async def _render_result(msg: Message, result: fsm.AdvanceResult) -> None:
    """统一渲染一个 AdvanceResult（OK/END/NEEDS_UNLOCK/NEEDS_AGE/INVALID）。"""
    script_id = result.script_id
    if result.status == fsm.STATUS_NEEDS_UNLOCK:
        product = await store.get_unlock_product(result.unlock_id)
        title = product.title if product else result.unlock_id
        try:
            price = await payment.unlock_price_usdt(result.unlock_id)
        except ValueError:
            price = 0.0
        text = (result.scene.fixed_text if result.scene else None) or "该分支需要解锁。"
        await msg.answer(
            text, reply_markup=_unlock_keyboard(script_id, result.unlock_id, title, price)
        )
        return
    if result.status == fsm.STATUS_NEEDS_AGE:
        text = (result.scene.fixed_text if result.scene else None) or "该分支需要先完成年龄验证。"
        await msg.answer(text, reply_markup=_age_keyboard(script_id))
        return
    if result.status == fsm.STATUS_INVALID:
        await msg.answer(result.message or "这个选择现在用不了，请用下面的按钮。")
        return
    scene = result.scene
    keyboard = None if result.status == fsm.STATUS_END else _scene_keyboard(script_id, scene)
    await msg.answer(_render_scene_text(scene), reply_markup=keyboard)


# ---------------- Step1：先选角色 ----------------

@router.message(Command("rstory"))
async def rstory_start(message: Message):
    """Step1：列出可选角色（聚合各线 script_characters，去重）。仅 private。"""
    if get_chat_mode(message) != "private":
        return
    seen: dict[str, store.Character] = {}
    for script_id in _ENTRY_SCRIPT_IDS:
        for char in await store.list_script_characters(script_id):
            seen.setdefault(char.char_id, char)
    if not seen:
        await message.answer("剧情系统暂未配置可选角色。")
        return
    rows = [
        [InlineKeyboardButton(text=char.name, callback_data=f"rstory:char:{char.char_id}")]
        for char in seen.values()
    ]
    await message.answer(
        "请选择你想互动的角色：", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


# ---------------- Step2：选定角色后选剧情线 ----------------

@router.callback_query(F.data.startswith("rstory:char:"))
async def rstory_pick_char(query: CallbackQuery):
    """Step2：列出选定角色可走的剧情线（线A/线B）。"""
    data = query.data or ""
    char_id = data.split(":", 2)[2] if data.count(":") >= 2 else ""
    msg = query.message
    if not msg:
        await _safe_ack(query)
        return
    char = await store.get_character(char_id)
    if char is None:
        await _safe_ack(query, "角色不存在")
        await msg.answer("该角色不存在，请重新 /rstory。")
        return

    rows: list[list[InlineKeyboardButton]] = []
    for script in await store.list_scripts(active_only=True):
        if script.script_id not in _ENTRY_SCRIPT_IDS:
            continue
        # 仅展示该角色确实出现的线。
        chars = await store.list_script_characters(script.script_id)
        if not any(c.char_id == char_id for c in chars):
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=script.title,
                    callback_data=f"rstory:line:{script.script_id}:{char_id}",
                )
            ]
        )
    await _safe_ack(query)
    if not rows:
        await msg.answer("该角色暂无可走的剧情线。")
        return
    await msg.answer(
        f"已选择 {char.name}。请选择剧情线：",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


# ---------------- Step3：进入所选 (角色, 线) 的 entry scene ----------------

@router.callback_query(F.data.startswith("rstory:line:"))
async def rstory_pick_line(query: CallbackQuery):
    """Step3：进入所选 (角色, 剧情线) 的 entry scene，写/恢复 user_game_state。"""
    data = query.data or ""
    parts = data.split(":", 3)  # ["rstory","line",script_id,char_id]
    script_id = parts[2] if len(parts) > 2 else ""
    char_id = parts[3] if len(parts) > 3 else ""
    user = query.from_user
    user_id = user.id if user else 0
    username = user.username if user else None
    msg = query.message
    if not msg:
        await _safe_ack(query)
        return

    try:
        state = await fsm.enter_story(user_id, script_id, char_id, username=username)
    except fsm.RStoryFSMError:
        await _safe_ack(query, "剧情线不可用")
        await msg.answer("该剧情线暂不可用，请重新 /rstory。")
        return

    char = await store.get_character(state.char_id) if state.char_id else None
    intro = f"{char.name}：{char.base_prompt}\n\n" if char else ""
    await _safe_ack(query)
    await msg.answer(
        intro + _render_scene_text(state.scene),
        reply_markup=_scene_keyboard(script_id, state.scene),
    )


# ---------------- 推进 / 支付 / 年龄验证（均携带 script_id 隔离）----------------

@router.callback_query(F.data.startswith("rstory:choice:"))
async def rstory_choice(query: CallbackQuery):
    """按当前场景 choice 转移推进。落到 gate 时弹解锁/验证按钮。"""
    data = query.data or ""
    parts = data.split(":", 3)  # ["rstory","choice",script_id,value]
    script_id = parts[2] if len(parts) > 2 else ""
    choice_value = parts[3] if len(parts) > 3 else ""
    user_id = query.from_user.id if query.from_user else 0
    msg = query.message
    if not msg:
        await _safe_ack(query)
        return
    result = await fsm.try_choice(user_id, script_id, choice_value)
    await _safe_ack(query)
    await _render_result(msg, result)


@router.callback_query(F.data.startswith("rstory:pay:"))
async def rstory_pay(query: CallbackQuery):
    """为 payment_gate 创建解锁订单（provider 由 config 决定），展示支付信息。"""
    data = query.data or ""
    parts = data.split(":", 3)  # ["rstory","pay",script_id,unlock_id]
    script_id = parts[2] if len(parts) > 2 else ""
    unlock_id = parts[3] if len(parts) > 3 else ""
    user_id = query.from_user.id if query.from_user else 0
    msg = query.message
    if not msg:
        await _safe_ack(query)
        return

    result = await payment.create_unlock_charge(user_id, unlock_id, script_id=script_id)
    await _safe_ack(query)
    if result.already_unlocked:
        # 已解锁：不重复收费，直接消费 payment 跃迁继续。
        advance = await fsm.consume_payment(user_id, script_id, f"{unlock_id}_paid")
        await msg.answer("该分支此前已解锁，直接继续。")
        await _render_result(msg, advance)
        return
    info = result.charge
    await msg.answer(info.pay_info, reply_markup=_confirm_keyboard(info.charge_id))


@router.callback_query(F.data.startswith("rstory:confirm:"))
async def rstory_confirm(query: CallbackQuery):
    """演示确认支付 → 写解锁记录 → 消费 FSM payment 跃迁（按订单记录的 script_id）。

    Mock provider 在确认前先 mark_paid 模拟到账；真实渠道（OxaPay）到账走 Webhook，
    这里 confirm 读本地订单状态。
    """
    data = query.data or ""
    charge_id = data.split(":", 2)[2] if data.count(":") >= 2 else ""
    msg = query.message
    if not msg:
        await _safe_ack(query)
        return

    provider = payment.get_provider()
    if isinstance(provider, payment.MockUSDTProvider):
        provider.mark_paid(charge_id)

    result = await payment.confirm_unlock(charge_id, provider=provider)
    await _safe_ack(query)
    if not result.ok:
        await msg.answer(result.message or "支付未确认。")
        return
    await msg.answer(result.message)
    if result.advance is not None:
        await _render_result(msg, result.advance)


@router.callback_query(F.data.startswith("rstory:age:"))
async def rstory_age_verify(query: CallbackQuery):
    """年龄验证：置 users.age_verified=1 + 写 content_access_log，消费 age_verify 转移。"""
    data = query.data or ""
    script_id = data.split(":", 2)[2] if data.count(":") >= 2 else ""
    user = query.from_user
    user_id = user.id if user else 0
    username = user.username if user else None
    msg = query.message
    if not msg:
        await _safe_ack(query)
        return

    await store.set_age_verified(user_id, username)
    state = await fsm.get_state(user_id, script_id)
    if state is not None:
        await store.log_content_access(
            user_id, state.scene.content_level, state.scene.scene_id, True
        )
    result = await fsm.consume_age_verify(user_id, script_id)
    await _safe_ack(query, "年龄验证已完成")
    await msg.answer("年龄验证已完成。")
    await _render_result(msg, result)
