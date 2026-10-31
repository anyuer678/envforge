"""只读采集器：机器信息、环境变量、运行时、开发软件、终端与项目。

设计约束：
- 只读：不写注册表、不改环境变量、不启动安装程序；
- 可注入：注册表/命令探测/文件系统访问都能替换，便于在非 Windows 上测试；
- 失败不抛出：单个采集项失败时返回空结果并记录 warning，避免整个画像失败。
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from .db import Database
from .envvars import provider_for, split_path
from .errors import EnvAccessError
from .models import looks_sensitive_key

DEV_SOFTWARE_KEYWORDS: tuple[str, ...] = (
    "python", "node.js", "nodejs", "node ", "npm", "yarn", "pnpm", "nvm",
    "git", "github", "gitlab", "visual studio", "vscode", "visual studio code",
    "jetbrains", "intellij", "pycharm", "webstorm", "clion", "goland", "rider",
    "golang", "go language", "rust", "cargo", "llvm", "gcc", "mingw", "msys",
    "cygwin", "cmake", "ninja", "make", "docker", "kubernetes", "java", "jdk",
    "jre", "openjdk", "oracle", "maven", "gradle", "android", "flutter",
    "dotnet", ".net", "powershell", "windows terminal", "wsl", "ubuntu",
    "vmware", "virtualbox", "unity", "unreal", "godot", "qt", "boost", "openssl",
    "sqlite", "mysql", "postgres", "redis", "mongodb", "navicat", "postman",
    "apifox", "neovim", "vim", "emacs", "sublime", "notepad++", "scoop",
    "chocolatey", "winget", "terraform", "packer", "aws", "azure", "gcloud",
    "cuda", "anaconda", "miniconda", "ruby", "php", "composer", "perl",
    "julia", "rstudio", "matlab", "ffmpeg", "graphviz", "imagemagick",
)

_URL_CREDENTIALS = re.compile(r"://[^/@\s]+:[^/@\s]+@")


_UNINSTALL_PATHS: tuple[tuple[str, str], ...] = (
    ("machine", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("machine", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("user", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
)


def current_machine() -> dict[str, Any]:
    """当前机器的基础信息（只读）。"""
    system = platform.system().lower()
    os_name = "windows" if system == "windows" else system
    arch = (platform.machine() or "").lower()
    return {
        "os": os_name,
        "arch": arch,
        "release": platform.release(),
        "version": platform.version(),
        "hostname": platform.node(),
        "python": platform.python_version(),
    }


def collect_env(
    scopes: Iterable[str] = ("user", "system"),
    providers: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """读取指定作用域的环境变量；敏感值只保留键名与 secret 标记。"""
    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    for scope in scopes:
        try:
            provider = provider_for(scope, providers)
            values = provider.list_vars()
        except (EnvAccessError, OSError, ValueError) as exc:
            warnings.append(f"无法读取 {scope} 作用域环境变量：{exc}")
            continue
        for key, value in sorted(values.items(), key=lambda item: item[0].upper()):
            # 变量名不像密钥但值里带 user:password@ 的 URL 同样按敏感处理。
            secret = looks_sensitive_key(key) or bool(
                _URL_CREDENTIALS.search(str(value or ""))
            )
            entries.append(
                {
                    "scope": scope,
                    "key": key,
                    "value": None if secret else value,
                    "secret": secret,
                }
            )
    return entries, warnings


def collect_path(
    env_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for entry in env_entries:
        if str(entry.get("key", "")).upper() != "PATH":
            continue
        blocks.append(
            {
                "scope": entry["scope"],
                "entries": split_path(str(entry.get("value") or "")),
            }
        )
    return blocks


def collect_runtimes(db: Database | None) -> list[dict[str, Any]]:
    if db is None:
        return []
    try:
        rows = db.query("SELECT kind, version, exe_path, source FROM runtimes ORDER BY kind, version DESC")
    except Exception:  # pragma: no cover - 数据库不可用时不影响画像
        return []
    return [
        {
            "kind": row["kind"],
            "version": row["version"],
            "exe_path": row["exe_path"],
            "source": row["source"],
        }
        for row in rows
    ]


def _match_keyword(name: str, keywords: tuple[str, ...]) -> bool:
    lowered = name.lower()
    return any(keyword in lowered for keyword in keywords)


def filter_developer_software(
    entries: Iterable[Mapping[str, Any]],
    keywords: tuple[str, ...] = DEV_SOFTWARE_KEYWORDS,
) -> list[dict[str, Any]]:
    """从已安装软件里筛出开发相关条目并按名称去重。"""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        name = str(entry.get("name") or "").strip()
        if not name or not _match_keyword(name, keywords):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "name": name,
                "version": str(entry.get("version") or ""),
                "publisher": str(entry.get("publisher") or ""),
                "location": str(entry.get("location") or ""),
                "source": str(entry.get("source") or "registry"),
            }
        )
    return sorted(result, key=lambda item: item["name"].lower())


Reader = Callable[[], list[dict[str, Any]]]


def _read_registry_software() -> list[dict[str, Any]]:
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return []
    roots = {"machine": winreg.HKEY_LOCAL_MACHINE, "user": winreg.HKEY_CURRENT_USER}
    entries: list[dict[str, Any]] = []
    for hive, sub in _UNINSTALL_PATHS:
        try:
            root = winreg.OpenKey(roots[hive], sub, 0, winreg.KEY_READ)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    key = winreg.OpenKey(root, name, 0, winreg.KEY_READ)
                except OSError:
                    continue
                with key:
                    data: dict[str, str] = {}
                    for field_name in ("DisplayName", "DisplayVersion", "Publisher", "InstallLocation"):
                        try:
                            value, _kind = winreg.QueryValueEx(key, field_name)
                        except OSError:
                            continue
                        if isinstance(value, str):
                            data[field_name] = value
                if not data.get("DisplayName"):
                    continue
                entries.append(
                    {
                        "name": data.get("DisplayName", ""),
                        "version": data.get("DisplayVersion", ""),
                        "publisher": data.get("Publisher", ""),
                        "location": data.get("InstallLocation", ""),
                        "source": f"registry:{hive}",
                    }
                )
    return entries


def scan_software(
    *,
    reader: Reader | None = None,
    keywords: tuple[str, ...] = DEV_SOFTWARE_KEYWORDS,
    max_items: int = 100,
) -> list[dict[str, Any]]:
    """扫描已安装开发软件（读注册表 Uninstall 键，只读）。"""
    try:
        raw = (reader or _read_registry_software)()
    except Exception:  # pragma: no cover - 注册表异常时返回空清单
        return []
    items = filter_developer_software(raw, keywords)
    return items[:max_items] if max_items > 0 else items


def collect_terminals(
    *,
    which: Callable[[str], str | None] | None = None,
    home: Path | None = None,
    exists: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """终端环境画像：可用 shell 与配置文件位置（只读）。"""
    which_fn = which or shutil.which
    home_dir = Path(home or Path.home())
    exists_fn = exists or os.path.isfile
    documents = home_dir / "Documents"
    profiles = [
        ("WindowsPowerShell", documents / "WindowsPowerShell" / "profile.ps1"),
        ("WindowsPowerShell", documents / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1"),
        ("PowerShell", documents / "PowerShell" / "profile.ps1"),
        ("PowerShell", documents / "PowerShell" / "Microsoft.PowerShell_profile.ps1"),
    ]
    return {
        "powershell": {
            "pwsh": which_fn("pwsh") or "",
            "powershell": which_fn("powershell") or "",
            "profiles": [
                {"shell": name, "path": str(path), "exists": bool(exists_fn(str(path)))}
                for name, path in profiles
            ],
        },
        "git_bash": {"bash": which_fn("bash") or ""},
    }


def collect_projects(db: Database | None) -> list[dict[str, Any]]:
    """项目与档案的变量引用（敏感值不输出）。"""
    if db is None:
        return []
    try:
        projects = db.query("SELECT id, name, path FROM projects ORDER BY name COLLATE NOCASE")
        profiles = db.query(
            "SELECT id, project_id, name, active FROM profiles ORDER BY name COLLATE NOCASE"
        )
        vars_rows = db.query(
            "SELECT profile_id, key, sensitive FROM profile_vars ORDER BY key COLLATE NOCASE"
        )
    except Exception:  # pragma: no cover
        return []
    result: list[dict[str, Any]] = []
    for project in projects:
        entries = []
        for profile in [row for row in profiles if row["project_id"] == project["id"]]:
            keys = [
                {"key": row["key"], "sensitive": bool(row["sensitive"])}
                for row in vars_rows
                if row["profile_id"] == profile["id"]
            ]
            entries.append(
                {"name": profile["name"], "active": bool(profile["active"]), "keys": keys}
            )
        result.append({"name": project["name"], "path": project["path"], "profiles": entries})
    return result


def capture_payload(
    *,
    db: Database | None = None,
    scopes: Iterable[str] = ("user", "system"),
    providers: Mapping[str, Any] | None = None,
    include_runtimes: bool = True,
    include_software: bool = True,
    include_terminals: bool = True,
    include_projects: bool = False,
    soft_reader: Reader | None = None,
) -> dict[str, Any]:
    """一次性采集全部只读信息，供画像报告、规格生成与基线使用。"""
    warnings: list[str] = []
    env_entries, env_warnings = collect_env(scopes, providers)
    warnings.extend(env_warnings)
    payload: dict[str, Any] = {
        "machine": current_machine(),
        "env": env_entries,
        "path": collect_path(env_entries),
        "runtimes": collect_runtimes(db) if include_runtimes else [],
        "software": scan_software(reader=soft_reader) if include_software else [],
        "terminals": collect_terminals() if include_terminals else {},
        "projects": collect_projects(db) if include_projects else [],
        "warnings": warnings,
    }
    return payload


def normalize_software_id(name: str) -> str:
    """软件名归一化，用于规格工具 id 与版本清单匹配（保留中英文等 Unicode 字符）。"""
    return re.sub(r"[^\w.-]+", "-", name.lower()).strip("-")
