"""报告渲染：Markdown 与自包含 HTML（不依赖外部 CSS / JS）。

报告只使用传入的数据，不做任何采集；便于 CLI 与 GUI 复用同一套输出。
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .spec import EVIDENCE_LABELS, SECTION_LABELS, STATUS_LABELS
from .baseline import SOURCE_LABELS

_STATUS_COLORS = {
    "ok": "#1a7f37",
    "missing": "#cf222e",
    "mismatch": "#bc4c00",
    "unknown": "#6e7781",
    "skipped": "#6e7781",
    "error": "#cf222e",
}


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def write_text(text: str, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


# ---------------------------------------------------------------------- 规格检查
def render_check_markdown(result: Mapping[str, Any]) -> str:
    spec = result.get("spec") or {}
    summary = result.get("summary") or {}
    score = result.get("score") or {}
    counts = spec.get("counts") or {}
    lines = [
        f"# 环境标准检查：{spec.get('name') or '未命名'}",
        "",
        f"- 规格文件：{spec.get('source') or '（未保存）'}",
        f"- 生成时间：{result.get('generated_at') or ''}",
        f"- 健康评分：{score.get('score', 0)} / 100（等级 {score.get('grade', '-')}）",
        f"- 规格内容：工具 {counts.get('tools', 0)}，变量 {counts.get('env', 0)}，"
        f"目录 {counts.get('paths', 0)}，命令 {counts.get('commands', 0)}，端口 {counts.get('ports', 0)}",
        "",
        "## 汇总",
        "",
        "| 状态 | 数量 |",
        "| --- | ---: |",
    ]
    for key in ("ok", "missing", "mismatch", "unknown", "skipped", "error"):
        lines.append(f"| {STATUS_LABELS.get(key, key)} | {summary.get(key, 0)} |")
    warnings = result.get("warnings") or []
    lines += ["", f"合计 {summary.get('total', 0)} 项。", ""]
    if warnings:
        lines += ["## 读取提示", ""] + [f"- {item}" for item in warnings] + [""]
    lines += ["## 明细", ""]
    lines += [
        "| 环节 | 目标 | 期望 | 实际 | 状态 | 证据 | 说明 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in result.get("items") or []:
        lines.append(
            "| {section} | {target} | {expected} | {actual} | {status} | {evidence} | {detail} |".format(
                section=_md_cell(item.get("section_label")),
                target=_md_cell(item.get("target")),
                expected=_md_cell(item.get("expected")),
                actual=_md_cell(item.get("actual")),
                status=_md_cell(item.get("status_label")),
                evidence=_md_cell(item.get("evidence_label")),
                detail=_md_cell(item.get("detail")),
            )
        )
    return "\n".join(lines) + "\n"


def render_check_html(result: Mapping[str, Any]) -> str:
    spec = result.get("spec") or {}
    summary = result.get("summary") or {}
    score = result.get("score") or {}
    cards = "".join(
        f'<div class="card"><div class="num" style="color:{_STATUS_COLORS.get(key, "#333")}">'
        f"{summary.get(key, 0)}</div><div class=\"label\">{_e(STATUS_LABELS.get(key, key))}</div></div>"
        for key in ("ok", "missing", "mismatch", "unknown", "skipped", "error")
    )
    rows: list[str] = []
    for item in result.get("items") or []:
        color = _STATUS_COLORS.get(str(item.get("status")), "#333")
        rows.append(
            "<tr>"
            f"<td>{_e(item.get('section_label'))}</td>"
            f"<td class=\"mono\">{_e(item.get('target'))}</td>"
            f"<td>{_e(item.get('expected'))}</td>"
            f"<td class=\"mono\">{_e(item.get('actual'))}</td>"
            f'<td><span class="badge" style="background:{color}">{_e(item.get("status_label"))}</span></td>'
            f"<td>{_e(item.get('evidence_label'))}</td>"
            f"<td>{_e(item.get('detail'))}</td>"
            "</tr>"
        )
    warnings = result.get("warnings") or []
    warning_block = ""
    if warnings:
        items = "".join(f"<li>{_e(item)}</li>" for item in warnings)
        warning_block = (
            '<section class="muted small"><p>读取提示：部分作用域未能读取，'
            "下列结果可能误报缺失。</p><ul>"
            f"{items}</ul></section>"
        )
    body = f"""
