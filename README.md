# EnvForge 环境工坊

EnvForge 是面向 Windows 开发者的本地环境配置与版本管理工具。它把「运行时多版本
登记与切换」「项目环境配置档案」「系统环境变量与 PATH」「环境健康检查」集中到一个
桌面界面和命令行里，所有数据只保存在本机。

核心逻辑只依赖 Python 标准库；桌面界面使用可选的 PySide6。PySide6 不可用时
`python -m envforge` 会回退到 CLI 帮助，不会崩溃。

## 功能

- **运行时版本**：自动探测 PATH 中的 Python / Node.js / Java / Go / Rust 等运行时，
  记录版本与路径，支持手动登记（例如未加入 PATH 的安装目录）。生成 PowerShell
  激活脚本，只对当前会话前置 PATH，不改系统变量。
- **项目与档案**：登记项目目录，为同一项目维护 `dev` / `test` / `prod` 等多个环境
  档案。变量按项目隔离，支持差异比较、`.env` 导出（默认脱敏）、会话激活。
- **敏感值保护**：键名命中 `SECRET|TOKEN|PASSWORD|API_KEY|PRIVATE_KEY|CREDENTIAL`
  等模式时，值经 Windows DPAPI（`CryptProtectData`）加密后入库；DPAPI 不可用时
  拒绝明文落库，只记录键名并给出警告。列表、导出、差异一律显示 `***`，需要明文
  必须显式 `--reveal`。
- **系统环境变量**：读取 `user`（HKCU）、`system`（HKLM，只读）、`session` 三种
  作用域；PATH 分析会标出重复段、失效目录和空段；写入前自动创建回滚快照，写入后
  广播 `WM_SETTINGCHANGE`。
- **写入可验证**：所有写入都在跨进程文件锁内执行，写完回读逐键比对；失败或值被
  改写时自动用写入前快照回滚，并明确报告失败键与「是否已回滚」，不会留下半改状态。
- **快照与回滚**：任意时刻保存环境快照，比较两个快照的差异，一键回滚（可选择同时
  删除快照中不存在的变量）。每次写入前都会先自动保存 `pre-apply` 快照。
- **审计与数据保护**：操作记录写入按天的 JSONL（只记变量名与结果，不记值），
  GUI「操作历史」与 `envforge audit list` 可查；打开数据库时做完整性自检，每天自动
  备份到 `backups/`（保留 7 份），崩溃时写 `logs/crash-*.log`。
- **常用变量库**：内置 151 个常用环境变量（10 个分类：运行时与工具链、路径与缓存、
  代理与网络、构建与编译、调试与日志、终端与本地化、容器与云、数据库、应用与密钥、
  Windows 系统），附中文说明、示例与敏感标记；支持搜索、按分类筛选、复制示例，或
  一键加入某个项目档案（敏感变量强制加密）。
- **健康体检**：汇总 PATH 问题、多版本并存、PATH 中的运行时未登记、敏感值未加密、
  项目目录失效等发现，按 error / warn / info 分级。
- **环境标准 Spec v1**：用 TOML 声明一台合格开发机应有的工具版本、环境变量、
  必需目录、命令与端口；`spec capture/check/diff/plan` 支持采集、核对、比较与
  生成安装计划。核对默认不执行命令、不探测端口，计划只生成脚本，不自动执行；
  GUI「环境标准」页同样支持选择/采集规格、核对与导出报告。
- **开发机画像与资产清单**：只读采集机器信息、开发相关环境变量、已登记运行时、
  已安装开发软件与终端，输出 Markdown / 自包含 HTML 报告。
- **环境变更追踪**：创建环境基线并比较两次基线；变更来源分为「已知（EnvForge
  审计）」「推测（目录时间等线索）」「未知」，不把推测当成事实。
- **环境实验室**：静态模拟 PATH 增删后的命令解析变化（系统 PATH 在前、用户 PATH
  在后、PATHEXT），检测变量循环引用与 `%VAR%` 缺失，并分析变量修改会影响哪些
  项目/档案。
- **依赖关系图**：项目 → 档案 → 变量 → 运行时 → 可执行文件，导出 JSON / DOT /
  Markdown，并标注失效运行时、未定义引用与 PATH 缺失目录。
