"""GitHub 助手服务（owner-only，只读优先）。

能力（v1，全部只读）：
- repo status summary：默认分支、可见性、open issues/PR、最近 push、star/fork
- list PRs：最近的 open PR 列表
- list recent Actions：最近的 workflow runs（名称/状态/结论/分支）
- security alert counts：Dependabot / code-scanning / secret-scanning 告警数量
- 解释「如果让我做某个 GitHub 工作，我会怎么做」（自然语言，由主脑回答）

安全边界（硬性）：
- 任何写 / 破坏性动作（合并 PR、关告警、删分支、部署、推代码、改设置等）
  绝不自动执行。识别到这类意图时，返回“需要 owner 亲自确认/操作”的说明，不调用任何写接口。
- 不从 Telegram 执行任意 shell。
- 全部走 GitHub REST API（GITHUB_TOKEN 可选）。token 不写日志、不回显。

无 token 时仍可对话与解释；实际拉数据会受未认证速率限制，私有仓库会 404/403。
"""

from __future__ import annotations

import aiohttp

from config import GITHUB_API_BASE, GITHUB_REPO, GITHUB_TOKEN
from services.admin_brain_service import ask_admin_brain
from utils.logger import setup_logging

logger = setup_logging()

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)

# 写 / 破坏性意图关键词：命中即拒绝自动执行，返回需 owner 确认。
_WRITE_INTENT_KEYWORDS = (
    "合并", "merge", "关闭", "close", "关告警", "dismiss", "删除", "delete", "删分支",
    "部署", "deploy", "发布", "release", "推代码", "push", "提交", "commit",
    "改设置", "修改设置", "settings", "新建", "create", "rerun", "重跑", "重新运行",
    "revert", "回滚", "force", "强推",
)


def _headers() -> dict:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "telegram-ai-bot-admin-agent",
    }
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


async def _gh_get(session: aiohttp.ClientSession, path: str, params: dict | None = None):
    """GET GitHub REST API。返回 (status_code, json_or_text)。只读，永不带 body。"""
    url = f"{GITHUB_API_BASE.rstrip('/')}/{path.lstrip('/')}"
    async with session.get(url, params=params or {}) as resp:
        status = resp.status
        try:
            data = await resp.json()
        except Exception:
            data = await resp.text()
        return status, data


def _is_write_intent(text: str) -> bool:
    low = (text or "").lower()
    return any(kw.lower() in low for kw in _WRITE_INTENT_KEYWORDS)


def _no_token_note() -> str:
    if GITHUB_TOKEN:
        return ""
    return "\n（提示：未配置 GITHUB_TOKEN，只读数据受未认证速率限制，私有仓库可能取不到。）"


async def _repo_status(session) -> str:
    status, repo = await _gh_get(session, f"repos/{GITHUB_REPO}")
    if status != 200 or not isinstance(repo, dict):
        return f"拉取仓库状态失败（HTTP {status}）。{_describe_error(repo)}{_no_token_note()}"
    lines = [
        f"仓库：{repo.get('full_name', GITHUB_REPO)}",
        f"可见性：{repo.get('visibility', 'private' if repo.get('private') else 'public')}",
        f"默认分支：{repo.get('default_branch', '?')}",
        f"open issues+PR：{repo.get('open_issues_count', '?')}",
        f"star：{repo.get('stargazers_count', 0)} / fork：{repo.get('forks_count', 0)}",
        f"最近 push：{repo.get('pushed_at', '?')}",
    ]
    return "【仓库状态】\n" + "\n".join(lines) + _no_token_note()


async def _list_prs(session, limit: int = 10) -> str:
    status, prs = await _gh_get(
        session, f"repos/{GITHUB_REPO}/pulls",
        params={"state": "open", "per_page": str(limit), "sort": "updated", "direction": "desc"},
    )
    if status != 200 or not isinstance(prs, list):
        return f"拉取 PR 列表失败（HTTP {status}）。{_describe_error(prs)}{_no_token_note()}"
    if not prs:
        return "【open PR】当前没有打开的 PR。" + _no_token_note()
    lines = [f"#{p.get('number')} {p.get('title', '').strip()} (by {p.get('user', {}).get('login', '?')})"
             for p in prs[:limit]]
    return f"【open PR · {len(prs)} 条】\n" + "\n".join(lines) + _no_token_note()


