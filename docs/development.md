# 源码开发

仓库包含工作台后端、网页、模型适配器、DSH 插件、提示词、测试和打包脚本。当前插件版本见 [package.json](../integrations/dsh-sim-workbench/package.json)。Python 运行环境要求 3.12+；DSH 插件和 JavaScript 测试要求 Node.js 24.9+。

## 本地运行

在仓库根目录运行：

```powershell
python run.py --agent-backend none
```

主 HTTP 服务仅使用 Python 标准库。终端显示本机地址；`--port 0` 可请求系统分配空闲端口。`SIM_ADVISOR_DATA` 可指定持久数据目录。首次克隆不含资料库和依赖运行包，按 [可选依赖说明](offline-dependencies.md) 配置所需功能。

`none` 禁止启动托管 Agent，保留工作台、公共工具和外部 Agent 接入。使用 `--agent-backend codex` 或 `--agent-backend dsh` 前，需自行安装并配置对应宿主。不要将模型凭据、用户配置或聊天记录提交到源码仓库。

## 测试

在独立开发环境安装 pytest 后，从仓库根目录运行：

```powershell
python -m pytest -q tests
node --test tests/*.test.mjs
node --test integrations/dsh-sim-workbench/service.test.js integrations/dsh-sim-workbench/test/*.test.js integrations/dsh-workbench-bridge/test/*.test.js integrations/dsh-managed-host/test/*.test.js
```

只从 `tests/` 收集 Python 测试，避免收集外部依赖中的测试。依赖 COMSOL、解析扩展或外部输入的测试需要单独准备环境；测试通过数不代表模型方案质量或数值结果正确性。

## 构建插件

```powershell
python scripts/build_dsh_workbench.py
```

脚本按显式清单组装运行资源，产物及校验清单写入 `dist/`。将生成的 `.tgz` 通过 DSH 插件管理器安装，不能把插件源码目录直接当作完整安装包。发布前核对实际压缩包清单，确保没有资料库、官方模型或手册、索引、凭据、运行环境、权重和本地实验数据。

## 代码入口

- [advisor/planning_service.py](../advisor/planning_service.py)：模型后端无关的公共业务工具。
- [advisor/server.py](../advisor/server.py)：本机 HTTP 服务和路由。
- [advisor/runtime_adapters.py](../advisor/runtime_adapters.py)：托管后端选择。
- [workbench_mcp.py](../workbench_mcp.py)、[tools_cli.py](../tools_cli.py)：MCP 与 CLI 适配。
- [integrations/dsh-sim-workbench/](../integrations/dsh-sim-workbench/)：完整 DSH 插件。
- [CONTRACT.md](../CONTRACT.md)：公共接口与版本、证据约束。

运行数据和构建产物由本机管理，不属于源码。`vendor/` 中列明的第三方源码和原始许可证需要一并保留，清单见 [VENDOR.json](../integrations/dsh-sim-workbench/vendor/VENDOR.json)。
