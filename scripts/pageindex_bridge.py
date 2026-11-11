"""Run isolated, explicitly selected PageIndex PDF work. JSON stdin/stdout."""
import contextlib
import json
import sys


def main():
    request = json.loads(sys.stdin.read())
    # Keep SDK progress messages away from the structured result channel.
    with contextlib.redirect_stdout(sys.stderr):
        from pageindex import PageIndexClient
        client = PageIndexClient(index=request["model"], chat=request["model"], storage_path=request["storage_path"])
        if request["action"] == "index":
            result = client.submit_document(request["path"], wait=True)
        elif request["action"] == "search":
            result = client.chat(request["query"], doc_id=request["doc_id"])
        else:
            raise ValueError("Unknown PageIndex action")
    print(json.dumps({"backend": "pageindex-sdk", "result": result}, default=str))


if __name__ == "__main__":
    main()
