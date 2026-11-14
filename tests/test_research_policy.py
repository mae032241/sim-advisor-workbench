import pytest

from advisor import agent_sessions
from advisor.planning_service import tool_definitions
from advisor.research_policy import (COMMON_PLANNING_POLICY, COMMON_POLICY_MARKER,
    RESEARCH_POLICY, RESEARCH_POLICY_MARKER, SEARCH_KNOWLEDGE_DESCRIPTION,
    READ_SOURCE_DESCRIPTION, load_planner_instructions, research_instructions)
from scripts import benchmark_cli, benchmark_codex_cli, benchmark_no_library_cli


def test_product_and_benchmark_transports_share_exact_task_independent_policies():
    for instructions in (agent_sessions.INSTRUCTIONS, benchmark_cli.API_INSTRUCTIONS,
                         benchmark_codex_cli.INSTRUCTIONS, benchmark_no_library_cli.INSTRUCTIONS):
        assert instructions.count(COMMON_PLANNING_POLICY) == 1
        assert COMMON_POLICY_MARKER not in instructions
        assert RESEARCH_POLICY_MARKER not in instructions
    for instructions in (agent_sessions.INSTRUCTIONS, benchmark_cli.API_INSTRUCTIONS,
                         benchmark_codex_cli.INSTRUCTIONS):
        assert instructions.count(RESEARCH_POLICY) == 1
    assert RESEARCH_POLICY not in benchmark_no_library_cli.INSTRUCTIONS
    assert "search_knowledge" not in benchmark_no_library_cli.INSTRUCTIONS
    assert "read_source" not in benchmark_no_library_cli.INSTRUCTIONS
    assert "通常约 3 次" not in benchmark_codex_cli.INSTRUCTIONS


def test_template_renders_shared_policy_without_changing_surrounding_instructions(tmp_path):
    template = tmp_path / "planner.txt"
    template.write_text("任务边界 {literal}\n" + COMMON_POLICY_MARKER + "\n研究\n" +
                        RESEARCH_POLICY_MARKER + "\n结束", encoding="utf-8")
    assert load_planner_instructions(template) == (
        "任务边界 {literal}\n" + COMMON_PLANNING_POLICY + "\n研究\n" +
        research_instructions("implementation") + "\n结束")


def test_research_modes_share_one_body_without_product_submission_in_route_research():
    route = research_instructions("route")
    implementation = research_instructions("implementation")
    assert research_instructions() == implementation
    for instructions in (route, implementation):
        assert instructions.startswith(RESEARCH_POLICY + "\n\n")
        assert instructions.count(RESEARCH_POLICY) == 1
        assert COMMON_PLANNING_POLICY not in instructions
    assert "本次聚焦路线判断" in route
    assert "本次聚焦实施规划" not in route
    assert "本次聚焦实施规划" in implementation
    for submission_tool in ("save_plan", "save_plan_settings", "review_plan"):
        assert submission_tool not in route
    assert agent_sessions.INSTRUCTIONS.count(implementation) == 1


@pytest.mark.parametrize("task_mode", ["", "unknown", "route-only", None])
def test_research_modes_reject_unsupported_values(task_mode):
    with pytest.raises(ValueError, match="task_mode must be 'route' or 'implementation'"):
        research_instructions(task_mode)


@pytest.mark.parametrize("marker", [COMMON_POLICY_MARKER, RESEARCH_POLICY_MARKER])
@pytest.mark.parametrize("occurrences", [0, 2])
def test_template_rejects_missing_or_duplicate_shared_policy(tmp_path, marker, occurrences):
    other = RESEARCH_POLICY_MARKER if marker == COMMON_POLICY_MARKER else COMMON_POLICY_MARKER
    template = tmp_path / "planner.txt"
    template.write_text("\n".join([other] + [marker] * occurrences), encoding="utf-8")
    with pytest.raises(ValueError, match="each shared policy marker exactly once"):
        load_planner_instructions(template)


def test_staged_policy_fields_and_discovery_tool_are_exposed_by_product_contract():
    tools = {tool["name"]: tool for tool in tool_definitions()}
    guidance = tools["get_mechanism_checks"]["inputSchema"]
    assert guidance["required"] == []  # Discovery does not need a plan or selected profile.
    assert guidance["properties"]["mechanisms"]["items"]["type"] == "string"
    for name in ("get_mechanism_checks", "save_plan_settings", "get_plan_settings", "review_plan"):
        assert name in COMMON_PLANNING_POLICY
        assert name in tools
    assert all(field in COMMON_PLANNING_POLICY for field in ("check_ids", "implementation", "step_ids",
                                                            "proposed_value"))
    for name, field in (("save_plan_settings", "settings"), ("save_plan", "plan_settings"),
                        ("review_plan", "plan_settings")):
        row = tools[name]["inputSchema"]["properties"][field]["items"]
        assert {"check_ids", "implementation"}.isdisjoint(row["required"])
        assert row["properties"]["check_ids"]["items"]["type"] == "string"
        binding = row["properties"]["implementation"]
        assert set(binding["properties"]) == {"step_id", "field"}
        assert set(binding["required"]) == {"step_id", "field"}
        assert set(binding["properties"]["field"]["enum"]) == {"action", "inputs", "checks"}


def test_product_and_benchmark_tools_explain_source_context_and_navigation():
    product = {tool["name"]: tool["description"] for tool in tool_definitions()}
    benchmark = {tool["function"]["name"]: tool["function"]["description"]
                 for tool in benchmark_cli.weak_tools()}
    for name, description in (("search_knowledge", SEARCH_KNOWLEDGE_DESCRIPTION),
                              ("read_source", READ_SOURCE_DESCRIPTION)):
        assert product[name].startswith(description)
        assert benchmark[name] == description


