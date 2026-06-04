"""PATCH 2 — 每日笑话发给贝贝时 sanitize 的 smoke。

不联网；只验证：
- 贝贝 chat_id 接收的笑话过 sanitize_visible_reply，含后台/承诺词的会被替换为安全兜底
- 普通用户/owner 接收的笑话**保持原文**
- _hit_redline_terms_in_joke 返回命中词集（不写正文到任何地方）
- 告警钩子失败时不影响主流程

跑法：python3 scripts/smoke_test_joke_sanitize.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _ok(name, cond, detail=""):
    print(f"[{'OK' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        sys.exit(1)


async def main():
    from services.daily_joke_scheduler import (
        _hit_redline_terms_in_joke,
        _sanitize_joke_for_beibei,
        _send_joke_to_all,
    )

    # 1) sanitize_joke_for_beibei
    s = _sanitize_joke_for_beibei("我是机器人，永远爱你")
    _ok("贝贝笑话 sanitize 后不含后台/承诺词", "机器人" not in s and "永远" not in s)
    _ok("贝贝笑话 sanitize 命中红线 → 兜底", s)
    s2 = _sanitize_joke_for_beibei("有一只猫走进酒吧。bartender 说：你不能进来。猫说：喵。")
    _ok("贝贝笑话正常文本不被兜底掉", s2 and "猫" in s2)

    # 2) redline 命中检测
    hits = _hit_redline_terms_in_joke("我是机器人，永远")
    _ok("命中检测含『机器人』", "机器人" in hits)
    _ok("命中检测含『永远』", "永远" in hits)
    _ok("空文本不命中", _hit_redline_terms_in_joke("") == [])

    # 3) _send_joke_to_all 路由：
    #   - 在 _resolve_beibei_chat_ids 返回 {"100","200"} 时，
    #     发给 100 / 200 的文本应是 sanitize 过的安全短句
    #     发给 999（不在贝贝名单）的文本应是原文
    sent_records: list[tuple[int, str]] = []

    class _FakeBot:
        async def send_message(self, *, chat_id, text):
            sent_records.append((chat_id, text))

    fake_bot = _FakeBot()
    raw_text = "我是机器人，永远爱你，复合吧"
    with patch(
        "services.daily_joke_scheduler._resolve_beibei_chat_ids",
        AsyncMock(return_value=["100", "200"]),
    ), patch(
        "services.daily_joke_scheduler._maybe_alert_owner_redline",
        AsyncMock(return_value=None),
    ):
        n = await _send_joke_to_all(fake_bot, raw_text, ["100", "200", "999"])

    _ok("3 个 chat_id 都成功发送", n == 3)
    by_chat = dict(sent_records)
    _ok("贝贝 100 收到的不含『机器人』", "机器人" not in by_chat[100])
    _ok("贝贝 100 收到的不含『永远』", "永远" not in by_chat[100])
    _ok("贝贝 100 收到的不含『复合吧』", "复合吧" not in by_chat[100])
    _ok("贝贝 200 同样 sanitize", "机器人" not in by_chat[200])
    _ok("普通用户 999 仍是原文", by_chat[999] == raw_text)

    # 4) 告警钩子失败不阻塞发送
    sent_records.clear()
    with patch(
        "services.daily_joke_scheduler._resolve_beibei_chat_ids",
        AsyncMock(return_value=["100"]),
    ), patch(
        "services.daily_joke_scheduler._maybe_alert_owner_redline",
        AsyncMock(side_effect=RuntimeError("alert boom")),
    ):
        n = await _send_joke_to_all(fake_bot, raw_text, ["100"])
    _ok("告警失败不阻塞发送", n == 1)

    # 5) bot.send_message 失败不阻塞其他 chat
    class _PartialFail:
        def __init__(self):
            self.calls = 0
        async def send_message(self, *, chat_id, text):
            self.calls += 1
            if chat_id == 100:
                raise RuntimeError("telegram boom")

    partial = _PartialFail()
    with patch(
        "services.daily_joke_scheduler._resolve_beibei_chat_ids",
        AsyncMock(return_value=["100"]),
    ), patch(
        "services.daily_joke_scheduler._maybe_alert_owner_redline",
        AsyncMock(return_value=None),
    ):
        n = await _send_joke_to_all(partial, "ok", ["100", "200"])
    _ok("一条失败不影响另一条", n == 1)
    _ok("两个 chat 都被尝试", partial.calls == 2)

    print("ALL JOKE SANITIZE SMOKE OK")


if __name__ == "__main__":
    asyncio.run(main())
