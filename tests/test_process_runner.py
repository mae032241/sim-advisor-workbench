"""Deadline/output checks exercise actual children, pipes, and tree cleanup."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

from advisor.build_control import BuildCancelled, BuildControl, build_control
from advisor.process_runner import ProcessOutputLimitError, ProcessTimeoutError, run_text


ROOT = Path(__file__).resolve().parents[1]


def pid_running(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            status = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(status))) and status.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed orphan can wait for its OS reaper; it no longer executes code.
    status = Path(f"/proc/{pid}/stat")
    if status.is_file() and status.read_text().split(")", 1)[1].split()[0] == "Z":
        return False
    return True


class ProcessRunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def run_python(self, code, *, input_text="", timeout=4, max_output=4096, args=()):
        return run_text([sys.executable, "-c", code, *args], input_text, self.root, timeout, max_output)

    def test_utf8_input_output_and_nonzero_exit(self):
        result = self.run_python("import sys; x=sys.stdin.read(); print(x,end=''); print('diagnostic',file=sys.stderr); sys.exit(7)",
                                 input_text="溫度及應力\n第二行")
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "溫度及應力\n第二行")
        self.assertEqual(result.stderr, "diagnostic\n")

    def test_no_shell_argument_interpretation(self):
        value = "a&b;$(would-be-command)"
        result = self.run_python("import sys; print(sys.argv[1],end='')", args=(value,))
        self.assertEqual(result.stdout, value)

    def test_concurrent_stdout_stderr_exceed_combined_limit(self):
        code = "import os,threading; t=threading.Thread(target=lambda:os.write(2,b'e'*100000));t.start();os.write(1,b'o'*100000);t.join()"
        with self.assertRaises(ProcessOutputLimitError) as caught:
            self.run_python(code, max_output=1000)
        self.assertEqual(caught.exception.max_output, 1000)
        self.assertNotIn("eeee", str(caught.exception))

    def test_exact_combined_boundary_allowed(self):
        result = self.run_python("import os;os.write(1,b'o'*128);os.write(2,b'e'*256)", max_output=384)
        self.assertEqual(result.stdout, "o" * 128)
        self.assertEqual(result.stderr, "e" * 256)

    def test_limit_counts_bytes_not_unicode_characters(self):
        with self.assertRaises(ProcessOutputLimitError):
            self.run_python("import sys;sys.stdout.buffer.write(bytes([0xe7,0x86,0xb1]))", max_output=2)

    def test_invalid_utf8_is_replaced(self):
        result = self.run_python("import os;os.write(1,bytes([0xff]))")
        self.assertEqual(result.stdout, "\ufffd")

    def test_timeout_also_covers_blocked_stdin(self):
        started = time.monotonic()
        with self.assertRaises(ProcessTimeoutError) as caught:
            self.run_python("import time;time.sleep(20)", input_text="private-context" * 100000, timeout=0.2)
        self.assertIsInstance(caught.exception, subprocess.TimeoutExpired)
        self.assertNotIn("private-context", str(caught.exception))
        self.assertLess(time.monotonic() - started, 4)

    def test_early_exit_does_not_block_large_stdin(self):
        result = self.run_python("import sys;sys.exit(3)", input_text="x" * 1000000)
        self.assertEqual(result.returncode, 3)

    def test_descendant_holding_pipes_is_collected_after_parent_exit(self):
        marker = self.root / "unexpected-child.txt"
        child = "import sys,time;time.sleep(1);open(sys.argv[1],'w').write('alive')"
        parent = "import subprocess,sys;subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);print('spawned',flush=True)"
        with self.assertRaises(ProcessTimeoutError):
            self.run_python(parent, args=(child, str(marker)), timeout=0.3)
        time.sleep(1.1)
        self.assertFalse(marker.exists(), "descendant survived the timed-out invocation")

    def test_background_descendant_cleaned_after_normal_completion(self):
        marker = self.root / "unexpected-background.txt"
        child = "import sys,time;time.sleep(1);open(sys.argv[1],'w').write('alive')"
        parent = "import subprocess,sys;subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);print('done')"
        result = self.run_python(parent, args=(child, str(marker)))
        self.assertEqual(result.stdout.strip(), "done")
        time.sleep(1.1)
        self.assertFalse(marker.exists(), "background descendant survived normal completion")

    def test_unrelated_process_with_same_executable_survives(self):
        marker = self.root / "unrelated.txt"
        unrelated = subprocess.Popen([sys.executable, "-c", "import sys,time;time.sleep(.7);open(sys.argv[1],'w').write('alive')", str(marker)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with self.assertRaises(ProcessTimeoutError):
                self.run_python("import time;time.sleep(20)", timeout=0.2)
            self.assertEqual(unrelated.wait(timeout=3), 0)
            self.assertEqual(marker.read_text(), "alive")
        finally:
            if unrelated.poll() is None:
                unrelated.kill()
                unrelated.wait(timeout=3)

    def test_invalid_arguments_and_missing_executable(self):
        for timeout in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.run_python("pass", timeout=timeout)
        for maximum in (0, -1, True, 1.2):
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                self.run_python("pass", max_output=maximum)
        with self.assertRaises(ValueError):
            run_text([], "", self.root, 1)
        with self.assertRaises(TypeError):
            run_text([sys.executable], None, self.root, 1)
        with self.assertRaises(OSError):
            run_text([str(self.root / "missing-executable")], "", self.root, 1)

    def test_cancelled_before_start_never_spawns(self):
        marker = self.root / "before.cancel"; marker.touch()
        with build_control(BuildControl(marker)), patch("advisor.process_runner.subprocess.Popen") as spawn:
            with self.assertRaises(BuildCancelled):
                self.run_python("raise AssertionError('must not start')")
        spawn.assert_not_called()

    def test_event_cancellation_interrupts_a_real_process_with_blocked_stdin(self):
        event = threading.Event()
        ready = self.root / "event-ready"
        errors = []
        def cancel():
            try:
                until = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < until:
                    time.sleep(0.01)
                if not ready.exists():
                    raise AssertionError("Child did not start before cancellation")
                event.set()
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=cancel, daemon=True); thread.start()
        started = time.monotonic()
        try:
            with build_control(BuildControl(self.root / "unused-marker", event)), self.assertRaises(BuildCancelled):
                self.run_python("import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)",
                                args=(str(ready),), input_text="private-input" * 100000, timeout=5)
        finally:
            thread.join(timeout=4)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
        self.assertLess(time.monotonic() - started, 4)
        self.assertFalse(pid_running(int(ready.read_text())))

    def test_cross_cli_cancel_kills_owned_parent_and_child_but_not_unrelated_python(self):
        marker, ready, child_ready = (self.root / name for name in ("job.cancel", "parent.json", "child.pid"))
        release, unrelated_done = self.root / "release-unrelated", self.root / "unrelated-survived"
        unrelated = subprocess.Popen([sys.executable, "-c",
            "import pathlib,sys,time;release=pathlib.Path(sys.argv[1]);\nwhile not release.exists():time.sleep(.01)\npathlib.Path(sys.argv[2]).write_text('alive')",
            str(release), str(unrelated_done)], cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child = "import pathlib,os,sys,time,json;pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'parent':os.getppid()}));time.sleep(30)"
        parent = ("import subprocess,sys,pathlib,json,os,time;"
                  "p=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[3]],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
                  "pathlib.Path(sys.argv[2]).write_text(json.dumps({'parent':os.getpid(),'child':p.pid}));time.sleep(30)")
        errors = []
        def cancel_from_cli():
            try:
                until = time.monotonic() + 4
                while not (ready.exists() and child_ready.exists()) and time.monotonic() < until:
                    time.sleep(0.01)
                if not (ready.exists() and child_ready.exists()):
                    raise AssertionError("Real parent/descendant did not start")
                subprocess.run([sys.executable, "-c", "import pathlib,sys;pathlib.Path(sys.argv[1]).touch()", str(marker)],
                               check=True, timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as error:
                errors.append(error)
                marker.touch()  # Ensure test cleanup still ends the owned invocation.
        thread = threading.Thread(target=cancel_from_cli, daemon=True); thread.start()
        try:
            with build_control(BuildControl(marker)), self.assertRaises(BuildCancelled):
                self.run_python(parent, args=(child, str(ready), str(child_ready)), timeout=7)
            thread.join(timeout=4)
            self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
            owned = json.loads(ready.read_text())
            child_runtime = json.loads(child_ready.read_text())
            # Windows venv python.exe can be a launcher with a distinct actual
            # interpreter PID. Check both processes, without assuming exec().
            self.assertIn(child_runtime["parent"], {owned["parent"], owned["child"]})
            owned["child_runtime"] = child_runtime["pid"]
            until = time.monotonic() + 2
            while any(pid_running(pid) for pid in owned.values()) and time.monotonic() < until:
                time.sleep(.01)
            self.assertTrue(all(not pid_running(pid) for pid in owned.values()), "Cancelled owned process tree is still running")
            self.assertIsNone(unrelated.poll(), "Cancellation killed an unrelated same-executable process")
            release.touch(); self.assertEqual(unrelated.wait(timeout=3), 0)
            self.assertEqual(unrelated_done.read_text(), "alive")
        finally:
            thread.join(timeout=4)
            if unrelated.poll() is None:
                unrelated.kill(); unrelated.wait(timeout=3)

    def test_cancelled_context_does_not_cancel_an_uncontrolled_worker(self):
        marker = self.root / "cancelled-context"; marker.touch()
        results, errors = [], []
        def other_thread():
            try:
                results.append(self.run_python("print('other build completes')").stdout)
            except Exception as error:
                errors.append(error)
        with build_control(BuildControl(marker)):
            thread = threading.Thread(target=other_thread)
            thread.start(); thread.join(timeout=4)
            self.assertFalse(thread.is_alive())
            with self.assertRaises(BuildCancelled):
                self.run_python("pass")
        self.assertEqual(errors, [])
        self.assertEqual(results, ["other build completes\n"])


if __name__ == "__main__":
    unittest.main()
