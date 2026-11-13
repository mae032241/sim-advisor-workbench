from types import SimpleNamespace
import json

import pytest

from scripts.benchmark_dsh_cli import DshTask, numeric_usage


def config(tools=()):
    return dict(provider="deepseek-official",model="deepseek-flash",reasoning_effort="high",
                tools=[dict(name=t) for t in tools])


def ready(task, names=()):
    return task.callback("/ready",dict(provider="deepseek-official",model="deepseek-flash",reasoning_effort="high",
                                      tool_names=list(names),ephemeral=True))


@pytest.mark.parametrize("change", [dict(model="other"),dict(ephemeral=False),dict(tool_names=["shell"])])
def test_ready_boundary_rejects_wrong_model_persistence_or_extra_tools(change):
    task=DshTask(config())
    data=dict(provider="deepseek-official",model="deepseek-flash",reasoning_effort="high",tool_names=[],ephemeral=True)|change
    assert task.callback("/ready",data)=={"accepted":False}
    assert not task.ready.is_set() and task.finished.is_set()


def test_tool_error_is_an_ordinary_response_and_does_not_finish_agent():
    def invoke(_method,_name,_arguments):
        return SimpleNamespace(isError=True,content=[SimpleNamespace(text=json.dumps({"error":"资料不存在。"}))])
    task=DshTask(config(["read_source"]),portal=SimpleNamespace(call=invoke),bridge=SimpleNamespace(call_tool=lambda:None))
    assert ready(task,["read_source"])["accepted"]
    assert task.callback("/tool",dict(name="read_source",arguments={"id":"typo"}))["success"] is False
    assert not task.finished.is_set()


def test_final_is_preserved_without_citation_or_physics_acceptance():
    task=DshTask(config())
    assert ready(task)["accepted"]
    text="# Plan\r\nUnread citation [typo-source].  \n"
    task.callback("/complete",dict(status="completed",text=text,selected={k:task.config[k] for k in ("provider","model","reasoning_effort")},usage=None))
    assert task.final==text and task.finished.is_set() and task.failure is None


def test_only_numeric_observable_usage_is_retained():
    assert numeric_usage({"inputTokens":10,"outputTokens":False,"modelCalls":2,"secret":"no","totalTokens":"12"})=={"inputTokens":10,"modelCalls":2}
