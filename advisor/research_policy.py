"""Shared, task-independent planning and evidence-research requirements.

The product and both benchmark transports import these exact policies. Keep
benchmark questions, references and evaluation diagnostics out of this module.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


PLANNING_POLICY_VERSION = "comsol-planning/v6"
RESEARCH_POLICY_VERSION = "comsol-research/v12"
COMMON_POLICY_MARKER = "{{COMMON_PLANNING_POLICY}}"
RESEARCH_POLICY_MARKER = "{{COMSOL_RESEARCH_POLICY}}"

COMMON_PLANNING_POLICY = """先核对已给目标量、定义与单位、关注区域、材料、工况及输入数据，再提出路线。已给输入全部作为当前已知条件，不重复追问或重新列为必须提供；用户声明所需输入齐全时直接推进，不把方法缺口或实施前检查变成再次索要输入。不要照搬其他问题或案例的工况。几何事实不证明材料、域身份、边界或已有物理场；没有提供的局部尺寸、面号、设置和数值结果保持未知。
有设置表工具时，按三个阶段完成规划，不把研究、取舍与成文混在一次自由写作中：
（1）提取：先核对用户输入；有get_mechanism_checks时先调用get_mechanism_checks({})发现目录，再按目标与路线选择相关mechanisms读取完整检查提示及检索词，不盲目选全部机制。检查提示用于发现实施问题，不是来源证据、默认设置或物理事实。结合适用来源逐项处置所选检查，用check_ids记录关联；每个决定方程、本构、作用域、激励/耦合、研究激活/顺序、离散或输出定义的独立必要设置单独一行。同一检查需要多个设置就拆行，一行可关联多个紧密相关检查，不按宽泛标题打包多项配置，不凑固定行数。区分来源原值、当前输入和未知；调用save_plan_settings保存参考清单，status=unresolved、proposed_value可为空，reason说明待核实或待取舍内容。source_refs只用实际读取的精确范围，用户输入/推导可无引用。
（2）取舍：按相同id逐行决定adopted/adapted/alternative/not_applicable，未核实的保留unresolved；写明proposed_value、单位/表达式、transfer_rationale、reason、gap和拟对应step_ids，再保存表。每个需要的检查均有设置行，未知项写缺口，不适用项保留not_applicable和任务相关理由，不因来源未展示而跳过。proposed_value应是可直接实施的简短原子赋值、公式或配置，含必要作用域和条件。来源方法不能只留在引文中；不采用时说明目标/工况差异。保留已提取id，不通过改名、删除或清空掩盖未决项；有unhandled_count先补处置。替代路线说明改变的约束及等价性依据，未证明等价不声称等价。
（3）组装：先用get_plan_settings重读已选设置，再据表写步骤，不凭记忆概括。新方案使用check_ids及implementation绑定；对有关联检查的adopted/adapted/alternative行，implementation.step_id须在该行step_ids中，implementation.field选action、inputs或checks。把proposed_value完整原文放入绑定字段；inputs/checks须在同一个数组条目内，建议原样粘贴到step.inputs，并在action写实际操作。只有步骤关联或概述不算落实，不能丢失公式、作用域、条件或传递量。旧表可省略这两个新增字段，不为兼容而让新方案省略。review_plan按check_ids展开所选机制并提示遗漏检查，核对设置映射、原文落实及阅读范围后再保存。服务只检查原文保留和结构，不判定公式、物理适用性或完整性；关键条目齐全后才缩写背景描述。
用户要求复现特定路线时按其路线核对；普通新任务可用有依据的替代，不照抄案例条件。
从目标量推导主导机制、计算域、接口、本构、激励、耦合方向与研究类型；有实质选择时比较成立条件和失效条件，条件未确定时给出参数化判断。核对选定接口、本构、激励、耦合与数值方法是否兼容；区分局部量与全局量、幅值与有效值等定义，并使表达式、单位和输出目标一致。不要因某个机制相似就把整套设置视为适用。
给出可实施的几何域与定位方法、材料、边界/初始条件、变量和耦合传递、网格、研究/求解顺序、目标量提取、验证与风险；具体程度按任务相关性决定。网格联系实际局部几何和物理尺度，说明局部加密、质量修复及目标量的离散误差检查；数值收敛和物理可信度分别验证。
交付前按当前公开目标、模型事实、用户回答和选定路线逐项建立自检清单；把每个要求交付的目标量、已给工况和关键输入对应到具体实施步骤，核对设置位置、域/边界定位、实际动作、表达式与单位、耦合的来源/接收量和传递顺序、研究/求解配置、结果提取定义及验收方法。仅写“考虑某机制”“添加某接口”或“核对单位”不等于已交代如何实现；选择不适用的环节须给出与当前路线一致的理由。清单和比较条件由本任务推导，不套用其他案例的物理设置或固定答案。
自检目标量→机制→域→接口/本构→激励/耦合→网格→研究/求解→结果/验证的一致性；review_plan只提示结构、覆盖和证据账本缺口，不能替代物理判断。区分用户输入缺口、方法尚未核实之处和实施前检查：已给数据不重复追问；方法依据不足写evidence_gaps并说明影响，实施前检查写在步骤中，不全转成用户必答问题。未执行的检查写成计划，不声称已求解或已验证。
保存前读取最新任务和版本。若save_plan因格式或字段约束失败，保留已编写内容，使用get_plan_draft读取草稿及具体错误，再用repair_plan_draft只修复出错字段；不得为通过格式验证而压缩成空泛短方案、清空引用或删除仍有效的实施细节。若当前工具未提供草稿修复能力，则依据具体错误修复原提交并重试。"""

RESEARCH_POLICY = """inspect_case_source 的非空XML正文页若返回 original_mph_xml 凭据，可原样用于该已读原件范围；它与保存/API配置的 case_configuration 身份不同。children元数据不授予正文信用。
先从用户目标、已给输入和拟选路线识别需要证据判断的问题，以原文章节和案例定位为起点，按相关性缩小资料库、模块、章节和案例范围，再阅读必要正文。方法卡或检查提示只作可选导航，不是来源证据；不强制使用方法卡、构建全依赖图或遍历全部设置。
只使用当前入口实际提供的工具及其schema。资料库范围可能已绑定并冻结，未提供范围查询或启停工具时，直接使用当前授权范围，不猜测工具或尝试改变范围。若提供get_library_scope，可先查看目录及启停状态，按相关性选择库；query是目录元数据的字面子串筛选，不是语义检索。筛选零匹配时查看global_library_count，必要时调用get_library_scope({})重新发现，不能据此声称全目录为空。仅在当前工具允许且任务需要时启用指定库，并等待成功后检索；启用与检索有先后依赖，不能并行。不得自动启用全部库。关闭库、heldout/隔离测试来源、评测答案及其他未授权来源不可读取，也不能经关联来源绕过访问边界。
需求和范围明确后，若当前提供get_html_navigation和read_html_section，优先定位功能手册中的父接口和合理候选方法，再读取适用条件、假设及关键量传递的原始正文。结合祖先标题区分不同接口下的同名特征，只按所需问题展开parent、children、siblings及相邻章节；next_offset结束只表示当前页或锚点正文结束，不代表整章已读。不强制遍历整本，也不规定固定读取次数。
QMD继续用于补充术语、表达式、实现代码和相近案例，或缩小手册定位范围；可将已发现的source_id交给get_html_navigation返回原生章节树。树未准备、来源没有HTML节点或入口不提供树工具时，使用当前可用的search_knowledge和read_source，不把not_prepared当作没有相关方法。树优先不禁用QMD，默认使用qmd_keyword，不自动构建向量或启用embedding。QMD仅覆盖当前授权、已登记并启用资料库的已建索引，不能把试点索引当作全部手册。功能手册树不开放模型实例教程、addins或隔离测试来源；案例仍按各自现有权限查阅。
按工具schema用library_ids、kind及可选feature_branch缩小范围。查询优先用短而有区分性的英文机制、接口或特征词，不堆叠版本号、完整任务长句或多个OR分支，不同问题分开查询。kind=manual可查定义，kind=all可补充案例配置和关联教程；kind=case会排除手册型教程，混合命中不能替代案例概要发现。feature_branch只筛已有分支标注，未标注的通用手册和模型概览会被排除；初查定义或概览默认不带分支，schema要求保留字段时用null。结果偏题、只有目录/泛理论或无关领域时，依据实际命中改写术语、收窄库/章节/案例，或沿children、parent、linked_topics定位具体正文，不重复堆叠宽泛查询。
用search_cases找相近案例概要，先read_source读概览判断目标、工况与方法是否适用；不适用时明确排除并说明与当前任务的差异，无需为排除而遍历配置。对相近或部分可借鉴的案例，在选路和设置迁移过程中用精确case_id调用get_case_navigation定位所需节点，按实际返回的read_requests或configuration_reads调用read_case_configuration读取实际设置；用manual_read或精确HTML节点调用read_html_section核对必要定义。case_id使用search_cases返回的精确id，不猜asset_key或路径。get_case_guide保留环节摘要入口，摘要不代表全部节点或完整依赖；工具缺失或not_prepared时，按当前提供的get_case_guide、search_knowledge(case_id/stage)等入口继续定位。若提供inspect_case_source，可直接用于原始保存结构、表达式与选择集的查证；依其schema使用返回的节点路径，逐层或按页读取原始MPH内XML，无需预建树或运行COMSOL；它只证明实际所见保存XML的原生位置和文件指纹，不授予case_configuration或API观察证据信用。已有保存/API配置可用read_case_configuration核对。原件检查与配置读取均可参与候选路线比较，不必等到路线已经选定后才使用；按当前证据问题选择合适入口。
案例树只覆盖当前已经解析的节点，不承诺整个MPH已被完整解析，父子顺序不是物理因果图。只追踪影响所选设置的必要参数、函数、变量、选择集、材料、耦合及研究/求解依赖，不要求读取全图。confirmed仅表示配置中观察到可解析关系，ambiguous和unresolved保留待核实原因，不把未知表达式中的每个词猜作用户变量。保存配置与API观察分别表述；相关依赖不可读时保留缺口，不推断默认值。若当前工具支持，可从手册节点以get_case_navigation的implementations视图及manual_node_id反查可访问案例；关联、matched、candidate都只是候选，不证明适用性。
以机制、目标量、接口/特征和条件共同判断相关性。搜索命中、树标题、关联摘要及get_html_navigation、get_case_navigation和get_case_guide导航都不算正文或设置证据；来源候选、父子关系、依赖边和手册匹配不证明物理有效性。引用前用read_html_section、read_source、inspect_case_source或read_case_configuration读取所需正文、原件或实际设置。查看source_context、祖先标题和实际范围，区分通用手册、教程、模型概览、保存配置、API观察与实现代码。saved_configuration正文若只是结构摘要，按configuration_hint读取实际属性；preview_only或未执行代码不能冒充已核验模型。
引用遵循当前工具实际返回的来源身份和定位；返回服务生成的evidence_refs时，需要source_refs就精确复制其中source_id/context/locator（单个属性可用返回的evidence_locator），stated_value只填写实际读到的内容，不猜定位。read_html_section的html_section引用保留原文件指纹、锚点和实际文本范围；真实读取后可引用返回的同一source_id，无需为登记引用重复QMD读取。HTML原文、source_text页段、原始保存XML、配置属性、保存值与API观察不能相互冒充。图片和公式图只保留引用，未转录的图像不能当作已读公式。检查next_offset，按当前问题需要续读；只把实际读过的范围称为已核对，服务接受引用范围也不证明值正确或可迁移。
用适用正文完成路线的条件约束判断：比较候选方法保留的现象、成立条件与失效条件，核对关键量从产生方到接收方的作用域、定义、方向和单位；方法存在或案例曾用过均不证明当前组合成立。选定路线后，按任务需要核对接口/特征作用域、材料本构、激励、关键表达式、耦合、研究激活/顺序、数值方法及目标量提取，使实施细则有证据支持。区分来源原值、当前输入、工程推断和未知；未核实的数值、表达式和默认值保持未知，不编数值、不照搬案例工况。网格细节与工具读取由实际几何、物理尺度及问题需要决定，不强迫调用网格工具。
按未解决的问题与剩余预算决定补查，没有固定调用次数、案例数量或依赖覆盖要求。需要判断的条件和关键实现主张已有适用证据，或已明确剩余未知及其影响时，可结束研究；证据不足的主张保留gap，影响路线成立时给出条件或阻塞。没有相近案例时说明实际搜索范围、差异和缺少的依据，不凑案例、不虚构支持，也不把查文档变成用户必答问题。替代路线说明改变的约束、机制或目标量定义及等价性是否已证实；引用数量不等于质量，不以删除引用掩盖未核实的方法。"""

_RESEARCH_MODE_FOCUS = {
    "route": "本次聚焦路线判断：依据目标量、已给条件与已读来源比较候选路线的成立条件、失效条件、取舍和关键证据缺口。相关案例的实际实现可用于检验候选路线和关键关系；明显不适用案例可说明差异后排除。最终集中交付路线判断，不展开完整实施步骤。",
    "implementation": "本次聚焦实施规划：形成工程师或后续编码Agent可执行的完整方案，技术路线是其中一部分。先以手册和相关案例的真实实现比较候选路线，再按实施依赖组织几何准备与简化、域/边界选择、材料和参数表达式、物理接口、激励边界、跨域耦合、网格、研究/求解、结果提取与验收。每个相关环节说明操作对象、关键设置、依赖、依据、检查与难点；说明案例设置的采用、修改或排除理由。不能用接口名或泛泛待确认替代实施细节。网格需要划分及加密策略、质量和独立性检查；尺寸或阈值未知时给出确定方法、参数化条件和影响，不编数值。未运行求解不等于可以省略实施设计。",
}

LIBRARY_SCOPE_DESCRIPTION = (
    "查询资料库目录、分组、启停状态及轻量根路径；包括已关闭库，不读正文或索引。query是元数据字面子串筛选；"
    "library_count/matched_library_count仅为匹配数，global_library_count才是完整目录数量。筛选零匹配时调用get_library_scope({})查看目录；不自动启用全部库。"
    "官方/自定义类型来自注册声明。全局查询，无需方案。"
)
REVIEW_PLAN_DESCRIPTION = (
    "审查当前或提供的方案草稿：列出公开需求、每步显式实施字段及引用账本缺口，可附可选需求到步骤的自检映射。"
    "按check_ids展开所选机制的检查并提示遗漏，核对implementation绑定及proposed_value原文保留。"
    "仅做非阻塞结构审查，不验证物理正确性；未映射不等于未满足，方法缺口不等于缺少用户输入。"
)

SEARCH_KNOWLEDGE_DESCRIPTION = (
    "检索当前授权范围内的本地手册、案例配置及代码。提供HTML工具时，功能手册优先用get_html_navigation定位父接口并用read_html_section读原文；本工具补充术语、表达式和案例，或在树不可用时回退。可用命中source_id返回HTML树。案例先search_cases并read_source读概览判断适用性；对相近案例可在选路和实施设计过程中用get_case_navigation定位配置，或用inspect_case_source直接检查原始保存结构，get_case_guide保留摘要。可用case_id与stage限定所需配置和关联教程，kind=all允许两种来源。kind=manual核对通用方法；"
    "可选feature_branch仅筛已有分支标注，未标注的通用手册/模型概览也会排除；查定义/概览初次默认不带分支，必要时再筛。"
    "优先短而有区分性的英文机制/接口词，避免堆叠版本号、任务长句或多个OR分支；结果偏题时改写查询并收窄范围。library_ids限定已启用库。默认qmd_keyword，项目术语扩展后以OR执行一次BM25；qmd_vector需显式选择，默认关闭。QMD仅覆盖已登记并启用库的已建索引；不可用时明确报告。"
    "结果只是候选，source_context提示来源角色和所属案例，不能视为适用或已读；引用前须用read_html_section、read_source、inspect_case_source或read_case_configuration核对实际正文、原件或设置。"
)
READ_SOURCE_DESCRIPTION = (
    "分页读取已发现来源及其source_context、目录父子和链接导航。功能手册可用source_id转get_html_navigation，再read_html_section核对父接口原文；无树时继续按本工具分页。案例先读概览判断适用条件，"
    "saved_configuration正文可能只是简短结构摘要，须检查configuration_hint并用read_case_configuration读取实际属性；不要将模板摘要作为设置原值。"
    "相关案例的原件和配置可在选路及实施设计过程中查证关键机制、作用域与设置迁移，不适用案例可说明差异后排除；若是目录/概论，沿children/parent/linked_topics找具体正文。"
    "related_sources只是依赖摘要，需另行read_source核实；检查next_offset，未读范围不能称已核对。"
)


def text_sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def research_instructions(task_mode="implementation"):
    """Return the canonical research policy once, with a task-specific focus.

    This research-only text is safe to reuse in read-only route transports; it
    does not include the product's plan-writing or submission contract.
    """
    if task_mode not in _RESEARCH_MODE_FOCUS:
        raise ValueError("task_mode must be 'route' or 'implementation'")
    return RESEARCH_POLICY + "\n\n" + _RESEARCH_MODE_FOCUS[task_mode]


def load_planner_instructions(path):
    """Render the portable product template with the canonical shared policy."""
    template = Path(path).read_text(encoding="utf-8")
    for marker, value in ((COMMON_POLICY_MARKER, COMMON_PLANNING_POLICY),
                          (RESEARCH_POLICY_MARKER, research_instructions())):
        if template.count(marker) != 1:
            raise ValueError("The planner template must contain each shared policy marker exactly once")
        template = template.replace(marker, value)
    return template
