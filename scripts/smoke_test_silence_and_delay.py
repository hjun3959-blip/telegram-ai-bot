"""Smoke test：抢话修复（owner cooldown）+ 真人延迟。

覆盖：
1) config 新加的 SELF_MESSAGE_SILENCE_AFTER_OWNER_SECONDS 与 BUSINESS_REPLY_DELAY_* 字段都在
2) context_service.mark_self_silence 后 is_in_owner_cooldown 立刻命中（business）
3) 30 秒（默认）窗口内未过期：仍命中；override 成 1s 后 sleep 1.2s 即过期，恢复可回
4) private 模式无 owner cooldown（不污染功能区）
5) typing_delay_service.compute_human_delay：
   - business 短句在 [MIN, MAX] 范围
   - business 长文本封顶 ≤ MAX
   - business 只贴纸时不超过 3s 上限的安全阈
   - private 默认 0
6) /play /help 文案仍不暴露隐藏管理功能

不联网，不真发；DB 用临时文件。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _fake_business_message(chat_id: int = 555, user_id: int = 1001, conn_id: str = "bc-x") -> SimpleNamespace:
    """构造最小可用的 business 模式 Message 替身。"""
    chat = SimpleNamespace(id=chat_id, type="private")
    from_user = SimpleNamespace(id=user_id, username="u", is_bot=False)
    return SimpleNamespace(
        chat=chat,
        from_user=from_user,
        business_connection_id=conn_id,
        text="hi",
        sticker=None,
        animation=None,
        photo=None,
        voice=None,
        video=None,
        caption=None,
    )


def _fake_private_message(chat_id: int = 7777, user_id: int = 1001) -> SimpleNamespace:
    chat = SimpleNamespace(id=chat_id, type="private")
    from_user = SimpleNamespace(id=user_id, username="u", is_bot=False)
    return SimpleNamespace(
        chat=chat,
        from_user=from_user,
        business_connection_id=None,
        text="hi",
        sticker=None,
        animation=None,
        photo=None,
        voice=None,
        video=None,
        caption=None,
    )


async def main() -> None:
    # 用临时 DB；并把 owner cooldown 改为 1s，便于过期测试。
    tmpdir = tempfile.mkdtemp(prefix="silence_delay_smoke_")
    db_path = os.path.join(tmpdir, "smoke.sqlite3")
    os.environ["BOT_DB_PATH"] = db_path
    os.environ["SELF_MESSAGE_SILENCE_AFTER_OWNER_SECONDS"] = "1"
    os.environ["SELF_MESSAGE_IGNORE_SECONDS"] = "1"
    # business 延迟环境：缩到很小，方便 compute_human_delay 校验范围
    os.environ["BUSINESS_REPLY_DELAY_MIN"] = "2.5"
    os.environ["BUSINESS_REPLY_DELAY_MAX"] = "9.0"
    os.environ["BUSINESS_REPLY_DELAY_PER_CHAR"] = "0.08"
    os.environ["BUSINESS_REPLY_DELAY_JITTER"] = "0.0"  # 取消 jitter 让范围确定

    # 清缓存
    for mod in (
        "config", "db.core",
        "services.context_service",
        "services.typing_delay_service",
        "routers.business", "routers.media", "routers.private",
    ):
        sys.modules.pop(mod, None)

    import config  # noqa: F401
    # 1) config 新字段
    assert hasattr(config, "SELF_MESSAGE_SILENCE_AFTER_OWNER_SECONDS")
    assert config.SELF_MESSAGE_SILENCE_AFTER_OWNER_SECONDS == 1
    assert hasattr(config, "BUSINESS_REPLY_DELAY_MIN")
    assert hasattr(config, "BUSINESS_REPLY_DELAY_MAX")
    assert hasattr(config, "BUSINESS_REPLY_DELAY_PER_CHAR")
    assert hasattr(config, "BUSINESS_REPLY_DELAY_JITTER")
    assert config.BUSINESS_REPLY_DELAY_MIN == 2.5
    assert config.BUSINESS_REPLY_DELAY_MAX == 9.0
    print("[ok] config: SELF_MESSAGE_SILENCE_AFTER_OWNER_SECONDS + BUSINESS_REPLY_DELAY_* 字段就位")

    # 2) owner cooldown 行为
    from services.context_service import (
        mark_self_silence,
        is_in_owner_cooldown,
        is_in_self_silence,
        owner_cooldown_remaining,
    )

    biz_msg = _fake_business_message(chat_id=111)
    assert is_in_owner_cooldown(biz_msg) is False, "未触发时 cooldown 不应命中"
    mark_self_silence(biz_msg)
    assert is_in_owner_cooldown(biz_msg) is True, "owner 自发后该 chat 应该立刻进入 cooldown"
    assert is_in_self_silence(biz_msg) is True, "短窗 self_silence 也应命中"
    rem = owner_cooldown_remaining(biz_msg)
    assert rem > 0, f"剩余时间应 >0，但拿到 {rem}"
    print(f"[ok] mark_self_silence 后 cooldown 命中 | remaining={rem:.2f}s")

    # 不影响别的 chat
    other_chat = _fake_business_message(chat_id=222)
    assert is_in_owner_cooldown(other_chat) is False, "owner 在 chat 111 说话不应让 chat 222 进入冷却"
    print("[ok] cooldown 只作用于同一 chat，不影响其它 chat")

    # 3) 过期测试：env 设为 1s，sleep 1.2s 后应该过期
    await asyncio.sleep(1.2)
    assert is_in_owner_cooldown(biz_msg) is False, "1s 后窗口必须过期，否则会永久吞消息"
    assert is_in_self_silence(biz_msg) is False, "短窗也应过期"
    print("[ok] cooldown 过期后自动恢复，未永久吞消息")

    # 4) private 模式不受 owner cooldown 影响
    priv_msg = _fake_private_message(chat_id=333)
    mark_self_silence(priv_msg)  # 即便调，也只影响内部 dict
    # private 模式 is_in_owner_cooldown 应该返回 False（实现里直接 short-circuit）
    assert is_in_owner_cooldown(priv_msg) is False, "private 模式不应进入 owner cooldown"
    print("[ok] private 模式不进入 owner cooldown")

    # 5) typing_delay_service：纯函数范围测试
    from services.typing_delay_service import compute_human_delay

    # 短句（5 字）：MIN + 5*0.08 = 2.9，处于 [2.5, 9]
    d_short = compute_human_delay(5, mode="business")
    assert 2.5 <= d_short <= 9.0, f"短句延迟应在 [2.5,9]，得到 {d_short}"
    print(f"[ok] business 短句延迟 ok | len=5 | delay={d_short:.2f}s")

    # 中等长度（40 字）：2.5 + 40*0.08 = 5.7
    d_mid = compute_human_delay(40, mode="business")
    assert 2.5 <= d_mid <= 9.0
    assert d_mid > d_short, "更长文本应得到更长延迟"
    print(f"[ok] business 中等长度延迟 ok | len=40 | delay={d_mid:.2f}s")

    # 长文本（500 字）：必须封顶到 MAX
    d_long = compute_human_delay(500, mode="business")
    assert d_long <= 9.0, f"长文本必须封顶到 MAX(9)，得到 {d_long}"
    assert d_long >= 2.5
    print(f"[ok] business 长回复封顶 ok | len=500 | delay={d_long:.2f}s")

    # 只发贴纸：应该比较短，不超过 3s
    d_sticker = compute_human_delay(0, mode="business", has_sticker_only=True)
    assert d_sticker <= 3.0, f"贴纸延迟上限应 <=3s，得到 {d_sticker}"
    print(f"[ok] business 仅贴纸延迟短 | delay={d_sticker:.2f}s")

    # private 默认 0
    d_priv = compute_human_delay(50, mode="private")
    assert d_priv == 0.0, f"private 默认应为 0，得到 {d_priv}"
    print("[ok] private 模式默认延迟 0（功能区不延迟）")

    # 6) /play /help 不暴露隐藏管理
    from routers.private import HELP_TEXT, PLAY_MENU_TEXT, HOW_TO_USE_TEXT, BEIBEI_PLAY_MENU_TEXT, BEIBEI_HELP_TEXT
    forbidden_tokens = ["小胖", "贝贝", "/新计划", "/计划列表", "/小胖", "/学习小胖聊天方式", "管理面板", "授权"]
    for ftext in (HELP_TEXT, PLAY_MENU_TEXT, HOW_TO_USE_TEXT, BEIBEI_PLAY_MENU_TEXT, BEIBEI_HELP_TEXT):
        for tok in forbidden_tokens:
            assert tok not in ftext, f"/play /help 暴露了隐藏关键词 {tok}"
    print("[ok] /play /help 文案不暴露隐藏功能")

    # 7) routers/business.py 与 routers/media.py 都引用了新静默 + 延迟（编译期检查）
    import routers.business as biz_router
    import routers.media as media_router
    assert hasattr(biz_router, "human_typing_delay") or "human_typing_delay" in biz_router.text_handler.__code__.co_names
    assert hasattr(media_router, "human_typing_delay") or "human_typing_delay" in media_router._handle_sticker_or_gif.__code__.co_names
    assert "is_in_owner_cooldown" in biz_router.text_handler.__code__.co_names
    print("[ok] routers business/media 已接入 owner cooldown 与 human_typing_delay")

    print("\nALL SILENCE + DELAY SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
