"""管理员主脑（OpenAI）对话服务。

只服务 owner 的私人控制台。与普通私聊 / Business 完全隔离：
- 用独立的 ADMIN_BRAIN_SYSTEM_PROMPT；
- 走 call_openai(..., response_json=False) 拿纯自然语言（不强制 JSON），
  避免污染普通聊天那套 reply_text/sticker JSON 协议；
- 维护一小段 per-owner 的 in-process 对话历史，让连续对话有上下文。
- 支持工具调用（Tool Use）：搜索、自动化任务管理等。

不写任何密钥到日志，也不持久化对话内容（进程级内存，重启即清）。
"""

from __future__ import annotations

from collections import deque

from config import ADMIN_BRAIN_SYSTEM_PROMPT, CORE_MODEL
from services.admin_brain_tools import call_tool, get_available_tools
from services.openai_service import call_openai
from utils.logger import setup_logging

logger = setup_logging()

# 每个 owner 保留最近 N 轮（user+assistant 算 2 条）对话，限制 token / 内存占用。
_MAX_TURNS = 12
_history: dict[str, deque] = {}


def reset_history(owner_key: str | int) -> None:
    """退出会话或 owner 主动重置时清空历史。"""
    _history.pop(str(owner_key), None)


def _get_history(owner_key: str) -> deque:
    dq = _history.get(owner_key)
    if dq is None:
        dq = deque(maxlen=_MAX_TURNS)
        _history[owner_key] = dq
    return dq


async def ask_admin_brain(owner_key: str | int, user_text: str) -> str:
    """把 owner 的自然语言指令交给主脑，返回自然语言回复。

    owner_key：用于隔离不同 owner 的对话历史（一般是 user_id）。
    失败时返回一句友好的中文提示，不抛异常给上层 handler。
    
    支持工具调用：主脑可以自主决定是否调用搜索、自动化任务等工具。
    """
    text = (user_text or "").strip()
    if not text:
        return "（主脑）你想聊点什么？直接把指令发我就行。"

    key = str(owner_key)
    history = _get_history(key)

    # 构建系统提示，包含可用工具信息
    tools_info = "\n\n【可用工具】\n"
    for tool in get_available_tools():
        tools_info += f"- {tool['name']}: {tool['description']}\n"
    
    system_prompt = ADMIN_BRAIN_SYSTEM_PROMPT + tools_info + (
        "\n### 重要指令 ###\n"
        "1. 你拥有真实的联网权限和任务调度权限。当用户要求你搜索、浏览或执行任务时，你必须立即调用对应工具，严禁回复“我无法访问”、“我没有权限”或“我只是个顾问”。\n"
        "2. 你的回复中如果包含工具调用，必须严格遵守以下格式：\n"
        "【工具调用】\n"
        "工具名: 工具名称\n"
        "参数: 参数名=\"参数值\"\n"
        "\n3. 先执行工具，再根据工具返回的结果给出最终回答。如果一次调用不够，可以连续引导用户或多次调用。"
    )

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(list(history))
    messages.append({"role": "user", "content": text})

    try:
        reply = await call_openai(
            messages,
            model=CORE_MODEL,
            mode="private",
            response_json=False,
            chat_id=f"admin_brain:{key}",
        )
    except Exception as e:
        logger.exception("admin brain call failed | err_type=%s", type(e).__name__)
        return "（主脑）刚才调用模型出错了，稍后再试一次。"

    reply = (reply or "").strip()
    if not reply:
        return "（主脑）我没接住，换个说法再发一次？"

    # 检查是否包含工具调用
    reply = await _process_tool_calls(reply)

    # 记录这一轮，供后续对话使用
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    return reply


async def _process_tool_calls(reply: str) -> str:
    """处理回复中的工具调用。
    
    检查回复中是否包含【工具调用】标记，如果有则执行工具并将结果插入回复。
    """
    import re
    
    # 匹配【工具调用】块
    pattern = r"【工具调用】\n工具名:\s*([\w_]+)\n参数:\s*(.+?)(?=\n\n|$)"
    matches = re.finditer(pattern, reply, re.DOTALL)
    
    tool_results = []
    for match in matches:
        tool_name = match.group(1).strip()
        params_str = match.group(2).strip()
        
        try:
            # 解析参数
            params = {}
            for param_line in params_str.split("\n"):
                if "=" in param_line:
                    key, value = param_line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"')
                    params[key] = value
            
            # 调用工具
            result = await call_tool(tool_name, **params)
            tool_results.append(f"\n【{tool_name} 结果】\n{str(result)[:500]}")
        except Exception as e:
            logger.exception("process_tool_calls failed | tool=%s | err=%s", tool_name, e)
            tool_results.append(f"\n【{tool_name} 错误】\n{str(e)[:200]}")
    
    # 将工具结果追加到回复末尾
    if tool_results:
        reply += "\n".join(tool_results)
    
    return reply