<section class="hero">
  <div>
    <h1>环境标准检查：{_e(spec.get('name') or '未命名')}</h1>
    <p class="muted">规格文件：{_e(spec.get('source') or '（未保存）')}　·　生成时间：{_e(result.get('generated_at'))}</p>
  </div>
  <div class="score"><div class="score-num">{score.get('score', 0)}</div>
    <div class="muted">健康评分 / 100　等级 {_e(score.get('grade', '-'))}</div></div>
</section>
<section class="cards">{cards}</section>
{warning_block}
<section>
<table>
<thead><tr><th>环节</th><th>目标</th><th>期望</th><th>实际</th><th>状态</th><th>证据</th><th>说明</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
</section>
<section class="muted small">
  <p>证据等级：实测 = 实际运行或读取得到；声明 = 来自配置文件/登记记录；推断 = 由线索推测；未知 = 数据不足。</p>
  <p>本报告只描述当前状态，不会自动修改环境。执行前请人工确认计划。</p>
</section>
"""
    return _page(f"环境检查 - {spec.get('name') or 'EnvForge'}", body)


# ---------------------------------------------------------------------- 开发机画像
def render_profile_markdown(payload: Mapping[str, Any]) -> str:
    machine = payload.get("machine") or {}
    lines = [
        f"# 开发机画像：{machine.get('hostname') or '未命名机器'}",
        "",
        f"- 操作系统：{machine.get('os') or '-'} {machine.get('release') or ''}",
        f"- 架构：{machine.get('arch') or '-'}",
        f"- 版本：{machine.get('version') or '-'}",
        f"- 内置 Python：{machine.get('python') or '-'}",
        "",
    ]
    warnings = payload.get("warnings") or []
    if warnings:
        lines += ["## 采集提示", ""] + [f"- {item}" for item in warnings] + [""]

    lines += ["## 已登记运行时", "", "| 类型 | 版本 | 路径 |", "| --- | --- | --- |"]
    for item in payload.get("runtimes") or []:
        lines.append(
            f"| {_md_cell(item.get('kind'))} | {_md_cell(item.get('version') or '未知')} | {_md_cell(item.get('exe_path'))} |"
        )
    if not payload.get("runtimes"):
        lines.append("| - | - | 暂无登记（运行 `envforge runtime detect`） |")

    lines += ["", "## 开发软件", "", "| 名称 | 版本 | 发布者 |", "| --- | --- | --- |"]
    for item in payload.get("software") or []:
        lines.append(
            f"| {_md_cell(item.get('name'))} | {_md_cell(item.get('version'))} | {_md_cell(item.get('publisher'))} |"
        )
    if not payload.get("software"):
        lines.append("| - | - | 未扫描到开发软件 |")

    lines += ["", "## 环境变量", "", "| 作用域 | 变量 | 值 |", "| --- | --- | --- |"]
    for item in payload.get("env") or []:
        value = "***" if item.get("secret") else item.get("value")
        if value is None:
            value = "（仅记录存在性）" if not item.get("secret") else "***"
        lines.append(f"| {_md_cell(item.get('scope'))} | {_md_cell(item.get('key'))} | {_md_cell(value)} |")

    lines += ["", "## PATH 目录", ""]
    for block in payload.get("path") or []:
        lines.append(f"### {block.get('scope')}")
        lines.append("")
        for entry in block.get("entries") or []:
            lines.append(f"- {entry}")
        lines.append("")

    terminals = payload.get("terminals") or {}
    if terminals:
        lines += ["## 终端", ""]
        power = terminals.get("powershell") or {}
        lines.append(f"- PowerShell 7：{power.get('pwsh') or '未找到'}")
        lines.append(f"- Windows PowerShell：{power.get('powershell') or '未找到'}")
        for profile in power.get("profiles") or []:
            state = "存在" if profile.get("exists") else "不存在"
            lines.append(f"- {profile.get('shell')} profile：{profile.get('path')}（{state}）")
        bash = terminals.get("git_bash") or {}
        lines.append(f"- Git Bash：{bash.get('bash') or '未找到'}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_profile_html(payload: Mapping[str, Any]) -> str:
    machine = payload.get("machine") or {}
    runtime_rows = "".join(
        f"<tr><td>{_e(item.get('kind'))}</td><td>{_e(item.get('version') or '未知')}</td>"
        f"<td class=\"mono\">{_e(item.get('exe_path'))}</td></tr>"
        for item in payload.get("runtimes") or []
    ) or '<tr><td colspan="3" class="muted">暂无登记运行时</td></tr>'
    software_rows = "".join(
        f"<tr><td>{_e(item.get('name'))}</td><td>{_e(item.get('version'))}</td>"
        f"<td>{_e(item.get('publisher'))}</td></tr>"
        for item in payload.get("software") or []
    ) or '<tr><td colspan="3" class="muted">未扫描到开发软件</td></tr>'
    env_rows = "".join(
        f"<tr><td>{_e(item.get('scope'))}</td><td class=\"mono\">{_e(item.get('key'))}</td>"
        f"<td class=\"mono\">"
        f"{'***' if item.get('secret') else _e(item.get('value') if item.get('value') is not None else '（仅存在性）')}"
        f"</td></tr>"
        for item in payload.get("env") or []
    ) or '<tr><td colspan="3" class="muted">未读取到环境变量</td></tr>'
    path_html = "".join(
        f"<h3>{_e(block.get('scope'))}</h3><ul>"
        + "".join(f"<li class=\"mono\">{_e(entry)}</li>" for entry in block.get("entries") or [])
        + "</ul>"
        for block in payload.get("path") or []
    )
    warnings = payload.get("warnings") or []
    warn_html = ""
    if warnings:
        warn_html = "<section class=\"warn\"><h3>采集提示</h3><ul>" + "".join(
            f"<li>{_e(item)}</li>" for item in warnings
        ) + "</ul></section>"
    body = f"""
