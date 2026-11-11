# 有界研究规划器

[ResearchPlanner](../advisor/research_planner.py) 保留批处理研究入口：`ResearchPlanner(data_dir, kb).refine(session)`。它与工作台持续对话是不同调用方式，返回结果由调用方校验 revision 后保存，不直接改变原始模型。

## 流程与输出

规划器先使用用户目标、已观察模型事实和回答形成问题，再检索和读取资料，比较候选路线并生成实施指南。必要时允许一次补充证据轮次，达到预算仍不足时保留明确受阻的草稿或报告失败。

返回内容包括 `plan`、实际读取的 `sources`、`questions`、`retired_questions`、`design_brief`、`research_trace` 与 `planning_incomplete`。`design_brief` 描述目标、计算域策略、候选路线、决策、几何解释和模型简化建议；具体结构以源码 schema 为准。

研究记录描述检索、读取及控制器状态，不是模型内部推理。模型调用由后端接口承担，业务工具和保存校验独立于模型提供方。批处理后端配置及超时边界见 [agent_backend.py](../advisor/agent_backend.py)。使用真实后端需要用户自行配置宿主、模型和凭据，可能产生账户用量。

## 约束

- CAD 的实体、名称和尺寸不能自动确定物理场、材料、载荷或计算域。
- 方案只能引用本轮实际读取或允许复用的来源，目录摘要不能代替实施正文。
- 已有问题 ID 的语义必须保持，退役问题保留理由及历史回答，不能替用户作答。
- 输入缺失、证据不足、过期方案和受阻路线分别保留；局部更新不能清除全局阻塞。
- 简化建议需说明对象、条件、可能丢失的效应和验证方法，不执行 CAD 修改或声称数值误差已验证。

几何事实引用使用本次快照中的精确 `fact:<key>` 或 `body:<id>`。自由文本、STEP 记录编号与整体计数不能补造局部面号、厚度、间隙或 COMSOL 边界编号。细节不足的候选保持待检查状态。

`refine_simplification(session, queries=None)` 为已有且非过期的 Agent 方案补充局部简化分析，保留继承的证据缺口和阻塞。失败或并发版本变化不能静默覆盖既有方案。

持续对话优先使用 [PlanningService 公共工具](../advisor/planning_service.py)；MCP、HTTP 和 DSH 共用同一保存与证据校验。规划提示词见 [comsol_planner.v1.txt](../prompts/comsol_planner.v1.txt)。相关测试使用可控后端验证编排与失败处理，不能替代用户自备案例的工程验收。本仓库不提供固定官方评测集或本地运行结果。
