# 仿真方案工作台 DSH 插件

`@local/sim-advisor-workbench` 当前版本 **0.2.63**，以 [package.json](package.json) 为准，提供本机工作台启动、资料库管理及当前 DSH 会话与方案的双向连接。业务工具通过独立 Python 服务提供。

## 安装

在仓库根目录使用 Python 3.12+ 构建：

```powershell
python scripts/build_dsh_workbench.py
```

通过 DSH 官方插件管理器安装 `dist/sim-advisor-workbench-0.2.63.tgz`。插件运行要求 Node.js 24.9+。源码目录不含构建时组装的完整 `runtime/`、Bridge 等资源，不能直接作为完整插件安装。

插件不附 Python/Node 运行环境、外部依赖二进制、COMSOL、官方资料、用户模型、索引或模型权重。在设置中配置工作台数据目录、Python 及所需扩展路径；这些数据与插件安装目录分开管理。

## 使用

- 侧栏或 `start_workbench` 启动或复用服务，`workbench_status` 查看状态。
- `create_workbench_plan({goal, model_path?})` 创建方案并绑定当前 DSH 会话。
- `bind_workbench({url})` 连接已有方案地址；聊天与结构化方案分别保存。
- `workbench_library({action,args?})` 管理自备资料库，参数以工具 schema 为准。
- `stop_workbench` 按用户要求停止插件拥有的服务。

默认使用 QMD 关键词检索；可选 QMD 向量检索默认关闭，embedding 默认关闭。向量和语义增强需显式选择。Deep API 依赖用户自备 COMSOL，不在普通规划流程中执行求解。

更多说明见仓库中的 [DSH 接入](../../docs/dsh-workbench.md)、[打包说明](../../docs/dsh-plugin-package.md) 和 [可选依赖](../../docs/offline-dependencies.md)。第三方源码清单和原始许可证位于 [vendor/](vendor/)，须随这些组件保留。
