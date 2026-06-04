import json
from datetime import datetime

from db.core import execute, fetchall, fetchone


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


async def create_plan(title: str, category: str = "general", priority: int = 3, due_date: str | None = None) -> int:
    ts = now_str()
    await execute(
        "INSERT INTO plans(title, category, status, priority, due_date, owner, created_at, updated_at) VALUES(?, ?, 'todo', ?, ?, 'owner', ?, ?)",
        (title.strip(), category, priority, due_date, ts, ts),
    )
    row = await fetchone("SELECT id FROM plans ORDER BY id DESC LIMIT 1")
    return int(row["id"])


async def list_plans(status: str | None = None, limit: int = 20) -> str:
    if status:
        rows = await fetchall("SELECT * FROM plans WHERE status=? ORDER BY priority ASC, id DESC LIMIT ?", (status, limit))
    else:
        rows = await fetchall("SELECT * FROM plans ORDER BY status ASC, priority ASC, id DESC LIMIT ?", (limit,))
    if not rows:
        return "还没有计划。"
    lines = ["计划列表"]
    for row in rows:
        lines.append(f"#{row['id']} | {row['status']} | P{row['priority']} | {row['title']}")
    return "\n".join(lines)


async def get_plan_detail(plan_id: int) -> str:
    row = await fetchone("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not row:
        return "没找到这个计划。"
    events = await fetchall("SELECT * FROM plan_events WHERE plan_id=? ORDER BY id DESC LIMIT 10", (plan_id,))
    lines = [
        f"计划 #{row['id']}",
        f"标题：{row['title']}",
        f"分类：{row['category']}",
        f"状态：{row['status']}",
        f"优先级：P{row['priority']}",
        f"截止：{row['due_date'] or '无'}",
    ]
    if events:
        lines.append("最近进展：")
        for e in events:
            lines.append(f"- {e['created_at']} | {e['event_type']} | {e['content'][:80]}")
    return "\n".join(lines)


async def update_plan_status(plan_id: int, status: str) -> str:
    row = await fetchone("SELECT id, title FROM plans WHERE id=?", (plan_id,))
    if not row:
        return "没找到这个计划。"
    await execute("UPDATE plans SET status=?, updated_at=? WHERE id=?", (status, now_str(), plan_id))
    await add_plan_event(plan_id, "status", f"状态更新为 {status}")
    return f"已更新计划 #{plan_id} 为 {status}"


async def add_plan_event(plan_id: int, event_type: str, content: str, source_chat_id: str | None = None, source_message_id: str | None = None):
    await execute(
        "INSERT INTO plan_events(plan_id, event_type, content, source_chat_id, source_message_id, created_at) VALUES(?, ?, ?, ?, ?, ?)",
        (plan_id, event_type, content, source_chat_id, source_message_id, now_str()),
    )


async def set_daily_focus(focus_text: str, top_tasks: list[str] | None = None, mood_note: str = "") -> str:
    await execute(
        "INSERT INTO daily_focus(day, focus_text, top_tasks_json, mood_note, updated_at) VALUES(?, ?, ?, ?, ?) ON CONFLICT(day) DO UPDATE SET focus_text=excluded.focus_text, top_tasks_json=excluded.top_tasks_json, mood_note=excluded.mood_note, updated_at=excluded.updated_at",
        (today_str(), focus_text.strip(), json.dumps(top_tasks or [], ensure_ascii=False), mood_note.strip(), now_str()),
    )
    return "今日计划焦点已更新。"


async def get_today_focus() -> str:
    row = await fetchone("SELECT * FROM daily_focus WHERE day=?", (today_str(),))
    if not row:
        return "今天还没设定焦点。"
    tasks = json.loads(row["top_tasks_json"] or "[]")
    lines = [f"今日焦点（{row['day']}）", f"重点：{row['focus_text'] or '无'}"]
    if tasks:
        lines.append("任务：")
        lines.extend([f"- {t}" for t in tasks])
    if row["mood_note"]:
        lines.append(f"备注：{row['mood_note']}")
    return "\n".join(lines)

