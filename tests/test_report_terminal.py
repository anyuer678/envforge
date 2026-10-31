from __future__ import annotations

import pytest

from envforge.errors import NotFoundError, ValidationError
from envforge.report import (
    render_check_html,
    render_check_markdown,
    render_diff_html,
    render_diff_markdown,
    render_graph_markdown,
    render_lab_markdown,
    render_profile_html,
    render_profile_markdown,
    write_text,
)
from envforge.spec import EnvironmentSpec, SpecCommand, SpecPath, check_spec
from envforge.terminal import SHELLS, TerminalService


# ---------------------------------------------------------------------- 报告
def _check_result():
    spec = EnvironmentSpec(
        name="demo <检查>",
        paths=[SpecPath(path="<script>alert(1)</script>", must_exist=False)],
        commands=[SpecCommand(run="echo hi")],
    )
    return check_spec(spec, check_spec_context())


def check_spec_context():
    from envforge.spec import CheckContext

    return CheckContext(exists=lambda path: False)


def test_render_check_markdown_contains_summary_and_items():
    text = render_check_markdown(_check_result())
    assert "# 环境标准检查" in text
    assert "健康评分" in text
    assert "| 环节 | 目标 |" in text
    assert "跳过" in text


def test_render_check_html_is_self_contained_and_escaped():
    html = render_check_html(_check_result())
    assert html.startswith("<!DOCTYPE html>")
    assert "<link" not in html
    assert "http://" not in html and "https://" not in html
    assert "&lt;script&gt;" in html
    assert "<script>alert(1)</script>" not in html


def test_render_check_reports_include_read_warnings():
    result = _check_result()
    result["warnings"] = ["无法读取 system：拒绝访问"]
    markdown = render_check_markdown(result)
    html = render_check_html(result)
    assert "读取提示" in markdown and "无法读取 system" in markdown
    assert "无法读取 system" in html


def _profile_payload():
    return {
        "machine": {
            "hostname": "devbox",
            "os": "Windows",
            "release": "11",
            "arch": "AMD64",
            "version": "10.0",
            "python": "3.12.3",
        },
        "warnings": ["注册表读取受限"],
        "runtimes": [{"kind": "python", "version": "3.12.3", "exe_path": r"C:\Python312\python.exe"}],
        "software": [{"name": "Git", "version": "2.44", "publisher": "The Git Project"}],
        "env": [
            {"scope": "user", "key": "JAVA_HOME", "value": r"C:\Java", "secret": False},
            {"scope": "user", "key": "DB_PASSWORD", "value": "hunter2", "secret": True},
            {"scope": "user", "key": "PATH", "value": None, "secret": False},
        ],
        "path": [{"scope": "user", "entries": [r"C:\Python312"]}],
        "terminals": {
            "powershell": {"pwsh": "", "powershell": "", "profiles": []},
            "git_bash": {"bash": ""},
        },
    }


def test_render_profile_markdown_masks_secrets():
    text = render_profile_markdown(_profile_payload())
    assert "# 开发机画像：devbox" in text
    assert "hunter2" not in text
    assert "***" in text
    assert "C:\\Java" in text
    assert "注册表读取受限" in text
    assert "C:\\Python312" in text


def test_render_profile_html_masks_secrets():
    html = render_profile_html(_profile_payload())
    assert "hunter2" not in html
    assert "***" in html
    assert "devbox" in html
    assert "Git" in html


def _diff_payload():
    return {
        "from": {"id": 1, "created_at": "2026-10-01T09:00:00", "label": "before", "scope": "user"},
        "to": {"id": 2, "created_at": "2026-10-02T09:00:00", "label": "after", "scope": "user"},
        "summary": {
            "added": 1,
            "removed": 0,
            "changed": 1,
            "total": 2,
            "by_source": {"envforge": 1, "suspected": 1, "unknown": 0},
        },
        "changes": [
            {
                "section": "env",
                "key": "JAVA_HOME",
                "change": "added",
                "before": None,
                "after": r"C:\Java\jdk-21",
                "source": "envforge",
                "detail": "EnvForge 写入",
            },
            {
                "section": "path",
                "key": r"C:\Tools",
                "change": "changed",
                "before": "C:\\Tools",
                "after": "C:\\Tools2",
                "source": "suspected",
                "detail": "目录时间晚于基线",
            },
        ],
        "notes": ["来源分类仅供参考。"],
    }


