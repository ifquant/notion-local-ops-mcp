"""Bounded non-interactive process execution shared by shell and delegate tools."""
from __future__ import annotations

import codecs
import math
import logging
import os
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import MAX_EXECUTIONS, TERM_GRACE_SECONDS, KILL_GRACE_SECONDS


EXECUTION_SLOTS = threading.BoundedSemaphore(MAX_EXECUTIONS)


def validate_seconds(value: float, *, maximum: float | None = None, allow_zero: bool = False) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        raise ValueError("timeout must be finite and positive (wait timeout may be zero)")
    if maximum is not None and value > maximum:
        raise ValueError(f"timeout exceeds {maximum}s; wait in separate calls")


@dataclass
class ProcessControl:
    lock: threading.RLock = field(default_factory=threading.RLock)
    process: subprocess.Popen | None = None
    stop_reason: str | None = None
    finished: bool = False
    cleanup_failed: bool = False
    dropped_bytes: int = 0

    def request_stop(self, reason: str) -> bool:
        with self.lock:
            if self.finished:
                return False
            if self.process is not None and self.process.poll() is not None and not group_alive(self.process.pid):
                return False
            if self.stop_reason is None:
                self.stop_reason = reason
            return self.stop_reason == reason


def group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        logging.error("Cannot signal managed process group %s", process.pid)


def run_process(
    *, args: list[str] | str, shell: bool, cwd: Path, timeout: float,
    control: ProcessControl, output_limit: int,
    on_output: Callable[[str, bytes], None],
    on_cleanup_failed: Callable[[str], None] | None = None,
    term_grace: float = TERM_GRACE_SECONDS, kill_grace: float = KILL_GRACE_SECONDS,
) -> dict[str, object]:
    """Caller owns admission. Keep control/slot live if group cleanup is unconfirmed."""
    validate_seconds(timeout)
    if os.name != "posix":
        return {"status": "failed", "error": "Managed process groups require POSIX; unsupported on this platform.",
                "exit_code": None, "execution_timed_out": False, "dropped_bytes": 0}
    selector = selectors.DefaultSelector()
    process = None
    error = None
    retained = dropped = 0
    started = time.monotonic()
    stopping_at = None
    killed_at = None
    decoders = {name: codecs.getincrementaldecoder("utf-8")("replace") for name in ("stdout", "stderr")}

    def report_cleanup(message: str) -> None:
        if not control.cleanup_failed:
            control.cleanup_failed = True
            if on_cleanup_failed:
                try:
                    on_cleanup_failed(message)
                except Exception:
                    logging.exception("Could not report process cleanup failure")

    def emit(name: str, raw: bytes, *, final: bool = False) -> None:
        nonlocal retained, dropped, error
        encoded = decoders[name].decode(raw, final=final).encode("utf-8")
        keep = encoded[:max(0, output_limit - retained)].decode("utf-8", errors="ignore").encode("utf-8")
        dropped += len(encoded) - len(keep)
        with control.lock:
            control.dropped_bytes = dropped
        if keep and error is None:
            try:
                on_output(name, keep)
                retained += len(keep)
            except Exception as exc:
                error = f"output_write_failed: {exc}"[:1000]
                control.request_stop("io_error")

    try:
        with control.lock:
            if control.stop_reason:
                control.finished = True
                return {"status": "cancelled", "exit_code": None, "execution_timed_out": False,
                        "dropped_bytes": 0, "stop_reason": control.stop_reason}
            process = subprocess.Popen(args, cwd=str(cwd), shell=shell, text=False,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
            control.process = process
        for name in ("stdout", "stderr"):
            pipe = getattr(process, name)
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        while True:
            # Observe natural completion before accepting a deadline stop.
            with control.lock:
                exited = process.poll() is not None
                alive = group_alive(process.pid)
                if exited and not alive and not selector.get_map():
                    control.finished = True
                    break
                if time.monotonic() - started >= timeout and not control.stop_reason:
                    if exited and not alive:
                        # The managed group exited but a detached descendant retained a pipe.
                        error = "output_incomplete: detached process retained an output pipe"
                        control.stop_reason = "io_error"
                    else:
                        control.request_stop("timeout")
                reason = control.stop_reason
            now = time.monotonic()
            if reason and stopping_at is None:
                _signal_group(process, signal.SIGTERM)
                stopping_at = now
            if stopping_at is not None and killed_at is None and now - stopping_at >= term_grace:
                _signal_group(process, signal.SIGKILL)
                killed_at = now
            if killed_at is not None and now - killed_at >= kill_grace:
                # A detached child can retain a pipe; do not wait forever for its EOF.
                for key in list(selector.get_map().values()):
                    emit(key.data, b"", final=True)
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                if process.poll() is not None and not group_alive(process.pid):
                    control.finished = True
                    break
                report_cleanup("Unable to confirm process-group exit; execution slot remains occupied.")
                # Reconciliation stays in this worker, never in the MCP request.
                time.sleep(0.1)
                continue
            for key, _ in selector.select(timeout=0.02):
                raw = os.read(key.fileobj.fileno(), 8192)
                if raw:
                    emit(key.data, raw)
                else:
                    emit(key.data, b"", final=True)
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
    except Exception as exc:
        error = f"execution_failed: {exc}"[:1000]
        if process is not None:
            _signal_group(process, signal.SIGKILL)
            # Do not release capacity while a child group is still alive.
            while process.poll() is None or group_alive(process.pid):
                report_cleanup(error)
                time.sleep(0.1)
        control.finished = True
    finally:
        for key in list(selector.get_map().values()):
            key.fileobj.close()
        selector.close()
    reason = control.stop_reason
    return {
        "status": "failed" if error or reason in {"timeout", "io_error"} else (
            "cancelled" if reason == "cancelled" else "succeeded" if process and process.returncode == 0 else "failed"),
        "exit_code": process.returncode if process else None,
        "execution_timed_out": reason == "timeout", "stop_reason": reason,
        "dropped_bytes": dropped, "output_truncated": dropped > 0,
        "error": error,
        "cleanup_failed": False,
    }
