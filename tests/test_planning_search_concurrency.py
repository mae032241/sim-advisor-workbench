from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
import time

import pytest

from advisor.planning_service import PlanningService
from test_workflow import FakeKB, SOURCE


def search(service, query):
    return service.call_public("search_knowledge", {"query": query, "kind": "manual", "mode": "native",
        "feature_branch": "physics", "library_ids": ["manual"]})


def test_search_serializes_only_retrieval_and_preserves_scope_and_result_order(tmp_path):
    kb = FakeKB()
    active = maximum = 0
    counter_lock = threading.Lock()
    received = []
    ready = threading.Barrier(6)

    def backend(query, **kwargs):
        nonlocal active, maximum
        with counter_lock:
            active += 1
            maximum = max(maximum, active)
            received.append((query, kwargs))
        try:
            time.sleep(0.02)
            return [{**SOURCE, "id": query + "-first"}, {**SOURCE, "id": query + "-second"}]
        finally:
            with counter_lock:
                active -= 1

    kb.search = backend
    service = PlanningService(tmp_path, None, kb)

    def request(index):
        ready.wait(timeout=2)
        return search(service, "query-" + str(index))

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(request, index) for index in range(6)]
        for index, future in enumerate(futures):
            assert [item["id"] for item in future.result(timeout=3)["results"]] == [f"query-{index}-first", f"query-{index}-second"]
    assert maximum == 1 and len(received) == 6
    assert all(kwargs == {"limit": 6, "kind": "manual", "filters": {
        "mode": "qmd_keyword", "feature_branch": "physics", "library_ids": ["manual"]}} for _, kwargs in received)


def test_failed_search_releases_the_retrieval_lock(tmp_path):
    kb = FakeKB()

    def backend(query, **kwargs):
        if query == "fail":
            raise RuntimeError("controlled search failure")
        return [SOURCE]

    kb.search = backend
    service = PlanningService(tmp_path, None, kb)
    with pytest.raises(RuntimeError, match="controlled"):
        search(service, "fail")
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(search, service, "valid").result(timeout=1)["results"][0]["id"] == SOURCE["id"]


def test_search_does_not_block_context_reads_or_library_operations(tmp_path):
    entered, release = threading.Event(), threading.Event()
    kb = FakeKB()
    kb.store = SimpleNamespace(library_scope=lambda **kwargs: {"libraries": []},
        set_enabled_many=lambda ids, enabled: {"updated": ids, "enabled": enabled})

    def backend(query, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return [SOURCE]

    kb.search = backend
    sessions = SimpleNamespace(get=lambda sid: {"id": sid, "revision": 1, "sources": []})
    service = PlanningService(tmp_path, sessions, kb)
    with ThreadPoolExecutor(max_workers=5) as pool:
        blocked = pool.submit(search, service, "blocked")
        assert entered.wait(timeout=1)
        try:
            calls = [
                ("get_task_context", {"session_id": "a" * 32}),
                ("read_source", {"id": SOURCE["id"], "offset": 0, "length": 500}),
                ("get_library_scope", {}),
                ("set_libraries_enabled", {"library_ids": ["manual"], "enabled": False}),
            ]
            for name, arguments in calls:
                assert pool.submit(service.call_public, name, arguments).result(timeout=1) is not None
        finally:
            release.set()
        assert blocked.result(timeout=1)["results"]
