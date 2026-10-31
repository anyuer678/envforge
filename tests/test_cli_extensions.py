from __future__ import annotations

import json
from pathlib import Path

import pytest

from envforge.cli import EXIT_CONFIRM, EXIT_ERROR, EXIT_USAGE, EXIT_OK
from envforge.cli import main


@pytest.fixture
def cli(tmp_path):
    db_path = tmp_path / "cli-ext.db"

    def run(*args: str) -> int:
        return main(["--db", str(db_path), *args])

    return run


# ---------------------------------------------------------------------- terminal
def test_terminal_cli_flow(cli, tmp_path, capsys):
    assert cli("terminal", "set", "dev", "--content", "$env:FOO = 'bar'") == EXIT_OK
    capsys.readouterr()

    assert cli("terminal", "list", "--json") == EXIT_OK
    items = json.loads(capsys.readouterr().out)
    assert items[0]["name"] == "dev"
    assert items[0]["shell"] == "powershell"

    assert cli("terminal", "show", "dev") == EXIT_OK
    assert "$env:FOO = 'bar'" in capsys.readouterr().out

    target = tmp_path / "profile.ps1"
    assert cli("terminal", "export", "dev", "--output", str(target)) == EXIT_OK
    text = target.read_text(encoding="utf-8")
    assert "EnvForge 终端片段" in text and "$env:FOO = 'bar'" in text

    assert cli("terminal", "remove", "dev") == EXIT_CONFIRM
    assert cli("terminal", "remove", "dev", "--yes") == EXIT_OK
    assert cli("terminal", "show", "dev") == 1


def test_terminal_export_requires_confirmation_to_overwrite(cli, tmp_path):
    assert cli("terminal", "set", "dev", "--content", "$env:FOO='bar'") == EXIT_OK
    target = tmp_path / "profile.ps1"
    target.write_text("原有内容", encoding="utf-8")
    assert cli("terminal", "export", "dev", "--output", str(target)) == EXIT_CONFIRM
    assert target.read_text(encoding="utf-8") == "原有内容"
    assert cli("terminal", "export", "dev", "--output", str(target), "--yes") == EXIT_OK
    assert "EnvForge 终端片段" in target.read_text(encoding="utf-8")


def test_terminal_export_rejects_directory_target(cli, tmp_path):
    assert cli("terminal", "set", "dev", "--content", "$env:FOO='bar'") == EXIT_OK
    target = tmp_path / "out"
    target.mkdir()
    assert cli("terminal", "export", "dev", "--output", str(target)) == EXIT_USAGE


def test_env_export_requires_confirmation_to_overwrite(cli, tmp_path, capsys):
    assert cli("env", "snapshot", "--scope", "user", "--json") == EXIT_OK
    snapshot_id = json.loads(capsys.readouterr().out)["id"]
    target = tmp_path / "snapshot.json"
    target.write_text("旧内容", encoding="utf-8")
    assert cli("env", "export", str(snapshot_id), "--output", str(target)) == EXIT_CONFIRM
    assert target.read_text(encoding="utf-8") == "旧内容"
    assert (
        cli("env", "export", str(snapshot_id), "--output", str(target), "--yes")
        == EXIT_OK
    )
    assert "values" in json.loads(target.read_text(encoding="utf-8"))


def test_terminal_set_from_file(cli, tmp_path, capsys):
    source = tmp_path / "snippet.ps1"
    source.write_text("Write-Host 你好\n", encoding="utf-8")
    assert cli("terminal", "set", "greet", "--file", str(source)) == EXIT_OK
    assert "已保存" in capsys.readouterr().out

    assert cli("terminal", "set", "bad", "--file", str(tmp_path / "none.ps1")) == EXIT_USAGE


# ---------------------------------------------------------------------- spec
def _write_spec(path: Path, **kwargs):
    from envforge.spec import EnvironmentSpec, save_spec

    spec = EnvironmentSpec(name=kwargs.pop("name", "demo"), **kwargs)
    return save_spec(spec, path)


