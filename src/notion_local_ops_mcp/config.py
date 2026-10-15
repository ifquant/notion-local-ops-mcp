"""Process-wide configuration loaded from environment variables.

Semantic note on ``WORKSPACE_ROOT`` / ``DEFAULT_CWD``
-----------------------------------------------------
Despite the name "root", this value is **not a sandbox boundary**. The project
is designed to give an MCP client (Notion Agent / Codex / Claude) arbitrary
local-shell capability; once a client passes the bearer token it has full shell
and full-filesystem access.

``WORKSPACE_ROOT`` is only used for two things:

1. **Relative-path anchor.** :func:`notion_local_ops_mcp.pathing.resolve_path`
   joins relative inputs onto it; absolute paths are returned as-is.
2. **Default ``cwd``.** :func:`notion_local_ops_mcp.pathing.resolve_cwd`
   falls back to it when neither the tool call nor the session-level override
   (``set_default_cwd``) provides a directory.

It therefore behaves like a *default working directory*, not a root. The
``DEFAULT_CWD`` alias below reflects that; ``WORKSPACE_ROOT`` is kept for
back-compat. The environment variable name ``NOTION_LOCAL_OPS_WORKSPACE_ROOT``
stays unchanged to avoid breaking existing setups.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_limit(name: str, default: int) -> int:
    value = int(os.environ.get(f"NOTION_LOCAL_OPS_{name}", str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


READ_MAX_LINES = _positive_limit("READ_MAX_LINES", 2000)
READ_MAX_BYTES = _positive_limit("READ_MAX_BYTES", 32768)
READ_BATCH_FILES = _positive_limit("READ_BATCH_FILES", 16)
READ_BATCH_BYTES = _positive_limit("READ_BATCH_BYTES", 65536)
LOG_READ_BYTES = _positive_limit("LOG_READ_BYTES", 32768)
LOG_RETAIN_BYTES = _positive_limit("LOG_RETAIN_BYTES", 16 * 1024 * 1024)
COMMAND_OUTPUT_BYTES = _positive_limit("COMMAND_OUTPUT_BYTES", 32768)
MAX_EXECUTIONS = _positive_limit("MAX_EXECUTIONS", 4)
MAX_WAIT_SECONDS = _positive_limit("MAX_WAIT_SECONDS", 30)
TERM_GRACE_SECONDS = _positive_limit("TERM_GRACE_SECONDS", 2)
KILL_GRACE_SECONDS = _positive_limit("KILL_GRACE_SECONDS", 2)


APP_NAME = "notion-local-ops-mcp"
HOST = os.environ.get("NOTION_LOCAL_OPS_HOST", "127.0.0.1")
PORT = int(os.environ.get("NOTION_LOCAL_OPS_PORT", "8766"))

# Default cwd for tool calls (see module docstring). Kept as WORKSPACE_ROOT
# for back-compat; DEFAULT_CWD is the preferred name going forward.
WORKSPACE_ROOT = Path(
    os.environ.get("NOTION_LOCAL_OPS_WORKSPACE_ROOT", str(Path.home()))
).expanduser().resolve()
DEFAULT_CWD = WORKSPACE_ROOT

STATE_DIR = Path(
    os.environ.get("NOTION_LOCAL_OPS_STATE_DIR", str(Path.home() / ".notion-local-ops-mcp"))
).expanduser().resolve()
AUTH_TOKEN = os.environ.get("NOTION_LOCAL_OPS_AUTH_TOKEN", "").strip()
AUTH_MODE = os.environ.get("NOTION_LOCAL_OPS_AUTH_MODE", "").strip().lower()
PUBLIC_BASE_URL = os.environ.get("NOTION_LOCAL_OPS_PUBLIC_BASE_URL", "").strip().rstrip("/")
OAUTH_LOGIN_TOKEN = os.environ.get("NOTION_LOCAL_OPS_OAUTH_LOGIN_TOKEN", "").strip()
OAUTH_SCOPES = tuple(
    scope
    for scope in os.environ.get("NOTION_LOCAL_OPS_OAUTH_SCOPES", "local-ops").split()
    if scope
)
OAUTH_TOKEN_TTL_SECONDS = int(os.environ.get("NOTION_LOCAL_OPS_OAUTH_TOKEN_TTL_SECONDS", "86400"))
CODEX_COMMAND = os.environ.get("NOTION_LOCAL_OPS_CODEX_COMMAND", "codex").strip()
CLAUDE_COMMAND = os.environ.get("NOTION_LOCAL_OPS_CLAUDE_COMMAND", "claude").strip()
COMMAND_TIMEOUT = int(os.environ.get("NOTION_LOCAL_OPS_COMMAND_TIMEOUT", "120"))
DELEGATE_TIMEOUT = int(os.environ.get("NOTION_LOCAL_OPS_DELEGATE_TIMEOUT", "1800"))
DEBUG_MCP_LOGGING = _env_flag("NOTION_LOCAL_OPS_DEBUG_MCP_LOGGING", default=False)
GRACEFUL_SHUTDOWN_SECONDS = int(
    os.environ.get("NOTION_LOCAL_OPS_GRACEFUL_SHUTDOWN_SECONDS", "30")
)

# Relay bridge: when enabled and a relay run is bound (see session.set_bound_run),
# each traced tool call fires a fire-and-forget HTTP trace to the relay's
# /internal/tool-trace endpoint so a local operator can watch tool calls in
# real time. Disabled or unbound => zero behavior change.
RELAY_BRIDGE_ENABLED = _env_flag("NOTION_LOCAL_OPS_RELAY_BRIDGE_ENABLED", default=True)
RELAY_BRIDGE_TIMEOUT = float(
    os.environ.get("NOTION_LOCAL_OPS_RELAY_BRIDGE_TIMEOUT", "1.5")
)
RELAY_BINDING_TTL_SECONDS = float(
    os.environ.get("NOTION_LOCAL_OPS_RELAY_BINDING_TTL_SECONDS", "3600")
)
# Default relay endpoint used when bind_relay_run is called without an explicit
# relay_url. Lets the agent omit the relay address entirely (it only needs to
# pass request_id + callback_token), keeping the relay host/port a deployment
# detail rather than something baked into agent instructions.
RELAY_URL = os.environ.get("NOTION_LOCAL_OPS_RELAY_URL", "http://127.0.0.1:8799").strip().rstrip("/")


def ensure_runtime_directories() -> None:
    if not WORKSPACE_ROOT.exists():
        raise FileNotFoundError(f"Default cwd does not exist: {WORKSPACE_ROOT}")
    if not WORKSPACE_ROOT.is_dir():
        raise NotADirectoryError(f"Default cwd is not a directory: {WORKSPACE_ROOT}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        STATE_DIR.chmod(0o700)
    except OSError:
        pass
