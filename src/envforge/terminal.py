"""终端配置管理：按 shell 保存可复用的启动片段。

只管理数据库里的片段，不会自动改写用户的 PowerShell / CMD / Git Bash 配置文件；
导出后由用户自行放入 profile.ps1 等位置。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .db import Database
from .errors import NotFoundError, ValidationError
from .models import now_iso

SHELLS = ("powershell", "cmd", "bash")
_SHELL_LABELS = {"powershell": "PowerShell", "cmd": "CMD", "bash": "Git Bash"}
_NAME_RE = re.compile(r"^[^\\/:*?\"<>|\s]+$")
_MAX_CONTENT = 20000


@dataclass(slots=True)
class TerminalConfig:
    name: str
    shell: str
    content: str
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "shell": self.shell,
            "shell_label": _SHELL_LABELS.get(self.shell, self.shell),
            "lines": len(self.content.splitlines()),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_content:
            data["content"] = self.content
        return data


def _validate_name(name: str) -> str:
    text = (name or "").strip()
    if not text:
        raise ValidationError("终端配置名称不能为空。")
    if not _NAME_RE.match(text) or len(text) > 64:
        raise ValidationError(
            f"终端配置名称 {text!r} 不合法；不能包含路径分隔符、空白或特殊字符，上限 64 字符。"
        )
    return text


def _validate_shell(shell: str) -> str:
    text = (shell or "").strip().lower()
    if text not in SHELLS:
        raise ValidationError(f"不支持的 shell {shell!r}；可选：{', '.join(SHELLS)}。")
    return text


class TerminalService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, name: str, shell: str, content: str) -> TerminalConfig:
        label = _validate_name(name)
        target_shell = _validate_shell(shell)
        text = content or ""
        if len(text) > _MAX_CONTENT:
            raise ValidationError(f"终端片段过长（上限 {_MAX_CONTENT} 字符）。")
        now = now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO terminal_configs(name, shell, content, created_at, updated_at)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(name, shell) DO UPDATE SET content = excluded.content,"
                " updated_at = excluded.updated_at",
                (label, target_shell, text, now, now),
            )
        return self.get(label, target_shell)

    def get(self, name: str, shell: str) -> TerminalConfig:
        label = _validate_name(name)
        target_shell = _validate_shell(shell)
        row = self.db.query_one(
            "SELECT * FROM terminal_configs WHERE name = ? COLLATE NOCASE AND shell = ?",
            (label, target_shell),
        )
        if not row:
            raise NotFoundError(
                f"没有名为 {label!r} 的 {target_shell} 终端配置；"
                "可用 `envforge terminal list` 查看已有配置。"
            )
        return self._row(row)

    def list(self, shell: str | None = None) -> list[TerminalConfig]:
        if shell is not None:
            target_shell = _validate_shell(shell)
            rows = self.db.query(
                "SELECT * FROM terminal_configs WHERE shell = ? ORDER BY name COLLATE NOCASE",
                (target_shell,),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM terminal_configs ORDER BY shell, name COLLATE NOCASE"
            )
        return [self._row(row) for row in rows]

    def remove(self, name: str, shell: str) -> None:
        label = _validate_name(name)
        target_shell = _validate_shell(shell)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM terminal_configs WHERE name = ? COLLATE NOCASE AND shell = ?",
                (label, target_shell),
            )
        if cursor.rowcount == 0:
            raise NotFoundError(f"没有名为 {label!r} 的 {target_shell} 终端配置。")

    def export(self, name: str, shell: str) -> str:
        config = self.get(name, shell)
        header = [
            f"# EnvForge 终端片段：{config.name}（{_SHELL_LABELS.get(config.shell, config.shell)}）",
            "# 请自行放入对应 shell 的启动文件；EnvForge 不会自动改写系统配置。",
        ]
        if config.shell == "powershell":
            header.append("# 位置：$PROFILE（PowerShell 7 或 Windows PowerShell 的 profile.ps1）")
        elif config.shell == "bash":
            header.append("# 位置：~/.bashrc 或 ~/.bash_profile")
        else:
            header.append("# CMD 不支持启动脚本；可保存为 .bat 并在需要时调用")
        return "\n".join(header + ["", config.content.rstrip(), ""])

    @staticmethod
    def _row(row: dict[str, Any]) -> TerminalConfig:
        return TerminalConfig(
            id=int(row["id"]),
            name=row["name"],
            shell=row["shell"],
            content=row["content"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
