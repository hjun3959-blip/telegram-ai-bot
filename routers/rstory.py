"""R 级互动剧情系统 —— 最小入口路由（/rstory）。

本路由只做"演示骨架"：用抽象支付流程把 FSM 推进 + USDT 三阶段解锁串起来，
不全面接入现有业务路由，也不破坏现有功能（只在 private 模式响应，Business 不触发）。

交互（用 inline 按钮，callback_data 全部以 rstory: 前缀，独立命名空间）：
- /rstory：开始/恢复默认角色剧情，展示当前节点 + 选项按钮。
- rstory:choice:<key>：按当前节点的转移推进；遇阶段边界且未解锁 → 弹"解锁"按钮。
- rstory:pay:<stage>：创建解锁订单（抽象 provider），展示占位支付信息 + "我已支付"按钮。
- rstory:confirm:<charge_id>：确认支付 → 写解锁记录 → 推进 FSM 进入新阶段。
  （演示用：Mock provider 在这里被 mark_paid 模拟到账，真实渠道改为查询链上即可。）

真实接入时只需把 provider 换成真实实现（config.RSTORY_PAYMENT_PROVIDER），
本路由逻辑不变。
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from services import rstory_content as content
from services import rstory_fsm_service as fsm
from services import rstory_payment as payment
from services.context_service import get_chat_mode
from utils.logger import setup_logging

logger = setup_logging()

router = Router(name="rstory")


def _node_keyboard(state: fsm.StateView) -> InlineKeyboardMarkup | None:
    """把当前节点的转移渲染成按钮。无转移（终点）返回 None。"""
    node = state.node
    rows: list[list[InlineKeyboardButton]] = []
    for tr in node.transitions:
        label = tr.label or tr.choice_key
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"rstory:choice:{tr.choice_key}")]
        )
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_node_text(state: fsm.StateView) -> str:
    node = state.node
    parts = [f"〔阶段{state.stage}〕{node.text}"]
    if node.media_placeholder:
        parts.append(node.media_placeholder)
    return "\n".join(parts)


def _unlock_keyboard(stage: int) -> InlineKeyboardMarkup:
    price = payment.stage_price_usdt(stage)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"解锁阶段{stage}（{price} USDT）",
                    callback_data=f"rstory:pay:{stage}",
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


async def _safe_ack(query: CallbackQuery, text: str | None = None) -> None:
    try:
        await query.answer(text) if text else await query.answer()
    except Exception:
        pass


@router.message(Command("rstory"))
async def rstory_start(message: Message):
    """开始/恢复默认角色剧情。仅 private。"""
    if get_chat_mode(message) != "private":
        return
    user_id = message.from_user.id if message.from_user else 0
    character_id = content.DEFAULT_CHARACTER_ID
    state = await fsm.start_story(user_id, character_id)
    ch = content.get_character(character_id)
    intro = f"{ch.name}：{ch.intro}\n\n" if ch else ""
    await message.answer(intro + _render_node_text(state), reply_markup=_node_keyboard(state))


@router.callback_query(F.data.startswith("rstory:choice:"))
async def rstory_choice(query: CallbackQuery):
    """按当前节点转移推进。阶段边界且未解锁 → 弹解锁按钮。"""
    data = query.data or ""
    choice_key = data.split(":", 2)[2] if data.count(":") >= 2 else ""
    user_id = query.from_user.id if query.from_user else 0
    character_id = content.DEFAULT_CHARACTER_ID
    msg = query.message
    if not msg:
        await _safe_ack(query)
        return

    result = await fsm.try_advance(user_id, character_id, choice_key)
    await _safe_ack(query)

    if result.status == fsm.STATUS_NEEDS_UNLOCK:
        stage = result.unlock_stage
        await msg.answer(
            f"再往下是阶段{stage}，需要先解锁。",
            reply_markup=_unlock_keyboard(stage),
        )
        return
    if result.status == fsm.STATUS_INVALID:
        await msg.answer("这个选择现在用不了，请用下面的按钮。")
        return

    state = fsm.StateView(character_id, result.stage, result.node.node_id, result.node)
    if result.status == fsm.STATUS_END:
        await msg.answer(_render_node_text(state))
        return
    await msg.answer(_render_node_text(state), reply_markup=_node_keyboard(state))


@router.callback_query(F.data.startswith("rstory:pay:"))
async def rstory_pay(query: CallbackQuery):
    """创建解锁订单（抽象 provider），展示占位支付信息。"""
    data = query.data or ""
    try:
        stage = int(data.split(":", 2)[2])
    except (ValueError, IndexError):
        await _safe_ack(query, "参数错误")
        return
    user_id = query.from_user.id if query.from_user else 0
    character_id = content.DEFAULT_CHARACTER_ID
    msg = query.message
    if not msg:
        await _safe_ack(query)
        return

    result = await payment.create_unlock_charge(user_id, character_id, stage)
    await _safe_ack(query)
    if result.already_unlocked:
        # 已解锁：不重复收费，直接进入该阶段
        state = await fsm.enter_stage(user_id, character_id, stage)
        await msg.answer(
            f"阶段{stage}此前已解锁，直接继续。\n\n" + _render_node_text(state),
            reply_markup=_node_keyboard(state),
        )
        return
    info = result.charge
    await msg.answer(info.pay_info, reply_markup=_confirm_keyboard(info.charge_id))


@router.callback_query(F.data.startswith("rstory:confirm:"))
async def rstory_confirm(query: CallbackQuery):
    """确认支付 → 写解锁记录 → 推进 FSM 进入新阶段。

    演示用：Mock provider 在确认前先 mark_paid 模拟到账；真实渠道删掉这段，
    confirm_charge 会去查询链上/聚合渠道的真实状态。
    """
    data = query.data or ""
    charge_id = data.split(":", 2)[2] if data.count(":") >= 2 else ""
    msg = query.message
    if not msg:
        await _safe_ack(query)
        return

    provider = payment.get_provider()
    # 演示：Mock 直接模拟到账。真实 provider 没有 mark_paid，confirm 走真实查询。
    if isinstance(provider, payment.MockUSDTProvider):
        provider.mark_paid(charge_id)

    result = await payment.confirm_unlock(charge_id, provider=provider)
    await _safe_ack(query)
    if not result.ok:
        await msg.answer(result.message or "支付未确认。")
        return
    state = result.state
    await msg.answer(
        f"{result.message}\n\n" + _render_node_text(state),
        reply_markup=_node_keyboard(state),
    )
