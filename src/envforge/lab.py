"""环境实验室：在不改动系统的前提下模拟 PATH / 变量修改的影响。

能力：
- PATH 静态解析：按 Windows 规则（系统 PATH 在前、用户 PATH 在后）与 PATHEXT 预测命令来源；
- 变量循环引用检测与 %VAR% 展开；
- 影响分析：修改某个变量会影响哪些项目/档案；
- 证据等级：实测（measured）、声明（declared）、推断（inferred）、未知（unknown）。

实验室只读：不会真正写入 PATH，也不会启动被测命令。
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .db import Database
from .envvars import analyze_path, split_path

DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"
COMMON_COMMANDS = (
    "python",
    "python3",
    "node",
    "npm",
    "java",
    "javac",
    "git",
    "code",
    "pip",
    "cmake",
    "make",
    "gcc",
    "go",
    "cargo",
    "dotnet",
    "pwsh",
    "bash",
)

_VAR_REF = re.compile(r"%([^%]+)%")


def path_entries(value: str) -> list[str]:
    return split_path(value)


def references(value: str) -> list[str]:
    return [match.strip() for match in _VAR_REF.findall(value or "") if match.strip()]


def expand_vars(
    value: str, env: Mapping[str, str], *, max_depth: int = 8
) -> dict[str, Any]:
    """展开 %VAR% 引用；返回展开结果、缺失变量名与是否出现循环。"""
    lookup = {str(key).upper(): str(val) for key, val in env.items()}
    missing: list[str] = []
    seen = False

    def _expand(text: str, depth: int, stack: tuple[str, ...]) -> str:
        nonlocal seen

        def replace(match: re.Match[str]) -> str:
            nonlocal seen
            name = match.group(1).strip()
            upper = name.upper()
            if upper in stack:
                seen = True
                return match.group(0)
            if upper not in lookup:
                if name not in missing:
                    missing.append(name)
                return match.group(0)
            if depth >= max_depth:
                return match.group(0)
            return _expand(lookup[upper], depth + 1, stack + (upper,))

        return _VAR_REF.sub(replace, text)

    expanded = _expand(value or "", 0, ())
    return {"value": expanded, "missing": missing, "cycle": seen}


def find_cycles(
    env: Mapping[str, str],
    *,
    max_cycles: int = 200,
    max_steps: int = 50_000,
) -> list[list[str]]:
    """找出变量之间的循环引用（不区分大小写），返回变量名列表。

    对每个变量作为起点做深度优先搜索，且只经过不小于起点的节点，
    因此每个简单环都会以其字典序最小的节点为起点恰好报告一次（含共享节点的多个环）。

    稠密引用图可能产生指数级简单路径，这里用 ``max_steps`` 与 ``max_cycles``
    限制搜索规模，达到上限时返回已找到的环，避免 CLI 长时间卡住。
    """
    values: dict[str, str] = {}
    display: dict[str, str] = {}
    for key, value in env.items():
        upper = str(key).upper()
        display.setdefault(upper, str(key))
        values[upper] = str(value)
    nodes = sorted(values)
    graph: dict[str, list[str]] = {
        node: sorted({ref.upper() for ref in references(values[node]) if ref.upper() in values})
        for node in nodes
    }

    cycles: list[list[str]] = []
    steps = 0
    truncated = False

    def walk(start: str, node: str, stack: list[str], seen: set[str]) -> None:
        nonlocal steps, truncated
        if truncated:
            return
        for child in graph.get(node, []):
            steps += 1
            if steps > max_steps or len(cycles) >= max_cycles:
                truncated = True
                return
            if child == start:
                cycle = stack[:]
                if cycle not in cycles:
                    cycles.append(cycle)
            elif child > start and child not in seen:
                seen.add(child)
                stack.append(child)
                walk(start, child, stack, seen)
                stack.pop()
                seen.discard(child)

    for start in nodes:
        walk(start, start, [start], {start})
    return [[display.get(item, item) for item in cycle] for cycle in cycles]


def _exists_dir(path: str) -> bool:
    try:
        return os.path.isdir(path)
    except OSError:
        return False


def _actual_name(directory: str, name: str) -> str:
    """取目录中的真实文件名（Windows 大小写不敏感，返回磁盘上的写法）。"""
    try:
        for entry in os.listdir(directory):
            if os.path.normcase(entry) == os.path.normcase(name):
                return entry
    except OSError:
        return name
    return name


def resolve_command(
    command: str,
    directories: Iterable[str],
    *,
    pathext: str = DEFAULT_PATHEXT,
    exists: Callable[[str], bool] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """按目录顺序与 PATHEXT 预测命令来源。

    目录中的 ``%VAR%`` 在文件系统检查前展开；返回的目录与解析结果仍保留原始写法。
    """
    exists_fn = exists or os.path.isfile
    extensions = [ext for ext in pathext.split(";") if ext]
    name = command.strip()
    candidates: list[dict[str, Any]] = []
    for index, directory in enumerate(directories):
        directory = directory.strip()
        if not directory:
            continue
        if env is not None:
            expanded_dir = expand_vars(directory, env)["value"]
        else:
            expanded_dir = os.path.expandvars(directory)
        attempts = [name]
        if not os.path.splitext(name)[1]:
            attempts += [name + ext.lower() for ext in extensions] + [
                name + ext.upper() for ext in extensions
            ]
        for attempt in attempts:
            candidate = os.path.join(expanded_dir, attempt)
            try:
                present = bool(exists_fn(candidate))
            except OSError:
                present = False
            candidates.append({"directory": directory, "candidate": candidate, "exists": present})
            if present:
                actual_name = _actual_name(expanded_dir, attempt)
                return {
                    "command": name,
                    "resolved": os.path.join(directory, actual_name),
                    "resolved_actual": os.path.join(expanded_dir, actual_name),
                    "directory": directory,
                    "index": index,
                    "candidates": candidates,
                }
    return {
        "command": name,
        "resolved": "",
        "resolved_actual": "",
        "directory": "",
        "index": -1,
        "candidates": candidates,
    }


def simulate_path_change(
    *,
    base_path: str,
    add: Iterable[str] = (),
    remove: Iterable[str] = (),
    commands: Iterable[str] | None = None,
    env: Mapping[str, str] | None = None,
    exists: Callable[[str], bool] | None = None,
    dir_exists: Callable[[str], bool] | None = None,
    measured: Callable[[str], str | None] | None = None,
    position: str = "prepend",
) -> dict[str, Any]:
    """模拟增删 PATH 项后的命令解析变化；默认把新目录放到最前。"""
    env_map = dict(env or {})

    def _expand(text: str) -> str:
        # 提供了变量表时按其展开，否则沿用进程环境（保持旧行为）。
        if env is None:
            return os.path.expandvars(text)
        return expand_vars(text, env_map)["value"]

    pathext = str(env_map.get("PATHEXT", DEFAULT_PATHEXT))
    before_entries = path_entries(base_path)
    remove_set = {os.path.normcase(item.strip()) for item in remove if str(item).strip()}
    removed = [
        item
        for item in before_entries
        if os.path.normcase(item) in remove_set
        or os.path.normcase(_expand(item)) in remove_set
    ]
    kept = [item for item in before_entries if item not in removed]
    added = [str(item).strip() for item in add if str(item).strip()]
    if position == "append":
        after_entries = kept + added
    else:
        after_entries = added + kept

    selected_commands = list(commands or COMMON_COMMANDS)
    env_arg = env_map if env is not None else None
    results: list[dict[str, Any]] = []
    warnings: list[str] = []
    for command in selected_commands:
        before = resolve_command(
            command, before_entries, pathext=pathext, exists=exists, env=env_arg
        )
        after = resolve_command(
            command, after_entries, pathext=pathext, exists=exists, env=env_arg
        )
        before_evidence = "inferred"
        if measured is not None:
            try:
                actual = measured(command) or ""
            except Exception:  # pragma: no cover - 外部探测失败按推断处理
                actual = ""
            if actual:
                before_evidence = "measured"
                if not before["resolved"]:
                    warnings.append(
                        f"{command} 实测可解析到 {actual}，但静态 PATH 中未找到（"
                        "PATH 可能包含未建模的目录）。"
                    )
                elif os.path.normcase(actual) != os.path.normcase(before["resolved_actual"]):
                    warnings.append(
                        f"{command} 的静态解析结果与实测不一致："
                        f"{before['resolved_actual']}（推断） vs {actual}（实测）。"
                    )
        changed = os.path.normcase(before["resolved_actual"]) != os.path.normcase(
            after["resolved_actual"]
        )
        results.append(
            {
                "command": command,
                "before": before["resolved"],
                "after": after["resolved"],
                "changed": changed,
                "before_index": before["index"],
                "after_index": after["index"],
                "before_evidence": before_evidence,
                "after_evidence": "inferred",
                "loses": bool(before["resolved_actual"] and not after["resolved_actual"]),
                "gains": bool(after["resolved_actual"] and not before["resolved_actual"]),
            }
        )

    analysis = analyze_path(
        ";".join(after_entries), exists_fn=dir_exists or _exists_dir, expand_fn=_expand
    )
    missing = [item.raw for item in analysis if item.raw and not item.exists]
    duplicates = [item.raw for item in analysis if item.duplicate_of is not None]
    return {
        "path": {
            "before": before_entries,
            "after": after_entries,
            "added": added,
            "removed": removed,
            "missing": missing,
            "duplicates": duplicates,
        },
        "commands": results,
        "warnings": warnings,
        "evidence": "measured" if measured is not None else "inferred",
        "notes": [
            "静态解析按 Windows PATH 顺序（系统在前、用户在后）与 PATHEXT 规则；",
            "after 始终是推断结果，实际生效还需重新打开终端或广播环境变更。",
        ],
    }


def combined_path(
    system_path: str, user_path: str, *, provider: str = "system-first"
) -> str:
    """Windows 进程 PATH = 系统 PATH + 用户 PATH；这里显式建模该顺序。"""
    parts = [system_path, user_path] if provider == "system-first" else [user_path, system_path]
    return ";".join(part for part in parts if part)


def impact(db: Database, key: str, *, scopes: Mapping[str, str] | None = None) -> dict[str, Any]:
    """分析修改某个变量会影响哪些项目/档案，以及哪些变量引用了它。"""
    target = key.strip()
    if not target:
        raise ValueError("变量名不能为空。")
    rows = db.query(
        "SELECT p.name AS project, f.name AS profile, v.key AS key"
        " FROM profile_vars v"
        " JOIN profiles f ON f.id = v.profile_id"
        " JOIN projects p ON p.id = f.project_id"
        " WHERE v.key = ? COLLATE NOCASE"
        " ORDER BY p.name COLLATE NOCASE, f.name COLLATE NOCASE",
        (target,),
    )
    projects: dict[str, list[str]] = {}
    for row in rows:
        projects.setdefault(row["project"], []).append(row["profile"])

    referencing: list[str] = []
    for name, value in (scopes or {}).items():
        if any(ref.upper() == target.upper() for ref in references(str(value))):
            referencing.append(str(name))
    return {
        "key": target,
        "projects": [
            {"project": project, "profiles": profiles}
            for project, profiles in projects.items()
        ],
        "referenced_by": referencing,
        "summary": {
            "projects": len(projects),
            "profiles": sum(len(items) for items in projects.values()),
            "referencing": len(referencing),
        },
    }
