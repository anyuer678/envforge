"""EnvForge Environment Specification（Spec v1）。

规格文件用 TOML 描述“一台合格的开发机应该是什么样子”：工具与版本约束、
环境变量、必须存在的目录、必须可执行的命令与端口。同一份规格可用于：

- capture：从当前机器采集并生成规格（敏感值只记录键名，不写明文）；
- check：逐项核对并给出证据等级（measured / declared / inferred / unknown）；
- diff：比较两份规格（两台机器、两个项目、两个时间点）；
- plan：把缺失项转成安装与配置计划（只生成脚本，不自动执行）。

本模块只做解析、校验与决策，不读写数据库、不改动系统环境。
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .errors import ValidationError
from .models import looks_sensitive_key, now_iso, parse_version

SPEC_VERSION = "1"

SECTION_LABELS = {
    "machine": "机器",
    "tool": "工具",
    "env": "环境变量",
    "path": "目录",
    "command": "命令",
    "port": "端口",
}

STATUS_LABELS = {
    "ok": "通过",
    "missing": "缺失",
    "mismatch": "不符",
    "unknown": "未知",
    "skipped": "跳过",
    "error": "失败",
}

EVIDENCE_LABELS = {
    "measured": "实测",
    "declared": "声明",
    "inferred": "推断",
    "unknown": "未知",
}

# 机器相关、换机后必然不同，不进入工作站画像。
MACHINE_SPECIFIC_ENV = {
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "HOMESHARE",
    "COMPUTERNAME",
    "USERNAME",
    "USERDOMAIN",
    "USERDNSDOMAIN",
    "APPDATA",
    "LOCALAPPDATA",
    "TEMP",
    "TMP",
    "PATHEXT",
    "COMSPEC",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)",
    "COMMONPROGRAMW6432",
    "PROGRAMDATA",
    "ALLUSERSPROFILE",
    "PUBLIC",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL",
    "PROCESSOR_REVISION",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "DRIVERDATA",
    "PSMODULEPATH",
    "ONEDRIVE",
    "SESSIONNAME",
    "LOGONSERVER",
    "USERPROFILE (X86)",
}

DEV_ENV_PATTERN = re.compile(
    r"(JAVA|JDK|JRE|MAVEN|GRADLE|PYTHON|PYTHONPATH|PIP|VIRTUAL_ENV|CONDA"
    r"|NODE|NPM|YARN|PNPM|NVM|GIT|GOPATH|GOROOT|GOBIN|CARGO|RUSTUP|CMAKE"
    r"|MSYS|MINGW|VCPKG|DOCKER|KUBERNETES|KUBECONFIG|ANDROID|FLUTTER|DART"
    r"|CUDA|HOMEBREW|BUN|DENO|DOTNET|NUGET|CHOCOLATEY|SCOOP|AZURE|AWS|GCLOUD"
    r"|SSH_|EDITOR|TERM|LANG|LC_|PGSQL|MYSQL|REDIS|MONGO)",
    re.IGNORECASE,
)

_SYSTEM_DIR_PATTERN = re.compile(
    r"^(%SystemRoot%|%windir%|C:\\Windows|C:\\Program Files\\WindowsApps)",
    re.IGNORECASE,
)

# URL 里带 user:password@ 的凭据（如 PIP_INDEX_URL），即使键名不含敏感词也不能落盘。
_URL_USERINFO = re.compile(r"://[^/@\s]+:[^/@\s]+@")


# ---------------------------------------------------------------------- 模型
@dataclass(slots=True)
class MachineSpec:
    os: str = ""
    arch: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"os": self.os, "arch": self.arch}


@dataclass(slots=True)
class SpecTool:
    id: str
    version: str = ""
    source: str = ""
    kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "source": self.source,
            "kind": self.kind,
        }


@dataclass(slots=True)
class SpecEnvVar:
    key: str
    scope: str = "user"
    value: str | None = None
    secret: bool = False
    required: bool = True

    def to_dict(self, *, reveal: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "key": self.key,
            "scope": self.scope,
            "secret": self.secret,
            "required": self.required,
        }
        if self.value is not None and (reveal or not self.secret):
            data["value"] = self.value
        return data


@dataclass(slots=True)
class SpecPath:
    path: str
    must_exist: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "must_exist": self.must_exist}


@dataclass(slots=True)
class SpecCommand:
    run: str
    expect: str = ""
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"run": self.run, "expect": self.expect, "required": self.required}


@dataclass(slots=True)
class SpecPort:
    port: int
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"port": self.port, "required": self.required}


@dataclass(slots=True)
class EnvironmentSpec:
    version: str = SPEC_VERSION
    name: str = ""
    kind: str = "workstation"
    machine: MachineSpec = field(default_factory=MachineSpec)
    tools: list[SpecTool] = field(default_factory=list)
    env: list[SpecEnvVar] = field(default_factory=list)
    paths: list[SpecPath] = field(default_factory=list)
    commands: list[SpecCommand] = field(default_factory=list)
    ports: list[SpecPort] = field(default_factory=list)
    source: str = ""

    def to_dict(self, *, reveal: bool = False) -> dict[str, Any]:
        return {
            "spec": {
                "version": self.version,
                "name": self.name,
                "kind": self.kind,
                "source": self.source,
            },
            "machine": self.machine.to_dict(),
            "tools": [item.to_dict() for item in self.tools],
            "env": [item.to_dict(reveal=reveal) for item in self.env],
            "paths": [item.to_dict() for item in self.paths],
            "commands": [item.to_dict() for item in self.commands],
            "ports": [item.to_dict() for item in self.ports],
        }

    def counts(self) -> dict[str, int]:
        return {
            "tools": len(self.tools),
            "env": len(self.env),
            "paths": len(self.paths),
            "commands": len(self.commands),
            "ports": len(self.ports),
        }


# ---------------------------------------------------------------------- 版本约束
_VERSION_NUM = re.compile(r"\d+(?:\.\d+)*")
_WIDTH = 8


def version_key(text: str | None) -> tuple[int, ...]:
    """提取版本号的数字段，允许前缀 v 与附加后缀；无法识别时返回空元组。"""
    if not text:
        return ()
    match = _VERSION_NUM.search(text.lstrip("vV").strip())
    if not match:
        return ()
    return tuple(int(part) for part in match.group(0).split("."))


def _padded(parts: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(parts[:_WIDTH]) + (0,) * max(0, _WIDTH - len(parts[:_WIDTH]))


def _compare(actual: tuple[int, ...], required: tuple[int, ...]) -> int:
    # 只比到双方都具备的精度：``<=21`` 接受 21.0.2，``>=3.11`` 接受 3.12.3。
    width = min(len(actual), len(required))
    left, right = _padded(actual[:width]), _padded(required[:width])
    if left < right:
        return -1
    if left > right:
        return 1
    return 0


def version_satisfies(actual: str | None, constraint: str | None) -> bool:
    """判断实际版本是否满足约束。

    约束语法：逗号分隔的多条，支持 ``>=``、``>``、``<=``、``<``、``==``、``!=``；
    不带运算符（或带 ``x`` 通配）时按前缀匹配，例如 ``3.12`` 接受 3.12.9。
    """
    required = (constraint or "").strip()
    actual_parts = version_key(actual)
    if not required:
        return bool(actual_parts)
    if not actual_parts:
        return False
    for token in (part.strip() for part in required.split(",")):
        if not token:
            continue
        match = re.match(r"^(>=|<=|==|!=|>|<)?\s*(.+)$", token)
        if not match:
            return False
        operator = match.group(1) or ""
        target_text = match.group(2).strip()
        wildcard = bool(re.search(r"[xX*]", target_text))
        target = version_key(target_text)
        if not target and not wildcard:
            return False
        if not wildcard and operator in ("", "=="):
            if operator == "==":
                if _compare(actual_parts, target) != 0:
                    return False
            elif actual_parts[: len(target)] != target:
                return False
            continue
        if wildcard:
            # 通配符只支持前缀匹配（可带 != 否定）；>=、<= 等运算符与通配符组合无意义，视为无效约束。
            matched = actual_parts[: len(target)] == target
            if operator == "!=":
                if matched:
                    return False
            elif operator in ("", "=="):
                if not matched:
                    return False
            else:
                return False
            continue
        relation = _compare(actual_parts, target)
        if operator == ">=" and relation < 0:
            return False
        if operator == ">" and relation <= 0:
            return False
        if operator == "<=" and relation > 0:
            return False
        if operator == "<" and relation >= 0:
            return False
        if operator == "==" and relation != 0:
            return False
        if operator == "!=" and relation == 0:
            return False
    return True


# ---------------------------------------------------------------------- 读写
def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _flag(value: Any, default: bool) -> bool:
    if value is None:
        return default
    return bool(value)


def parse_spec(data: Mapping[str, Any], *, source: str = "") -> EnvironmentSpec:
    if not isinstance(data, Mapping):
        raise ValidationError("规格顶层必须是 TOML 表。")
    meta = data.get("spec")
    if not isinstance(meta, Mapping):
        raise ValidationError("规格缺少 [spec] 段。")
    version = _text(meta.get("version"), SPEC_VERSION)
    if version != SPEC_VERSION:
        raise ValidationError(
            f"不支持的规格版本 {version!r}；本程序支持 Spec v{SPEC_VERSION}。"
        )
    spec = EnvironmentSpec(
        version=version,
        name=_text(meta.get("name"), "未命名规格"),
        kind=_text(meta.get("kind"), "workstation"),
        source=source,
    )
    machine = data.get("machine")
    if isinstance(machine, Mapping):
        spec.machine = MachineSpec(
            os=_text(machine.get("os")).lower(),
            arch=_text(machine.get("arch")).lower(),
        )

    for index, raw in enumerate(_section(data, "tools"), start=1):
        if not isinstance(raw, Mapping):
            raise ValidationError(f"[[tools]] 第 {index} 项必须是表。")
        tool_id = _text(raw.get("id"))
        if not tool_id:
            raise ValidationError(f"[[tools]] 第 {index} 项缺少 id。")
        spec.tools.append(
            SpecTool(
                id=tool_id,
                version=_text(raw.get("version")),
                source=_text(raw.get("source")),
                kind=_text(raw.get("kind")),
            )
        )

    for index, raw in enumerate(_section(data, "env"), start=1):
        if not isinstance(raw, Mapping):
            raise ValidationError(f"[[env]] 第 {index} 项必须是表。")
        key = _text(raw.get("key"))
        if not key:
            raise ValidationError(f"[[env]] 第 {index} 项缺少 key。")
        scope = _text(raw.get("scope"), "user").lower()
        if scope not in ("user", "system", "session"):
            raise ValidationError(
                f"[[env]] {key} 的 scope 为 {scope!r}；可选 user / system / session。"
            )
        secret = _flag(raw.get("secret"), False)
        value = raw.get("value")
        spec.env.append(
            SpecEnvVar(
                key=key,
                scope=scope,
                value=None if secret or value is None else str(value),
                secret=secret,
                required=_flag(raw.get("required"), True),
            )
        )

    for index, raw in enumerate(_section(data, "paths"), start=1):
        if not isinstance(raw, Mapping):
            raise ValidationError(f"[[paths]] 第 {index} 项必须是表。")
        path = _text(raw.get("path"))
        if not path:
            raise ValidationError(f"[[paths]] 第 {index} 项缺少 path。")
        spec.paths.append(SpecPath(path=path, must_exist=_flag(raw.get("must_exist"), True)))

    for index, raw in enumerate(_section(data, "commands"), start=1):
        if not isinstance(raw, Mapping):
            raise ValidationError(f"[[commands]] 第 {index} 项必须是表。")
        run = _text(raw.get("run"))
        if not run:
            raise ValidationError(f"[[commands]] 第 {index} 项缺少 run。")
        spec.commands.append(
            SpecCommand(
                run=run,
                expect=_text(raw.get("expect")),
                required=_flag(raw.get("required"), True),
            )
        )

    for index, raw in enumerate(_section(data, "ports"), start=1):
        if not isinstance(raw, Mapping):
            raise ValidationError(f"[[ports]] 第 {index} 项必须是表。")
        try:
            port = int(raw.get("port"))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"[[ports]] 第 {index} 项 port 必须是整数。") from exc
        if not 1 <= port <= 65535:
            raise ValidationError(f"[[ports]] 端口 {port} 超出 1-65535。")
        spec.ports.append(SpecPort(port=port, required=_flag(raw.get("required"), True)))
    return spec


def _section(data: Mapping[str, Any], name: str) -> list[Any]:
    value = data.get(name, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError(f"[[{name}]] 必须是表数组。")
    return value


def load_spec(path: str | Path) -> EnvironmentSpec:
    target = Path(path)
    if not target.exists():
        raise ValidationError(f"规格文件不存在：{target}；请先用 `envforge spec capture` 生成。")
    if target.is_dir():
        raise ValidationError(f"{target} 是目录；请指定 .toml 规格文件。")
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"无法读取规格文件 {target}：{exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(f"规格文件不是合法的 TOML：{exc}") from exc
    return parse_spec(data, source=str(target))


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def dump_spec(spec: EnvironmentSpec) -> str:
    lines = [
        "# EnvForge Environment Specification v1",
        "# 由 EnvForge 生成；可直接编辑后用于 check / diff / plan。",
        "",
        "[spec]",
        f"version = {_quote(spec.version)}",
        f"name = {_quote(spec.name)}",
        f"kind = {_quote(spec.kind)}",
    ]
    if spec.machine.os or spec.machine.arch:
        lines += ["", "[machine]"]
        if spec.machine.os:
            lines.append(f"os = {_quote(spec.machine.os)}")
        if spec.machine.arch:
            lines.append(f"arch = {_quote(spec.machine.arch)}")
    for tool in spec.tools:
        lines += ["", "[[tools]]", f"id = {_quote(tool.id)}"]
        if tool.version:
            lines.append(f"version = {_quote(tool.version)}")
        if tool.source:
            lines.append(f"source = {_quote(tool.source)}")
        if tool.kind:
            lines.append(f"kind = {_quote(tool.kind)}")
    for var in spec.env:
        lines += ["", "[[env]]", f"scope = {_quote(var.scope)}", f"key = {_quote(var.key)}"]
        if var.value is not None and not var.secret:
            lines.append(f"value = {_quote(var.value)}")
        if var.secret:
            lines.append("secret = true")
        if not var.required:
            lines.append("required = false")
    for path in spec.paths:
        lines += ["", "[[paths]]", f"path = {_quote(path.path)}"]
        if not path.must_exist:
            lines.append("must_exist = false")
    for command in spec.commands:
        lines += ["", "[[commands]]", f"run = {_quote(command.run)}"]
        if command.expect:
            lines.append(f"expect = {_quote(command.expect)}")
        if not command.required:
            lines.append("required = false")
    for port in spec.ports:
        lines += ["", "[[ports]]", f"port = {port.port}"]
        if not port.required:
            lines.append("required = false")
    return "\n".join(lines).rstrip() + "\n"


def save_spec(spec: EnvironmentSpec, path: str | Path) -> Path:
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dump_spec(spec), encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"无法写入规格文件 {target}：{exc}") from exc
    spec.source = str(target)
    return target


# ---------------------------------------------------------------------- 核对
@dataclass(slots=True)
class CheckContext:
    """核对所需的全部外部输入；测试中可注入假实现，不触碰真实系统。"""

    env: dict[tuple[str, str], str] = field(default_factory=dict)
    which: Callable[[str], str | None] = field(default_factory=lambda: _default_which)
    probe: Callable[[str, str], tuple[str, str]] | None = None
    exists: Callable[[str], bool] = field(default_factory=lambda: os.path.isdir)
    run: Callable[[str], tuple[int, str]] | None = None
    known_tools: dict[str, str] = field(default_factory=dict)
    port_checker: Callable[[int], bool] | None = None
    software_checked: bool = False

    def env_value(self, scope: str, key: str) -> str | None:
        direct = self.env.get((scope, key))
        if direct is not None:
            return direct
        return self.env.get((scope, key.upper()))


def _default_which(command: str) -> str | None:
    import shutil

    return shutil.which(command)


def make_check_item(
    section: str,
    target: str,
    status: str,
    *,
    expected: str = "",
    actual: str = "",
    evidence: str = "declared",
    detail: str = "",
    suggestion: str = "",
    ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "section": section,
        "section_label": SECTION_LABELS.get(section, section),
        "target": target,
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "expected": expected,
        "actual": actual,
        "evidence": evidence,
        "evidence_label": EVIDENCE_LABELS.get(evidence, evidence),
        "detail": detail,
        "suggestion": suggestion,
        "ref": ref or {},
    }


def check_spec(
    spec: EnvironmentSpec,
    context: CheckContext | None = None,
    *,
    machine: Mapping[str, Any] | None = None,
    run_commands: bool = False,
    check_ports: bool = False,
) -> dict[str, Any]:
    """逐项核对规格。默认不执行任何命令、不探测端口，除非显式开启。"""
    ctx = context or CheckContext()
    items: list[dict[str, Any]] = []
    current_machine = dict(machine or {})

    actual_os = _text(current_machine.get("os")).lower()
    if spec.machine.os:
        if actual_os and actual_os != spec.machine.os:
            items.append(
                make_check_item(
                    "machine",
                    "os",
                    "mismatch",
                    expected=spec.machine.os,
                    actual=actual_os,
                    evidence="measured",
                    detail="操作系统类型与规格不一致。",
                    suggestion="确认该规格是否适用于当前平台。",
                )
            )
        else:
            items.append(
                make_check_item(
                    "machine",
                    "os",
                    "ok" if actual_os else "unknown",
                    expected=spec.machine.os,
                    actual=actual_os,
                    evidence="measured" if actual_os else "unknown",
                    detail="操作系统一致。" if actual_os else "未能获取当前操作系统信息。",
                )
            )
    if spec.machine.arch:
        actual_arch = _text(current_machine.get("arch")).lower()
        if actual_arch and actual_arch != spec.machine.arch:
            items.append(
                make_check_item(
                    "machine",
                    "arch",
                    "mismatch",
                    expected=spec.machine.arch,
                    actual=actual_arch,
                    evidence="measured",
                    detail="CPU 架构与规格不一致。",
                    suggestion="更换对应架构的安装包。",
                )
            )
        elif actual_arch:
            items.append(
                make_check_item(
                    "machine",
                    "arch",
                    "ok",
                    expected=spec.machine.arch,
                    actual=actual_arch,
                    evidence="measured",
                    detail="CPU 架构一致。",
                )
            )

    # 工具
    for tool in spec.tools:
        path = ctx.which(tool.id)
        actual_version = ctx.known_tools.get(tool.id, "")
        evidence = "declared" if actual_version else "unknown"
        if ctx.probe and path:
            probed, _output = ctx.probe(path, tool.kind or "custom")
            if probed:
                actual_version = probed
                evidence = "measured"
        expected = tool.version
        ref = tool.to_dict()
        if tool.kind == "software" and not ctx.software_checked and not actual_version:
            items.append(
                make_check_item(
                    "tool",
                    tool.id,
                    "skipped",
                    expected=expected,
                    actual=actual_version,
                    evidence="unknown",
                    detail="未提供软件清单，无法核对该项（加 --software 可扫描已安装软件）。",
                    ref=ref,
                )
            )
            continue
        if not path and not actual_version:
            items.append(
                make_check_item(
                    "tool",
                    tool.id,
                    "missing",
                    expected=expected,
                    actual="",
                    evidence="measured",
                    detail="PATH 中找不到该命令，也没有已登记的运行时记录。",
                    suggestion=f"安装 {tool.id} 后重新运行 check。",
                    ref=ref,
                )
            )
            continue
        if expected and not actual_version:
            items.append(
                make_check_item(
                    "tool",
                    tool.id,
                    "unknown",
                    expected=expected,
                    actual="",
                    evidence="declared",
                    detail="已找到命令，但未探测到版本；可用 --probe 实测。",
                    ref=ref,
                )
            )
            continue
        if expected and not version_satisfies(actual_version, expected):
            items.append(
                make_check_item(
                    "tool",
                    tool.id,
                    "mismatch",
                    expected=expected,
                    actual=actual_version,
                    evidence=evidence,
                    detail=f"实际版本 {actual_version} 不在约束 {expected} 范围内。",
                    suggestion=f"升级或切换 {tool.id} 到满足 {expected} 的版本。",
                    ref=ref,
                )
            )
            continue
        items.append(
            make_check_item(
                "tool",
                tool.id,
                "ok",
                expected=expected,
                actual=actual_version,
                evidence=evidence,
                detail=path or "已登记版本",
                ref=ref,
            )
        )

    # 环境变量
    for var in spec.env:
        actual = ctx.env_value(var.scope, var.key)
        target = f"{var.scope}:{var.key}"
        ref = var.to_dict()
        if actual is None:
            items.append(
                make_check_item(
                    "env",
                    target,
                    "missing" if var.required else "skipped",
                    expected="存在" + ("" if var.value is None else f"={var.value}"),
                    evidence="declared",
                    detail="当前环境中不存在该变量。" if var.required else "规格未要求必须存在。",
                    suggestion=f"设置 {var.key}（作用域 {var.scope}）。" if var.required else "",
                    ref=ref,
                )
            )
            continue
        if var.secret:
            items.append(
                make_check_item(
                    "env",
                    target,
                    "ok",
                    expected="存在（敏感值不入库）",
                    actual="已设置",
                    evidence="declared",
                    detail="变量已设置；按隐私设计不读取、不比较敏感值。",
                    ref=ref,
                )
            )
            continue
        if var.value is not None and actual != var.value:
            items.append(
                make_check_item(
                    "env",
                    target,
                    "mismatch",
                    expected=var.value,
                    actual=actual,
                    evidence="declared",
                    detail="变量值与规格不同。",
                    suggestion="确认期望值后更新变量，或调整规格。",
                    ref=ref,
                )
            )
            continue
        items.append(
            make_check_item(
                "env",
                target,
                "ok",
                expected=var.value if var.value is not None else "存在",
                actual=actual,
                evidence="declared",
                detail="变量存在且与规格一致。",
                ref=ref,
            )
        )

    # 目录
    for entry in spec.paths:
        present = _safe_exists(ctx.exists, entry.path)
        items.append(
            make_check_item(
                "path",
                entry.path,
                "ok" if present else ("missing" if entry.must_exist else "skipped"),
                expected="存在" if entry.must_exist else "不强制",
                actual="存在" if present else "不存在",
                evidence="measured",
                detail="目录存在。" if present else "目录不存在。",
                suggestion="创建目录或安装对应工具。" if not present and entry.must_exist else "",
                ref=entry.to_dict(),
            )
        )

    # 命令
    for command in spec.commands:
        ref = command.to_dict()
        if not run_commands or ctx.run is None:
            items.append(
                make_check_item(
                    "command",
                    command.run,
                    "skipped",
                    expected=command.expect,
                    evidence="declared",
                    detail="未开启命令执行（加 --commands 才会实际运行）。",
                    ref=ref,
                )
            )
            continue
        code, output = ctx.run(command.run)
        text = output.strip()
        if code != 0:
            items.append(
                make_check_item(
                    "command",
                    command.run,
                    "error",
                    expected=command.expect,
                    actual=f"退出码 {code}",
                    evidence="measured",
                    detail=text[:300] or "命令执行失败。",
                    suggestion="修复工具链或确认该命令可在当前终端执行。",
                    ref=ref,
                )
            )
            continue
        if command.expect and command.expect not in text:
            items.append(
                make_check_item(
                    "command",
                    command.run,
                    "mismatch",
                    expected=command.expect,
                    actual=text[:200],
                    evidence="measured",
                    detail="输出中未找到期望文本。",
                    ref=ref,
                )
            )
            continue
        items.append(
            make_check_item(
                "command",
                command.run,
                "ok",
                expected=command.expect,
                actual=text[:200],
                evidence="measured",
                detail="命令执行成功。",
                ref=ref,
            )
        )

    # 端口
    for port in spec.ports:
        ref = port.to_dict()
        if not check_ports or ctx.port_checker is None:
            items.append(
                make_check_item(
                    "port",
                    str(port.port),
                    "skipped",
                    expected="可用",
                    evidence="declared",
                    detail="未开启端口检查（加 --ports 才会检测占用）。",
                    ref=ref,
                )
            )
            continue
        available = bool(ctx.port_checker(port.port))
        items.append(
            make_check_item(
                "port",
                str(port.port),
                "ok" if available else ("mismatch" if port.required else "skipped"),
                expected="可用",
                actual="空闲" if available else "被占用",
                evidence="measured",
                detail="端口空闲。" if available else f"端口 {port.port} 已被占用。",
                suggestion="释放端口或改用其他端口。" if not available else "",
                ref=ref,
            )
        )

    summary = summarize_items(items)
    return {
        "spec": {
            "version": spec.version,
            "name": spec.name,
            "kind": spec.kind,
            "source": spec.source,
            "counts": spec.counts(),
        },
        "generated_at": now_iso(),
        "machine": current_machine or None,
        "items": items,
        "summary": summary,
        "score": health_score(summary),
    }


def _safe_exists(fn: Callable[[str], bool], path: str) -> bool:
    try:
        return bool(fn(path))
    except OSError:
        return False


def summarize_items(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in STATUS_LABELS}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    counts["total"] = len(items)
    return counts


def health_score(summary: Mapping[str, int]) -> dict[str, Any]:
    """环境健康评分：从 100 分扣减，只用于快速判断，不代表绝对安全等级。"""
    score = 100
    score -= 15 * int(summary.get("missing", 0))
    score -= 15 * int(summary.get("error", 0))
    score -= 8 * int(summary.get("mismatch", 0))
    score -= 3 * int(summary.get("unknown", 0))
    score = max(0, min(100, score))
    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D"
    return {"score": score, "grade": grade}


# ---------------------------------------------------------------------- 差异
def _tool_key(tool: SpecTool) -> str:
    return tool.id.strip().lower()


def _env_key(var: SpecEnvVar) -> str:
    return f"{var.scope}:{var.key}".upper()


def _diff_entries(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
    key_fn: Callable[[dict[str, Any]], str],
) -> dict[str, list[dict[str, Any]]]:
    a = {key_fn(item): item for item in left}
    b = {key_fn(item): item for item in right}
    added = [b[key] for key in b if key not in a]
    removed = [a[key] for key in a if key not in b]
    changed = [
        {"before": a[key], "after": b[key]}
        for key in b
        if key in a and a[key] != b[key]
    ]
    return {"added": added, "removed": removed, "changed": changed}


def diff_specs(left: EnvironmentSpec, right: EnvironmentSpec) -> dict[str, Any]:
    sections = {
        "tools": _diff_entries(
            [item.to_dict() for item in left.tools],
            [item.to_dict() for item in right.tools],
            lambda item: str(item["id"]).lower(),
        ),
        "env": _diff_entries(
            [item.to_dict() for item in left.env],
            [item.to_dict() for item in right.env],
            lambda item: f"{item['scope']}:{item['key']}".upper(),
        ),
        "paths": _diff_entries(
            [item.to_dict() for item in left.paths],
            [item.to_dict() for item in right.paths],
            lambda item: os.path.normcase(str(item["path"])),
        ),
        "commands": _diff_entries(
            [item.to_dict() for item in left.commands],
            [item.to_dict() for item in right.commands],
            lambda item: str(item["run"]),
        ),
        "ports": _diff_entries(
            [item.to_dict() for item in left.ports],
            [item.to_dict() for item in right.ports],
            lambda item: str(item["port"]),
        ),
    }
    summary = {
        section: {
            "added": len(data["added"]),
            "removed": len(data["removed"]),
            "changed": len(data["changed"]),
        }
        for section, data in sections.items()
    }
    summary["total"] = sum(
        value["added"] + value["removed"] + value["changed"] for value in summary.values()
    )
    machine_changed = left.machine.to_dict() != right.machine.to_dict()
    return {
        "left": {"name": left.name, "source": left.source},
        "right": {"name": right.name, "source": right.source},
        "machine": {
            "changed": machine_changed,
            "before": left.machine.to_dict(),
            "after": right.machine.to_dict(),
        },
        "sections": sections,
        "summary": summary,
    }


# ---------------------------------------------------------------------- 计划
def install_command(tool: SpecTool) -> tuple[str, bool]:
    """根据 source 声明生成安装命令；返回（命令, 是否可自动执行）。"""
    source = (tool.source or "").strip()
    lowered = source.lower()
    if lowered.startswith("winget:"):
        package = source.split(":", 1)[1].strip()
        if package:
            return (
                f"winget install --id {package} --accept-package-agreements "
                "--accept-source-agreements --silent",
                True,
            )
    if lowered.startswith("scoop:"):
        package = source.split(":", 1)[1].strip()
        if package:
            return f"scoop install {package}", True
    if lowered.startswith("choco:"):
        package = source.split(":", 1)[1].strip()
        if package:
            return f"choco install {package} -y", True
    return "", False


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_plan(
    spec: EnvironmentSpec, report: Mapping[str, Any], *, shell: str = "powershell"
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    for item in report.get("items", []):
        if item.get("status") not in ("missing", "mismatch", "error"):
            continue
        section = item["section"]
        ref = item.get("ref") or {}
        step = {
            "section": section,
            "target": item["target"],
            "reason": item["detail"] or item["status_label"],
            "command": "",
            "automatic": False,
            "manual": True,
        }
        if section == "tool":
            tool = SpecTool(
                id=str(ref.get("id", item["target"])),
                version=str(ref.get("version", "")),
                source=str(ref.get("source", "")),
                kind=str(ref.get("kind", "")),
            )
            command, automatic = install_command(tool)
            if command:
                step.update(manual=False, automatic=automatic, command=command)
            else:
                step["reason"] += " 未声明安装来源，需要手工安装或补充 source = \"winget:<包名>\"。"
        elif section == "env":
            key = str(ref.get("key", item["target"].split(":")[-1]))
            scope = str(ref.get("scope", "user"))
            value = ref.get("value")
            if ref.get("secret") or value is None:
                step["reason"] += " 该项为敏感值或只声明存在性，请手工设置。"
            elif scope == "session":
                step.update(
                    manual=False,
                    automatic=False,
                    command=f"$env:{key} = {_ps_quote(str(value))}",
                )
            else:
                hive = "Machine" if scope == "system" else "User"
                step.update(
                    manual=False,
                    automatic=False,
                    command=(
                        f"[Environment]::SetEnvironmentVariable({_ps_quote(key)}, "
                        f"{_ps_quote(str(value))}, {_ps_quote(hive)})"
                    ),
                )
        elif section == "path":
            step.update(
                manual=False,
                automatic=True,
                command=f"New-Item -ItemType Directory -Force -Path {_ps_quote(item['target'])} | Out-Null",
            )
        elif section == "port":
            step["reason"] += " 端口冲突需要人工确认占用进程后处理。"
        elif section == "command":
            step["reason"] += " 命令失败通常由工具链缺失或 PATH 解析问题导致，请先修复对应工具。"
        steps.append(step)

    lines = [
        "# EnvForge 环境计划（请逐条确认后执行；EnvForge 不会自动运行本脚本）",
        f"# 规格：{spec.name}（{spec.source or '未保存'}）",
        "",
    ]
    if not steps:
        lines.append("# 没有需要处理的差异。")
    for index, step in enumerate(steps, start=1):
        lines.append(f"# [{index}] {step['section']} {step['target']}：{step['reason']}")
        if step["command"]:
            lines.append(step["command"])
        else:
            lines.append("# （需要手工处理）")
        lines.append("")
    return {
        "spec": {"name": spec.name, "source": spec.source},
        "shell": shell,
        "steps": steps,
        "script": "\n".join(lines).rstrip() + "\n",
    }


# ---------------------------------------------------------------------- 采集
def _slug(text: str) -> str:
    # 与 collectors.normalize_software_id 保持一致：保留 Unicode 字符，
    # 否则中文软件名会退化成 "item" 并在核对时永远匹配不上。
    cleaned = re.sub(r"[^\w.-]+", "-", text.strip().lower()).strip("-")
    return cleaned or "item"


def _unique(items: list[SpecTool]) -> list[SpecTool]:
    seen: set[str] = set()
    result: list[SpecTool] = []
    for item in items:
        key = item.id.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def capture_from_payload(
    payload: Mapping[str, Any],
    *,
    name: str = "",
    kind: str = "workstation",
) -> EnvironmentSpec:
    """把采集结果转成规格。只保留键名与版本，敏感值与机器相关值不落盘。"""
    machine_raw = payload.get("machine") or {}
    spec = EnvironmentSpec(
        version=SPEC_VERSION,
        name=name or _text(machine_raw.get("hostname"), "开发机"),
        kind=kind,
        machine=MachineSpec(
            os=_text(machine_raw.get("os")).lower(),
            arch=_text(machine_raw.get("arch")).lower(),
        ),
    )

    runtime_tools: list[SpecTool] = []
    for runtime in sorted(
        payload.get("runtimes") or [],
        key=lambda item: _padded(version_key(str(item.get("version", "")))),
        reverse=True,
    ):
        kind_name = _text(runtime.get("kind"))
        if not kind_name:
            continue
        runtime_tools.append(
            SpecTool(
                id=kind_name,
                version=_text(runtime.get("version")),
                source="detected",
                kind="runtime",
            )
        )
    runtime_tools = _unique(runtime_tools)

    software_tools: list[SpecTool] = []
    for software in (payload.get("software") or [])[:50]:
        label = _text(software.get("name"))
        if not label:
            continue
        software_tools.append(
            SpecTool(
                id=_slug(label),
                version=_text(software.get("version")),
                source="registry",
                kind="software",
            )
        )

    spec.tools = runtime_tools + _unique(software_tools)

    for var in payload.get("env") or []:
        key = _text(var.get("key"))
        scope = _text(var.get("scope"), "user").lower()
        if not key or scope not in ("user", "system", "session"):
            continue
        upper = key.upper()
        if upper in MACHINE_SPECIFIC_ENV:
            continue
        secret = bool(var.get("secret")) or looks_sensitive_key(key)
        value = var.get("value")
        # URL 中携带 user:password@ 的值按敏感处理，避免把凭据写进规格。
        if value is not None and _URL_USERINFO.search(str(value)):
            secret = True
        if upper == "PATH":
            spec.env.append(SpecEnvVar(key=key, scope=scope, value=None, secret=False, required=True))
            continue
        if secret:
            spec.env.append(SpecEnvVar(key=key, scope=scope, value=None, secret=True, required=True))
            continue
        if DEV_ENV_PATTERN.search(key) and value is not None:
            spec.env.append(SpecEnvVar(key=key, scope=scope, value=str(value), required=True))
        else:
            spec.env.append(SpecEnvVar(key=key, scope=scope, value=None, required=False))

    seen_paths: set[str] = set()
    for block in payload.get("path") or []:
        for raw in block.get("entries") or []:
            text = _text(raw)
            if not text or _SYSTEM_DIR_PATTERN.search(text):
                continue
            key = os.path.normcase(os.path.normpath(text))
            if key in seen_paths:
                continue
            seen_paths.add(key)
            spec.paths.append(SpecPath(path=text))
            if len(spec.paths) >= 30:
                break
        if len(spec.paths) >= 30:
            break

    commands: list[SpecCommand] = []
    for tool in runtime_tools:
        if not tool.version:
            continue
        commands.append(
            SpecCommand(
                run=f"{tool.id} --version",
                expect=parse_version(tool.version) or tool.version,
            )
        )
    spec.commands = commands
    return spec
