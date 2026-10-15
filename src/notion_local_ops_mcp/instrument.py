"""Generic tool-call instrumentation.

This module is deliberately free of any relay/HTTP references: it provides a
small observer-pattern substrate so any tool handler wrapped in :func:`traced`
will record timing, build a conservative argument/result summary, capture
exceptions, and fan the resulting :class:`ToolEvent` out to every registered
:class:`ToolSink`. Sinks decide for themselves what to do with events (log
them, POST them somewhere, emit metrics, ...); one bad sink can never break a
tool call because :func:`notify_sinks` swallows sink exceptions.

Design notes:

- ``@traced`` is transparent to FastMCP. It uses ``functools.wraps`` so the
  decorated function keeps its original name, signature, and return value. The
  intended decorator order is ``@mcp.tool(...)`` on the outside and
  ``@traced(...)`` directly above the ``def`` (innermost), so ``tool.fn``
  FastMCP exposes is the traced wrapper and calling it exercises tracing.
- The default ``args_fn``/``result_fn`` are conservative: they never emit full
  file/patch/command content. Huge params (``content``, ``patch``, ``command``,
  ``message`` ...) are replaced with ``{bytes: N}`` (for str) or ``{len: N}``
  (for list/dict). Read-like dict results are summarized as
  ``{lines: N, bytes: N, truncated: bool}`` only.
- On handler exception the event is built with ``ok=False`` and the original
  exception is re-raised unchanged (never swallowed).
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Protocol, runtime_checkable

# Parameter names that are known to be large (file contents, patches, shell
# commands, commit messages, prompts). The default args summary replaces their
# values with a size pointer instead of copying the payload into the trace.
_HUGE_PARAM_NAMES: frozenset[str] = frozenset(
    {
        "content",
        "patch",
        "command",
        "message",
        "task",
        "goal",
        "prompt",
        "old_text",
        "new_text",
        "acceptance_criteria",
        "verification_commands",
        "context_files",
        "output_schema",
    }
)

# Result dict keys that are themselves large payloads (file bodies, diffs, full
# stdout/stderr, blame lines). The default read-style result summary drops them
# and only reports size metadata.
_HUGE_RESULT_KEYS: frozenset[str] = frozenset(
    {
        "content",
        "diff",
        "stdout",
        "stderr",
        "entries",
        "results",
        "matches",
        "file_diffs",
        "files",
        "staged",
        "unstaged",
        "untracked",
        "body",
    }
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _utf8_bytes(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    try:
        return len(str(value).encode("utf-8"))
    except Exception:
        return 0


def _summarize_arg_value(name: str, value: object) -> object:
    """Reduce a single argument value to a small, safe-to-trace representation."""
    if value is None:
        return None
    if name in _HUGE_PARAM_NAMES:
        if isinstance(value, str):
            return {"bytes": _utf8_bytes(value)}
        if isinstance(value, (list, tuple)):
            return {"len": len(value)}
        if isinstance(value, dict):
            return {"len": len(value)}
    if isinstance(value, str):
        return value if len(value) <= 200 else f"{value[:200]}…({len(value)} chars)"
    if isinstance(value, (list, tuple, dict, set, frozenset)):
        return f"<{type(value).__name__} len={len(value)}>"
    if isinstance(value, (bool, int, float)):
        return value
    # Paths, enums, custom objects: stringify defensively.
    return repr(value)[:200]


def default_args_fn(kwargs: dict[str, Any]) -> dict[str, object]:
    """Default argument summarizer: drop huge params, keep small scalars."""
    summary: dict[str, object] = {}
    for name, value in kwargs.items():
        if name.startswith("_"):
            continue
        try:
            summary[name] = _summarize_arg_value(name, value)
        except Exception:
            summary[name] = "<unrepresentable>"
    return summary


def _iter_lines(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return value.count("\n") + (1 if value and not value.endswith("\n") else 0)
    return 0


def default_result_fn(result: object) -> dict[str, object] | None:
    """Default result summarizer.

    For dict results (the universal tool return shape in this server) we report
    small scalar outcome fields plus size metadata for large fields, and never
    the full content. Non-dict results are summarized as a tiny type/size hint.
    """
    if result is None:
        return None
    if not isinstance(result, dict):
        return {"type": type(result).__name__, "bytes": _utf8_bytes(result)}

    summary: dict[str, object] = {}
    # First, surface success + error code (cheap, high-signal).
    if "success" in result:
        summary["success"] = result["success"]
    err = result.get("error")
    if isinstance(err, dict) and "code" in err:
        summary["error_code"] = err["code"]

    for key, value in result.items():
        if key in {"success", "error"}:
            continue
        if key in _HUGE_RESULT_KEYS:
            if isinstance(value, str):
                summary[key + "_bytes"] = _utf8_bytes(value)
                summary[key + "_lines"] = _iter_lines(value)
            elif isinstance(value, (list, tuple, dict)):
                summary[key + "_len"] = len(value)
            # Whether the underlying read was truncated.
            if isinstance(result.get("truncated"), bool):
                summary.setdefault("truncated", result["truncated"])
            continue
        if isinstance(value, (bool, int, float)):
            summary[key] = value
        elif isinstance(value, str):
            summary[key] = value if len(value) <= 120 else f"{value[:120]}…"
        elif isinstance(value, (list, tuple, dict, set, frozenset)):
            summary[key] = f"<{type(value).__name__} len={len(value)}>"
        elif value is None:
            summary[key] = None
        else:
            summary[key] = repr(value)[:120]

    # If no explicit truncated flag was set, infer from a top-level content-like
    # field so read summaries always carry a truncated boolean per the spec.
    if "truncated" not in summary:
        summary["truncated"] = bool(result.get("truncated", False))

    return summary


def default_title_fn(tool_name: str, kwargs: dict[str, Any]) -> str:
    """Build a short human-readable title like ``apply_patch → path``.

    Picks the most identifying argument available for the tool (path-ish first,
    then command, then patch, then ref, then task_id), falling back to the
    tool name plus the first non-huge kwarg.
    """
    priority = ("path", "ref", "command", "patch", "task_id", "task", "goal", "message")
    for name in priority:
        if name in kwargs and kwargs[name] is not None:
            value = kwargs[name]
            if isinstance(value, str):
                # Use basename for path-like strings to keep the title short.
                display = value.rsplit("/", 1)[-1] if "/" in value else value
                if len(display) > 80:
                    display = display[:77] + "…"
                return f"{tool_name} → {display}"
    # Fall back to first scalar kwarg.
    for name, value in kwargs.items():
        if name.startswith("_"):
            continue
        if isinstance(value, (bool, int, float)):
            return f"{tool_name}({name}={value})"
        if isinstance(value, str) and len(value) <= 40:
            return f"{tool_name}({name}={value!r})"
    return tool_name


@dataclass
class ToolEvent:
    """A single traced tool invocation, ready to be fanned out to sinks."""

    tool: str
    title: str
    args_summary: dict[str, object]
    result_summary: dict[str, object] | None
    started_at: str
    duration_ms: int
    ok: bool
    error: str | None = None
    # Free-form extra metadata a sink may want (kept empty by default).
    extra: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "tool": self.tool,
            "title": self.title,
            "args_summary": self.args_summary,
            "result_summary": self.result_summary,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "ok": self.ok,
            "error": self.error,
            "extra": self.extra,
        }


@runtime_checkable
class ToolSink(Protocol):
    """Observer protocol for tool events.

    Implementations MUST never raise from ``on_tool_event``; :func:`notify_sinks`
    guards against it anyway, but a sink that misbehaves repeatedly only adds
    noise to logs. Sinks are synchronous; if a sink needs to do slow work
    (e.g. an HTTP POST) it should bound that work with a short timeout itself.
    """

    def on_tool_event(self, event: ToolEvent) -> None: ...


_sinks: list[ToolSink] = []
_sinks_lock = None  # lazily initialized to avoid importing threading at module top


def _sink_lock():
    global _sinks_lock
    if _sinks_lock is None:
        import threading

        _sinks_lock = threading.RLock()
    return _sinks_lock


def register_sink(sink: ToolSink) -> None:
    """Register a sink to receive future tool events."""
    with _sink_lock():
        if sink not in _sinks:
            _sinks.append(sink)


def clear_sinks() -> None:
    """Remove every registered sink (mainly for tests)."""
    with _sink_lock():
        _sinks.clear()


def notify_sinks(event: ToolEvent) -> None:
    """Fan an event out to every registered sink, swallowing any sink error.

    A single misbehaving sink must never propagate a failure into the tool call
    that triggered the trace, so every ``on_tool_event`` call is wrapped.
    """
    with _sink_lock():
        sinks = list(_sinks)
    for sink in sinks:
        try:
            sink.on_tool_event(event)
        except Exception:
            # Intentionally swallowed: instrumentation must be best-effort.
            pass


def traced(
    tool_name: str,
    *,
    title_fn: Callable[[str, dict[str, Any]], str] | None = None,
    args_fn: Callable[[dict[str, Any]], dict[str, object]] | None = None,
    result_fn: Callable[[object], dict[str, object] | None] | None = None,
):
    """Decorate a tool handler so its execution is traced and fanned out to sinks.

    The wrapper preserves the handler's signature (via ``functools.wraps``) and
    return value, and re-raises any exception the handler raises after building
    a failure event. When no sinks are registered the only overhead is the
    timing/summary construction (cheap), so this is safe to apply to every
    working tool unconditionally.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        import asyncio as _asyncio
        import time as _time

        if _asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                started_at = _now_iso()
                start = _time.perf_counter()
                kw_summary = dict(kwargs)
                title = (title_fn or default_title_fn)(tool_name, kw_summary)
                arg_summary = (args_fn or default_args_fn)(kw_summary)

                ok = False
                error: str | None = None
                result: object = None
                try:
                    result = await fn(*args, **kwargs)
                    ok = True
                    return result
                except Exception as exc:
                    error = str(exc)[:500]
                    raise
                finally:
                    duration_ms = int((_time.perf_counter() - start) * 1000)
                    result_summary = None
                    if ok:
                        try:
                            result_summary = (result_fn or default_result_fn)(result)
                        except Exception:
                            result_summary = None
                    event = ToolEvent(
                        tool=tool_name,
                        title=title,
                        args_summary=arg_summary,
                        result_summary=result_summary,
                        started_at=started_at,
                        duration_ms=duration_ms,
                        ok=ok,
                        error=error,
                    )
                    notify_sinks(event)

            return async_wrapper

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started_at = _now_iso()
            start = _time.perf_counter()
            # Bound kwargs for the summary (positional args are not summarized by
            # default; this server's tools are all kwarg-driven at the MCP layer).
            kw_summary = dict(kwargs)
            title = (title_fn or default_title_fn)(tool_name, kw_summary)
            arg_summary = (args_fn or default_args_fn)(kw_summary)

            ok = False
            error: str | None = None
            result: object = None
            try:
                result = fn(*args, **kwargs)
                ok = True
                return result
            except Exception as exc:
                error = str(exc)[:500]
                raise
            finally:
                duration_ms = int((_time.perf_counter() - start) * 1000)
                result_summary = None
                if ok:
                    try:
                        result_summary = (result_fn or default_result_fn)(result)
                    except Exception:
                        result_summary = None
                event = ToolEvent(
                    tool=tool_name,
                    title=title,
                    args_summary=arg_summary,
                    result_summary=result_summary,
                    started_at=started_at,
                    duration_ms=duration_ms,
                    ok=ok,
                    error=error,
                )
                notify_sinks(event)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Per-tool summary helpers