async def _list_actions(session, limit: int = 8) -> str:
    status, data = await _gh_get(
        session, f"repos/{GITHUB_REPO}/actions/runs", params={"per_page": str(limit)},
    )
    if status != 200 or not isinstance(data, dict):
        return f"拉取 Actions 失败（HTTP {status}）。{_describe_error(data)}{_no_token_note()}"
    runs = data.get("workflow_runs") or []
    if not runs:
        return "【最近 Actions】没有 workflow 运行记录。" + _no_token_note()
    lines = [
        f"{r.get('name', '?')} · {r.get('status', '?')}/{r.get('conclusion') or '进行中'} "
        f"· {r.get('head_branch', '?')} · {r.get('created_at', '?')}"
        for r in runs[:limit]
    ]
    return f"【最近 Actions · {len(lines)} 条】\n" + "\n".join(lines) + _no_token_note()


async def _security_alert_counts(session) -> str:
    out = []
    for label, path in (
        ("Dependabot", f"repos/{GITHUB_REPO}/dependabot/alerts"),
        ("Code scanning", f"repos/{GITHUB_REPO}/code-scanning/alerts"),
        ("Secret scanning", f"repos/{GITHUB_REPO}/secret-scanning/alerts"),
    ):
        status, data = await _gh_get(session, path, params={"state": "open", "per_page": "100"})
        if status == 200 and isinstance(data, list):
            out.append(f"{label}：{len(data)} 条 open")
        elif status in (403, 404):
            out.append(f"{label}：无权限或未启用（HTTP {status}）")
        else:
            out.append(f"{label}：查询失败（HTTP {status}）")
    return "【安全告警】\n" + "\n".join(out) + _no_token_note()


def _describe_error(data) -> str:
    if isinstance(data, dict):
        msg = data.get("message")
        if msg:
            return str(msg)
    return ""


async def handle_github_message(owner_key: str | int, user_text: str) -> str:
    """GitHub 助手主入口：自然语言进，自然语言出。

    路由顺序：
    1. 写/破坏性意图 → 拒绝自动执行，返回需 owner 确认。
    2. 命中只读意图关键词 → 调对应 REST API 汇总。
    3. 其它 → 交给主脑，用 GitHub 工程顾问口吻自然作答（解释「我会怎么做」）。
    """
    text = (user_text or "").strip()
    if not text:
        return (f"（GitHub 助手）当前仓库 {GITHUB_REPO}。可以问我：仓库状态 / open PR / "
                "最近 Actions / 安全告警，或描述你想做的 GitHub 工作。")

    low = text.lower()

    if _is_write_intent(text):
        return (
            "（GitHub 助手）这是写入/破坏性操作（如合并 PR、关告警、删分支、部署、推代码、改设置）。\n"
            "出于安全，v1 不会自动执行，需要你本人确认并亲自操作。\n"
            f"我可以先帮你：说明具体步骤、列出受影响范围、或汇总 {GITHUB_REPO} 当前状态供你决策。"
        )

    wants_status = any(k in low for k in ("仓库状态", "状态", "status", "概况", "summary"))
    wants_pr = any(k in low for k in ("pr", "pull request", "拉取请求", "合并请求列表"))
    wants_actions = any(k in low for k in ("action", "workflow", "ci", "构建", "流水线", "工作流"))
    wants_security = any(k in low for k in ("安全", "告警", "alert", "security", "漏洞", "dependabot"))

    try:
        async with aiohttp.ClientSession(headers=_headers(), timeout=_HTTP_TIMEOUT) as session:
            parts: list[str] = []
            if wants_security:
                parts.append(await _security_alert_counts(session))
            if wants_actions:
                parts.append(await _list_actions(session))
            if wants_pr and not wants_status:
                parts.append(await _list_prs(session))
            if wants_status:
                parts.append(await _repo_status(session))
                if wants_pr:
                    parts.append(await _list_prs(session))
            if parts:
                return "\n\n".join(parts)
    except Exception as e:
        logger.exception("github helper REST failed | err_type=%s", type(e).__name__)
        return f"（GitHub 助手）访问 GitHub 出错了：{type(e).__name__}。稍后再试。{_no_token_note()}"

    # 没命中具体只读意图 → 让主脑以 GitHub 工程顾问口吻回答（含「我会怎么做」类问题）。
    consult_prompt = (
        f"你现在是 GitHub 工程助手，聚焦仓库 {GITHUB_REPO}。owner 的问题：\n{text}\n\n"
        "请用自然语言回答。如果是只读查询类（仓库状态/PR/Actions/安全告警），"
        "告诉他可以直接说『仓库状态』『open PR』『最近 Actions』『安全告警』来拉实时数据。"
        "如果涉及写入/部署/合并等动作，明确说明这些需要他本人确认后亲自执行，并给出你建议的步骤。"
    )
    return await ask_admin_brain(f"github:{owner_key}", consult_prompt)
