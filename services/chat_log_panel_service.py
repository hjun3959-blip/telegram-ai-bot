"""全局聊天记录采集面板服务（仅限 owner，唯一性保护）。

功能定位：
- 只有 OWNER_USERNAMES（@jinlid、@pay9l）能触发，任何其他用户静默拒绝。
- 从 message_log 表按 scope/chat_id 分组，动态生成「每个用户一个按钮」的 InlineKeyboard。
- 点击具体用户按钮可查看该用户最近 N 条收发记录，分页翻页。
- 提供唯一性保护：同一 chat_id 只存一份「进入面板」状态，避免重复弹菜单。
- 命令入口：/采集记录  /chatlog  /全部记录
"""

from __future__ import annotations

from datetime import datetime

from db.core import fetchall, fetchone

# ── 每页显示条数 ──────────────────────────────────────────────
PAGE_SIZE = 20
# 用户按钮最多显示多少个（超出时分页）
USER_BTN_PAGE_SIZE = 10


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ─────────────────────────────────────────────────────────────
# 1. 采集所有有记录的用户列表（分组聚合）
# ─────────────────────────────────────────────────────────────

async def list_known_scopes(offset: int = 0) -> list[dict]:
    """按 scope 分组，返回每个用户的摘要：scope、chat_id、最新消息时间、消息总数。"""
    rows = await fetchall(
        """
        SELECT
            scope,
            chat_id,
            MAX(ts) AS last_ts,
            COUNT(*) AS total
        FROM message_log
        GROUP BY scope, chat_id
        ORDER BY last_ts DESC
        LIMIT ? OFFSET ?
        """,
        (USER_BTN_PAGE_SIZE, offset),
    )
    return [dict(r) for r in rows] if rows else []


async def count_known_scopes() -> int:
    """有记录的用户（scope+chat_id 组合）总数。"""
    row = await fetchone(
        "SELECT COUNT(DISTINCT scope || ':' || chat_id) AS cnt FROM message_log"
    )
    return row["cnt"] if row else 0


# ─────────────────────────────────────────────────────────────
# 2. 拉取某个用户的具体记录
# ─────────────────────────────────────────────────────────────

async def get_user_log(
    scope: str,
    chat_id: str | None = None,
    limit: int = PAGE_SIZE,
    offset: int = 0,
) -> list[dict]:
    """拉取指定 scope（+ 可选 chat_id）的消息记录，倒序（最新在前）。"""
    if chat_id:
        rows = await fetchall(
            """
            SELECT ts, direction, content_type, content_text
            FROM message_log
            WHERE scope=? AND chat_id=?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (scope, chat_id, limit, offset),
        )
    else:
        rows = await fetchall(
            """
            SELECT ts, direction, content_type, content_text
            FROM message_log
            WHERE scope=?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (scope, limit, offset),
        )
    return [dict(r) for r in rows] if rows else []


async def count_user_log(scope: str, chat_id: str | None = None) -> int:
    if chat_id:
        row = await fetchone(
            "SELECT COUNT(*) AS cnt FROM message_log WHERE scope=? AND chat_id=?",
            (scope, chat_id),
        )
    else:
        row = await fetchone(
            "SELECT COUNT(*) AS cnt FROM message_log WHERE scope=?", (scope,)
        )
    return row["cnt"] if row else 0


# ─────────────────────────────────────────────────────────────
# 3. 格式化输出文本
# ─────────────────────────────────────────────────────────────

def _role_label(direction: str) -> str:
    return "↓用户" if direction == "incoming" else "↑机器人"


def format_user_log_text(
    scope: str,
    rows: list[dict],
    offset: int,
    total: int,
) -> str:
    """把记录列表格式化成可读文本。"""
    if not rows:
        return f"📭 [{scope}] 暂无记录。"
    lines = [f"📋 用户记录：{scope}  （第 {offset+1}–{offset+len(rows)} 条 / 共 {total} 条）"]
    lines.append("─" * 28)
    for r in rows:
        role = _role_label(r.get("direction", ""))
        ts = (r.get("ts") or "")[:16]
        ctype = r.get("content_type") or "text"
        txt = (r.get("content_text") or "").strip().replace("\n", " ")
        preview = txt[:80] + ("…" if len(txt) > 80 else "")
        if ctype != "text":
            preview = f"[{ctype}] {preview}"
        lines.append(f"{ts}  {role}  {preview}")
    return "\n".join(lines)


def format_panel_header(total_users: int, page: int, total_pages: int) -> str:
    return (
        f"📊 全局聊天记录面板\n"
        f"共 {total_users} 个用户有记录  第 {page+1}/{max(total_pages,1)} 页\n"
        f"点击用户按钮查看该用户最近记录 👇"
    )


# ─────────────────────────────────────────────────────────────
# 4. 构建 InlineKeyboard（动态，按用户生成按钮）
# ─────────────────────────────────────────────────────────────

def build_panel_keyboard(
    scopes: list[dict],
    current_page: int,
    total_users: int,
) -> list[list[dict]]:
    """返回二维 [[{text, callback_data}]] 结构，供 router 层组装成 InlineKeyboardMarkup。

    callback_data 格式：
      chatlog:user:<scope>:<chat_id>:0      # 查看某用户第 0 页
      chatlog:panel:<page>                  # 切换面板页
    """
    rows: list[list[dict]] = []

    # 每个已知 scope 生成一个按钮，两列并排
    pair: list[dict] = []
    for item in scopes:
        scope = item.get("scope") or "unknown"
        chat_id = str(item.get("chat_id") or "")
        total = item.get("total", 0)
        last = (item.get("last_ts") or "")[:10]
        label = f"{scope}  {total}条  {last}"
        cb = f"chatlog:user:{scope}:{chat_id}:0"
        pair.append({"text": label, "callback_data": cb})
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)

    # 翻页行
    total_pages = max(1, (total_users + USER_BTN_PAGE_SIZE - 1) // USER_BTN_PAGE_SIZE)
    nav: list[dict] = []
    if current_page > 0:
        nav.append({"text": "⬅️ 上一页", "callback_data": f"chatlog:panel:{current_page-1}"})
    if current_page < total_pages - 1:
        nav.append({"text": "下一页 ➡️", "callback_data": f"chatlog:panel:{current_page+1}"})
    if nav:
        rows.append(nav)

    # 关闭按钮
    rows.append([{"text": "✖️ 关闭", "callback_data": "chatlog:close"}])
    return rows


def build_user_log_keyboard(
    scope: str,
    chat_id: str,
    offset: int,
    total: int,
    panel_page: int = 0,
) -> list[list[dict]]:
    """单用户记录翻页键盘。"""
    rows: list[list[dict]] = []
    nav: list[dict] = []
    if offset > 0:
        prev_offset = max(0, offset - PAGE_SIZE)
        nav.append({"text": "⬅️ 更早", "callback_data": f"chatlog:user:{scope}:{chat_id}:{prev_offset}"})
    if offset + PAGE_SIZE < total:
        nav.append({"text": "更新 ➡️", "callback_data": f"chatlog:user:{scope}:{chat_id}:{offset+PAGE_SIZE}"})
    if nav:
        rows.append(nav)
    rows.append([
        {"text": "⬅️ 返回用户列表", "callback_data": f"chatlog:panel:{panel_page}"},
        {"text": "✖️ 关闭", "callback_data": "chatlog:close"},
    ])
    return rows
