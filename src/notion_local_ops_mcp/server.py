from __future__ import annotations

import argparse
import os
import re

from fastmcp import FastMCP
import uvicorn

from .http_compat import build_http_compat_app
from . import config

from .config import (
    APP_NAME,
    AUTH_MODE,
    AUTH_TOKEN,
    CLAUDE_COMMAND,
    CODEX_COMMAND,
    COMMAND_TIMEOUT,
    DEBUG_MCP_LOGGING,
    DELEGATE_TIMEOUT,
    GRACEFUL_SHUTDOWN_SECONDS,
    HOST,
    OAUTH_LOGIN_TOKEN,
    OAUTH_SCOPES,
    OAUTH_TOKEN_TTL_SECONDS,
    PORT,
    PUBLIC_BASE_URL,
    RELAY_BRIDGE_ENABLED,
    RELAY_BINDING_TTL_SECONDS,
    RELAY_BRIDGE_TIMEOUT,
    RELAY_URL,
    STATE_DIR,
    WORKSPACE_ROOT,
    ensure_runtime_directories,
)
from .executors import ExecutorRegistry
from .files import list_files as list_files_impl
from .files import read_file as read_file_impl
from .files import read_files as read_files_impl
from .files import write_file as write_file_impl
from .gitops import git_blame as git_blame_impl
from .gitops import git_commit as git_commit_impl
from .gitops import git_diff as git_diff_impl
from .gitops import git_log as git_log_impl
from .gitops import git_show as git_show_impl
from .gitops import git_status as git_status_impl
from .instrument import (
    _git_commit_result_summary,
    _patch_args_summary,
    _patch_result_summary,
    _purge_result_summary,
    _read_result_summary,
    _run_command_result_summary,
    _server_info_result_summary,
    _task_result_summary,
    _write_file_result_summary,
    register_sink,
    traced,
)
from .oauth import OAuthRuntimeConfig
from .patching import apply_patch as apply_patch_impl
from . import relay_bridge
from .relay_bridge import RelayBridgeSink
from . import session
from .pathing import resolve_cwd, resolve_file_cwd, resolve_path
from .search import glob_files as glob_files_impl
from .search import grep_files as grep_files_impl
from .shell import run_command as run_command_impl
from .skills import list_skills as list_skills_impl
from .tasks import TaskStore


# Bearer auth lives exclusively in the HTTP layer (http_compat.HTTPBearerAuthMiddleware)
# so unauthenticated clients can't even open an SSE session. The FastMCP
# protocol-layer middleware was redundant and has been removed.

store = TaskStore(STATE_DIR)
registry = ExecutorRegistry(
    store=store,
    codex_command=CODEX_COMMAND,
    claude_command=CLAUDE_COMMAND,
)

MCP_INSTRUCTIONS = (
    "Pass explicit cwd for every project operation; the legacy default cwd is process-wide. "
    "Use direct tools first for normal tasks. Prioritize apply_patch/write_file for edits and "
    "run_command_stream/wait_task for long-running shell work. "
    "Use search/read_text for focused repo discovery and reading, not as a substitute for every shell step. "
    "Use git_* only when the current cwd is actually inside a git repository. "
    "Before edits, inspect the project baseline; afterward re-read changed files, inspect the diff, and run verification. "
    "Follow next_cursor and deferred reads instead of treating partial content as complete; on file_changed, re-read. "
    "On wait_timed_out, continue querying the same task_id, never resubmit the command. "
    "If a submission response is lost, its outcome is unknown: do not blindly retry side-effecting commands. "
    "On capacity_exceeded, retry later without busy polling. On partial write failure, re-read before repair. "
    "On cleanup_failed or control_unavailable, do not assume the process stopped. "
    "Read remaining logs while has_more is true even after completed; disclose output truncation. "
    "Report completion only when the requested change and its verification both succeed. "
    "Use delegate_task only when direct tools are insufficient for a complex, long-running, or multi-file task."
)

mcp = FastMCP(
    APP_NAME,
    instructions=MCP_INSTRUCTIONS,
)


def _current_auth_token() -> str:
    # Resolved via module globals so tests that monkeypatch ``AUTH_TOKEN`` on
    # this module (and runtime overrides) are honored per-request.
    return globals().get("AUTH_TOKEN", "") or ""


