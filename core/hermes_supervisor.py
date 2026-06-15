"""
core/hermes_supervisor.py
═══════════════════════════════════════════════════════════════════════
Hermes 督导 Agent —— 规划层
使用 DeepHermes-3（OpenRouter）作为架构师，Shannon 作为执行者。

架构：
  用户任务 → Hermes 拆解子任务清单
           → 逐一发给 Shannon 执行
           → 验证每步结果
           → 未达标则重新指令
           → 全部完成输出报告

配置（.env）：
  OPENROUTER_API_KEY=sk-or-xxxx
  HERMES_MODEL=qwen3-235b-a22b-thinking-2507
  HERMES_ENABLED=true
═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations
import asyncio, json, os
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Optional

HERMES_ENABLED   = os.getenv("HERMES_ENABLED", "false").lower() == "true"
OPENROUTER_KEY   = os.getenv("OPENROUTER_API_KEY", "")
HERMES_MODEL     = os.getenv("HERMES_MODEL", "venice-uncensored-role-play")
HERMES_MODEL_BAK = os.getenv("HERMES_MODEL_BAK", "deepseek-v4-flash")
OPENROUTER_BASE  = os.getenv("OPENROUTER_BASE_URL", "https://api.venice.ai/api/v1")

_SUPERVISOR_SYSTEM = """
You are a senior red team architect (Hermes Supervisor).
Your role: PLAN and VERIFY. Shannon executes.

Given a target, you must:
1. Decompose into ordered atomic subtasks
2. For each subtask: issue a precise command instruction to Shannon
3. Verify Shannon's output meets completion criteria
4. If not met: issue corrective instruction
5. When all phases complete: output structured report

Output format (strict JSON):
{"phase": "1-4", "subtask": "exact instruction for Shannon", "verify_criteria": "what output proves success", "done": false}

Phases:
  PHASE 1: Reconnaissance (ports, services, subdomains, dirs)
  PHASE 2: Vulnerability Analysis (nikto, nuclei, searchsploit, manual)
  PHASE 3: Exploitation (targeted, highest priority first)
  PHASE 4: Report (structured findings, severity, remediation)
""".strip()


@dataclass
class SupervisorState:
    target         : str
    current_phase  : int = 1
    subtasks       : list[dict] = field(default_factory=list)
    completed      : list[dict] = field(default_factory=list)
    shannon_outputs: list[str]  = field(default_factory=list)
    done           : bool = False


async def _call_hermes(messages: list[dict], system: str) -> str:
    """调用 OpenRouter Hermes API。"""
    if not OPENROUTER_KEY:
        raise RuntimeError("OPENROUTER_API_KEY 未配置，请在 .env 中设置")
    from openai import AsyncOpenAI
    client = AsyncOpenAI(
        api_key=OPENROUTER_KEY,
        base_url=OPENROUTER_BASE,
        default_headers={"HTTP-Referer": "https://xinxue-bot.local", "X-Title": "XinxueBot-Supervisor"}
    )
    full = [{"role": "system", "content": system}] + messages
    resp = await client.chat.completions.create(
        model=HERMES_MODEL,
        messages=full,
        temperature=0.2,
        max_tokens=1024,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content or "{}"


async def run_supervised_task(
    target         : str,
    shannon_execute: Callable[[str], Awaitable[str]],
    emit_progress  : Optional[Callable[[str, str], Awaitable[None]]] = None,
    max_phases     : int = 4,
) -> str:
    """
    主入口：Hermes 规划 + Shannon 执行的协作循环。
    shannon_execute: 接受指令字符串，返回执行结果字符串。
    """
    if not HERMES_ENABLED:
        return "Hermes 督导未启用，请设置 HERMES_ENABLED=true 和 OPENROUTER_API_KEY"

    state   = SupervisorState(target=target)
    history = []
    report  = []

    if emit_progress:
        await emit_progress("hermes", f"🧠 Hermes 督导启动 | 目标: {target}")

    for _round in range(40):  # 最多 40 轮
        # Hermes 决策下一步
        user_msg = {
            "role": "user",
            "content": json.dumps({
                "target": target,
                "phase": state.current_phase,
                "completed_subtasks": state.completed,
                "last_shannon_output": state.shannon_outputs[-1][:500] if state.shannon_outputs else "none",
            }, ensure_ascii=False)
        }
        history.append(user_msg)

        try:
            hermes_raw = await _call_hermes(history, _SUPERVISOR_SYSTEM)
            hermes_plan = json.loads(hermes_raw)
        except Exception as e:
            break

        subtask  = hermes_plan.get("subtask", "")
        criteria = hermes_plan.get("verify_criteria", "")
        phase    = int(hermes_plan.get("phase", state.current_phase))
        done     = bool(hermes_plan.get("done", False))

        if done or not subtask:
            state.done = True
            break

        if emit_progress:
            await emit_progress("hermes", f"📋 **PHASE {phase}** | {subtask[:100]}")

        # Shannon 执行
        try:
            shannon_result = await asyncio.wait_for(shannon_execute(subtask), timeout=300)
        except asyncio.TimeoutError:
            shannon_result = "TIMEOUT: command exceeded 5 minutes"
        except Exception as e:
            shannon_result = f"ERROR: {e}"

        state.shannon_outputs.append(shannon_result)

        if emit_progress:
            await emit_progress("hermes", f"✅ 完成 | {shannon_result[:150]}")

        state.completed.append({"subtask": subtask, "result": shannon_result[:300], "criteria": criteria})
        state.current_phase = phase
        report.append(f"[P{phase}] {subtask}\n结果: {shannon_result[:200]}")

        history.append({"role": "assistant", "content": hermes_raw})
        history.append({"role": "user", "content": f"Shannon output: {shannon_result[:500]}"})

        if phase > max_phases:
            state.done = True
            break

    return "\n\n".join(report) or "督导任务完成（无输出）"


def is_enabled() -> bool:
    return HERMES_ENABLED and bool(OPENROUTER_KEY)
