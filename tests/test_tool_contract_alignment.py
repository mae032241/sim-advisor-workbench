"""Exercise the actual schemas, discovery and draft repair without inference."""
import copy
import json

import pytest

from advisor.agent_sessions import AgentSessions
from advisor.plan_drafts import PlanDraftError, schema_errors
from advisor.planning_service import PlanningService, tool_definitions
from advisor.sessions import SessionStore
from advisor.tool_contracts import ToolContractError, read_contract, schema_node
from test_agent_sessions import payload
from test_workflow import FakeKB, FakePlanner, GOAL, SOURCE


@pytest.fixture
def service(tmp_path):
    kb = FakeKB()
    sessions = SessionStore(tmp_path, kb, FakePlanner())
    task = sessions.create(GOAL, None)
    return PlanningService(tmp_path, sessions, kb), sessions, task


def test_contract_lookup_is_read_only_and_needs_no_session(service):
    tools, sessions, task = service
    old = copy.deepcopy(sessions.get(task['id']))
    result = tools.call_public('get_tool_contract', {
        'tool': 'save_plan', 'path': '/design_brief/simplification/candidates/0/status'})
    assert result['expected']['enum'] == ['conditional', 'needs_inspection', 'keep']
    assert json.loads(result['text'])['enum'] == result['expected']['enum']
    assert result['read_authority'] == 'none'
    assert tools.read_sources(task['id']) == []
    assert sessions.get(task['id']) == old
    assert not (tools.data_dir / 'plan-drafts').exists()


@pytest.mark.parametrize('bound', [False, True])
def test_every_catalog_contract_can_be_reassembled_exactly(bound):
    definitions = tool_definitions(bound=bound, include_alias=True)
    for definition in definitions:
        offset, pieces = 0, []
        while True:
            result = read_contract(definitions, {'tool': definition['name'], 'offset': offset, 'length': 997})
            pieces.append(result['text'])
            assert result['returned_characters'] == len(result['text'])
            if result['next_offset'] is None:
                break
            assert result['next_offset'] > offset
            offset = result['next_offset']
        assert json.loads(''.join(pieces)) == definition['inputSchema']


def test_public_and_bound_contracts_match_their_own_session_binding(service):
    tools, _sessions, task = service
    public = tools.call_public('get_tool_contract', {'tool': 'save_plan'})
    bound = tools.call(task['id'], 'get_tool_contract', {'tool': 'save_plan'})
    assert 'session_id' in public['expected']['required']
    assert 'session_id' not in bound['expected']['required']
    assert public['schema_sha256'] != bound['schema_sha256']
    for definition in tool_definitions():
        if definition['name'] == 'get_tool_contract':
            assert 'session_id' not in definition['inputSchema']['properties']


@pytest.mark.parametrize('name', [t['name'] for t in tool_definitions(bound=True)
                                  if t['name'] != 'save_plan'])
def test_bound_and_public_reject_unknown_fields_before_any_effect(service, name):
    tools, sessions, task = service
    supplied = {'__unexpected': True}
    with pytest.raises((ToolContractError, PlanDraftError)) as bound:
        tools.call(task['id'], name, supplied)
    public_args = dict(supplied)
    if name != 'get_tool_contract':
        public_args['session_id'] = task['id']
    with pytest.raises((ToolContractError, PlanDraftError)) as public:
        tools.call_public(name, public_args)
    get_pairs = lambda error: {(e['path'], e['code']) for e in error.errors}
    assert get_pairs(bound.value) == get_pairs(public.value)
    assert sessions.get(task['id'])['revision'] == 1
    assert tools.read_sources(task['id']) == []


def test_actual_read_defaults_work_identically_and_expose_real_tail_length(service):
    tools, _sessions, task = service
    unbound = tools.call_public('read_source', {'id': SOURCE['id']})
    bound = tools.call(task['id'], 'read_source', {'id': SOURCE['id']})
    assert unbound == bound
    assert bound['offset'] == 0 and bound['length'] == 12000
    assert bound['returned_characters'] == len(bound['text'])
    assert bound['next_offset'] is None


@pytest.mark.parametrize('length', [True, 499, 16001, '500'])
def test_invalid_page_length_has_exact_expected_constraints(service, length):
    tools, _sessions, task = service
    for caller in (lambda: tools.call(task['id'], 'read_source', {'id': SOURCE['id'], 'length': length}),
                   lambda: tools.call_public('read_source', {'id': SOURCE['id'], 'length': length})):
        with pytest.raises(ToolContractError) as invalid:
            caller()
        error = next(e for e in invalid.value.errors if e['path'] == '/length')
        assert error['expected']['type'] == 'integer'
        assert error['expected']['minimum'] == 500
        assert error['expected']['maximum'] == 16000
    assert tools.read_sources(task['id']) == []