def _current_oauth_config() -> OAuthRuntimeConfig:
    return OAuthRuntimeConfig(
        auth_mode=globals().get("AUTH_MODE", "") or "",
        auth_token=_current_auth_token(),
        public_base_url=globals().get("PUBLIC_BASE_URL", "") or "",
        state_dir=globals().get("STATE_DIR", STATE_DIR),
        oauth_login_token=globals().get("OAUTH_LOGIN_TOKEN", "") or "",
        oauth_scopes=tuple(globals().get("OAUTH_SCOPES", ("local-ops",)) or ("local-ops",)),
        oauth_token_ttl_seconds=int(globals().get("OAUTH_TOKEN_TTL_SECONDS", 86400) or 86400),
    )


def _current_debug_mcp_logging() -> bool:
    return bool(globals().get("DEBUG_MCP_LOGGING", False))


# Register the relay bridge sink at import time. The sink is a no-op unless a
# relay run is bound (session.set_bound_run), so registering it unconditionally
# (when enabled) does not change behavior for non-bridged clients. Re-registration
# is guarded inside register_sink, so repeated imports of this module are safe.
if RELAY_BRIDGE_ENABLED:
    register_sink(RelayBridgeSink())


READ_ONLY_TOOL = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

LOCAL_STATE_TOOL = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

LOCAL_WRITE_TOOL = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": False,
}

OPEN_WORLD_WRITE_TOOL = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}


@mcp.tool(
    name="list_skills",
    title="List Skills",
    annotations=READ_ONLY_TOOL,
    description=(
        "List project and global agent skills as lightweight summaries. "
        "Pass cwd for project discovery; omitted cwd uses WORKSPACE_ROOT, not shared default cwd. "
        "Returns skill name, description, preferred path, and source locations. "
        "Use namespace ('agents' | 'codex' | 'claude') to scope, name_pattern "
        "(fnmatch, e.g. 'git-*') to filter by skill name, and "
        "description_max_length to cap long descriptions for index-style scans."
    ),
)
@traced("list_skills", result_fn=_read_result_summary)
def list_skills(
    include_project: bool = True,
    include_global: bool = True,
    namespace: str | None = None,
    name_pattern: str | None = None,
    description_max_length: int | None = None,
    cwd: str | None = None,
) -> dict[str, object]:
    base = resolve_file_cwd(cwd, WORKSPACE_ROOT)
    result = list_skills_impl(
        workspace_root=base,
        include_project=include_project,
        include_global=include_global,
        namespace=namespace,
        name_pattern=name_pattern,
        description_max_length=description_max_length,
    )
    return {**result, "resolved_cwd": str(base)}


@mcp.tool(
    name="list_files",
    title="List Files",
    annotations=READ_ONLY_TOOL,
    description=(
        "List files and directories. Hidden entries, common junk dirs "
        "(.git / .venv / node_modules / __pycache__ / ...) and .gitignore'd "
        "paths are excluded by default. Set include_hidden=True or "
        "respect_gitignore=False to see them; add exclude_patterns for "
        "fnmatch-style patterns (matched against both name and relative path). "
        "Pass cwd for relative paths; omitted cwd preserves WORKSPACE_ROOT."
    ),
)
@traced("list_files", result_fn=_read_result_summary)
def list_files(
    path: str | None = None,
    recursive: bool = False,
    limit: int = 200,
    offset: int = 0,
    include_hidden: bool = False,
    respect_gitignore: bool = True,
    exclude_patterns: list[str] | None = None,
    cwd: str | None = None,
) -> dict[str, object]:
    if limit <= 0 or offset < 0:
        return {"success": False, "error": {"code": "invalid_arguments", "message": "limit must be positive; offset must be non-negative."}}
    base = resolve_file_cwd(cwd, WORKSPACE_ROOT)
    target = resolve_path(path or ".", base)
    result = list_files_impl(
        target,
        recursive=recursive,
        limit=limit,
        offset=offset,
        include_hidden=include_hidden,
        respect_gitignore=respect_gitignore,
        exclude_patterns=exclude_patterns,
    )
    return {**result, "resolved_cwd": str(base)}


