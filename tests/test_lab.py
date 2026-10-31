from __future__ import annotations

import os

import pytest

from envforge.lab import (
    DEFAULT_PATHEXT,
    combined_path,
    expand_vars,
    find_cycles,
    impact,
    resolve_command,
    simulate_path_change,
)
from envforge.profiles import ProfileService, ProjectService


def test_expand_vars_resolves_nested_references():
    env = {"JAVA_HOME": r"C:\Java", "BIN": r"%JAVA_HOME%\bin"}
    result = expand_vars("%BIN%;%MISSING%", env)
    assert result["value"] == r"C:\Java\bin;%MISSING%"
    assert result["missing"] == ["MISSING"]
    assert result["cycle"] is False


def test_expand_vars_detects_cycle():
    env = {"A": "%B%", "B": "%A%"}
    result = expand_vars("%A%", env)
    assert result["cycle"] is True


def test_expand_vars_depth_limit():
    env = {"A": "%A%x"}
    result = expand_vars("%A%", env, max_depth=2)
    assert result["cycle"] is True


def test_find_cycles_returns_groups():
    env = {"A": "%B%", "B": "%C%", "C": "%A%", "D": "plain"}
    cycles = find_cycles(env)
    assert len(cycles) == 1
    assert set(cycles[0]) == {"A", "B", "C"}


def test_find_cycles_reports_each_simple_cycle():
    # A 同时指向 B 与 C，存在 A→B→C→A 和 A→C→A 两个简单环，都应报告。
    cycles = find_cycles({"A": "%B%;%C%", "B": "%C%", "C": "%A%"})
    assert cycles == [["A", "B", "C"], ["A", "C"]]


def test_find_cycles_self_reference_and_none():
    assert find_cycles({"X": "%X%"}) == [["X"]]
    assert find_cycles({"X": "plain", "Y": "%X%"}) == []


def test_resolve_command_respects_directory_order(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "python.exe").write_text("", encoding="utf-8")
    (second / "python.exe").write_text("", encoding="utf-8")
    result = resolve_command("python", [str(first), str(second)])
    assert result["resolved"] == str(first / "python.exe")
    assert result["index"] == 0


def test_resolve_command_uses_pathext(tmp_path):
    folder = tmp_path / "bin"
    folder.mkdir()
    (folder / "tool.CMD").write_text("", encoding="utf-8")
    result = resolve_command("tool", [str(folder)], pathext=".EXE;.CMD")
    assert result["resolved"] == str(folder / "tool.CMD")


def test_resolve_command_missing(tmp_path):
    result = resolve_command("nope", [str(tmp_path)])
    assert result["resolved"] == ""
    assert result["index"] == -1


def test_simulate_add_changes_resolution(tmp_path):
    existing = tmp_path / "old"
    new = tmp_path / "new"
    existing.mkdir()
    new.mkdir()
    (new / "python.exe").write_text("", encoding="utf-8")
    result = simulate_path_change(
        base_path=str(existing),
        add=[str(new)],
        commands=["python"],
    )
    item = result["commands"][0]
    assert item["after"] == str(new / "python.exe")
    assert item["before"] == ""
    assert item["gains"] is True
    assert result["path"]["added"] == [str(new)]


def test_simulate_remove_loses_command(tmp_path):
    folder = tmp_path / "py"
    folder.mkdir()
    (folder / "python.exe").write_text("", encoding="utf-8")
    result = simulate_path_change(base_path=str(folder), remove=[str(folder)], commands=["python"])
    item = result["commands"][0]
    assert item["changed"] is True
    assert item["loses"] is True
    assert result["path"]["removed"] == [str(folder)]


def test_simulate_append_keeps_existing_priority(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "python.exe").write_text("", encoding="utf-8")
    (new / "python.exe").write_text("", encoding="utf-8")
    result = simulate_path_change(
        base_path=str(old), add=[str(new)], commands=["python"], position="append"
    )
    assert result["commands"][0]["before"] == str(old / "python.exe")
    assert result["commands"][0]["after"] == str(old / "python.exe")
    assert result["path"]["after"][-1] == str(new)


