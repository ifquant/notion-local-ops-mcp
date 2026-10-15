"""Durable work identity; task JSON/logs remain the execution source of truth."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


SCHEMA_VERSION = 1
LOCK_WAIT_SECONDS = 2
RECORD_MAX_BYTES = 65536
STORE_MAX_BYTES = 64 * 1024 * 1024


class WorkError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def result(self) -> dict[str, object]:
        return {"success": False, "error": {"code": self.code, "message": str(self)}}


@dataclass(frozen=True)
class RunScope:
    run_id: str
    workspace_id: str
    root: Path

    def check(self, path: Path) -> Path:
        if not path.is_relative_to(self.root):
            raise WorkError("scope_conflict", f"Path is outside this run's workspace: {path}")
        return path


current_scope: ContextVar[RunScope | None] = ContextVar("work_scope", default=None)


def _id(value: str, name: str, length: int = 32) -> None:
    if not isinstance(value, str) or re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is None:
        raise WorkError("invalid_arguments", f"Invalid {name}.")


class WorkStore:
    def __init__(self, state_dir: Path) -> None:
        # Lazy initialization keeps legacy-only installations independent of SQLite.
        self.directory = state_dir / "work"
        self.path = self.directory / "identity.sqlite3"

    @contextmanager
    def _connect(self):
        connection = None
        try:
            if self.directory.is_symlink():
                raise WorkError("store_unsafe", "Work directory must not be a symbolic link.")
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.directory.chmod(0o700)
            for path in (self.path, *(Path(str(self.path) + suffix) for suffix in ("-journal", "-wal", "-shm"))):
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    # SQLite journal files can disappear when another process commits.
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise WorkError("store_unsafe", "Work database files must be regular, unlinked files.")
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            connection = sqlite3.connect(self.path, timeout=LOCK_WAIT_SECONDS)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            connection.execute(f"PRAGMA max_page_count = {STORE_MAX_BYTES // page_size}")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                connection.execute("BEGIN IMMEDIATE")
                # A second process may have initialized while we waited for the lock.
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    if connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                        raise WorkError("store_schema_error", "Unversioned database is not empty; migration required.")
                    for statement in (
                        "CREATE TABLE workspaces (workspace_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, "
                        "device INTEGER NOT NULL, inode INTEGER NOT NULL, created_at TEXT NOT NULL)",
                        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces, "
                        "goal TEXT NOT NULL, acceptance_criteria TEXT NOT NULL, creation_key TEXT NOT NULL, "
                        "created_at TEXT NOT NULL, UNIQUE(workspace_id, creation_key))",
                        "CREATE TABLE task_runs (task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs)",
                        "CREATE INDEX task_runs_run ON task_runs(run_id)",
                    ):
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    version = SCHEMA_VERSION
                connection.commit()
            if version != SCHEMA_VERSION:
                raise WorkError("store_schema_error", f"Unsupported work schema {version}; expected {SCHEMA_VERSION}.")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            code = getattr(exc, "sqlite_errorcode", 0) & 0xff
            if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                error = "store_busy"
            elif code == sqlite3.SQLITE_FULL:
                error = "store_full"
            else:
                error = "store_error"
            raise WorkError(error, f"Work storage unavailable; existing records were not reset: {exc}") from exc
        except OSError as exc:
            raise WorkError("store_io_error", f"Work storage unavailable: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()  # Rolls back unfinished transactions, including failed migrations.

    @staticmethod
    def _workspace(db, workspace_id: str) -> dict:
        _id(workspace_id, "workspace_id")
        row = db.execute("SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)).fetchone()
        if row is None:
            raise WorkError("workspace_not_found", "Workspace not found.")
        return dict(row)

    @staticmethod
    def _check_root(workspace: dict) -> Path:
        root = Path(workspace["path"])
        try:
            info = root.stat()
            valid = root.is_dir() and os.access(root, os.R_OK | os.X_OK) and root.resolve(strict=True) == root and (
                info.st_dev, info.st_ino) == (workspace["device"], workspace["inode"])
        except (OSError, RuntimeError):
            valid = False
        if not valid:
            raise WorkError("workspace_unavailable", "Workspace missing, inaccessible or replaced; explicit reconciliation required.")
        return root

    def open_workspace(self, path: Path) -> dict:
        try:
            root = path.expanduser().resolve(strict=True)
            if not root.is_dir():
                raise OSError("Workspace must be an existing directory.")
            info = root.stat()
        except (OSError, RuntimeError) as exc:
            raise WorkError("workspace_unavailable", str(exc)) from exc
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM workspaces WHERE path = ?", (str(root),)).fetchone()
            if row is None:
                db.execute("INSERT INTO workspaces VALUES (?, ?, ?, ?, ?)",
                           (uuid4().hex, str(root), info.st_dev, info.st_ino, datetime.now(UTC).isoformat()))
                row = db.execute("SELECT * FROM workspaces WHERE path = ?", (str(root),)).fetchone()
            workspace = dict(row)
            self._check_root(workspace)
            return {"success": True, **workspace}

    def start_run(self, workspace_id: str, goal: str, acceptance_criteria: list[str], creation_key: str) -> dict:
        if not isinstance(goal, str) or not goal.strip() or not isinstance(acceptance_criteria, list) or any(
                not isinstance(item, str) for item in acceptance_criteria):
            raise WorkError("invalid_arguments", "Provide a nonempty goal and a list of acceptance strings.")
        if not isinstance(creation_key, str) or not creation_key.strip() or len(creation_key.encode()) > 128:
            raise WorkError("invalid_arguments", "creation_key must be nonempty and at most 128 UTF-8 bytes.")
        criteria = json.dumps(acceptance_criteria, ensure_ascii=False)
        if len(json.dumps([goal, acceptance_criteria, creation_key], ensure_ascii=False).encode()) > RECORD_MAX_BYTES:
            raise WorkError("invalid_arguments", "Run input exceeds 64 KiB.")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            workspace = self._workspace(db, workspace_id)
            self._check_root(workspace)
            row = db.execute("SELECT * FROM runs WHERE workspace_id = ? AND creation_key = ?",
                             (workspace_id, creation_key)).fetchone()
            if row is None:
                run_id = uuid4().hex
                db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                           (run_id, workspace_id, goal, criteria, creation_key, datetime.now(UTC).isoformat()))
            else:
                if row["goal"] != goal or row["acceptance_criteria"] != criteria:
                    raise WorkError("creation_conflict", "creation_key already belongs to a different run request.")
                run_id = row["run_id"]
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict:
        _id(run_id, "run_id")
        with self._connect() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise WorkError("run_not_found", "Run not found.")
            run = dict(row)
            try:
                criteria = json.loads(run["acceptance_criteria"])
                if not isinstance(criteria, list) or any(not isinstance(item, str) for item in criteria):
                    raise ValueError("Invalid criteria")
            except (ValueError, TypeError) as exc:
                raise WorkError("store_error", "Stored run criteria are corrupt; records were not reset.") from exc
            run["acceptance_criteria"] = criteria
            workspace = self._workspace(db, run["workspace_id"])
        try:
            self._check_root(workspace)
            available = True
        except WorkError:
            available = False
        return {"success": True, **run, "workspace": workspace, "workspace_available": available}

    def scope(self, run_id: str) -> RunScope:
        run = self.get_run(run_id)
        return RunScope(run_id, run["workspace_id"], self._check_root(run["workspace"]))

    def associate_task(self, task_id: str, run_id: str) -> None:
        _id(task_id, "task_id", 12)
        _id(run_id, "run_id")
        with self._connect() as db:
            db.execute("INSERT INTO task_runs VALUES (?, ?)", (task_id, run_id))

    def task_run(self, task_id: str) -> dict | None:
        _id(task_id, "task_id", 12)
        if not self.path.exists() and not self.path.is_symlink():
            return None
        with self._connect() as db:
            row = db.execute("SELECT runs.run_id, runs.workspace_id FROM task_runs "
                             "JOIN runs USING(run_id) WHERE task_id = ?", (task_id,)).fetchone()
            return dict(row) if row else None