@mcp.tool(
    name="search",
    title="Search Workspace",
    annotations=READ_ONLY_TOOL,
    description=(
        "Canonical search tool that unifies glob, regex grep, and plain-text search. "
        "Pass cwd for relative paths; omitted cwd preserves WORKSPACE_ROOT. "
        "Use mode='glob' for path discovery, mode='regex' for code/text regex, and "
        "mode='text' for literal substring search. Hidden entries and .gitignore'd "
        "paths are excluded by default; regex/text search also accept a single file path."
    ),
)
@traced("search", result_fn=_read_result_summary)
def search(
    mode: str = "regex",
    path: str | None = None,
    pattern: str | None = None,
    query: str | None = None,
    glob: str | None = None,
    output_mode: str = "content",
    before: int = 0,
    after: int = 0,
    ignore_case: bool = False,
    limit: int = 200,
    offset: int = 0,
    multiline: bool = False,
    include_hidden: bool = False,
    respect_gitignore: bool = True,
    exclude_patterns: list[str] | None = None,
    cwd: str | None = None,
) -> dict[str, object]:
    if limit <= 0 or offset < 0 or before < 0 or after < 0:
        return {"success": False, "error": {"code": "invalid_arguments", "message": "limit must be positive; offset/before/after must be non-negative."}}
    base = resolve_file_cwd(cwd, WORKSPACE_ROOT)
    target = resolve_path(path or ".", base)

    if mode == "glob":
        if not pattern:
            return {
                "success": False,
                "error": {"code": "missing_pattern", "message": "mode=glob requires `pattern`."},
            }
        result = glob_files_impl(
            target,
            pattern=pattern,
            limit=limit,
            offset=offset,
            include_hidden=include_hidden,
            respect_gitignore=respect_gitignore,
            exclude_patterns=exclude_patterns,
        )
        if isinstance(result, dict):
            result["mode"] = mode
            result["resolved_cwd"] = str(base)
        return result

    if mode in {"regex", "text"}:
        effective_pattern = pattern
        if mode == "text":
            if query is None:
                return {
                    "success": False,
                    "error": {
                        "code": "missing_query",
                        "message": "mode=text requires `query`.",
                    },
                }
            effective_pattern = re.escape(query)
        elif effective_pattern is None:
            return {
                "success": False,
                "error": {"code": "missing_pattern", "message": "mode=regex requires `pattern`."},
            }

        result = grep_files_impl(
            target,
            pattern=effective_pattern,
            glob_pattern=glob,
            output_mode=output_mode,
            before=before,
            after=after,
            ignore_case=ignore_case,
            head_limit=limit,
            offset=offset,
            multiline=multiline,
            include_hidden=include_hidden,
            respect_gitignore=respect_gitignore,
            exclude_patterns=exclude_patterns,
        )
        if isinstance(result, dict):
            result["mode"] = mode
            result["resolved_cwd"] = str(base)
            if mode == "text" and query is not None:
                result["query"] = query
        return result

    return {
        "success": False,
        "error": {
            "code": "invalid_mode",
            "message": "mode must be one of: glob, regex, text.",
        },
    }


@mcp.tool(
    name="read_text",
    title="Read Text",
    annotations=READ_ONLY_TOOL,
    description=(
        "Canonical text-reader tool. Pass either `path` for single-file reads or "
        "`paths` for batch reads. Pagination is line-based via start_line/line_limit. "
        "Prefer returned next_cursor to continue, especially for partial long lines; do not combine cursor and start_line. "
        "Batch deferred entries must be read separately. Pass cwd for project-relative paths. "
        "Set include_line_numbers=true for evidence. Invalid UTF-8 is rejected."
    ),
)
@traced("read_text", result_fn=_read_result_summary)
def read_text(
    path: str | None = None,
    paths: list[str] | None = None,
    start_line: int | None = None,
    line_limit: int | None = None,
    include_line_numbers: bool = False,
    cwd: str | None = None,
    cursor: str | None = None,
) -> dict[str, object]:
    base = resolve_file_cwd(cwd, WORKSPACE_ROOT)
    has_path = bool(path)
    has_paths = bool(paths)
    if has_path == has_paths:
        return {
            "success": False,
            "error": {
                "code": "invalid_arguments",
                "message": "Provide exactly one of `path` or `paths`.",
            },
        }

    effective_offset = start_line
    effective_limit = line_limit
    if path:
        target = resolve_path(path, base)
        result = read_file_impl(
            target,
            offset=effective_offset,
            limit=effective_limit,
            max_lines=200,
            max_bytes=config.READ_MAX_BYTES,
            include_line_numbers=include_line_numbers,
            cursor=cursor,
        )
        if isinstance(result, dict):
            result["mode"] = "single"
            result["resolved_cwd"] = str(base)
        return result

    if cursor is not None:
        return {"success": False, "error": {"code": "invalid_arguments", "message": "Use cursor with a single path."}}
    targets = [resolve_path(item, base) for item in (paths or [])]
    result = read_files_impl(
        targets,
        offset=effective_offset,
        limit=effective_limit,
        max_lines=200,
        max_bytes=config.READ_MAX_BYTES,
        include_line_numbers=include_line_numbers,
    )
    if isinstance(result, dict):
        result["mode"] = "batch"
        result["resolved_cwd"] = str(base)
    return result


