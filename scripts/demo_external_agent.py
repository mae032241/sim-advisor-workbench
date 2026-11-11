"""Explicit echo demo for workbench transport/UI testing; no LLM or planning."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from external_agent_client import ExternalAgentClient, ExternalAgentError


def run_demo(client, *, delay=.2):
    """Keep dispatching stop/disconnect while the cancellable echo worker waits."""
    current = {"cancel": None, "worker": None, "generation": 0}
    lock = threading.RLock()
    client.register("DEMO echo（非 LLM，不生成方案）", model="echo-demo", reasoning_effort="none", conversation_id="echo-" + uuid.uuid4().hex)
    client.start_pump()
    def cancel():
        with lock:
            current["generation"] += 1
            if current["cancel"]:
                current["cancel"].set()
    def start(text):
        cancel()
        with lock:
            generation, stop = current["generation"], threading.Event()
            current["cancel"] = stop
        turn_id, message_id = "demo-" + uuid.uuid4().hex, "demo-message-" + uuid.uuid4().hex
        client.publish({"type": "state", "status": "running", "turn_id": turn_id})
        client.publish({"type": "activity", "text": "DEMO：仅演示收件、回显与停止，不调用模型、不修改方案。", "turn_id": turn_id})
        def echo():
            if stop.wait(delay):
                return
            try:
                with lock:
                    if stop.is_set() or generation != current["generation"]:
                        return
                    client.publish({"type": "message", "message_id": message_id, "turn_id": turn_id,
                                    "text": "[DEMO echo · 非 LLM，未生成/修改方案] 收到：" + text})
                    client.publish({"type": "state", "status": "idle", "turn_id": turn_id})
            except ExternalAgentError:
                stop.set()
        worker = threading.Thread(target=echo, name="workbench-echo-demo", daemon=True)
        current["worker"] = worker
        worker.start()
    try:
        client.publish({"type": "state", "status": "idle"})
        client.publish({"type": "message", "message_id": "demo-introduction", "text": "[DEMO echo · 非 LLM] 已接入。这里只回显消息，可验证双向通信与停止；不会研究资料或更新方案。"})
        while True:
            command = client.receive(timeout=.5)
            if command is None:
                if not client.connected:
                    break
                continue
            if command["type"] == "disconnect":
                break
            if command["type"] == "interrupt":
                cancel()
                client.publish({"type": "activity", "text": "DEMO 回显任务已停止；没有模型或求解任务。"})
                client.publish({"type": "state", "status": "idle"})
            elif command["type"] == "message":
                start(command.get("text", ""))
    finally:
        cancel()
        worker = current["worker"]
        if worker and worker.is_alive():
            worker.join(timeout=1)
        client.unregister()


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="外部 Agent 接入回显演示；不调用 LLM/Codex，不生成方案。")
    parser.add_argument("session_id")
    parser.add_argument("--url", help="默认读取 data/server.json")
    parser.add_argument("--delay", type=float, default=.2, help="可取消的回显等待秒数，便于测试停止")
    args = parser.parse_args(argv)
    if not 0 <= args.delay <= 300:
        parser.error("--delay 必须在0–300秒内。")
    try:
        client = ExternalAgentClient(args.session_id, args.url)
        print("DEMO echo 已启动：仅回显，非 LLM；Ctrl+C 断开。", flush=True)
        run_demo(client, delay=args.delay)
    except KeyboardInterrupt:
        return 0
    except (ExternalAgentError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
