# 案例发现与实现导航

案例概要发现与逐环节导航复用现有 QMD 关键词索引。导航只提供配置和教程入口，不证明正文已读、设置生效或案例适用于新任务。

## 使用顺序

1. `get_library_scope` 查看范围，按需启用有关资料库；等待启用成功后再检索。
2. `search_cases` 只搜索模型概要，使用返回的精确 `id` 作为 `case_id`。
3. `read_source` 读取选中概要，初步核对目标、条件和接口。
4. `get_case_navigation(case_id)` 逐层查看已解析节点、依赖与手册候选；`get_case_guide(case_id)` 保留短环节摘要。详见 [案例树导航](case-tree-navigation.md)。
5. `search_knowledge` 可传 `case_id` 和 `stage`，搜索该版本的配置与关联教程。`kind=all` 可同时包含配置和教程；`kind=case` 会排除手册类型的教程。
6. `read_case_configuration` 读取实际保存配置或 API 观察。默认一次读取一个节点；准确的 `compact:node/N` 可精确定位。`source=saved` 与 `source=api` 是独立证据范围。
7. 对新任务独立查阅手册，核对方法条件、输入、耦合和迁移边界，再形成方案。
8. `review_plan` 检查需求映射、显式实施字段、依赖及阅读账本，修订后保存。

`case_id` 不能填写文件路径或 `asset_key`。候选、导航和配置摘要不能代替正文与实际属性读取。保存值、API getter、教程描述和 Agent 推断需分别标注；部分提取、未解码项、未评估激活状态及未运行物理验证的限制仍需保留。

## 设置与依据表

最终流程提交前，可用 `save_plan_settings` 暂存当前方案 `revision` 的设置表。它不修改流程或用户回答。每个独立设置单独登记：

- `requirement` 与 `setting`：本任务要求和需要落实的设置。
- `proposed_value`：拟用表达式、数值及单位，或明确的方法条件。
- `status`：`adopted`、`adapted`、`alternative`、`unresolved` 或 `not_applicable`。
- `reason`、`transfer_rationale`、`gap`：取舍理由、迁移依据与仍缺的事实。
- `step_ids`：对应实施步骤。
- `source_refs`：实际阅读的 `source_id`、`context`、`locator` 和来源原值 `stated_value`。

`read_source` 返回本页 `evidence_refs`；配置返回节点引用，属性另有 `evidence_locator`。应复制服务返回的完整定位，不能猜测。定位吻合只证明该范围返回过，不证明原值抄录正确、方法适用或模型已启用。独立推导与用户输入可没有来源引用，但要说明依据。

先提取参考设置并暂存为 `unresolved`，再按相同 ID 决定采用、调整、替代或不适用，最后据取舍生成实施步骤。暂存更新漏掉已有条目时，`get_plan_settings(view="unhandled")` 可分页查看待处置项；说明处置后再提交。此检查不能发现最初就未提取的知识。有意清空表时，可在完整 `save_plan` 中显式提交 `plan_settings=[]`。

`get_plan_settings` 按行分页或按 `step_id` 查看。每页最多 24 KiB，单行最多 12 KiB，整表最多 2 MiB。`save_plan` 和 `update_steps` 会采用同版本暂存表并检查步骤引用，表随流程进入历史和导出。旧版本暂存表不会自动附到新输入上。

网页中的“设置与依据”按步骤折叠显示；未采用或待处理设置单独折叠。表中实施落点及机制检查见 [机制检查](mechanism-checks.md)。

## 草稿与精确修复

`save_plan` 在校验前保留原始提交。失败响应包含 `draft_id`、`draft_revision` 与 JSON pointer 错误路径，无效草稿不更新方案。用 `get_plan_draft` 读取出错字段或分页查看，再以 `repair_plan_draft` 提交 `add`、`replace` 或 `remove` 补丁。

每个方案只保留最近一次草稿；新完整提交替换旧草稿。全部校验通过才保存，补丁不能绕过来源权限、阅读范围或版本冲突。格式修复不应删除仍有意义的表达式、单位、参数或研究设置。准确 schema 与分页方式见 [工具契约](tool-contract-transports.md)。

## 网页与命令行