@mcp.tool(
    name="write_file",
    title="Write File",
    annotations=LOCAL_WRITE_TOOL,
    description="Write full content to a file (supports dry_run). Pass cwd for project-relative paths; omitted cwd preserves WORKSPACE_ROOT. On write failure reread the target because partial writes are possible.",
)
@traced("write_file", result_fn=_write_file_result_summary)
def write_file(path: str, content: str, dry_run: bool = False, cwd: str | None = None) -> dict[str, object]:
    base = resolve_file_cwd(cwd, WORKSPACE_ROOT)
    target = resolve_path(path, base)
    return {**write_file_impl(target, content=content, dry_run=dry_run), "resolved_cwd": str(base)}


@mcp.tool(
    name="apply_patch",
    title="Apply Patch",
    annotations=LOCAL_WRITE_TOOL,
    description=(
        "Apply a structured patch using *** Begin Patch / *** Update File blocks. "
        "Pass cwd for relative source and move-target paths. Multi-file writes are not atomic; inspect partial failure details. "
        "Each @@ hunk must contain at least one '+' or '-' line and must "
        "match exactly one location in the target file; pure-context hunks are rejected."
    ),
)
@traced(
    "apply_patch",
    args_fn=_patch_args_summary,
    result_fn=_patch_result_summary,
)
def apply_patch(
    patch: str,
    dry_run: bool = False,
    validate_only: bool = False,
    return_diff: bool = False,
    cwd: str | None = None,
) -> dict[str, object]:
    base = resolve_file_cwd(cwd, WORKSPACE_ROOT)
    result = apply_patch_impl(
        patch=patch,
        workspace_root=base,
        dry_run=dry_run,
        validate_only=validate_only,
        return_diff=return_diff,
    )
    return {**result, "resolved_cwd": str(base)}


@mcp.tool(
    name="server_info",
    title="Server Info",
    annotations=READ_ONLY_TOOL,
    description=(
        "Return server metadata: app name, host/port, workspace root, state dir, "
        "timeouts, auth mode, and the list of registered tools. Useful as a first "
        "call to confirm which bridge you are connected to and what it can do."
    ),
)
@traced("server_info", result_fn=_server_info_result_summary)
async def server_info() -> dict[str, object]:
    list_tools = getattr(mcp, "_list_tools")
    try:
        registered = await list_tools()
    except TypeError:
        # fastmcp 2.14 requires a context arg; None works for server-side listing.
        registered = await list_tools(None)
    tools = sorted(tool.name for tool in registered)
    session_cwd = session.get_default_cwd()
    binding = session.get_bound_run()
    return {
        "success": True,
        "app_name": APP_NAME,
        "host": HOST,
        "port": PORT,
        "workspace_root": str(WORKSPACE_ROOT),
        "session_cwd": str(session_cwd) if session_cwd else None,
        "state_dir": str(STATE_DIR),
        "command_timeout_seconds": COMMAND_TIMEOUT,
        "delegate_timeout_seconds": DELEGATE_TIMEOUT,
        "auth": _current_oauth_config().normalized_auth_mode,
        "debug_mcp_logging": bool(DEBUG_MCP_LOGGING),
        "codex_command": CODEX_COMMAND,
        "claude_command": CLAUDE_COMMAND,
        "relay_bridge": {
            "enabled": bool(RELAY_BRIDGE_ENABLED),
            "timeout_seconds": float(RELAY_BRIDGE_TIMEOUT),
            "binding_ttl_seconds": float(RELAY_BINDING_TTL_SECONDS),
            "default_relay_url": RELAY_URL,
            "bound": binding is not None,
            "request_id": binding.get("request_id") if binding else None,
            "conversation_key": binding.get("conversation_key") if binding else None,
            "relay_url": binding.get("relay_url") if binding else None,
            "bound_at": binding.get("bound_at") if binding else None,
            "dropped_traces": relay_bridge.get_dropped_traces(),
        },
        "tools": tools,
        "tool_count": len(tools),
        "limits": {name.lower(): getattr(config, name) for name in (
            "READ_MAX_LINES", "READ_MAX_BYTES", "READ_BATCH_FILES", "READ_BATCH_BYTES",
            "LOG_READ_BYTES", "LOG_RETAIN_BYTES", "COMMAND_OUTPUT_BYTES", "MAX_EXECUTIONS",
            "MAX_WAIT_SECONDS", "TERM_GRACE_SECONDS", "KILL_GRACE_SECONDS")},
        "process_groups_supported": os.name == "posix",
    }


