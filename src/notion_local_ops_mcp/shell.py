from __future__ import annotations

import threading
from pathlib import Path

from .config import COMMAND_OUTPUT_BYTES, TERM_GRACE_SECONDS, KILL_GRACE_SECONDS
from .execution import EXECUTION_SLOTS, ProcessControl, run_process, validate_seconds


TIMEOUT_EXIT_CODE = -1


def run_command(*, command: str, cwd: Path, timeout: int) -> dict[str, object]:
    result = {"success": False, "command": command[:4096], "command_truncated": len(command) > 4096,
              "cwd": str(cwd), "resolved_cwd": str(cwd),
              "exit_code": TIMEOUT_EXIT_CODE, "stdout": "", "stderr": "", "timed_out": False}
    if not cwd.is_dir():
        code = "cwd_not_directory" if cwd.exists() else "cwd_not_found"
        return {**result, "error": {"code": code, "message": f"Invalid working directory: {cwd}"}}
    try:
        validate_seconds(timeout)
        if len(command.encode("utf-8")) > 65536:
            raise ValueError("Command exceeds 64 KiB; put large commands in a script file.")
    except (ValueError, TypeError) as exc:
        return {**result, "error": {"code": "invalid_arguments", "message": str(exc)}}
    if not EXECUTION_SLOTS.acquire(blocking=False):
        return {**result, "error": {"code": "capacity_exceeded", "message": "Execution capacity is full; retry later."}}
    control = ProcessControl()
    finished = threading.Event()
    chunks = {"stdout": bytearray(), "stderr": bytearray()}
    lock = threading.Lock()

    def output(name, data):
        with lock:
            chunks[name].extend(data)

    def worker():
        try:
            outcome = run_process(args=command, shell=True, cwd=cwd, timeout=timeout, control=control,
                                  output_limit=COMMAND_OUTPUT_BYTES, on_output=output)
            with lock:
                result.update(outcome)
                result["success"] = outcome["status"] == "succeeded"
                result["timed_out"] = outcome["execution_timed_out"]
                if result["timed_out"]:
                    result.update(exit_code=TIMEOUT_EXIT_CODE, timeout=timeout, hint="consider_delegate_task",
                                  error={"code": "timed_out", "message": f"Command exceeded its {timeout}s timeout."})
                elif result["exit_code"] is None:
                    result["exit_code"] = TIMEOUT_EXIT_CODE
        finally:
            EXECUTION_SLOTS.release()
            finished.set()

    thread = threading.Thread(target=worker, daemon=True)
    try:
        thread.start()
    except Exception:
        EXECUTION_SLOTS.release()
        raise
    completed = finished.wait(timeout + TERM_GRACE_SECONDS + KILL_GRACE_SECONDS + 1)
    with lock:
        if not completed:
            result.update(success=False, completed=False, cleanup_failed=True,
                          error={"code": "cleanup_failed", "message": "Process cleanup is unconfirmed; capacity remains occupied."})
        return {**result, **{name: bytes(data).decode("utf-8") for name, data in chunks.items()}}
