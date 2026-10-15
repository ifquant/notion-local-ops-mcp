from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anyio
import pytest

from notion_local_ops_mcp import session
from notion_local_ops_mcp.executors import ExecutorRegistry
from notion_local_ops_mcp.tasks import TaskStore
from notion_local_ops_mcp.workspaces import WorkError, current_scope
from test_mcp_local_simulation import _call_tool, _mcp_session, _running_server


def call(tool, *args, **kwargs):
    return (tool.fn if hasattr(tool, "fn") else tool)(*args, **kwargs)


@pytest.fixture
def scoped(tmp_path, monkeypatch):
    from notion_local_ops_mcp import server
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(store=store, codex_command=sys.executable + " -c \"print('delegate')\"", claude_command=None)
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "registry", registry)
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(session, "_default_cwd", None)
    runs = []
    for name in ("a", "b"):
        root = tmp_path / name
        root.mkdir()
        (root / "sub").mkdir()
        (root / "note.txt").write_text(name)
        workspace = call(server.open_workspace, path=name)
        run = call(server.start_run, workspace_id=workspace["workspace_id"], goal=name,
                   acceptance_criteria=["verified"], creation_key="first")
        assert run["success"]
        runs.append(run)
    return server, runs


def test_scoped_calls_and_legacy_defaults_do_not_bleed(scoped, tmp_path):
    server, (a, b) = scoped
    call(server.set_default_cwd, path=str(tmp_path / "b"))
    assert call(server.read_text, path="note.txt", run_id=a["run_id"])["content"] == "a"
    assert call(server.read_text, path="note.txt", run_id=b["run_id"])["content"] == "b"
    result = call(server.write_file, path="new.txt", content="inside", cwd="sub", run_id=a["run_id"])
    assert result["success"]
    assert (tmp_path / "a/sub/new.txt").read_text() == "inside"
    assert call(server.run_command, command="pwd", run_id=a["run_id"])["stdout"].strip() == str(tmp_path / "a")
    assert call(server.run_command, command="pwd")["stdout"].strip() == str(tmp_path / "b")
    call(server.write_file, path="legacy.txt", content="legacy")
    assert (tmp_path / "legacy.txt").read_text() == "legacy"
    assert current_scope.get() is None


def test_context_is_thread_local(scoped, tmp_path, monkeypatch):
    server, runs = scoped
    barrier = threading.Barrier(2)
    original = server.read_file_impl
    def read(*args, **kwargs):
        barrier.wait(timeout=5)
        return original(*args, **kwargs)
    monkeypatch.setattr(server, "read_file_impl", read)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda run: call(server.read_text, path="note.txt", run_id=run["run_id"]), runs))
    assert [result["content"] for result in results] == ["a", "b"]
    assert [result["run_id"] for result in results] == [run["run_id"] for run in runs]


@pytest.mark.parametrize("name,args", [
    ("list_files", {"path": "../b"}),
    ("search", {"path": "../b", "mode": "text", "query": "b"}),
    ("read_text", {"paths": ["note.txt", "../b/note.txt"]}),
    ("write_file", {"path": "../b/note.txt", "content": "bad"}),
    ("apply_patch", {"patch": "*** Begin Patch\n*** Update File: note.txt\n*** Move to: ../b/moved.txt\n@@\n-a\n+bad\n*** End Patch"}),
    ("run_command", {"command": "pwd", "cwd": "../b"}),
    ("run_command_stream", {"command": "pwd", "cwd": "../b"}),
    ("delegate_task", {"task": "inspect", "context_files": ["../b/note.txt"]}),
    ("git_status", {"cwd": "../b"}),
    ("list_skills", {"cwd": "../b"}),
])
def test_outside_targets_rejected_before_side_effects(scoped, tmp_path, name, args):
    server, (a, _) = scoped
    result = call(getattr(server, name), **args, run_id=a["run_id"])
    assert result["success"] is False, result
    assert result["error"]["code"] == "scope_conflict", result
    assert (tmp_path / "b/note.txt").read_text() == "b"
    assert (tmp_path / "a/note.txt").read_text() == "a"
    assert not (tmp_path / "state/tasks").exists()
    assert current_scope.get() is None


