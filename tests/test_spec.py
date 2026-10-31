from __future__ import annotations

import tomllib

import pytest

from envforge.errors import ValidationError
from envforge.spec import (
    CheckContext,
    EnvironmentSpec,
    MachineSpec,
    SpecCommand,
    SpecEnvVar,
    SpecPath,
    SpecPort,
    SpecTool,
    build_plan,
    capture_from_payload,
    check_spec,
    diff_specs,
    dump_spec,
    health_score,
    install_command,
    load_spec,
    parse_spec,
    save_spec,
    summarize_items,
    version_key,
    version_satisfies,
)


def _spec() -> EnvironmentSpec:
    return EnvironmentSpec(
        name="demo",
        kind="workstation",
        machine=MachineSpec(os="windows", arch="x64"),
        tools=[SpecTool(id="python", version=">=3.11,<3.13", source="winget:Python.Python.3.12")],
        env=[
            SpecEnvVar(key="JAVA_HOME", value=r"C:\Java\jdk-21"),
            SpecEnvVar(key="DB_TOKEN", secret=True),
            SpecEnvVar(key="OPTIONAL", required=False),
        ],
        paths=[SpecPath(path=r"C:\Tools")],
        commands=[SpecCommand(run="python --version", expect="Python 3")],
        ports=[SpecPort(port=8080)],
    )


# ---------------------------------------------------------------------- 版本
@pytest.mark.parametrize(
    "actual,constraint,expected",
    [
        ("3.12.3", ">=3.11", True),
        ("3.10.1", ">=3.11", False),
        ("3.12.3", ">=3.11,<3.13", True),
        ("3.13.0", "<3.13", False),
        ("3.12.3", "3.12", True),
        ("3.120.1", "3.12", False),
        ("3.12.3", "3.12.x", True),
        ("3.11.2", "!=3.12.x", True),
        ("3.12.3", "!=3.12.x", False),
        ("3.12.3", "==3.12.x", True),
        ("3.12.3", ">=3.12.x", False),
        ("3.12.3", "==3.12.3", True),
        ("3.12.4", "!=3.12.3", True),
        ("", ">=3", False),
        ("3.12.3", "", True),
        ("v20.11.1", ">=20", True),
        ("21.0.2", "<=21", True),
        ("python 3.9.0", ">3.9", False),
    ],
)
def test_version_satisfies(actual, constraint, expected):
    assert version_satisfies(actual, constraint) is expected


def test_version_key_extracts_numeric_parts():
    assert version_key("Python 3.12.3") == (3, 12, 3)
    assert version_key("v20") == (20,)
    assert version_key("no digits") == ()
    assert version_key(None) == ()


# ---------------------------------------------------------------------- 读写
def test_dump_and_parse_roundtrip():
    spec = _spec()
    text = dump_spec(spec)
    data = tomllib.loads(text)
    parsed = parse_spec(data, source="inline")
    assert parsed.name == "demo"
    assert parsed.source == "inline"
    assert parsed.machine.os == "windows"
    assert parsed.tools[0].version == ">=3.11,<3.13"
    assert parsed.env[0].value == r"C:\Java\jdk-21"
    # 敏感值不落盘
    secret = [item for item in parsed.env if item.key == "DB_TOKEN"][0]
    assert secret.secret is True and secret.value is None
    assert "secret = true" in text
    assert parsed.commands[0].expect == "Python 3"
    assert parsed.ports[0].port == 8080


def test_dump_skips_secret_value_even_if_set():
    spec = EnvironmentSpec(env=[SpecEnvVar(key="API_KEY", value="plain", secret=True)])
    text = dump_spec(spec)
    assert "plain" not in text
    assert "secret = true" in text


