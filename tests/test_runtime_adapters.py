"""Debug backend selection is lazy, replaceable, and independent of tools."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from advisor import runtime_adapters as adapters


class RuntimeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="runtime-adapter-", dir=Path(__file__).parent)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "codex-home"
        self.home.mkdir()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.environment = patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        self.environment.start()
        self.event = lambda event: None
        self.tool = lambda name, args: {"tool": name, "arguments": args}

    def tearDown(self):
        self.environment.stop()
        self.assertEqual(self.root.parent, Path(__file__).parent.resolve())
        self.temporary.cleanup()

    def connect(self, factory, **settings):
        return factory(cwd=self.workspace, on_event=self.event, on_tool=self.tool, **settings)

    def test_default_and_environment_selection_do_not_read_configuration(self):
        with patch.dict(os.environ, {}, clear=False), patch.object(adapters, "runtime_command", side_effect=AssertionError("eager config read")):
            os.environ.pop("SIM_ADVISOR_AGENT_BACKEND", None)
            factory = adapters.get_runtime_factory()
            self.assertEqual(factory.backend_id, "codex-app-server")
            with patch.object(adapters, "codex_command", return_value="codex.exe"):
                status = factory.status()
                self.assertTrue(status["available"])
                self.assertEqual(status["mode"], "app-server-stdio")
                self.assertEqual(status["backend_id"], "codex-app-server")
            os.environ["SIM_ADVISOR_AGENT_BACKEND"] = "none"
            self.assertEqual(adapters.get_runtime_factory().backend_id, "none")
            self.assertEqual(adapters.get_runtime_factory("codex").backend_id, "codex-app-server")

    def test_command_disables_each_mcp_name_without_copying_config_values(self):
        config = self.home / "config.toml"
        original = ('[mcp_servers.node_repl]\ncommand="PRIVATE_COMMAND"\n'
                    '[mcp_servers.second-server]\nurl="PRIVATE_URL"\n')
        config.write_text(original, encoding="utf-8")
        auth = self.home / "auth.json"
        auth.write_text('{"token":"SYNTHETIC_AUTH_SECRET"}', encoding="utf-8")
        original_read = Path.read_text
        reads = []
        def read(path, *args, **kwargs):
            reads.append(path.resolve())
            return original_read(path, *args, **kwargs)
        with patch.object(Path, "read_text", read):
            argv = adapters.runtime_command("codex.exe")
        self.assertEqual(argv, ["codex.exe", "app-server", "--stdio", "-c", "mcp_servers.node_repl.enabled=false",
                                "-c", "mcp_servers.second-server.enabled=false"])
        self.assertEqual(reads, [config.resolve()])
        self.assertNotIn("PRIVATE", " ".join(argv))
        self.assertNotIn("SYNTHETIC", " ".join(argv))
        self.assertEqual(config.read_text(encoding="utf-8"), original)
        self.assertEqual(auth.read_text(encoding="utf-8"), '{"token":"SYNTHETIC_AUTH_SECRET"}')
        self.assertEqual(sorted(p.name for p in self.workspace.iterdir()), [])

    def test_codex_factory_forwards_settings_and_exposes_runtime_backend(self):
        (self.home / "config.toml").write_text('[mcp_servers.node_repl]\ncommand="local"\n', encoding="utf-8")
        factory = adapters.get_runtime_factory("codex")
        with patch.object(adapters, "codex_command", return_value="codex.exe"), patch("subprocess.Popen") as popen:
            runtime = self.connect(factory, model="gpt-6.1-sol", reasoning_effort="high")
            self.assertEqual(runtime.backend_id, "codex-app-server")
            self.assertEqual(runtime.status()["backend_id"], "codex-app-server")
            self.assertEqual(runtime.status()["reasoning_effort"], "high")
            self.assertEqual(runtime.model, "gpt-6.1-sol")
            self.assertEqual(runtime.command[-1], "mcp_servers.node_repl.enabled=false")
            self.assertIs(runtime.on_event, self.event)
            self.assertIs(runtime.on_tool, self.tool)
            self.assertEqual(runtime.cwd, self.workspace)
            self.assertTrue(all(callable(getattr(runtime, name)) for name in ("start", "send", "interrupt", "close", "status")))
            popen.assert_not_called()
            runtime.close()

    def test_missing_codex_reports_unavailable_without_reading_configuration(self):
        factory = adapters.get_runtime_factory("codex")
        with patch.object(adapters, "codex_command", return_value=None), patch.object(adapters, "runtime_command") as command:
            self.assertFalse(factory.status()["available"])
            with self.assertRaisesRegex(adapters.RuntimeUnavailableError, "SIM_ADVISOR_CODEX"):
                self.connect(factory)
            command.assert_not_called()

    def test_none_disables_only_runtime_without_discovering_codex(self):
        with patch.object(adapters, "codex_command", side_effect=AssertionError("Codex discovery not allowed")), patch.object(adapters, "runtime_command", side_effect=AssertionError("configuration read not allowed")):
            factory = adapters.get_runtime_factory("none")
            status = factory.status()
            self.assertEqual(status["backend_id"], "none")
            self.assertFalse(status["available"])
            self.assertEqual(status["mode"], "disabled")
            with self.assertRaisesRegex(adapters.RuntimeUnavailableError, "已禁用"):
                self.connect(factory)
            # Backend selection has no lifecycle or permission control over tools.
            self.assertEqual(self.tool("get_plan", {}), {"tool": "get_plan", "arguments": {}})

    def test_unknown_backend_never_falls_back(self):
        for backend in ("", "not-installed", "codex-exec"):
            with self.subTest(backend=backend), self.assertRaises(ValueError):
                adapters.get_runtime_factory(backend)
        with self.assertRaises(TypeError):
            adapters.get_runtime_factory(False)

    def test_plain_injected_factory_needs_no_codex_or_registration(self):
        calls, result = [], object()
        def factory(**kwargs):
            calls.append(kwargs)
            return result
        with patch.object(adapters, "codex_command", side_effect=AssertionError("must not discover Codex")):
            selected = adapters.get_runtime_factory(factory)
            self.assertEqual(selected.status()["backend_id"], "custom")
            self.assertTrue(selected.status()["available"])
            self.assertIs(self.connect(selected, model="other-model", reasoning_effort="low"), result)
        self.assertEqual(calls, [{"cwd": self.workspace, "on_event": self.event, "on_tool": self.tool,
                                 "model": "other-model", "reasoning_effort": "low"}])
        self.assertNotIn("command", calls[0])

    def test_factory_object_retains_its_own_status_and_identity(self):
        class CustomFactory:
            backend_id = "external-debug"
            def status(self):
                return {"available": False, "mode": "external", "detail": "Not connected", "backend_id": self.backend_id}
            def __call__(self, **kwargs):
                return kwargs
        original = CustomFactory()
        self.assertIs(adapters.get_runtime_factory(original), original)
        self.assertFalse(adapters.get_runtime_factory(original).status()["available"])

    def test_bad_mcp_configuration_is_safe_and_only_fails_on_connection(self):
        for text in ('[mcp_servers."unsafe.name"]\ncommand="SECRET_VALUE"', 'mcp_servers="SECRET_VALUE"', 'broken="SECRET_VALUE'):
            with self.subTest(text=text):
                (self.home / "config.toml").write_text(text, encoding="utf-8")
                factory = adapters.get_runtime_factory("codex")
                with patch.object(adapters, "codex_command", return_value="codex.exe"):
                    self.assertTrue(factory.status()["available"])
                    with self.assertRaises(adapters.RuntimeUnavailableError) as caught:
                        self.connect(factory)
                self.assertNotIn("SECRET_VALUE", str(caught.exception))

    def test_absent_configuration_preserves_default_command_and_settings(self):
        self.assertEqual(adapters.runtime_command("codex.exe"), ["codex.exe", "app-server", "--stdio"])
        with patch.object(adapters, "codex_command", return_value="codex.exe"):
            runtime = self.connect(adapters.get_runtime_factory("codex"))
        self.assertEqual(runtime.model, "gpt-6.1-sol")
        self.assertEqual(runtime.reasoning_effort, "xhigh")
        runtime.close()


if __name__ == "__main__":
    unittest.main()