@mcp.tool(
    name="set_default_cwd",
    title="Set Default CWD",
    annotations=LOCAL_STATE_TOOL,
    description=(
        "Set the PROCESS-WIDE shared default working directory used whenever a Shell/Git tool call "
        "omits `cwd`. Pass null (or omit path) to clear the override and fall back to "
        "the server's workspace root. Useful when running many commands in the same "
        "repo: set it once instead of passing `cwd` on every call."
    ),
)
@traced("set_default_cwd")
def set_default_cwd(path: str | None = None) -> dict[str, object]:
    if not path:
        session.set_default_cwd(None)
        return {
            "success": True,
            "session_cwd": None,
            "workspace_root": str(WORKSPACE_ROOT),
            "cleared": True,
        }
    target = resolve_path(path, WORKSPACE_ROOT)
    if not target.exists():
        return {
            "success": False,
            "error": {
                "code": "cwd_not_found",
                "message": f"Path does not exist: {target}",
            },
            "path": str(target),
        }
    if not target.is_dir():
        return {
            "success": False,
            "error": {
                "code": "cwd_not_directory",
                "message": f"Path is not a directory: {target}",
            },
            "path": str(target),
        }
    session.set_default_cwd(target)
    return {
        "success": True,
        "session_cwd": str(target),
        "workspace_root": str(WORKSPACE_ROOT),
        "cleared": False,
    }


@mcp.tool(
    name="get_default_cwd",
    title="Get Default CWD",
    annotations=READ_ONLY_TOOL,
    description=(
        "Return the PROCESS-WIDE shared default working directory and whether it comes "
        "from the session override (set_default_cwd) or from the server's workspace root."
    ),
)
@traced("get_default_cwd")
def get_default_cwd() -> dict[str, object]:
    session_cwd = session.get_default_cwd()
    effective = session_cwd if session_cwd is not None else WORKSPACE_ROOT
    return {
        "success": True,
        "session_cwd": str(session_cwd) if session_cwd else None,
        "workspace_root": str(WORKSPACE_ROOT),
        "effective_cwd": str(effective),
        "source": "session" if session_cwd else "workspace_root",
    }


@mcp.tool(
    name="git_status",
    title="Git Status",
    annotations=READ_ONLY_TOOL,
    description="Return structured git status for the repository at cwd or the current workspace root.",
)
@traced("git_status", result_fn=_read_result_summary)
def git_status(cwd: str | None = None) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return git_status_impl(cwd=resolved_cwd)


