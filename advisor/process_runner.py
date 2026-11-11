"""Run an isolated CLI with bounded output and a deadline for its whole lifetime.

Only the explicitly spawned process tree is cleaned up. No shell is involved;
Windows uses a private kill-on-close Job Object where the host permits one.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import threading
import time

from .build_control import check_cancelled


class ProcessTimeoutError(subprocess.TimeoutExpired):
    """A bounded invocation exceeded its wall-clock deadline."""

    def __str__(self):
        return f"Subprocess exceeded the {self.timeout:g} second deadline."


class ProcessOutputLimitError(RuntimeError):
    """Combined stdout/stderr exceeded the requested byte budget."""

    def __init__(self, max_output: int):
        self.max_output = max_output
        super().__init__(f"Subprocess output exceeded the {max_output} byte limit.")


class _ProcessTree:
    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.job = None
        self.kernel = None
        self.accounting_type = None
        if os.name == "nt":
            self._attach_windows_job()

    def _attach_windows_job(self):
        # ctypes stays optional on non-Windows. Handle/pointer widths are
        # explicit: default ctypes return types truncate handles on 64-bit.
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IOCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in (
                "TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")]
            _fields_ += [(name, wintypes.DWORD) for name in (
                "TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]

        try:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel.CreateJobObjectW.restype = wintypes.HANDLE
            kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, wintypes.DWORD]
            kernel.SetInformationJobObject.restype = wintypes.BOOL
            kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel.TerminateJobObject.restype = wintypes.BOOL
            kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                         wintypes.DWORD, ctypes.c_void_p]
            kernel.QueryInformationJobObject.restype = wintypes.BOOL
            job = kernel.CreateJobObjectW(None, None)
            if not job:
                return
            limits = ExtendedLimit()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if (not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                    or not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(int(self.process._handle)))):
                kernel.CloseHandle(job)
                return
            self.job, self.kernel = job, kernel
            self.accounting_type = Accounting
        except (OSError, AttributeError, TypeError):
            # Hosts can forbid nested jobs. Cleanup then targets this PID tree.
            return

    def close(self):
        if self.job is not None:
            # Termination is asynchronous; retain the job until the parent has
            # been reaped instead of relying only on kill-on-close.
            self.kernel.TerminateJobObject(self.job, 1)
        elif os.name == "nt":
            # PID-specific /T is intentionally used; never kill by executable
            # name, since the user may have unrelated Codex/Python processes.
            if self.process.poll() is None:
                try:
                    subprocess.run(["taskkill.exe", "/PID", str(self.process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   stdin=subprocess.DEVNULL, timeout=3,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        else:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.process.poll() is None:
            try:
                self.process.kill()
            except OSError:
                pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        if self.job is not None:
            import ctypes
            until = time.monotonic() + 2
            while time.monotonic() < until:
                accounting = self.accounting_type()
                if not self.kernel.QueryInformationJobObject(self.job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                    break
                if not accounting.ActiveProcesses:
                    break
                time.sleep(0.01)
            self.kernel.CloseHandle(self.job)
            self.job = None
        if os.name == "nt" and self.process.returncode is not None:
            # An exception traceback can retain Popen long after this returns.
            # Release the Windows process handle now, after wait has cached its
            # return code, so caller-owned temporary working dirs can close.
            self.process._handle.Close()


def run_text(command: list[str], input_text: str, cwd: Path, timeout: int,
             max_output: int = 4 * 1024 * 1024) -> subprocess.CompletedProcess[str]:
    """Return CompletedProcess with UTF-8 text stdout/stderr and exit status.

    Nonzero exits are returned for the caller to interpret. Timeout raises
    ProcessTimeoutError (a subprocess.TimeoutExpired subclass); output overflow
    raises ProcessOutputLimitError. Neither exception message contains captured
    logs or stdin. Combined pipe bytes are bounded *before* decoding. Stdin is
    also written on a worker so an uncooperative reader cannot block timeout.
    The lifetime includes inherited pipes held open by descendant processes.
    A current build control raises BuildCancelled before launch or during wait;
    the same finally block cleans up only this invocation's process tree.
    """
    if not isinstance(command, list) or not command or not all(isinstance(v, str) and v for v in command):
        raise ValueError("command must be a nonempty list of nonempty strings")
    if not isinstance(input_text, str):
        raise TypeError("input_text must be a string")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
        raise ValueError("timeout must be a finite positive number")
    if isinstance(max_output, bool) or not isinstance(max_output, int) or max_output <= 0:
        raise ValueError("max_output must be a positive integer")
    check_cancelled()
    input_data = input_text.encode("utf-8")
    deadline = time.monotonic() + timeout
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    check_cancelled()
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               cwd=cwd, env=env, shell=False, bufsize=0, **kwargs)
    tree = _ProcessTree(process)
    buffers = [bytearray(), bytearray()]
    total = 0
    lock = threading.Lock()
    overflow = threading.Event()
    wake = threading.Event()
    finished = [threading.Event() for _ in range(3)]

    def read_pipe(stream, index):
        nonlocal total
        try:
            while chunk := stream.read(65536):
                with lock:
                    buffers[index].extend(chunk[:max(0, max_output - total)])
                    total += len(chunk)
                    if total > max_output:
                        overflow.set()
                wake.set()
                if overflow.is_set():
                    break
        except OSError:
            pass  # Expected when cleanup closes/kills an interrupted pipe.
        finally:
            stream.close()
            finished[index].set()
            wake.set()

    def write_input():
        try:
            data = memoryview(input_data)
            while data:
                count = process.stdin.write(data[:65536])
                if not count:
                    break
                data = data[count:]
        except (BrokenPipeError, OSError):
            pass  # A CLI may choose to exit without consuming all stdin.
        finally:
            process.stdin.close()
            finished[2].set()
            wake.set()

    workers = [threading.Thread(target=read_pipe, args=(stream, index), daemon=True)
               for index, stream in enumerate((process.stdout, process.stderr))]
    workers.append(threading.Thread(target=write_input, daemon=True))
    completed = False
    try:
        for worker in workers:
            worker.start()
        while True:
            check_cancelled()
            if overflow.is_set():
                raise ProcessOutputLimitError(max_output)
            if process.poll() is not None and all(event.is_set() for event in finished):
                completed = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessTimeoutError(command, timeout)
            wake.wait(min(0.05, remaining))
            wake.clear()
    finally:
        # KILL_ON_JOB_CLOSE / killpg also collects CLI descendants that close
        # their pipes and outlive a normally completed parent.
        tree.close()
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=0.5)
        if os.name == "nt" and not completed:
            # Windows can release a forcibly terminated process's cwd handle
            # one scheduler tick after the process is signalled/job count is
            # zero. Allow teardown to settle before the caller deletes its
            # temporary cwd. This is bounded and only applies to aborts.
            time.sleep(0.05)
    def decode(data):
        # Match subprocess.run(text=True)'s universal newline behavior.
        return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")

    return subprocess.CompletedProcess(command, process.returncode, decode(buffers[0]), decode(buffers[1]))