- **终端配置片段**：按 PowerShell / CMD / Git Bash 保存可复用片段并导出，绝不自动
  改写用户的 profile。
- **双入口**：桌面 GUI 与命令行共享同一套服务层；CLI 支持 `--json`，破坏性操作
  一律要求 `--yes`。

## 目录

```text
EnvForge/
  pyproject.toml
  requirements.txt
  README.md
  LICENSE                   # MIT
  envforge.py               # 源码检出时的 python -m envforge 兼容启动器
  EnvForge.spec             # PyInstaller 打包配置
  assets/                   # 应用图标（png/ico）
  docs/
    architecture.md
    FEATURE_MATRIX.md       # 功能完成度矩阵（含代码/测试证据）
    RISK_REGISTER.md        # 缺陷、风险与未来需求登记册
    packaging.md
    test-report.md
    screenshot-main.png
  packaging/
    entry_gui.py            # 打包入口（必须放在子目录，避免遮蔽包名）
    make_icon.py            # 生成图标
    installer.iss           # Inno Setup 安装器脚本
  src/envforge/
    __init__.py  __main__.py  cli.py  models.py  errors.py
    db.py  secrets.py  library.py  audit.py  lock.py
    profiles.py  runtimes.py  envvars.py  envstore.py  doctor.py
    spec.py  collectors.py  baseline.py  lab.py  graph.py  report.py
    terminal.py
    ui/__init__.py  ui/qt_app.py
  tests/
```

## 环境与安装

- Python 3.12+，Windows 10/11。
- 核心零第三方依赖；测试使用 pytest，GUI 使用 `PySide6>=6.7`。

源码目录直接运行（不安装项目）：

```powershell
cd EnvForge
python -m envforge --help
python -m envforge            # 打开 GUI（已安装 PySide6 时）
python -m envforge runtime detect
python -m envforge doctor
```

根目录的 `envforge.py` 只负责把启动交给 `src/envforge`，不会进入正式 wheel。

安装为包：

```powershell
python -m pip install -e .
python -m pip install -e ".[gui]"   # 需要 GUI 时
envforge --help
```

## 启动

```powershell
python -m envforge                 # 有 PySide6 时启动桌面界面，否则打印 CLI 帮助
python -m envforge gui             # 显式启动 GUI（缺少 PySide6 时返回非零退出码）
python -m envforge runtime detect  # CLI 入门
python -m envforge doctor
```

GUI 与服务层共享同一数据库，可用 `--db` 或 `ENVFORGE_DB` 指定。关闭窗口时会提示
是否有正在执行的后台操作，避免写入中途退出。

## 命令行速查

```powershell
# 运行时
envforge runtime detect
envforge runtime list
envforge runtime add --kind python --path C:\Python312\python.exe
envforge runtime activate > activate.ps1      # 当前会话前置 PATH

# 项目与档案
envforge project add --name demo --path D:\work\demo
envforge profile set demo dev API_URL=http://localhost:8000
envforge profile set demo dev DB_PASSWORD=... --secret
envforge profile show demo dev                # 敏感值显示 ***
envforge profile export demo dev --output .env
envforge profile diff demo dev prod
envforge profile activate demo dev --yes      # 生成会话激活脚本

# 常用变量库（151 个内置变量，10 个分类）
envforge library categories
envforge library list --category 代理与网络
envforge library list --search JAVA
envforge library show DATABASE_URL --json

# 系统环境变量
envforge env show --scope user
envforge env path --scope user --json
envforge env snapshot --scope user --label before-change
envforge env diff 1 2
envforge env restore 1 --yes                  # 先自动创建回滚前快照
envforge env dedupe --scope user --apply --yes
envforge env db-check                         # 数据库完整性自检
envforge env db-backup                        # 立即备份到 backups/

# 环境标准 Spec v1
envforge spec capture --output workstation.toml
envforge spec check workstation.toml --report reports/
envforge spec check workstation.toml --commands --ports   # 显式开启命令执行/端口探测
envforge spec diff a.toml b.toml --json
envforge spec plan workstation.toml --output plan.ps1     # 只生成脚本，不自动执行

# 开发机画像、软件清单与基线
envforge machine profile --output reports/
envforge machine software --json
envforge baseline capture --label before-upgrade
envforge baseline diff 1 2 --report reports/

# 环境实验室、依赖图与终端片段
envforge lab path --add C:\Python312 --command python
envforge lab cycles --json
envforge lab impact --key JAVA_HOME
envforge graph build --format dot --output graph.dot
envforge terminal set dev --content '$env:FOO = "bar"'
envforge terminal export dev --output dev.ps1

# 审计与诊断
envforge audit list --limit 30
envforge doctor --json
envforge doctor --fail-on warn                # 供 CI/脚本使用
```

