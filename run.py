"""python run.py [--port 8765] [--build-index]"""
import argparse
from advisor.server import App, make_server


def main():
    parser = argparse.ArgumentParser(description="本地 COMSOL 仿真方案助手")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--build-index", action="store_true")
    parser.add_argument("--agent-backend", choices=["codex", "dsh", "none"], help="选择托管Codex/DSH调试后端；none仅运行工作台和独立工具服务")
    args = parser.parse_args()
    app = App(runtime_factory=args.agent_backend)
    if args.build_index:
        print("正在建立本机 COMSOL 手册索引…", flush=True)
        print(app.kb.build(), flush=True)
    try:
        server = make_server(app, args.port)
    except OSError as exc:
        if args.port == 0 or getattr(exc, "winerror", None) not in (10013, 10048):
            raise
        print(f"端口 {args.port} 被占用或保留；改用系统分配的空闲端口。", flush=True)
        server = make_server(app, 0)
    import os
    from advisor.config import DATA
    from advisor.sessions import write_json
    write_json(DATA / "server.json", {"url": f"http://127.0.0.1:{server.server_port}", "pid": os.getpid(), "data_dir": str(DATA.resolve())})
    print(f"COMSOL 仿真方案助手：http://127.0.0.1:{server.server_port}", flush=True)
    print("仅本机访问；Ctrl+C 停止。模型只读，未运行 COMSOL 求解。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.agents.close()
        server.server_close()


if __name__ == "__main__":
    main()
