# EnvForge 测试报告

## 1. 环境

| 项目 | 值 |
| --- | --- |
| 操作系统 | Windows 11 x64 |
| Python | 3.12.3 |
| pytest | 9.0.3 |
| coverage | 7.14.1 |
| PySide6 | 6.12.0（测试使用 `QT_QPA_PLATFORM=offscreen`） |
| PyInstaller | 6.20.0 |
| Inno Setup | 6.7.3 |

测试命令：

```powershell
python -m pytest -q --cov=envforge --cov-report=term-missing
```

## 2. 结果汇总

```text
272 passed in 12.06s
```

| 测试文件 | 用例数 | 覆盖内容 |
| --- | ---: | --- |
| tests/test_core.py | 15 | 数据模型、版本解析（含预发布后缀）、敏感键识别、数据库与 schema v2 迁移、完整性自检、备份与保留期 |
| tests/test_profiles.py | 19 | 项目登记、档案 CRUD、敏感值加密、激活、差异（含敏感值轮换）、导出（含 `#` 引号） |
| tests/test_runtimes.py | 13 | 探测去重、存根跳过、登记校验、默认版本、激活脚本（含不安全变量名转义） |
| tests/test_env.py | 33 | PATH 分析（含引号段）、作用域 Provider、快照、差异、写入回读校验/自动回滚、并发写锁、审计脱敏、回滚大小写不敏感 |
| tests/test_cli.py | 25 | 原有子命令输出、`--json` 位置无关、退出码、确认拦截、变量库查询 |
| tests/test_gui.py | 15 | 窗口与六个 Tab、环境标准页核对与渲染、最高版本选取、关闭拦截、变量库筛选、预设对话框、脱敏展示 |
| tests/test_library.py | 7 | 目录数量 ≥100、键名唯一合法、分类非空、搜索、敏感标记一致性 |
| tests/test_spec.py | 39 | 版本约束、TOML 往返、敏感值不落盘（含 URL 凭据）、解析校验、capture、check、评分、diff、plan（含 session 作用域）、中文软件 id |
| tests/test_collectors.py | 14 | 环境变量脱敏（含 URL 凭据）、PATH 拆分、运行时、软件筛选、终端、capture_payload |
| tests/test_baseline.py | 13 | 基线创建/列表、指纹、env/path/敏感值差异（含新增/删除掩码）、审计 known、作用域归因、快照变量展开、suspected、runtime 版本变化 |
| tests/test_lab.py | 25 | `%VAR%` 展开、多环检测与规模上限、PATHEXT、PATH 增删模拟（含变量展开与目录存在性）、实测对照、影响分析 |
| tests/test_graph.py | 7 | 节点/边、问题清单、PATH 变量展开、运行时解析对照、DOT 导出与转义 |
| tests/test_report_terminal.py | 15 | 五类报告渲染、HTML 自包含与转义、检查报告读取警告、终端片段 CRUD/导出/校验 |
| tests/test_cli_extensions.py | 32 | spec/baseline/lab/graph/terminal/machine 子命令端到端、导出覆盖确认、`--json` 纯净化、多作用域拒绝、UTF-16 片段文件、`gui --db` 传递 |

所有测试使用内存或临时目录数据库；服务层测试的注册表操作通过 `MemoryProvider`
注入，不读写真实 `HKCU`/`HKLM`。`test_cli_extensions.py` 中的 `machine`、`baseline
capture`、`spec capture` 会以只读方式读取当前机器的真实环境与注册表，用于验证
采集链路，但不做任何写入。

## 3. 覆盖率

| 模块 | 语句 | 未覆盖 | 覆盖率 |
| --- | --- | --- | --- |
| `errors.py` | 14 | 0 | 100% |
| `report.py` | 154 | 0 | 100% |
| `terminal.py` | 81 | 1 | 99% |
| `models.py` | 106 | 2 | 98% |
| `doctor.py` | 116 | 3 | 97% |
| `graph.py` | 123 | 4 | 97% |
| `lab.py` | 161 | 7 | 96% |
| `envstore.py` | 197 | 11 | 94% |
| `runtimes.py` | 155 | 10 | 94% |
| `baseline.py` | 207 | 14 | 93% |
| `profiles.py` | 249 | 19 | 92% |
| `collectors.py` | 137 | 11 | 92% |
| `spec.py` | 553 | 52 | 91% |
| `secrets.py` | 93 | 8 | 91% |
| `library.py` | 81 | 8 | 90% |
| `lock.py` | 65 | 10 | 85% |
| `db.py` | 161 | 34 | 79% |
| `envvars.py` | 153 | 39 | 75% |
| `cli.py` | 1119 | 295 | 74% |
| `audit.py` | 130 | 51 | 61% |
| `ui/qt_app.py` | 1252 | 601 | 52% |
| **合计** | **5313** | **1182** | **78%** |