def test_saved_evidence_summaries_bound_context_and_exclude_unrelated_payloads():
    source = {"id": "known", "kind": "case", "library_id": "enabled", "text": "x" * 4000,
              "source_context": {"role": "saved_configuration", "case_identity": "a" * 1000,
                                 "physics": ["solid"] * 50, "secret": "MUST_NOT_PERSIST"},
              "access_token": "MUST_NOT_PERSIST", "raw": {"api_key": "MUST_NOT_PERSIST"}}
    result = benchmark_cli.source_ledger_summary(source)
    assert len(result["content_excerpt"]) == 500
    assert len(result["source_context"]["case_identity"]) == 240
    assert len(result["source_context"]["physics"]) == 8
    assert "MUST_NOT_PERSIST" not in str(result)


def test_shared_research_prefers_native_parent_interface_then_original_html_with_qmd_fallback():
    assert RESEARCH_POLICY.index('get_library_scope') < RESEARCH_POLICY.index('get_html_navigation')
    assert RESEARCH_POLICY.index('get_html_navigation') < RESEARCH_POLICY.index('read_html_section')
    for phrase in ('父接口', '候选方法', '祖先标题', 'siblings', 'next_offset', '不强制遍历整本',
                   '不规定固定读取次数', 'QMD继续用于', '树优先不禁用QMD', 'not_prepared',
                   '不自动构建向量或启用embedding', 'search_cases', 'get_case_guide', 'read_case_configuration'):
        assert phrase in RESEARCH_POLICY
    assert '模型实例教程、addins或隔离测试来源' in RESEARCH_POLICY
    assert '产生方到接收方' in RESEARCH_POLICY and '成立条件与失效条件' in RESEARCH_POLICY
    assert 'html_section' in RESEARCH_POLICY and '无需为登记引用重复QMD读取' in RESEARCH_POLICY
    assert '未转录的图像不能当作已读公式' in RESEARCH_POLICY
    for name in ('get_html_navigation', 'read_html_section'):
        assert name in agent_sessions.INSTRUCTIONS
        assert name in {tool['name'] for tool in tool_definitions()}


def test_case_research_uses_overview_then_structure_and_observed_dependency_reads():
    sequence = RESEARCH_POLICY.index('用search_cases找相近案例概要')
    excerpt = RESEARCH_POLICY[sequence:RESEARCH_POLICY.index('以机制、目标量', sequence)]
    assert excerpt.index('先read_source读概览') < excerpt.index('调用get_case_navigation')
    assert 'get_case_guide保留环节摘要' in excerpt
    for phrase in ('read_requests', 'configuration_reads', 'manual_read', 'read_case_configuration', 'read_html_section',
                   '当前已经解析的节点', '不承诺整个MPH已被完整解析', '不是物理因果图',
                   'confirmed', 'ambiguous', 'unresolved', '保存配置与API观察',
                   'implementations', 'manual_node_id'):
        assert phrase in excerpt
    assert 'get_case_navigation和get_case_guide导航都不算正文或设置证据' in RESEARCH_POLICY
    assert '来源候选、父子关系、依赖边和手册匹配不证明物理有效性' in RESEARCH_POLICY
    assert 'get_case_navigation' in agent_sessions.INSTRUCTIONS
    assert 'get_case_navigation' in {tool['name'] for tool in tool_definitions()}


def test_light_research_uses_bound_scope_and_stops_at_evidence_needs():
    for phrase in ('资料库范围可能已绑定并冻结', '未提供范围查询或启停工具时',
                   '直接使用当前授权范围', '不猜测工具', '不得自动启用全部库',
                   '关闭库、heldout/隔离测试来源', '不能经关联来源绕过访问边界',
                   '不强制使用方法卡、构建全依赖图', '默认使用qmd_keyword',
                   '按相关性缩小资料库、模块、章节和案例范围',
                   '短而有区分性的英文机制', '依据实际命中改写术语、收窄库/章节/案例',
                   '没有固定调用次数、案例数量或依赖覆盖要求',
                   '条件和关键实现主张已有适用证据', '可结束研究'):
        assert phrase in RESEARCH_POLICY


def test_configuration_reads_follow_applicability_and_preserve_unknowns():
    overview = RESEARCH_POLICY.index('先read_source读概览')
    exclusion = RESEARCH_POLICY.index('不适用时明确排除', overview)
    implementation = RESEARCH_POLICY.index('对相近或部分可借鉴的案例', exclusion)
    configuration = RESEARCH_POLICY.index('调用read_case_configuration读取实际设置', implementation)
    assert overview < exclusion < implementation < configuration
    for phrase in ('无需为排除而遍历配置', '只追踪影响所选设置的必要',
                   '未核实的数值、表达式和默认值保持未知', '不编数值',
                   '不强迫调用网格工具', 'stated_value只填写实际读到的内容',
                   '区分来源原值、当前输入、工程推断和未知', '影响路线成立时给出条件或阻塞'):
        assert phrase in RESEARCH_POLICY


def test_raw_case_inspection_is_optional_and_keeps_its_xml_evidence_identity():
    for phrase in ('若提供inspect_case_source', '可直接用于原始保存结构、表达式与选择集的查证',
                   '依其schema使用返回的节点路径', '逐层或按页读取原始MPH内XML',
                   '无需预建树或运行COMSOL', '不授予case_configuration或API观察证据信用',
                   '已有保存/API配置可用read_case_configuration核对',
                   '原件检查与配置读取均可参与候选路线比较', '不必等到路线已经选定后才使用',
                   '原始保存XML、配置属性、保存值与API观察不能相互冒充'):
        assert phrase in RESEARCH_POLICY
