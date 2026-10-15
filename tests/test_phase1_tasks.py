import base64
import json
import os
import shlex
import sys
import threading
import time
from pathlib import Path

import pytest

from notion_local_ops_mcp import execution, patching, shell
from notion_local_ops_mcp import executors
from notion_local_ops_mcp.executors import ExecutorRegistry, Invocation
from notion_local_ops_mcp.tasks import TaskStore


def command(code):
    return "exec " + shlex.quote(sys.executable) + " -u -c " + shlex.quote(code)


@pytest.fixture
def registry(tmp_path):
    reg = ExecutorRegistry(store=TaskStore(tmp_path / "state"), codex_command=sys.executable,
                           claude_command=None, slots=threading.BoundedSemaphore(4))
    yield reg
    for task_id in list(reg._controls):
        with reg._lock:
            event = reg._completion_events.get(task_id)
        reg.cancel(task_id)
        if event:
            assert event.wait(5), "test worker did not finish cleanup"
    assert not reg._controls, "test left managed processes running"


def submit(registry, tmp_path, code, timeout=5):
    result = registry.submit_command(command=command(code), cwd=tmp_path, timeout=timeout)
    assert "task_id" in result, result
    return result["task_id"]


def test_live_output_cursor_utf8_and_multiple_readers(registry, tmp_path):
    gate = tmp_path / "release"
    task = submit(registry, tmp_path,
                  f"import os,time,pathlib; os.write(1, '中文😀'.encode()); "
                  f"\nwhile not pathlib.Path({str(gate)!r}).exists(): time.sleep(.01)\nprint('DONE')")
    first = registry.wait(task, timeout=2, cursor="start", return_on_output=True)
    assert first["stdout"] == "中文😀" and not first["completed"]
    assert registry.get(task, cursor="start")["stdout"] == first["stdout"]
    gate.touch()
    final = registry.wait(task, timeout=3, cursor=first["next_cursor"])
    assert final["stdout"] == "DONE\n"
    assert final["completed"] and not final["has_more"]


def test_dual_stream_flood_is_bounded_and_drained(registry, tmp_path):
    registry.log_limit = 1024
    task = submit(registry, tmp_path, "import os; [(os.write(1,b'x'*8192),os.write(2,b'y'*8192)) for _ in range(100)]")
    result = registry.wait(task, timeout=5, cursor="start")
    assert result["status"] == "succeeded"
    assert result["output_truncated"] and result["dropped_bytes"] > 0
    assert len(result["stdout"].encode()) + len(result["stderr"].encode()) == 1024
    assert result["structured_output"] is None


def test_completed_can_have_more_unread_output(registry, tmp_path):
    task = submit(registry, tmp_path, "print('abcdef'*100)")
    result = registry.wait(task, timeout=5, cursor="start", max_bytes=8)
    assert result["completed"] and result["has_more"]
    chunks = [result["stdout"]]
    while result["has_more"]:
        result = registry.get(task, cursor=result["next_cursor"], max_bytes=8)
        chunks.append(result["stdout"])
    assert "".join(chunks) == "abcdef" * 100 + "\n"


def test_wait_timeout_never_erases_execution_timeout(registry, tmp_path):
    task = submit(registry, tmp_path, "import time; time.sleep(10)", timeout=.2)
    first = registry.wait(task, timeout=0)
    assert first["wait_timed_out"] and not first["execution_timed_out"]
    final = registry.wait(task, timeout=5)
    assert final["status"] == "failed" and final["execution_timed_out"]
    assert not final["wait_timed_out"]
    assert registry.store.get(task)["timed_out"]
    assert registry.get(task)["execution_timed_out"]


def test_cancel_terminal_is_idempotent(registry, tmp_path):
    task = submit(registry, tmp_path, "print('done')")
    assert registry.wait(task, timeout=5)["status"] == "succeeded"
    assert registry.cancel(task)["status"] == "succeeded"
    assert registry.cancel(task)["cancelled"] is False


