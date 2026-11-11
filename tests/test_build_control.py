"""Cancellation is job-local, cooperative and visible across CLI processes."""
from contextvars import copy_context
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from advisor.build_control import BuildCancelled, BuildControl, build_control, check_cancelled, current_control


ROOT = Path(__file__).resolve().parents[1]


class BuildControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_no_control_is_a_noop_and_cancelled_exception_is_not_runtime_error(self):
        self.assertIsNone(current_control())
        check_cancelled()
        self.assertEqual(BuildCancelled.__bases__, (Exception,))
        self.assertNotIsInstance(BuildCancelled(), RuntimeError)

    def test_another_cli_marker_cancels_and_does_not_get_deleted(self):
        marker = self.root / "job.cancel"
        control = BuildControl(marker)
        self.assertFalse(control.cancelled)
        subprocess.run([sys.executable, "-c", "import pathlib,sys;pathlib.Path(sys.argv[1]).touch()", str(marker)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        self.assertTrue(control.cancelled)
        with self.assertRaises(BuildCancelled) as caught:
            control.check()
        self.assertNotIn(str(marker), str(caught.exception))
        self.assertTrue(marker.is_file())
        marker.unlink()
        self.assertTrue(control.cancelled, "Cancellation must remain latched after observing the request")

    def test_optional_event_cancels_without_creating_files(self):
        event = threading.Event()
        marker = self.root / "not-created" / "job.cancel"
        control = BuildControl(marker, event)
        self.assertFalse(control.cancelled)
        event.set()
        with self.assertRaises(BuildCancelled):
            control.check()
        event.clear()
        self.assertTrue(control.cancelled)
        self.assertFalse(marker.parent.exists())

    def test_nested_contexts_and_exception_restore_the_previous_control(self):
        first, second = BuildControl(self.root / "first"), BuildControl(self.root / "second")
        with build_control(first):
            self.assertIs(current_control(), first)
            with self.assertRaisesRegex(ValueError, "test exception"):
                with build_control(second):
                    self.assertIs(current_control(), second)
                    raise ValueError("test exception")
            self.assertIs(current_control(), first)
            with build_control(None):
                self.assertIsNone(current_control())
            self.assertIs(current_control(), first)
        self.assertIsNone(current_control())

    def test_cancelled_worker_context_does_not_leak_to_another_thread(self):
        event = threading.Event(); event.set()
        cancelled = BuildControl(self.root / "cancelled", event)
        other = BuildControl(self.root / "other")
        results = []
        def worker():
            results.append(current_control())
            check_cancelled()
            with build_control(other):
                check_cancelled()
                results.append(current_control())
            results.append(current_control())
        with build_control(cancelled):
            thread = threading.Thread(target=worker)
            thread.start(); thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            with self.assertRaises(BuildCancelled):
                check_cancelled()
        self.assertEqual(results, [None, other, None])

    def test_copy_context_has_explicit_independent_binding(self):
        first, second = BuildControl(self.root / "first"), BuildControl(self.root / "second")
        with build_control(first):
            copied = copy_context()
            def separate():
                self.assertIs(current_control(), first)
                with build_control(second):
                    self.assertIs(current_control(), second)
                self.assertIs(current_control(), first)
            copied.run(separate)
            self.assertIs(current_control(), first)
        self.assertIsNone(current_control())


if __name__ == "__main__":
    unittest.main()
