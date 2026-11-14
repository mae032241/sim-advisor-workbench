# 案例分层导航、依赖与手册关系

0.2.65 为已有解析档案增加案例树与配置引用索引。它保留当前已解析节点的父子层级、物理接口祖先、来源身份及准确配置读取入口，避免只凭少数环节摘要定位设置。案例原生树是结构导航，不是物理因果图。

## 使用顺序

0.2.66 增加轻量原件入口：案例定位后先读概要判断相关性，再按当前证据缺口选用 `read_source` 回包里的 `case_reading`。其中 `inspect_case_source(case_id=...)` 可离线查看注册目录中原始 MPH 的保存 XML，不需要预建案例树、方法卡或启动 COMSOL；`read_case_configuration` 继续读取已有保存/API配置，案例树仍是可选导航。无需为调用工具而展开不适用案例，也不强制读取全图。

`inspect_case_source` 默认返回 `dmodel.xml` 的直接子节点；沿返回的 `node_path`（如 `/0/2`）展开，或用 `view="xml"` 和 `offset/next_offset` 阅读小页原文。这里的零起始 XML 路径与 `compact:node/N` 不是同一种坐标。回包保留原始文件路径、SHA256、成员和读取范围，明确为 `original_saved_xml`；不冒充 API 观察、有效激活或已求解证明，也不产生 `case_configuration` 阅读信用。非空 XML 正文页由工作台单独返回并登记 `original_mph_xml` 精确范围凭据，可用于设置表中的原始保存值引用；仅浏览 children 元数据不会获得正文信用。同一文件版本会复用解析和指纹缓存；文件版本或预期指纹变化后重新校验。每次读取前后仍检查当前库状态、测试集边界和注册根目录。

1. 用 `search_cases` 找概要，再 `read_source` 读概览，先核对目标、工况和方法是否适用。
2. 以返回的精确 `case_id` 调用 `get_case_navigation`，逐层展开相关节点；按需要查看依赖和手册关联。
3. 用节点返回的 `read_requests` 或关联返回的 `configuration_reads` 调用 `read_case_configuration`，核对实际保存值或 API 观察。
4. 沿手册候选的 `manual_read` 或精确 HTML 节点调用 `read_html_section`，结合父接口和祖先标题核对特征定义、条件与关键量传递。
5. `get_case_guide` 仍提供简短环节摘要；QMD 继续补充术语、表达式、案例配置及关联教程。没有准备案例树时，保留这些既有入口。

功能手册研究仍以 [HTML 手册树](html-manual-tree.md) 为主导航，QMD 补充与回退，不强制遍历整本手册或整个案例。

## 工具视图

`get_case_navigation` 的准确字段以当前 `get_tool_contract` 为准：

| `view` | 必需身份 | 用途 |
| --- | --- | --- |
| `children`（默认） | `case_id` | 浏览层级；可用 `node_path` 展开节点，或用 `query` 在节点元数据中定位 |
| `dependencies` | `case_id` | 按 `node_path` 看配置引用，`direction` 可为 `outgoing`、`incoming` 或 `both` |
| `manual_links` | `case_id` | 查看案例或所选节点的手册关联候选 |
| `implementations` | `manual_node_id` | 从手册节点反查已构造且当前可访问的案例实现候选 |

`node_path` 使用工具返回的准确 `compact:node/N`；`manual_node_id` 使用手册树返回的不透明公共身份，不手写文件路径。每页 `limit` 为 1–8，默认 8；按真实 `next_offset` 续读。局部页面不是全树或全部依赖已读。

## 构造与覆盖

脚本只读取当前案例的已有结构化档案与已准备的手册目录元数据，不调用 LLM、COMSOL 求解器或 embedding，不重建 QMD。它不接收测试问题或答案，不为具体案例写物理路线别名。

案例按精确身份和版本构造，增量原子发布会保留其他已构造案例。节点索引覆盖当前已经解析的结构，不再受旧摘要中每环节选取少数节点的展示上限影响；阶段只是分类标签。保存态与 API 观察保持来源模式，不因名称相近而合并成同一事实。

“覆盖已解析节点”不等于完整解析整个 MPH。未提取内容、预览模型、未知属性、未评估激活状态和未运行物理验证等限制继续保留。缺失节点不证明模型中不存在相应设置。

明确选少量已有案例：

```powershell
python scripts/build_case_tree.py --data-dir <资料库目录> --case-id <精确案例概要ID> --case-id <另一个ID> --report <本地报告JSON>
```

这里的 `--data-dir` 是包含 `catalog.sqlite3` 的资料库目录，通常为 `<工作台数据目录>/libraries`。