@mcp.tool(
    name="git_diff",
    title="Git Diff",
    annotations=READ_ONLY_TOOL,
    description=(
        "Return git diff output plus per-file diffs with added/removed counts. "
        "Each file is truncated independently to per_file_max_bytes so a single huge "
        "file does not hide changes in other files."
    ),
)
@traced("git_diff", result_fn=_read_result_summary)
def git_diff(
    cwd: str | None = None,
    staged: bool = False,
    paths: list[str] | None = None,
    max_bytes: int = 65536,
    per_file_max_bytes: int = 16384,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return git_diff_impl(
        cwd=resolved_cwd,
        staged=staged,
        paths=paths,
        max_bytes=max_bytes,
        per_file_max_bytes=per_file_max_bytes,
    )


@mcp.tool(
    name="git_commit",
    title="Git Commit",
    annotations=LOCAL_WRITE_TOOL,
    description=(
        "Create a git commit for staged changes, selected paths, or all current changes. "
        "Supports amend (rewrite HEAD), allow_empty (commit without changes), custom author, "
        "sign_off (append Signed-off-by trailer), and dry_run preview."
    ),
)
@traced("git_commit", result_fn=_git_commit_result_summary)
def git_commit(
    message: str,
    cwd: str | None = None,
    paths: list[str] | None = None,
    stage_all: bool = False,
    amend: bool = False,
    allow_empty: bool = False,
    author: str | None = None,
    sign_off: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return git_commit_impl(
        cwd=resolved_cwd,
        message=message,
        paths=paths,
        stage_all=stage_all,
        amend=amend,
        allow_empty=allow_empty,
        author=author,
        sign_off=sign_off,
        dry_run=dry_run,
    )


@mcp.tool(
    name="git_log",
    title="Git Log",
    annotations=READ_ONLY_TOOL,
    description="Return recent git commits for the repository at cwd.",
)
@traced("git_log", result_fn=_read_result_summary)
def git_log(cwd: str | None = None, limit: int = 10) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return git_log_impl(cwd=resolved_cwd, limit=limit)


@mcp.tool(
    name="git_show",
    title="Git Show",
    annotations=READ_ONLY_TOOL,
    description=(
        "Show metadata + per-file diff for a commit or any git ref (defaults to HEAD). "
        "Useful for inspecting a specific commit without shelling out."
    ),
)
@traced("git_show", result_fn=_read_result_summary)
def git_show(
    ref: str = "HEAD",
    cwd: str | None = None,
    max_bytes: int = 65536,
    per_file_max_bytes: int = 16384,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return git_show_impl(
        cwd=resolved_cwd,
        ref=ref,
        max_bytes=max_bytes,
        per_file_max_bytes=per_file_max_bytes,
    )


@mcp.tool(
    name="git_blame",
    title="Git Blame",
    annotations=READ_ONLY_TOOL,
    description=(
        "Return per-line blame info (commit, author, summary, content) for a file. "
        "Restrict to a line range via start_line / end_line."
    ),
)
@traced("git_blame", result_fn=_read_result_summary)
def git_blame(
    path: str,
    cwd: str | None = None,
    ref: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return git_blame_impl(
        cwd=resolved_cwd,
        path=path,
        ref=ref,
        start_line=start_line,
        end_line=end_line,
    )


@mcp.tool(
    name="run_command",
    title="Run Command",
    annotations=OPEN_WORLD_WRITE_TOOL,
    description="Run a local shell command now or queue it as a background task for wait_task/get_task polling.",
)
@traced("run_command", result_fn=_run_command_result_summary)
def run_command(
    command: str,
    cwd: str | None = None,
    timeout: int | None = None,
    run_in_background: bool = False,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    effective_timeout = timeout if timeout is not None else COMMAND_TIMEOUT
    if run_in_background:
        return registry.submit_command(
            command=command,
            cwd=resolved_cwd,
            timeout=effective_timeout,
        )
    return run_command_impl(
        command=command,
        cwd=resolved_cwd,
        timeout=effective_timeout,
    )


@mcp.tool(
    name="run_command_stream",
    title="Run Command Stream",
    annotations=OPEN_WORLD_WRITE_TOOL,
    description=(
        "Start a shell command in background and return task_id immediately for "
        "stream-like polling via get_task/wait_task."
    ),
)
@traced("run_command_stream", result_fn=_task_result_summary)
def run_command_stream(
    command: str,
    cwd: str | None = None,
    timeout: int | None = None,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    effective_timeout = timeout if timeout is not None else COMMAND_TIMEOUT
    queued = registry.submit_command(
        command=command,
        cwd=resolved_cwd,
        timeout=effective_timeout,
    )
    if "task_id" not in queued:
        return queued
    queued["stream_mode"] = "task-polling"
    queued["next"] = "call get_task(task_id) or wait_task(task_id)"
    return queued


@mcp.tool(
    name="delegate_task",
    title="Delegate Task",
    annotations=OPEN_WORLD_WRITE_TOOL,
    description=(
        "Fallback only. Use this when direct tools are insufficient for a complex, long-running, or "
        "multi-file task. Supported executors: auto, codex, claude-code. "
        "Optionally provide output_schema and parse_structured_output=true to "
        "capture JSON output as structured_output."
    ),
)
@traced("delegate_task", result_fn=_task_result_summary)
def delegate_task(
    task: str | None = None,
    goal: str | None = None,
    executor: str = "auto",
    cwd: str | None = None,
    context_files: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    verification_commands: list[str] | None = None,
    commit_mode: str = "allowed",
    timeout: int | None = None,
    output_schema: dict[str, object] | None = None,
    parse_structured_output: bool = True,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return registry.submit(
        task=task,
        goal=goal,
        executor=executor,
        cwd=resolved_cwd,
        timeout=timeout if timeout is not None else DELEGATE_TIMEOUT,
        context_files=[str(resolve_path(item, resolved_cwd)) for item in (context_files or [])],
        acceptance_criteria=acceptance_criteria,
        verification_commands=verification_commands,
        commit_mode=commit_mode,
        output_schema=output_schema,
        parse_structured_output=parse_structured_output,
    )


@mcp.tool(
    name="get_task",
    title="Get Task",
    annotations=READ_ONLY_TOOL,
    description="Get task status and bounded output. Omit cursor for legacy tails, use cursor='start' to read from the beginning, then next_cursor for incremental reads. completed and has_more are independent. output_truncated means excess log data was discarded.",
)
@traced("get_task", result_fn=_task_result_summary)
def get_task(task_id: str, cursor: str | None = None, max_bytes: int = config.LOG_READ_BYTES) -> dict[str, object]:
    return registry.get(task_id, cursor=cursor, max_bytes=max_bytes)


@mcp.tool(
    name="wait_task",
    title="Wait Task",
    annotations=READ_ONLY_TOOL,
    description="Wait up to 30 seconds (zero polls immediately) for task completion; return_on_output also wakes on new output. Pass cursor from get_task for incremental logs. wait_timed_out does not cancel execution; execution_timed_out records the command deadline separately.",
)
@traced("wait_task", result_fn=_task_result_summary)
def wait_task(task_id: str, timeout: float = 30, poll_interval: float = 0.5,
              cursor: str | None = None, max_bytes: int = config.LOG_READ_BYTES,
              return_on_output: bool = False) -> dict[str, object]:
    return registry.wait(task_id, timeout=timeout, poll_interval=poll_interval,
                         cursor=cursor, max_bytes=max_bytes, return_on_output=return_on_output)


@mcp.tool(
    name="cancel_task",
    title="Cancel Task",
    annotations=LOCAL_WRITE_TOOL,
    description="Request process-group cancellation and wait briefly for confirmed cleanup. cancelled=true only after confirmed termination; cancel_requested alone is not completion. Terminal tasks keep their result. Unowned tasks cannot be cancelled by this process.",
)
@traced("cancel_task", result_fn=_task_result_summary)
def cancel_task(task_id: str) -> dict[str, object]:
    return registry.cancel(task_id)


@mcp.tool(
    name="purge_tasks",
    title="Purge Tasks",
    annotations=LOCAL_WRITE_TOOL,
    description=(
        "Delete old terminal task metadata/log directories under STATE_DIR/tasks. Active, corrupt and linked tasks are skipped. "
        "Defaults to 7 days; supports dry_run preview."
    ),
)
@traced("purge_tasks", result_fn=_purge_result_summary)
def purge_tasks(older_than_hours: float = 24 * 7, dry_run: bool = False) -> dict[str, object]:
    return store.purge_tasks(
        older_than_seconds=float(older_than_hours) * 3600.0,
        dry_run=dry_run,
    )


@mcp.tool(
    name="bind_relay_run",
    title="Bind Relay Run",
    annotations=LOCAL_STATE_TOOL,
    description=(
        "Use this once after a relay trigger to mirror your tool calls to the local "
        "operator. Pass only the request_id and callback_token from the relay trigger "
        "(conversation_key too if available). The relay address is already configured "
        "locally, so you do not need to pass it. After this, every tool you call here "
        "is automatically reported to the relay so the operator can follow your work in "
        "real time. Pass null/empty for request_id (or call clear_relay_run) to unbind. "
        "No-op if you're not working with a relay. Binding is process-wide and "
        "single-valued; a new bind overwrites the previous one. The relay being "
        "unreachable never blocks tool execution."
    ),
)
def bind_relay_run(
    request_id: str | None = None,
    callback_token: str | None = None,
    relay_url: str | None = None,
    conversation_key: str | None = None,
) -> dict[str, object]:
    # Treat an empty/whitespace request_id as an explicit unbind so agents that
    # "pass null to unbind" work without needing a separate tool.
    if not request_id or not request_id.strip():
        session.clear_bound_run()
        return {
            "success": True,
            "bound": False,
            "request_id": None,
            "relay_url": None,
        }
    if not callback_token:
        return {
            "success": False,
            "error": {
                "code": "missing_callback_token",
                "message": "bind_relay_run requires a callback_token when request_id is set.",
            },
        }
    # relay_url is optional: fall back to the locally configured default so the
    # agent never has to know the relay host/port.
    resolved_relay_url = (relay_url.strip() if isinstance(relay_url, str) and relay_url.strip() else RELAY_URL)
    active = session.set_bound_run(
        {
            "request_id": request_id,
            "callback_token": callback_token,
            "relay_url": resolved_relay_url,
            "conversation_key": conversation_key,
        }
    )
    return {
        "success": True,
        "bound": True,
        "request_id": active.get("request_id") if active else request_id,
        "relay_url": active.get("relay_url") if active else resolved_relay_url,
        "conversation_key": active.get("conversation_key") if active else conversation_key,
        "bound_at": active.get("bound_at") if active else None,
    }


@mcp.tool(
    name="clear_relay_run",
    title="Clear Relay Run",
    annotations=LOCAL_STATE_TOOL,
    description=(
        "Unbind the relay run so subsequent tool calls are no longer mirrored to a "
        "relay. Equivalent to calling bind_relay_run with a null request_id. Safe to "
        "call even when nothing is bound."
    ),
)
def clear_relay_run() -> dict[str, object]:
    session.clear_bound_run()
    return {
        "success": True,
        "bound": False,
        "request_id": None,
        "relay_url": None,
    }


def build_http_app():
    streamable_app = mcp.http_app(
        path="/mcp",
        transport="streamable-http",
    )
    legacy_sse_app = mcp.http_app(
        path="/mcp",
        transport="sse",
    )
    return build_http_compat_app(
        streamable_app=streamable_app,
        legacy_sse_app=legacy_sse_app,
        app_name=APP_NAME,
        mcp_path="/mcp",
        get_auth_token=_current_auth_token,
        get_oauth_config=_current_oauth_config,
        get_debug_enabled=_current_debug_mcp_logging,
        instructions=MCP_INSTRUCTIONS,
    )


class _ReadySignalServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, *, ready_fd: int | None) -> None:
        super().__init__(config)
        self._ready_fd = ready_fd

    def _emit_ready(self) -> None:
        if self._ready_fd is None:
            return
        os.write(self._ready_fd, b"ready\n")
        os.close(self._ready_fd)
        self._ready_fd = None

    def _close_ready_fd(self) -> None:
        if self._ready_fd is None:
            return
        os.close(self._ready_fd)
        self._ready_fd = None

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        if not self.should_exit:
            self._emit_ready()

    async def serve(self, sockets=None) -> None:
        try:
            await super().serve(sockets=sockets)
        finally:
            self._close_ready_fd()


def _consume_ready_fd() -> int | None:
    raw_value = os.environ.pop("NOTION_LOCAL_OPS_READY_FD", "").strip()
    if not raw_value:
        return None
    return int(raw_value)


def build_uvicorn_server(*, fd: int | None = None, ready_fd: int | None = None) -> uvicorn.Server:
    app = build_http_app()
    config = uvicorn.Config(
        app,
        host=HOST,
        port=PORT,
        fd=fd,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    return _ReadySignalServer(config, ready_fd=ready_fd)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the notion-local-ops MCP server.")
    parser.add_argument("--fd", type=int, default=None, help="Inherited listening socket fd.")
    args = parser.parse_args(argv)

    ensure_runtime_directories()
    print(f"Starting {APP_NAME} on {HOST}:{PORT}")
    print(f"workspace_root={WORKSPACE_ROOT}")
    print(f"state_dir={STATE_DIR}")
    print("transport=streamable-http")
    print("mcp_path=/mcp")
    print(f"debug_mcp_logging={DEBUG_MCP_LOGGING}")
    print(f"graceful_shutdown_seconds={GRACEFUL_SHUTDOWN_SECONDS}")

    oauth_config = _current_oauth_config()
    if oauth_config.normalized_auth_mode == "oauth":
        if not oauth_config.public_base_url:
            print(
                "WARNING: NOTION_LOCAL_OPS_PUBLIC_BASE_URL is not set; OAuth "
                "metadata will fall back to the request Host header. Set it to "
                "your public tunnel URL (e.g. https://mcp.example.com) so issuer "
                "URLs cannot be spoofed."
            )
        if not oauth_config.oauth_login_token and oauth_config.auth_token:
            print(
                "WARNING: NOTION_LOCAL_OPS_OAUTH_LOGIN_TOKEN is not set; "
                "AUTH_TOKEN is being reused as the OAuth login token. Anyone "
                "with AUTH_TOKEN can mint long-TTL OAuth access tokens. After "
                "rotating AUTH_TOKEN, also clear oauth.json[\"tokens\"] under "
                f"{STATE_DIR}/oauth.json."
            )

    server = build_uvicorn_server(fd=args.fd, ready_fd=_consume_ready_fd())
    server.run()


if __name__ == "__main__":
    main()
