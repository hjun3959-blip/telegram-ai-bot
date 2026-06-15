"""
tool_registry.py — TASK_MODE_SKILLS 中央工具注册表
权限分级：read_only / write_safe / destructive
ToolCircuitBreaker：三态（closed / open / half_open）+ 自动恢复
"""
from __future__ import annotations
from utils.text import make_bigrams as _make_bigrams

import asyncio, os, time
from dataclasses import dataclass, field
from typing import Callable, Any, Awaitable


@dataclass
class ToolDef:
    name       : str
    desc       : str
    category   : str
    permission : str = "read_only"
    parameters : dict = field(default_factory=dict)
    handler    : Callable[..., Awaitable[Any]] | None = field(default=None, repr=False)

    def to_summary(self) -> dict:
        return {"name":self.name,"desc":self.desc,"category":self.category,
                "permission":self.permission,"parameters":self.parameters}


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDef] = {}
        self._breakers: dict[str, "ToolCircuitBreaker"] = {}

    def register(self, tool: ToolDef) -> None:
        if tool.name in self._tools:
            import logging as _log
            _log.getLogger(__name__).warning("ToolRegistry: 工具 %s 重复注册，已覆盖", tool.name)
        self._tools[tool.name] = tool
        self._breakers[tool.name] = ToolCircuitBreaker(tool.name)

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def list_all(self) -> list[dict]:
        return [t.to_summary() for t in self._tools.values()]

    def search_tools(self, query: str, top_k: int = 5) -> list[dict]:
        bigrams_q = set(_make_bigrams(query))
        scored = []
        for tool in self._tools.values():
            text = tool.name + " " + tool.desc + " " + tool.category
            bigrams_t = set(_make_bigrams(text))
            overlap = len(bigrams_q & bigrams_t)
            if overlap > 0:
                union = len(bigrams_q | bigrams_t)
                scored.append((overlap/union, tool.to_summary()))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s[1] for s in scored[:top_k]]

    async def call(self, name: str, **kwargs) -> Any:
        tool = self._tools.get(name)
        if not tool:
            raise ValueError(f"未知工具：{name}")
        if not tool.handler:
            raise NotImplementedError(f"工具 {name} 尚未绑定 handler")
        breaker = self._breakers[name]
        _timeout = int(os.getenv("TOOL_CALL_TIMEOUT", "30"))
        return await asyncio.wait_for(breaker.call(tool.handler, **kwargs), timeout=_timeout)


_registry = ToolRegistry()
def get_registry() -> ToolRegistry: return _registry
def register_tool(tool: ToolDef) -> None: _registry.register(tool)


class ToolCircuitBreaker:
    def __init__(self, name:str, threshold:int=3, recovery_secs:int=60):
        self.name=name; self.threshold=threshold; self.recovery_secs=recovery_secs
        self.fail_count=0; self.state="closed"; self._open_at=0.0

    def _maybe_recover(self):
        if self.state=="open" and (time.time()-self._open_at)>=self.recovery_secs:
            self.state="half_open"

    async def call(self, func, *args, **kwargs):
        self._maybe_recover()
        if self.state=="open":
            remaining=int(self.recovery_secs-(time.time()-self._open_at))
            raise ToolCircuitOpenError(f"Tool [{self.name}] circuit OPEN，{remaining}s 后自动恢复")
        try:
            result = await func(*args, **kwargs)
            self.fail_count=0; self.state="closed"
            return result
        except ToolCircuitOpenError:
            raise
        except Exception as exc:
            self.fail_count += 1
            if self.fail_count >= self.threshold:
                self.state="open"; self._open_at=time.time()
                raise ToolCircuitOpenError(f"Tool [{self.name}] 熔断（连续失败 {self.fail_count} 次）") from exc
            raise


class ToolCircuitOpenError(Exception):
    """工具熔断器处于 OPEN 状态时抛出。"""


