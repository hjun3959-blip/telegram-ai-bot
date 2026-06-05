"""R 级互动剧情系统 —— 数据驱动入口路由（/rstory）。

重构（用户最终决定）：剧情从 DB 读规则推进（数据驱动 FSM）。本路由只做"最小入口"：
展示当前 scene 的 fixed_text + choices，处理 choice 推进、payment_gate 触发 OxaPay 支付、
age_gate 触发年龄验证。不全面接入现有业务路由，也不破坏现有功能（仅 private 模式响应）。

交互（inline 按钮，callback_data 以 rstory: 前缀，独立命名空间）：
- /rstory：开始/恢复默认剧本，展示当前场景 + 选项按钮。
- rstory:choice:<value>：按当前场景的 choice 转移推进。
- rstory:pay:<unlock_id>：未解锁的 payment_gate，创建 OxaPay/Mock 订单，展示支付信息。
- rstory:confirm:<charge_id>：演示确认支付（Mock 直接 mark_paid；真实渠道走 Webhook）。
- rstory:age:verify：年龄验证确认，置 users.age_verified=1 并消费 age_verify 转移。

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


def _scene_keyboard(scene: store.Scene) -> InlineKeyboardMarkup | None:
    """把当前 scene 的 choices 渲染成按钮。无 choices 返回 None。"""
    rows: list[list[InlineKeyboardButton]] = []
    for choice in scene.choices:
        label = choice.get("label") or choice.get("value", "")
        value = choice.get("value", "")
        if not value:
            continue
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"rstory:choice:{value}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _render_scene_text(scene: store.Scene) -> str:
    title = f"〔{scene.title}〕" if scene.title else ""
    if scene.fixed_text:
        return f"{title}{scene.fixed_text}".strip()
    if scene.scene_type == "ai_free":
        return f"{title}（进入自由对话场景，分级 L{scene.content_level}）".strip()
    return f"{title}".strip() or "（无内容）"


def _unlock_keyboard(unlock_id: str, title: str, price: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"解锁「{title}」（{price} USDT）",
                    callback_data=f"rstory:pay:{unlock_id}",
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


def _age_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="我已满 18 岁，确认验证", callback_data="rstory:age:verify")]
        ]
    )


async def _safe_ack(query: CallbackQuery, text: str | None = None) -> None:
    try:
        await query.answer(text) if text else await query.answer()
    except Exception:  # noqa: BLE001
        pass


async def _render_result(msg: Message, result: fsm.AdvanceResult) -> None:
    """统一渲染一个 AdvanceResult（OK/END/NEEDS_UNLOCK/NEEDS_AGE/INVALID）。"""
    if result.status == fsm.STATUS_NEEDS_UNLOCK:
        product = await store.get_unlock_product(result.unlock_id)
        title = product.title if product else result.unlock_id
        try:
            price = await payment.unlock_price_usdt(result.unlock_id)
        except ValueError:
            price = 0.0
        text = result.scene.fixed_text or "该分支需要解锁。"
        await msg.answer(text, reply_markup=_unlock_keyboard(result.unlock_id, title, price))
        return
    if result.status == fsm.STATUS_NEEDS_AGE:
        text = result.scene.fixed_text or "该分支需要先完成年龄验证。"
        await msg.answer(text, reply_markup=_age_keyboard())
        return
    if result.status == fsm.STATUS_INVALID:
        await msg.answer(result.message or "这个选择现在用不了，请用下面的按钮。")
        return
    scene = result.scene
    keyboard = None if result.status == fsm.STATUS_END else _scene_keyboard(scene)
    await msg.answer(_render_scene_text(scene), reply_markup=keyboard)


async def _render_state(msg: Message, state: fsm.StateView) -> None:
    await msg.answer(_render_scene_text(state.scene), reply_markup=_scene_keyboard(state.scene))


@router.message(Command("rstory"))
async def rstory_start(message: Message):
    """开始/恢复默认剧本。仅 private。"""
    if get_chat_mode(message) != "private":
        return
    user = message.from_user
    user_id = user.id if user else 0
    username = user.username if user else None
    state = await fsm.start_story(user_id, fsm.DEFAULT_SCRIPT_ID, username=username)
    char = await store.get_character(state.char_id) if state.char_id else None
    intro = f"{char.name}：{char.base_prompt}\n\n" if char else ""
    await message.answer(intro + _render_scene_text(state.scene), reply_markup=_scene_keyboard(state.scene))


@router.callback_query(F.data.startswith("rstory:choice:"))
async def rstory_choice(query: CallbackQuery):
    """按当前场景 choice 转移推进。落到 gate 时弹解锁/验证按钮。"""
    data = query.data or ""
    choice_value = data.split(":", 2)[2] if data.count(":") >= 2 else ""
    user_id = query.from_user.id if query.from_user else 0
    msg = query.message
    if not msg:
        await _safe_ack(query)
        return
    result = await fsm.try_choice(user_id, fsm.DEFAULT_SCRIPT_ID, choice_value)
    await _safe_ack(query)
    await _render_result(msg, result)


@router.callback_query(F.data.startswith("rstory:pay:"))
async def rstory_pay(query: CallbackQuery):
    """为 payment_gate 创建解锁订单（provider 由 config 决定），展示支付信息。"""
    data = query.data or ""
    unlock_id = data.split(":", 2)[2] if data.count(":") >= 2 else ""
    user_id = query.from_user.id if query.from_user else 0
    msg = query.message
    if not msg:
        await _safe_ack(query)
        return

    result = await payment.create_unlock_charge(user_id, unlock_id)
    await _safe_ack(query)
    if result.already_unlocked:
        # 已解锁：不重复收费，直接消费 payment 跃迁继续。
        advance = await fsm.consume_payment(user_id, fsm.DEFAULT_SCRIPT_ID, f"{unlock_id}_paid")
        await msg.answer("该分支此前已解锁，直接继续。")
        await _render_result(msg, advance)
        return
    info = result.charge
    await msg.answer(info.pay_info, reply_markup=_confirm_keyboard(info.charge_id))


@router.callback_query(F.data.startswith("rstory:confirm:"))
async def rstory_confirm(query: CallbackQuery):
    """演示确认支付 → 写解锁记录 → 消费 FSM payment 跃迁。

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


@router.callback_query(F.data == "rstory:age:verify")
async def rstory_age_verify(query: CallbackQuery):
    """年龄验证：置 users.age_verified=1 + 写 content_access_log，消费 age_verify 转移。"""
    user = query.from_user
    user_id = user.id if user else 0
    username = user.username if user else None
    msg = query.message
    if not msg:
        await _safe_ack(query)
        return

    await store.set_age_verified(user_id, username)
    state = await fsm.get_state(user_id, fsm.DEFAULT_SCRIPT_ID)
    if state is not None:
        await store.log_content_access(
            user_id, state.scene.content_level, state.scene.scene_id, True
        )
    result = await fsm.consume_age_verify(user_id, fsm.DEFAULT_SCRIPT_ID)
    await _safe_ack(query, "年龄验证已完成")
    await msg.answer("年龄验证已完成。")
    await _render_result(msg, result)
