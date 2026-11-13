"""Backend experiments change execution mode, never product prompts or scopes."""
import copy
import json
from types import SimpleNamespace

import pytest

from advisor.agent_sessions import AgentSessions, INSTRUCTIONS, planning_tools
from advisor.planning_service import PlanningService, tool_definitions
from scripts.benchmark_backend_policy import BackendPolicyError, install_backend_policy, policy_description
from scripts import benchmark_product_batch as batch


class Knowledge:
    def __init__(self, actual=None, empty=False):
        self.actual, self.empty, self.calls = actual, empty, []
    def search(self, query, limit=8, kind='all', filters=None):
        self.calls.append(copy.deepcopy({'query': query, 'limit': limit, 'kind': kind, 'filters': filters}))
        if self.empty:
            return []
        actual = self.actual or filters['mode']
        return [{'id': 'source-one', 'title': 'Mesh settings', 'kind': 'case',
                 'retrieval': {'backend': actual, 'score': .8, 'generation': 'existing'}}]


def service(tmp_path, **kwargs):
    kb = Knowledge(**kwargs)
    return PlanningService(tmp_path, SimpleNamespace(), kb), kb


@pytest.mark.parametrize('backend,requested', [('qmd_keyword', 'native'), ('qmd_keyword', 'qmd_vector'), ('qmd_keyword', 'qmd_keyword')])
@pytest.mark.parametrize('entry', ['call', 'call_public'])
def test_common_dispatcher_fixes_mode_and_keeps_scope_and_arguments(tmp_path, backend, requested, entry):
    tools, kb = service(tmp_path)
    records = []
    before_schema, before_tools, before_prompt = copy.deepcopy(planning_tools()), copy.deepcopy(tool_definitions()), INSTRUCTIONS
    description = install_backend_policy(tools, backend, on_search=records.append)
    arguments = {'query': 'mesh size', 'kind': 'case', 'mode': requested,
                 'library_ids': ['b', 'b', 'a'], 'feature_branch': 'mesh'}
    original = copy.deepcopy(arguments)
    if entry == 'call':
        result = tools.call(None, 'search_knowledge', arguments)
    else:
        result = tools.call_public('search_knowledge', arguments)
    assert arguments == original
    assert result['mode'] == backend and result['results'][0]['retrieval']['backend'] == backend
    assert kb.calls == [{'query': 'mesh size', 'kind': 'case', 'limit': 6,
                         'filters': {'mode': backend, 'feature_branch': 'mesh', 'library_ids': ['b', 'a']}}]
    assert records[0]['requested_mode'] == ('qmd_keyword' if entry == 'call_public' and requested == 'native' else requested)
    assert records[0]['effective_mode'] == backend
    assert records[0]['observed_backends'] == [backend] and records[0]['success']
    assert records[0]['backend_validation'] == 'verified_hits'
    assert description['instructions_changed'] is False and description['tool_schema_changed'] is False
    assert planning_tools() == before_schema and tool_definitions() == before_tools and INSTRUCTIONS == before_prompt


def test_default_agent_policy_leaves_original_dispatcher_and_behavior_untouched(tmp_path):
    tools, kb = service(tmp_path)
    call, search = tools.call, kb.search
    records = []
    assert install_backend_policy(tools, 'agent', on_search=records.append)['fixed'] is False
    assert tools.call == call and kb.search == search
    assert tools.call(None, 'search_knowledge', {'query': 'mesh', 'mode': 'native'})['mode'] == 'qmd_keyword'
    assert records == []


@pytest.mark.parametrize('invalid', ['qmd_vector', 'qmd', '', None])
def test_illegal_experiment_backend_is_rejected_explicitly(tmp_path, invalid):
    tools, _ = service(tmp_path)
    with pytest.raises(ValueError, match='agent or qmd_keyword'):
        install_backend_policy(tools, invalid)


@pytest.mark.parametrize('invalid', ['other', '', None, [], 1])
def test_illegal_requested_mode_is_not_silently_repaired(tmp_path, invalid):
    tools, kb = service(tmp_path)
    records = []
    install_backend_policy(tools, 'qmd_keyword', on_search=records.append)
    with pytest.raises(ValueError, match='Requested retrieval mode'):
        tools.call(None, 'search_knowledge', {'query': 'mesh', 'mode': invalid})
    assert kb.calls == [] and not records[0]['success']