def test_symlink_escape_and_git_scope(scoped, tmp_path):
    server, (a, _) = scoped
    root = tmp_path / "a"
    (root / "linked.txt").symlink_to(tmp_path / "b/note.txt")
    assert call(server.read_text, path="linked.txt", run_id=a["run_id"])["error"]["code"] == "scope_conflict"
    matches = call(server.search, mode="text", query="b", respect_gitignore=False, run_id=a["run_id"])
    assert not matches["matches"]
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    assert call(server.git_status, run_id=a["run_id"])["success"]
    for name, args in [("git_diff", {"paths": ["../b/note.txt"]}),
                       ("git_commit", {"message": "test", "paths": ["../b/note.txt"]}),
                       ("git_blame", {"path": "../b/note.txt"}),
                       ("git_diff", {"paths": [":(top)*"]})]:
        assert call(getattr(server, name), **args, run_id=a["run_id"])["error"]["code"] == "scope_conflict"
    sub_workspace = call(server.open_workspace, path=str(root / "sub"))
    sub_run = call(server.start_run, workspace_id=sub_workspace["workspace_id"], goal="sub", acceptance_criteria=[], creation_key="sub")
    assert call(server.git_commit, message="bad", stage_all=True, run_id=sub_run["run_id"])["error"]["code"] == "scope_conflict"


def test_entire_patch_is_scope_checked_before_first_write(scoped, tmp_path):
    server, (a, _) = scoped
    patch = "*** Begin Patch\n*** Add File: good.txt\n+good\n*** Add File: ../b/bad.txt\n+bad\n*** End Patch"
    result = call(server.apply_patch, patch=patch, run_id=a["run_id"])
    assert result["error"]["code"] == "scope_conflict"
    assert not (tmp_path / "a/good.txt").exists()
    assert not (tmp_path / "b/bad.txt").exists()
    for run_id in ("", "unknown", "f" * 32):
        result = call(server.write_file, path="good.txt", content="bad", run_id=run_id)
        assert result["success"] is False
    assert not (tmp_path / "good.txt").exists()


def test_scoped_patch_and_project_skill_success(scoped, tmp_path):
    server, (a, _) = scoped
    patch = "*** Begin Patch\n*** Update File: note.txt\n@@\n-a\n+changed\n*** End Patch"
    assert call(server.apply_patch, patch=patch, run_id=a["run_id"])["success"]
    assert (tmp_path / "a/note.txt").read_text() == "changed"
    skills = tmp_path / "a/.agents/skills"
    (skills / "inside").mkdir(parents=True)
    (skills / "inside/SKILL.md").write_text("---\nname: inside\ndescription: in project\n---\n")
    outside = tmp_path / "b/SKILL.md"
    outside.write_text("---\nname: outside\ndescription: wrong project\n---\n")
    (skills / "escape").mkdir()
    (skills / "escape/SKILL.md").symlink_to(outside)
    result = call(server.list_skills, include_global=False, run_id=a["run_id"])
    assert [skill["name"] for skill in result["skills"]] == ["inside"]


@pytest.mark.parametrize("entry", ["run_command", "run_command_stream", "delegate_task"])
def test_task_association_all_submit_paths_and_control_checks(scoped, tmp_path, entry):
    server, (a, b) = scoped
    args = {"task": "inspect"} if entry == "delegate_task" else {"command": "printf done"}
    if entry == "run_command":
        args["run_in_background"] = True
    queued = call(getattr(server, entry), **args, run_id=a["run_id"])
    assert queued["run_id"] == a["run_id"]
    task_id = queued["task_id"]
    for name in ("get_task", "wait_task", "cancel_task"):
        assert call(getattr(server, name), task_id=task_id, run_id=b["run_id"])["error"]["code"] == "scope_conflict"
    result = call(server.wait_task, task_id=task_id, timeout=5)
    assert result["status"] == "succeeded", result
    assert result["run_id"] == a["run_id"]
    assert result["workspace_id"] == a["workspace_id"]
    assert "run_id" not in server.store.get(task_id)  # Association has one authoritative location.
    (tmp_path / "a").rename(tmp_path / "moved")
    assert call(server.get_task, task_id=task_id)["run_id"] == a["run_id"]
    assert call(server.cancel_task, task_id=task_id)["status"] == "succeeded"


def test_failed_association_never_starts_process(scoped, tmp_path, monkeypatch):
    server, (a, _) = scoped
    def fail(*args):
        raise WorkError("store_full", "injected full database")
    monkeypatch.setattr(server.registry.store.work, "associate_task", fail)
    marker = tmp_path / "a/started"
    result = call(server.run_command_stream, command=f"touch {shlex.quote(str(marker))}", run_id=a["run_id"])
    assert result["error"]["code"] == "store_full"
    assert not marker.exists()
    assert not server.registry._controls
    metas = list((tmp_path / "state/tasks").glob("*/meta.json"))
    assert len(metas) == 1
    assert json.loads(metas[0].read_text())["status"] == "failed"


