# 查询实际工具契约

工具目录、调用校验与 `get_tool_contract` 共用工作台实际定义。宿主将深层对象显示为 `unknown`，或需要确认嵌套字段时，应先查契约，不要把现有草稿值当作允许值清单。

```json
{"tool":"save_plan","path":"/design_brief/simplification/candidates/0/status","offset":0,"length":1200}
```

`path` 指向输入字段，数组下标用于定位元素规则，不是 schema 内部的 `/properties/...` 路径。省略 `path` 可分页读取完整输入 schema。`text` 是 JSON 文本切片，按 `next_offset` 继续；`schema_sha256` 可确认各页仍属于同一契约。`rules` 与 `output_contract` 提供额外语义及结果含义。

契约查询不打开方案或资料正文，不授予阅读回执，也不证明方案物理正确。

## HTTP、MCP 与托管 Agent

- `GET /api/tools` 返回公共工具目录，包含外部会话邮箱工具；`POST /api/tools/call` 调用工具。
- `get_tool_contract` 不需要 `session_id` 或外部绑定。
- `GET /api/tools?bound=true` 返回固定方案的精确工具定义与兼容别名，不含邮箱管理工具，也不要求模型传入 `session_id` 或绑定凭据。
- 公共工具与固定方案工具的 schema 可以不同，应查询当前入口的契约。

MCP 转发公共目录与调用，不维护第二份业务 schema。Codex、托管 DSH 和外部 DSH 桥接读取当前固定方案定义；只有旧服务不支持目录时才使用兼容名单。断开绑定会移除相应工具与提示词。

Codex 线程恢复会保留创建时的工具。工作台按工具和提示词版本指纹处理升级；规范变化时建立新的执行线程并登记新工具，原方案、聊天和旧线程记录保留。同一规范下可正常恢复。外部 DSH 会话更新后需重新绑定。

## 命令行

连接运行中的工作台：

```powershell
python tools_cli.py list
python tools_cli.py contract save_plan --path /design_brief/simplification/candidates/0/status --length 1200
python tools_cli.py call get_tool_contract --args-file <参数JSON>
```

`contract` 是同一公共调用的快捷命令。没有运行中的工作台时，可读取当前源码的静态契约：

```powershell
python agent_tools.py list
python agent_tools.py contract save_plan --path /design_brief/simplification/candidates/0/status
```

这两个本地命令不创建资料库或方案目录，`--data-dir` 不影响契约定义。若工作台安装版本与当前源码不同，应优先查询运行中的工作台。

## 外部客户端

```python
from external_agent_client import ExternalAgentClient

client = ExternalAgentClient(url="http://127.0.0.1:8765")
definitions = client.list_tools()
contract = client.call_tool("get_tool_contract", {
    "tool": "read_source",
    "path": "/length",
})
```

公共目录和契约查询无需注册或方案 ID。绑定对话时应提供实际方案身份并注册，再使用该活动绑定的固定方案契约。

## 有界传输

宿主可能通过 Code Mode 返回 JSON 字符串，应先解码再读取字段。使用受限传输配置时，每个执行单元只调用一个业务工具一次，并只输出该次解码后的完整有界回包；避免批量拼接、重复打印或用投影替代实际证据交付。

正文按来源偏移分页，保留 `next_offset`、实际读取范围与引用。大 schema 按契约路径和偏移展开。实验性结果适配器还可提供 `read_tool_page`，按其返回的 handle、path 和 offset 继续；JSON 片页不能冒充完整属性已读。具体入口以当前会话实际注册的工具为准，不能根据名字猜测可用工具。

实验方法卡只用于证据导航，其工具与构造产物不属于默认公共工具目录，状态说明见 [案例导航](case-navigation.md#实验性方法卡)。
