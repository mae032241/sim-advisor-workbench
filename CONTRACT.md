# 工作台接口约定

本机单用户工作台使用 Python 3.12+ 标准库 HTTP 服务与原生 HTML/CSS/JavaScript 页面，监听 loopback 地址。默认端口为 8765，实际端口以启动输出为准。服务没有多用户认证或远程部署契约。

HTTP、CLI、MCP、DSH 与托管模型适配器共用独立 `PlanningService`，业务方案 ID 不依赖模型后端。模型读取与普通规划流程不修改原始输入或执行 COMSOL 求解。

## 公共工具

`GET /api/tools` 返回当前 `planning-tools/v1` schema。`POST /api/tools/call` 接收 `{name,arguments}`，返回 `{result}`。当前主清单包括：

| 工具 | 用途 |
| --- | --- |
| `get_task_context` | 读取方案需求、事实、回答、缺口和版本 |
| `search_knowledge` | 按资料库、类型和可选功能分支检索 |
| `read_source` | 分页读取原文并可关联方案证据 |
| `save_plan` | 以当前 revision 保存完整路线设计 |
| `update_steps` | 按已有 ID 更新局部步骤 |
| `get_library_scope` | 查询包括关闭库在内的轻量目录 |
| `set_libraries_enabled` | 显式批量启停指定库 |
| `register_external_agent` | 注册常驻外部宿主 |
| `poll_external_messages` | 收取网页消息、停止和解绑命令 |
| `publish_external_event` | 回传回复、活动、状态和错误 |
| `unregister_external_agent` | 解除外部绑定 |

`get_plan` 是可调用的兼容别名，不列为主工具。精确参数、枚举和必填项以实际 schema 为准。

`session_id` 是持久工作台方案 ID，不能用 MCP 连接 ID 或聊天 thread ID 替代。任务读取和方案写入要求该 ID，写入还要求当前 `revision`。独立检索/读取可省略它；未关联方案的阅读不会自动成为该方案已读证据。资料库目录和启停是全局工具。

## 方案与证据

方案创建只保存需求和可用模型事实，不从 CAD 固定分类物理场或生成预设路线。`save_plan` 保存结构化方案；`update_steps` 只更新已有步骤，不清除全局缺口。聊天文字不自动生成或覆盖方案。

方案引用须符合已读来源、当前可见性和版本校验。用户输入改变会使旧确认失效；未解决的必填问题、过期或不完整方案、受阻路线不能确认。工具不能替用户回答或确认。确认表示认可当前方案版本，不证明数值有效或授权自动求解。

几何观察与解释分别处理。`fact:<key>` 和 `body:<id>` 必须来自实际提供的快照；自由文字和声明计数不能发明局部实体或 COMSOL 边界编号。模型简化是带条件和验证要求的指导，不是已执行 CAD 修改。

## 常用 HTTP 入口

| 入口 | 用途 |
| --- | --- |
| `GET /api/health` | 进程和 HTTP 就绪状态 |
| `GET /api/status` | 服务、资料与方案状态 |
| `POST /api/models/inspect` | 用 `{path}` 读取模型快照 |
| `POST /api/sessions` | 用 `{goal,model_path?,model_id?}` 创建方案 |
| `GET /api/sessions/{id}` | 读取方案 |
| `POST /api/sessions/{id}/answers` | 用当前 revision 保存用户回答 |
| `POST /api/sessions/{id}/confirm` | 校验并确认当前 revision |
| `GET /api/sessions/{id}/versions` | 历史版本目录 |
| `GET /api/sessions/{id}/export?format=markdown\|json` | 导出方案 |
| `GET /api/library-scope` | 轻量资料库目录 |
| `POST /api/libraries/enable` | 批量资料库启停 |

完整路由及错误处理以 [server.py](advisor/server.py) 为准。版本冲突返回 409；参数和约束失败通常返回 422。资料库启停、删除及测试集隔离同样限制旧来源 ID 的新读取。

## 托管与外部 Agent

托管模式选择 Codex 或 DSH，模型配置和进程生命周期由相应适配器管理。`python run.py --agent-backend none` 禁止托管模型启动，仍保留外部接入。后端状态、模型配置和实际调用成功分别判断。

直接外部入口为 `POST /api/sessions/{id}/external/{register|poll|event|unregister|tool}`。注册使用常驻宿主 PID，返回 `binding_id` 和应纳入宿主上下文的 `instructions`。后续调用需持有绑定凭据，轮询使用返回的 cursor。绑定凭据不应进入模型上下文或公开日志。

外部宿主负责模型调用、调度及实际取消；心跳线程不能被长模型调用阻塞。SDK `ExternalAgentClient` 提供注册、收件 pump、事件发布和工具调用；不能同时运行 pump 与手动 poll。解绑只解除协作关系，不终止外部宿主进程。

源码见 [external_agent_client.py](external_agent_client.py)、[chat_bridge_tools.py](advisor/chat_bridge_tools.py) 和 [external_agents.py](advisor/external_agents.py)。MCP stdio 桥接只连接已有本机工作台，不启动模型或服务。

## 本地资料与发布

运行数据由 `SIM_ADVISOR_DATA` 或插件的数据目录设置管理，和源码及安装包分开。默认 QMD 关键词检索，旧 native 选择仅兼容转为 qmd_keyword，embedding 默认关闭并需手动配置。COMSOL Deep API 与可选扩展依赖用户自行提供。

公开源码不包含官方资料、模型、固定测试集、benchmark 资料、索引、凭据、聊天或实验结果。测试输入由用户通过相应 `--selection`、`--scenario` 或 `--root` 参数提供。第三方 vendor 源码和原始许可证单独保留。
