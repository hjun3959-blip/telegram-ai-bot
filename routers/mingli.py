"""
八字命理路由 — /八字 命令 (aiogram FSMContext)

状态机流程：性别 → 年 → 月 → 日 → 时 → 确认 → AI 解读
"""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from services.mingli_service import format_bazi_card, interpret_bazi
from utils.logger import setup_logging

logger = setup_logging()
router = Router(name="mingli")


# ── FSM 状态 ──────────────────────────────────────────────────────────────────
class BaziStates(StatesGroup):
    ask_gender = State()
    ask_year   = State()
    ask_month  = State()
    ask_day    = State()
    ask_hour   = State()
    confirm    = State()


def _gender_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="♂ 男", callback_data="bazi_gender_男"),
         InlineKeyboardButton(text="♀ 女", callback_data="bazi_gender_女")]
    ])


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ 确认，开始解读", callback_data="bazi_confirm")],
        [InlineKeyboardButton(text="🔄 重新输入",         callback_data="bazi_restart")],
    ])


# ── 入口 ──────────────────────────────────────────────────────────────────────
@router.message(Command("八字", "bazi", "mingli"))
async def cmd_bazi(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BaziStates.ask_gender)
    await message.answer(
        "🔮 *八字命理解读*\n\n请先告诉我你的性别：",
        parse_mode="Markdown",
        reply_markup=_gender_kb(),
    )


# ── 性别 ──────────────────────────────────────────────────────────────────────
@router.callback_query(BaziStates.ask_gender, F.data.startswith("bazi_gender_"))
async def cb_gender(callback: CallbackQuery, state: FSMContext) -> None:
    gender = callback.data.replace("bazi_gender_", "")
    await state.update_data(gender=gender)
    await state.set_state(BaziStates.ask_year)
    label = "♂ 男" if gender == "男" else "♀ 女"
    await callback.message.edit_text(
        f"已选择性别：{label}\n\n📅 请输入出生*年份*（如：1990）：",
        parse_mode="Markdown",
    )
    await callback.answer()


# ── 年 ───────────────────────────────────────────────────────────────────────
@router.message(BaziStates.ask_year, F.text)
async def ask_year(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not re.fullmatch(r"\d{4}", text) or not (1900 <= int(text) <= 2030):
        await message.answer("⚠️ 请输入有效年份（1900–2030），如：1990")
        return
    await state.update_data(year=int(text))
    await state.set_state(BaziStates.ask_month)
    await message.answer("请输入出生*月份*（1–12）：", parse_mode="Markdown")


# ── 月 ───────────────────────────────────────────────────────────────────────
@router.message(BaziStates.ask_month, F.text)
async def ask_month(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not re.fullmatch(r"\d{1,2}", text) or not (1 <= int(text) <= 12):
        await message.answer("⚠️ 请输入有效月份（1–12）")
        return
    await state.update_data(month=int(text))
    await state.set_state(BaziStates.ask_day)
    await message.answer("请输入出生*日期*（1–31）：", parse_mode="Markdown")


# ── 日 ───────────────────────────────────────────────────────────────────────
@router.message(BaziStates.ask_day, F.text)
async def ask_day(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not re.fullmatch(r"\d{1,2}", text) or not (1 <= int(text) <= 31):
        await message.answer("⚠️ 请输入有效日期（1–31）")
        return
    await state.update_data(day=int(text))
    await state.set_state(BaziStates.ask_hour)
    await message.answer(
        "请输入出生*小时*（0–23，24小时制）\n不确定时辰可输入 0（子时）",
        parse_mode="Markdown",
    )


# ── 时 → 展示确认卡 ──────────────────────────────────────────────────────────
@router.message(BaziStates.ask_hour, F.text)
async def ask_hour(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not re.fullmatch(r"\d{1,2}", text) or not (0 <= int(text) <= 23):
        await message.answer("⚠️ 请输入有效小时（0–23）")
        return
    await state.update_data(hour=int(text))
    await state.set_state(BaziStates.confirm)

    data = await state.get_data()
    card = format_bazi_card(data["year"], data["month"], data["day"], data["hour"], data["gender"])
    await message.answer(
        card + "\n\n以上信息是否正确？",
        parse_mode="Markdown",
        reply_markup=_confirm_kb(),
    )


# ── 确认 → AI 解读 ────────────────────────────────────────────────────────────
@router.callback_query(BaziStates.confirm, F.data == "bazi_confirm")
async def cb_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()

    await callback.message.edit_text("🔮 正在为你推算八字命盘，请稍候…")
    await callback.answer()

    try:
        reading = await interpret_bazi(
            year=data["year"],
            month=data["month"],
            day=data["day"],
            hour=data["hour"],
            gender=data["gender"],
            chat_id=callback.from_user.id,
        )
        await _send_long(callback.message, reading)
    except Exception as e:
        logger.exception("bazi interpret error | user=%s | err=%s", callback.from_user.id, e)
        await callback.message.answer("⚠️ 解读生成失败，请稍后重试。")


# ── 重新输入 ──────────────────────────────────────────────────────────────────
@router.callback_query(BaziStates.confirm, F.data == "bazi_restart")
async def cb_restart(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BaziStates.ask_gender)
    await callback.message.edit_text(
        "🔮 *八字命理解读*\n\n请先告诉我你的性别：",
        parse_mode="Markdown",
        reply_markup=_gender_kb(),
    )
    await callback.answer()


# ── 工具 ──────────────────────────────────────────────────────────────────────
async def _send_long(message: Message, text: str, max_len: int = 4000) -> None:
    """长文本分段发送，避免超过 Telegram 4096 字符限制。"""
    if len(text) <= max_len:
        await message.answer(text, parse_mode="Markdown")
        return
    paragraphs = text.split("\n")
    chunk = ""
    for para in paragraphs:
        if len(chunk) + len(para) + 1 > max_len:
            if chunk.strip():
                await message.answer(chunk.strip(), parse_mode="Markdown")
            chunk = para + "\n"
        else:
            chunk += para + "\n"
    if chunk.strip():
        await message.answer(chunk.strip(), parse_mode="Markdown")
