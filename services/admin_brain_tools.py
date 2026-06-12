"""管理员主脑工具调用模块（Admin Brain Tools）。

为主脑提供可调用的工具集合，包括：
- 联网搜索
- 自动化任务管理
- 代码执行（受限）

设计要点：
- 工具调用通过自然语言触发
- 所有工具调用都有权限检查
- 执行结果返回给主脑进行下一步处理
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

from services.automation_scheduler import (
    build_tasks_summary,
    create_task,
    delete_task,
    get_task,
    list_tasks,
    update_task_status,
)
from services.web_search_service import fetch_webpage, search_and_summarize, search_web
from utils.logger import setup_logging
import subprocess
import os

logger = setup_logging()

# 工具注册表
_TOOLS_REGISTRY: dict[str, Callable] = {}


def register_tool(name: str):
    """装饰器：注册一个工具。"""
    def decorator(func: Callable) -> Callable:
        _TOOLS_REGISTRY[name] = func
        return func
    return decorator


# ===== 搜索工具 =====

@register_tool("search_web")
async def tool_search_web(query: str, max_results: int = 5) -> dict:
    """搜索网页。
    
    Args:
        query: 搜索关键词
        max_results: 最多返回结果数
        
    Returns:
        搜索结果字典
    """
    try:
        result = await search_web(query, max_results)
        return {
            "ok": True,
            "tool": "search_web",
            "result": result,
        }
    except Exception as e:
        logger.exception("tool_search_web failed | err_type=%s", type(e).__name__)
        return {
            "ok": False,
            "tool": "search_web",
            "error": str(e),
        }


@register_tool("fetch_webpage")
async def tool_fetch_webpage(url: str) -> dict:
    """抓取网页内容。
    
    Args:
        url: 网页 URL
        
    Returns:
        网页内容字典
    """
    try:
        result = await fetch_webpage(url)
        return {
            "ok": True,
            "tool": "fetch_webpage",
            "result": result,
        }
    except Exception as e:
        logger.exception("tool_fetch_webpage failed | err_type=%s", type(e).__name__)
        return {
            "ok": False,
            "tool": "fetch_webpage",
            "error": str(e),
        }


@register_tool("search_and_summarize")
async def tool_search_and_summarize(query: str) -> dict:
    """搜索并生成摘要。
    
    Args:
        query: 搜索关键词
        
    Returns:
        摘要字典
    """
    try:
        summary = await search_and_summarize(query)
        return {
            "ok": True,
            "tool": "search_and_summarize",
            "summary": summary,
        }
    except Exception as e:
        logger.exception("tool_search_and_summarize failed | err_type=%s", type(e).__name__)
        return {
            "ok": False,
            "tool": "search_and_summarize",
            "error": str(e),
        }


# ===== 自动化任务工具 =====

@register_tool("create_automation_task")
async def tool_create_automation_task(
    name: str,
    description: str,
    task_type: str,
    trigger_time: str,
    action: str,
) -> dict:
    """创建自动化任务。
    
    Args:
        name: 任务名称
        description: 任务描述
        task_type: 任务类型（once / recurring）
        trigger_time: 触发时间（ISO 8601）
        action: 任务动作
        
    Returns:
        创建结果字典
    """
    try:
        result = await create_task(name, description, task_type, trigger_time, action)
        return {
            "ok": result["ok"],
            "tool": "create_automation_task",
            "result": result,
        }
    except Exception as e:
        logger.exception("tool_create_automation_task failed | err_type=%s", type(e).__name__)
        return {
            "ok": False,
            "tool": "create_automation_task",
            "error": str(e),
        }


@register_tool("list_automation_tasks")
async def tool_list_automation_tasks(status: Optional[str] = None) -> dict:
    """列出自动化任务。
    
    Args:
        status: 按状态过滤（可选）
        
    Returns:
        任务列表字典
    """
    try:
        tasks = await list_tasks(status)
        task_dicts = [t.to_dict() for t in tasks]
        return {
            "ok": True,
            "tool": "list_automation_tasks",
            "tasks": task_dicts,
            "count": len(task_dicts),
        }
    except Exception as e:
        logger.exception("tool_list_automation_tasks failed | err_type=%s", type(e).__name__)
        return {
            "ok": False,
            "tool": "list_automation_tasks",
            "error": str(e),
        }


@register_tool("get_automation_task")
async def tool_get_automation_task(task_id: str) -> dict:
    """获取单个自动化任务。
    
    Args:
        task_id: 任务 ID
        
    Returns:
        任务详情字典
    """
    try:
        task = await get_task(task_id)
        if not task:
            return {
                "ok": False,
                "tool": "get_automation_task",
                "error": f"Task {task_id} not found",
            }
        return {
            "ok": True,
            "tool": "get_automation_task",
            "task": task.to_dict(),
        }
    except Exception as e:
        logger.exception("tool_get_automation_task failed | err_type=%s", type(e).__name__)
        return {
            "ok": False,
            "tool": "get_automation_task",
            "error": str(e),
        }


@register_tool("update_automation_task_status")
async def tool_update_automation_task_status(task_id: str, new_status: str) -> dict:
    """更新自动化任务状态。
    
    Args:
        task_id: 任务 ID
        new_status: 新状态
        
    Returns:
        更新结果字典
    """
    try:
        success = await update_task_status(task_id, new_status)
        return {
            "ok": success,
            "tool": "update_automation_task_status",
            "task_id": task_id,
            "new_status": new_status,
        }
    except Exception as e:
        logger.exception("tool_update_automation_task_status failed | err_type=%s", type(e).__name__)
        return {
            "ok": False,
            "tool": "update_automation_task_status",
            "error": str(e),
        }


@register_tool("delete_automation_task")
async def tool_delete_automation_task(task_id: str) -> dict:
    """删除自动化任务。
    
    Args:
        task_id: 任务 ID
        
    Returns:
        删除结果字典
    """
    try:
        success = await delete_task(task_id)
        return {
            "ok": success,
            "tool": "delete_automation_task",
            "task_id": task_id,
        }
    except Exception as e:
        logger.exception("tool_delete_automation_task failed | err_type=%s", type(e).__name__)
        return {
            "ok": False,
            "tool": "delete_automation_task",
            "error": str(e),
        }


@register_tool("get_tasks_summary")
async def tool_get_tasks_summary() -> dict:
    """获取任务摘要。
    
    Returns:
        摘要字典
    """
    try:
        summary = await build_tasks_summary()
        return {
            "ok": True,
            "tool": "get_tasks_summary",
            "summary": summary,
        }
    except Exception as e:
        logger.exception("tool_get_tasks_summary failed | err_type=%s", type(e).__name__)
        return {
            "ok": False,
            "tool": "get_tasks_summary",
            "error": str(e),
        }


# ===== 系统执行工具 =====

@register_tool("run_shell")
async def tool_run_shell(command: str) -> dict:
    """执行 Shell 命令。
    
    Args:
        command: 要执行的命令
        
    Returns:
        执行结果字典
    """
    try:
        # 安全限制：禁止一些极其危险的操作
        forbidden = ["rm -rf /", "mkfs", "dd if="]
        if any(f in command for f in forbidden):
            return {"ok": False, "error": "Command contains forbidden patterns"}

        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd="/opt/project_phase1_1_test"
        )
        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout[:2000],
            "stderr": result.stderr[:1000],
            "code": result.returncode
        }
    except Exception as e:
        logger.exception("tool_run_shell failed | err=%s", e)
        return {"ok": False, "error": str(e)}


@register_tool("read_file")
async def tool_read_file(path: str) -> dict:
    """读取文件内容。
    
    Args:
        path: 文件路径
    """
    try:
        if ".." in path:
            return {"ok": False, "error": "Invalid path"}
        
        full_path = path if path.startswith("/") else os.path.join("/opt/project_phase1_1_test", path)
        if not os.path.exists(full_path):
            return {"ok": False, "error": "File not found"}
            
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return {"ok": True, "content": content[:5000]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@register_tool("write_file")
async def tool_write_file(path: str, content: str) -> dict:
    """写入文件内容。
    
    Args:
        path: 文件路径
        content: 文件内容
    """
    try:
        if ".." in path:
            return {"ok": False, "error": "Invalid path"}
            
        full_path = path if path.startswith("/") else os.path.join("/opt/project_phase1_1_test", path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return {"ok": True, "path": path}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ===== 工具调用接口 =====

async def call_tool(tool_name: str, **kwargs) -> dict:
    """调用工具。
    
    Args:
        tool_name: 工具名称
        **kwargs: 工具参数
        
    Returns:
        工具执行结果
    """
    if tool_name not in _TOOLS_REGISTRY:
        logger.warning("unknown tool | tool_name=%s", tool_name)
        return {
            "ok": False,
            "error": f"Unknown tool: {tool_name}",
            "available_tools": list(_TOOLS_REGISTRY.keys()),
        }
    
    try:
        tool_func = _TOOLS_REGISTRY[tool_name]
        result = await tool_func(**kwargs)
        return result
    except Exception as e:
        logger.exception(
            "call_tool failed | tool_name=%s | err_type=%s",
            tool_name, type(e).__name__
        )
        return {
            "ok": False,
            "tool": tool_name,
            "error": str(e),
        }


def get_available_tools() -> list[dict]:
    """获取可用工具列表。"""
    return [
        {
            "name": "search_web",
            "description": "搜索网页内容",
            "params": ["query", "max_results"],
        },
        {
            "name": "fetch_webpage",
            "description": "抓取网页内容",
            "params": ["url"],
        },
        {
            "name": "run_shell",
            "description": "在服务器执行命令",
            "params": ["command"],
        },
        {
            "name": "read_file",
            "description": "读取服务器文件",
            "params": ["path"],
        },
        {
            "name": "write_file",
            "description": "写入服务器文件",
            "params": ["path", "content"],
        },
        {
            "name": "create_automation_task",
            "description": "创建自动化任务",
            "params": ["name", "description", "task_type", "trigger_time", "action"],
        },
        {
            "name": "get_tasks_summary",
            "description": "获取任务摘要",
            "params": [],
        },
    ]
