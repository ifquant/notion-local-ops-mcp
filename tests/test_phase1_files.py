from pathlib import Path

import pytest

from notion_local_ops_mcp import server, session
from notion_local_ops_mcp.files import read_file, read_files


def read(path, **kwargs):
    return read_file(path, offset=None, limit=None, max_lines=200, max_bytes=32, **kwargs)


def test_explicit_file_cwd_does_not_follow_shared_override(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    (tmp_path / "same.txt").write_text("legacy")
    (first / "same.txt").write_text("first")
    (second / "same.txt").write_text("second")
    monkeypatch.setattr(session, "_default_cwd", second)
    assert server.read_text(path="same.txt", cwd="a")["content"] == "first"
    assert server.read_text(path="same.txt")["content"] == "legacy"
    assert server.read_text(path=str(second / "same.txt"), cwd="a")["content"] == "second"
    server.write_file("new.txt", "new", cwd="a")
    assert (first / "new.txt").read_text() == "new"
    result = server.apply_patch("*** Begin Patch\n*** Update File: new.txt\n*** Move to: moved.txt\n@@\n-new\n+changed\n*** End Patch", cwd="a")
    assert result["success"]
    assert (first / "moved.txt").read_text().strip() == "changed"
    assert server.search(mode="text", query="changed", cwd="a")["resolved_cwd"] == str(first.resolve())
    assert server.list_files(cwd="a")["resolved_cwd"] == str(first.resolve())
    assert server.list_skills(cwd="a", include_global=False)["resolved_cwd"] == str(first.resolve())


@pytest.mark.parametrize("text", ["", "a\nb\n", "中文😀" * 100 + "\r\nEND", "x" * 300 + "\nMIDDLE\nLAST\n", "\n\nend"])
def test_cursor_roundtrip_without_skipping_long_lines(tmp_path, text):
    path = tmp_path / "file.txt"
    path.write_bytes(text.encode())
    result = read(path)
    pieces = []
    cursors = set()
    while True:
        assert result["success"], result
        assert len(result["content"].encode()) <= 32
        pieces.append(result["content"] + result["separator_after"])
        if not result["truncated"]:
            break
        cursor = result["next_cursor"]
        assert cursor not in cursors
        cursors.add(cursor)
        if result["partial_line"]:
            assert result["next_offset"] is None
        result = read(path, cursor=cursor)
    assert "".join(pieces) == text.replace("\r\n", "\n")


def test_cursor_version_path_and_arguments(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("x" * 100)
    b.write_text("y" * 100)
    cursor = read(a)["next_cursor"]
    assert read(b, cursor=cursor)["error"]["code"] == "invalid_cursor"
    assert read(a, cursor="nonsense")["error"]["code"] == "invalid_cursor"
    a.write_text("z" * 100)
    assert read(a, cursor=cursor)["error"]["code"] == "file_changed"
    assert read_file(a, offset=1, limit=0, max_lines=200, max_bytes=32)["success"] is False
    assert read_file(a, offset=1, limit=2001, max_lines=200, max_bytes=32)["success"] is False


def test_batch_budget_missing_file_and_deferred(tmp_path):
    paths = [tmp_path / name for name in ["missing", "a", "b", "c"]]
    for p in paths[1:]:
        p.write_text("x" * 50)
    result = read_files(paths, offset=None, limit=None, max_lines=200, max_bytes=32, total_max_bytes=48)
    assert result["bytes_returned"] <= 48
    assert result["results"][0]["error"]["code"] == "file_not_found"
    assert result["results"][-1]["deferred"]


def test_numbered_long_line_respects_budget(tmp_path):
    p = tmp_path / "x"
    p.write_text("中" * 40 + "\nend")
    result = read(p, include_line_numbers=True)
    assert result["content"].startswith("1: ")
    following = read(p, cursor=result["next_cursor"], include_line_numbers=True)
    assert not following["content"].startswith("1: ")
    assert len(following["content"].encode()) <= 32


def test_read_is_streamed_and_encoding_errors_are_explicit(tmp_path, monkeypatch):
    path = tmp_path / "large"
    path.write_text("x" * 100000)
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("whole-file read"))
    assert read(path)["partial_line"]
    bad = tmp_path / "bad"
    with bad.open("wb") as handle:
        handle.write(b"invalid\xff")
    assert read(bad)["error"]["code"] == "not_text_file"


def test_cursor_start_conflict_and_batch_count_limit(tmp_path):
    path = tmp_path / "large"
    path.write_text("x" * 100)
    cursor = read(path)["next_cursor"]
    result = read_file(path, offset=1, limit=1, max_lines=200, max_bytes=32, cursor=cursor)
    assert result["error"]["code"] == "invalid_arguments"
    result = read_files([path] * 17, offset=None, limit=None, max_lines=200, max_bytes=32)
    assert result["error"]["code"] == "too_many_files"