def test_cancel_before_invocation_prevents_launch(registry, tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    marker = tmp_path / "should-not-exist"
    original = registry._build_invocation
    def invocation(**kwargs):
        entered.set()
        assert release.wait(5)
        return Invocation(command(f"from pathlib import Path; Path({str(marker)!r}).touch()"), True)
    monkeypatch.setattr(registry, "_build_invocation", invocation)
    task = registry.submit(task="test", executor="codex", cwd=tmp_path, timeout=5)["task_id"]
    assert entered.wait(2)
    with registry._lock:
        assert registry._controls[task].request_stop("cancelled")
    release.set()
    assert registry.wait(task, timeout=5)["status"] == "cancelled"
    assert not marker.exists()


def test_multiple_waiters_wake_after_cancel(registry, tmp_path):
    task = submit(registry, tmp_path, "import time; time.sleep(10)")
    barrier = threading.Barrier(4)
    results = []
    def waiter():
        barrier.wait()
        results.append(registry.wait(task, timeout=5))
    threads = [threading.Thread(target=waiter) for _ in range(3)]
    for thread in threads:
        thread.start()
    barrier.wait()
    assert registry.cancel(task)["cancelled"]
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert all(r["status"] == "cancelled" for r in results)


def test_cancel_kills_term_ignoring_child_group(registry, tmp_path):
    task = submit(registry, tmp_path,
                  "import signal,time,os; signal.signal(signal.SIGTERM,signal.SIG_IGN); print(os.getpid()); time.sleep(20)", timeout=25)
    started = registry.wait(task, timeout=2, cursor="start", return_on_output=True)
    pid = int(started["stdout"].strip())
    assert registry.cancel(task)["cancelled"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_exited_parent_with_child_holding_pipe_is_bounded(registry, tmp_path):
    # Parent exits while the ordinary same-group child retains both pipes.
    task = submit(registry, tmp_path,
                  "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'])", timeout=.2)
    final = registry.wait(task, timeout=5)
    assert final["completed"], final
    assert final["execution_timed_out"]


def test_capacity_shared_and_released(registry, tmp_path):
    registry.slots = threading.BoundedSemaphore(1)
    task = submit(registry, tmp_path, "import time; time.sleep(20)", timeout=25)
    refused = registry.submit_command(command="echo should-not-start", cwd=tmp_path, timeout=2)
    assert refused["error"]["code"] == "capacity_exceeded"
    registry.cancel(task)
    second = submit(registry, tmp_path, "print('ok')")
    assert registry.wait(second, timeout=3)["status"] == "succeeded"


def test_delegate_spawn_error_is_terminal(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "_build_invocation", lambda **kw: Invocation(["/not/a/real/executable"], False))
    task = registry.submit(task="fail", executor="codex", cwd=tmp_path, timeout=3)["task_id"]
    result = registry.wait(task, timeout=3)
    assert result["status"] == "failed" and result["completed"]
    assert "execution_failed" in result["error"]


def test_disk_log_failure_stops_process(registry, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("simulated disk full")
    monkeypatch.setattr(registry.store, "append_log", fail)
    task = submit(registry, tmp_path, "import time; print('go'); time.sleep(20)", timeout=25)
    result = registry.wait(task, timeout=5)
    assert result["status"] == "failed" and "output_write_failed" in result["error"]


def test_metadata_failure_has_queryable_memory_error(registry, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("simulated metadata failure")
    monkeypatch.setattr(registry.store, "update", fail)
    task = submit(registry, tmp_path, "print('not started')")
    result = registry.wait(task, timeout=2)
    assert result["success"] is False and result.get("persistence_error")


def test_invalid_and_foreign_log_cursor(registry, tmp_path):
    task = submit(registry, tmp_path, "print('中')")
    result = registry.wait(task, timeout=3)
    assert registry.get(task, cursor="garbage")["success"] is False
    cursor = base64.urlsafe_b64encode(json.dumps({"task_id":task, "offsets":[1,0]}).encode()).decode()
    assert registry.get(task, cursor=cursor)["success"] is False
    assert registry.get("../escape")["success"] is False
    assert registry.wait(task, timeout=float("nan"))["success"] is False
    assert registry.wait(task, timeout=31)["success"] is False


def test_purge_protects_active_corrupt_and_linked_tasks(registry, tmp_path):
    store = registry.store
    active = store.create(task="old active", executor="shell", cwd=str(tmp_path))["task_id"]
    path = store._meta_path(active)
    payload = store.get(active)
    payload["updated_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(payload))
    corrupt = store.create(task="corrupt", executor="shell", cwd=str(tmp_path))["task_id"]
    store._meta_path(corrupt).write_text("{")
    external = tmp_path / "external"
    external.mkdir()
    (store.root / "tasks" / "aaaaaaaaaaaa").symlink_to(external, target_is_directory=True)
    result = store.purge_tasks(older_than_seconds=0)
    assert result["purged"] == 0 and len(result["skipped"]) == 3
    assert external.exists() and path.exists()
    assert registry.cancel(active)["error"]["code"] == "control_unavailable"


def test_partial_patch_reports_real_disk_changes(tmp_path, monkeypatch):
    for name in ("a", "b", "c"):
        (tmp_path / name).write_text("old\n")
    original = patching._apply_change
    def fail_second(change):
        if change.path.name == "b":
            raise OSError("simulated write failure")
        return original(change)
    monkeypatch.setattr(patching, "_apply_change", fail_second)
    patch = "*** Begin Patch\n" + "".join(f"*** Update File: {name}\n@@\n-old\n+new\n" for name in ("a", "b", "c")) + "*** End Patch"
    result = patching.apply_patch(patch=patch, workspace_root=tmp_path)
    assert result["applied_files"] == [str(tmp_path / "a")]
    assert result["failed_file"] == str(tmp_path / "b")
    assert result["not_applied_files"] == [str(tmp_path / "c")]
    assert (tmp_path / "a").read_text() == "new\n"
    assert (tmp_path / "c").read_text() == "old\n"


def test_sync_output_cap_and_stdin_closed(tmp_path):
    result = shell.run_command(command=command("import sys; assert sys.stdin.read() == ''; print('x'*100000)"), cwd=tmp_path, timeout=5)
    assert result["success"] and result["output_truncated"]
    assert len(result["stdout"].encode()) <= 32768


def test_first_stop_reason_wins_and_finished_control_rejects_stop():
    control = execution.ProcessControl()
    assert control.request_stop("timeout")
    assert not control.request_stop("cancelled")
    assert control.stop_reason == "timeout"
    control.finished = True
    assert not control.request_stop("timeout")


def test_cleanup_failure_keeps_capacity_until_confirmed_exit(registry, tmp_path, monkeypatch):
    registry.slots = threading.BoundedSemaphore(1)
    original_alive = execution.group_alive
    original_run = executors.run_process
    permit_confirmation = threading.Event()
    reported = threading.Event()
    def alive(pid):
        return original_alive(pid) or not permit_confirmation.is_set()
    def run(**kwargs):
        original_callback = kwargs["on_cleanup_failed"]
        def callback(message):
            original_callback(message)
            reported.set()
        kwargs["on_cleanup_failed"] = callback
        return original_run(**kwargs, term_grace=.01, kill_grace=.01)
    monkeypatch.setattr(execution, "group_alive", alive)
    monkeypatch.setattr(executors, "run_process", run)
    task = submit(registry, tmp_path, "print('exit')", timeout=.1)
    try:
        assert reported.wait(3)
        result = registry.get(task)
        assert result["cleanup_failed"] and not result["completed"]
        refused = registry.submit_command(command="echo no", cwd=tmp_path, timeout=1)
        assert refused["error"]["code"] == "capacity_exceeded"
    finally:
        permit_confirmation.set()
    # A cleanup_failed response is deliberately immediate; use the worker event for reconciliation.
    with registry._lock:
        event = registry._completion_events.get(task)
    if event:
        assert event.wait(3)
    assert registry.get(task)["completed"]


def test_split_utf8_output_decoder(registry, tmp_path):
    gate = tmp_path / "continue"
    code = (f"import os,time,pathlib; os.write(1, b'\\xe4'); print('ready',file=__import__('sys').stderr);"
            f"\nwhile not pathlib.Path({str(gate)!r}).exists(): time.sleep(.01)\nos.write(1, b'\\xb8\\xad')")
    task = submit(registry, tmp_path, code)
    first = registry.wait(task, timeout=2, cursor="start", return_on_output=True)
    assert first["stderr"] == "ready\n" and first["stdout"] == ""
    gate.touch()
    final = registry.wait(task, timeout=3, cursor=first["next_cursor"])
    assert final["stdout"] == "中"


def test_purge_waits_for_read_snapshot_without_half_deleted_logs(registry, tmp_path, monkeypatch):
    task = submit(registry, tmp_path, "print('stored')")
    assert registry.wait(task, timeout=3)["completed"]
    entered, release = threading.Event(), threading.Event()
    original = registry.store.read_summary
    def summary(task_id):
        entered.set()
        assert release.wait(3)
        return original(task_id)
    monkeypatch.setattr(registry.store, "read_summary", summary)
    reads, purges = [], []
    reader = threading.Thread(target=lambda: reads.append(registry.get(task, cursor="start")))
    purger = threading.Thread(target=lambda: purges.append(registry.store.purge_tasks(older_than_seconds=0)))
    reader.start()
    assert entered.wait(2)
    purger.start()
    release.set()
    reader.join(3)
    purger.join(3)
    assert not reader.is_alive() and not purger.is_alive()
    assert reads[0]["stdout"] == "stored\n" and purges[0]["purged"] == 1
    assert registry.get(task)["success"] is False


def test_log_reads_use_seek_not_full_read(registry, tmp_path, monkeypatch):
    task = submit(registry, tmp_path, "print('x'*100000)")
    assert registry.wait(task, timeout=3)["completed"]
    monkeypatch.setattr(registry.store, "read_stdout", lambda _: pytest.fail("whole-file read"))
    monkeypatch.setattr(registry.store, "read_stderr", lambda _: pytest.fail("whole-file read"))
    result = registry.get(task, cursor="start", max_bytes=32)
    assert len(result["stdout"].encode()) == 32


def test_natural_exit_observed_before_cancel_keeps_success(registry, tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = registry.store.write_summary
    def summary(task_id, text):
        entered.set()
        assert release.wait(3)
        original(task_id, text)
    monkeypatch.setattr(registry.store, "write_summary", summary)
    task = submit(registry, tmp_path, "print('done')")
    assert entered.wait(2)
    with registry._lock:
        control = registry._controls[task]
    assert control.finished and not control.request_stop("cancelled")
    release.set()
    assert registry.wait(task, timeout=3)["status"] == "succeeded"


def test_sync_and_background_share_process_admission(registry, tmp_path, monkeypatch):
    registry.slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(shell, "EXECUTION_SLOTS", registry.slots)
    task = submit(registry, tmp_path, "import time; time.sleep(20)", timeout=25)
    result = shell.run_command(command="echo no", cwd=tmp_path, timeout=2)
    assert result["error"]["code"] == "capacity_exceeded"
    registry.cancel(task)
    assert shell.run_command(command="echo yes", cwd=tmp_path, timeout=2)["success"]


def test_wrong_task_and_future_cursor_rejected(registry, tmp_path):
    first = submit(registry, tmp_path, "print('first')")
    second = submit(registry, tmp_path, "print('second')")
    cursor = registry.wait(first, timeout=3)["next_cursor"]
    registry.wait(second, timeout=3)
    assert registry.get(second, cursor=cursor)["success"] is False
    future = base64.urlsafe_b64encode(json.dumps({"task_id": first, "offsets": [999999, 0]}).encode()).decode()
    assert registry.get(first, cursor=future)["success"] is False


def test_direct_write_failure_requires_reread(tmp_path, monkeypatch):
    from notion_local_ops_mcp.files import write_file
    path = tmp_path / "target"
    def fail(self, *args, **kwargs):
        raise OSError("simulated partial write")
    monkeypatch.setattr(Path, "write_text", fail)
    result = write_file(path, content="new")
    assert not result["success"] and result["requires_reread"]


@pytest.mark.parametrize("value,expected", [("false", False), ("0", 0)])
def test_small_json_false_and_zero_are_not_lost(registry, tmp_path, value, expected):
    task = submit(registry, tmp_path, f"print({value!r})")
    result = registry.wait(task, timeout=3)
    assert result["structured_output"] == expected
    assert result["structured_output_status"] == "parsed"
    assert result["schema_validation"] == "not_performed"
