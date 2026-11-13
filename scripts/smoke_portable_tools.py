"""Real MCP client -> live workbench -> copied plan, without an Agent/model."""
import asyncio
import copy
import hashlib
import json
import sys
from pathlib import Path
from urllib.request import Request,urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from advisor.config import DATA,ROOT
from advisor.sessions import SessionStore,write_json
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client


def http(base,path):
    with urlopen(Request(base+path),timeout=30) as response:
        return json.load(response)


async def main():
    base=json.loads((DATA/"server.json").read_text(encoding="utf-8"))["url"]
    source_path=DATA/"sessions"/"0c3eb28311f44c7c9a4a74f3fba1b72b.json"
    source_bytes=source_path.read_bytes()
    original=json.loads(source_bytes)
    store=SessionStore(DATA,None,None)
    sample=store.create("[跨后端工具联调副本]"+original["goal"],copy.deepcopy(original.get("model")))
    for key in ("plan","sources","design_brief","questions","answers","planning_incomplete","plan_stale","research_trace"):
        if key in original: sample[key]=copy.deepcopy(original[key])
    sample["plan"]["title"]="独立 MCP 工具联调 · "+sample["plan"]["title"]
    write_json(store.path(sample["id"]),sample)
    sid=sample["id"]
    out=DATA/"portable-tools-smoke";out.mkdir(exist_ok=True)
    write_json(out/"run.json",{"session_id":sid,"status":"running","purpose":"工具接口测试；不评测仿真方案质量"})
    parameters=StdioServerParameters(command=sys.executable,args=[str(ROOT/"workbench_mcp.py"),"--url",base])
    async with stdio_client(parameters) as (reader,writer):
        async with ClientSession(reader,writer) as client:
            await client.initialize()
            tools=await client.list_tools()
            async def call(name,args):
                result=await client.call_tool(name,args)
                if result.isError:
                    raise RuntimeError("MCP tool failed: "+name)
                return json.loads(result.content[0].text)
            context=await call("get_task_context",{"session_id":sid})
            hits=await call("search_knowledge",{"query":"Mesh Element Quality","kind":"manual","mode":"qmd_keyword"})
            source_id="cs-a67ff0e99e600d559754cdbb"
            document=await call("read_source",{"session_id":sid,"id":source_id,"offset":0,"length":10000})
            mesh=next(step for step in context["plan"]["steps"] if "网格" in step["title"])
            changed=copy.deepcopy(mesh)
            changed["checks"].append("[工具接口联调] 本条只验证外部工具写入，不表示已构建网格或执行检查。")
            changed["source_ids"]=list(dict.fromkeys(changed["source_ids"]+[source_id]))
            saved=await call("update_steps",{"session_id":sid,"revision":context["revision"],"steps":[changed]})
    current=store.get(sid)
    state=http(base,f"/api/sessions/{sid}/agent")
    report={"session_id":sid,"status":"completed","transport":"official MCP SDK stdio -> HTTP -> PlanningService",
        "tool_names":[tool.name for tool in tools.tools],"knowledge_results":len(hits["results"]),
        "source_read":document["id"]==source_id and bool(document["text"]),
        "saved":saved["saved"],"revision_incremented":current["revision"]==sample["revision"]+1,
        "other_steps_unchanged":[s for s in current["plan"]["steps"] if s["id"]!=mesh["id"]]==[s for s in sample["plan"]["steps"] if s["id"]!=mesh["id"]],
        "debug_agent_not_started":not state["alive"] and state["pid"] is None,
        "edit_backend":current["research_trace"][-1]["backend"],
        "source_session_unchanged":hashlib.sha256(source_path.read_bytes()).digest()==hashlib.sha256(source_bytes).digest(),
        "physical_validation":"not_run","model_calls":0}
    write_json(out/"run.json",report)
    write_json(out/"session.json",current)
    print(json.dumps(report,ensure_ascii=False))
    assert all(report[key] for key in ("source_read","saved","revision_incremented","other_steps_unchanged","debug_agent_not_started","source_session_unchanged"))


if __name__=="__main__":
    asyncio.run(main())
