"""Search filters cross each public entry without building or authorizing citations."""
import copy
import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlencode
from urllib.request import urlopen

import library_cli
from advisor.planning_service import FEATURE_BRANCHES, PlanningService, tool_definitions
from advisor.server import App, make_server
from advisor.sessions import SessionStore
from test_workflow import FakeKB, FakePlanner, GOAL, SOURCE, TEST_TEMP


class BranchToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TEMP)
        self.root = Path(self.temp.name)
        self.kb = FakeKB()
        self.sessions = SessionStore(self.root, self.kb, FakePlanner())
        self.sid = self.sessions.create(GOAL, None)["id"]
        self.tools = PlanningService(self.root, self.sessions, self.kb)

    def tearDown(self):
        self.temp.cleanup()

    def test_optional_branch_schema_is_compatible_with_planning_and_library_tools(self):
        definitions = tool_definitions()
        self.assertEqual(len(definitions), 7)
        for definitions in [definitions, tool_definitions(bound=True)]:
            schema = next(item for item in definitions if item["name"] == "search_knowledge")["inputSchema"]
            self.assertEqual(schema["properties"]["feature_branch"]["enum"], list(FEATURE_BRANCHES))
            self.assertNotIn("feature_branch", schema["required"])

    def test_search_forwards_branch_and_kind_before_retrieval_without_read_authorization(self):
        hit = {**SOURCE, "retrieval": {"scope": {"feature_branch": "mesh", "kind": "code"}}, "text": "not a full read", "path": "private-path"}
        self.kb.search = Mock(return_value=[hit])
        with patch("subprocess.Popen", side_effect=AssertionError("Search must not start an Agent")):
            result = self.tools.call_public("search_knowledge", {"session_id": self.sid, "query": "boundary layer", "kind": "code", "mode": "qmd_keyword", "feature_branch": "mesh"})
        self.kb.search.assert_called_once_with("boundary layer", limit=6, kind="code", filters={"mode": "qmd_keyword", "feature_branch": "mesh"})
        self.assertEqual(result["results"][0]["retrieval"], hit["retrieval"])
        self.assertNotIn("text", result["results"][0])
        self.assertNotIn("path", result["results"][0])
        self.assertEqual(self.tools.read_sources(self.sid), [])

    def test_old_search_call_keeps_default_unrestricted_branch(self):
        self.kb.search = Mock(return_value=[])
        self.tools.call_public("search_knowledge", {"query": "mesh", "kind": "all", "mode": "native"})
        self.kb.search.assert_called_once_with("mesh", limit=6, kind="all", filters={"mode": "native"})

    def test_invalid_branch_is_rejected_without_broadening_search(self):
        self.kb.search = Mock(return_value=[])
        for value in ["../mesh", "Mesh", "", None, [], "--enrich"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.tools.call_public("search_knowledge", {"query": "test", "kind": "all", "mode": "native", "feature_branch": value})
        self.kb.search.assert_not_called()

    def test_related_summary_is_not_full_text_and_only_opened_document_is_read(self):
        self.kb.related_sources = Mock(return_value=[
            {"id": "lo-function", "type": "implementation_snippet", "feature_branch": "func", "relation": "uses", "status": "resolved", "title": "temperature", "text": "DO NOT AUTO READ", "path": "private-local-path"},
            {"title": "unresolved local function", "type": "implementation_snippet", "feature_branch": "func", "status": "unresolved", "relation": "uses"},
        ])
        result = self.tools.call_public("read_source", {"session_id": self.sid, "id": SOURCE["id"], "offset": 0, "length": 500})
        self.kb.related_sources.assert_called_once_with(SOURCE["id"], limit=8)
        self.assertEqual(len(result["related_sources"]), 2)
        self.assertNotIn("text", result["related_sources"][0])
        self.assertNotIn("path", result["related_sources"][0])
        self.assertEqual(result["related_sources"][1]["status"], "unresolved")
        self.assertNotIn("id", result["related_sources"][1])
        self.assertEqual(self.tools.read_sources(self.sid), [SOURCE["id"]])

    def test_old_provider_without_related_method_still_reads_normally(self):
        result = self.tools.call_public("read_source", {"id": SOURCE["id"], "offset": 0, "length": 500})
        self.assertEqual(result["text"], SOURCE["text"])
        self.assertEqual(result["related_sources"], [])

    def test_failed_related_lookup_does_not_block_valid_body_or_authorize_related_evidence(self):
        self.kb.related_sources = Mock(side_effect=RuntimeError("deliberate fixture failure"))
        result = self.tools.call_public("read_source", {"session_id": self.sid, "id": SOURCE["id"], "offset": 0, "length": 500})
        self.assertEqual(result["text"], SOURCE["text"])
        self.assertEqual(result["related_sources"], [])
        self.assertIn("暂不可读取", result["related_sources_warning"])
        self.assertEqual(self.tools.read_sources(self.sid), [SOURCE["id"]])

    def test_read_code_uses_only_requested_body_page_and_does_not_repeat_full_data_code(self):
        text = "CODE-" * 1000
        source = {**SOURCE, "kind": "code", "type": "implementation_snippet", "text": text, "excerpt": text,
            "data": {"code": text, "summary": "code summary", "feature_branch": "mesh", "function": {"name": "run"}}}
        self.kb.get_document = Mock(return_value=source)
        result = self.tools.call_public("read_source", {"id": SOURCE["id"], "offset": 500, "length": 500})
        self.assertEqual(result["text"], text[500:1000])
        self.assertEqual(result["next_offset"], 1000)
        self.assertEqual(result["total_characters"], len(text))
        self.assertNotIn("code", result["data"]); self.assertNotIn("excerpt", result)
        self.assertNotIn("candidate_only", result); self.assertEqual(source["data"]["code"], text)


class LibraryCLIEntryTests(unittest.TestCase):
    def test_cli_forwards_optional_kind_and_branch_without_build_or_enrichment(self):
        calls = []

        class Store:
            def __init__(self, path): self.root = path
            def index_status_summary(self): return {"modes": {"native": {"ready": True}}}

        class Search:
            def __init__(self, path): calls.append(("root", path))
            def search(self, query, **kwargs):
                calls.append((query, kwargs))
                return [{"id": "lo-feature", "retrieval": {"scope": kwargs}}]

        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory:
            for extra, expected in [([], {"kind": "all", "feature_branch": None}), (["--kind", "code", "--feature-branch", "physics"], {"kind": "code", "feature_branch": "physics"})]:
                output = io.StringIO()
                argv = ["library_cli.py", "--data-dir", directory, "search", "fluid interface", *extra]
                with patch.object(library_cli, "LibraryStore", Store), patch("advisor.library_search.LibrarySearch", Search), patch.object(sys, "argv", argv), redirect_stdout(output):
                    self.assertEqual(library_cli.main(), 0)
                self.assertEqual(calls[-1][1], {"mode": "native", "library_ids": None, "limit": 8, **expected})
                self.assertEqual(json.loads(output.getvalue())[0]["id"], "lo-feature")

    def test_cli_read_adds_related_summary_and_retains_full_source_body(self):
        source = {**SOURCE, "text": "full code body", "data": {"code": "full code body"}}

        class Store:
            def __init__(self, path): self.root = path
            def read(self, _id): return source
            def related_sources(self, _id, limit=8): return [{"id": "lo-other", "relation": "requires", "status": "source_relation", "text": "not auto read"}]

        output = io.StringIO()
        with patch.object(library_cli, "LibraryStore", Store), patch.object(sys, "argv", ["library_cli.py", "read", SOURCE["id"]]), redirect_stdout(output):
            self.assertEqual(library_cli.main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["text"], source["text"])
        self.assertEqual(result["related_sources"], [{"id": "lo-other", "relation": "requires", "status": "source_relation"}])
        self.assertNotIn("related_sources", source)


class HTTPSearchEntryTests(unittest.TestCase):
    def test_http_preserves_kind_mode_libraries_and_optional_branch(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory:
            app = App(Path(directory), Path(directory) / "missing-docs", runtime_factory="none")
            app.kb.search = Mock(return_value=[{"id": "lo-mesh", "type": "implementation_snippet", "kind": "code", "data": {"code": "raw full code" * 1000, "summary": "Mesh source candidate", "function": {"name": "run"}}, "retrieval": {"scope": {"feature_branch": "mesh"}}}])
            server = make_server(app, 0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                query = urlencode({"q": "boundary layer", "kind": "code", "mode": "qmd_keyword", "feature_branch": "mesh", "limit": "5", "library": ["lib-a", "lib-b"]}, doseq=True)
                with urlopen(f"http://127.0.0.1:{server.server_port}/api/search?{query}", timeout=5) as response:
                    result = json.load(response)
                app.kb.search.assert_called_once_with("boundary layer", limit=5, kind="code", filters={"library_ids": ["lib-a", "lib-b"], "mode": "qmd_keyword", "feature_branch": "mesh"})
                self.assertEqual(result["results"][0]["retrieval"]["scope"]["feature_branch"], "mesh")
                self.assertNotIn("code", result["results"][0]["data"])
                self.assertEqual(result["results"][0]["data"]["function"]["name"], "run")
                app.kb.search.reset_mock()
                with urlopen(f"http://127.0.0.1:{server.server_port}/api/search?q=mesh", timeout=5) as response:
                    json.load(response)
                app.kb.search.assert_called_once_with("mesh", limit=8, kind="all", filters={})
                source = {**SOURCE, "data": {"code": "full body retained"}, "text": "full body retained"}
                app.kb.get_document = Mock(return_value=source)
                app.kb.related_sources = Mock(return_value=[{"id": "lo-dependency", "relation": "requires", "status": "source_relation", "text": "not auto read"}])
                with urlopen(f"http://127.0.0.1:{server.server_port}/api/sources/{SOURCE['id']}", timeout=5) as response:
                    opened = json.load(response)
                self.assertEqual(opened["text"], "full body retained")
                self.assertEqual(opened["related_sources"], [{"id": "lo-dependency", "relation": "requires", "status": "source_relation"}])
                self.assertNotIn("related_sources", source)
                self.assertEqual(list((Path(directory) / "tool-contexts").glob("*.json")), [])
                app.kb.related_sources.side_effect = RuntimeError("deliberate fixture failure")
                with urlopen(f"http://127.0.0.1:{server.server_port}/api/sources/{SOURCE['id']}", timeout=5) as response:
                    opened = json.load(response)
                self.assertEqual(opened["text"], "full body retained")
                self.assertIn("暂不可读取", opened["related_sources_warning"])
            finally:
                server.shutdown(); server.server_close(); worker.join(); app.agents.close()


if __name__ == "__main__":
    unittest.main()
