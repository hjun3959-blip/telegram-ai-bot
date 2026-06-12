"""自动化工作调度器（Automation Scheduler）。

为主脑提供后台任务调度能力。

功能：
- 创建和管理定时任务
- 支持一次性任务和循环任务
- 任务持久化（存储到数据库）
- 任务执行日志和状态跟踪

设计要点：
- 基于 asyncio，不引入额外依赖（如 APScheduler）
- 任务状态存储在 meta 表中
- 支持 owner 通过主脑查询和管理任务
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from config import OWNER_CHAT_IDS
from db.core import execute, fetchall, fetchone
from utils.logger import setup_logging

logger = setup_logging()

# 任务状态常量
TASK_STATUS_PENDING = "pending"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELLED = "cancelled"

# 任务类型
TASK_TYPE_ONCE = "once"  # 一次性任务
TASK_TYPE_RECURRING = "recurring"  # 循环任务


class AutomationTask:
    """自动化任务定义。"""
    
    def __init__(
        self,
        task_id: str,
        name: str,
        description: str,
        task_type: str,
        trigger_time: str,  # ISO 8601 格式或 cron 表达式
        action: str,  # 任务动作描述
        status: str = TASK_STATUS_PENDING,
        created_at: Optional[str] = None,
        last_run: Optional[str] = None,
        next_run: Optional[str] = None,
    ):
        self.task_id = task_id
        self.name = name
        self.description = description
        self.task_type = task_type
        self.trigger_time = trigger_time
        self.action = action
        self.status = status
        self.created_at = created_at or datetime.now(ZoneInfo("UTC")).isoformat()
        self.last_run = last_run
        self.next_run = next_run
    
    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "description": self.description,
            "task_type": self.task_type,
            "trigger_time": self.trigger_time,
            "action": self.action,
            "status": self.status,
            "created_at": self.created_at,
            "last_run": self.last_run,
            "next_run": self.next_run,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> AutomationTask:
        return cls(**data)


async def _get_meta_key(suffix: str) -> str:
    """生成 meta key。"""
    return f"automation:{suffix}"


async def create_task(
    name: str,
    description: str,
    task_type: str,
    trigger_time: str,
    action: str,
) -> dict:
    """创建新任务。
    
    Args:
        name: 任务名称
        description: 任务描述
        task_type: 任务类型（once / recurring）
        trigger_time: 触发时间（ISO 8601 或 cron）
        action: 任务动作
        
    Returns:
        {"ok": bool, "task_id": str, "error": str}
    """
    try:
        import uuid
        task_id = str(uuid.uuid4())[:8]
        
        task = AutomationTask(
            task_id=task_id,
            name=name,
            description=description,
            task_type=task_type,
            trigger_time=trigger_time,
            action=action,
        )
        
        # 存储到 meta 表
        key = await _get_meta_key(f"task:{task_id}")
        await execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(task.to_dict())),
        )
        
        logger.info(
            "automation_task_created | task_id=%s | name=%s | type=%s",
            task_id, name, task_type
        )
        
        return {"ok": True, "task_id": task_id, "error": ""}
    
    except Exception as e:
        logger.exception("create_task failed | err_type=%s", type(e).__name__)
        return {"ok": False, "task_id": "", "error": str(e)}


async def get_task(task_id: str) -> Optional[AutomationTask]:
    """获取任务详情。"""
    try:
        key = await _get_meta_key(f"task:{task_id}")
        row = await fetchone("SELECT value FROM meta WHERE key=?", (key,))
        if row:
            data = json.loads(row["value"])
            return AutomationTask.from_dict(data)
        return None
    except Exception as e:
        logger.exception("get_task failed | task_id=%s | err_type=%s", task_id, type(e).__name__)
        return None


async def list_tasks(status: Optional[str] = None) -> list[AutomationTask]:
    """列出所有任务。
    
    Args:
        status: 按状态过滤（可选）
        
    Returns:
        任务列表
    """
    try:
        rows = await fetchall(
            "SELECT value FROM meta WHERE key LIKE 'automation:task:%'",
        )
        
        tasks = []
        for row in rows:
            try:
                data = json.loads(row["value"])
                task = AutomationTask.from_dict(data)
                if status is None or task.status == status:
                    tasks.append(task)
            except Exception:
                continue
        
        return tasks
    except Exception as e:
        logger.exception("list_tasks failed | err_type=%s", type(e).__name__)
        return []


async def update_task_status(task_id: str, new_status: str) -> bool:
    """更新任务状态。"""
    try:
        task = await get_task(task_id)
        if not task:
            return False
        
        task.status = new_status
        if new_status == TASK_STATUS_RUNNING:
            task.last_run = datetime.now(ZoneInfo("UTC")).isoformat()
        
        key = await _get_meta_key(f"task:{task_id}")
        await execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(task.to_dict())),
        )
        
        logger.info(
            "automation_task_status_updated | task_id=%s | status=%s",
            task_id, new_status
        )
        
        return True
    except Exception as e:
        logger.exception(
            "update_task_status failed | task_id=%s | err_type=%s",
            task_id, type(e).__name__
        )
        return False


async def delete_task(task_id: str) -> bool:
    """删除任务。"""
    try:
        key = await _get_meta_key(f"task:{task_id}")
        await execute("DELETE FROM meta WHERE key=?", (key,))
        
        logger.info("automation_task_deleted | task_id=%s", task_id)
        return True
    except Exception as e:
        logger.exception("delete_task failed | task_id=%s | err_type=%s", task_id, type(e).__name__)
        return False


async def build_tasks_summary() -> str:
    """生成任务摘要（供主脑查询）。"""
    try:
        tasks = await list_tasks()
        
        if not tasks:
            return "暂无任何自动化任务。"
        
        summary = f"当前共有 {len(tasks)} 个自动化任务：\n\n"
        
        for task in tasks:
            summary += f"【{task.name}】\n"
            summary += f"  ID: {task.task_id}\n"
            summary += f"  状态: {task.status}\n"
            summary += f"  类型: {task.task_type}\n"
            summary += f"  触发: {task.trigger_time}\n"
            summary += f"  描述: {task.description}\n"
            if task.last_run:
                summary += f"  上次运行: {task.last_run}\n"
            summary += "\n"
        
        return summary.strip()
    except Exception as e:
        logger.exception("build_tasks_summary failed | err_type=%s", type(e).__name__)
        return f"获取任务摘要失败：{type(e).__name__}"


class AutomationScheduler:
    """后台任务调度器。
    
    用法：
        scheduler = AutomationScheduler()
        scheduler.start()
        ...
        await scheduler.stop()
    """
    
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
    
    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()
    
    def start(self) -> None:
        """启动调度器。"""
        if self.is_running:
            logger.info("automation scheduler: already running")
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="automation-scheduler")
        logger.info("automation scheduler started")
    
    async def stop(self) -> None:
        """停止调度器。"""
        if not self._task:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None
        logger.info("automation scheduler stopped")
    
    async def _run_loop(self) -> None:
        """主循环：每分钟检查一次待执行的任务。"""
        try:
            while not self._stop_event.is_set():
                await self._check_and_execute_tasks()
                # 每分钟检查一次
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.exception("automation scheduler loop crashed | err=%s", e)
    
    async def _check_and_execute_tasks(self) -> None:
        """检查并执行待执行的任务。"""
        try:
            tasks = await list_tasks(status=TASK_STATUS_PENDING)
            
            now = datetime.now(ZoneInfo("UTC"))
            
            for task in tasks:
                try:
                    # 简单的时间比较（生产环境应使用更复杂的 cron 解析）
                    trigger_dt = datetime.fromisoformat(task.trigger_time)
                    
                    if trigger_dt <= now:
                        await self._execute_task(task)
                except Exception as e:
                    logger.warning(
                        "check_task failed | task_id=%s | err=%s",
                        task.task_id, type(e).__name__
                    )
        except Exception as e:
            logger.exception("check_and_execute_tasks failed | err=%s", e)
    
    async def _execute_task(self, task: AutomationTask) -> None:
        """执行单个任务。"""
        try:
            await update_task_status(task.task_id, TASK_STATUS_RUNNING)
            
            # 这里可以根据 task.action 执行不同的操作
            # 示例：发送通知、执行 API 调用等
            logger.info(
                "automation_task_executing | task_id=%s | name=%s | action=%s",
                task.task_id, task.name, task.action
            )
            
            # 任务完成
            await update_task_status(task.task_id, TASK_STATUS_COMPLETED)
        
        except Exception as e:
            logger.exception(
                "execute_task failed | task_id=%s | err_type=%s",
                task.task_id, type(e).__name__
            )
            await update_task_status(task.task_id, TASK_STATUS_FAILED)
