"""环境依赖关系图：项目 → 档案 → 环境变量 → 运行时 → 可执行文件。

只读分析。图的边区分来源：
- declared：数据库里明确记录的引用（项目包含档案、档案包含变量、运行时指向 exe）；
- referenced：变量值里通过 %VAR% 引用其他变量；
- inferred：根据变量值包含运行时目录等线索推断（不能当作事实）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from .db import Database
from .envvars import split_path
from .lab import expand_vars, references, resolve_command

NODE_LABELS = {
    "project": "项目",
    "profile": "档案",
    "var": "变量",
    "runtime": "运行时",
    "exe": "可执行文件",
}


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path.strip()))


def build_graph(
    db: Database,
    *,
    env_values: Mapping[str, str] | None = None,
    path_value: str = "",
) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add_node(node_id: str, node_type: str, label: str, **extra: Any) -> None:
        if node_id in nodes:
            nodes[node_id].update(extra)
            return
        nodes[node_id] = {"id": node_id, "type": node_type, "label": label, **extra}

    def add_edge(source: str, target: str, kind: str) -> None:
        if source not in nodes or target not in nodes:
            return
        key = (source, target, kind)
        if key not in edges:
            edges[key] = {"source": source, "target": target, "kind": kind}

    projects = db.query("SELECT id, name, path FROM projects ORDER BY name COLLATE NOCASE")
    profiles = db.query("SELECT id, project_id, name, active FROM profiles")
    profile_vars = db.query(
        "SELECT profile_id, key, value, sensitive FROM profile_vars ORDER BY key COLLATE NOCASE"
    )
    runtimes = db.query(
        "SELECT id, kind, version, exe_path, source FROM runtimes ORDER BY kind, version DESC"
    )

    project_ids: dict[int, str] = {}
    for project in projects:
        node_id = f"project:{project['name']}"
        project_ids[int(project["id"])] = node_id
        add_node(node_id, "project", project["name"], detail=project["path"])

    profile_ids: dict[int, str] = {}
    for profile in profiles:
        project_node = project_ids.get(int(profile["project_id"]))
        if not project_node:
            continue
        node_id = f"profile:{project_node.split(':', 1)[1]}/{profile['name']}"
        profile_ids[int(profile["id"])] = node_id
        add_node(
            node_id,
            "profile",
            profile["name"],
            detail="当前档案" if profile["active"] else "",
        )
        add_edge(project_node, node_id, "declared")

    var_values: dict[str, str] = {}
    for var in profile_vars:
        key = str(var["key"]).upper()
        node_id = f"var:{key}"
        secret = bool(var["sensitive"])
        add_node(node_id, "var", str(var["key"]), detail="敏感" if secret else "")
        profile_node = profile_ids.get(int(var["profile_id"]))
        if profile_node:
            add_edge(profile_node, node_id, "declared")
        if not secret and var.get("value"):
            var_values.setdefault(key, str(var["value"]))

    # 档案中未加密的变量值也参与引用分析
    for scope_value in (env_values or {}).items():
        key, value = scope_value
        upper = str(key).upper()
        node_id = f"var:{upper}"
        add_node(node_id, "var", str(key), detail="环境变量")
        var_values.setdefault(upper, str(value))

    for key, value in var_values.items():
        for ref in references(value):
            target = f"var:{ref.upper()}"
            if target in nodes:
                add_edge(f"var:{key}", target, "referenced")

    runtime_nodes: list[dict[str, Any]] = []
    for runtime in runtimes:
        exe_path = str(runtime["exe_path"])
        node_id = f"runtime:{runtime['id']}"
        label = f"{runtime['kind']} {runtime['version'] or '未知'}"
        exists = os.path.exists(exe_path)
        add_node(node_id, "runtime", label, detail=exe_path, missing=not exists)
        exe_id = f"exe:{_norm(exe_path)}"
        add_node(exe_id, "exe", os.path.basename(exe_path) or exe_path, detail=exe_path, missing=not exists)
        add_edge(node_id, exe_id, "declared")
        runtime_nodes.append(
            {
                "node": node_id,
                "exe": exe_id,
                "dir": _norm(os.path.dirname(exe_path)),
                "path": exe_path,
            }
        )

    # 变量值指向运行时目录 → 推断依赖
    for runtime in runtime_nodes:
        directory = runtime["dir"]
        if not directory:
            continue
        for key, value in var_values.items():
            if directory in _norm(value):
                add_edge(f"var:{key}", runtime["node"], "referenced")

    issues: list[dict[str, Any]] = []
    for runtime in runtime_nodes:
        if not os.path.exists(runtime["path"]):
            issues.append(
                {
                    "code": "runtime.missing",
                    "severity": "error",
                    "detail": f"运行时记录指向不存在的文件：{runtime['path']}",
                }
            )

    provided = {str(key).upper() for key in (env_values or {})}
    for var in profile_vars:
        key = str(var["key"]).upper()
        if env_values is not None and key not in provided:
            issues.append(
                {
                    "code": "var.not-set",
                    "severity": "info",
                    "detail": f"档案变量 {var['key']} 在当前环境变量中不存在（激活档案时才会注入）。",
                }
            )

    for block_entry in split_path(path_value):
        expanded = expand_vars(block_entry, env_values or {})["value"]
        if not os.path.isdir(expanded):
            issues.append(
                {
                    "code": "path.missing",
                    "severity": "warn",
                    "detail": f"PATH 目录不存在：{block_entry}",
                }
            )

    for key, value in var_values.items():
        for ref in references(value):
            if f"var:{ref.upper()}" not in nodes:
                issues.append(
                    {
                        "code": "var.unresolved",
                        "severity": "warn",
                        "detail": f"{key} 引用了未定义的变量 %{ref}%。",
                    }
                )

    edge_list = list(edges.values())
    return {
        "nodes": list(nodes.values()),
        "edges": edge_list,
        "issues": issues,
        "summary": {
            "nodes": len(nodes),
            "edges": len(edge_list),
            "projects": sum(1 for node in nodes.values() if node["type"] == "project"),
            "profiles": sum(1 for node in nodes.values() if node["type"] == "profile"),
            "vars": sum(1 for node in nodes.values() if node["type"] == "var"),
            "runtimes": sum(1 for node in nodes.values() if node["type"] == "runtime"),
            "issues": len(issues),
        },
    }


def observed_resolution(graph: Mapping[str, Any], command: str, directories: list[str]) -> dict[str, Any] | None:
    """在给定 PATH 目录中解析命令，并与图中的运行时节点对照。"""
    result = resolve_command(command, directories)
    if not result["resolved"]:
        return None
    target = f"exe:{_norm(result['resolved'])}"
    known = any(node["id"] == target for node in graph.get("nodes", []))
    return {"command": command, "resolved": result["resolved"], "matched_runtime": known}


def _dot_escape(text: str) -> str:
    """转义 DOT 字符串中的反斜杠与双引号（Windows 路径与变量名都要安全）。"""
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def to_dot(graph: Mapping[str, Any]) -> str:
    shapes = {
        "project": "box",
        "profile": "ellipse",
        "var": "hexagon",
        "runtime": "component",
        "exe": "note",
    }
    lines = ["digraph envforge {", "  rankdir=LR;", '  node [fontname="Consolas"];']
    for node in graph.get("nodes", []):
        label = _dot_escape(node["label"])
        shape = shapes.get(node["type"], "box")
        style = ", style=dashed" if node.get("missing") else ""
        lines.append(f'  "{_dot_escape(node["id"])}" [label="{label}", shape={shape}{style}];')
    styles = {
        "declared": "",
        "referenced": ', style=dotted',
        "inferred": ', style=dashed',
    }
    for edge in graph.get("edges", []):
        lines.append(
            f'  "{_dot_escape(edge["source"])}" -> "{_dot_escape(edge["target"])}"'
            f' [label="{_dot_escape(edge["kind"])}"{styles.get(edge["kind"], "")}];'
        )
    lines.append("}")
    return "\n".join(lines) + "\n"