也可在用户选择的库中按目录身份作确定性小样本预览：

```powershell
python scripts/build_case_tree.py --data-dir <资料库目录> --library <资料库ID> --limit 3 --dry-run --report <预览JSON>
```

`--case-id` 可重复，不能与 `--library` 或显式 `--limit` 混选；`--library` 也可重复，采样默认上限为 3。`--dry-run` 只输出选择清单，不构造；本脚本没有默认全库 `--all` 入口。选择与报告由用户在本地管理，不随源码或插件附送。

默认在构造节点与依赖后，显式构造案例到手册的关联；报告分别保留 `tree` 和 `manual_links` 阶段结果。`--no-manual-links` 仅用于诊断性地跳过手册关联，不代表该案例没有相关手册。

## 依赖如何提取

依赖来自当前配置中可观察的引用，保留来源模式、属性定位和目标节点或来源入口，不另存完整属性正文。分类包括：

| `kind` | 结构含义 |
| --- | --- |
| `expression_symbol` | 表达式中能与已知定义对照的符号引用 |
| `function_call` | 可识别函数调用 |
| `named_selection` | 命名选择引用 |
| `node_tag` | 显式节点标签引用 |
| `geometry_ref`、`physics_ref`、`study_ref`、`solver_ref` | 对相应结构对象的引用 |
| `unknown` | 无法可靠归类的已观察关系 |

依赖 `status` 为 `confirmed`、`ambiguous` 或 `unresolved`。`confirmed` 只表示解析器在既有定义中找到匹配，不证明数值激活、物理方向或有效性；歧义保留候选，无可靠目标时保留未决原因。内置名称和未识别表达式词不自动变成用户定义变量，阶段顺序不自动产生因果边。

## 手册如何关联

关联器只读案例树和功能手册 TOC 元数据。优先使用原记录中的文档 href/ref；缺少精确关联时，使用通用的大小写、CamelCase、Unicode 标点及标题词规范化，先找 Physics 父接口，再在该子树中匹配特征。没有按具体物理场或案例添加私有别名，也不让 LLM 决定对应项。

没有 Physics 祖先的网格、几何、研究和求解器等结构节点，按记录的阶段和原生祖先类型缩小章节范围，再做通用词形与前缀匹配；这些结果始终只标为候选或歧义。Physics 父接口只有弱匹配时，也只给父接口候选，不跨候选接口替叶节点选定特征。报告分别统计 Physics 子树、结构节点与其他节点，避免用全部节点的分母掩盖接口定位缺口。

每条结果对应一个候选目标，`mapping_state` 区分 `matched`、`candidate`、`ambiguous` 和 `unresolved`，并保留匹配 `basis`、`confidence` 与 `verified:false`。这些值描述关联方式，不是物理验证或统计置信保证；弱匹配要核对，多项候选保留歧义，缺少可靠父接口或目标时明确未决。不能只因叶节点同名就选定手册条目。反向 `implementations` 视图提供同样范围内的结构候选，不能把“有案例使用”当成适用于当前任务的证明。

关联读取使用请求内的批量来源与手册授权，先过滤再计算分页范围，返回前复核选中页。只缓存版本未变的目录元数据，不跨请求缓存权限；相关 HTML 文件按请求核对原文指纹。

## 证据与边界

`children`、依赖边、手册关系和反向候选都只是导航，不授予正文或原值阅读信用。只有 `read_case_configuration`、`read_source`、`read_html_section` 的真实读取返回相应 `evidence_refs`。保存值、API 观察、正文与 `html_section` 的定位不能互换。

案例与手册两侧仍受库启用、删除、版本、根目录及隔离策略约束；旧节点或关联记录不能绕过关闭和保留来源限制。元数据过期时按实际状态补建，而不是继续沿旧值推断。读取也不证明求解成功、公式正确或耦合完整，关键量传递和适用条件仍需工程判断。

本次只新增派生元数据和工具，不重新提取案例原件、不修改模型、材料、研究或来源文本；旧离线依赖包可以继续使用，无新增权重或默认 embedding。升级后重启工作台，外部会话重新绑定以获得当前工具与共享规范。

## 如何检验建库的意义

严格对照应给原始地址组同一批案例和文档的原始地址及普通文件/解析工具，建库组额外获得上述导航和检索能力。不能把已有 QMD 或非方法卡答案当作这一对照。题面、模型、预算、可见原件与保存格式保持一致，具体见[原始地址对照协议](raw-address-ablation.md)。结构验证通过也不等于规划质量已提升。