# ═══════════════ 真实 Handler 导入（仅导入实际存在的文件）═══════════════
from tools.handlers.search_web    import handle_search_web
from tools.handlers.read_url      import handle_read_url
from tools.handlers.file_ops      import handle_write_file, handle_read_file
from tools.handlers.run_python    import handle_run_python
from tools.handlers.query_db      import handle_query_db
from tools.handlers.docker_tool   import handle_docker_run, handle_docker_list, handle_docker_stop
from tools.handlers.crypto_steg_tool import (
    handle_encrypt_file, handle_decrypt_file,
    handle_encrypt_text, handle_decrypt_text,
    handle_steg_embed, handle_steg_extract,
)
from tools.handlers.network_tool  import (
    handle_tor_request, handle_proxy_request,
    handle_ss_check, handle_sliver_sessions, handle_sliver_execute,
)
from tools.handlers.firecracker_tool import (
    handle_fc_create_vm, handle_fc_start_vm,
    handle_fc_stop_vm, handle_fc_list_vms,
)
from tools.handlers.temporal_tool import (
    handle_temporal_start, handle_temporal_list,
    handle_temporal_describe, handle_temporal_signal,
)

# 安全工具（可选，加载失败不影响启动）
try:
    from tools.handlers.code_search import handle_code_search
    _HAS_CODE_SEARCH = True
except Exception:
    _HAS_CODE_SEARCH = False

try:
    from tools.handlers.proxy_exec import handle_proxy_exec
    _HAS_PROXY_EXEC = True
except Exception:
    _HAS_PROXY_EXEC = False