@pytest.mark.parametrize('actual', ['native', 'qmd_vector', 'native-html-hierarchy-sqlite-fts5'])
def test_qmd_native_or_vector_fallback_is_a_visible_failure(tmp_path, actual):
    tools, kb = service(tmp_path, actual=actual)
    records = []
    install_backend_policy(tools, 'qmd_keyword', on_search=records.append)
    with pytest.raises(BackendPolicyError, match='no fallback allowed'):
        tools.call_public('search_knowledge', {'query': 'mesh', 'kind': 'all', 'mode': 'native'})
    assert len(kb.calls) == 1
    assert not records[0]['success'] and records[0]['observed_backends'] == [actual]


def test_missing_actual_backend_metadata_fails_and_empty_hits_are_transparent(tmp_path):
    tools, kb = service(tmp_path)
    original = kb.search
    def without_metadata(*args, **kwargs):
        hits = original(*args, **kwargs)
        for hit in hits:
            hit.pop('retrieval')
        return hits
    kb.search = without_metadata
    records = []
    install_backend_policy(tools, 'qmd_keyword', on_search=records.append)
    with pytest.raises(BackendPolicyError, match='observed None'):
        tools.call(None, 'search_knowledge', {'query': 'mesh', 'mode': 'native'})
    kb.empty = True
    assert tools.call(None, 'search_knowledge', {'query': 'mesh', 'mode': 'native'})['results'] == []
    assert records[-1]['backend_validation'] == 'empty_result_no_hit_evidence'


def test_non_search_tools_and_unrelated_provider_calls_pass_through_unchanged():
    seen = []
    kb = Knowledge()
    def original(sid, name, args, *, provenance=None):
        seen.append((sid, name, args, provenance))
        return args
    tools = SimpleNamespace(kb=kb, call=original)
    install_backend_policy(tools, 'qmd_keyword')
    arguments, provenance = {'id': 'source', 'offset': 0, 'length': 500}, {'backend': 'same'}
    assert tools.call('task', 'read_source', arguments, provenance=provenance) is arguments
    assert seen == [('task', 'read_source', arguments, provenance)]
    kb.search('unrelated', filters={'mode': 'native'})
    assert kb.calls[-1]['filters']['mode'] == 'native'


def test_retired_native_experiment_is_rejected_instead_of_mislabeled_qmd(tmp_path):
    tools, _ = service(tmp_path)
    with pytest.raises(ValueError, match='retired'):
        install_backend_policy(tools, 'native')


def test_batch_runtime_audit_and_public_path_share_policy_without_prompt_changes(tmp_path, monkeypatch):
    tools, kb = service(tmp_path, actual='native')
    agents = AgentSessions.__new__(AgentSessions)
    agents.call_tool = lambda sid, name, args: tools.call(None, name, args)
    app = SimpleNamespace(tools=tools, agents=agents)
    monkeypatch.setattr(batch, 'HERE', tmp_path / 'attempt')
    audit = tmp_path / 'runtime.jsonl'
    monkeypatch.setitem(batch.AUDITS, ('rag', 'task'), audit)
    batch.audit_tools(app, 'rag', 'qmd_keyword')
    result = app.agents.runtime_tool('task', 'search_knowledge', {'query': 'mesh', 'mode': 'native'})
    assert result['success'] is False
    runtime = json.loads(audit.read_text(encoding='utf-8'))
    assert runtime['requested_mode'] == 'native' and runtime['effective_mode'] == 'qmd_keyword'
    assert not runtime['success']
    policy_log = tmp_path / 'attempt/backend-policy-audits/rag.jsonl'
    record = json.loads(policy_log.read_text(encoding='utf-8'))
    assert record['observed_backends'] == ['native'] and record['backend_validation'] == 'failed'
    kb.actual = None
    result = tools.call_public('search_knowledge', {'query': 'mesh', 'kind': 'case', 'mode': 'native'})
    assert result['mode'] == 'qmd_keyword'
    assert len(policy_log.read_text(encoding='utf-8').splitlines()) == 2
    assert policy_description('qmd_keyword')['automatic_fallback'] is False
