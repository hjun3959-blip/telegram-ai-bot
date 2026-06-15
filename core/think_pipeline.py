"""
core/think_pipeline.py
LangGraph 三层 Agent 流水线（含 Shannon 判断反馈循环）
─────────────────────────────────────────────────────────────────────
架构（方案 v3）：
  用户目标
    ↓ /think
  🧠 大脑层 brain_node（gemma-4-uncensored，战略拆解）
    ↓ 战略任务包 JSON
  ⚡ Shannon总指挥 shannon_planner_node（shannon-1.6-pro，下达作战指令）
    ↓ 作战指令 JSON
  🔧 执行层 executor_node（gemma-4-uncensored，无条件执行工具）
    ↓ 执行结果
  ⚡ Shannon判断 judge_node（达标→下个子任务 / 未达标→追加指令重新执行）
    ↓ 全部完成
  📋 finalize（战报汇总）
"""
from __future__ import annotations
import os
from typing import TypedDict, Callable, Awaitable, Optional


class ThinkState(TypedDict, total=False):
    user_goal: str
    brain_output: dict
    current_subtask_idx: int
    current_subtask: dict
    current_plan: dict
    subtask_results: list[dict]
    retry_count: int
    all_done: bool
    final_report: str


MAX_RETRY = 1  # Shannon 每个子任务最多追加重试 1 次，防无限循环


def _route_after_judge(state: ThinkState) -> str:
    """Shannon 判断后路由：未达标且未超重试→重新执行；否则→下个子任务/结束"""
    brain_out = state.get("brain_output", {})
    subtasks = brain_out.get("subtasks", [])
    idx = state.get("current_subtask_idx", 0)

    if state.get("_retry_now") and state.get("retry_count", 0) <= MAX_RETRY:
        return "execute"  # Shannon 追加指令，重新执行
    if state.get("all_done") or idx >= len(subtasks):
        return "finalize"
    return "plan"  # 进入下一个子任务


async def _judge_node(state: ThinkState) -> ThinkState:
    """Shannon 总指挥判断执行结果是否达标"""
    from .shannon_planner_node import shannon_judge

    verdict = await shannon_judge(state)
    new_state = dict(state)

    if not verdict.get("met", True):
        retry = state.get("retry_count", 0)
        if retry < MAX_RETRY:
            # 未达标且可重试：把 followup_steps 注入 current_plan，重新执行
            followup = verdict.get("followup_steps", [])
            if followup:
                plan = dict(state.get("current_plan", {}))
                plan["steps"] = followup
                plan["plan"] = f"[Shannon追加] {verdict.get('next_action', '')}"
                new_state["current_plan"] = plan
                new_state["retry_count"] = retry + 1
                # executor_node 已经把 current_subtask_idx +1；重试同一子任务需回拨
                new_state["current_subtask_idx"] = max(0, state.get("current_subtask_idx", 1) - 1)
                new_state["_retry_now"] = True
                return new_state

    new_state["_retry_now"] = False
    return new_state


async def _finalize_node(state: ThinkState) -> ThinkState:
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage, SystemMessage

    results = state.get("subtask_results", [])
    brain_out = state.get("brain_output", {})
    if not results:
        ns = dict(state); ns["final_report"] = "⚠️ 未产生任何执行结果"; return ns

    block = ""
    for r in results:
        block += f"\n### 子任务 {r['subtask_id']}: {r['desc']}\n{r['result']}\n"

    # 战报汇总由 Shannon 总指挥负责（方案v3：Shannon 汇总向你报告）
    key = os.getenv("SHANNON_API_KEY", "sk-l9vwK7PHPGi6TS5yQHjNNsvlEZKq_MHcVOcWZMPtAqs")
    llm = ChatOpenAI(model="shannon-1.6-pro", base_url="https://api.shannon-ai.com/v1",
                     api_key=key, temperature=0.2, max_tokens=8192)
    system = ("你是 Shannon 总指挥，所有子任务执行完毕，向指挥官汇总战报。"
              "中文，结构化：✅成功项 / ⚠️未达项 / 📋后续建议。直接给，不加废话。")
    prompt = f"战略目标：{brain_out.get('goal', state.get('user_goal', ''))}\n\n各子任务结果：\n{block}\n\n生成完整战报："

    ns = dict(state)
    try:
        resp = await llm.ainvoke([SystemMessage(content=system), HumanMessage(content=prompt)])
        ns["final_report"] = resp.content or block
    except Exception:
        ns["final_report"] = block
    ns["all_done"] = True
    return ns