# ═══════════════ 工具列表 ═══════════════
TASK_MODE_SKILLS: list[ToolDef] = [
    ToolDef(name="search_web",    desc="搜索互联网，返回最新信息和网页摘要",            category="互联网",      permission="read_only",   parameters={"query":"string","top_k":"int"},              handler=handle_search_web),
    ToolDef(name="read_url",      desc="抓取指定 URL 的网页全文内容",                  category="互联网",      permission="read_only",   parameters={"url":"string"},                               handler=handle_read_url),
    ToolDef(name="write_file",    desc="将内容写入本地文件",                           category="文件",        permission="write_safe",  parameters={"path":"string","content":"string"},         handler=handle_write_file),
    ToolDef(name="read_file",     desc="读取本地文件内容",                             category="文件",        permission="read_only",   parameters={"path":"string"},                             handler=handle_read_file),
    ToolDef(name="run_python",    desc="执行 Python 代码片段，返回 stdout",            category="系统",        permission="write_safe",  parameters={"code":"string"},                             handler=handle_run_python),
    ToolDef(name="query_db",      desc="查询 PostgreSQL/SQLite 数据库",               category="数据库",      permission="read_only",   parameters={"sql":"string"},                              handler=handle_query_db),
    ToolDef(name="docker_run",    desc="在隔离容器内执行 shell 命令",                  category="系统/沙箱",   permission="write_safe",  parameters={"command":"string","image":"string"},       handler=handle_docker_run),
    ToolDef(name="docker_list",   desc="列出当前运行中的 Docker 容器",                 category="系统/沙箱",   permission="read_only",   parameters={},                                               handler=handle_docker_list),
    ToolDef(name="docker_stop",   desc="停止指定 Docker 容器",                        category="系统/沙箱",   permission="write_safe",  parameters={"container_id":"string"},                    handler=handle_docker_stop),
    ToolDef(name="encrypt_file",  desc="AES-256 加密文件",                            category="加密",        permission="write_safe",  parameters={"input_path":"string","output_path":"string","passphrase":"string"}, handler=handle_encrypt_file),
    ToolDef(name="decrypt_file",  desc="AES-256 解密文件",                            category="加密",        permission="write_safe",  parameters={"input_path":"string","output_path":"string","passphrase":"string"}, handler=handle_decrypt_file),
    ToolDef(name="encrypt_text",  desc="加密文本，返回 base64 密文",                   category="加密",        permission="write_safe",  parameters={"plaintext":"string","passphrase":"string"}, handler=handle_encrypt_text),
    ToolDef(name="decrypt_text",  desc="解密 base64 密文",                            category="加密",        permission="write_safe",  parameters={"ciphertext":"string","passphrase":"string"}, handler=handle_decrypt_text),
    ToolDef(name="steg_embed",    desc="Steghide 隐写进图片",                         category="隐写",        permission="write_safe",  parameters={"cover_image":"string","secret_file":"string","output_image":"string","passphrase":"string"}, handler=handle_steg_embed),
    ToolDef(name="steg_extract",  desc="Steghide 从图片提取隐藏文件",                  category="隐写",        permission="write_safe",  parameters={"stego_image":"string","output_file":"string","passphrase":"string"}, handler=handle_steg_extract),
    ToolDef(name="tor_request",   desc="通过 Tor SOCKS5 代理发送 HTTP 请求",          category="网络/匿名",   permission="write_safe",  parameters={"url":"string","method":"string"},         handler=handle_tor_request),
    ToolDef(name="proxy_request", desc="通过住宅 IP 代理池发送请求",                   category="网络/匿名",   permission="write_safe",  parameters={"url":"string","method":"string"},         handler=handle_proxy_request),
    ToolDef(name="ss_check",      desc="检测 Shadowsocks 节点连通性",                  category="网络/匿名",   permission="read_only",   parameters={"target_url":"string"},                      handler=handle_ss_check),
    ToolDef(name="sliver_sessions",desc="列出 Sliver C2 活跃 session",                category="C2",          permission="destructive", parameters={"filter_os":"string"},                        handler=handle_sliver_sessions),
    ToolDef(name="sliver_execute", desc="在 Sliver session 上执行命令",                category="C2",          permission="destructive", parameters={"session_id":"string","command":"string"},handler=handle_sliver_execute),
    ToolDef(name="fc_create_vm",  desc="创建并配置 Firecracker microVM",              category="系统/虚拟化", permission="write_safe",  parameters={"vm_id":"string","vcpu_count":"int","mem_size_mib":"int"}, handler=handle_fc_create_vm),
    ToolDef(name="fc_start_vm",   desc="启动 Firecracker microVM",                   category="系统/虚拟化", permission="write_safe",  parameters={"socket_path":"string"},                     handler=handle_fc_start_vm),
    ToolDef(name="fc_stop_vm",    desc="停止 Firecracker microVM",                   category="系统/虚拟化", permission="write_safe",  parameters={"socket_path":"string"},                     handler=handle_fc_stop_vm),
    ToolDef(name="fc_list_vms",   desc="列出所有活跃 Firecracker microVM",            category="系统/虚拟化", permission="read_only",   parameters={},                                               handler=handle_fc_list_vms),
    ToolDef(name="temporal_start",  desc="启动 Temporal 工作流",                      category="任务编排",    permission="write_safe",  parameters={"workflow_type":"string","workflow_id":"string","task_queue":"string","args":"list"}, handler=handle_temporal_start),
    ToolDef(name="temporal_list",   desc="列出 Temporal 工作流列表",                  category="任务编排",    permission="read_only",   parameters={"max_results":"int"},                        handler=handle_temporal_list),
    ToolDef(name="temporal_describe",desc="查看 Temporal 工作流状态详情",             category="任务编排",    permission="read_only",   parameters={"workflow_id":"string","run_id":"string"}, handler=handle_temporal_describe),
    ToolDef(name="temporal_signal", desc="向 Temporal 工作流发送 Signal",             category="任务编排",    permission="write_safe",  parameters={"workflow_id":"string","signal_name":"string","payload":"any"}, handler=handle_temporal_signal),
]

if _HAS_CODE_SEARCH:
    TASK_MODE_SKILLS.append(ToolDef(name="code_search", desc="在本地代码库中搜索相关代码片段", category="安全/代码审计", permission="read_only", parameters={"query":"string","repo":"string","top_k":"int"}, handler=handle_code_search))

if _HAS_PROXY_EXEC:
    TASK_MODE_SKILLS.append(ToolDef(name="proxy_exec", desc="通过本地代理执行攻击命令（Burp Suite等）", category="安全/渗透", permission="destructive", parameters={"cmd":"string"}, handler=handle_proxy_exec))

for _tool in TASK_MODE_SKILLS:
    register_tool(_tool)
