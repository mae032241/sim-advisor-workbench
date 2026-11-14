# DSH 接入工作台

推荐使用完整的 `@local/sim-advisor-workbench` 插件，当前版本 **0.2.68**，以 [package.json](../integrations/dsh-sim-workbench/package.json) 为准。构建后通过 DSH 官方插件管理器安装 `.tgz`，步骤见 [插件打包](dsh-plugin-package.md)。不要直接安装插件源码目录。DSH 与所用模型服务需自行安装、登录和配置。

## 在当前 DSH 会话使用

配置插件的数据目录和所需依赖后，可从“仿真工作台”侧栏打开页面。工具入口包括：

| 工具 | 用途 |
| --- | --- |
| `start_workbench` | 启动或复用本插件的本机工作台，返回地址 |
| `workbench_status` | 查看进程和配置状态 |
| `create_workbench_plan` | 用 `goal` 和可选 `model_path` 创建方案并绑定当前会话 |
| `workbench_library` | 登记、构建、检索、读取和管理用户提供的资料库 |
| `stop_workbench` | 按用户要求停止本插件拥有的服务并解除绑定 |

Bridge 另提供 `bind_workbench({url})` 绑定已有方案，以及状态和解绑入口。工作台页面中的“复制 DSH 绑定链接”生成当前方案地址；在当前 DSH 对话中请求连接此地址。复制链接本身不会建立绑定或启动模型。

绑定后，网页消息进入当前 DSH 会话；回复与活动由 Bridge 回传。聊天回复不会自动保存方案，Agent 仍须调用 `save_plan` 或 `update_steps`。引用读取、当前 revision、输入缺口及确认条件仍由业务服务校验。

## 从工作台托管 DSH

独立启动工作台时，可用 `python run.py --agent-backend dsh`，或在可用的托管模式中选择 DSH。网页中保存后端、模型和推理配置后，再连接或发送任务。该模式由服务管理独立 DSH 进程；已有外部会话的绑定走前述 Bridge。

`--agent-backend none` 禁止托管进程启动，仍可使用外部 DSH 绑定和公共工具。模型后端的命令可用性不等于登录或模型调用已验证，状态以实际配置和运行结果为准。

## 资料与数据

资料库全局管理，不绑定某个方案。新增库需自行提供来源，构建后手动启用。默认使用 QMD 关键词检索，可选 QMD 向量检索默认关闭，embedding 默认关闭，向量和语义增强只按显式选择执行。Deep API 增强需用户自备 COMSOL。

数据目录与插件安装目录分开。迁移前停止使用该数据目录的服务和 CLI，再完整复制数据目录并重新配置依赖和原始资料位置；插件设置中切换目录不会替用户搬迁数据。原始来源改位置可使用 `library_cli.py rebind`。

工具参数以安装版本返回的 schema 为准。源码实现见 [完整插件](../integrations/dsh-sim-workbench/index.js)、[Bridge](../integrations/dsh-workbench-bridge/index.js) 与 [接口约定](../CONTRACT.md)。

## 0.2.68 更新

采用远端工作台的网页布局与 Agent 长轮询通信：状态变化唤醒等待，仅传递完成回合的新增回复，页面隐藏或断开后停止对应刷新。保留资料库管理、章节树和案例工具。

`inspect_case_source` 按精确案例 ID 离线读取已登记 MPH 原件的保存 XML，无需 COMSOL；案例概览返回可用的原件与配置读取入口。保存值不等同于当前激活状态或求解验证。章节导航明确标题匹配方式与收窄建议。完整实施规划使用共享研究规范与任务模板，CLI 与工具说明保持一致。旧离线依赖包可继续使用，不新增权重或默认 embedding。
