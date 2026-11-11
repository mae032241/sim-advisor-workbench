"""Two real Codex turns in a copied plan; never modify benchmark sessions."""
import argparse
import copy
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.server import App
from advisor.sessions import write_json


def wait_idle(agents, sid, timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = agents.status(sid)
        if state["error"] or not state["alive"]:
            raise RuntimeError("Codex smoke turn failed; inspect safe runtime state")
        if state["status"] == "idle":
            return state
        time.sleep(1)
    agents.interrupt(sid)
    raise RuntimeError("Codex smoke turn timed out")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="b751f6f25cb4497c97983c4c69a99208")
    args = parser.parse_args()
    app = App()
    source_path = app.sessions.path(args.source)
    before_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    original = app.sessions.get(args.source)
    sample = app.sessions.create("Codex会话联调：" + original["goal"], original.get("model"))
    sid = sample["id"]
    for key in ("plan", "sources", "design_brief", "questions", "answers", "planning_incomplete", "plan_stale", "geometry_reference_catalog", "research_trace"):
        if key in original:
            sample[key] = copy.deepcopy(original[key])
    sample["plan"]["title"] = "Codex会话联调 · " + sample["plan"]["title"]
    write_json(app.sessions.path(sid), sample)
    output = app.data_dir / "codex-session-smoke"
    output.mkdir(exist_ok=True)
    write_json(output / "run.json", {"session_id": sid, "source_session": args.source,
        "status": "running", "purpose": "会话/工具接入联调，不是方案质量评测或仿真结果"})
    print(json.dumps({"session_id": sid, "phase": "starting"}), flush=True)
    started = time.monotonic()
    try:
        app.agents.message(sid, "这是会话接口联调，只做本消息要求，不进行完整规划。先调用get_plan确认当前模型格式，然后调用search_knowledge用native检索Mesh Element Quality，再调用read_source读取cs-a67ff0e99e600d559754cdbb正文（offset=0,length=10000）。记住校验词：青竹47。不要更新方案。最后简短回复模型格式、已读来源标题与校验词。")
        first = wait_idle(app.agents, sid)
        write_json(output / "turn-1.json", first)
        print(json.dumps({"phase": "turn1_completed", "thread_id": first["thread_id"], "seconds": round(time.monotonic() - started, 2)}), flush=True)
        mesh = next(s for s in sample["plan"]["steps"] if "网格" in s["title"])
        app.agents.message(sid, "继续接口联调：先复述上轮校验词，再调用get_plan。仅用update_steps修改已有节点 " + mesh["id"] + "，保留其id与依赖，其他节点不变。把刚才Mesh Element Quality手册中可支持的质量指标、坏单元定位、修复/重建后的复检落实到action/checks/difficulty，并用已读来源cs-a67ff0e99e600d559754cdbb引用支持；未执行的质量检查要写成计划，不编造阈值或结果。不要调用save_plan、不要重新研究其他环节。最后简短说明更新与还未做的验证。")
        second = wait_idle(app.agents, sid)
        updated = app.sessions.get(sid)
        other_before = [s for s in sample["plan"]["steps"] if s["id"] != mesh["id"]]
        other_after = [s for s in updated["plan"]["steps"] if s["id"] != mesh["id"]]
        mesh_after = next(s for s in updated["plan"]["steps"] if s["id"] == mesh["id"])
        second_text = "\n".join(m["text"] for m in second["messages"][len(first["messages"]):] if m["role"] == "assistant")
        report = {"session_id": sid, "source_session": args.source, "status": "completed", "seconds": round(time.monotonic() - started, 2),
            "same_thread": first["thread_id"] == second["thread_id"], "thread_id": second["thread_id"],
            "remembered_token": "青竹47" in second_text,
            "source_read": "cs-a67ff0e99e600d559754cdbb" in app.tools.read_sources(sid),
            "revision_incremented": updated["revision"] == sample["revision"] + 1,
            "mesh_changed": mesh != mesh_after, "other_steps_unchanged": other_before == other_after,
            "source_session_unchanged": hashlib.sha256(source_path.read_bytes()).hexdigest() == before_hash,
            "pending_gates_preserved": updated.get("planning_incomplete") == sample.get("planning_incomplete") and updated.get("answers") == sample.get("answers"),
            "physical_validation": "not_run"}
        write_json(output / "turn-2.json", second)
        write_json(output / "session.json", updated)
        write_json(output / "run.json", report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if not all(report[k] for k in ("same_thread", "remembered_token", "source_read", "revision_incremented", "mesh_changed", "other_steps_unchanged", "source_session_unchanged", "pending_gates_preserved")):
            raise RuntimeError("Codex smoke assertions failed")
    finally:
        app.agents.close()


if __name__ == "__main__":
    main()
