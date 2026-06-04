#!/usr/bin/env python3
"""Local CodeGraph builder — dev/maintenance tooling only.

Scans the repository's Python files using the stdlib ``ast`` module and emits a
code graph describing modules, imports, functions/classes, call references,
aiogram router handlers, and a few important service edges
(call_openai / send_reply / store_message / sanitize*).

This is *static analysis only*. It never imports project code, never touches
.env / sqlite / log files, and never makes network calls. It does not change
any bot runtime behavior.

Usage:
    python scripts/build_codegraph.py            # scan repo, write artifacts
    python scripts/build_codegraph.py --self-test  # no-network self-test

Artifacts (written under docs/codegraph/):
    codegraph.json   — full graph
    README.md        — human summary + regenerate instructions
    codegraph.mmd    — Mermaid module-import diagram
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

# --- Configuration -----------------------------------------------------------

# Directories never scanned (secrets / data / vcs / artifacts).
EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    "docs",
}

# File suffixes/names we refuse to read regardless of extension.
EXCLUDE_FILE_SUFFIXES = (".env", ".sqlite", ".sqlite3", ".db", ".log")
EXCLUDE_FILE_NAMES = {".env", ".env.example"}

# Service functions whose call sites we explicitly track as "important edges".
IMPORTANT_CALLEES = {
    "call_openai",
    "send_reply",
    "store_message",
    "sanitize_visible_reply",
    "sanitize",
}

# aiogram decorator method names that mark a router handler.
HANDLER_DECORATOR_METHODS = {
    "message",
    "callback_query",
    "business_message",
    "business_connection",
    "edited_message",
    "inline_query",
    "chat_member",
    "my_chat_member",
    "poll",
    "poll_answer",
    "channel_post",
}


# --- Data model --------------------------------------------------------------


@dataclass
class ModuleInfo:
    module: str  # dotted module name, e.g. "services.openai_service"
    path: str  # repo-relative path
    imports: list = field(default_factory=list)  # list of dotted targets
    functions: list = field(default_factory=list)
    classes: list = field(default_factory=list)
    handlers: list = field(default_factory=list)
    parse_error: str = ""


# --- Helpers -----------------------------------------------------------------


def _is_excluded_file(name: str) -> bool:
    if name in EXCLUDE_FILE_NAMES:
        return True
    return any(name.endswith(sfx) for sfx in EXCLUDE_FILE_SUFFIXES)


def find_python_files(root: str) -> list:
    """Return repo-relative paths to .py files, skipping excluded trees."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            if _is_excluded_file(fn):
                continue
            full = os.path.join(dirpath, fn)
            out.append(os.path.relpath(full, root))
    return sorted(out)


def path_to_module(rel_path: str) -> str:
    """Convert a repo-relative .py path to a dotted module name."""
    no_ext = rel_path[:-3] if rel_path.endswith(".py") else rel_path
    parts = no_ext.split(os.sep)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _decorator_router_method(dec: ast.AST):
    """If a decorator looks like @router.message(...) / @dp.business_connection(),
    return the method name (e.g. "message"), else None."""
    call = dec
    if isinstance(call, ast.Call):
        func = call.func
    else:
        func = call
    if isinstance(func, ast.Attribute) and func.attr in HANDLER_DECORATOR_METHODS:
        # ensure the value is a Name (router / dp / self.router etc.)
        return func.attr
    return None


def _decorator_repr(dec: ast.AST) -> str:
    try:
        return ast.unparse(dec)
    except Exception:
        return "<decorator>"


# --- Per-module AST visitor --------------------------------------------------


class CallCollector(ast.NodeVisitor):
    """Collect called names within a function body."""

    def __init__(self):
        self.calls = []

    def visit_Call(self, node: ast.Call):
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name:
            self.calls.append(name)
        self.generic_visit(node)


def analyze_module(rel_path: str, source: str) -> ModuleInfo:
    module = path_to_module(rel_path)
    info = ModuleInfo(module=module, path=rel_path)

    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError as e:
        info.parse_error = f"{type(e).__name__}: {e}"
        return info

    # Imports
    seen_imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                seen_imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                seen_imports.add(node.module)
            elif node.level > 0:
                # relative import; record best-effort
                base = ".".join(module.split(".")[: -node.level]) if module else ""
                target = f"{base}.{node.module}" if node.module else base
                if target:
                    seen_imports.add(target)
    info.imports = sorted(seen_imports)

    def _process_function(fn, qualifier: str = ""):
        cc = CallCollector()
        for child in fn.body:
            cc.visit(child)
        calls = sorted(set(cc.calls))
        important = sorted(set(c for c in cc.calls if c in IMPORTANT_CALLEES))
        qualified = f"{qualifier}.{fn.name}" if qualifier else fn.name

        # Detect handler decorators.
        handler_events = []
        for dec in fn.decorator_list:
            m = _decorator_router_method(dec)
            if m:
                handler_events.append(
                    {"event": m, "decorator": _decorator_repr(dec)}
                )

        entry = {
            "name": qualified,
            "lineno": fn.lineno,
            "is_async": isinstance(fn, ast.AsyncFunctionDef),
            "calls": calls,
            "important_calls": important,
        }
        info.functions.append(entry)

        if handler_events:
            info.handlers.append(
                {
                    "handler": qualified,
                    "lineno": fn.lineno,
                    "events": handler_events,
                    "important_calls": important,
                }
            )

    # Top-level functions & classes
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _process_function(node)
        elif isinstance(node, ast.ClassDef):
            methods = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _process_function(item, qualifier=node.name)
                    methods.append(item.name)
            info.classes.append(
                {"name": node.name, "lineno": node.lineno, "methods": sorted(methods)}
            )

    return info