def test_render_diff_markdown_and_html():
    diff = _diff_payload()
    text = render_diff_markdown(diff)
    assert "# 环境变更追踪" in text
    assert "JAVA_HOME" in text
    assert "已知（EnvForge 审计）" in text
    assert "推测（外部/手工）" in text

    html = render_diff_html(diff)
    assert "环境变更追踪" in html
    assert "JAVA_HOME" in html
    assert "<link" not in html


def test_render_diff_handles_no_changes():
    diff = _diff_payload()
    diff["changes"] = []
    diff["summary"] = {"added": 0, "removed": 0, "changed": 0, "total": 0, "by_source": {}}
    assert "两个基线没有差异" in render_diff_markdown(diff)
    assert "两个基线没有差异" in render_diff_html(diff)


def test_render_graph_markdown():
    graph = {
        "summary": {"nodes": 3, "edges": 2, "projects": 1, "profiles": 1, "vars": 1, "runtimes": 0, "issues": 0},
        "nodes": [],
        "edges": [{"source": "project:demo", "target": "profile:demo/dev", "kind": "declared"}],
        "issues": [],
    }
    text = render_graph_markdown(graph)
    assert "# 环境依赖关系图" in text
    assert "project:demo" in text
    assert "未发现失效依赖链" in text


def test_render_lab_markdown():
    result = {
        "evidence": "measured",
        "path": {"added": ["C:\\New"], "removed": ["C:\\Old"], "missing": [], "duplicates": []},
        "commands": [
            {"command": "python", "before": "", "after": r"C:\New\python.exe", "changed": True}
        ],
        "warnings": ["实测与静态解析不一致"],
        "notes": ["只读模拟"],
    }
    text = render_lab_markdown(result)
    assert "# 环境实验结果" in text
    assert "实测" in text
    assert "C:\\New" in text
    assert "实测与静态解析不一致" in text


def test_write_text_creates_parent_directory(tmp_path):
    target = write_text("hello", tmp_path / "nested" / "deep" / "report.md")
    assert target.read_text(encoding="utf-8") == "hello"


# ---------------------------------------------------------------------- 终端配置
def test_terminal_upsert_get_and_update(db):
    service = TerminalService(db)
    first = service.upsert("dev", "powershell", "$env:FOO = 'bar'")
    assert first.id is not None and first.shell == "powershell"
    assert first.to_dict()["lines"] == 1

    updated = service.upsert("DEV", "powershell", "$env:FOO = 'baz'\nWrite-Host ok")
    assert updated.id == first.id
    assert updated.to_dict()["lines"] == 2
    assert "$env:FOO = 'baz'" in service.get("dev", "powershell").content


def test_terminal_list_filters_by_shell(db):
    service = TerminalService(db)
    service.upsert("dev", "powershell", "echo ps")
    service.upsert("dev", "bash", "export A=1")
    assert len(service.list()) == 2
    assert [item.shell for item in service.list("bash")] == ["bash"]


def test_terminal_export_has_header_and_content(db):
    service = TerminalService(db)
    service.upsert("dev", "powershell", "$env:FOO = 'bar'")
    text = service.export("dev", "powershell")
    assert "EnvForge 终端片段" in text
    assert "$PROFILE" in text
    assert "$env:FOO = 'bar'" in text

    service.upsert("dev", "bash", "export A=1")
    assert "~/.bashrc" in service.export("dev", "bash")

    service.upsert("dev", "cmd", "set A=1")
    assert ".bat" in service.export("dev", "cmd")


def test_terminal_remove_and_missing(db):
    service = TerminalService(db)
    service.upsert("dev", "powershell", "echo hi")
    service.remove("dev", "powershell")
    with pytest.raises(NotFoundError):
        service.get("dev", "powershell")
    with pytest.raises(NotFoundError):
        service.remove("dev", "powershell")


def test_terminal_validation(db):
    service = TerminalService(db)
    with pytest.raises(ValidationError):
        service.upsert("bad name", "powershell", "x")
    with pytest.raises(ValidationError):
        service.upsert("", "powershell", "x")
    with pytest.raises(ValidationError):
        service.upsert("dev", "nushell", "x")
    with pytest.raises(ValidationError):
        service.upsert("dev", "powershell", "x" * 20001)
    assert set(SHELLS) == {"powershell", "cmd", "bash"}
