# Sim Advisor Workbench

本地 COMSOL 仿真方案工作台。当前 DSH 插件版本为 **0.2.49**，同时提供独立 Python 服务、CLI 和 MCP 桥接。

工作台将需求、STEP/STP/MPH 模型事实、用户回答、资料引用和方案版本放在同一流程中。规划工具由独立的 `PlanningService` 提供，模型后端通过适配器接入。保存方案与聊天回复分别管理，结构校验和用户确认不能替代工程验证。

规划模板通过 `advisor.research_policy.load_planner_instructions` 加载，展开通用规划和资料研究策略；自定义 Agent 宿主也应使用该入口。检索及读取返回的 `source_context` 区分手册、案例教程、保存配置和 API 观测，并保留适用性未核实的状态。Agent 应分别查阅手册和相近案例，核对机制、配置依赖与迁移边界，不能把检索命中当作已经验证的实现。

## 从源码启动

需要 Python 3.12 或更新版本。在仓库根目录运行：

```powershell
python run.py --agent-backend none
```

打开终端显示的本机地址，按 `Ctrl+C` 停止。此模式运行工作台和公共工具服务。主 HTTP 服务使用 Python 标准库；模型解析、检索扩展和模型后端按需配置。首次克隆不附带资料库，需自行提供有权使用的资料并登记、构建和启用。

DSH 插件和 JavaScript 测试需要 Node.js 24.9 或更新版本。DSH、Codex、COMSOL、sim-cli、QMD、PageIndex 及其运行依赖由用户按所需功能另行准备；源码发布不附依赖二进制、模型权重或离线运行包。

## DSH 插件

```powershell
python scripts/build_dsh_workbench.py
```

将生成的 `dist/sim-advisor-workbench-0.2.49.tgz` 通过 DSH 官方插件管理器安装。应安装构建后的 `.tgz` 包；源码目录缺少打包时组装的运行资源，不能直接作为完整插件安装。配置数据目录和所需依赖后，可通过侧栏或 `start_workbench` 启动，通过 `create_workbench_plan` 创建方案并绑定当前会话。

详见 [插件打包](docs/dsh-plugin-package.md)、[DSH 接入](docs/dsh-workbench.md) 和 [可选依赖](docs/offline-dependencies.md)。

## CLI 与 MCP

服务启动后，CLI 可查看和调用同一套 HTTP 业务工具：

```powershell
python tools_cli.py list
python tools_cli.py call get_task_context --args-file task-context.json
```

`task-context.json` 示例：`{"session_id":"实际方案ID"}`。参数以 `tools_cli.py list` 返回的 schema 为准。

外部 Agent 可通过标准 MCP stdio 桥接访问正在运行的服务：

```powershell
python -m pip install -r requirements-mcp.txt
python workbench_mcp.py --url http://127.0.0.1:8765
```

端口使用实际启动输出。MCP 桥接只转接业务工具，不启动工作台或模型。公共入口为 `GET /api/tools` 和 `POST /api/tools/call`；方案读写、资料检索、资料库启停与外部对话共用后端实现。实现见 [PlanningService](advisor/planning_service.py)、[HTTP 服务](advisor/server.py)、[MCP 桥接](workbench_mcp.py) 和 [外部 Agent 客户端](external_agent_client.py)。

## 资料与检索

支持登记手册或模型目录，原始来源由用户管理。默认检索为原生 SQLite FTS/BM25；QMD 关键词检索是可选扩展。向量构建需手动选择，embedding 默认关闭，不在普通启动时自动下载模型或构建向量。COMSOL Deep API 读取依赖用户自备的可用 COMSOL 安装。

```powershell
python library_cli.py --help
python library_cli.py add "我的资料" --type user-manuals --path "<资料目录>"
python library_cli.py build LIBRARY_ID --modes native
python library_cli.py enable LIBRARY_ID
python library_cli.py search "网格收敛" --library LIBRARY_ID
```

库类型中的“官方/自定义”是登记声明。读取到的设置、文档说明与 Agent 推断分别处理；缺失字段不能视为不存在。普通规划、检索和离线提取不执行 COMSOL 求解。几何准备等专用脚本的执行范围见各自说明。

## 源码与测试

| 目录或入口 | 用途 |
| --- | --- |
| [advisor/](advisor/) | 业务服务、资料库、解析、检索与模型适配器 |
| [web/](web/) | 本地工作台页面 |
| [prompts/](prompts/) | 规划提示词与结构约束 |
| [integrations/](integrations/) | DSH 插件与桥接 |
| [scripts/](scripts/) | 构建、资料处理与评测脚本 |
| [tests/](tests/) | Python 与 JavaScript 测试 |

开发和测试步骤见 [开发指南](docs/development.md)。检索配置见 [检索说明](scripts/RETRIEVAL.md)，有界研究流程见 [研究规划器](scripts/RESEARCH_PLANNER.md)。[测试集隔离](docs/evaluation-sets.md) 与 [几何挑战](scripts/geometry_challenge/README.md) 提供工具说明；本仓库不含固定官方测试集、benchmark 资料或既有评测结果，输入和验收标准由用户自行准备。

本仓库发布源码和必要的第三方依赖源码闭包，不包含 COMSOL 官方资料、模型、索引、会话、凭据、本地实验数据、运行环境或安装产物。第三方源码清单与许可证保留在 [vendor/](integrations/dsh-sim-workbench/vendor/)，适用各自许可。