def test_simulate_reports_missing_and_duplicates(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    missing = str(tmp_path / "missing")
    result = simulate_path_change(
        base_path=f"{real};{real};{missing}", commands=["python"]
    )
    assert str(real) in result["path"]["duplicates"]
    assert missing in result["path"]["missing"]


def test_simulate_expands_vars_in_path_entries(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "python.exe").write_text("", encoding="utf-8")
    result = simulate_path_change(
        base_path="%TOOLS%", commands=["python"], env={"TOOLS": str(tools)}
    )
    assert result["commands"][0]["before"].startswith("%TOOLS%")
    assert result["commands"][0]["before"].endswith("python.exe")
    # PATH 中的 %TOOLS% 已展开为真实目录，不应被报告为缺失。
    assert result["path"]["missing"] == []


def test_simulate_path_checks_directory_existence_separately(tmp_path):
    folder = tmp_path / "bin"
    folder.mkdir()
    (folder / "python.exe").write_text("", encoding="utf-8")
    missing_dir = str(tmp_path / "missing-dir")
    # exists 只影响命令解析（文件存在性），目录缺失仍应如实报告。
    result = simulate_path_change(
        base_path=str(folder),
        add=[missing_dir],
        commands=["python"],
        exists=lambda _path: True,
        env={"PATH": str(folder)},
    )
    assert missing_dir in result["path"]["missing"]

    result = simulate_path_change(
        base_path=str(folder), commands=["python"], dir_exists=lambda _path: False
    )
    assert str(folder) in result["path"]["missing"]


def test_simulate_measured_matches_expanded_path(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "python.exe").write_text("", encoding="utf-8")
    result = simulate_path_change(
        base_path="%TOOLS%",
        commands=["python"],
        env={"TOOLS": str(tools)},
        measured=lambda command: str(tools / "python.exe"),
    )
    command = result["commands"][0]
    assert command["before_evidence"] == "measured"
    assert result["warnings"] == []
    assert command["before"].startswith("%TOOLS%")


def test_find_cycles_respects_step_budget():
    # 稠密 DAG：无环但简单路径数指数增长，必须在上限内返回。
    env = {
        f"V{i}": ";".join(f"%V{j}%" for j in range(i + 1, 20))
        for i in range(20)
    }
    assert find_cycles(env, max_steps=2_000) == []


def test_find_cycles_respects_cycle_limit():
    env = {"A": "%B%;%C%", "B": "%C%", "C": "%A%"}
    assert len(find_cycles(env, max_cycles=1)) == 1


def test_simulate_measured_evidence(tmp_path):
    folder = tmp_path / "bin"
    folder.mkdir()
    (folder / "python.exe").write_text("", encoding="utf-8")
    measured_path = str(folder / "python.exe")
    result = simulate_path_change(
        base_path=str(folder),
        commands=["python"],
        measured=lambda command: measured_path if command == "python" else None,
    )
    assert result["evidence"] == "measured"
    assert result["commands"][0]["before_evidence"] == "measured"
    assert result["commands"][0]["after_evidence"] == "inferred"


def test_measured_mismatch_warns(tmp_path):
    folder = tmp_path / "bin"
    folder.mkdir()
    (folder / "python.exe").write_text("", encoding="utf-8")
    result = simulate_path_change(
        base_path=str(tmp_path), commands=["python"], measured=lambda command: str(folder / "python.exe")
    )
    assert result["warnings"]


def test_combined_path_order():
    assert combined_path("sys", "user") == "sys;user"
    assert combined_path("sys", "user", provider="user-first") == "user;sys"
    assert combined_path("", "user") == "user"


def test_impact_lists_projects_and_referencing_vars(db, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    projects = ProjectService(db)
    projects.add("demo", str(project))
    profiles = ProfileService(db)
    profiles.set_var("demo", "dev", "API_URL", "http://localhost")
    profiles.set_var("demo", "dev", "API_BASE", "%API_URL%/v1")

    result = impact(db, "API_URL", scopes={"API_BASE": "%API_URL%/v1", "OTHER": "x"})
    assert result["projects"] == [{"project": "demo", "profiles": ["dev"]}]
    assert result["referenced_by"] == ["API_BASE"]
    assert result["summary"]["projects"] == 1


def test_impact_empty(db):
    result = impact(db, "NOPE")
    assert result["projects"] == []
    assert result["summary"]["referencing"] == 0


def test_impact_rejects_blank(db):
    with pytest.raises(ValueError):
        impact(db, "  ")


def test_pathext_constant():
    assert DEFAULT_PATHEXT.startswith(".COM")
    assert os.path.sep  # 确保在真实平台上运行