<section class="hero">
  <div><h1>开发机画像：{_e(machine.get('hostname') or '未命名机器')}</h1>
  <p class="muted">{_e(machine.get('os'))} {_e(machine.get('release'))} · {_e(machine.get('arch'))} · Python {_e(machine.get('python'))}</p></div>
</section>
{warn_html}
<section><h2>已登记运行时</h2><table><thead><tr><th>类型</th><th>版本</th><th>路径</th></tr></thead>
<tbody>{runtime_rows}</tbody></table></section>
<section><h2>开发软件</h2><table><thead><tr><th>名称</th><th>版本</th><th>发布者</th></tr></thead>
<tbody>{software_rows}</tbody></table></section>
<section><h2>环境变量</h2><table><thead><tr><th>作用域</th><th>变量</th><th>值</th></tr></thead>
<tbody>{env_rows}</tbody></table></section>
<section><h2>PATH 目录</h2>{path_html or '<p class="muted">未读取到 PATH</p>'}</section>
"""
    return _page(f"开发机画像 - {machine.get('hostname') or 'EnvForge'}", body)


# ---------------------------------------------------------------------- 基线差异
def render_diff_markdown(diff: Mapping[str, Any]) -> str:
    left = diff.get("from") or {}
    right = diff.get("to") or {}
    summary = diff.get("summary") or {}
    lines = [
        f"# 环境变更追踪：#{left.get('id')} → #{right.get('id')}",
        "",
        f"- 起点：{left.get('created_at')}（{left.get('label') or '无标签'}，作用域 {left.get('scope')}）",
        f"- 终点：{right.get('created_at')}（{right.get('label') or '无标签'}，作用域 {right.get('scope')}）",
        f"- 变更：新增 {summary.get('added', 0)}，移除 {summary.get('removed', 0)}，"
        f"修改 {summary.get('changed', 0)}，合计 {summary.get('total', 0)}",
        "",
    ]
    by_source = summary.get("by_source") or {}
    lines += [
        "| 来源 | 数量 |",
        "| --- | ---: |",
        f"| 已知（EnvForge 审计） | {by_source.get('envforge', 0)} |",
        f"| 推测（外部/手工） | {by_source.get('suspected', 0)} |",
        f"| 未知 | {by_source.get('unknown', 0)} |",
        "",
        "## 变更明细",
        "",
        "| 环节 | 目标 | 变化 | 之前 | 之后 | 来源 | 说明 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for change in diff.get("changes") or []:
        lines.append(
            "| {section} | {key} | {change} | {before} | {after} | {source} | {detail} |".format(
                section=_md_cell(SECTION_LABELS.get(change.get("section"), change.get("section"))),
                key=_md_cell(change.get("key")),
                change={"added": "新增", "removed": "移除", "changed": "修改"}.get(
                    change.get("change"), change.get("change")
                ),
                before=_md_cell(change.get("before")),
                after=_md_cell(change.get("after")),
                source=_md_cell(SOURCE_LABELS.get(change.get("source"), change.get("source"))),
                detail=_md_cell(change.get("detail")),
            )
        )
    if not diff.get("changes"):
        lines.append("| - | - | - | - | - | - | 两个基线没有差异 |")
    lines += ["", "## 说明", ""] + [f"- {note}" for note in diff.get("notes") or []]
    return "\n".join(lines) + "\n"


def render_diff_html(diff: Mapping[str, Any]) -> str:
    left = diff.get("from") or {}
    right = diff.get("to") or {}
    summary = diff.get("summary") or {}
    by_source = summary.get("by_source") or {}
    rows: list[str] = []
    for change in diff.get("changes") or []:
        change_kind = str(change.get("change"))
        color = {"added": "#1a7f37", "removed": "#cf222e", "changed": "#bc4c00"}.get(
            change_kind, "#333"
        )
        source = SOURCE_LABELS.get(str(change.get("source")), str(change.get("source")))
        rows.append(
            "<tr>"
            f"<td>{_e(SECTION_LABELS.get(change.get('section'), change.get('section')))}</td>"
            f"<td class=\"mono\">{_e(change.get('key'))}</td>"
            f'<td><span class="badge" style="background:{color}">'
            f"{_e({'added': '新增', 'removed': '移除', 'changed': '修改'}.get(change_kind, change_kind))}</span></td>"
            f"<td class=\"mono\">{_e(change.get('before'))}</td>"
            f"<td class=\"mono\">{_e(change.get('after'))}</td>"
            f"<td>{_e(source)}</td>"
            f"<td>{_e(change.get('detail'))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="7" class="muted">两个基线没有差异</td></tr>')
    body = f"""
