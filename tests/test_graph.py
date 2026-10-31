from __future__ import annotations

import sys

from envforge.graph import build_graph, observed_resolution, to_dot
from envforge.models import now_iso
from envforge.profiles import ProfileService, ProjectService
from envforge.runtimes import RuntimeService


def _make_project(db, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    ProjectService(db).add("demo", str(project))
    profiles = ProfileService(db)
    profiles.set_var("demo", "dev", "API_URL", "http://localhost")
    profiles.set_var("demo", "dev", "API_BASE", "%API_URL%/v1")
    return project


def test_build_graph_has_declared_and_referenced_edges(db, tmp_path):
    _make_project(db, tmp_path)
    RuntimeService(db).register("python", sys.executable, "3.12.0")
    graph = build_graph(db)

    ids = {node["id"] for node in graph["nodes"]}
    assert "project:demo" in ids
    assert "profile:demo/dev" in ids
    assert "var:API_URL" in ids and "var:API_BASE" in ids
    assert any(node_id.startswith("runtime:") for node_id in ids)
    assert any(node_id.startswith("exe:") for node_id in ids)

    edges = {(edge["source"], edge["target"], edge["kind"]) for edge in graph["edges"]}
    assert ("project:demo", "profile:demo/dev", "declared") in edges
    assert ("profile:demo/dev", "var:API_URL", "declared") in edges
    assert ("var:API_BASE", "var:API_URL", "referenced") in edges

    summary = graph["summary"]
    assert summary["projects"] == 1
    assert summary["profiles"] == 1
    assert summary["runtimes"] == 1
    assert summary["nodes"] == len(graph["nodes"])
    assert summary["edges"] == len(graph["edges"])
    assert summary["issues"] == 0


def test_build_graph_reports_issues(db, tmp_path):
    _make_project(db, tmp_path)
    missing_exe = str(tmp_path / "gone" / "python.exe")
    db.execute(
        "INSERT INTO runtimes(kind, version, exe_path, source, notes, created_at)"
        " VALUES('python','3.11.0',?,'manual','',?)",
        (missing_exe, now_iso()),
    )
    graph = build_graph(
        db,
        env_values={"API_BASE": "%API_URL%/v1", "UNKNOWN": "%NOPE%"},
        path_value=str(tmp_path / "missing-dir"),
    )
    codes = {issue["code"] for issue in graph["issues"]}
    assert "runtime.missing" in codes
    assert "var.not-set" in codes  # API_URL 未在当前环境变量里
    assert "var.unresolved" in codes  # NOPE 未定义
    assert "path.missing" in codes
    assert graph["summary"]["issues"] == len(graph["issues"])
    runtime_node = next(node for node in graph["nodes"] if node["type"] == "runtime")
    assert runtime_node["missing"] is True


def test_build_graph_without_env_values_skips_not_set(db, tmp_path):
    _make_project(db, tmp_path)
    graph = build_graph(db)
    assert all(issue["code"] != "var.not-set" for issue in graph["issues"])


def test_observed_resolution_matches_known_runtime(db, tmp_path):
    folder = tmp_path / "bin"
    folder.mkdir()
    exe = folder / "python.exe"
    exe.write_text("", encoding="utf-8")
    RuntimeService(db).register("python", str(exe), "3.12.0")
    graph = build_graph(db)

    matched = observed_resolution(graph, "python", [str(folder)])
    assert matched is not None
    assert matched["matched_runtime"] is True

    other = tmp_path / "other"
    other.mkdir()
    (other / "python.exe").write_text("", encoding="utf-8")
    outside = observed_resolution(graph, "python", [str(other)])
    assert outside is not None and outside["matched_runtime"] is False
    assert observed_resolution(graph, "nope", []) is None


def test_build_graph_expands_vars_in_path(db, tmp_path):
    _make_project(db, tmp_path)
    folder = tmp_path / "bin"
    folder.mkdir()
    graph = build_graph(db, env_values={"BIN": str(folder)}, path_value="%BIN%")
    assert all(issue["code"] != "path.missing" for issue in graph["issues"])


def test_to_dot_escapes_ids_and_labels():
    graph = {
        "nodes": [{"id": 'var:A"B', "type": "var", "label": r'C:\x"y'}],
        "edges": [],
    }
    dot = to_dot(graph)
    assert 'var:A\\"B' in dot
    assert r'C:\\x\"y' in dot


def test_to_dot_renders_shapes_and_missing_style(db, tmp_path):
    _make_project(db, tmp_path)
    db.execute(
        "INSERT INTO runtimes(kind, version, exe_path, source, notes, created_at)"
        " VALUES('python','3.11.0',?,'manual','',?)",
        (str(tmp_path / "gone" / "python.exe"), now_iso()),
    )
    dot = to_dot(build_graph(db))
    assert dot.startswith("digraph envforge {")
    assert 'shape=box' in dot
    assert 'shape=component' in dot
    assert "style=dashed" in dot
    assert "->" in dot and dot.rstrip().endswith("}")
