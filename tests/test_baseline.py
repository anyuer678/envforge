from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from envforge.audit import AuditLog
from envforge.baseline import BaselineService
from envforge.errors import NotFoundError


def payload(java="jdk-17", token="a", paths=None, python="3.11", software=None):
    return {
        "machine": {"os": "windows", "hostname": "box"},
        "env": [
            {"scope": "user", "key": "JAVA_HOME", "value": java, "secret": False},
            {"scope": "user", "key": "DB_TOKEN", "value": token, "secret": True},
            {"scope": "user", "key": "PATH", "value": ";".join(paths or []), "secret": False},
        ],
        "path": [{"scope": "user", "entries": list(paths or [])}],
        "runtimes": [{"kind": "python", "version": python, "exe_path": r"C:\Python\python.exe"}],
        "software": software or [{"name": "Git", "version": "2.44"}],
    }


def test_capture_list_and_get(db):
    service = BaselineService(db)
    first = service.capture(label="before", payload_fn=lambda: payload())
    second = service.capture(label="after", payload_fn=lambda: payload(java="jdk-21"))
    assert first.id == 1 and first.fingerprint
    assert second.id == 2
    assert first.fingerprint == service.get(1).fingerprint
    items = service.list(limit=10)
    assert [item.id for item in items] == [2, 1]
    assert items[0].label == "after"
    # 基线默认覆盖 user,system；按任一作用域过滤都能查到。
    assert [item.id for item in service.list("user")] == [2, 1]
    assert service.list("session") == []


def test_get_missing_raises(db):
    with pytest.raises(NotFoundError):
        BaselineService(db).get(99)


def test_capture_is_idempotent_for_same_payload(db):
    service = BaselineService(db)
    a = service.capture(payload_fn=lambda: payload())
    b = service.capture(payload_fn=lambda: payload())
    assert a.fingerprint == b.fingerprint


def test_diff_reports_env_and_path_changes_without_secrets(db):
    service = BaselineService(db)
    before = service.capture(payload_fn=lambda: payload(paths=[r"C:\A"]))
    after = service.capture(
        payload_fn=lambda: payload(java="jdk-21", token="b", paths=[r"C:\A", r"C:\B"])
    )
    diff = service.diff(before.id, after.id)
    changes = {change["key"]: change for change in diff["changes"]}
    assert changes["user:JAVA_HOME"]["change"] == "changed"
    assert changes["user:JAVA_HOME"]["before"] == "jdk-17"
    assert changes["user:JAVA_HOME"]["after"] == "jdk-21"
    assert changes["user:DB_TOKEN"]["secret"] is True
    assert changes["user:DB_TOKEN"]["before"] == "***"
    assert changes["user:DB_TOKEN"]["after"] == "***"
    assert any(change["section"] == "path" and change["change"] == "added" for change in diff["changes"])
    assert diff["summary"]["by_source"]["unknown"] >= 1


def test_path_source_expands_snapshot_env(db, tmp_path):
    service = BaselineService(db)
    tools = tmp_path / "tools"
    tools.mkdir()
    before_ts = datetime.now(timezone.utc) - timedelta(minutes=5)
    source, evidence, _ = service._path_source(
        "%ENVFORGE_TEST_TOOLS%",
        before_ts,
        set(),
        "user",
        {"ENVFORGE_TEST_TOOLS": str(tools)},
    )
    assert source == "suspected"
    assert evidence == "inferred"
    # 变量不在进程环境中且未提供快照值时无法展开，只能如实标记 unknown。
    missing, _, _ = service._path_source("%ENVFORGE_TEST_TOOLS%", before_ts, set(), "user")
    assert missing == "unknown"


def test_diff_marks_envforge_writes_as_known(db, tmp_path):
    audit = AuditLog(tmp_path / "logs")
    service = BaselineService(db, audit=audit)
    before = service.capture(payload_fn=lambda: payload())
    audit.append("env.apply", scope="user", keys=["JAVA_HOME"], result="ok")
    after = service.capture(payload_fn=lambda: payload(java="jdk-21"))
    diff = service.diff(before.id, after.id)
    change = [item for item in diff["changes"] if item["key"] == "user:JAVA_HOME"][0]
    assert change["source"] == "envforge"
    assert change["evidence"] == "audit"