def test_spec_show_check_and_diff(cli, tmp_path, capsys):
    from envforge.spec import SpecCommand, SpecPath, SpecPort

    left = _write_spec(tmp_path / "left.toml", paths=[SpecPath(path=str(tmp_path))])
    right = _write_spec(
        tmp_path / "right.toml",
        name="demo2",
        paths=[SpecPath(path=str(tmp_path)), SpecPath(path=str(tmp_path / "extra"))],
        commands=[SpecCommand(run="echo hi", expect="hi")],
        ports=[SpecPort(port=59999)],
    )

    assert cli("spec", "show", str(left), "--json") == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["spec"]["name"] == "demo"
    assert "[[paths]]" in (tmp_path / "left.toml").read_text(encoding="utf-8")

    assert cli("spec", "check", str(left), "--json") == EXIT_OK
    result = json.loads(capsys.readouterr().out)
    assert result["summary"]["total"] == len(result["items"])
    assert result["items"][0]["section_label"]
    assert isinstance(result.get("warnings"), list)

    assert cli("spec", "diff", str(left), str(right), "--json") == EXIT_OK
    diff = json.loads(capsys.readouterr().out)
    assert diff["sections"]["paths"]["added"][0]["path"] == str(tmp_path / "extra")


def test_spec_check_writes_reports(cli, tmp_path, capsys):
    from envforge.spec import SpecPath

    spec = _write_spec(tmp_path / "spec.toml", paths=[SpecPath(path=str(tmp_path))])
    report_dir = tmp_path / "reports"
    assert cli("spec", "check", str(spec), "--report", str(report_dir)) == EXIT_OK
    assert list(report_dir.glob("spec-check-*.md"))
    assert list(report_dir.glob("spec-check-*.html"))


def test_spec_check_fail_on_levels(cli, tmp_path, capsys):
    from envforge.spec import SpecPath

    spec = _write_spec(
        tmp_path / "fail.toml", paths=[SpecPath(path=str(tmp_path / "missing-dir"))]
    )
    capsys.readouterr()
    assert cli("spec", "check", str(spec), "--fail-on", "missing") == EXIT_ERROR
    assert cli("spec", "check", str(spec), "--fail-on", "mismatch") == EXIT_OK
    assert cli("spec", "check", str(spec), "--fail-on", "bogus") == EXIT_USAGE


def test_spec_check_fail_on_is_validated_before_output(cli, tmp_path, capsys):
    from envforge.spec import SpecPath

    spec = _write_spec(tmp_path / "early.toml", paths=[SpecPath(path=str(tmp_path))])
    report_dir = tmp_path / "early-reports"
    assert (
        cli("spec", "check", str(spec), "--fail-on", "bogus", "--report", str(report_dir))
        == EXIT_USAGE
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert not report_dir.exists()


def test_spec_check_uses_current_arch(cli, tmp_path, capsys):
    from envforge.spec import MachineSpec

    spec = _write_spec(
        tmp_path / "arch.toml", machine=MachineSpec(os="windows", arch="definitely-other")
    )
    assert cli("spec", "check", str(spec), "--json") == EXIT_OK
    result = json.loads(capsys.readouterr().out)
    arch = [item for item in result["items"] if item["target"] == "arch"]
    assert arch and arch[0]["status"] == "mismatch"


def test_spec_check_rejects_existing_non_report_file(cli, tmp_path, capsys):
    from envforge.spec import SpecPath

    spec = _write_spec(tmp_path / "spec2.toml", paths=[SpecPath(path=str(tmp_path))])
    target = tmp_path / "not-a-report"
    target.write_text("x", encoding="utf-8")
    assert cli("spec", "check", str(spec), "--report", str(target)) == EXIT_USAGE


def test_spec_plan_lists_actions(cli, tmp_path, capsys):
    from envforge.spec import SpecEnvVar, SpecPath, SpecTool

    spec = _write_spec(
        tmp_path / "plan.toml",
        tools=[SpecTool(id="definitely-not-installed-tool", version=">=1", source="winget:Vendor.Tool")],
        env=[SpecEnvVar(key="ENVFORGE_SPEC_DEMO", value="demo")],
        paths=[SpecPath(path=str(tmp_path / "new-folder"))],
    )
    assert cli("spec", "plan", str(spec), "--json") == EXIT_OK
    plan = json.loads(capsys.readouterr().out)
    targets = {step["target"] for step in plan["steps"]}
    assert "definitely-not-installed-tool" in targets
    assert "user:ENVFORGE_SPEC_DEMO" in targets
    assert str(tmp_path / "new-folder") in targets


def test_spec_capture_writes_file(cli, tmp_path, capsys):
    target = tmp_path / "captured.toml"
    assert cli("spec", "capture", "--output", str(target), "--no-software", "--json") == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert target.exists()
    assert data["spec"]["version"] == "1"
    assert isinstance(data["env"], list)


# ---------------------------------------------------------------------- baseline
def test_baseline_capture_list_and_diff(cli, capsys):
    assert cli("baseline", "capture", "--label", "first") == EXIT_OK
    capsys.readouterr()
    assert cli("baseline", "capture", "--label", "second") == EXIT_OK
    capsys.readouterr()

    assert cli("baseline", "list", "--json") == EXIT_OK
    items = json.loads(capsys.readouterr().out)
    assert len(items) == 2
    assert {item["label"] for item in items} == {"first", "second"}

    assert cli("baseline", "diff", "1", "2", "--json") == EXIT_OK
    diff = json.loads(capsys.readouterr().out)
    assert diff["summary"]["total"] == 0
    assert set(diff["summary"]["by_source"]) == {"envforge", "suspected", "unknown"}


# ---------------------------------------------------------------------- lab / graph
def test_lab_commands(cli, tmp_path, capsys):
    assert cli("lab", "expand", "%PATH%", "--json") == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"value", "missing", "cycle"}

    assert cli("lab", "cycles", "--json") == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data["cycles"], list)

    assert cli("lab", "impact", "--key", "PATH", "--json") == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["key"] == "PATH"

    assert cli("lab", "path", "--add", str(tmp_path), "--command", "python", "--json") == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["commands"][0]["command"] == "python"
    assert str(tmp_path) in data["path"]["added"]


