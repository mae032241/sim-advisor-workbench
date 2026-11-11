"""Managed-backend selection and migration; no real Agent/model is started."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import types
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from advisor.agent_sessions import AgentSessions
from advisor.runtime_adapters import get_runtime_factory
from advisor.server import App, make_server
from advisor.sessions import ConflictError, SessionStore, write_json
from test_agent_sessions import payload
from test_workflow import FakeKB, FakePlanner, GOAL, SOURCE


class Runtime:
    def __init__(self, factory, **kwargs):
        self.factory, self.kwargs = factory, kwargs
        self.backend_id = factory.backend_id
        self.running, self.busy, self.thread_id = False, False, None

    def start(self, thread_id=None, **_):
        self.factory.starts.append(thread_id)
        self.thread_id = thread_id or self.backend_id + "-conversation"
        self.running = True
        return {"thread_id": self.thread_id, "pid": 12345}

    def status(self):
        return {"running": self.running, "busy": self.busy, "model": self.kwargs["model"],
                "reasoning_effort": self.kwargs["reasoning_effort"], "turn_id": "turn" if self.busy else None, "pid": 12345}

    def send(self, text):
        self.busy = True
        return {"mode": "start", "text": text}

    def interrupt(self):
        self.busy = False

    def close(self):
        self.running = False


class Factory:
    def __init__(self, backend):
        self.backend_id, self.starts, self.runtimes = backend, [], []

    def status(self):
        return {"available": True, "mode": "injected-test", "backend_id": self.backend_id}

    def __call__(self, **kwargs):
        result = Runtime(self, **kwargs)
        self.runtimes.append(result)
        return result


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv("SIM_ADVISOR_MODEL", raising=False)
    codex, dsh = Factory("codex-app-server"), Factory("dsh")
    original = get_runtime_factory
    def select(value=None):
        if value == "codex": return codex
        if value == "dsh": return dsh
        return original(value)
    monkeypatch.setattr("advisor.agent_sessions.get_runtime_factory", select)
    kb = FakeKB()
    sessions = SessionStore(tmp_path, kb, FakePlanner())
    task = sessions.create(GOAL, None)
    manager = AgentSessions(tmp_path, sessions, kb, runtime_factory=codex)
    try:
        yield types.SimpleNamespace(root=tmp_path, kb=kb, sessions=sessions, task=task, sid=task["id"], manager=manager, codex=codex, dsh=dsh)
    finally:
        manager.close()


def test_switches_restore_per_backend_settings_and_never_cross_resume_ids(setup):
    x=setup; a=x.manager
    a.settings(x.sid,"custom-codex","medium")
    a.message(x.sid,"保留当前方案并讨论网格")
    before=a.status(x.sid)["messages"]
    with pytest.raises(ConflictError): a.settings(x.sid,managed_backend="dsh")
    assert x.codex.runtimes[-1].running and not x.dsh.runtimes
    a.disconnect(x.sid)
    status=a.settings(x.sid,managed_backend="dsh")
    assert (status["managed_backend"],status["managed_model"],status["managed_reasoning_effort"])==("dsh","deepseek-flash","high")
    assert status["thread_id"] is None and status["messages"]==before
    a.settings(x.sid,"deepseek-flash","max")
    a.connect(x.sid)
    assert x.dsh.starts==[None]
    assert a.status(x.sid)["thread_id"]=="dsh-conversation"
    a.disconnect(x.sid)
    status=a.settings(x.sid,managed_backend="codex")
    assert (status["managed_model"],status["managed_reasoning_effort"])==("custom-codex","medium")
    a.connect(x.sid)
    assert x.codex.starts==[None,"codex-app-server-conversation"]
    a.disconnect(x.sid)
    a.settings(x.sid,managed_backend="dsh")
    a.connect(x.sid)
    assert x.dsh.starts==[None,"dsh-conversation"]
    assert a.entries[x.sid]["record"]["backend_threads"]=={"codex":"codex-app-server-conversation","dsh":"dsh-conversation"}
    assert x.sessions.get(x.sid)["revision"]==1


def test_legacy_record_belongs_to_codex_even_when_new_default_is_dsh(setup):
    x=setup
    legacy={"thread_id":"old-codex-thread","model":"gpt-6.1-sol","reasoning_effort":"xhigh",
            "messages":[{"role":"user","text":"保留历史"}],"read_sources":[SOURCE["id"]]}
    write_json(x.root/"agent-sessions"/(x.sid+".json"),legacy)
    manager=AgentSessions(x.root,x.sessions,x.kb,runtime_factory=x.dsh)
    try:
        status=manager.status(x.sid)
        assert status["managed_backend"]=="codex" and status["thread_id"]=="old-codex-thread"
        record=json.loads((x.root/"agent-sessions"/(x.sid+".json")).read_text(encoding="utf-8"))
        assert record["backend_threads"]=={"codex":"old-codex-thread"}
        manager.settings(x.sid,managed_backend="dsh")
        manager.connect(x.sid)
        assert x.dsh.starts==[None]
        assert manager.status(x.sid)["messages"]==legacy["messages"]
        other=x.sessions.create("另一份全新需求需要进行分析",None)
        assert manager.status(other["id"])["managed_backend"]=="dsh"
        assert manager.status(other["id"])["model"]=="deepseek-flash"
    finally: manager.close()


def test_status_metadata_and_dsh_effort_validation_do_not_start_process_or_read_auth(setup,monkeypatch):
    x=setup
    original=Path.read_text
    def read(path,*args,**kwargs):
        assert path.name not in {"auth.json","config.toml"}
        return original(path,*args,**kwargs)
    with patch.object(Path,"read_text",read),patch("subprocess.Popen",side_effect=AssertionError("no launch")):
        status=x.manager.status(x.sid)
    options={item["id"]:item for item in status["backend_options"]}
    assert options["codex"]["effort_options"]==["low","medium","high","xhigh"]
    assert options["dsh"]["effort_options"]==["off","low","high","max"]
    assert (options["dsh"]["default_model"],options["dsh"]["default_effort"])==("deepseek-flash","high")
    x.manager.settings(x.sid,managed_backend="dsh")
    for effort in ["off","low","high","max"]:
        assert x.manager.settings(x.sid,reasoning_effort=effort)["managed_reasoning_effort"]==effort
    with pytest.raises(ValueError): x.manager.settings(x.sid,reasoning_effort="xhigh")
    with pytest.raises(ValueError): x.manager.settings(x.sid,managed_backend="unknown")


def test_global_none_never_starts_selected_dsh_and_external_keeps_managed_profile(setup):
    x=setup
    disabled=AgentSessions(x.root/"disabled",x.sessions,x.kb,runtime_factory="none")
    try:
        disabled.settings(x.sid,managed_backend="dsh")
        with patch("advisor.runtime_adapters.codex_command",side_effect=AssertionError("no Codex discovery")),patch("subprocess.Popen",side_effect=AssertionError("no launch")):
            status=disabled.status(x.sid)
            assert all(not item["enabled"] for item in status["backend_options"])
            with pytest.raises(RuntimeError,match="禁用"): disabled.connect(x.sid)
        before=status["managed_backend_settings"]
        registered=disabled.register_external(x.sid,{"name":"external","pid":os.getpid(),"backend":"external-dsh","model":"other-model","reasoning_effort":"custom","conversation_id":"external-conversation"})
        ext=disabled.status(x.sid)
        assert ext["backend"]=="external-dsh" and ext["managed_backend"]=="dsh"
        assert ext["managed_backend_settings"]==before
        disabled.unregister_external(x.sid,{"binding_id":registered["binding_id"]})
        with pytest.raises(RuntimeError,match="外部"): disabled.connect(x.sid)
    finally: disabled.close()


def test_tool_attribution_uses_selected_managed_backend_conversation(setup):
    x=setup
    x.manager.settings(x.sid,managed_backend="dsh")
    x.manager.connect(x.sid)
    x.manager.call_tool(x.sid,"read_source",{"id":SOURCE["id"],"offset":0,"length":500})
    x.manager.call_tool(x.sid,"save_plan",payload(x.task))
    saved=x.sessions.get(x.sid)
    assert saved["plan"]["engine"]=="dsh / deepseek-flash / high"
    trace=saved["research_trace"][-1]
    assert trace["backend"]=="dsh" and trace["conversation_id"]=="dsh-conversation"


def test_dsh_factory_forwards_defaults_and_has_no_codex_fallback(monkeypatch,tmp_path):
    calls=[]
    runtime_module=types.SimpleNamespace(DshRuntime=lambda **kwargs:calls.append(kwargs) or kwargs)
    monkeypatch.setitem(sys.modules,"advisor.dsh_runtime",runtime_module)
    monkeypatch.setattr("advisor.dsh_command.discover_dsh_command",lambda:["dsh.cmd"])
    with patch("advisor.runtime_adapters.codex_command",side_effect=AssertionError("no fallback")),patch("subprocess.Popen",side_effect=AssertionError("no launch")):
        factory=get_runtime_factory("dsh")
        factory(cwd=tmp_path,on_event=None,on_tool=None)
        assert calls[-1]["command"]==["dsh.cmd"] and calls[-1]["model"]=="deepseek-flash" and calls[-1]["reasoning_effort"]=="high"
        monkeypatch.setattr("advisor.dsh_command.discover_dsh_command",lambda:None)
        with pytest.raises(RuntimeError,match="SIM_ADVISOR_DSH"):
            factory(cwd=tmp_path,on_event=None,on_tool=None)


def test_http_backend_only_settings_and_old_payloads_are_both_supported(setup):
    x=setup
    app=App(x.root/"http",x.root/"missing-docs",runtime_factory=x.codex)
    task=app.sessions.create(GOAL,None)
    server=make_server(app,0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def request(body):
        req=Request(f"http://127.0.0.1:{server.server_port}/api/sessions/{task['id']}/agent/settings",data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
        try:
            with urlopen(req,timeout=5) as response:return response.status,json.load(response)
        except HTTPError as exc:return exc.code,json.load(exc)
    try:
        code,value=request({"managed_backend":"dsh"})
        assert code==200 and value["model"]=="deepseek-flash" and value["reasoning_effort"]=="high"
        assert request({"model":"deepseek-flash","reasoning_effort":"max"})[0]==200
        assert request({"reasoning_effort":"medium"})[0]==422
        code,value=request({"managed_backend":"codex","model":"gpt-6.1-sol","reasoning_effort":"xhigh"})
        assert code==200 and value["managed_backend"]=="codex"
        app.agents.connect(task["id"])
        assert request({"managed_backend":"dsh"})[0]==409
    finally:
        server.shutdown();server.server_close();thread.join();app.agents.close()


def test_cli_accepts_dsh_settings_without_requiring_codex_fields(monkeypatch,capsys):
    import agent_cli
    requests=[]
    monkeypatch.setattr(sys,"argv",["agent_cli.py","settings","a"*32,"--url","http://127.0.0.1:8765","--managed-backend","dsh","--effort","max"])
    monkeypatch.setattr(agent_cli,"urlopen",lambda request,timeout:requests.append(request) or io.BytesIO(b'{"managed_backend":"dsh"}'))
    agent_cli.main()
    assert json.loads(requests[0].data)=={"managed_backend":"dsh","reasoning_effort":"max"}
    assert "dsh" in capsys.readouterr().out


def test_run_cli_accepts_dsh_without_constructing_a_real_service():
    result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/"run.py"),"--agent-backend","dsh","--help"],capture_output=True,timeout=10)
    assert result.returncode==0 and b"dsh" in result.stdout