def test_description_bounds_are_generated_from_actual_schema():
    for definition in tool_definitions(bound=True):
        assert 'get_tool_contract' in definition['description']
        for name, spec in definition['inputSchema']['properties'].items():
            if spec.get('type') in ['string', 'integer', 'boolean', 'number']:
                for key in ['minimum', 'maximum', 'default', 'enum']:
                    if key in spec:
                        assert json.dumps({key: spec[key]}, ensure_ascii=False, separators=(',', ':'))[1:-1] in definition['description']


def test_model_can_repair_wrong_enum_once_using_returned_contract(service):
    tools, sessions, task = service
    tools.call(task['id'], 'read_source', {'id': SOURCE['id']})
    submitted = payload(task)
    original_action = submitted['plan']['steps'][0]['action']
    submitted['design_brief']['candidate_routes'][0]['status'] = 'invented'
    with pytest.raises(PlanDraftError) as failed:
        tools.call(task['id'], 'save_plan', submitted)
    error = failed.value.errors[0]
    assert error['path'] == '/design_brief/candidate_routes/0/status'
    assert error['expected']['enum'] == ['candidate', 'recommended', 'blocked']
    result = tools.call(task['id'], 'get_tool_contract', {'tool': 'save_plan', 'path': error['path']})
    assert result['expected'] == error['expected']
    repaired = tools.call(task['id'], 'repair_plan_draft', {
        'draft_id': failed.value.draft_id, 'draft_revision': failed.value.draft_revision,
        'patches': [{'op': 'replace', 'path': error['path'], 'value': 'recommended'}]})
    assert repaired['saved']
    assert sessions.get(task['id'])['plan']['steps'][0]['action'] == original_action


def test_unobserved_simplification_does_not_force_unrelated_citations(service):
    tools, sessions, task = service
    tools.call(task['id'], 'read_source', {'id': SOURCE['id']})
    submitted = payload(task)
    submitted['design_brief']['simplification'].update(protected_features=['Preserve target-relevant geometry'], candidates=[{
        'id': 'check_symmetry', 'target': 'whole model', 'action': 'Inspect symmetry before reducing dimension',
        'rationale': 'Potential cost reduction', 'applicability': 'Requires geometric and loading symmetry',
        'expected_benefit': 'Fewer degrees of freedom', 'lost_effects': ['Asymmetric behavior'],
        'geometry_evidence': ['Geometry topology has not been inspected'], 'geometry_fact_refs': [],
        'target_scope': 'global', 'source_ids': [], 'verification': ['Compare target quantities before adoption'],
        'status': 'conditional', 'requires_confirmation': True}])
    result = tools.call(task['id'], 'save_plan', submitted)
    assert result['saved']
    candidate = sessions.get(task['id'])['design_brief']['simplification']['candidates'][0]
    assert candidate['status'] == 'needs_inspection' and candidate['source_ids'] == []


def test_unique_items_and_numeric_types_use_shared_validator():
    errors = schema_errors(['same', 'same'], {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string'}})
    assert errors[0]['code'] == 'uniqueItems'
    assert schema_errors(True, {'type': 'integer'})[0]['code'] == 'type'


def test_exact_contract_path_rejects_guessed_fields(service):
    tools, _sessions, _task = service
    with pytest.raises(ToolContractError):
        tools.call_public('get_tool_contract', {'tool': 'save_plan', 'path': '/design_brief/description'})


@pytest.mark.parametrize('name,args,error_path', [
    ('get_plan_draft', {}, '/draft_id'),
    ('repair_plan_draft', {'draft_id': 'a'*32, 'draft_revision': 1,
                          'patches': [{'op': 'invented', 'path': '/plan/title'}]}, '/patches/0/op'),
])
def test_draft_argument_error_points_to_the_correct_tool_contract(service,name,args,error_path):
    tools, _sessions, task = service
    with pytest.raises(PlanDraftError) as invalid:
        tools.call(task['id'],name,args)
    assert invalid.value.to_dict()['contract']['arguments']['tool'] == name
    rule = tools.call(task['id'],'get_tool_contract',{'tool':name,'path':error_path})
    expected = next(error['expected'] for error in invalid.value.errors if error['path']==error_path)
    assert rule['expected'] == expected


@pytest.mark.parametrize('tool,path,boundary', [
    ('get_library_scope','/query',2000),
    ('set_libraries_enabled','/library_ids/0',100),
])
def test_library_contract_exposes_real_backend_limits(service,tool,path,boundary):
    tools, _sessions, _task = service
    rule = tools.call_public('get_tool_contract',{'tool':tool,'path':path})
    assert rule['expected']['maxLength']==boundary
    arguments={'query':'x'*(boundary+1)} if tool=='get_library_scope' else {'library_ids':['x'*(boundary+1)],'enabled':True}
    with pytest.raises(ToolContractError) as invalid:
        tools.call_public(tool,arguments)
    assert any(error['path']==path and error['expected']['maxLength']==boundary for error in invalid.value.errors)