def test_diff_does_not_attribute_system_scope_to_envforge(db, tmp_path):
    audit = AuditLog(tmp_path / "logs")
    service = BaselineService(db, audit=audit)

    def system_payload(value):
        return {
            "machine": {"os": "windows"},
            "env": [{"scope": "system", "key": "JAVA_HOME", "value": value, "secret": False}],
            "path": [],
            "runtimes": [],
            "software": [],
        }

    before = service.capture(payload_fn=lambda: system_payload("a"))
    audit.append("env.apply", scope="user", keys=["JAVA_HOME"], result="ok")
    after = service.capture(payload_fn=lambda: system_payload("b"))
    diff = service.diff(before.id, after.id)
    change = [item for item in diff["changes"] if item["key"] == "system:JAVA_HOME"][0]
    assert change["source"] == "unknown"


def test_diff_notes_document_secret_rotation_limit(db):
    service = BaselineService(db)
    service.capture(payload_fn=lambda: payload())
    service.capture(payload_fn=lambda: payload(token="b"))
    diff = service.diff(1, 2)
    assert any("明文" in note for note in diff["notes"])


def test_diff_marks_recent_path_as_suspected(db, tmp_path):
    service = BaselineService(db)
    before = service.capture(payload_fn=lambda: payload(paths=[]))
    # 把基线时间挪到过去，保证目录 mtime 明确晚于基线
    db.execute(
        "UPDATE env_baselines SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (before.id,),
    )
    added_dir = tmp_path / "ToolBin"
    added_dir.mkdir()
    after = service.capture(payload_fn=lambda: payload(paths=[str(added_dir)]))
    diff = service.diff(before.id, after.id)
    change = [item for item in diff["changes"] if item["section"] == "path"][0]
    assert change["source"] == "suspected"
    assert change["evidence"] == "inferred"
    assert "推测" in change["detail"]


def test_diff_detects_runtime_version_change(db):
    service = BaselineService(db)
    before = service.capture(payload_fn=lambda: payload(python="3.11"))
    after = service.capture(payload_fn=lambda: payload(python="3.12"))
    diff = service.diff(before.id, after.id)
    change = [item for item in diff["changes"] if item["section"] == "runtimes"][0]
    assert change["change"] == "changed"
    assert change["before"] == "3.11"
    assert change["after"] == "3.12"


def test_diff_masks_added_and_removed_secrets(db):
    service = BaselineService(db)
    without = payload()
    without["env"] = [item for item in without["env"] if item["key"] != "DB_TOKEN"]
    before = service.capture(payload_fn=lambda: without)
    after = service.capture(payload_fn=lambda: payload(token=None))
    diff = service.diff(before.id, after.id)
    change = [item for item in diff["changes"] if item["key"] == "user:DB_TOKEN"][0]
    assert change["change"] == "added"
    assert change["before"] is None
    assert change["after"] == "***"


def test_diff_masks_removed_secret(db):
    service = BaselineService(db)
    before = service.capture(payload_fn=lambda: payload(token=None))
    without = payload()
    without["env"] = [item for item in without["env"] if item["key"] != "DB_TOKEN"]
    after = service.capture(payload_fn=lambda: without)
    diff = service.diff(before.id, after.id)
    change = [item for item in diff["changes"] if item["key"] == "user:DB_TOKEN"][0]
    assert change["change"] == "removed"
    assert change["before"] == "***"
    assert change["after"] is None


def test_diff_identical_baselines(db):
    service = BaselineService(db)
    before = service.capture(payload_fn=lambda: payload())
    after = service.capture(payload_fn=lambda: payload())
    diff = service.diff(before.id, after.id)
    assert diff["changes"] == []
    assert diff["summary"]["total"] == 0
