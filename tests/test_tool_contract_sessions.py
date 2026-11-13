"""Old Codex threads must not silently retain stale dynamic tool definitions."""
import copy
import json

from advisor.agent_sessions import AgentSessions, PLANNING_CONTRACT_SHA256
from test_managed_backend_selection import setup


def test_same_contract_reconnect_resumes_the_existing_thread(setup):
    value = setup
    first = value.manager.connect(value.sid)
    value.manager.disconnect(value.sid)
    value.manager.connect(value.sid)
    assert value.codex.starts == [None, first['thread_id']]


def test_changed_contract_starts_fresh_but_preserves_plan_and_messages(setup):
    value = setup
    value.manager.connect(value.sid)
    entry = value.manager.entries[value.sid]
    entry['record']['messages'] = [{'role': 'user', 'text': 'Keep the prior requirements', 'at': 'fixture'}]
    messages, plan = copy.deepcopy(entry['record']['messages']), value.sessions.get(value.sid)
    value.manager.disconnect(value.sid)
    entry['record']['backend_contracts']['codex'] = 'old-tools'
    state = value.manager.connect(value.sid)
    assert value.codex.starts == [None, None]
    assert state['warning'] and '规范已升级' in state['warning']
    assert entry['record']['messages'] == messages
    assert value.sessions.get(value.sid) == plan
    assert entry['record']['backend_contracts']['codex'] == PLANNING_CONTRACT_SHA256
    value.manager.disconnect(value.sid)
    value.manager.connect(value.sid)
    assert value.codex.starts[-1] == state['thread_id']


def test_legacy_record_without_contract_fingerprint_registers_new_tools(setup):
    value = setup
    value.manager.connect(value.sid)
    value.manager.close()
    path = value.root / 'agent-sessions' / (value.sid + '.json')
    record = json.loads(path.read_text(encoding='utf-8'))
    record.pop('backend_contracts')
    record['messages'] = [{'role': 'assistant', 'text': 'Existing saved conversation', 'at': 'fixture'}]
    path.write_text(json.dumps(record), encoding='utf-8')
    restored = AgentSessions(value.root, value.sessions, value.kb, runtime_factory=value.codex)
    try:
        state = restored.connect(value.sid)
        assert value.codex.starts[-1] is None
        assert state['messages'][0]['text'] == 'Existing saved conversation'
        assert restored.entries[value.sid]['record']['backend_contracts']['codex'] == PLANNING_CONTRACT_SHA256
    finally:
        restored.close()