网页“文档与案例检索”可选择“找案例概要”，打开案例后加载实现流程。环节默认折叠，展开时才请求导航；配置也需单独展开读取，不随全局状态刷新传输整例详情。

连接运行中的工作台，可使用：

```powershell
python tools_cli.py --url <本机服务地址> list
python tools_cli.py --url <本机服务地址> call get_case_guide --args-file <参数JSON>
```

也可使用 `agent_tools.py --data-dir <数据目录> cases <查询>` 和 `case-guide <案例概要ID> [--stage mesh]`。参数以当前版本的实际工具契约为准。

## 构建派生导航

新建库发布 QMD 后会从已有结构化档案派生概要目录。对现有资料库补齐导航：

```powershell
python scripts/build_case_guides.py --data-dir <工作台数据目录> --all --report <本地报告JSON>
```

只处理显式选择的案例：

```powershell
python scripts/build_case_guides.py --data-dir <工作台数据目录> --case <精确案例概要ID> --case <另一个ID>
```

派生目录位于 `libraries/search/runtime/case-navigation.sqlite3` 和 `case-guides.sqlite3`，按案例存储和查询，不是另一份全文索引。构造不调用 COMSOL、不生成 embedding、不启用资料库，也不修改 QMD 正文。查询仍受当前库权限和隔离策略约束；缺配置或构造失败时保留具体缺口。

导航选择适合展示的配置与教程入口，可能只列出部分节点。需要核查未列出的设置时，使用现有原始配置入口和准确节点定位。Frame、视图或显示属性不因出现于模型中就成为方法实施步骤。

构造进度与中断标记位于报告指定位置；`--progress-file` 可改进度路径，`--no-resume` 可显式重构。同代已完成案例可复用。目录迁移或代际变化后可能需要重建派生元数据，不需要重新 embedding。

## 可追溯的检索上下文

`enrich_case_contexts.py` 可从已有关联教程的 Introduction、Model Definition、建模方法等章节提取有界原文片段，记录来源 ID、偏移与哈希，再更新案例概要。它不接收检索问题、不调用 LLM、不手写案例摘要；缺少可靠正文时保留缺口。每例最多 3,600 字符，不保存完整教程副本。

停止工作台后，先预览，再显式应用：

```powershell
python scripts/enrich_case_contexts.py --data-dir <数据目录> --case <概要ID> --report <预览JSON>
python scripts/enrich_case_contexts.py --data-dir <数据目录> --case <概要ID> --apply --report <结果JSON>
```

批量处理可使用 `enrich_all_case_contexts.py --data-dir <数据目录> --all --apply --batch-size 64 --report <本地报告JSON>`。报告提供断点与中断位置；同代同策略可续接，不复制完整教程作为永久缓存。

增量更新保留原来源身份和其余语料，不重新解析 MPH 或生成 embedding。增强概要的模型库与教程来源库都需启用；`get_library_scope` 会列出依赖。关闭教程库后不附加其摘录，直接模型读取仍遵守独立权限。

迁移后可用 `enrich_case_contexts.py --data-dir <新数据目录> --rebind` 核对并重关联元数据；只有未完成更新才使用 `--recover`。工作台不会将缺少权限约束的半成品当作有效索引。

## 实验性方法卡

方法卡是可选的研究模块，尚未作为默认规划流程启用。它用短目录和逐节说明组织目标、机制、适用条件、排除条件、替代方法、所需输入、耦合依赖、实施入口与缺口。

构造器只接受显式案例身份，读取该案例、直接关联教程与允许范围内的通用手册；方法选择依据应来自这些来源。它区分正文、保存配置、API 观察、推断与未知，引用需来自本次实际交付的有界读取。完整节点标题目录用于发现入口，不表示整例属性已读。

可选导航工具为 `list_route_methods` 与 `read_method_card`，仅在实验入口显式注册时可用。卡中的来源定位用于重新找到原文；规划会话仍需真实读取后才能引用。卡不能证明来源结论正确、配置已生效、方法适用或求解完成。

构造记录、方法卡包、冻结目录与交付审计由用户在自己的输出目录管理，不随公开源码附送。该模块的存在不代表已验证的方案质量或替代通用工程审阅。
