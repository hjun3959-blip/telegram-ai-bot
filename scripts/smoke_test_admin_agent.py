"""管理员对话网关 smoke（owner-only 主脑 + GitHub 助手）。

不联网。验证：
- 配置开关 ADMIN_AGENT_ENABLED / GITHUB_REPO 存在且默认安全（默认关闭）。
- _owner_private 门禁：非 owner / 非私聊 / 关闭时一律 False。
- 主脑：ask_admin_brain 走 call_openai(response_json=False)，返回自然语言；维护历史。
- GitHub 助手：写/破坏性意图被拒绝（不调用任何接口），只读意图汇总成功。
- 命令集合 / 别名 / 退出命令齐全。
- app.py 已条件注册 admin_agent_router，且在 private 之前。

跑法：python3 scripts/smoke_test_admin_agent.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import types

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
    def __init__(self, text, uid, username, ctype="private", business_connection_id=None):
        self.text = text
        self.from_user = _FakeUser(uid, username)
        self.chat = _FakeChat(uid, ctype)
        self.business_connection_id = business_connection_id
        self.photo = self.video = self.sticker = self.animation = None


async def _async_main():
    # owner 身份与开关：开启网关，固定 owner id
    os.environ["ADMIN_AGENT_ENABLED"] = "true"
    os.environ["OWNER_USER_IDS"] = "111"
    os.environ["OWNER_CHAT_IDS"] = "111"
    os.environ["GITHUB_REPO"] = "hjun3959-blip/telegram-ai-bot"
    os.environ["GITHUB_TOKEN"] = ""
    os.environ["OPENAI_API_KEY"] = "sk-fake"
    os.environ["TELEGRAM_TOKEN"] = "telegram-fake"

    for mod in (
        "config",
        "services.openai_service",
        "services.admin_brain_service",
        "services.github_helper_service",
        "services.context_service",
        "routers.admin_agent",
    ):
        sys.modules.pop(mod, None)

    import config
    _ok("ADMIN_AGENT_ENABLED 读到 True", config.ADMIN_AGENT_ENABLED is True)
    _ok("GITHUB_REPO 默认本仓库", config.GITHUB_REPO == "hjun3959-blip/telegram-ai-bot")
    _ok("ADMIN_BRAIN_SYSTEM_PROMPT 非空", isinstance(config.ADMIN_BRAIN_SYSTEM_PROMPT, str) and len(config.ADMIN_BRAIN_SYSTEM_PROMPT) > 10)

    # 默认安全：在干净环境下（无 env）应为关闭
    import importlib
    saved = os.environ.pop("ADMIN_AGENT_ENABLED", None)
    importlib.reload(config)
    _ok("ADMIN_AGENT_ENABLED 默认关闭", config.ADMIN_AGENT_ENABLED is False)
    os.environ["ADMIN_AGENT_ENABLED"] = saved or "true"
    importlib.reload(config)

    # --- mock OpenAI：避免联网 ---
    import services.openai_service as oai

    async def _fake_call_openai(messages, model, mode, response_json=True, chat_id=None):
        _ok("call_openai 用 response_json=False", response_json is False)
        # 回显最后一条 user，确认历史/prompt 串起来了
        last_user = [m for m in messages if m["role"] == "user"][-1]["content"]
        return f"主脑收到：{last_user[:20]}"

    oai.call_openai = _fake_call_openai

    import services.admin_brain_service as brain
    brain.call_openai = _fake_call_openai

    # 主脑：单条
    reply = await brain.ask_admin_brain(111, "帮我想想灰度方案")
    _ok("主脑返回自然语言", isinstance(reply, str) and reply.startswith("主脑收到："))
    # 历史累积
    await brain.ask_admin_brain(111, "继续")
    hist = brain._get_history("111")
    _ok("主脑历史累积 4 条（2 轮）", len(hist) == 4, f"len={len(hist)}")
    brain.reset_history(111)
    _ok("主脑历史可重置", len(brain._get_history("111")) == 0)

    # 空输入兜底
    empty = await brain.ask_admin_brain(111, "   ")
    _ok("主脑空输入有兜底", isinstance(empty, str) and len(empty) > 0)

    # --- GitHub 助手 ---
    import services.github_helper_service as gh

    # 写/破坏性意图：必须拒绝，且不触发任何 HTTP（用桩 session 断言不被调用）
    class _NoCallSession:
        async def __aenter__(self):
            raise AssertionError("write-intent must NOT open HTTP session")

        async def __aexit__(self, *a):
            return False

    gh.aiohttp = types.SimpleNamespace(
        ClientSession=lambda *a, **k: _NoCallSession(),
        ClientTimeout=lambda **k: None,
    )
    for write_text in ("帮我合并 PR #3", "merge this PR", "删分支 feature/x", "部署到生产", "push 代码"):
        r = await gh.handle_github_message(111, write_text)
        _ok(f"写意图被拒绝：{write_text}", "不会自动执行" in r and "确认" in r)

    # 只读意图：用桩 _gh_get 返回假数据
    async def _fake_gh_get(session, path, params=None):
        if path.endswith(config.GITHUB_REPO) or path == f"repos/{config.GITHUB_REPO}":
            return 200, {"full_name": config.GITHUB_REPO, "default_branch": "master",
                         "private": False, "open_issues_count": 2, "pushed_at": "2026-06-06T00:00:00Z"}
        if path.endswith("/pulls"):
            return 200, [{"number": 9, "title": "feat: x", "user": {"login": "alice"}}]
        if path.endswith("/actions/runs"):
            return 200, {"workflow_runs": [{"name": "CI", "status": "completed",
                         "conclusion": "success", "head_branch": "master", "created_at": "2026-06-06"}]}
        if "alerts" in path:
            return 200, [{"x": 1}, {"x": 2}]
        return 404, {"message": "not found"}

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    gh.aiohttp = types.SimpleNamespace(
        ClientSession=lambda *a, **k: _FakeSession(),
        ClientTimeout=lambda **k: None,
    )
    gh._gh_get = _fake_gh_get

    status_reply = await gh.handle_github_message(111, "仓库状态")
    _ok("GitHub 仓库状态汇总", "仓库状态" in status_reply and config.GITHUB_REPO in status_reply)
    pr_reply = await gh.handle_github_message(111, "列一下 open PR")
    _ok("GitHub PR 列表", "#9" in pr_reply)
    act_reply = await gh.handle_github_message(111, "最近 actions 怎么样")
    _ok("GitHub Actions 汇总", "CI" in act_reply)
    sec_reply = await gh.handle_github_message(111, "有哪些安全告警")
    _ok("GitHub 安全告警计数", "Dependabot" in sec_reply)

    # --- router 门禁 ---
    import routers.admin_agent as ra
    owner_priv = _FakeMessage("/主脑 hi", 111, "owner")
    stranger = _FakeMessage("/主脑 hi", 222, "stranger")
    biz = _FakeMessage("/主脑 hi", 111, "owner", ctype="private", business_connection_id="bc1")
    group = _FakeMessage("/主脑 hi", 111, "owner", ctype="supergroup")

    _ok("owner+私聊 通过门禁", ra._owner_private(owner_priv) is True)
    _ok("陌生人被门禁拦截", ra._owner_private(stranger) is False)
    _ok("Business 上下文被拦截", ra._owner_private(biz) is False)
    _ok("群聊被拦截", ra._owner_private(group) is False)

    _ok("主脑命令集合", ra._BRAIN_CMDS == {"/主脑", "/openai", "/brain"})
    _ok("github 命令集合", ra._GITHUB_CMDS == {"/github", "/gh", "/git"})
    _ok("退出命令集合", ra._EXIT_CMDS == {"/退出", "/exit", "/quit", "/q"})
    _ok("_arg_after_command 解析参数", ra._arg_after_command("/主脑 帮我看看") == "帮我看看")
    _ok("_arg_after_command 无参数空串", ra._arg_after_command("/主脑") == "")

    # 关闭网关时门禁应 False（即便 owner 私聊）
    saved2 = os.environ["ADMIN_AGENT_ENABLED"]
    os.environ["ADMIN_AGENT_ENABLED"] = "false"
    importlib.reload(config)
    sys.modules.pop("routers.admin_agent", None)
    import routers.admin_agent as ra2
    _ok("关闭时门禁 False", ra2._owner_private(owner_priv) is False)
    os.environ["ADMIN_AGENT_ENABLED"] = saved2

    # --- app.py 注册检查 ---
    app_src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    _ok("app.py import admin_agent_router", "admin_agent_router" in app_src)
    _ok("app.py 条件注册", "if ADMIN_AGENT_ENABLED:" in app_src)
    # admin_agent 注册在 private 之前
    _ok(
        "admin_agent 在 private 之前注册",
        app_src.index("include_router(admin_agent_router)") < app_src.index("include_router(private_router)"),
    )

    print("ALL ADMIN AGENT SMOKE OK")


if __name__ == "__main__":
    asyncio.run(_async_main())
