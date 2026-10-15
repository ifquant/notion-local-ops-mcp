from __future__ import annotations

from pathlib import Path

from . import session
from .workspaces import WorkError, current_scope


def resolve_path(path: str, workspace_root: Path) -> Path:
    raw = Path(path).expanduser()
    target = raw.resolve(strict=False) if raw.is_absolute() else (workspace_root / raw).resolve(strict=False)
    scope = current_scope.get()
    return scope.check(target) if scope else target


def resolve_cwd(cwd: str | None, workspace_root: Path) -> Path:
    """Resolve a tool ``cwd`` argument.

    Fallback order when ``cwd`` is not provided:
    1. session.get_default_cwd() (set via the set_default_cwd tool)
    2. workspace_root
    """
    scope = current_scope.get()
    if scope:
        return resolve_file_cwd(cwd, scope.root)
    if cwd:
        return resolve_path(cwd, workspace_root)
    session_cwd = session.get_default_cwd()
    if session_cwd is not None:
        return session_cwd
    return workspace_root


def resolve_file_cwd(cwd: str | None, workspace_root: Path) -> Path:
    """Explicit per-call file base; legacy calls never use the shared override."""
    scope = current_scope.get()
    if scope:
        workspace_root = scope.root
    base = resolve_path(cwd, workspace_root) if cwd is not None else workspace_root
    if not base.is_dir():
        if scope:
            raise WorkError("cwd_unavailable", f"cwd must be an existing directory: {base}")
        raise ValueError(f"cwd must be an existing directory: {base}")
    return base
