"""Backend-neutral CLI for the same tools exposed through HTTP and MCP."""
import argparse
import json
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler,Request,build_opener,ProxyHandler

from advisor.config import DATA


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def main():
    parser=argparse.ArgumentParser(description="独立仿真任务/知识工具客户端，不启动Agent或模型")
    parser.add_argument("--url",help="本机工作台HTTP地址；默认data/server.json")
    commands=parser.add_subparsers(dest="command",required=True)
    commands.add_parser("list",help="列出工具及JSON schema")
    scope=commands.add_parser("scope",help="查询资料库目录与当前启停，不需要方案")
    scope.add_argument("--query")
    scope.add_argument("--type",choices=["official-manuals","official-models","user-manuals","user-models"])
    enable=commands.add_parser("enable-many",help="显式批量启停指定库，不构建索引")
    enable.add_argument("--library",action="append",dest="library_ids",required=True)
    enable.add_argument("--enabled",choices=["true","false"],required=True)
    call=commands.add_parser("call",help="调用独立工具")
    call.add_argument("name")
    arguments=call.add_mutually_exclusive_group()
    arguments.add_argument("--args",help="JSON对象；复杂参数建议用--args-file")
    arguments.add_argument("--args-file",help="UTF-8 JSON参数文件")
    args=parser.parse_args()
    try:
        base=args.url or json.loads((DATA/"server.json").read_text(encoding="utf-8"))["url"]
        parsed=urlsplit(base)
        if parsed.scheme!="http" or parsed.hostname not in {"127.0.0.1","localhost","::1"} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"","/"}:
            raise ValueError("仅支持本机工作台HTTP地址。")
        payload=None
        if args.command=="call":
            from pathlib import Path
            value=json.loads(Path(args.args_file).read_text(encoding="utf-8-sig") if args.args_file else args.args or "{}")
            if not isinstance(value,dict):
                raise ValueError("工具参数需为JSON对象。")
            payload=json.dumps({"name":args.name,"arguments":value},ensure_ascii=False).encode("utf-8")
        elif args.command in {"scope","enable-many"}:
            name="get_library_scope" if args.command=="scope" else "set_libraries_enabled"
            value={key:getattr(args,key) for key in ("query","type") if getattr(args,key) is not None} if args.command=="scope" else {"library_ids":args.library_ids,"enabled":args.enabled=="true"}
            payload=json.dumps({"name":name,"arguments":value},ensure_ascii=False).encode("utf-8")
        path="/api/tools" if payload is None else "/api/tools/call"
        opener=build_opener(ProxyHandler({}),NoRedirect())
        with opener.open(Request(base.rstrip("/")+path,data=payload,headers={"Content-Type":"application/json"}),timeout=120) as response:
            print(json.dumps(json.load(response),ensure_ascii=False,indent=2))
        return 0
    except HTTPError as exc:
        try:
            error=json.loads(exc.read().decode("utf-8"))
        except (ValueError,UnicodeError):
            error={"error":f"工作台HTTP调用失败：{exc.code}"}
        print(json.dumps(error,ensure_ascii=False),file=sys.stderr)
        return 1
    except (ValueError,OSError,KeyError) as exc:
        print(json.dumps({"error":str(exc)},ensure_ascii=False),file=sys.stderr)
        return 1


if __name__=="__main__":
    if hasattr(sys.stdout,"reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