def test_merged_env_prefers_user_and_combines_path():
    from envforge.cli import _merged_env

    values = {
        ("system", "JAVA_HOME"): r"C:\sys",
        ("user", "JAVA_HOME"): r"C:\user",
        ("system", "PATH"): r"C:\Windows",
        ("user", "PATH"): r"C:\Tools",
    }
    merged = _merged_env(values)
    assert merged["JAVA_HOME"] == r"C:\user"
    assert merged["PATH"] == r"C:\Windows;C:\Tools"


def test_help_for_new_commands_does_not_crash(capsys):
    for command in ("lab", "spec", "baseline", "graph", "terminal", "machine"):
        with pytest.raises(SystemExit) as exc:
            main([command, "--help"])
        assert exc.value.code == 0


def test_lab_rejects_unknown_scope(cli, capsys):
    assert cli("lab", "cycles", "--scope", "nope") == 2
    assert "输入错误" in capsys.readouterr().err


def test_graph_build_formats(cli, tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    assert cli("project", "add", "--name", "demo", "--path", str(project)) == EXIT_OK
    assert cli("profile", "set", "demo", "dev", "API_URL=http://localhost") == EXIT_OK
    capsys.readouterr()

    dot = tmp_path / "graph.dot"
    assert cli("graph", "build", "--format", "dot", "--output", str(dot)) == EXIT_OK
    assert dot.read_text(encoding="utf-8").startswith("digraph envforge {")
    capsys.readouterr()

    md = tmp_path / "graph.md"
    assert cli("graph", "build", "--format", "md", "--output", str(md)) == EXIT_OK
    assert "# 环境依赖关系图" in md.read_text(encoding="utf-8")
    capsys.readouterr()

    assert cli("graph", "build", "--format", "json") == EXIT_OK
    graph = json.loads(capsys.readouterr().out)
    assert graph["summary"]["projects"] == 1
    assert any(edge["kind"] == "declared" for edge in graph["edges"])

    json_file = tmp_path / "graph.json"
    assert cli("graph", "build", "--format", "json", "--json", "--output", str(json_file)) == EXIT_OK
    captured = capsys.readouterr()
    assert captured.out == ""  # --json 时写入提示走 stderr，不污染 stdout
    assert json.loads(json_file.read_text(encoding="utf-8"))["summary"]["projects"] == 1


# ---------------------------------------------------------------------- machine
def test_machine_software_json(cli, capsys):
    assert cli("machine", "software", "--json") == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert {"total", "items"} == set(data)
    assert data["total"] == len(data["items"])


def test_machine_profile_json(cli, capsys):
    assert cli("machine", "profile", "--no-software", "--json") == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert set(data) >= {"machine", "env", "path", "runtimes", "software", "warnings"}
    assert data["machine"]["os"]


def test_machine_profile_format_json_without_output(cli, capsys):
    assert cli("machine", "profile", "--no-software", "--format", "json") == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["machine"]["os"]


def test_machine_profile_json_rejects_existing_file_output(cli, tmp_path, capsys):
    target = tmp_path / "profile.json"
    target.write_text("x", encoding="utf-8")
    assert (
        cli("machine", "profile", "--no-software", "--format", "json", "--output", str(target))
        == EXIT_USAGE
    )


# ---------------------------------------------------------------------- 回归
def test_spec_plan_json_with_output_keeps_stdout_pure(cli, tmp_path, capsys):
    from envforge.spec import SpecPath

    spec = _write_spec(
        tmp_path / "plan.toml", paths=[SpecPath(path=str(tmp_path / "missing"))]
    )
    target = tmp_path / "plan.ps1"
    assert cli("spec", "plan", str(spec), "--output", str(target), "--json") == EXIT_OK
    captured = capsys.readouterr()
    assert json.loads(captured.out)["script"]
    assert "已写入计划脚本" in captured.err


def test_spec_check_json_with_report_keeps_stdout_pure(cli, tmp_path, capsys):
    from envforge.spec import SpecPath

    spec = _write_spec(tmp_path / "check.toml", paths=[SpecPath(path=str(tmp_path))])
    report_dir = tmp_path / "reports"
    assert cli("spec", "check", str(spec), "--report", str(report_dir), "--json") == EXIT_OK
    captured = capsys.readouterr()
    assert json.loads(captured.out)["summary"]
    assert "已写入" in captured.err


def test_machine_profile_json_with_output_keeps_stdout_pure(cli, tmp_path, capsys):
    target = tmp_path / "machine"
    target.mkdir()
    assert cli("machine", "profile", "--no-software", "--json", "--output", str(target)) == EXIT_OK
    captured = capsys.readouterr()
    assert json.loads(captured.out)["machine"]["os"]
    assert "已写入" in captured.err


def test_env_export_json_with_output_keeps_stdout_pure(cli, tmp_path, capsys):
    assert cli("env", "snapshot", "--scope", "session", "--json") == EXIT_OK
    snapshot_id = json.loads(capsys.readouterr().out)["id"]
    target = tmp_path / "snap.json"
    assert cli("env", "export", str(snapshot_id), "--output", str(target), "--json") == EXIT_OK
    captured = capsys.readouterr()
    assert json.loads(captured.out)["values"]
    assert "已导出到" in captured.err


def test_profile_export_json_returns_json(cli, tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    assert cli("project", "add", "--name", "demo", "--path", str(project)) == EXIT_OK
    capsys.readouterr()
    assert cli("profile", "set", "demo", "dev", "A=1") == EXIT_OK
    capsys.readouterr()
    assert cli("profile", "export", "demo", "dev", "--json") == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["project"] == "demo" and "A=1" in data["content"]


def test_terminal_export_json_with_output_keeps_stdout_pure(cli, tmp_path, capsys):
    assert cli("terminal", "set", "dev", "--content", "Write-Host hi") == EXIT_OK
    capsys.readouterr()
    target = tmp_path / "dev.ps1"
    assert cli("terminal", "export", "dev", "--output", str(target), "--json") == EXIT_OK
    captured = capsys.readouterr()
    assert json.loads(captured.out)["script"]
    assert "已导出到" in captured.err


def test_terminal_set_reads_utf16_file(cli, tmp_path, capsys):
    source = tmp_path / "utf16.ps1"
    source.write_bytes("Write-Host 你好\n".encode("utf-16"))
    assert cli("terminal", "set", "utf16", "--file", str(source)) == EXIT_OK
    assert "已保存" in capsys.readouterr().out


def test_env_commands_reject_multiple_scopes(cli, capsys):
    assert cli("env", "show", "--scope", "user,system") == EXIT_USAGE
    assert "只支持单个作用域" in capsys.readouterr().err


def test_gui_subcommand_passes_db_path(monkeypatch, cli):
    import envforge.cli as cli_module

    seen = {}

    def fake(db_path=None):
        seen["db"] = db_path
        return 0

    monkeypatch.setattr(cli_module, "_launch_gui", fake)
    assert cli("gui") == 0
    assert seen["db"] and Path(seen["db"]).name == "cli-ext.db"
