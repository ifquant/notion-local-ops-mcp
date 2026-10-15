from __future__ import annotations

import multiprocessing
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from notion_local_ops_mcp import workspaces
from notion_local_ops_mcp.workspaces import WorkError, WorkStore


def _open_and_start(state, project, barrier, queue):
    try:
        store = WorkStore(Path(state))
        barrier.wait(timeout=10)
        for _ in range(20):
            workspace = store.open_workspace(Path(project))
            run = store.start_run(workspace["workspace_id"], "goal", ["verified"], "same-request")
        queue.put((os.getpid(), workspace["workspace_id"], run["run_id"]))
    except Exception as exc:
        queue.put(("error", repr(exc)))


def test_identity_is_unique_across_processes_and_survives_restart(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / "state"
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_open_and_start, args=(str(state), str(project), barrier, queue)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        results = [queue.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert all(result[0] != "error" for result in results), results
        assert results[0][0] != results[1][0]
        assert results[0][1:] == results[1][1:]
        reopened = WorkStore(state)
        assert reopened.get_run(results[0][2])["goal"] == "goal"
        with sqlite3.connect(reopened.path) as db:
            assert db.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        queue.close()


def test_aliases_separate_roots_and_creation_conflict(tmp_path):
    store = WorkStore(tmp_path / "state")
    project = tmp_path / "project"
    project.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    first = store.open_workspace(project)
    assert store.open_workspace(alias)["workspace_id"] == first["workspace_id"]
    other = tmp_path / "other-worktree"
    other.mkdir()
    assert store.open_workspace(other)["workspace_id"] != first["workspace_id"]
    run = store.start_run(first["workspace_id"], "goal", ["done"], "key")
    assert store.start_run(first["workspace_id"], "goal", ["done"], "key") == run
    with pytest.raises(WorkError, match="different run request"):
        store.start_run(first["workspace_id"], "different", ["done"], "key")
    assert store.start_run(first["workspace_id"], "goal", ["done"], "another")["run_id"] != run["run_id"]
    assert store.directory.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_replaced_or_missing_root_not_silently_rebound(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = WorkStore(tmp_path / "state")
    workspace = store.open_workspace(project)
    run = store.start_run(workspace["workspace_id"], "goal", [], "key")
    project.rename(tmp_path / "moved")
    assert store.get_run(run["run_id"])["workspace_available"] is False
    project.mkdir()
    with pytest.raises(WorkError) as error:
        store.open_workspace(project)
    assert error.value.code == "workspace_unavailable"
    with pytest.raises(WorkError):
        store.scope(run["run_id"])


def test_locked_database_fails_bounded_and_recovers(tmp_path, monkeypatch):
    store = WorkStore(tmp_path / "state")
    workspace = store.open_workspace(tmp_path)
    monkeypatch.setattr(workspaces, "LOCK_WAIT_SECONDS", 0.05)
    with sqlite3.connect(store.path) as locked:
        locked.execute("BEGIN IMMEDIATE")
        start = time.monotonic()
        with pytest.raises(WorkError) as error:
            store.start_run(workspace["workspace_id"], "goal", [], "key")
        assert error.value.code == "store_busy"
        assert time.monotonic() - start < 1
    assert store.start_run(workspace["workspace_id"], "goal", [], "key")["success"]


@pytest.mark.parametrize("kind", ["corrupt", "future", "partial"])
def test_invalid_database_never_reset(tmp_path, kind):
    store = WorkStore(tmp_path / "state")
    store.directory.mkdir(parents=True)
    if kind == "corrupt":
        store.path.write_bytes(b"not a sqlite database")
    else:
        with sqlite3.connect(store.path) as db:
            db.execute("CREATE TABLE evidence (value TEXT)")
            db.execute("INSERT INTO evidence VALUES ('keep')")
            if kind == "future":
                db.execute("PRAGMA user_version = 999")
    before = store.path.read_bytes()
    with pytest.raises(WorkError):
        store.open_workspace(tmp_path)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["directory_link", "file_link", "hardlink"])
def test_unsafe_store_paths_rejected(tmp_path, kind):
    store = WorkStore(tmp_path / "state")
    store.directory.parent.mkdir()
    outside = tmp_path / "outside"
    if kind == "directory_link":
        outside.mkdir()
        store.directory.symlink_to(outside, target_is_directory=True)
    else:
        store.directory.mkdir()
        outside.write_bytes(b"keep")
        if kind == "file_link":
            store.path.symlink_to(outside)
        else:
            os.link(outside, store.path)
    with pytest.raises(WorkError) as error:
        store.open_workspace(tmp_path)
    assert error.value.code == "store_unsafe"
    if outside.is_file():
        assert outside.read_bytes() == b"keep"


def test_store_limit_rolls_back_without_losing_records(tmp_path, monkeypatch):
    store = WorkStore(tmp_path / "state")
    workspace = store.open_workspace(tmp_path)
    run = store.start_run(workspace["workspace_id"], "keep", [], "keep")
    monkeypatch.setattr(workspaces, "STORE_MAX_BYTES", store.path.stat().st_size)
    with pytest.raises(WorkError) as error:
        store.start_run(workspace["workspace_id"], "x" * 50000, [], "new")
    assert error.value.code == "store_full"
    assert store.get_run(run["run_id"])["goal"] == "keep"
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_storage_io_failure_and_input_limits(tmp_path, monkeypatch):
    store = WorkStore(tmp_path / "state")
    workspace = store.open_workspace(tmp_path)
    for goal, criteria, key in [("", [], "k"), ("a", [1], "k"), ("a", [], ""), ("a", [], "界" * 100), ("x" * 65536, [], "k")]:
        with pytest.raises(WorkError) as error:
            store.start_run(workspace["workspace_id"], goal, criteria, key)
        assert error.value.code == "invalid_arguments"
    before = store.path.read_bytes()
    def fail(*args, **kwargs):
        raise PermissionError("read only")
    monkeypatch.setattr(workspaces.os, "open", fail)
    with pytest.raises(WorkError) as error:
        store.start_run(workspace["workspace_id"], "goal", [], "key")
    assert error.value.code == "store_io_error"
    assert store.path.read_bytes() == before


def test_task_association_persistent_and_no_status_copy(tmp_path):
    store = WorkStore(tmp_path / "state")
    workspace = store.open_workspace(tmp_path)
    run = store.start_run(workspace["workspace_id"], "goal", [], "key")
    store.associate_task("a" * 12, run["run_id"])
    assert WorkStore(tmp_path / "state").task_run("a" * 12) == {
        "run_id": run["run_id"], "workspace_id": workspace["workspace_id"]}
    assert store.task_run("b" * 12) is None
    with sqlite3.connect(store.path) as db:
        assert [row[1] for row in db.execute("PRAGMA table_info(task_runs)")] == ["task_id", "run_id"]


def test_schema_initialization_failure_rolls_back(tmp_path, monkeypatch):
    store = WorkStore(tmp_path / "state")
    original = sqlite3.connect
    class FailingConnection(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if statement.startswith("CREATE TABLE runs"):
                raise sqlite3.OperationalError("injected migration failure")
            return super().execute(statement, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(workspaces.sqlite3, "connect", lambda *args, **kwargs: original(*args, **kwargs, factory=FailingConnection))
        with pytest.raises(WorkError) as error:
            store.open_workspace(tmp_path)
        assert error.value.code == "store_error"
    with original(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    assert store.open_workspace(tmp_path)["success"]


def test_linked_git_worktrees_have_distinct_workspace_ids(tmp_path):
    project, linked = tmp_path / "project", tmp_path / "linked"
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-qm", "initial"], cwd=project, check=True)
    subprocess.run(["git", "worktree", "add", "--detach", str(linked)], cwd=project,
                   check=True, capture_output=True)
    store = WorkStore(tmp_path / "state")
    assert store.open_workspace(project)["workspace_id"] != store.open_workspace(linked)["workspace_id"]


@pytest.mark.parametrize("criteria", ["broken json", "{}", "[1]"])
def test_corrupt_run_record_fails_without_reset(tmp_path, criteria):
    store = WorkStore(tmp_path / "state")
    workspace = store.open_workspace(tmp_path)
    run = store.start_run(workspace["workspace_id"], "goal", [], "key")
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE runs SET acceptance_criteria = ?", (criteria,))
    with pytest.raises(WorkError) as error:
        store.get_run(run["run_id"])
    assert error.value.code == "store_error"
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT acceptance_criteria FROM runs").fetchone()[0] == criteria


def test_inaccessible_root_is_reported_without_losing_identity(tmp_path, monkeypatch):
    store = WorkStore(tmp_path / "state")
    workspace = store.open_workspace(tmp_path)
    run = store.start_run(workspace["workspace_id"], "goal", [], "key")
    monkeypatch.setattr(workspaces.os, "access", lambda *args: False)
    assert store.get_run(run["run_id"])["workspace_available"] is False
    with pytest.raises(WorkError) as error:
        store.scope(run["run_id"])
    assert error.value.code == "workspace_unavailable"
