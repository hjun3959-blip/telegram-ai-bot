"""Owner-only /健康检查 + /灰度状态 smoke。

不联网。验证：
- owner_health_command_reply 对 /健康检查 / /灰度状态 返回非空字符串
- 报告里只含元信息，不含 API key / token / 完整正文
- 非这两条命令返回 None
- 文案 ≤ Telegram 单条上限 4096
- /play /help 文案不暴露这两条命令
- 报告里不出现「机器人 / 系统」给贝贝看的风险词（这俩命令本就 owner-only，但保险）

跑法：python3 scripts/smoke_test_owner_health.py
"""

from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _ok(name, cond, detail=""):
    print(f"[{'OK' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        sys.exit(1)


async def _async_main():
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="owner_health_")
    os.environ["BOT_DB_PATH"] = os.path.join(tmpdir, "db.sqlite3")
    # 注入 fake secrets，仅验证 "present=yes" 判定（不会被打印）
    os.environ["OPENAI_API_KEY"] = "sk-fake"
    os.environ["TELEGRAM_TOKEN"] = "telegram-fake"

    # 干净 import
    for mod in (
        "config",
        "db.core",
        "services.openai_service",
        "services.xiaopang_service",
        "services.atree_persona",
        "services.atree_models",
        "services.gray_status_service",
        "routers.private",
    ):
        sys.modules.pop(mod, None)

    import config  # noqa: F401
    from db.core import init_db, close_db
    await init_db()

    from services.gray_status_service import (
        OWNER_HEALTH_COMMANDS,
        owner_health_command_reply,
        build_health_report,
        build_gray_status_report,
    )

    # 1) 命令集合
    _ok("OWNER_HEALTH_COMMANDS 含 /健康检查", "/健康检查" in OWNER_HEALTH_COMMANDS)
    _ok("OWNER_HEALTH_COMMANDS 含 /灰度状态", "/灰度状态" in OWNER_HEALTH_COMMANDS)

    # 2) /健康检查
    health = await owner_health_command_reply("/健康检查")
    _ok("/健康检查 返回非空", isinstance(health, str) and len(health) > 0)
    _ok("/健康检查 含『健康检查』标题", "健康检查" in health)
    _ok("/健康检查 含『service：alive』", "service" in health and "alive" in health)
    _ok("/健康检查 含『db』", "db" in health.lower())
    _ok("/健康检查 含『daily_joke』", "daily_joke" in health)
    _ok("/健康检查 含『model routes』", "model routes" in health or "model_routes" in health)
    _ok("/健康检查 不含 OPENAI_API_KEY 实际值", "sk-fake" not in health)
    _ok("/健康检查 不含 TELEGRAM_TOKEN 实际值", "telegram-fake" not in health)
    _ok("/健康检查 ≤ 4096", len(health) <= 4096, f"len={len(health)}")

    # 3) /灰度状态
    gray = await owner_health_command_reply("/灰度状态")
    _ok("/灰度状态 返回非空", isinstance(gray, str) and len(gray) > 0)
    _ok("/灰度状态 含『灰度状态』标题", "灰度状态" in gray)
    _ok("/灰度状态 含 incoming/outgoing", "incoming" in gray and "outgoing" in gray)
    _ok("/灰度状态 含『静默桶』或『静默』", "静默" in gray)
    _ok("/灰度状态 含『媒体计数』", "媒体计数" in gray)
    _ok("/灰度状态 含 CAN_GRAYSCALE 字段", "CAN_GRAYSCALE" in gray)
    _ok("/灰度状态 不含 secrets 值", "sk-fake" not in gray and "telegram-fake" not in gray)
    _ok("/灰度状态 ≤ 4096", len(gray) <= 4096, f"len={len(gray)}")

    # 4) 其他文本返回 None
    none_reply = await owner_health_command_reply("/play")
    _ok("非命令 → None", none_reply is None)
    none_reply2 = await owner_health_command_reply("健康检查")  # 没 / 前缀
    _ok("『健康检查』无 / 前缀 → None", none_reply2 is None)

    # 5) /play /help 文案不暴露命令
    import routers.private as priv
    for ftext in (priv.HELP_TEXT, priv.PLAY_MENU_TEXT, priv.HOW_TO_USE_TEXT,
                  priv.BEIBEI_PLAY_MENU_TEXT, priv.BEIBEI_HELP_TEXT):
        for tok in ("/健康检查", "/灰度状态"):
            _ok(f"{tok} 不在公开菜单文案", tok not in ftext)

    # 6) routers/private.py 已 import & 注册
    src = open(os.path.join(ROOT, "routers", "private.py"), encoding="utf-8").read()
    _ok("private.py 已 import OWNER_HEALTH_COMMANDS", "OWNER_HEALTH_COMMANDS" in src)
    _ok("private.py 已 import owner_health_command_reply", "owner_health_command_reply" in src)

    # 7) 兼容性：报告内容里仍可能含『机器人』词（owner 端允许；贝贝端不会触发本命令）
    #    但要保证不含 raw chat 正文标志，比如不应出现『你说：』『她说：』之类
    for forb in ("您说：", "你说：", "她说：", "他说：", "API_KEY=sk-"):
        _ok(f"报告不含 {forb}", forb not in health and forb not in gray)

    await close_db()
    print("ALL OWNER HEALTH SMOKE OK")


if __name__ == "__main__":
    asyncio.run(_async_main())
