"""环境基线（变更追踪）。

基线是某个时间点的只读环境画像（机器、环境变量、PATH、运行时、软件），
用于回答“安装某个程序之后，环境到底变了什么”。

来源区分原则：
- known：EnvForge 自己的写入审计日志能对上；
- suspected：目录创建时间等线索指向外部安装程序，但无法确认；
- unknown：无法追溯，必须如实标注。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from . import collectors
from .audit import AuditLog
from .db import Database
from .errors import NotFoundError
from .lab import expand_vars
from .models import now_iso, looks_sensitive_key
from .secrets import REDACTED

SOURCE_LABELS = {
    "envforge": "已知：EnvForge 写入",
    "suspected": "推测：外部程序或手工修改",
    "unknown": "未知：无法追溯来源",
}


@dataclass(slots=True)
class Baseline:
    id: int | None
    scope: str
    label: str
    payload: dict[str, Any]
    fingerprint: str
    created_at: str

    def to_dict(self, *, include_payload: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "scope": self.scope,
            "label": self.label,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
            "counts": {
                "env": len(self.payload.get("env") or []),
                "runtimes": len(self.payload.get("runtimes") or []),
                "software": len(self.payload.get("software") or []),
            },
        }
        if include_payload:
            data["payload"] = self.payload
        return data


def _fingerprint(payload: Mapping[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _mtime(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _payload_env(payload: Mapping[str, Any]) -> dict[str, str]:
    """提取基线快照中可安全展开的变量值（敏感值不保存明文，直接跳过）。"""
    env: dict[str, str] = {}
    for item in payload.get("env") or []:
        if item.get("secret"):
            continue
        key = str(item.get("key", "")).strip()
        value = item.get("value")
        if key and value is not None:
            env.setdefault(key, str(value))
    return env


def _parse_ts(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


class BaselineService:
    def __init__(self, db: Database, audit: AuditLog | None = None) -> None:
        self.db = db
        self.audit = audit

    # ------------------------------------------------------------------ 采集
    def capture(
        self,
        *,
        scope: str = "user,system",
        label: str = "",
        payload_fn: Callable[[], dict[str, Any]] | None = None,
    ) -> Baseline:
        if payload_fn is None:
            scopes = tuple(item.strip() for item in scope.split(",") if item.strip())
            payload = collectors.capture_payload(
                db=self.db,
                scopes=scopes,
                include_software=True,
                include_projects=False,
            )
        else:
            payload = payload_fn()
        # 去掉空格并保留逗号分隔形式，避免 "user, system" 与 "user,system" 存成两种值。
        stored_scope = ",".join(
            part.strip() for part in str(scope).split(",") if part.strip()
        )
        fingerprint = _fingerprint(payload)
        now = now_iso()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO env_baselines(scope, label, payload_json, fingerprint, created_at)"
                " VALUES(?,?,?,?,?)",
                (stored_scope, label, json.dumps(payload, ensure_ascii=False), fingerprint, now),
            )
            new_id = int(cursor.lastrowid)
        return Baseline(
            id=new_id,
            scope=stored_scope,
            label=label,
            payload=payload,
            fingerprint=fingerprint,
            created_at=now,
        )

    # ------------------------------------------------------------------ 读取
    def list(self, scope: str | None = None, limit: int = 50) -> list[Baseline]:
        if limit <= 0:
            return []
        if scope:
            # 基线可能记录多个作用域（如 "user,system"）；按逗号分段匹配而不是精确相等。
            rows = self.db.query(
                "SELECT * FROM env_baselines"
                " WHERE scope = ? OR instr(',' || scope || ',', ',' || ? || ',') > 0"
                " ORDER BY created_at DESC, id DESC LIMIT ?",
                (scope, scope, limit),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM env_baselines ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            )
        return [self._row_to_baseline(row) for row in rows]

    def get(self, baseline_id: int) -> Baseline:
        row = self.db.query_one("SELECT * FROM env_baselines WHERE id = ?", (baseline_id,))
        if not row:
            raise NotFoundError(
                f"基线 #{baseline_id} 不存在；用 `envforge baseline list` 查看已有基线。"
            )
        return self._row_to_baseline(row)

    @staticmethod
    def _row_to_baseline(row: Mapping[str, Any]) -> Baseline:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            payload = {}
        return Baseline(
            id=int(row["id"]),
            scope=row["scope"],
            label=row["label"],
            payload=payload if isinstance(payload, dict) else {},
            fingerprint=row["fingerprint"],
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------ 差异
    def diff(self, before_id: int, after_id: int) -> dict[str, Any]:
        before = self.get(before_id)
        after = self.get(after_id)
        changes: list[dict[str, Any]] = []

        audit_keys = self._audit_keys(before.created_at, after.created_at)
        before_ts = _parse_ts(before.created_at)

        env_a = {
            (item.get("scope", ""), str(item.get("key", "")).upper()): item
            for item in before.payload.get("env") or []
        }
        env_b = {
            (item.get("scope", ""), str(item.get("key", "")).upper()): item
            for item in after.payload.get("env") or []
        }
        for key in sorted(set(env_a) | set(env_b)):
            left = env_a.get(key)
            right = env_b.get(key)
            scope, name = key
            if left is None and right is not None:
                changes.append(
                    self._env_change(scope, name, right, "added", None, audit_keys)
                )
            elif right is None and left is not None:
                changes.append(
                    self._env_change(
                        scope, name, left, "removed", left.get("value"), audit_keys
                    )
                )
            elif left is not None and right is not None and left.get("value") != right.get("value"):
                changes.append(
                    self._env_change(scope, name, right, "changed", left.get("value"), audit_keys)
                )

        path_a = self._path_map(before.payload)
        path_b = self._path_map(after.payload)
        after_env = _payload_env(after.payload)
        for scope in sorted(set(path_a) | set(path_b)):
            old = path_a.get(scope, [])
            new = path_b.get(scope, [])
            old_set = {os.path.normcase(item) for item in old}
            new_set = {os.path.normcase(item) for item in new}
            for raw in new:
                if os.path.normcase(raw) not in old_set:
                    source, evidence, detail = self._path_source(
                        raw, before_ts, audit_keys, scope, after_env
                    )
                    changes.append(
                        {
                            "section": "path",
                            "key": f"{scope}:{raw}",
                            "change": "added",
                            "before": None,
                            "after": raw,
                            "secret": False,
                            "source": source,
                            "evidence": evidence,
                            "detail": detail,
                        }
                    )
            for raw in old:
                if os.path.normcase(raw) not in new_set:
                    changes.append(
                        {
                            "section": "path",
                            "key": f"{scope}:{raw}",
                            "change": "removed",
                            "before": raw,
                            "after": None,
                            "secret": False,
                            "source": "unknown",
                            "evidence": "unknown",
                            "detail": "PATH 项被移除，无法从现有记录追溯操作者。",
                        }
                    )

        changes.extend(self._object_changes(before, after, "runtimes", "kind", "exe_path", "runtime"))
        changes.extend(self._object_changes(before, after, "software", "name", "", "software"))

        summary = {"added": 0, "removed": 0, "changed": 0}
        for change in changes:
            summary[change["change"]] = summary.get(change["change"], 0) + 1
        by_source = {"envforge": 0, "suspected": 0, "unknown": 0}
        for change in changes:
            by_source[change["source"]] = by_source.get(change["source"], 0) + 1
        summary["total"] = len(changes)
        summary["by_source"] = by_source
        return {
            "from": before.to_dict(),
            "to": after.to_dict(),
            "changes": changes,
            "summary": summary,
            "notes": [
                "known 仅表示 EnvForge 审计日志里有对应写入记录；",
                "suspected 来自目录创建时间等线索，不能证明具体安装程序；",
                "unknown 表示现有数据无法追溯，建议对比安装前后基线缩小范围。",
                "敏感变量在基线中不保存明文（值记为 ***），因此无法判断其值是否轮换。",
            ],
        }

    # ------------------------------------------------------------------ 内部
    def _env_change(
        self,
        scope: str,
        key: str,
        item: Mapping[str, Any],
        change: str,
        before_value: str | None,
        audit_keys: set[str],
    ) -> dict[str, Any]:
        secret = bool(item.get("secret")) or looks_sensitive_key(key)
        after_value = None if change == "removed" else item.get("value")
        if secret:
            # 敏感值不保存明文（真实采集时 value 为 None），按变更类型显示掩码而非 None。
            if change == "added":
                before_value, after_value = None, REDACTED
            elif change == "removed":
                before_value, after_value = REDACTED, None
            else:
                before_value, after_value = REDACTED, REDACTED
        if scope == "user" and key.upper() in audit_keys:
            # EnvForge 只写用户作用域；系统作用域的同名变量不能被归因给本工具。
            source, evidence, detail = "envforge", "audit", "审计日志中有该变量的写入记录。"
        else:
            source, evidence, detail = "unknown", "unknown", "审计日志中没有对应的写入记录。"
        return {
            "section": "env",
            "key": f"{scope}:{key}",
            "change": change,
            "before": before_value,
            "after": after_value,
            "secret": secret,
            "source": source,
            "evidence": evidence,
            "detail": detail,
        }

    def _path_source(
        self,
        raw: str,
        before_ts: datetime | None,
        audit_keys: set[str],
        scope: str,
        env_map: Mapping[str, str] | None = None,
    ) -> tuple[str, str, str]:
        if scope == "user" and "PATH" in audit_keys:
            return "envforge", "audit", "审计日志中有 PATH 写入记录。"
        # 目录项可能含 %VAR%：优先用基线快照中的变量展开，进程环境作为兜底。
        lookup = dict(os.environ)
        lookup.update({str(key): str(value) for key, value in (env_map or {}).items()})
        mtime = _mtime(expand_vars(raw, lookup)["value"])
        if mtime is not None and before_ts is not None:
            try:
                created = datetime.fromtimestamp(mtime, tz=before_ts.tzinfo)
            except (OSError, OverflowError, ValueError):
                created = None
            if created is not None and created >= before_ts:
                return (
                    "suspected",
                    "inferred",
                    f"目录创建/修改时间（{created:%Y-%m-%d %H:%M}）晚于基线时间，推测由安装程序添加。",
                )
        return "unknown", "unknown", "目录已存在或时间信息不足，无法推断来源。"

    @staticmethod
    def _path_map(payload: Mapping[str, Any]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for block in payload.get("path") or []:
            scope = str(block.get("scope") or "")
            entries = [str(item) for item in block.get("entries") or []]
            result[scope] = result.get(scope, []) + entries
        return result

    def _object_changes(
        self,
        before: Baseline,
        after: Baseline,
        section: str,
        primary: str,
        secondary: str,
        label: str,
    ) -> list[dict[str, Any]]:
        def key_of(item: Mapping[str, Any]) -> str:
            first = str(item.get(primary) or "")
            second = str(item.get(secondary) or "")
            return f"{first}|{second}".lower()

        old = {key_of(item): item for item in before.payload.get(section) or []}
        new = {key_of(item): item for item in after.payload.get(section) or []}
        changes: list[dict[str, Any]] = []
        for key in sorted(set(old) | set(new)):
            left = old.get(key)
            right = new.get(key)
            if left is None and right is not None:
                changes.append(
                    self._object_change(section, label, key, right, "added", None)
                )
            elif right is None and left is not None:
                changes.append(
                    self._object_change(section, label, key, left, "removed", None)
                )
            elif left is not None and right is not None:
                version_a = str(left.get("version") or "")
                version_b = str(right.get("version") or "")
                if version_a != version_b:
                    changes.append(
                        self._object_change(
                            section, label, key, right, "changed", version_a
                        )
                    )
        return changes

    @staticmethod
    def _object_change(
        section: str,
        label: str,
        key: str,
        item: Mapping[str, Any],
        change: str,
        before_value: str | None,
    ) -> dict[str, Any]:
        name = str(item.get("name") or item.get("kind") or key)
        version = str(item.get("version") or "")
        return {
            "section": section,
            "key": f"{name} {version}".strip(),
            "change": change,
            "before": before_value,
            "after": version or None,
            "secret": False,
            "source": "unknown",
            "evidence": "declared",
            "detail": f"{label}清单发生变化；可结合安装记录进一步确认。",
        }

    def _audit_keys(self, start: str, end: str) -> set[str]:
        if self.audit is None:
            return set()
        start_ts = _parse_ts(start)
        end_ts = _parse_ts(end)
        keys: set[str] = set()
        for entry in self.audit.recent(limit=500):
            if entry.get("action") not in ("env.apply", "env.restore", "env.dedupe"):
                continue
            ts = _parse_ts(str(entry.get("ts") or ""))
            if start_ts and end_ts and ts is not None:
                if not (start_ts <= ts <= end_ts):
                    continue
            for key in entry.get("keys") or []:
                keys.add(str(key).upper())
        return keys
