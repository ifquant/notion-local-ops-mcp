from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from pathlib import PureWindowsPath

from .tasks import TaskStore
from .config import LOG_RETAIN_BYTES, LOG_READ_BYTES, MAX_WAIT_SECONDS
from .execution import EXECUTION_SLOTS, ProcessControl, run_process, validate_seconds


TERMINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled"}
ALLOWED_COMMIT_MODES = {"allowed", "required", "forbidden"}
IS_WINDOWS = os.name == "nt"


def _split_command(command: str) -> list[str]:
    return shlex.split(command)


def _binary_name(binary: str) -> str:
    if IS_WINDOWS:
        return PureWindowsPath(binary).stem.lower()
    return Path(binary).stem.lower()


def _resolve_delegate_command_parts(command: str) -> list[str]:
    parts = _split_command(command)
    if not IS_WINDOWS or not parts:
        return parts
    if _binary_name(parts[0]) not in {"codex", "claude"}:
        return parts
    resolved = shutil.which(parts[0])
    if resolved:
        parts[0] = resolved
    return parts


def _decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


def _command_available(command: str | None) -> bool:
    if not command:
        return False
    parts = _split_command(command)
    if not parts:
        return False
    binary = parts[0]
    if Path(binary).exists():
        return True
    return shutil.which(binary) is not None


def _summarize(stdout: str, stderr: str) -> str:
    for candidate in (stdout.strip(), stderr.strip()):
        if candidate:
            return candidate.splitlines()[-1]
    return ""


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _extract_structured_output(text: str) -> object | None:
    """Best-effort JSON extraction from tool output."""
    stripped = (text or "").strip()
    if not stripped:
        return None

    # Prefer the last fenced json block if present.
    matches = _JSON_BLOCK_RE.findall(stripped)
    if matches:
        candidate = matches[-1].strip()
        if candidate:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

    # Fallback: entire payload is JSON.
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _cwd_error(command: str, cwd: Path) -> dict[str, object] | None:
    if not cwd.exists():
        return {
            "success": False,
            "error": {
                "code": "cwd_not_found",
                "message": f"Working directory not found: {cwd}",
            },
            "cwd": str(cwd),
            "command": command,
        }
    if not cwd.is_dir():
        return {
            "success": False,
            "error": {
                "code": "cwd_not_directory",
                "message": f"Working directory is not a directory: {cwd}",
            },
            "cwd": str(cwd),
            "command": command,
        }
    return None


@dataclass(frozen=True)
class Invocation:
    args: list[str] | str
    use_shell: bool


