"""Shared, task-independent planning and evidence-research requirements.

The product and both benchmark transports import these exact policies. Keep
benchmark questions, references and evaluation diagnostics out of this module.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


PLANNING_POLICY_VERSION = "comsol-planning/v6"
RESEARCH_POLICY_VERSION = "comsol-research/v8"
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

RESEARCH_POLICY = """先把需求和拟选路线拆成可核查的实施问题，再检索与阅读。先用get_library_scope查看资料库目录和启停状态，按任务需要显式启用指定库并等待返回成功后再检索；启用与检索有先后依赖，不能放入Promise.all/并行批次。并行只用于互不依赖的读取或查询。query是目录元数据的字面子串筛选，不是语义检索。筛选零匹配不表示全目录为空，应查看global_library_count并调用get_library_scope({})重新发现；不得因此声称无可用资料，也不得自动启用全部库。核对case_context_dependencies等实际依赖提示，按需要选择相关来源库。
按任务所选机制检查拆出原子实施问题，用其检索词寻找依据；检查提示本身不能填source_refs。阅读适用案例时逐项登记决定方程/特征作用域、材料本构、边界激励、耦合传递、研究激活/顺序与数值离散的独立设置，明确采用、调整、不适用或尚未核实，再生成当前任务流程。避免只摘录接口名和概要；来源中出现的重要设置若不采用，应说明与当前需求的差异。
read_source和read_case_configuration返回服务生成的evidence_refs；source_refs精确复制其中source_id/context/locator（引用单个属性时locator可用该属性返回的evidence_locator），再填写实际读到的stated_value。正文页段与配置节点/属性、保存值与API观察不能相互冒充；不要自己猜locator，get_case_guide导航不算设置证据。服务匹配阅读范围也不证明你抄写的值正确或可迁移，仍需逐项核对。尚未读到的候选不要填成已核实设置依据，保留gap并有针对性补读。表已保存可用get_plan_settings分页查看，save_plan会把当前版本暂存表与流程一起保存。
用 search_cases 找相近案例概要，避免Frame等内部节点挤占发现结果；用 search_knowledge kind=manual 查接口、特征和方法定义。已选案例可 get_case_guide 获取实现流程，再用 search_knowledge 的 case_id 与 stage 定位该案例配置及关联教程，或 read_case_configuration 读取原生保存/API配置。case_id必须使用search_cases结果的精确id，不使用asset_key或文件路径。not_prepared不是无案例，可read_case_configuration按category读取配置，并读取相关教程/手册；不要因导航未准备而跳过实际配置。导航提示不算正文已读，不能直接作为配置已核实的依据。kind=all 的混合命中不能替代案例发现；相关代码可用 kind=code 补查。按工具 schema 使用 library_ids、kind 和可选 feature_branch；分支 geometry、mesh、physics、material、multiphysics、selection、study、solver、result、param、func、variable、misc 只是范围限制，不证明适用性。feature_branch 只筛已有分支标注，未标注条目（包括通用手册和模型概览）将被排除；查通用定义或案例概览时，初次默认不带分支，必要时再筛。schema要求保留字段时用 null 表示不筛分支。查询优先用短而有区分性的英文机制、接口或特征词；避免堆叠版本号、完整任务长句或多个 OR 分支，不同实施问题分开精准查询。
以机制、目标量、接口/特征和条件共同判断相关性，不能只按标题或关键词选源。搜索命中只是候选，引用前必须 read_source 或 read_case_configuration 读取所需正文或实际设置。查看 source_context 的来源角色、所属案例、包、分支及状态等实际字段，区分通用手册、教程、模型概览、保存配置、API观察与实现代码；关联摘要也不算已读。
有相关案例时，阅读其概览以核对目标、适用工况与建模路线，再使用get_case_guide(stage=...)、search_knowledge(case_id=...,stage=...)或read_case_configuration(category=...)读取所选路线相关环节的具体配置及必要依赖，不能把流程概要或功能名称当作可实施设置。按实际引用关系沿related_sources和配置/教程入口核对参数、函数、变量、选择集、耦合及研究/求解依赖的正文；若依赖不可读或不完整，保留缺口，不推断默认值。区分物理类比、可直接迁移设置和必须改变的条件，不能把别的案例整套接口、材料、激励或求解器套入当前任务。原生保存配置、API解析、教程描述与工程推断分别表述；preview_only 或未执行代码不冒充已核验模型。
若结果是目录、概论、泛理论或无关领域，沿 children、parent、linked_topics 和相关案例导航到具体正文，或用更精准的英文接口名、特征名和物理术语改写查询。检查 next_offset，按需要续读；只读过的范围才称已核对。QMD 仅覆盖已登记并启用资料库中的已建索引，不能把一个试点索引当作全部手册。
对已选路线，用适用正文核对接口/特征作用域、材料本构、激励定义、关键表达式与单位、耦合变量和方向、研究顺序及数值方法限制；特别核对局部与全局量及后处理定义的一致性。手册说明某功能存在、案例使用过某设置，都不单独证明该组合适合当前条件。
按实施问题和剩余预算分配检索与读取，没有固定读取次数或案例数量要求。方法缺口先定位缺失环节，如产生量→接收量、作用域、表达式/单位、研究激活/顺序或成立条件，再用对应机制、接口/特征词定向补查，不用无关来源填补。没有相近案例时说明实际搜索范围、差异及缺少的依据，不凑案例、不虚构支持；尚未核实的方法保留gap及其影响，不把查文档转成用户必答问题。
最终方案把具体主张与实际读取且当前可用的适用来源对应，逐个检查关键实现问题的配置/正文依据，缺失的继续补查或明确保留。重要差异与证据缺口影响路线时保留条件或阻塞。可采用有依据和成立条件的替代路线，但须写明改变的约束、机制或目标量定义，以及与原路线的等价性是否已证实；替代路线的依据不能冒充原方法已核实。引用数量不等于方案质量，不以删除引用掩盖关键方法未核实。"""

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
    "检索本地手册、案例配置及代码。找案例概要优先search_cases；选定后可用case_id与stage限定该案例配置和关联教程，kind=all允许两种来源。kind=manual核对通用方法；"
    "可选feature_branch仅筛已有分支标注，未标注的通用手册/模型概览也会排除；查定义/概览初次默认不带分支，必要时再筛。"
    "优先短而有区分性的英文机制/接口词，避免堆叠版本号、任务长句或多个OR分支。library_ids限定已启用库。默认qmd_keyword，项目术语扩展后以OR执行一次BM25；qmd_vector需显式选择，默认关闭。QMD仅覆盖已登记并启用库的已建索引；不可用时明确报告。"
    "结果只是候选，source_context提示来源角色和所属案例，不能视为适用或已读；引用前必须read_source或read_case_configuration核对实际设置。"
)
READ_SOURCE_DESCRIPTION = (
    "分页读取已发现来源及其source_context、目录父子和链接导航。案例先读概览判断适用条件，"
    "saved_configuration正文可能只是简短结构摘要，须检查configuration_hint并用read_case_configuration读取实际属性；不要将模板摘要作为设置原值。"
    "再读相关配置和必要依赖；若是目录/概论，沿children/parent/linked_topics找具体正文。"
    "related_sources只是依赖摘要，需另行read_source核实；检查next_offset，未读范围不能称已核对。"
)


def text_sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_planner_instructions(path):
    """Render the portable product template with the canonical shared policy."""
    template = Path(path).read_text(encoding="utf-8")
    for marker, value in ((COMMON_POLICY_MARKER, COMMON_PLANNING_POLICY),
                          (RESEARCH_POLICY_MARKER, RESEARCH_POLICY)):
        if template.count(marker) != 1:
            raise ValueError("The planner template must contain each shared policy marker exactly once")
        template = template.replace(marker, value)
    return template
