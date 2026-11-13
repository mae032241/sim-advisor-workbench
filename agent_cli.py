"""Small client for the workbench's plan-bound Agent conversation."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from advisor.config import DATA


def main():
    parser = argparse.ArgumentParser(description="调用仿真工作台中与方案绑定的 Agent 会话")
    parser.add_argument("action", choices=["status", "connect", "message", "interrupt", "disconnect", "settings"])
    parser.add_argument("session", help="方案ID（工作台网址中的session）")
    parser.add_argument("text", nargs="?", help="发送给会话的文本")
    parser.add_argument("--text-file", type=Path, help="message从UTF-8文件读取文本，避免命令行长度限制；与text互斥")
    parser.add_argument("--url", help="默认从data/server.json读取")
    parser.add_argument("--managed-backend", choices=["codex", "dsh"], help="断开后选择托管后端；不改变managed/external连接方式")
    parser.add_argument("--connection-mode", choices=["managed", "external"], help="settings使用的连接方式")
    parser.add_argument("--model", help="settings使用的模型ID")
    parser.add_argument("--effort", choices=["off", "low", "medium", "high", "xhigh", "max"], help="推理强度候选以所选后端status为准")
    args = parser.parse_args()
    if args.text_file is not None:
        if args.action != "message" or args.text is not None:
            parser.error("--text-file仅用于message，且不能同时传text。")
        try:
            with args.text_file.open("rb") as stream:
                blob = stream.read(65537)
            if len(blob) > 65536:
                parser.error("消息文件超过64KiB；会话消息仍限12000字符。")
            args.text = blob.decode("utf-8-sig")
        except (OSError, UnicodeError):
            parser.error("无法读取UTF-8消息文件。")
        if not 1 <= len(args.text.strip()) <= 12000:
            parser.error("消息文件需包含1至12000字符，不改变工作台消息长度上限。")
    import re
    if not re.fullmatch(r"[a-f0-9]{32}", args.session):
        parser.error("方案ID应为32位十六进制字符")
    url = args.url or json.loads((DATA / "server.json").read_text(encoding="utf-8"))["url"]
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("仅支持本机工作台地址")
    if args.action == "message" and not args.text:
        parser.error("message需要文本参数")
    if args.action == "settings" and not any((args.model, args.effort, args.managed_backend, args.connection_mode)):
        parser.error("settings需要模型、推理强度、托管后端或连接方式参数。")
    endpoint = url.rstrip("/") + f"/api/sessions/{args.session}/agent"
    data = None
    if args.action != "status":
        endpoint += "/" + args.action
        payload = {"text": args.text} if args.action == "message" else {k:v for k,v in {
            "model":args.model,"reasoning_effort":args.effort,"managed_backend":args.managed_backend,
            "connection_mode":args.connection_mode}.items() if v is not None} if args.action == "settings" else {}
        data = json.dumps(payload).encode("utf-8")
    request = Request(endpoint, data=data, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=90) as response:
        print(json.dumps(json.load(response), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