# --- Graph assembly ----------------------------------------------------------


def build_graph(root: str) -> dict:
    files = find_python_files(root)
    modules = {}
    for rel in files:
        full = os.path.join(root, rel)
        try:
            with open(full, "r", encoding="utf-8") as fh:
                source = fh.read()
        except (OSError, UnicodeDecodeError) as e:
            modules[path_to_module(rel)] = ModuleInfo(
                module=path_to_module(rel), path=rel, parse_error=f"read error: {e}"
            )
            continue
        info = analyze_module(rel, source)
        modules[info.module] = info

    known = set(modules.keys())

    # Internal import edges (only edges where the target is a module we scanned).
    import_edges = []
    for mod, info in modules.items():
        for target in info.imports:
            resolved = _resolve_import(target, known)
            if resolved and resolved != mod:
                import_edges.append({"from": mod, "to": resolved, "raw": target})

    # Important service-call edges, attributed to the calling module/function.
    service_edges = []
    for mod, info in modules.items():
        for fn in info.functions:
            for callee in fn["important_calls"]:
                service_edges.append(
                    {"from_module": mod, "from_function": fn["name"], "callee": callee}
                )

    handlers = []
    for mod, info in modules.items():
        for h in info.handlers:
            handlers.append({"module": mod, **h})

    graph = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tool": "scripts/build_codegraph.py",
            "root": os.path.basename(os.path.abspath(root)),
            "module_count": len(modules),
            "import_edge_count": len(import_edges),
            "handler_count": len(handlers),
            "service_edge_count": len(service_edges),
            "note": "Static ast-only analysis. No code imported, no network, no secrets/db/logs read.",
        },
        "modules": [
            {
                "module": info.module,
                "path": info.path,
                "imports": info.imports,
                "functions": info.functions,
                "classes": info.classes,
                "handlers": info.handlers,
                "parse_error": info.parse_error,
            }
            for info in sorted(modules.values(), key=lambda m: m.module)
        ],
        "import_edges": sorted(
            import_edges, key=lambda e: (e["from"], e["to"])
        ),
        "handlers": sorted(handlers, key=lambda h: (h["module"], h["lineno"])),
        "service_edges": sorted(
            service_edges, key=lambda e: (e["callee"], e["from_module"])
        ),
    }
    return graph


def _resolve_import(target: str, known: set):
    """Map an import target to a scanned module name if it (or a prefix) matches."""
    if target in known:
        return target
    # Try progressively shorter prefixes: e.g. "services.openai_service.foo".
    parts = target.split(".")
    for i in range(len(parts), 0, -1):
        cand = ".".join(parts[:i])
        if cand in known:
            return cand
    return None


# --- Rendering ---------------------------------------------------------------


def render_mermaid(graph: dict) -> str:
    lines = ["%% Auto-generated by scripts/build_codegraph.py", "graph LR"]

    def node_id(mod: str) -> str:
        return "m_" + mod.replace(".", "_")

    # Only render modules that participate in an internal import edge to keep
    # the diagram readable.
    used = set()
    for e in graph["import_edges"]:
        used.add(e["from"])
        used.add(e["to"])
    for mod in sorted(used):
        lines.append(f'    {node_id(mod)}["{mod}"]')
    for e in graph["import_edges"]:
        lines.append(f'    {node_id(e["from"])} --> {node_id(e["to"])}')
    return "\n".join(lines) + "\n"