<section class="hero">
  <div><h1>环境变更追踪</h1>
  <p class="muted">#{_e(left.get('id'))} {_e(left.get('created_at'))} → #{_e(right.get('id'))} {_e(right.get('created_at'))}</p></div>
  <div class="score"><div class="score-num">{summary.get('total', 0)}</div><div class="muted">项变更</div></div>
</section>
<section class="cards">
  <div class="card"><div class="num" style="color:#1a7f37">{summary.get('added', 0)}</div><div class="label">新增</div></div>
  <div class="card"><div class="num" style="color:#cf222e">{summary.get('removed', 0)}</div><div class="label">移除</div></div>
  <div class="card"><div class="num" style="color:#bc4c00">{summary.get('changed', 0)}</div><div class="label">修改</div></div>
  <div class="card"><div class="num">{by_source.get('envforge', 0)}</div><div class="label">已知来源</div></div>
  <div class="card"><div class="num">{by_source.get('suspected', 0)}</div><div class="label">推测来源</div></div>
  <div class="card"><div class="num">{by_source.get('unknown', 0)}</div><div class="label">未知来源</div></div>
</section>
<section><table><thead><tr><th>环节</th><th>目标</th><th>变化</th><th>之前</th><th>之后</th><th>来源</th><th>说明</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></section>
<section class="muted small">
  <p>已知来源仅表示 EnvForge 审计日志中有对应写入；推测来源来自目录时间等线索；未知表示无法追溯。</p>