核心引擎（errors / models / db / secrets / profiles / runtimes / envvars /
envstore / doctor / library / spec / collectors / baseline / lab / graph /
report / terminal / lock / audit）整体覆盖率约 **90%**，高于 80% 的验收门槛。
`audit.py` 未覆盖部分集中在保留期清理与崩溃钩子（需要真实崩溃/跨天场景）；
`cli.py` 未覆盖部分主要是各子命令的错误分支与格式化输出；`ui/qt_app.py` 的交互
分支依赖人工点击，自动化只覆盖窗口构建、变量库筛选与展示逻辑。

## 4. 真实环境验证

在开发机上用真实数据（非 mock）完成了以下验证：

1. **运行时探测**：识别 Python 3.12.3、Node 24.15.0、JDK 21.0.12，路径去重正确；
   `WindowsApps\python3.exe` 存根因探测不出版本被正确跳过（有回归用例
   `test_detect_skips_unprobeable_stubs`）。
2. **DPAPI**：`--secret` 写入后直接查询数据库，密文中不含明文；`profile show`
   显示 `***`；`--reveal` 才显示明文。
3. **PATH 诊断**：真实 PATH 上检出重复目录与失效目录，`env path` 的分级统计与
   人工核对一致。
4. **快照与回滚**：对 `session` 作用域执行 apply → diff，行为符合预期
   （session 写入不跨进程，CLI 每次调用是新进程，这是设计语义而非缺陷）。
5. **变量库**：`library categories` 输出 151 个变量、10 个分类；`library show
   DATABASE_URL` 正确标记为敏感；GUI 变量库页在 1120×720 窗口下列宽完整、无横向
   滚动条，分类筛选与关键词搜索均生效。
6. **打包产物**：安装器静默安装、启动（主窗口标题 `EnvForge 环境工坊 v0.1.0`）、
   静默卸载均通过；卸载后安装目录与卸载项已清除。
7. **Spec 采集与核对**：用真实环境执行 `spec capture` 生成 TOML，敏感变量只出现
   键名；`spec check` 在默认模式下不执行命令、不探测端口，报告中的 `skipped`
   项与显式开关一一对应。
8. **基线与来源分类**：连续创建两个基线后 `baseline diff` 为 0 处变更；对同一
   `user` 作用域变量做一次 EnvForge 写入后再比较，变更被标记为 `envforge`（已知），
   外部手工修改的目录则落在 `suspected` 或 `unknown`。
9. **环境实验室**：用真实 PATH 运行 `lab path --command python`，静态解析结果与
   `--measured` 的实测结果一致；循环引用检测对人工构造的 `A=%B%` / `B=%A%` 组合
   返回 1 组循环。
10. **依赖图与报告**：`graph build --format dot` 生成可被 Graphviz 解析的 DOT；
    检查报告 HTML 单文件在离线浏览器中正常显示，无外部请求。
11. **GUI 环境标准页**：手工打开「环境标准」标签，选择 TOML 后点击检查，结果表
    与评分摘要正确渲染；「采集本机」可在后台线程完成并自动触发核对；「导出报告」
    生成 Markdown 与自包含 HTML 两份文件。

## 5. 已知缺口

- GUI 的按钮回调、后台线程信号链没有逐条自动化，靠人工在打包产物上走查。
- 注册表广播 `WM_SETTINGCHANGE` 的失败分支未覆盖（需要构造异常环境）。
- 未在 Windows 10 上实测，仅在 Windows 11 x64 验证。
- 崩溃日志钩子只做了手工验证，未纳入自动化（需要真实未捕获异常）。
- 跨进程锁的自动化用例是「同进程双句柄 + 进程内锁」；真实双进程竞争用临时脚本
  验证过：子进程 A 持锁 6 秒，子进程 B 以 `timeout=2` 获取，2 秒后得到
  `另一个 EnvForge 窗口或命令行进程正在写入环境变量…请关闭其他窗口后重试`，
  未出现互斥失效。该验证是手工执行的一次性检查，未纳入自动化回归。
- Spec 的 `--commands` / `--ports` 真实执行分支只做了注入式单测，未在回归中默认
  运行（避免副作用）；需要人工在受控环境验证。
- 环境实验室是静态推断，与真实 shell 的 App Paths / 别名 / 函数解析存在差异；
  `--measured` 只能对照当前进程的解析结果，不覆盖所有 shell 场景。
- 基线变更来源依赖审计日志与文件时间，`suspected` / `unknown` 仅作提示，不能作为
  责任归属证据。