#
# These give particular tools slightly richer, tool-specific summaries than the
# generic defaults while staying small and never including full content. They
# are referenced from server.py when decorating the relevant tools.
# ---------------------------------------------------------------------------


def _read_result_summary(result: object) -> dict[str, object] | None:
    """Read-like tools (read_text/list_files/search/git_*): sizes only, no content."""
    if not isinstance(result, dict):
        return default_result_fn(result)
    summary: dict[str, object] = {}
    if "success" in result:
        summary["success"] = result["success"]
    err = result.get("error")
    if isinstance(err, dict) and "code" in err:
        summary["error_code"] = err["code"]
        return summary
    # Aggregate the most useful size fields across the read tool family.
    for key in ("content", "diff", "stdout", "stderr"):
        value = result.get(key)
        if isinstance(value, str):
            summary[key + "_bytes"] = _utf8_bytes(value)
            summary[key + "_lines"] = _iter_lines(value)
    for key in ("entries", "results", "matches", "file_diffs", "files", "counts"):
        value = result.get(key)
        if isinstance(value, (list, tuple, dict)):
            summary[key + "_len"] = len(value)
    summary["truncated"] = bool(result.get("truncated", False))
    # Surface a couple of cheap, high-signal scalars where present.
    for key in ("clean", "branch", "commit", "short_commit", "ref", "mode", "output_mode"):
        value = result.get(key)
        if isinstance(value, (bool, str, int)):
            summary[key] = value
    return summary


