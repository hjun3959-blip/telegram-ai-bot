"""Owner 私聊功能按钮菜单 smoke（owner-only 控制台 UI）。

不联网。验证：
- 配置开关 OWNER_MENU_ENABLED 存在；默认跟随 ADMIN_AGENT_ENABLED；可显式覆盖。
- _owner_private_msg / _owner_private_cb 门禁：非 owner / 非私聊 / 关闭时一律 False。
- 主菜单键盘包含全部入口；owner 专属用 ownmenu:* 回调，娱乐/出图复用 private 的 home:* 回调。
- callback 路由：brain/github 进会话（写入 admin_agent._active_session）、copyfix 登记 pending、
  plans/today/health 直接调对应 service 并回结果、home/help 返回文案。
- 非 owner 点回调被拦截（静默 ack，不触发任何 service）。
- app.py 已条件注册 owner_menu_router，且在 private 之前。

跑法：python3 scripts/smoke_test_owner_menu.py
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _ok(name, cond, detail=""):
    print(f"[{'OK' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        sys.exit(1)


class _FakeUser:
    def __init__(self, uid, username):
        self.id = uid
        self.username = username
        self.is_bot = False


class _FakeChat:
    def __init__(self, cid, ctype="private"):
        self.id = cid
        self.type = ctype


class _FakeMessage:
    """同时当 message（有 chat/from_user）和 callback.message（chat 是 bot 发的菜单）用。"""

    def __init__(self, text, uid, username, ctype="private", business_connection_id=None):
        self.text = text
        self.from_user = _FakeUser(uid, username)
        self.chat = _FakeChat(uid, ctype)
        self.business_connection_id = business_connection_id
        self.photo = self.video = self.sticker = self.animation = None
        self.answers: list[tuple[str, object]] = []
        self.bot = None

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


class _FakeCallback:
    def __init__(self, data, from_uid, from_username, msg):
        self.data = data
        self.from_user = _FakeUser(from_uid, from_username)
        self.message = msg
        self.bot = None
        self.acked = False

    async def answer(self, text=None):
        self.acked = True


async def _async_main():
    os.environ["OWNER_MENU_ENABLED"] = "true"
    os.environ["ADMIN_AGENT_ENABLED"] = "true"
    os.environ["OWNER_USER_IDS"] = "111"
    os.environ["OWNER_CHAT_IDS"] = "111"
    os.environ["GITHUB_REPO"] = "hjun3959-blip/telegram-ai-bot"
    os.environ["GITHUB_TOKEN"] = ""
    os.environ["OPENAI_API_KEY"] = "sk-fake"
    os.environ["TELEGRAM_TOKEN"] = "telegram-fake"

    for mod in (
        "config",
        "services.context_service",
        "services.plan_service",
        "services.gray_status_service",
        "services.pending_style_service",
        "routers.admin_agent",
        "routers.owner_menu",
    ):
        sys.modules.pop(mod, None)

    import config
    _ok("OWNER_MENU_ENABLED 读到 True", config.OWNER_MENU_ENABLED is True)

    # 默认跟随 ADMIN_AGENT_ENABLED：清掉 OWNER_MENU_ENABLED，开 admin → True；关 admin → False
    os.environ.pop("OWNER_MENU_ENABLED", None)
    os.environ["ADMIN_AGENT_ENABLED"] = "true"
    importlib.reload(config)
    _ok("默认跟随 ADMIN_AGENT_ENABLED（开）", config.OWNER_MENU_ENABLED is True)
    os.environ["ADMIN_AGENT_ENABLED"] = "false"
    importlib.reload(config)
    _ok("默认跟随 ADMIN_AGENT_ENABLED（关）", config.OWNER_MENU_ENABLED is False)
    # 显式覆盖：admin 关，但 menu 显式开 → True
    os.environ["OWNER_MENU_ENABLED"] = "true"
    importlib.reload(config)
    _ok("OWNER_MENU_ENABLED 显式覆盖为开", config.OWNER_MENU_ENABLED is True)
    os.environ["ADMIN_AGENT_ENABLED"] = "true"
    importlib.reload(config)

    # 重新加载依赖 config 的模块，确保读到最新开关
    for mod in (
        "services.context_service",
        "routers.admin_agent",
        "routers.owner_menu",
    ):
        sys.modules.pop(mod, None)
    import routers.owner_menu as om

    # ---- 门禁 _owner_private_msg ----
    owner_priv = _FakeMessage("/菜单", 111, "owner")
    stranger = _FakeMessage("/菜单", 222, "stranger")
    biz = _FakeMessage("/菜单", 111, "owner", business_connection_id="bc1")
    group = _FakeMessage("/菜单", 111, "owner", ctype="supergroup")
    _ok("owner+私聊 通过门禁", om._owner_private_msg(owner_priv) is True)
    _ok("陌生人被门禁拦截", om._owner_private_msg(stranger) is False)
    _ok("Business 上下文被拦截", om._owner_private_msg(biz) is False)
    _ok("群聊被拦截", om._owner_private_msg(group) is False)

    # 关闭时门禁 False（即便 owner 私聊）
    os.environ["OWNER_MENU_ENABLED"] = "false"
    os.environ["ADMIN_AGENT_ENABLED"] = "false"
    importlib.reload(config)
    sys.modules.pop("routers.owner_menu", None)
    import routers.owner_menu as om_off
    _ok("关闭时门禁 False", om_off._owner_private_msg(owner_priv) is False)
    # 恢复开启
    os.environ["OWNER_MENU_ENABLED"] = "true"
    os.environ["ADMIN_AGENT_ENABLED"] = "true"
    importlib.reload(config)
    sys.modules.pop("routers.owner_menu", None)
    sys.modules.pop("routers.admin_agent", None)
    import routers.owner_menu as om
    import routers.admin_agent as ra

    # ---- 键盘布局 ----
    kb = om._build_menu_keyboard()
    all_cb = [b.callback_data for row in kb.inline_keyboard for b in row]
    for expect in (
        "ownmenu:brain", "ownmenu:github", "home:make_image", "home:fun",
        "home:tools", "ownmenu:copyfix", "ownmenu:plans", "ownmenu:today",
        "ownmenu:health", "ownmenu:help",
    ):
        _ok(f"主菜单含按钮 {expect}", expect in all_cb)

    # ---- 回调门禁 _owner_private_cb ----
    menu_msg = _FakeMessage(None, 111, "ownerbot")  # callback.message 是 bot 发的菜单消息
    cb_owner = _FakeCallback("ownmenu:help", 111, "owner", menu_msg)
    cb_stranger = _FakeCallback("ownmenu:help", 222, "stranger", menu_msg)
    _ok("owner 点回调通过门禁", om._owner_private_cb(cb_owner) is True)
    _ok("陌生人点回调被拦截", om._owner_private_cb(cb_stranger) is False)

    # ---- mock services（避免联网 / DB）----
    import services.plan_service as plan_svc
    import services.gray_status_service as gray_svc

    async def _fake_list_plans(*a, **k):
        return "计划#1：写测试"

    async def _fake_today(*a, **k):
        return "今日焦点：发布灰度"

    async def _fake_health(text):
        return "🩺 健康检查\n- ok"

    plan_svc.list_plans = _fake_list_plans
    plan_svc.get_today_focus = _fake_today
    gray_svc.owner_health_command_reply = _fake_health
    om.list_plans = _fake_list_plans
    om.get_today_focus = _fake_today
    om.owner_health_command_reply = _fake_health

    # send_long_text 用 bot.send_message，测试里 bot=None，统一打桩为收集器。
    captured: list[str] = []

    async def _fake_send_long_text(bot, chat_id, text):
        captured.append(text)

    om.send_long_text = _fake_send_long_text

    # ---- callback 路由：brain ----
    captured.clear()
    ra._active_session.pop("111", None)
    msg_b = _FakeMessage(None, 111, "ownerbot")
    cb_b = _FakeCallback("ownmenu:brain", 111, "owner", msg_b)
    await om.owner_menu_callback(cb_b, bot=None)
    _ok("点主脑进入 brain 会话", ra._active_session.get("111") == "brain")
    _ok("点主脑有就绪提示", any("主脑" in t for t in captured))

    # ---- callback 路由：github ----
    captured.clear()
    ra._active_session.pop("111", None)
    msg_g = _FakeMessage(None, 111, "ownerbot")
    cb_g = _FakeCallback("ownmenu:github", 111, "owner", msg_g)
    await om.owner_menu_callback(cb_g, bot=None)
    _ok("点 GitHub 进入 github 会话", ra._active_session.get("111") == "github")

    # ---- callback 路由：copyfix 登记 pending ----
    import services.pending_style_service as pend
    pend.clear_pending_style(111)
    msg_c = _FakeMessage(None, 111, "ownerbot")
    cb_c = _FakeCallback("ownmenu:copyfix", 111, "owner", msg_c)
    await om.owner_menu_callback(cb_c, bot=None)
    p = pend.get_pending_style(111)
    _ok("点文案优化登记 copyfix pending", p is not None and p.tool == "copyfix")
    pend.clear_pending_style(111)

    # ---- callback 路由：plans / today / health 直接出结果 ----
    for data, needle in (
        ("ownmenu:plans", "计划#1"),
        ("ownmenu:today", "今日焦点"),
        ("ownmenu:health", "健康检查"),
        ("ownmenu:help", "私信控制台用法"),
    ):
        captured.clear()
        m = _FakeMessage(None, 111, "ownerbot")
        cb = _FakeCallback(data, 111, "owner", m)
        await om.owner_menu_callback(cb, bot=None)
        _ok(f"回调 {data} 出结果含「{needle}」", any(needle in t for t in captured), f"got={captured}")

    # ---- 非 owner 点回调被拦截：不写 session、不登记 pending ----
    ra._active_session.pop("999", None)
    msg_x = _FakeMessage(None, 999, "stranger")
    cb_x = _FakeCallback("ownmenu:brain", 999, "stranger", msg_x)
    await om.owner_menu_callback(cb_x, bot=None)
    _ok("非 owner 点回调被静默 ack", cb_x.acked is True)
    _ok("非 owner 点回调不进会话", "999" not in ra._active_session)

    # ---- home:* 回调不属于本 router（前缀检查）----
    _ok("home:* 不以 ownmenu: 开头（落到 private）", not "home:make_image".startswith("ownmenu:"))

    # ---- app.py 注册检查 ----
    app_src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    _ok("app.py import owner_menu_router", "owner_menu_router" in app_src)
    _ok("app.py 条件注册 owner_menu", "if OWNER_MENU_ENABLED:" in app_src)
    _ok(
        "owner_menu 在 private 之前注册",
        app_src.index("include_router(owner_menu_router)") < app_src.index("include_router(private_router)"),
    )

    print("ALL OWNER MENU SMOKE OK")


if __name__ == "__main__":
    asyncio.run(_async_main())