def test_parse_section_validation():
    with pytest.raises(ValidationError):
        parse_spec({})
    with pytest.raises(ValidationError):
        parse_spec({"spec": {"version": "2"}})
    with pytest.raises(ValidationError):
        parse_spec({"spec": {"version": "1"}, "tools": [{"version": "1"}]})
    with pytest.raises(ValidationError):
        parse_spec({"spec": {"version": "1"}, "env": [{"key": "A", "scope": "nope"}]})
    with pytest.raises(ValidationError):
        parse_spec({"spec": {"version": "1"}, "ports": [{"port": 70000}]})
    with pytest.raises(ValidationError):
        parse_spec({"spec": {"version": "1"}, "paths": [{}]})


def test_load_spec_missing_and_invalid(tmp_path):
    with pytest.raises(ValidationError):
        load_spec(tmp_path / "none.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("[spec\nversion='1'", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_spec(bad)
    with pytest.raises(ValidationError):
        load_spec(tmp_path)


def test_save_spec_writes_file(tmp_path):
    target = save_spec(_spec(), tmp_path / "nested" / "spec.toml")
    assert target.exists()
    assert "[[tools]]" in target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------- 采集
def _payload() -> dict:
    return {
        "machine": {"os": "Windows", "arch": "AMD64", "hostname": "devbox"},
        "runtimes": [
            {"kind": "python", "version": "3.12.3", "exe_path": r"C:\Python312\python.exe"},
            {"kind": "node", "version": "22.1.0", "exe_path": r"C:\Program Files\nodejs\node.exe"},
        ],
        "software": [
            {"name": "Git", "version": "2.44.0", "publisher": "The Git Project"},
            {"name": "Visual Studio Code", "version": "1.90", "publisher": "Microsoft"},
        ],
        "env": [
            {"scope": "user", "key": "PATH", "value": r"C:\Python312;C:\Windows"},
            {"scope": "user", "key": "JAVA_HOME", "value": r"C:\Java\jdk-21"},
            {"scope": "user", "key": "DB_PASSWORD", "value": "hidden", "secret": True},
            {"scope": "system", "key": "COMPUTERNAME", "value": "devbox"},
            {"scope": "user", "key": "SOME_LOCAL", "value": "x"},
        ],
        "path": [
            {"scope": "user", "entries": [r"C:\Python312", r"C:\Windows", r"C:\Python312"]},
        ],
        "warnings": [],
    }


def test_capture_from_payload_builds_spec():
    spec = capture_from_payload(_payload())
    assert spec.name == "devbox"
    assert spec.machine.os == "windows"
    assert spec.machine.arch == "amd64"
    tools = {tool.id: tool for tool in spec.tools}
    assert tools["python"].version == "3.12.3"
    assert tools["python"].kind == "runtime"
    assert "git" in tools and tools["git"].kind == "software"

    env = {var.key: var for var in spec.env}
    assert env["PATH"].value is None
    assert env["JAVA_HOME"].value == r"C:\Java\jdk-21"
    assert env["DB_PASSWORD"].secret is True and env["DB_PASSWORD"].value is None
    assert "COMPUTERNAME" not in env
    assert env["SOME_LOCAL"].value is None and env["SOME_LOCAL"].required is False

    paths = [item.path for item in spec.paths]
    assert r"C:\Python312" in paths
    assert r"C:\Windows" not in paths
    assert len(paths) == len(set(paths))

    commands = [item.run for item in spec.commands]
    assert "python --version" in commands


def test_capture_marks_url_credentials_as_secret():
    payload = _payload()
    payload["env"].append(
        {
            "scope": "user",
            "key": "PIP_INDEX_URL",
            "value": "https://user:s3cret@pypi.example.com/simple",
        }
    )
    spec = capture_from_payload(payload)
    var = next(item for item in spec.env if item.key == "PIP_INDEX_URL")
    assert var.secret is True
    assert var.value is None
    assert "s3cret" not in dump_spec(spec)


def test_capture_keeps_unicode_software_ids():
    from envforge.collectors import normalize_software_id

    payload = _payload()
    payload["software"] = [{"name": "微信开发者工具", "version": "1.06"}]
    spec = capture_from_payload(payload)
    tool = next(item for item in spec.tools if item.kind == "software")
    assert tool.id == normalize_software_id("微信开发者工具")
    assert tool.id != "item"


# ---------------------------------------------------------------------- 核对
def _context(**kwargs) -> CheckContext:
    base = dict(
        env={
            ("user", "JAVA_HOME"): r"C:\Java\jdk-21",
            ("user", "DB_TOKEN"): "present",
        },
        which=lambda name: r"C:\Python312\python.exe" if name == "python" else None,
        exists=lambda path: path == r"C:\Tools",
        known_tools={"python": "3.12.3"},
    )
    base.update(kwargs)
    return CheckContext(**base)


def test_check_spec_ok_and_counts():
    result = check_spec(_spec(), _context(), machine={"os": "windows", "arch": "x64"})
    assert result["summary"]["total"] == len(result["items"])
    statuses = {(item["section"], item["target"]): item["status"] for item in result["items"]}
    assert statuses[("tool", "python")] == "ok"
    assert statuses[("env", "user:JAVA_HOME")] == "ok"
    assert statuses[("env", "user:DB_TOKEN")] == "ok"
    assert statuses[("env", "user:OPTIONAL")] == "skipped"
    assert statuses[("path", r"C:\Tools")] == "ok"
    assert statuses[("command", "python --version")] == "skipped"
    assert statuses[("port", "8080")] == "skipped"
    assert result["score"]["score"] >= 90


def test_check_spec_missing_and_mismatch():
    spec = _spec()
    context = _context(
        env={("user", "JAVA_HOME"): r"C:\Java\jdk-17"},
        which=lambda name: None,
        exists=lambda path: False,
        known_tools={},
    )
    result = check_spec(spec, context, machine={"os": "linux", "arch": "arm64"})
    statuses = {(item["section"], item["target"]): item["status"] for item in result["items"]}
    assert statuses[("machine", "os")] == "mismatch"
    assert statuses[("machine", "arch")] == "mismatch"
    assert statuses[("tool", "python")] == "missing"
    assert statuses[("env", "user:JAVA_HOME")] == "mismatch"
    assert statuses[("env", "user:DB_TOKEN")] == "missing"
    assert statuses[("path", r"C:\Tools")] == "missing"
    assert result["summary"]["missing"] >= 2
    assert result["score"]["grade"] in {"C", "D"}


def test_check_spec_command_execution_and_ports():
    spec = EnvironmentSpec(
        commands=[SpecCommand(run="demo", expect="ok")],
        ports=[SpecPort(port=9000)],
    )
    context = CheckContext(run=lambda cmd: (0, "ok"), port_checker=lambda port: False)
    result = check_spec(spec, context, run_commands=True, check_ports=True)
    statuses = {item["target"]: item["status"] for item in result["items"]}
    assert statuses["demo"] == "ok"
    assert statuses["9000"] == "mismatch"


def test_check_spec_command_error():
    spec = EnvironmentSpec(commands=[SpecCommand(run="broken")])
    context = CheckContext(run=lambda cmd: (2, "boom"))
    result = check_spec(spec, context, run_commands=True)
    assert result["items"][0]["status"] == "error"
    assert "boom" in result["items"][0]["detail"]


def test_check_spec_software_requires_listing():
    spec = EnvironmentSpec(tools=[SpecTool(id="git", version=">=2", kind="software")])
    result = check_spec(spec, CheckContext())
    assert result["items"][0]["status"] == "skipped"
    result2 = check_spec(
        spec,
        CheckContext(which=lambda name: None, known_tools={"git": "2.44"}, software_checked=True),
    )
    assert result2["items"][0]["status"] == "ok"


def test_summary_and_score_bounds():
    summary = summarize_items(
        [
            {"status": "missing"},
            {"status": "mismatch"},
            {"status": "ok"},
        ]
    )
    assert summary["missing"] == 1 and summary["total"] == 3
    assert health_score(summary)["score"] == 77
    heavy = health_score({"missing": 10})
    assert heavy["score"] == 0 and heavy["grade"] == "D"


# ---------------------------------------------------------------------- 差异
def test_diff_specs_detects_changes():
    left = _spec()
    right = EnvironmentSpec(
        name="demo2",
        machine=MachineSpec(os="windows", arch="x64"),
        tools=[
            SpecTool(id="python", version=">=3.12", source="winget:Python.Python.3.12"),
            SpecTool(id="node", version="22"),
        ],
        env=[
            SpecEnvVar(key="JAVA_HOME", value=r"C:\Java\jdk-17"),
            SpecEnvVar(key="DB_TOKEN", secret=True),
        ],
        paths=[SpecPath(path=r"C:\Tools"), SpecPath(path=r"C:\Other")],
        commands=[SpecCommand(run="python --version", expect="Python 3.12")],
        ports=[SpecPort(port=8080, required=False)],
    )
    diff = diff_specs(left, right)
    assert diff["summary"]["total"] > 0
    assert diff["sections"]["tools"]["added"][0]["id"] == "node"
    assert diff["sections"]["tools"]["changed"][0]["after"]["version"] == ">=3.12"
    assert diff["sections"]["paths"]["added"][0]["path"] == r"C:\Other"


def test_diff_specs_identical():
    diff = diff_specs(_spec(), _spec())
    assert diff["summary"]["total"] == 0


# ---------------------------------------------------------------------- 计划
def test_install_command_variants():
    assert install_command(SpecTool(id="x", source="winget:Pkg.Name"))[1] is True
    assert "winget install --id Pkg.Name" in install_command(SpecTool(id="x", source="winget:Pkg.Name"))[0]
    assert install_command(SpecTool(id="x", source="scoop:python"))[0] == "scoop install python"
    assert install_command(SpecTool(id="x", source="choco:git"))[0].startswith("choco install git")
    assert install_command(SpecTool(id="x")) == ("", False)


def test_build_plan_turns_findings_into_steps():
    spec = EnvironmentSpec(
        name="demo",
        tools=[SpecTool(id="python", version=">=3.11", source="winget:Python.Python.3.12")],
        env=[SpecEnvVar(key="JAVA_HOME", value=r"C:\Java"), SpecEnvVar(key="DB_TOKEN", secret=True)],
        paths=[SpecPath(path=r"C:\Tools")],
    )
    context = CheckContext(env={}, which=lambda name: None, exists=lambda path: False)
    report = check_spec(spec, context)
    plan = build_plan(spec, report)
    targets = {step["target"]: step for step in plan["steps"]}
    assert "winget install --id Python.Python.3.12" in targets["python"]["command"]
    assert targets["python"]["automatic"] is True
    assert targets["user:JAVA_HOME"]["command"] == (
        r"[Environment]::SetEnvironmentVariable('JAVA_HOME', 'C:\Java', 'User')"
    )
    assert targets["user:DB_TOKEN"]["manual"] is True
    assert "New-Item" in targets[r"C:\Tools"]["command"]
    assert "EnvForge 环境计划" in plan["script"]


def test_build_plan_session_env_uses_process_assignment():
    spec = EnvironmentSpec(
        name="demo", env=[SpecEnvVar(key="SESSION_ONLY", value="demo", scope="session")]
    )
    context = CheckContext(env={}, which=lambda name: None, exists=lambda path: False)
    report = check_spec(spec, context)
    plan = build_plan(spec, report)
    step = [item for item in plan["steps"] if item["target"] == "session:SESSION_ONLY"][0]
    assert step["command"] == "$env:SESSION_ONLY = 'demo'"


def test_build_plan_no_changes():
    spec = EnvironmentSpec(name="clean")
    plan = build_plan(spec, {"items": []})
    assert plan["steps"] == []
    assert "没有需要处理的差异" in plan["script"]