def _patch_args_summary(kwargs: dict[str, Any]) -> dict[str, object]:
    summary = default_args_fn(kwargs)
    patch = kwargs.get("patch")
    if isinstance(patch, str):
        hunk_count = patch.count("\n@@") + (
            1 if patch.lstrip().startswith("@@") else 0
        )
        # Count "*** Update File:" / "*** Add File:" / "*** Delete File:" headers.
        file_markers = sum(
            patch.count(marker)
            for marker in (
                "*** Update File: ",
                "*** Add File: ",
                "*** Delete File: ",
            )
        )
        summary["patch"] = {"bytes": _utf8_bytes(patch), "hunks": hunk_count, "files": file_markers}
    return summary


def _patch_result_summary(result: object) -> dict[str, object] | None:
    if not isinstance(result, dict):
        return default_result_fn(result)
    summary: dict[str, object] = {"success": result.get("success")}
    err = result.get("error")
    if isinstance(err, dict) and "code" in err:
        summary["error_code"] = err["code"]
        return summary
    files = result.get("files")
    if isinstance(files, list):
        summary["files_len"] = len(files)
        summary["applied"] = bool(result.get("applied"))
        summary["validated"] = bool(result.get("validated"))
        # Aggregate line counts across per-file summaries.
        added = 0
        removed = 0
        for entry in files:
            if isinstance(entry, dict):
                added += int(entry.get("lines_added") or 0)
                removed += int(entry.get("lines_removed") or 0)
        summary["lines_added"] = added
        summary["lines_removed"] = removed
        warnings = result.get("warnings")
        if isinstance(warnings, list):
            summary["warnings_len"] = len(warnings)
    return summary