class ExecutorRegistry:
    def __init__(self, *, store: TaskStore, codex_command: str | None, claude_command: str | None,
                 slots=None, log_limit: int = LOG_RETAIN_BYTES) -> None:
        self.store = store
        self.codex_command = codex_command
        self.claude_command = claude_command
        self.slots = slots if slots is not None else EXECUTION_SLOTS
        self.log_limit = log_limit
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._generation = 0
        self._controls: dict[str, ProcessControl] = {}
        self._completion_events: dict[str, threading.Event] = {}
        self._failures: dict[str, dict[str, object]] = {}

    def _notify(self) -> None:
        with self._changed:
            self._generation += 1
            self._changed.notify_all()

    def _persist(self, task_id: str, **fields) -> None:
        try:
            self.store.update(task_id, **fields)
        except Exception as exc:
            logging.error("Task %s metadata write failed: %s", task_id, exc)
            with self._lock:
                self._failures[task_id] = {
                    "task_id": task_id, "status": fields.get("status", "running"),
                    "persistence_error": str(exc)[:1000], "error": "metadata_write_failed",
                    **fields,
                }
            raise
        finally:
            self._notify()

    def _submit(self, *, task: str, executor: str, cwd: Path, timeout: float,
                invocation_factory, context_files=None, metadata=None, parse=True) -> dict[str, object]:
        try:
            validate_seconds(timeout)
            if len(json.dumps({"task": task, "context_files": context_files, "metadata": metadata}, ensure_ascii=False).encode()) > 65536:
                raise ValueError("Task input exceeds 64 KiB; put large context in files and pass paths.")
        except (ValueError, TypeError) as exc:
            return {"success": False, "error": {"code": "invalid_arguments", "message": str(exc)}}
        cwd_error = _cwd_error(task, cwd)
        if cwd_error:
            return cwd_error
        if not self.slots.acquire(blocking=False):
            return {"success": False, "error": {"code": "capacity_exceeded", "message": "Execution capacity is full; retry later."}}
        try:
            created = self.store.create(task=task, executor=executor, cwd=str(cwd), timeout=timeout,
                                        context_files=context_files, metadata=metadata)
            task_id = created["task_id"]
            with self._lock:
                self._controls[task_id] = ProcessControl()
                self._completion_events[task_id] = threading.Event()
            thread = threading.Thread(target=self._execute,
                                      args=(task_id, cwd, timeout, invocation_factory, parse), daemon=True)
            thread.start()
        except Exception as exc:
            self.slots.release()
            if "task_id" in locals():
                with self._lock:
                    self._controls.pop(task_id, None)
                    self._completion_events.pop(task_id, None)
                try:
                    self.store.update(task_id, status="failed", error=str(exc)[:1000])
                except Exception:
                    pass
            return {"success": False, "error": {"code": "submit_failed", "message": str(exc)[:1000]}}
        return {"task_id": task_id, "executor": executor, "status": created["status"], "resolved_cwd": str(cwd)}

    def submit(self, *, task: str | None, goal: str | None = None, executor: str,
               cwd: Path, timeout: int, context_files: list[str] | None = None,
               acceptance_criteria: list[str] | None = None, verification_commands: list[str] | None = None,
               commit_mode: str = "allowed", output_schema: dict[str, object] | None = None,
               parse_structured_output: bool = True) -> dict[str, object]:
        task, goal = (task or "").strip(), (goal or "").strip()
        if not task and not goal:
            raise ValueError("delegate_task requires task or goal.")
        if commit_mode not in ALLOWED_COMMIT_MODES:
            raise ValueError(f"Unsupported commit_mode: {commit_mode}")
        chosen_executor, command = self._resolve_executor(executor)
        context_files = [str((cwd / p).resolve()) for p in (context_files or [])]
        metadata = {"goal": goal or None, "acceptance_criteria": acceptance_criteria or [],
                    "verification_commands": verification_commands or [], "commit_mode": commit_mode,
                    "output_schema": output_schema or None, "parse_structured_output": parse_structured_output}
        return self._submit(
            task=task or goal, executor=chosen_executor, cwd=cwd, timeout=timeout,
            context_files=context_files, metadata=metadata, parse=parse_structured_output,
            invocation_factory=lambda: self._build_invocation(
                executor_name=chosen_executor, command=command, task=task or None, goal=goal or None,
                cwd=cwd, context_files=context_files, acceptance_criteria=acceptance_criteria or [],
                verification_commands=verification_commands or [], commit_mode=commit_mode))

    def submit_command(self, *, command: str, cwd: Path, timeout: int) -> dict[str, object]:
        return self._submit(task=command, executor="shell", cwd=cwd, timeout=timeout,
                            invocation_factory=lambda: Invocation(args=command, use_shell=True))

    def get(self, task_id: str, *, cursor: str | None = None, max_bytes: int = LOG_READ_BYTES) -> dict[str, object]:
        try:
            meta = self.store.snapshot(task_id, cursor=cursor, max_bytes=max_bytes)
        except (OSError, ValueError, TypeError) as exc:
            meta = {"success": False, "task_id": task_id,
                    "error": {"code": "task_read_failed", "message": str(exc)[:1000]}, "completed": False}
        with self._lock:
            failure = self._failures.get(task_id)
            control = self._controls.get(task_id)
        if failure:
            meta.update(failure)
            meta["completed"] = failure.get("status") in TERMINAL_TASK_STATUSES
            meta["success"] = False
        meta["control_available"] = control is not None
        if control:
            with control.lock:
                meta["cancel_requested"] = control.stop_reason == "cancelled"
                meta["cleanup_failed"] = control.cleanup_failed
                meta["dropped_bytes"] = control.dropped_bytes
                meta["output_truncated"] = control.dropped_bytes > 0
        meta.setdefault("output_truncated", False)
        meta.setdefault("dropped_bytes", 0)
        return meta

    def wait(self, task_id: str, timeout: float, poll_interval: float = 0.5, *,
             cursor: str | None = None, max_bytes: int = LOG_READ_BYTES,
             return_on_output: bool = False) -> dict[str, object]:
        try:
            validate_seconds(timeout, maximum=MAX_WAIT_SECONDS, allow_zero=True)
            validate_seconds(poll_interval)
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": {"code": "invalid_arguments", "message": str(exc)}}
        deadline = time.monotonic() + timeout
        # Capture a starting position so existing tail output doesn't wake an output wait.
        if return_on_output and cursor is None:
            cursor = self.get(task_id, max_bytes=max_bytes).get("next_cursor")
        while True:
            with self._changed:
                generation = self._generation
            meta = self.get(task_id, cursor=cursor, max_bytes=max_bytes)
            output = bool(meta.get("stdout") or meta.get("stderr"))
            done = bool(meta.get("completed"))
            if done or meta.get("success") is False or meta.get("cleanup_failed") or (return_on_output and output):
                meta["wait_timed_out"] = False
                meta["timed_out"] = False  # legacy wait meaning, never persisted
                return meta
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                meta["wait_timed_out"] = True
                meta["timed_out"] = True
                return meta
            with self._changed:
                if generation == self._generation:
                    self._changed.wait(min(remaining, max(0.05, poll_interval)))

    def cancel(self, task_id: str) -> dict[str, object]:
        meta = self.get(task_id)
        if meta.get("completed") or meta.get("success") is False:
            return {**meta, "cancelled": meta.get("status") == "cancelled"}
        with self._lock:
            control = self._controls.get(task_id)
        if control is None:
            return {**meta, "success": False, "cancelled": False,
                    "error": {"code": "control_unavailable", "message": "This process does not own the active task; it was not cancelled."}}
        accepted = control.request_stop("cancelled")
        self._notify()
        # Bounded acknowledgement: do not report cancellation until cleanup is confirmed.
        result = self.wait(task_id, timeout=min(5.0, MAX_WAIT_SECONDS))
        return {**result, "cancel_requested": accepted, "cancelled": result.get("status") == "cancelled"}

    def _resolve_executor(self, executor: str) -> tuple[str, str]:
        if executor not in {"auto", "codex", "claude-code"}:
            raise ValueError(f"Unsupported executor: {executor}")
        for name, command in (("codex", self.codex_command), ("claude-code", self.claude_command)):
            if executor in ("auto", name) and _command_available(command):
                return name, command or ""
        raise RuntimeError("Requested delegate executor command is not available.")

    def _execute(self, task_id: str, cwd: Path, timeout: float, invocation_factory, parse: bool) -> None:
        with self._lock:
            control = self._controls[task_id]
        def output(stream, data):
            self.store.append_log(task_id, stream, data)
            self._notify()
        def cleanup_error(message):
            try:
                self._persist(task_id, cleanup_failed=True, error=message)
            except Exception:
                pass
        try:
            self._persist(task_id, status="running")
            invocation = invocation_factory()
            result = run_process(args=invocation.args, shell=invocation.use_shell, cwd=cwd, timeout=timeout,
                                 control=control, output_limit=self.log_limit, on_output=output,
                                 on_cleanup_failed=cleanup_error)
            snapshot = self.store.snapshot(task_id)
            summary = _summarize(str(snapshot["stdout_tail"]), str(snapshot["stderr_tail"]))[:1000]
            self.store.write_summary(task_id, summary)
            structured = None
            parse_status = "disabled" if not parse else "skipped_truncated" if result.get("output_truncated") else "skipped_size_limit"
            if parse and not result.get("output_truncated"):
                # Only bounded output is eligible for best-effort JSON parsing.
                small = self.store.snapshot(task_id, cursor="start")
                if not small["has_more"]:
                    structured = _extract_structured_output(small["stdout"])
                    if structured is None:
                        structured = _extract_structured_output(small["stderr"])
                    parse_status = "parsed" if structured is not None else "not_parsed"
            self._persist(task_id, **result, timed_out=result["execution_timed_out"], structured_output=structured,
                          structured_output_status=parse_status, schema_validation="not_performed")
        except Exception as exc:
            logging.exception("Task %s failed", task_id)
            result = {"status": "failed", "error": str(exc)[:1000]}
            try:
                self._persist(task_id, **result)
            except Exception:
                with self._lock:
                    self._failures[task_id] = {"task_id": task_id, **result, "persistence_error": str(exc)[:1000]}
        finally:
            with self._lock:
                event = self._completion_events.pop(task_id, None)
                if event:
                    event.set()
                self._controls.pop(task_id, None)
            self.slots.release()
            self._notify()

    def _build_invocation(
        self,
        *,
        executor_name: str,
        command: str,
        task: str | None,
        goal: str | None,
        cwd: Path,
        context_files: list[str],
        acceptance_criteria: list[str],
        verification_commands: list[str],
        commit_mode: str,
    ) -> Invocation:
        prompt = self._build_prompt(
            task=task,
            goal=goal,
            context_files=context_files,
            acceptance_criteria=acceptance_criteria,
            verification_commands=verification_commands,
            commit_mode=commit_mode,
        )
        if executor_name == "codex":
            parts = _resolve_delegate_command_parts(command)
            if _binary_name(parts[0]) == "codex":
                args = [
                    *parts,
                    "exec",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "-C",
                    str(cwd),
                ]
                if not (cwd / ".git").exists():
                    args.append("--skip-git-repo-check")
                args.append(prompt)
                return Invocation(args=args, use_shell=False)
        if executor_name == "claude-code":
            parts = _resolve_delegate_command_parts(command)
            if _binary_name(parts[0]) == "claude":
                return Invocation(
                    args=[
                        *parts,
                        "--print",
                        "--dangerously-skip-permissions",
                        "--permission-mode",
                        "bypassPermissions",
                        "--output-format",
                        "text",
                        prompt,
                    ],
                    use_shell=False,
                )
        return Invocation(args=command, use_shell=True)

    def _build_prompt(
        self,
        *,
        task: str | None,
        goal: str | None,
        context_files: list[str],
        acceptance_criteria: list[str],
        verification_commands: list[str],
        commit_mode: str,
    ) -> str:
        lines: list[str] = []
        if goal:
            lines.extend(["Goal:", goal, ""])
        if task:
            lines.extend(["Task:", task, ""])
        if acceptance_criteria:
            lines.append("Acceptance criteria:")
            lines.extend(f"- {item}" for item in acceptance_criteria)
            lines.append("")
        if verification_commands:
            lines.append("Verification commands:")
            lines.extend(f"- {item}" for item in verification_commands)
            lines.append("")
        lines.append(f"Commit mode: {commit_mode}")
        if context_files:
            lines.extend(["", "Context files:"])
            lines.extend(f"- {path}" for path in context_files)
        return "\n".join(lines)