def render_readme(graph: dict) -> str:
    meta = graph["meta"]
    out = []
    out.append("# CodeGraph (local dev tooling)")
    out.append("")
    out.append(
        "Static code-graph artifacts for the Telegram bot project, produced by "
        "`scripts/build_codegraph.py`. This is **development/maintenance tooling "
        "only** — it does not run, import, or alter the bot."
    )
    out.append("")
    out.append("## How to regenerate")
    out.append("")
    out.append("```bash")
    out.append("python scripts/build_codegraph.py     # or: make codegraph")
    out.append("```")
    out.append("")
    out.append("Self-test (no network, writes to a temp dir):")
    out.append("")
    out.append("```bash")
    out.append("python scripts/build_codegraph.py --self-test   # or: make codegraph-test")
    out.append("```")
    out.append("")
    out.append("## Summary")
    out.append("")
    out.append(f"- Generated at: `{meta['generated_at']}`")
    out.append(f"- Modules scanned: **{meta['module_count']}**")
    out.append(f"- Internal import edges: **{meta['import_edge_count']}**")
    out.append(f"- Detected aiogram handlers: **{meta['handler_count']}**")
    out.append(f"- Important service-call edges: **{meta['service_edge_count']}**")
    out.append("")
    out.append(
        "> Analysis is `ast`-only. No project code is imported; no `.env`, "
        "sqlite, or log files are read."
    )
    out.append("")

    # Handlers / routes
    out.append("## aiogram routes / handlers")
    out.append("")
    if graph["handlers"]:
        out.append("| Module | Handler | Events | Line |")
        out.append("| --- | --- | --- | --- |")
        for h in graph["handlers"]:
            events = ", ".join(
                f"`{e['decorator']}`" for e in h["events"]
            )
            out.append(
                f"| `{h['module']}` | `{h['handler']}` | {events} | {h['lineno']} |"
            )
    else:
        out.append("_No handlers detected._")
    out.append("")

    # Important service edges grouped by callee
    out.append("## Important service edges")
    out.append("")
    out.append(
        "Call sites of key service functions "
        "(`call_openai`, `send_reply`, `store_message`, `sanitize*`)."
    )
    out.append("")
    by_callee = {}
    for e in graph["service_edges"]:
        by_callee.setdefault(e["callee"], []).append(e)
    if by_callee:
        for callee in sorted(by_callee):
            out.append(f"### `{callee}`")
            out.append("")
            for e in sorted(
                by_callee[callee], key=lambda x: (x["from_module"], x["from_function"])
            ):
                out.append(f"- `{e['from_module']}` → `{e['from_function']}`")
            out.append("")
    else:
        out.append("_No important service edges detected._")
    out.append("")

    # Module import overview
    out.append("## Module import graph")
    out.append("")
    out.append("See `codegraph.mmd` (Mermaid) for a visual diagram. Edge list:")
    out.append("")
    if graph["import_edges"]:
        for e in graph["import_edges"]:
            out.append(f"- `{e['from']}` → `{e['to']}`")
    else:
        out.append("_No internal import edges detected._")
    out.append("")

    # Parse errors, if any
    errs = [m for m in graph["modules"] if m["parse_error"]]
    if errs:
        out.append("## Parse errors")
        out.append("")
        for m in errs:
            out.append(f"- `{m['path']}`: {m['parse_error']}")
        out.append("")

    out.append("---")
    out.append("")
    out.append("_Generated by `scripts/build_codegraph.py`. Safe to commit; dev-only._")
    out.append("")
    return "\n".join(out)


def write_artifacts(graph: dict, out_dir: str) -> list:
    os.makedirs(out_dir, exist_ok=True)
    written = []

    json_path = os.path.join(out_dir, "codegraph.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(graph, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    written.append(json_path)

    readme_path = os.path.join(out_dir, "README.md")
    with open(readme_path, "w", encoding="utf-8") as fh:
        fh.write(render_readme(graph))
    written.append(readme_path)

    mmd_path = os.path.join(out_dir, "codegraph.mmd")
    with open(mmd_path, "w", encoding="utf-8") as fh:
        fh.write(render_mermaid(graph))
    written.append(mmd_path)

    return written


# --- Self-test ---------------------------------------------------------------


def run_self_test() -> int:
    """No-network self-test: build the graph for this repo into a temp dir and
    assert the expected artifacts exist and are well-formed."""
    root = repo_root()
    graph = build_graph(root)

    assert graph["meta"]["module_count"] > 0, "expected to scan at least one module"
    assert isinstance(graph["modules"], list)
    assert isinstance(graph["import_edges"], list)
    assert isinstance(graph["handlers"], list)
    assert isinstance(graph["service_edges"], list)

    with tempfile.TemporaryDirectory() as tmp:
        written = write_artifacts(graph, tmp)
        assert len(written) == 3, f"expected 3 artifacts, got {written}"
        for p in written:
            assert os.path.exists(p) and os.path.getsize(p) > 0, f"missing/empty {p}"
        # JSON must round-trip.
        with open(os.path.join(tmp, "codegraph.json"), encoding="utf-8") as fh:
            reloaded = json.load(fh)
        assert reloaded["meta"]["module_count"] == graph["meta"]["module_count"]

    print("[self-test] OK")
    print(f"[self-test] modules={graph['meta']['module_count']} "
          f"import_edges={graph['meta']['import_edge_count']} "
          f"handlers={graph['meta']['handler_count']} "
          f"service_edges={graph['meta']['service_edge_count']}")
    return 0


# --- Entry point -------------------------------------------------------------


def repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build a static code graph of the repo.")
    parser.add_argument(
        "--root", default=repo_root(), help="Repo root to scan (default: repo root)."
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output dir (default: <root>/docs/codegraph).",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run a no-network self-test and exit.",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    root = os.path.abspath(args.root)
    out_dir = args.out or os.path.join(root, "docs", "codegraph")
    graph = build_graph(root)
    written = write_artifacts(graph, out_dir)

    print(f"CodeGraph written ({graph['meta']['module_count']} modules, "
          f"{graph['meta']['handler_count']} handlers, "
          f"{graph['meta']['service_edge_count']} service edges):")
    for p in written:
        print(f"  - {os.path.relpath(p, root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
