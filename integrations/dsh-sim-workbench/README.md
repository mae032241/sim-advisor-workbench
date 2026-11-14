# 仿真方案工作台 DSH 插件

`@local/sim-advisor-workbench` 当前版本 **0.2.68**，以 [package.json](package.json) 为准，提供本机工作台启动、资料库管理及当前 DSH 会话与方案的双向连接。业务工具通过独立 Python 服务提供。

## 安装

在仓库根目录使用 Python 3.12+ 构建：

```powershell
python scripts/build_dsh_workbench.py
```

通过 DSH 官方插件管理器安装 `dist/sim-advisor-workbench-0.2.68.tgz`。插件运行要求 Node.js 24.9+。源码目录不含构建时组装的完整 `runtime/`、Bridge 等资源，不能直接作为完整插件安装。

插件不附 Python/Node 运行环境、外部依赖二进制、COMSOL、官方资料、用户模型、索引或模型权重。在设置中配置工作台数据目录、Python 及所需扩展路径；这些数据与插件安装目录分开管理。

## 使用

- 侧栏或 `start_workbench` 启动或复用服务，`workbench_status` 查看状态。
- `create_workbench_plan({goal, model_path?})` 创建方案并绑定当前 DSH 会话。
- `bind_workbench({url})` 连接已有方案地址；聊天与结构化方案分别保存。
- `workbench_library({action,args?})` 管理自备资料库，参数以工具 schema 为准。
- `stop_workbench` 按用户要求停止插件拥有的服务。

默认使用 QMD 关键词检索；可选 QMD 向量检索默认关闭，embedding 默认关闭。向量和语义增强需显式选择。Deep API 依赖用户自备 COMSOL，不在普通规划流程中执行求解。

更多说明见仓库中的 [DSH 接入](../../docs/dsh-workbench.md)、[打包说明](../../docs/dsh-plugin-package.md) 和 [可选依赖](../../docs/offline-dependencies.md)。第三方源码清单和原始许可证位于 [vendor/](vendor/)，须随这些组件保留。

0.2.64 通过资料库管理原生 HTML 章节树；托管后端、外部会话、CLI 与 MCP 使用同一树优先、QMD 补充及回退规范。已有库可在网页详情点击“补建章节树”，不需重解析正文或 QMD。升级后重启工作台，外部会话重新绑定；旧离线包可继续使用，无新增权重、Python 第三方依赖或默认 embedding。详见 [手册树说明](../../docs/html-manual-tree.md)。

0.2.65 增加 `get_case_navigation` 及 [案例树构造说明](../../docs/case-tree-navigation.md)，保留旧环节摘要入口。分层、依赖和手册匹配不授予正文阅读信用，原值与原文仍需实际读取。旧离线包可复用，不新增依赖、权重或默认 embedding。

## 0.2.68 更新

采用远端工作台的网页布局与 Agent 长轮询通信：状态变化唤醒等待，仅传递完成回合的新增回复，页面隐藏或断开后停止对应刷新。保留资料库管理、章节树和案例工具。

`inspect_case_source` 按精确案例 ID 离线读取已登记 MPH 原件的保存 XML，无需 COMSOL；案例概览返回可用的原件与配置读取入口。保存值不等同于当前激活状态或求解验证。章节导航明确标题匹配方式与收窄建议。完整实施规划使用共享研究规范与任务模板，CLI 与工具说明保持一致。旧离线依赖包可继续使用，不新增权重或默认 embedding。
