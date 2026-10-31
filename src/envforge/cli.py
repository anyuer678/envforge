"""命令行入口。与 GUI 共享同一套核心服务；所有破坏性操作要求 --yes。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .audit import AuditLog
from .baseline import BaselineService
from .collectors import capture_payload, current_machine, normalize_software_id, scan_software
from .db import Database, DatabaseError, default_db_path
from .doctor import Doctor, summarize
from .envstore import EnvService
from .envvars import SCOPES, dedupe_path, scope_label
from .errors import (
    ConfirmationRequired,
    EnvAccessError,
    EnvForgeError,
    EnvWriteFailure,
    NotFoundError,
    ValidationError,
)
from . import graph as graph_module
from . import lab as lab_module
from . import report as report_module
from .library import CATEGORIES, all_presets, count_by_category, get as get_preset, search as search_presets
from .models import RUNTIME_KINDS
from .profiles import ProfileService, ProjectService
from .runtimes import RuntimeService
from .spec import (
    CheckContext,
    build_plan,
    capture_from_payload,
    check_spec,
    diff_specs,
    dump_spec,
    load_spec,
    save_spec,
    version_key,
)
from .terminal import SHELLS, TerminalService

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFIRM = 3


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _split_scope(text: str | None, *, allow_all: bool = False) -> list[str]:
    if not text:
        return ["user", "system"] if allow_all else ["user"]
    scopes = [item.strip() for item in text.split(",") if item.strip()]
    for scope in scopes:
        if scope not in SCOPES:
            raise ValidationError(f"未知作用域 {scope!r}；可选：{', '.join(SCOPES)}")
    return scopes


def _single_scope(text: str | None) -> str:
    scopes = _split_scope(text)
    if len(scopes) != 1:
        raise ValidationError("该子命令只支持单个作用域；请分别对 user / system / session 执行。")
    return scopes[0]


def _read_text_file(path: Path) -> str:
    """读取用户文本文件，兼容 PowerShell 5 常见的 UTF-16/GBK 导出。"""
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeError:
            pass
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeError:
            continue
    raise ValidationError(
        f"无法识别内容文件 {path} 的编码；支持 UTF-8 / UTF-16 / GB18030。"
    )


def _parse_pair(text: str) -> tuple[str, str]:
    if "=" not in text:
        raise ValidationError(f"{text!r} 不是 KEY=VALUE 形式；示例：API_URL=https://example.com")
    key, value = text.split("=", 1)
    if not key.strip():
        raise ValidationError("KEY 不能为空。")
    return key.strip(), value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="envforge",
        description="EnvForge：运行时版本、项目环境档案与系统环境变量管理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用示例：\n"
            "  envforge runtime detect\n"
            "  envforge project add --name demo --path D:\\work\\demo\n"
            "  envforge profile set demo dev API_URL=http://localhost:8000\n"
            "  envforge profile set demo dev DB_PASSWORD=*** --secret\n"
            "  envforge profile activate demo dev --yes\n"
            "  envforge library list --category 代理与网络\n"
            "  envforge library show JAVA_HOME --json\n"
            "  envforge env snapshot --scope user --label before-change\n"
            "  envforge env path --scope user --json\n"
            "  envforge env diff 1 2\n"
            "  envforge env db-check\n"
            "  envforge audit list --limit 30\n"
            "  envforge doctor --json\n"
            "  envforge machine profile --output reports\n"
            "  envforge spec capture --output envforge-spec.toml\n"
            "  envforge spec check envforge-spec.toml --report reports\n"
            "  envforge spec diff old.toml new.toml\n"
            "  envforge baseline capture --label before-upgrade\n"
            "  envforge baseline diff 1 2\n"
            "  envforge lab path --scope user --add D:\\Python312 --command python\n"
            "  envforge graph build --format dot --output deps.dot\n"
            "  envforge terminal set dev --shell powershell --content \"$env:MODE='dev'\"\n"
            "  envforge gui\n"
        ),
    )
    parser.add_argument("--db", default=None, help=f"数据库路径（默认 {default_db_path()}）")
    parser.add_argument("--version", action="version", version=f"EnvForge {__version__}")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（便于脚本处理）")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # ---------------------------------------------------------------- runtime
    runtime = sub.add_parser("runtime", help="运行时版本探测、登记与移除")
    runtime_sub = runtime.add_subparsers(dest="action", metavar="ACTION")

    p = runtime_sub.add_parser("detect", help="探测 PATH 中已安装的运行时并登记")
    p.add_argument("--kind", action="append", choices=RUNTIME_KINDS, help="仅探测指定类型，可重复")

    p = runtime_sub.add_parser("list", help="列出已登记运行时")
    p.add_argument("--kind", choices=RUNTIME_KINDS)

    p = runtime_sub.add_parser("add", help="手动登记一个运行时")
    p.add_argument("--kind", required=True, choices=RUNTIME_KINDS)
    p.add_argument("--path", required=True, help="可执行文件路径，如 C:\\Python312\\python.exe")
    p.add_argument("--version", default=None, help="版本号；缺省时自动探测")
    p.add_argument("--notes", default="")

    p = runtime_sub.add_parser("remove", help="移除运行时登记（不卸载软件）")
    p.add_argument("id", type=int)
    p.add_argument("--yes", action="store_true", help="确认移除")

    p = runtime_sub.add_parser("activate", help="生成当前会话的激活脚本")
    p.add_argument("--shell", choices=["powershell"], default="powershell")

    # ---------------------------------------------------------------- project
    project = sub.add_parser("project", help="项目登记")
    project_sub = project.add_subparsers(dest="action", metavar="ACTION")

    p = project_sub.add_parser("add", help="登记项目目录")
    p.add_argument("--name", required=True)
    p.add_argument("--path", required=True)

    p = project_sub.add_parser("list", help="列出项目")

    p = project_sub.add_parser("remove", help="删除项目及其全部档案")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true")

    # ---------------------------------------------------------------- profile
    profile = sub.add_parser("profile", help="项目环境档案")
    profile_sub = profile.add_subparsers(dest="action", metavar="ACTION")

    p = profile_sub.add_parser("list", help="列出项目下的档案")
    p.add_argument("project")

    p = profile_sub.add_parser("set", help="设置变量（敏感值自动加密）")
    p.add_argument("project")
    p.add_argument("profile")
    p.add_argument("pair", metavar="KEY=VALUE")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--secret", action="store_true", help="强制按敏感值加密存储")
    group.add_argument("--plain", action="store_true", help="强制按普通值存储（不加密）")

    p = profile_sub.add_parser("unset", help="删除变量")
    p.add_argument("project")
    p.add_argument("profile")
    p.add_argument("key")
    p.add_argument("--yes", action="store_true")

    p = profile_sub.add_parser("show", help="查看档案变量（默认脱敏）")
    p.add_argument("project")
    p.add_argument("profile")
    p.add_argument("--reveal", action="store_true", help="显示敏感值明文（谨慎使用）")

    p = profile_sub.add_parser("export", help="导出 .env 文本（默认脱敏）")
    p.add_argument("project")
    p.add_argument("profile")
    p.add_argument("--reveal", action="store_true")
    p.add_argument("--output", default=None, help="写入文件；缺省打印到终端")
    p.add_argument("--yes", action="store_true", help="允许覆盖已存在的输出文件")

    p = profile_sub.add_parser("diff", help="比较两个档案")
    p.add_argument("project")
    p.add_argument("profile_a")
    p.add_argument("profile_b")
    p.add_argument("--reveal", action="store_true")

    p = profile_sub.add_parser("activate", help="生成含档案变量的会话激活脚本（需确认）")
    p.add_argument("project")
    p.add_argument("profile")
    p.add_argument("--yes", action="store_true")

    p = profile_sub.add_parser("remove", help="删除档案")
    p.add_argument("project")
    p.add_argument("profile")
    p.add_argument("--yes", action="store_true")

    # ---------------------------------------------------------------- env
    env = sub.add_parser("env", help="系统环境变量与 PATH")
    env_sub = env.add_subparsers(dest="action", metavar="ACTION")

    p = env_sub.add_parser("show", help="查看某作用域的全部变量")
    p.add_argument("--scope", default="user")
    p.add_argument("--reveal", action="store_true")

    p = env_sub.add_parser("path", help="分析 PATH（重复、失效、空段）")
    p.add_argument("--scope", default="user")

    p = env_sub.add_parser("dedupe", help="生成 PATH 去重结果（默认只预览）")
    p.add_argument("--scope", default="user", choices=["user"])
    p.add_argument("--apply", action="store_true", help="写入去重后的 PATH")
    p.add_argument("--yes", action="store_true")

    p = env_sub.add_parser("snapshot", help="创建环境快照")
    p.add_argument("--scope", default="user")
    p.add_argument("--label", default="")

    p = env_sub.add_parser("snapshots", help="列出快照")
    p.add_argument("--scope", default=None)
    p.add_argument("--limit", type=int, default=50)

    p = env_sub.add_parser("diff", help="比较两个快照")
    p.add_argument("before", type=int)
    p.add_argument("after", type=int)
    p.add_argument("--reveal", action="store_true")

    p = env_sub.add_parser("export", help="导出快照 JSON")
    p.add_argument("id", type=int)
    p.add_argument("--reveal", action="store_true")
    p.add_argument("--output", default=None)
    p.add_argument("--yes", action="store_true", help="允许覆盖已存在的输出文件")

    p = env_sub.add_parser("restore", help="回滚到快照（需确认）")
    p.add_argument("id", type=int)
    p.add_argument("--delete-extras", action="store_true", help="同时删除快照中不存在的变量")
    p.add_argument("--yes", action="store_true")

    p = env_sub.add_parser("apply", help="写入指定变量（需确认）")
    p.add_argument("--scope", default="user", choices=["user"])
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--unset", action="append", default=[], metavar="KEY")
    p.add_argument("--yes", action="store_true")

    env_sub.add_parser("db-check", help="数据库完整性自检（PRAGMA quick_check）")
    env_sub.add_parser("db-backup", help="立即备份数据库到同目录 backups/")

    # ---------------------------------------------------------------- audit
    audit_cmd = sub.add_parser("audit", help="本地操作审计日志（只含变量名与结果）")
    audit_sub = audit_cmd.add_subparsers(dest="action", metavar="ACTION")
    p = audit_sub.add_parser("list", help="列出最近的操作记录")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--filter", dest="audit_action", default=None, help="按动作过滤，如 env.apply")

    # ---------------------------------------------------------------- library
    library = sub.add_parser("library", help="内置常用环境变量目录")
    library_sub = library.add_subparsers(dest="action", metavar="ACTION")

    p = library_sub.add_parser("list", help="列出内置常用变量（可筛选）")
    p.add_argument("--category", choices=CATEGORIES, help="按分类筛选")
    p.add_argument("--search", default=None, help="按变量名、分类或说明筛选")

    p = library_sub.add_parser("show", help="查看单个变量的详情")
    p.add_argument("key")

    library_sub.add_parser("categories", help="列出分类与数量")

    # ---------------------------------------------------------------- machine
    machine = sub.add_parser("machine", help="开发机画像与已安装开发软件")
    machine_sub = machine.add_subparsers(dest="action", metavar="ACTION")

    p = machine_sub.add_parser("profile", help="生成开发机画像报告")
    p.add_argument("--scope", default="user,system", help="逗号分隔的作用域")
    p.add_argument("--format", choices=["md", "html", "json", "both"], default="both")
    p.add_argument("--output", default=None, help="输出目录；缺省在终端打印 Markdown")
    p.add_argument("--no-software", action="store_true", help="跳过已安装软件扫描")

    p = machine_sub.add_parser("software", help="扫描已安装的开发软件（只读）")
    p.add_argument("--search", default=None, help="按名称筛选")

    # ---------------------------------------------------------------- spec
    spec_cmd = sub.add_parser("spec", help="环境标准 Spec v1：采集、核对、比较与计划")
    spec_sub = spec_cmd.add_subparsers(dest="action", metavar="ACTION")

    p = spec_sub.add_parser("capture", help="从当前机器采集并生成规格文件")
    p.add_argument("--output", default="envforge-spec.toml", help="规格文件路径")
    p.add_argument("--name", default="", help="规格名称；缺省用机器名")
    p.add_argument("--kind", choices=["workstation", "project"], default="workstation")
    p.add_argument("--scope", default="user,system")
    p.add_argument("--no-software", action="store_true", help="不把已安装软件写入规格")

    p = spec_sub.add_parser("show", help="查看规格内容")
    p.add_argument("path")

    p = spec_sub.add_parser("check", help="按规格核对当前机器")
    p.add_argument("path")
    p.add_argument("--commands", action="store_true", help="实际执行规格中的命令（默认不执行）")
    p.add_argument("--probe", action="store_true", help="调用工具自身 --version 探测版本")
    p.add_argument("--ports", action="store_true", help="检查端口是否被占用")
    p.add_argument("--software", action="store_true", help="扫描已安装软件以核对 software 类工具")
    p.add_argument("--report", default=None, help="报告输出目录")
    p.add_argument("--format", choices=["md", "html", "both"], default="both")
    p.add_argument(
        "--fail-on",
        default=None,
        help="出现指定状态时返回非 0（逗号分隔：missing,mismatch,error）",
    )

    p = spec_sub.add_parser("diff", help="比较两份规格")
    p.add_argument("left")
    p.add_argument("right")

    p = spec_sub.add_parser("plan", help="根据规格差异生成安装与配置计划")
    p.add_argument("path")
    p.add_argument("--output", default=None, help="把计划脚本写入文件")
    p.add_argument("--software", action="store_true")

    # ---------------------------------------------------------------- baseline
    baseline = sub.add_parser("baseline", help="环境基线：变更追踪")
    baseline_sub = baseline.add_subparsers(dest="action", metavar="ACTION")

    p = baseline_sub.add_parser("capture", help="创建环境基线")
    p.add_argument("--scope", default="user,system")
    p.add_argument("--label", default="")

    p = baseline_sub.add_parser("list", help="列出基线")
    p.add_argument("--scope", default=None)
    p.add_argument("--limit", type=int, default=50)

    p = baseline_sub.add_parser("diff", help="比较两个基线并推断变更来源")
    p.add_argument("before", type=int)
    p.add_argument("after", type=int)
    p.add_argument("--report", default=None, help="报告输出目录")
    p.add_argument("--format", choices=["md", "html", "both"], default="both")

    # ---------------------------------------------------------------- lab
    lab_cmd = sub.add_parser("lab", help="环境实验室：模拟 PATH 与变量修改的影响")
    lab_sub = lab_cmd.add_subparsers(dest="action", metavar="ACTION")

    p = lab_sub.add_parser("path", help="模拟 PATH 增删后的命令解析变化")
    p.add_argument("--scope", default="user", choices=["user", "system"])
    p.add_argument("--add", action="append", default=[], metavar="DIR")
    p.add_argument("--remove", action="append", default=[], metavar="DIR")
    p.add_argument("--command", action="append", dest="lab_commands", default=[], metavar="CMD")
    p.add_argument("--append", action="store_true", help="把新目录追加到末尾（默认最前）")
    p.add_argument("--measured", action="store_true", help="用 where.exe 实测当前解析结果")

    p = lab_sub.add_parser("cycles", help="检测环境变量的循环引用")
    p.add_argument("--scope", default="user,system")

    p = lab_sub.add_parser("expand", help="展开 %%VAR%% 引用并列出缺失变量")
    p.add_argument("value")
    p.add_argument("--scope", default="user,system")

    p = lab_sub.add_parser("impact", help="分析修改某个变量会影响哪些项目/档案")
    p.add_argument("--key", required=True)
    p.add_argument("--scope", default="user,system")

    # ---------------------------------------------------------------- graph
    graph_cmd = sub.add_parser("graph", help="环境依赖关系图")
    graph_sub = graph_cmd.add_subparsers(dest="action", metavar="ACTION")
    p = graph_sub.add_parser("build", help="生成依赖图（JSON / DOT / Markdown）")
    p.add_argument("--format", choices=["json", "dot", "md"], default="json")
    p.add_argument("--output", default=None)

    # ---------------------------------------------------------------- terminal
    terminal = sub.add_parser("terminal", help="终端配置片段管理")
    terminal_sub = terminal.add_subparsers(dest="action", metavar="ACTION")

    p = terminal_sub.add_parser("list", help="列出终端配置")
    p.add_argument("--shell", choices=SHELLS, default=None)

    p = terminal_sub.add_parser("set", help="保存或更新终端配置")
    p.add_argument("name")
    p.add_argument("--shell", choices=SHELLS, default="powershell")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--content", default=None)
    group.add_argument("--file", default=None, help="从文件读取内容")

    p = terminal_sub.add_parser("show", help="查看终端配置内容")
    p.add_argument("name")
    p.add_argument("--shell", choices=SHELLS, default="powershell")

    p = terminal_sub.add_parser("export", help="导出可直接放入 profile 的脚本")
    p.add_argument("name")
    p.add_argument("--shell", choices=SHELLS, default="powershell")
    p.add_argument("--output", default=None)
    p.add_argument("--yes", action="store_true", help="允许覆盖已存在的输出文件")

    p = terminal_sub.add_parser("remove", help="删除终端配置")
    p.add_argument("name")
    p.add_argument("--shell", choices=SHELLS, default="powershell")
    p.add_argument("--yes", action="store_true")

    # ---------------------------------------------------------------- doctor / gui
    p = sub.add_parser("doctor", help="环境健康检查")
    p.add_argument("--scope", default="user,system", help="逗号分隔的作用域")
    p.add_argument("--fail-on", choices=["error", "warn"], default=None, help="出现该级别问题时返回非 0")

    p = sub.add_parser("gui", help="启动桌面界面")
    p.add_argument("--no-wait", action="store_true", help="仅用于自动化测试")

    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # --json 允许出现在任意位置（包括子命令之后），解析前先统一剥离。
    json_flag = False
    while "--json" in raw:
        raw.remove("--json")
        json_flag = True
    parser = _build_parser()
    args = parser.parse_args(raw)
    if json_flag:
        args.json = True
    if not getattr(args, "command", None):
        if not raw:
            return _launch_gui_or_help()
        parser.print_help()
        return EXIT_OK

    if args.command == "gui":
        return _launch_gui(args.db)

    if args.command == "library":
        # 目录查询不依赖数据库，失败时也只按参数/未找到处理。
        try:
            return _cmd_library(args)
        except ValidationError as exc:
            print(f"输入错误：{exc}", file=sys.stderr)
            return EXIT_USAGE
        except NotFoundError as exc:
            print(f"未找到：{exc}", file=sys.stderr)
            return EXIT_ERROR

    try:
        db = Database(args.db)
    except DatabaseError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        with db:
            _check_database(db)
            return _dispatch(args, db)
    except ConfirmationRequired as exc:
        print(f"需要确认：{exc}", file=sys.stderr)
        return EXIT_CONFIRM
    except ValidationError as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except NotFoundError as exc:
        print(f"未找到：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except EnvWriteFailure as exc:
        print(f"写入失败：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except EnvAccessError as exc:
        print(f"环境访问失败：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except EnvForgeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_ERROR


def _check_database(db: Database) -> None:
    """启动时自检 + 每日备份；失败只告警，不阻断本次命令。"""
    if not db.integrity.get("ok"):
        backups = Path(db.path).parent / "backups"
        print(
            f"警告：数据库完整性检查失败（{db.integrity['detail']}）。"
            f"建议从 {backups} 里的备份恢复，或运行 `envforge env db-backup` 先保住当前文件。",
            file=sys.stderr,
        )
    try:
        db.backup_daily()
    except DatabaseError as exc:
        print(f"警告：每日自动备份失败（{exc}）。", file=sys.stderr)


def _dispatch(args: argparse.Namespace, db: Database) -> int:
    if args.command == "runtime":
        return _cmd_runtime(args, db)
    if args.command == "project":
        return _cmd_project(args, db)
    if args.command == "profile":
        return _cmd_profile(args, db)
    if args.command == "env":
        return _cmd_env(args, db)
    if args.command == "doctor":
        return _cmd_doctor(args, db)
    if args.command == "audit":
        return _cmd_audit(args, db)
    if args.command == "machine":
        return _cmd_machine(args, db)
    if args.command == "spec":
        return _cmd_spec(args, db)
    if args.command == "baseline":
        return _cmd_baseline(args, db)
    if args.command == "lab":
        return _cmd_lab(args, db)
    if args.command == "graph":
        return _cmd_graph(args, db)
    if args.command == "terminal":
        return _cmd_terminal(args, db)
    raise ValidationError(f"未知命令 {args.command!r}")


def _audit_log(db: Database) -> AuditLog:
    return AuditLog.for_database(db.path)


def _cmd_runtime(args: argparse.Namespace, db: Database) -> int:
    service = RuntimeService(db)
    if args.action == "detect":
        kinds = args.kind or None
        result = service.detect_and_register(kinds)
        if args.json:
            _print_json(result)
        else:
            if not result["detected"]:
                print("未在 PATH 中发现可登记的运行时。可用 runtime add --path 手动登记。")
            for item in result["detected"]:
                print(f"[探测] {item['kind']:<7} {item['version'] or '未知':<10} {item['exe_path']}")
            print(f"新增 {result['added']} 条，更新 {result['updated']} 条。")
        return EXIT_OK
    if args.action == "list":
        items = [entry.to_dict() for entry in service.list(args.kind)]
        if args.json:
            _print_json(items)
        elif not items:
            print("暂无登记；先运行 `envforge runtime detect`。")
        else:
            for item in items:
                print(
                    f"#{item['id']:<4} {item['kind']:<7} {item['version'] or '未知':<10} "
                    f"{item['exe_path']}  ({item['source']})"
                )
        return EXIT_OK
    if args.action == "add":
        try:
            entry = service.register(args.kind, args.path, args.version, notes=args.notes)
        except ValidationError:
            raise
        if args.json:
            _print_json(entry.to_dict())
        else:
            print(f"已登记 #{entry.id} {entry.kind} {entry.version or '未知'} -> {entry.exe_path}")
        return EXIT_OK
    if args.action == "remove":
        if not args.yes:
            raise ConfirmationRequired("移除运行时登记会删除对应记录（不会卸载软件）。请加 --yes。")
        service.remove(args.id)
        print(f"已移除运行时记录 #{args.id}。")
        return EXIT_OK
    if args.action == "activate":
        script = service.activation_script()
        if args.json:
            _print_json({"script": script, "entries": [e.to_dict() for e in service.default_entries()]})
        else:
            print(script, end="")
        return EXIT_OK
    raise ValidationError("请指定 runtime 子命令：detect / list / add / remove / activate")


def _cmd_project(args: argparse.Namespace, db: Database) -> int:
    projects = ProjectService(db)
    if args.action == "add":
        entry = projects.add(args.name, args.path)
        if args.json:
            _print_json(entry.to_dict())
        else:
            print(f"已登记项目 {entry.name} -> {entry.path}")
        return EXIT_OK
    if args.action == "list":
        items = [item.to_dict() for item in projects.list()]
        if args.json:
            _print_json(items)
        elif not items:
            print("暂无项目；使用 `envforge project add --name ... --path ...` 登记。")
        else:
            for item in items:
                print(f"{item['name']:<20} {item['path']}")
        return EXIT_OK
    if args.action == "remove":
        if not args.yes:
            raise ConfirmationRequired(
                f"删除项目 {args.name!r} 会一并删除其全部环境档案。请加 --yes。"
            )
        projects.remove(args.name)
        print(f"已删除项目 {args.name} 及其档案。")
        return EXIT_OK
    raise ValidationError("请指定 project 子命令：add / list / remove")


def _cmd_profile(args: argparse.Namespace, db: Database) -> int:
    projects = ProjectService(db)
    profiles = ProfileService(db, projects, audit=_audit_log(db))
    if args.action == "list":
        items = [item.to_dict() for item in profiles.list_profiles(args.project)]
        if args.json:
            _print_json(items)
        elif not items:
            print("该项目还没有档案。")
        else:
            for item in items:
                mark = " *" if item["active"] else ""
                print(f"{item['name']}{mark}  {item['description']}")
        return EXIT_OK
    if args.action == "set":
        key, value = _parse_pair(args.pair)
        sensitive = True if args.secret else (False if args.plain else None)
        var, warning = profiles.set_var(args.project, args.profile, key, value, sensitive)
        if args.json:
            _print_json({"var": var.to_dict(), "warning": warning})
        else:
            flag = "（已加密）" if var.encrypted else ""
            print(f"已设置 {var.key}{flag}")
            if warning:
                print(f"警告：{warning}", file=sys.stderr)
        return EXIT_OK
    if args.action == "unset":
        if not args.yes:
            raise ConfirmationRequired(f"删除变量 {args.key!r} 不可撤销。请加 --yes。")
        profiles.unset_var(args.project, args.profile, args.key)
        print(f"已删除变量 {args.key}。")
        return EXIT_OK
    if args.action == "show":
        items = [item.to_dict() for item in profiles.list_vars(args.project, args.profile, args.reveal)]
        if args.json:
            _print_json(items)
        elif not items:
            print("档案为空。")
        else:
            for item in items:
                marker = " [敏感]" if item["sensitive"] else ""
                stored = "" if item["stored"] else "  <未存储>"
                print(f"{item['key']}={item['value']}{marker}{stored}")
        return EXIT_OK
    if args.action == "export":
        text = profiles.export_dotenv(args.project, args.profile, args.reveal)
        payload = {"project": args.project, "profile": args.profile, "content": text}
        if args.output:
            path = _write_output(text, args.output, allow_overwrite=args.yes)
            if args.json:
                print(f"已导出到 {path}", file=sys.stderr)
                _print_json(payload)
            else:
                print(f"已导出到 {path}")
        elif args.json:
            _print_json(payload)
        else:
            print(text, end="")
        return EXIT_OK
    if args.action == "diff":
        changes = [item.to_dict() for item in profiles.diff(
            args.project, args.profile_a, args.profile_b, args.reveal
        )]
        if args.json:
            _print_json(changes)
        elif not changes:
            print("两个档案没有差异。")
        else:
            for item in changes:
                print(f"{item['key']}: {item['before']} -> {item['after']}")
        return EXIT_OK
    if args.action == "activate":
        env, warnings = profiles.activation(args.project, args.profile, confirm=args.yes)
        script_service = RuntimeService(db)
        script = script_service.activation_script(
            project_path=env.pop("ENVFORGE_PROJECT", None), extra_vars=env
        )
        if args.json:
            _print_json({"script": script, "warnings": warnings})
        else:
            print(script, end="")
            for warning in warnings:
                print(f"警告：{warning}", file=sys.stderr)
        return EXIT_OK
    if args.action == "remove":
        if not args.yes:
            raise ConfirmationRequired(f"删除档案 {args.profile!r} 会丢失其全部变量。请加 --yes。")
        profiles.remove_profile(args.project, args.profile)
        print("已删除档案。")
        return EXIT_OK
    raise ValidationError("请指定 profile 子命令：list / set / unset / show / export / diff / activate / remove")


def _cmd_env(args: argparse.Namespace, db: Database) -> int:
    service = EnvService(db, audit=_audit_log(db))
    if args.action == "show":
        scope = _single_scope(args.scope)
        snapshot = service.current(scope)
        data = snapshot.to_dict(reveal=args.reveal)
        if args.json:
            _print_json(data)
        else:
            for key in sorted(data["values"]):
                print(f"{key}={data['values'][key]}")
        return EXIT_OK
    if args.action == "path":
        scope = _single_scope(args.scope)
        report = service.path_report(scope)
        if args.json:
            _print_json(report)
        else:
            labels = {"ok": "正常", "missing": "不存在", "duplicate": "重复", "empty": "空段"}
            for item in report:
                print(f"{item['index']:>3}  {labels.get(item['status'], item['status']):<4}  {item['raw'] or '<空>'}")
        return EXIT_OK
    if args.action == "dedupe":
        scope = _single_scope(args.scope)
        current = service.current(scope)
        path_value = ""
        for key, value in current.values.items():
            if key.upper() == "PATH":
                path_value = value
                break
        cleaned, removed = dedupe_path(path_value)
        if args.json:
            _print_json({"removed": removed, "path": cleaned, "applied": bool(args.apply and args.yes)})
        else:
            print(f"可移除 {removed} 个空段或重复目录。")
            if removed:
                print(f"去重后：{cleaned}")
        if args.apply:
            if removed == 0:
                if not args.json:
                    print("没有需要写入的变更。")
                return EXIT_OK
            result = service.apply(scope, {"PATH": cleaned}, confirm=args.yes, label="PATH 去重")
            if not args.json:
                print(f"已写入 PATH（已回读校验），回滚快照 #{result['pre_snapshot_id']}。")
        return EXIT_OK
    if args.action == "snapshot":
        scope = _single_scope(args.scope)
        snapshot = service.create(scope, args.label)
        if args.json:
            _print_json(snapshot.to_dict())
        else:
            print(
                f"已创建快照 #{snapshot.id}（{scope_label(snapshot.scope)}，{len(snapshot.values)} 个变量）"
            )
        return EXIT_OK
    if args.action == "snapshots":
        if args.scope:
            _single_scope(args.scope)
        items = [item.to_dict() for item in service.list(args.scope, args.limit)]
        if args.json:
            _print_json(items)
        elif not items:
            print("暂无快照；使用 `envforge env snapshot --scope user` 创建。")
        else:
            for item in items:
                print(
                    f"#{item['id']:<4} {scope_label(item['scope']):<6} {item['created_at']}  "
                    f"{item['label'] or '-'}  ({item['var_count']} 个变量)"
                )
        return EXIT_OK
    if args.action == "diff":
        data = service.diff(args.before, args.after, args.reveal)
        if args.json:
            _print_json(data)
        else:
            for key, value in data["added"].items():
                print(f"+ {key}={value}")
            for key, value in data["removed"].items():
                print(f"- {key}={value}")
            for item in data["changed"]:
                print(f"~ {item['key']}: {item['before']} -> {item['after']}")
            if not (data["added"] or data["removed"] or data["changed"]):
                print("两个快照没有差异。")
        return EXIT_OK
    if args.action == "export":
        data = service.export(args.id, args.reveal)
        if args.output:
            path = _write_output(
                json.dumps(data, ensure_ascii=False, indent=2),
                args.output,
                allow_overwrite=args.yes,
            )
            if args.json:
                print(f"已导出到 {path}", file=sys.stderr)
                _print_json(data)
            else:
                print(f"已导出到 {path}")
        else:
            _print_json(data)
        return EXIT_OK
    if args.action == "restore":
        result = service.restore(
            args.id, confirm=args.yes, delete_extras=args.delete_extras
        )
        if args.json:
            _print_json(result)
        elif not result["changed"] and not result["deleted"]:
            print("当前环境已与快照一致，无需回滚。")
        else:
            print(
                f"已回滚 {len(result['changed'])} 个变量、删除 {len(result['deleted'])} 个；"
                f"回滚前备份快照 #{result['pre_snapshot_id']}。"
            )
        return EXIT_OK
    if args.action == "apply":
        updates: dict[str, str | None] = {}
        for pair in args.set:
            key, value = _parse_pair(pair)
            updates[key] = value
        for key in args.unset:
            updates[key.strip()] = None
        scope = _single_scope(args.scope)
        result = service.apply(scope, updates, confirm=args.yes, label="CLI 写入")
        if args.json:
            _print_json(result)
        else:
            print(
                f"已写入 {len(result['changed'])} 个变量、删除 {len(result['deleted'])} 个；"
                f"回读校验通过，回滚快照 #{result['pre_snapshot_id']}。"
            )
        return EXIT_OK
    if args.action == "db-check":
        report = db.integrity_check()
        _audit_log(db).append(
            "env.db-check", result="ok" if report["ok"] else "failed", detail=report["detail"]
        )
        if args.json:
            _print_json(report)
        elif report["ok"]:
            print(f"数据库完整性检查通过：{db.path}")
        else:
            print(
                f"数据库完整性检查失败：{report['detail']}。"
                f"请从 {Path(db.path).parent / 'backups'} 恢复备份，或先运行 `envforge env db-backup` 保住当前文件。",
                file=sys.stderr,
            )
            return EXIT_ERROR
        return EXIT_OK
    if args.action == "db-backup":
        folder = Path(db.path).parent / "backups"
        target = db.backup_to(folder / f"envforge-manual-{datetime.now():%Y%m%d-%H%M%S}.db")
        _audit_log(db).append("env.db-backup", result="ok", detail=str(target))
        if args.json:
            _print_json({"path": str(target)})
        else:
            print(f"已备份到 {target}")
        return EXIT_OK
    raise ValidationError(
        "请指定 env 子命令：show / path / dedupe / snapshot / snapshots / diff / export / restore /"
        " apply / db-check / db-backup"
    )


def _cmd_library(args: argparse.Namespace) -> int:
    action = args.action
    if action == "categories":
        counts = count_by_category()
        if args.json:
            _print_json({"total": len(all_presets()), "categories": counts})
        else:
            print(f"内置常用环境变量：{len(all_presets())} 个")
            for name in CATEGORIES:
                print(f"  {name}：{counts.get(name, 0)} 个")
        return EXIT_OK
    if action == "show":
        preset = get_preset(args.key)
        if preset is None:
            raise NotFoundError(
                f"目录中没有 {args.key}；可用 `envforge library list --search {args.key}` 查找。"
            )
        if args.json:
            _print_json(preset.to_dict())
        else:
            print(f"{preset.key}  [{preset.category}]")
            print(f"  说明：{preset.description}")
            print(f"  示例：{preset.example}")
            print(f"  敏感：{'是（按密钥处理）' if preset.sensitive else '否'}")
            if preset.note:
                print(f"  注意：{preset.note}")
        return EXIT_OK
    # 默认（含 action == "list"）列出目录
    items = search_presets(category=getattr(args, "category", None), keyword=getattr(args, "search", None))
    if args.json:
        _print_json(
            {"total": len(items), "catalog_total": len(all_presets()), "items": [i.to_dict() for i in items]}
        )
        return EXIT_OK
    if not items:
        print("没有匹配的变量；换个关键词或分类试试。")
    for item in items:
        flag = "  [敏感]" if item.sensitive else ""
        print(f"{item.key}{flag}  [{item.category}]")
        print(f"    示例：{item.example}")
        print(f"    {item.description}")
    print(f"共 {len(items)} 个（目录总计 {len(all_presets())} 个）")
    return EXIT_OK


def _cmd_audit(args: argparse.Namespace, db: Database) -> int:
    if args.action != "list":
        raise ValidationError("请指定 audit 子命令：list")
    log = _audit_log(db)
    entries = log.recent(args.limit, args.audit_action)
    if args.json:
        _print_json(entries)
    else:
        print(log.format_lines(entries))
    return EXIT_OK


def _cmd_doctor(args: argparse.Namespace, db: Database) -> int:
    scopes = _split_scope(args.scope, allow_all=True)
    findings = Doctor(db, EnvService(db)).run(scopes)
    counts = summarize(findings)
    if args.json:
        _print_json({"findings": [item.to_dict() for item in findings], "summary": counts})
    else:
        labels = {"error": "错误", "warn": "警告", "info": "提示"}
        if not findings:
            print("未发现问题。")
        for item in findings:
            print(f"[{labels.get(item.severity, item.severity)}] {item.title}")
            print(f"    详情：{item.detail}")
            if item.suggestion:
                print(f"    建议：{item.suggestion}")
        print(f"合计：错误 {counts['error']}，警告 {counts['warn']}，提示 {counts['info']}")
    if args.fail_on == "error" and counts["error"]:
        return EXIT_ERROR
    if args.fail_on == "warn" and (counts["error"] or counts["warn"]):
        return EXIT_ERROR
    return EXIT_OK


def _vkey(version: str) -> tuple[int, ...]:
    return version_key(version)


def _env_lookup(service: EnvService, scopes: list[str]) -> tuple[dict[tuple[str, str], str], list[str]]:
    mapping: dict[tuple[str, str], str] = {}
    warnings: list[str] = []
    for scope in scopes:
        try:
            snapshot = service.current(scope)
        except EnvAccessError as exc:
            warnings.append(f"无法读取 {scope}：{exc}")
            continue
        for key, value in snapshot.values.items():
            mapping[(scope, key.upper())] = value
    return mapping, warnings


def _merged_env(values: dict[tuple[str, str], str]) -> dict[str, str]:
    """按 Windows 语义合并作用域变量：system 打底、user 覆盖；PATH 单独拼接。"""
    merged: dict[str, str] = {}
    for scope in ("system", "user"):
        for (item_scope, key), value in values.items():
            if item_scope == scope and key != "PATH":
                merged[key] = value
    path = lab_module.combined_path(
        values.get(("system", "PATH")) or "", values.get(("user", "PATH")) or ""
    )
    if path:
        merged["PATH"] = path
    return merged


def _write_output(
    text: str, target: str | Path, *, allow_overwrite: bool = True
) -> Path:
    """写入文本文件：目录目标或未确认覆盖时拒绝，OSError 统一转为 ValidationError。"""
    path = Path(target)
    if path.exists() and path.is_dir():
        raise ValidationError(f"输出路径 {path} 是目录，请指定文件路径。")
    if path.exists() and not allow_overwrite:
        raise ConfirmationRequired(f"导出目标 {path} 已存在，继续会覆盖该文件。请加 --yes。")
    try:
        return report_module.write_text(text, path)
    except OSError as exc:
        raise ValidationError(f"写入 {path} 失败：{exc}") from exc


def _combined_path(db: Database, scope: str = "user") -> str:
    service = EnvService(db)
    system = ""
    user = ""
    try:
        system = service.current("system").values.get("PATH", "")
    except EnvAccessError:
        system = ""
    try:
        user = service.current("user").values.get("PATH", "")
    except EnvAccessError:
        user = ""
    if scope == "system":
        return system
    return lab_module.combined_path(system, user)


def _port_available(port: int) -> bool:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _run_spec_command(command: str) -> tuple[int, str]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=15,
            errors="replace",
            creationflags=creationflags,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _check_context(args: argparse.Namespace, db: Database, spec: Any) -> tuple[CheckContext, list[str]]:
    service = EnvService(db)
    scopes = sorted({var.scope for var in spec.env}) or ["user"]
    env_map, warnings = _env_lookup(service, scopes)
    known: dict[str, str] = {}
    runtime_service = RuntimeService(db)
    for entry in runtime_service.list():
        if not entry.version:
            continue
        current = known.get(entry.kind)
        if current is None or _vkey(entry.version) > _vkey(current):
            known[entry.kind] = entry.version
    software_checked = False
    if getattr(args, "software", False):
        for item in scan_software():
            known.setdefault(normalize_software_id(item["name"]), item["version"])
        software_checked = True
    context = CheckContext(
        env=env_map,
        which=shutil.which,
        exists=os.path.isdir,
        known_tools=known,
        software_checked=software_checked,
        run=_run_spec_command if getattr(args, "commands", False) else None,
        port_checker=_port_available if getattr(args, "ports", False) else None,
    )
    if getattr(args, "probe", False):
        context.probe = runtime_service.probe
    return context, warnings


def _write_report_files(
    target: str | None, stem: str, *, markdown: str = "", html: str = "", fmt: str = "both"
) -> list[str]:
    if not target:
        return []
    path = Path(target)
    written: list[str] = []
    suffix = path.suffix.lower()
    if suffix == ".md":
        written.append(str(_write_output(markdown, path)))
    elif suffix == ".html":
        written.append(str(_write_output(html, path)))
    else:
        if path.exists() and path.is_file():
            raise ValidationError(
                f"报告路径 {path} 已存在且不是目录；请使用 .md/.html 后缀或指定其他目录。"
            )
        path.mkdir(parents=True, exist_ok=True)
        if fmt in ("md", "both") and markdown:
            written.append(str(_write_output(markdown, path / f"{stem}.md")))
        if fmt in ("html", "both") and html:
            written.append(str(_write_output(html, path / f"{stem}.html")))
    return written


def _cmd_machine(args: argparse.Namespace, db: Database) -> int:
    if args.action == "software":
        items = scan_software()
        if args.search:
            text = args.search.lower()
            items = [
                item
                for item in items
                if text in item["name"].lower() or text in item["version"].lower()
            ]
        if args.json:
            _print_json({"total": len(items), "items": items})
        elif not items:
            print("未扫描到开发软件（仅支持 Windows 注册表 Uninstall 键）。")
        else:
            for item in items:
                print(f"{item['name']:<40} {item['version'] or '未知':<15} {item['publisher']}")
            print(f"共 {len(items)} 项。")
        return EXIT_OK
    if args.action != "profile":
        raise ValidationError("请指定 machine 子命令：profile / software")

    scopes = [item.strip() for item in args.scope.split(",") if item.strip()]
    for scope in scopes:
        if scope not in SCOPES:
            raise ValidationError(f"未知作用域 {scope!r}；可选：{', '.join(SCOPES)}")
    payload = capture_payload(
        db=db,
        scopes=scopes,
        include_software=not args.no_software,
        include_projects=True,
    )
    markdown = report_module.render_profile_markdown(payload)
    html = report_module.render_profile_html(payload)
    if args.json or args.format == "json":
        if args.output:
            target = Path(args.output)
            if target.exists() and target.is_file():
                raise ValidationError(
                    f"输出路径 {target} 已存在且不是目录；请指定目录或其他路径。"
                )
            target.mkdir(parents=True, exist_ok=True)
            path = target / f"machine-profile-{datetime.now():%Y%m%d-%H%M%S}.json"
            _write_output(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str), path
            )
            _print_json(payload)
            print(f"已写入 {path}", file=sys.stderr)
        else:
            _print_json(payload)
        return EXIT_OK
    if args.output:
        written = _write_report_files(
            args.output, f"machine-profile-{datetime.now():%Y%m%d-%H%M%S}",
            markdown=markdown, html=html, fmt=args.format,
        )
        for item in written:
            print(f"已写入 {item}")
    else:
        print(markdown, end="")
    for warning in payload.get("warnings") or []:
        print(f"警告：{warning}", file=sys.stderr)
    return EXIT_OK


def _cmd_spec(args: argparse.Namespace, db: Database) -> int:
    if args.action == "capture":
        scopes = [item.strip() for item in args.scope.split(",") if item.strip()]
        payload = capture_payload(
            db=db,
            scopes=scopes,
            include_software=not args.no_software,
            include_projects=False,
        )
        spec = capture_from_payload(payload, name=args.name, kind=args.kind)
        target = save_spec(spec, args.output)
        if args.json:
            _print_json(spec.to_dict())
        else:
            counts = spec.counts()
            print(f"已生成规格 {target}")
            print(
                f"名称：{spec.name}（{spec.kind}）；工具 {counts['tools']}，"
                f"变量 {counts['env']}，目录 {counts['paths']}，命令 {counts['commands']}"
            )
            if payload.get("warnings"):
                for warning in payload["warnings"]:
                    print(f"警告：{warning}", file=sys.stderr)
        return EXIT_OK
    if args.action == "show":
        spec = load_spec(args.path)
        if args.json:
            _print_json(spec.to_dict())
        else:
            print(dump_spec(spec), end="")
        return EXIT_OK
    if args.action == "check":
        spec = load_spec(args.path)
        fail_on = {
            part.strip() for part in (getattr(args, "fail_on", None) or "").split(",") if part.strip()
        }
        unknown = fail_on - {"missing", "mismatch", "error"}
        if unknown:
            raise ValidationError(
                f"--fail-on 不支持的状态 {', '.join(sorted(unknown))}；可选：missing, mismatch, error"
            )
        context, warnings = _check_context(args, db, spec)
        result = check_spec(
            spec,
            context,
            machine=current_machine(),
            run_commands=args.commands,
            check_ports=args.ports,
        )
        result["warnings"] = warnings
        if args.json:
            _print_json(result)
        else:
            score = result["score"]
            summary = result["summary"]
            print(
                f"规格：{spec.name}　健康评分：{score['score']}/100（{score['grade']}）　"
                f"通过 {summary['ok']}，缺失 {summary['missing']}，不符 {summary['mismatch']}，"
                f"未知 {summary['unknown']}，跳过 {summary['skipped']}"
            )
            for item in result["items"]:
                if item["status"] == "ok":
                    continue
                print(
                    f"[{item['status_label']}] {item['section_label']} {item['target']}："
                    f"{item['detail']}"
                )
            for warning in warnings:
                print(f"警告：{warning}", file=sys.stderr)
        written = _write_report_files(
            args.report,
            f"spec-check-{datetime.now():%Y%m%d-%H%M%S}",
            markdown=report_module.render_check_markdown(result),
            html=report_module.render_check_html(result),
            fmt=args.format,
        )
        for item in written:
            print(f"已写入 {item}", file=sys.stderr if args.json else sys.stdout)
        if fail_on and any(item["status"] in fail_on for item in result["items"]):
            return EXIT_ERROR
        return EXIT_OK
    if args.action == "diff":
        left = load_spec(args.left)
        right = load_spec(args.right)
        diff = diff_specs(left, right)
        if args.json:
            _print_json(diff)
        else:
            total = diff["summary"].pop("total", 0)
            for section, data in diff["sections"].items():
                if data["added"] or data["removed"] or data["changed"]:
                    print(f"## {section}")
                    for item in data["added"]:
                        print(f"+ {item}")
                    for item in data["removed"]:
                        print(f"- {item}")
                    for item in data["changed"]:
                        print(f"~ {item['before']} -> {item['after']}")
            if total == 0:
                print("两份规格没有差异。")
            else:
                print(f"合计 {total} 处差异。")
        return EXIT_OK
    if args.action == "plan":
        spec = load_spec(args.path)
        context, _warnings = _check_context(args, db, spec)
        result = check_spec(
            spec, context, machine={"os": "windows" if sys.platform == "win32" else sys.platform}
        )
        plan = build_plan(spec, result)
        if args.output:
            path = _write_output(plan["script"], args.output)
            print(
                f"已写入计划脚本 {path}",
                file=sys.stderr if args.json else sys.stdout,
            )
            if args.json:
                _print_json(plan)
        elif args.json:
            _print_json(plan)
        else:
            print(plan["script"], end="")
        return EXIT_OK
    raise ValidationError("请指定 spec 子命令：capture / show / check / diff / plan")


def _cmd_baseline(args: argparse.Namespace, db: Database) -> int:
    service = BaselineService(db, audit=_audit_log(db))
    if args.action == "capture":
        baseline = service.capture(scope=args.scope, label=args.label)
        if args.json:
            _print_json(baseline.to_dict())
        else:
            counts = baseline.to_dict()["counts"]
            print(
                f"已创建基线 #{baseline.id}（{baseline.created_at}）："
                f"变量 {counts['env']}，运行时 {counts['runtimes']}，软件 {counts['software']}"
            )
        return EXIT_OK
    if args.action == "list":
        items = [item.to_dict() for item in service.list(args.scope, args.limit)]
        if args.json:
            _print_json(items)
        elif not items:
            print("暂无基线；使用 `envforge baseline capture` 创建。")
        else:
            for item in items:
                counts = item["counts"]
                print(
                    f"#{item['id']:<4} {item['created_at']}  {item['label'] or '-':<20} "
                    f"{item['scope']:<12} 变量 {counts['env']} / 运行时 {counts['runtimes']} / 软件 {counts['software']}"
                )
        return EXIT_OK
    if args.action == "diff":
        diff = service.diff(args.before, args.after)
        if args.json:
            _print_json(diff)
        else:
            summary = diff["summary"]
            print(
                f"#{args.before} → #{args.after}：新增 {summary['added']}，移除 {summary['removed']}，"
                f"修改 {summary['changed']}（已知 {summary['by_source']['envforge']}，"
                f"推测 {summary['by_source']['suspected']}，未知 {summary['by_source']['unknown']}）"
            )
            for change in diff["changes"]:
                print(
                    f"[{change['source']}] {change['section']} {change['key']}："
                    f"{change['change']} {change['before']} -> {change['after']}"
                )
            for note in diff["notes"]:
                print(f"说明：{note}")
        written = _write_report_files(
            args.report,
            f"baseline-diff-{args.before}-{args.after}",
            markdown=report_module.render_diff_markdown(diff),
            html=report_module.render_diff_html(diff),
            fmt=args.format,
        )
        for item in written:
            print(f"已写入 {item}")
        return EXIT_OK
    raise ValidationError("请指定 baseline 子命令：capture / list / diff")


def _cmd_lab(args: argparse.Namespace, db: Database) -> int:
    service = EnvService(db)
    scopes = [item.strip() for item in args.scope.split(",") if item.strip()]
    for scope in scopes:
        if scope not in SCOPES:
            raise ValidationError(f"未知作用域 {scope!r}；可选：{', '.join(SCOPES)}")
    if args.action == "path":
        base = _combined_path(db, args.scope)
        env_values, warnings = _env_lookup(service, ["user", "system"])
        env_upper = _merged_env(env_values)
        result = lab_module.simulate_path_change(
            base_path=base,
            add=args.add,
            remove=args.remove,
            commands=args.lab_commands,
            env=env_upper,
            measured=shutil.which if args.measured else None,
            position="append" if args.append else "prepend",
        )
        result["warnings"] = warnings + result["warnings"]
        if args.json:
            _print_json(result)
        else:
            print(report_module.render_lab_markdown(result), end="")
        return EXIT_OK
    if args.action == "cycles":
        values, warnings = _env_lookup(service, scopes)
        env_upper = _merged_env(values)
        cycles = lab_module.find_cycles(env_upper)
        if args.json:
            _print_json({"cycles": cycles, "warnings": warnings})
        elif not cycles:
            print("未发现循环引用。")
        else:
            for cycle in cycles:
                print("循环：" + " → ".join(cycle) + f" → {cycle[0]}")
            print(f"共 {len(cycles)} 组循环引用。")
        for warning in warnings:
            print(f"警告：{warning}", file=sys.stderr)
        return EXIT_OK
    if args.action == "expand":
        values, warnings = _env_lookup(service, scopes)
        env_upper = _merged_env(values)
        result = lab_module.expand_vars(args.value, env_upper)
        if args.json:
            _print_json(result)
        else:
            print(f"展开结果：{result['value']}")
            print(f"缺失变量：{', '.join(result['missing']) or '无'}")
            print(f"循环引用：{'是' if result['cycle'] else '否'}")
        for warning in warnings:
            print(f"警告：{warning}", file=sys.stderr)
        return EXIT_OK
    if args.action == "impact":
        values, warnings = _env_lookup(service, scopes)
        env_upper = _merged_env(values)
        result = lab_module.impact(db, args.key, scopes=env_upper)
        if args.json:
            _print_json(result)
        else:
            print(f"变量 {result['key']} 的影响范围：")
            for project in result["projects"]:
                print(f"  项目 {project['project']} → 档案 {', '.join(project['profiles'])}")
            if not result["projects"]:
                print("  没有档案引用该变量。")
            print(f"  被其他变量引用：{', '.join(result['referenced_by']) or '无'}")
        for warning in warnings:
            print(f"警告：{warning}", file=sys.stderr)
        return EXIT_OK
    raise ValidationError("请指定 lab 子命令：path / cycles / expand / impact")


def _cmd_graph(args: argparse.Namespace, db: Database) -> int:
    if args.action != "build":
        raise ValidationError("请指定 graph 子命令：build")
    service = EnvService(db)
    values, _warnings = _env_lookup(service, ["user", "system"])
    env_map = _merged_env(values)
    graph = graph_module.build_graph(
        db,
        env_values=env_map,
        path_value=_combined_path(db, "user"),
    )
    if args.format == "dot":
        text = graph_module.to_dot(graph)
    elif args.format == "md":
        text = report_module.render_graph_markdown(graph)
    else:
        text = json.dumps(graph, ensure_ascii=False, indent=2, default=str)
    if args.output:
        path = _write_output(text, args.output)
        print(
            f"已写入 {path}",
            file=sys.stderr if args.json else sys.stdout,
        )
    elif args.json or args.format == "json":
        print(text)
    else:
        print(text, end="")
    if graph["issues"] and not args.json:
        print(f"发现 {len(graph['issues'])} 个依赖问题，详见输出。", file=sys.stderr)
    return EXIT_OK


def _cmd_terminal(args: argparse.Namespace, db: Database) -> int:
    service = TerminalService(db)
    if args.action == "list":
        items = [item.to_dict() for item in service.list(args.shell)]
        if args.json:
            _print_json(items)
        elif not items:
            print("暂无终端配置；使用 `envforge terminal set <名称> --content ...` 创建。")
        else:
            for item in items:
                print(f"{item['name']:<20} {item['shell_label']:<12} {item['lines']} 行  {item['updated_at']}")
        return EXIT_OK
    if args.action == "set":
        content = args.content or ""
        if args.file:
            path = Path(args.file)
            if not path.exists():
                raise ValidationError(f"内容文件不存在：{path}")
            try:
                content = _read_text_file(path)
            except OSError as exc:
                raise ValidationError(f"读取内容文件 {path} 失败：{exc}") from exc
        config = service.upsert(args.name, args.shell, content)
        if args.json:
            _print_json(config.to_dict(include_content=True))
        else:
            print(f"已保存终端配置 {config.name}（{config.shell}），{len(config.content.splitlines())} 行。")
        return EXIT_OK
    if args.action == "show":
        config = service.get(args.name, args.shell)
        if args.json:
            _print_json(config.to_dict(include_content=True))
        else:
            print(config.content)
        return EXIT_OK
    if args.action == "export":
        text = service.export(args.name, args.shell)
        payload = {"name": args.name, "shell": args.shell, "script": text}
        if args.output:
            path = _write_output(text, args.output, allow_overwrite=args.yes)
            if args.json:
                print(f"已导出到 {path}", file=sys.stderr)
                _print_json(payload)
            else:
                print(f"已导出到 {path}")
        elif args.json:
            _print_json(payload)
        else:
            print(text, end="")
        return EXIT_OK
    if args.action == "remove":
        if not args.yes:
            raise ConfirmationRequired(f"删除终端配置 {args.name!r} 不可撤销。请加 --yes。")
        service.remove(args.name, args.shell)
        print(f"已删除终端配置 {args.name}（{args.shell}）。")
        return EXIT_OK
    raise ValidationError("请指定 terminal 子命令：list / set / show / export / remove")


def _launch_gui(db_path: str | None = None) -> int:
    try:
        from .ui.qt_app import launch
    except ImportError as exc:
        print(
            f"无法启动桌面界面：{exc}。请先安装 PySide6（pip install PySide6），"
            "或改用命令行：envforge --help。",
            file=sys.stderr,
        )
        return EXIT_ERROR
    return launch(db_path=db_path)


def _launch_gui_or_help() -> int:
    try:
        from .ui.qt_app import launch
    except ImportError:
        _build_parser().print_help()
        return EXIT_OK
    return launch()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