def test_legacy_task_and_reopened_registry(scoped, tmp_path, monkeypatch):
    server, (a, _) = scoped
    legacy = call(server.run_command_stream, command="printf old", cwd=str(tmp_path / "a"))
    assert call(server.wait_task, task_id=legacy["task_id"], timeout=5)["run_id"] is None
    assert call(server.get_task, task_id=legacy["task_id"], run_id=a["run_id"])["error"]["code"] == "scope_conflict"
    queued = call(server.run_command_stream, command="printf new", run_id=a["run_id"])
    call(server.wait_task, task_id=queued["task_id"], timeout=5)
    reopened = ExecutorRegistry(store=TaskStore(tmp_path / "state"), codex_command=None, claude_command=None)
    monkeypatch.setattr(server, "registry", reopened)
    result = call(server.get_task, task_id=queued["task_id"])
    assert result["run_id"] == a["run_id"]
    assert result["status"] == "succeeded"
    assert result["control_available"] is False
    assert call(server.get_run, run_id=a["run_id"])["goal"] == "a"
    metadata = reopened.store._meta_path(queued["task_id"])
    metadata.rename(metadata.with_suffix(".saved"))
    missing = call(server.get_task, task_id=queued["task_id"])
    assert missing["success"] is False
    assert missing["error"]["code"] == "task_read_failed"
    assert missing["run_id"] == a["run_id"]
    assert missing["completed"] is False


def test_scoped_relay_does_not_use_global_binding(scoped, monkeypatch):
    from notion_local_ops_mcp.relay_bridge import RelayBridgeSink
    server, (a, _) = scoped
    monkeypatch.setattr(session, "_bound_run", {"request_id": "unrelated", "callback_token": "private", "relay_url": "http://127.0.0.1:1"})
    sent = []
    monkeypatch.setattr(RelayBridgeSink, "_post_trace", lambda *args, **kwargs: sent.append(args))
    assert call(server.read_text, path="note.txt", run_id=a["run_id"])["content"] == "a"
    call(server.get_run, run_id=a["run_id"])
    assert sent == []
    assert session.get_bound_run()["request_id"] == "unrelated"


def test_scoped_schema_and_capability_flags(scoped):
    server, _ = scoped
    async def inspect():
        tools = {tool.name: tool for tool in await server.mcp._list_tools()}
        for name in ("read_text", "write_file", "apply_patch", "search", "list_files", "list_skills",
                     "git_status", "git_diff", "git_commit", "git_log", "git_show", "git_blame",
                     "run_command", "run_command_stream", "delegate_task", "get_task", "wait_task", "cancel_task"):
            assert "run_id" in tools[name].parameters["properties"], name
        info = await call(server.server_info)
        assert info["work_identity"]["run_scoped_paths"] is True
        assert info["work_identity"]["checkpoint_recovery"] is False
        assert info["work_identity"]["submission_deduplication"] is False
    asyncio.run(inspect())


def test_two_http_clients_scoped_work_and_reconnect(tmp_path, monkeypatch):
    for name in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    with _running_server(tmp_path, monkeypatch, auth_token="phase2a-local-test") as url:
        async def scenario():
            async with _mcp_session(url, token="phase2a-local-test") as first:
                async with _mcp_session(url, token="phase2a-local-test") as second:
                    runs = []
                    for client, name in ((first, "a"), (second, "b")):
                        workspace = await _call_tool(client, "open_workspace", {"path": name})
                        run = await _call_tool(client, "start_run", {
                            "workspace_id": workspace["workspace_id"], "goal": name,
                            "acceptance_criteria": ["saved"], "creation_key": "first"})
                        runs.append(run)
                    await _call_tool(second, "set_default_cwd", {"path": str(tmp_path / "b")})
                    async with anyio.create_task_group() as group:
                        async def write(client, run, text):
                            result = await _call_tool(client, "write_file", {"path": "note.txt", "content": text, "run_id": run["run_id"]})
                            assert result["success"]
                        group.start_soon(write, first, runs[0], "alpha")
                        group.start_soon(write, second, runs[1], "beta")
                    read = await _call_tool(first, "read_text", {"path": "note.txt", "run_id": runs[0]["run_id"]})
                    assert read["content"] == "alpha"
                    wrong = await _call_tool(first, "write_file", {"path": "../b/note.txt", "content": "bad", "run_id": runs[0]["run_id"]})
                    assert wrong["error"]["code"] == "scope_conflict"
                    queued = await _call_tool(first, "run_command_stream", {"command": "printf complete", "run_id": runs[0]["run_id"]})
            async with _mcp_session(url, token="phase2a-local-test") as reconnect:
                recovered = await _call_tool(reconnect, "get_run", {"run_id": runs[0]["run_id"]})
                assert recovered["goal"] == "a"
                done = await _call_tool(reconnect, "wait_task", {"task_id": queued["task_id"], "timeout": 5})
                assert done["status"] == "succeeded"
                assert done["run_id"] == runs[0]["run_id"]
                assert done["stdout"] == "complete"
        anyio.run(scenario)
    assert (tmp_path / "a/note.txt").read_text() == "alpha"
    assert (tmp_path / "b/note.txt").read_text() == "beta"