</section>
"""
    return _page("环境变更追踪", body)


# ---------------------------------------------------------------------- 通用页面
def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 32px; font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
    background: #f6f8fa; color: #1f2328; line-height: 1.55; }}
  .wrap {{ max-width: 1180px; margin: 0 auto; }}
  .hero {{ display: flex; justify-content: space-between; gap: 24px; align-items: center;
    background: #fff; border: 1px solid #d0d7de; border-radius: 10px; padding: 24px 28px; margin-bottom: 20px; }}
  h1 {{ margin: 0 0 6px; font-size: 26px; }}
  h2 {{ font-size: 18px; margin: 0 0 12px; }}
  h3 {{ font-size: 15px; margin: 16px 0 6px; }}
  .muted {{ color: #57606a; }}
  .small {{ font-size: 12px; }}
  .score {{ text-align: right; min-width: 140px; }}
  .score-num {{ font-size: 36px; font-weight: 700; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }}
  .card {{ flex: 1 1 120px; background: #fff; border: 1px solid #d0d7de; border-radius: 8px;
    padding: 14px 16px; text-align: center; }}
  .card .num {{ font-size: 24px; font-weight: 700; }}
  .card .label {{ font-size: 12px; color: #57606a; }}
  section {{ background: #fff; border: 1px solid #d0d7de; border-radius: 10px;
    padding: 20px 24px; margin-bottom: 20px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; background: #f6f8fa; color: #57606a; font-weight: 600;
    padding: 8px 10px; border-bottom: 1px solid #d0d7de; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #eaeef2; vertical-align: top; }}
  .mono {{ font-family: Consolas, "Cascadia Mono", monospace; font-size: 12px; word-break: break-all; }}
  .badge {{ color: #fff; border-radius: 999px; padding: 2px 9px; font-size: 12px; white-space: nowrap; }}
  .warn {{ border-color: #f0b429; background: #fff8e6; }}
  ul {{ margin: 6px 0; padding-left: 20px; }}
</style>
</head>
<body><div class="wrap">{body}</div></body>
</html>
"""


def render_graph_markdown(graph: Mapping[str, Any]) -> str:
    summary = graph.get("summary") or {}
    lines = [
        "# 环境依赖关系图",
        "",
        f"- 节点：{summary.get('nodes', 0)}（项目 {summary.get('projects', 0)}、"
        f"档案 {summary.get('profiles', 0)}、变量 {summary.get('vars', 0)}、"
        f"运行时 {summary.get('runtimes', 0)}）",
        f"- 边：{summary.get('edges', 0)}",
        f"- 问题：{summary.get('issues', 0)}",
        "",
        "## 依赖边",
        "",
        "| 来源 | 目标 | 类型 |",
        "| --- | --- | --- |",
    ]
    for edge in graph.get("edges") or []:
        lines.append(f"| {_md_cell(edge['source'])} | {_md_cell(edge['target'])} | {_md_cell(edge['kind'])} |")
    lines += ["", "## 问题", ""]
    for issue in graph.get("issues") or []:
        lines.append(f"- [{issue.get('severity')}] {issue.get('detail')}")
    if not graph.get("issues"):
        lines.append("- 未发现失效依赖链。")
    return "\n".join(lines) + "\n"


def render_lab_markdown(result: Mapping[str, Any]) -> str:
    path = result.get("path") or {}
    lines = [
        "# 环境实验结果",
        "",
        f"- 证据等级：{EVIDENCE_LABELS.get(str(result.get('evidence')), result.get('evidence'))}",
        "",
        "## PATH 变化",
        "",
        f"- 新增：{', '.join(path.get('added') or []) or '无'}",
        f"- 移除：{', '.join(path.get('removed') or []) or '无'}",
        f"- 缺失目录：{', '.join(path.get('missing') or []) or '无'}",
        f"- 重复目录：{', '.join(path.get('duplicates') or []) or '无'}",
        "",
        "## 命令解析",
        "",
        "| 命令 | 修改前 | 修改后 | 变化 |",
        "| --- | --- | --- | --- |",
    ]
    for item in result.get("commands") or []:
        lines.append(
            f"| {_md_cell(item['command'])} | {_md_cell(item['before'] or '未找到')} | "
            f"{_md_cell(item['after'] or '未找到')} | {'是' if item['changed'] else '否'} |"
        )
    if result.get("warnings"):
        lines += ["", "## 警告", ""] + [f"- {item}" for item in result["warnings"]]
    lines += ["", "## 说明", ""] + [f"- {item}" for item in result.get("notes") or []]
    return "\n".join(lines) + "\n"
