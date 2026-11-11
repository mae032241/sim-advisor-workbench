"""Shared, task-independent planning and evidence-research requirements.

The product and both benchmark transports import these exact policies. Keep
benchmark questions, references and evaluation diagnostics out of this module.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


PLANNING_POLICY_VERSION = "comsol-planning/v2"
RESEARCH_POLICY_VERSION = "comsol-research/v3"
COMMON_POLICY_MARKER = "{{COMMON_PLANNING_POLICY}}"
RESEARCH_POLICY_MARKER = "{{COMSOL_RESEARCH_POLICY}}"

COMMON_PLANNING_POLICY = """先核对已给目标量、定义与单位、关注区域、材料、工况及输入数据，再提出路线。已给输入优先；不要把已有数据重新列为用户必须提供，也不要照搬其他问题或案例的工况。几何事实不证明材料、域身份、边界或已有物理场；没有提供的局部尺寸、面号、设置和数值结果保持未知。
从目标量推导主导机制、计算域、接口、本构、激励、耦合方向与研究类型；有实质选择时比较成立条件和失效条件，条件未确定时给出参数化判断。核对选定接口、本构、激励、耦合与数值方法是否兼容；区分局部量与全局量、幅值与有效值等定义，并使表达式、单位和输出目标一致。不要因某个机制相似就把整套设置视为适用。
给出可实施的几何域与定位方法、材料、边界/初始条件、变量和耦合传递、网格、研究/求解顺序、目标量提取、验证与风险；具体程度按任务相关性决定。网格联系实际局部几何和物理尺度，说明局部加密、质量修复及目标量的离散误差检查；数值收敛和物理可信度分别验证。
交付前自检目标量→机制→域→接口/本构→激励/耦合→网格→研究/求解→结果/验证的一致性。区分用户输入缺口、方法尚未核实之处和实施前检查；缺数据列明影响与检查方法，未执行的检查写成计划，不声称已求解或已验证。"""

RESEARCH_POLICY = """先把需求和拟选路线拆成可核查的实施问题，再检索与阅读。分别用 kind=manual 查接口、特征和方法定义，用 kind=case 查相近官方/用户案例；kind=all 的混合命中不能替代对案例范围的针对性检索。相关代码可用 kind=code 补查。按工具 schema 使用 library_ids、kind 和可选 feature_branch；分支 geometry、mesh、physics、material、multiphysics、selection、study、solver、result、param、func、variable、misc 只是范围限制，不证明适用性。feature_branch 只筛已有分支标注，未标注条目（包括通用手册和模型概览）将被排除；查通用定义或案例概览时，初次默认不带分支，必要时再筛。schema要求保留字段时用 null 表示不筛分支。查询优先用短而有区分性的英文机制、接口或特征词；避免堆叠版本号、完整任务长句或多个 OR 分支，不同实施问题分开精准查询。
以机制、目标量、接口/特征和条件共同判断相关性，不能只按标题或关键词选源。搜索命中只是候选，引用前必须 read_source。查看 source_context 的来源角色、所属案例、包、分支及状态等实际字段，区分通用手册、教程、模型概览、保存配置、API观察与实现代码；关联摘要也不算已读。
有相关案例时，阅读其概览以核对目标、适用工况与建模路线，再阅读所选路线相关的具体配置及必要依赖。沿 related_sources 核对参数、函数、变量、选择集等被实际设置引用的正文；若依赖不可读或不完整，保留缺口，不推断默认值。区分物理类比、可直接迁移设置和必须改变的条件，不能把别的案例整套接口、材料、激励或求解器套入当前任务。原生保存配置、API解析、教程描述与工程推断分别表述；preview_only 或未执行代码不冒充已核验模型。
若结果是目录、概论、泛理论或无关领域，沿 children、parent、linked_topics 和相关案例导航到具体正文，或用更精准的英文接口名、特征名和物理术语改写查询。检查 next_offset，按需要续读；只读过的范围才称已核对。native 与 QMD 模式只覆盖各自已建索引，不能把一个试点索引当作全部手册。
对已选路线，用适用正文核对接口/特征作用域、材料本构、激励定义、关键表达式与单位、耦合变量和方向、研究顺序及数值方法限制；特别核对局部与全局量及后处理定义的一致性。手册说明某功能存在、案例使用过某设置，都不单独证明该组合适合当前条件。
按实施问题和剩余预算分配检索与读取，没有固定读取次数或案例数量要求。若没有相近案例，说明实际搜索范围、发现的差异及仍缺哪些依据；不要凑无关案例。找不到方法依据时列为尚未核实并给出有针对性的补查方向，不把查文档转成用户必答问题，也不虚构支持。
最终方案把具体主张与实际读取的适用来源对应，说明证据支持哪些设置、哪些条件不适用或尚未核实；重要差异与证据缺口影响路线时保留条件或阻塞。引用数量不等于方案质量，不以删除引用来掩盖关键方法未核实。"""

SEARCH_KNOWLEDGE_DESCRIPTION = (
    "检索本地手册、官方/用户案例及代码。分别用kind=manual核对方法、kind=case查相近案例；"
    "可选feature_branch仅筛已有分支标注，未标注的通用手册/模型概览也会排除；查定义/概览初次默认不带分支，必要时再筛。"
    "优先短而有区分性的英文机制/接口词，避免堆叠版本号、任务长句或多个OR分支。library_ids限定已启用库。native/QMD覆盖各自已建索引。"
    "结果只是候选，source_context提示来源角色和所属案例，不能视为适用或已读；引用前必须read_source。"
)
READ_SOURCE_DESCRIPTION = (
    "分页读取已发现来源及其source_context、目录父子和链接导航。案例先读概览判断适用条件，"
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