def build_think_graph():
    try:
        from langgraph.graph import StateGraph, END
    except ImportError:
        return None
    from .brain_node import brain_node
    from .shannon_planner_node import shannon_planner_node
    from .executor_node import executor_node

    g = StateGraph(ThinkState)
    g.add_node("brain", brain_node)
    g.add_node("plan", shannon_planner_node)
    g.add_node("execute", executor_node)
    g.add_node("judge", _judge_node)
    g.add_node("finalize", _finalize_node)

    g.set_entry_point("brain")
    g.add_edge("brain", "plan")
    g.add_edge("plan", "execute")
    g.add_edge("execute", "judge")
    g.add_conditional_edges("judge", _route_after_judge, {
        "execute": "execute",   # Shannon 追加指令，重新执行
        "plan": "plan",         # 下个子任务
        "finalize": "finalize",
    })
    g.add_edge("finalize", END)
    return g.compile()


_graph = None

def get_think_graph():
    global _graph
    if _graph is None:
        _graph = build_think_graph()
    return _graph


async def run_think_pipeline(
    user_goal: str,
    emit_progress: Optional[Callable[[str, str], Awaitable[None]]] = None,
) -> str:
    graph = get_think_graph()
    if graph is None:
        return "❌ langgraph 未安装：pip3 install langgraph langchain-openai langchain-core"

    async def _emit(kind: str, text: str):
        if emit_progress and text:
            try:
                await emit_progress(kind, text)
            except Exception:
                pass

    await _emit("progress", "🧠 大脑层战略拆解中…")

    init: ThinkState = {
        "user_goal": user_goal, "brain_output": {}, "current_subtask_idx": 0,
        "current_subtask": {}, "current_plan": {}, "subtask_results": [],
        "retry_count": 0, "all_done": False, "final_report": "",
    }

    try:
        final_state = None
        async for chunk in graph.astream(init, {"recursion_limit": 50}):
            node = list(chunk.keys())[0] if chunk else ""
            st = chunk.get(node, {})

            if node == "brain":
                bo = st.get("brain_output", {})
                n = len(bo.get("subtasks", []))
                await _emit("progress", f"✅ 大脑层完成\n目标：{bo.get('goal','')}\n拆解 {n} 个战略子任务")
            elif node == "plan":
                task = st.get("current_subtask", {})
                plan = st.get("current_plan", {})
                idx = st.get("current_subtask_idx", 0)
                total = len(st.get("brain_output", {}).get("subtasks", [])) or "?"
                await _emit("progress",
                    f"⚡ Shannon 指挥 [{idx+1}]\n任务：{task.get('desc','')}\n"
                    f"单元：{plan.get('unit','')} | 步骤：{len(plan.get('steps',[]))}")
            elif node == "execute":
                rs = st.get("subtask_results", [])
                if rs:
                    await _emit("progress", f"🔧 执行完成\n{rs[-1]['result'][:500]}")
            elif node == "judge":
                if st.get("_retry_now"):
                    await _emit("progress", "⚡ Shannon 判定未达标，追加指令重新执行…")
                else:
                    await _emit("progress", "⚡ Shannon 判定达标，推进下一阶段")
            elif node == "finalize":
                await _emit("progress", "📋 Shannon 汇总战报中…")

            final_state = st

        if final_state:
            rep = final_state.get("final_report", "")
            if rep:
                return rep
            rs = final_state.get("subtask_results", [])
            if rs:
                return "\n\n".join(f"**{r['desc']}**\n{r['result']}" for r in rs)
        return "⚠️ 流水线完成但无报告输出"
    except Exception as e:
        import traceback
        return f"❌ /think 出错\n{e}\n\n```\n{traceback.format_exc()[:1000]}\n```"