def _write_file_result_summary(result: object) -> dict[str, object] | None:
    if not isinstance(result, dict):
        return default_result_fn(result)
    summary: dict[str, object] = {"success": result.get("success")}
    err = result.get("error")
    if isinstance(err, dict) and "code" in err:
        summary["error_code"] = err["code"]
        return summary
    for key in ("bytes_written", "dry_run", "written"):
        if key in result:
            summary[key] = result[key]
    return summary


def _git_commit_result_summary(result: object) -> dict[str, object] | None:
    if not isinstance(result, dict):
        return default_result_fn(result)
    summary: dict[str, object] = {"success": result.get("success")}
    err = result.get("error")
    if isinstance(err, dict) and "code" in err:
        summary["error_code"] = err["code"]
        return summary
    for key in ("commit", "short_commit", "branch", "amended", "allow_empty", "dry_run"):
        if key in result:
            summary[key] = result[key]
    files = result.get("files")
    if isinstance(files, list):
        summary["files_len"] = len(files)
    return summary


def _run_command_result_summary(result: object) -> dict[str, object] | None:
    if not isinstance(result, dict):
        return default_result_fn(result)
    summary: dict[str, object] = {"success": result.get("success")}
    err = result.get("error")
    if isinstance(err, dict) and "code" in err:
        summary["error_code"] = err["code"]
    for key in ("exit_code", "timed_out", "cwd"):
        if key in result:
            summary[key] = result[key]
    stdout = result.get("stdout")
    stderr = result.get("stderr")
    if isinstance(stdout, str):
        summary["stdout_bytes"] = _utf8_bytes(stdout)
        summary["stdout_tail"] = stdout[-500:]
    if isinstance(stderr, str):
        summary["stderr_bytes"] = _utf8_bytes(stderr)
        summary["stderr_tail"] = stderr[-500:]
    return summary


def _task_result_summary(result: object) -> dict[str, object] | None:
    """delegate_task / run_command_stream / get_task / wait_task / cancel_task."""
    if not isinstance(result, dict):
        return default_result_fn(result)
    summary: dict[str, object] = {"success": result.get("success", True)}
    err = result.get("error")
    if isinstance(err, dict) and "code" in err:
        summary["error_code"] = err["code"]
    for key in ("task_id", "executor", "status", "completed", "cancelled", "timed_out",
                "wait_timed_out", "execution_timed_out", "output_truncated", "cleanup_failed"):
        if key in result:
            summary[key] = result[key]
    stdout_tail = result.get("stdout_tail")
    if isinstance(stdout_tail, str):
        summary["stdout_tail"] = stdout_tail[-500:]
    stderr_tail = result.get("stderr_tail")
    if isinstance(stderr_tail, str):
        summary["stderr_tail"] = stderr_tail[-500:]
    return summary


def _purge_result_summary(result: object) -> dict[str, object] | None:
    if not isinstance(result, dict):
        return default_result_fn(result)
    summary: dict[str, object] = {"success": result.get("success")}
    for key in ("scanned", "purged", "dry_run"):
        if key in result:
            summary[key] = result[key]
    return summary


def _server_info_result_summary(result: object) -> dict[str, object] | None:
    if not isinstance(result, dict):
        return default_result_fn(result)
    # server_info is itself small metadata; surface a few identifying fields.
    summary: dict[str, object] = {}
    for key in ("app_name", "host", "port", "auth", "tool_count"):
        if key in result:
            summary[key] = result[key]
    return summary


def _identity_args_summary(kwargs: dict[str, Any]) -> dict[str, object]:
    """For tools whose args are all small scalars (e.g. set/get_default_cwd)."""
    return default_args_fn(kwargs)


def event_to_json(event: ToolEvent) -> str:
    """Serialize an event to JSON (used by sinks that want a stable form)."""
    return json.dumps(event.to_dict(), default=str, ensure_ascii=False)