退出码：`0` 成功，`1` 运行错误，`2` 参数错误，`3` 需要确认（缺少 `--yes`）。

## 架构

服务层与界面解耦：`cli.py` 与 `ui/qt_app.py` 只负责参数解析、渲染和交互，业务逻辑
集中在 `runtimes` / `profiles` / `envvars` / `envstore` / `spec` / `collectors` /
`baseline` / `lab` / `graph` / `report` / `terminal` 等模块；`db.py` 负责 SQLite
schema、迁移与备份，`secrets.py` 封装 DPAPI，`lock.py` 提供跨进程文件锁。
模块边界、数据流与安全策略见 [docs/architecture.md](docs/architecture.md)。

## 数据位置与安全

- 数据库默认位于 `%LOCALAPPDATA%\EnvForge\envforge.db`，可用 `ENVFORGE_DATA_DIR`
  或 CLI `--db` 覆盖。GUI 同样支持 `ENVFORGE_DB`。
- 审计与崩溃日志在同目录 `logs/`（JSONL，只含变量名与结果），每日备份在 `backups/`
  （保留 7 份）。审计保留 30 天，崩溃日志保留 90 天。
- 写入失败会尝试自动回滚；如果自动回滚也失败，错误信息会给出可复制的
  `envforge env restore <快照号> --yes` 命令。
- 敏感值以 DPAPI 密文存储，密文只能用同一 Windows 用户的凭据解密；换机器或换用户
  无法读取（会显示为不可解密而不是明文）。
- `system` 作用域只读：EnvForge 不会在未经确认的情况下修改 HKLM，也不提供静默写
  入路径。
- 导出文件和快照默认脱敏；确需明文时请自行评估风险。

## 打包

预构建产物在仓库同级的 `release/`：

- `release/installers/EnvForge-0.1.0-setup.exe`：按用户安装，无需管理员权限。
- `release/EnvForge-0.1.0-win64.zip`：便携包，解压后运行 `EnvForge\EnvForge.exe`。

自行构建见 [docs/packaging.md](docs/packaging.md)。

## 测试

```powershell
python -m pip install -e ".[gui]"
python -m pip install pytest coverage
python -m pytest -q
python -m pytest -q --cov=envforge --cov-report=term-missing
```

当前状态：272 项测试通过，核心模块覆盖率约 90%，全量约 78%。测试全部使用临时目录
数据库，注册表读写通过 `MemoryProvider` 注入；采集链路只读验证。功能完成度与已知
风险分别见 [docs/FEATURE_MATRIX.md](docs/FEATURE_MATRIX.md) 与
[docs/RISK_REGISTER.md](docs/RISK_REGISTER.md)，测试详情见
[docs/test-report.md](docs/test-report.md)。

## 已知限制

- 仅在 Windows 11 x64 上实测，未在 Windows 10 上验证。
- `system` 作用域只读；需要修改 HKLM 时请使用系统工具或以管理员身份操作。
- GUI 按钮回调与后台线程信号链主要靠人工走查，自动化只覆盖窗口构建与展示逻辑。
- 跨进程锁的自动化用例是同进程双句柄；真实双进程竞争做过手工验证，未纳入回归。
- Spec 的 `--commands` / `--ports` 真实执行分支只做注入式单测，默认不运行，避免副作用。
- 环境实验室是静态推断，与真实 shell 的 App Paths / 别名 / 函数解析存在差异。
- 基线变更来源依赖审计日志与文件时间，`suspected` / `unknown` 仅作提示，不作为
  责任归属证据。
- DPAPI 密文绑定当前 Windows 用户与机器，换用户或换机器无法解密。

## 许可

本项目采用 MIT 许可证，详见 [LICENSE](LICENSE)。
