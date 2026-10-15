from __future__ import annotations

import difflib
import base64
import json
import mimetypes
import os
import subprocess
from fnmatch import fnmatch
from pathlib import Path

from .config import READ_MAX_LINES, READ_MAX_BYTES, READ_BATCH_FILES, READ_BATCH_BYTES
from .workspaces import current_scope


# Directories we almost never want to return to an agent. Used as a default
# prune list when recursing so that a single `list_files(recursive=True)` on a
# project root doesn't explode the context with vendor / cache content.
DEFAULT_EXCLUDE_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".cache",
        "dist",
        "build",
        "target",
        ".next",
        ".nuxt",
        ".idea",
        ".vscode",
        ".gradle",
        ".terraform",
        ".DS_Store",
    }
)


def _error(code: str, message: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "success": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
    payload.update(extra)
    return payload


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if b"\x00" in raw[:1024]:
        raise ValueError("Binary files are not supported.")
    return raw.decode("utf-8", errors="replace")


def _render_lines(
    lines: list[str],
    *,
    start_line: int,
    include_line_numbers: bool,
) -> str:
    if not include_line_numbers:
        return "\n".join(lines)
    return "\n".join(f"{start_line + index}: {line}" for index, line in enumerate(lines))


def _find_git_root(start: Path) -> Path | None:
    current = start if start.is_dir() else start.parent
    while True:
        if (current / ".git").exists():
            return current
        if current == current.parent:
            return None
        current = current.parent


def _git_tracked_allowed_paths(repo_root: Path) -> set[Path] | None:
    """Return the set of absolute paths inside ``repo_root`` that are either
    tracked or untracked-but-not-ignored, plus every parent directory leading
    to them. Returns ``None`` if git is unavailable or the command fails.

    The directory-parents expansion is important because we filter entries by
    membership in this set, and we still want to return the parent directories
    when a child file is allowed.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None

    repo_root_resolved = repo_root.resolve()
    allowed: set[Path] = set()
    for line in result.stdout.splitlines():
        if not line:
            continue
        full = (repo_root_resolved / line).resolve()
        allowed.add(full)
        for parent in full.parents:
            allowed.add(parent)
            if parent == repo_root_resolved:
                break
    return allowed


def _iter_filtered(
    root: Path,
    *,
    recursive: bool,
    include_hidden: bool,
    exclude_dir_names: frozenset[str],
    exclude_patterns: tuple[str, ...],
    allowed: set[Path] | None,
):
    """Yield entries under ``root`` applying prune rules. When ``recursive`` is
    False only direct children are returned.
    """
    root_resolved = root.resolve()

    def matches_exclude_pattern(entry: Path) -> bool:
        if not exclude_patterns:
            return False
        name = entry.name
        try:
            rel = str(entry.relative_to(root))
        except ValueError:
            rel = name
        return any(fnmatch(name, pat) or fnmatch(rel, pat) for pat in exclude_patterns)

    def entry_allowed(entry: Path) -> bool:
        scope = current_scope.get()
        if scope is not None:
            try:
                if not entry.resolve().is_relative_to(scope.root):
                    return False
            except (OSError, RuntimeError):
                return False
        if allowed is None:
            return True
        try:
            return entry.resolve() in allowed
        except OSError:
            return False

    if not recursive:
        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            name = entry.name
            if not include_hidden and name.startswith("."):
                continue
            if entry.is_dir() and name in exclude_dir_names:
                continue
            if matches_exclude_pattern(entry):
                continue
            if not entry_allowed(entry):
                continue
            yield entry
        return

    # Recursive walk with directory pruning.
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirpath_p = Path(dirpath)
        # Prune in-place so os.walk will not descend into them.
        pruned: list[str] = []
        for d in sorted(dirnames):
            if not include_hidden and d.startswith("."):
                continue
            if d in exclude_dir_names:
                continue
            subdir = dirpath_p / d
            if matches_exclude_pattern(subdir):
                continue
            if not entry_allowed(subdir):
                continue
            pruned.append(d)
        dirnames[:] = pruned

        # Yield directories first (sorted), then files.
        for d in pruned:
            if dirpath_p == root:
                # root-level dir already yielded via iteration? we still yield
                # so list matches non-recursive behavior.
                pass
            yield dirpath_p / d
        for f in sorted(filenames):
            if not include_hidden and f.startswith("."):
                continue
            full = dirpath_p / f
            if matches_exclude_pattern(full):
                continue
            if not entry_allowed(full):
                continue
            yield full


def list_files(
    path: Path,
    *,
    recursive: bool,
    limit: int,
    offset: int = 0,
    include_hidden: bool = False,
    respect_gitignore: bool = True,
    exclude_patterns: list[str] | tuple[str, ...] | None = None,
) -> dict[str, object]:
    """List entries under ``path``.

    Defaults are tuned for agent use: hidden entries and common junk dirs
    (``.git``, ``.venv``, ``node_modules``, ``__pycache__``, ...) are excluded,
    and when ``path`` is inside a git repository the result is filtered to
    tracked + untracked-but-not-ignored paths. Pass ``include_hidden=True`` or
    ``respect_gitignore=False`` to disable those filters; add more patterns via
    ``exclude_patterns``.
    """
    if not path.exists():
        return _error("path_not_found", f"Path not found: {path}", resolved_path=str(path))
    if not path.is_dir():
        return _error("not_a_directory", f"Path is not a directory: {path}", resolved_path=str(path))

    patterns: tuple[str, ...] = tuple(exclude_patterns or ())

    allowed: set[Path] | None = None
    gitignore_applied = False
    if respect_gitignore:
        repo_root = _find_git_root(path)
        if repo_root is not None:
            allowed = _git_tracked_allowed_paths(repo_root)
            if allowed is not None:
                gitignore_applied = True

    entries_iter = _iter_filtered(
        path,
        recursive=recursive,
        include_hidden=include_hidden,
        exclude_dir_names=DEFAULT_EXCLUDE_DIR_NAMES,
        exclude_patterns=patterns,
        allowed=allowed,
    )

    # Materialize with stable ordering. We already sort at each level inside
    # _iter_filtered, but recursive walks interleave dirs/files per directory;
    # for deterministic pagination we sort the final list by string path.
    entries_all = sorted(entries_iter, key=lambda item: str(item))

    start = max(offset, 0)
    selected = entries_all[start:]
    entries: list[dict[str, object]] = []
    truncated = False
    for index, entry in enumerate(selected):
        if limit != 0 and index >= limit:
            truncated = True
            break
        try:
            stat_result = entry.stat()
            size = stat_result.st_size if entry.is_file() else None
            mtime = stat_result.st_mtime
        except OSError:
            size = None
            mtime = None
        entries.append(
            {
                "name": entry.name,
                "path": str(entry),
                "is_dir": entry.is_dir(),
                "size": size,
                "mtime": mtime,
            }
        )
    return {
        "success": True,
        "base_path": str(path),
        "entries": entries,
        "truncated": truncated,
        "next_offset": start + len(entries) if truncated else None,
        "filters": {
            "include_hidden": include_hidden,
            "respect_gitignore": respect_gitignore,
            "gitignore_applied": gitignore_applied,
            "exclude_patterns": list(patterns),
        },
    }


def read_file(
    path: Path,
    *,
    offset: int | None,
    limit: int | None,
    max_lines: int,
    max_bytes: int,
    include_line_numbers: bool = False,
    cursor: str | None = None,
) -> dict[str, object]:
    if not path.exists():
        return _error("file_not_found", f"File not found: {path}", resolved_path=str(path))
    if not path.is_file():
        return _error("not_a_file", f"Path is not a file: {path}", resolved_path=str(path))

    start = 1 if offset is None else offset
    line_limit = max_lines if limit is None else limit
    if (not isinstance(start, int) or isinstance(start, bool) or start < 1
            or not isinstance(line_limit, int) or isinstance(line_limit, bool)
            or not 1 <= line_limit <= READ_MAX_LINES or max_bytes < 1):
        return _error("invalid_arguments", f"start_line must be positive; line_limit must be 1..{READ_MAX_LINES}.")
    if cursor is not None and offset is not None:
        return _error("invalid_arguments", "cursor and start_line are mutually exclusive.")
    budget = min(max_bytes, READ_MAX_BYTES)
    current_line, column, position = start, 0, 0
    parts: list[str] = []
    used = 0
    separator_after = ""
    try:
        stat = path.stat()
        version = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
        if cursor is not None:
            try:
                if len(cursor) > 8192:
                    raise ValueError()
                data = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
                if data["path"] != str(path.resolve()):
                    raise ValueError()
                position, current_line, column = data["position"], data["line"], data["column"]
                if any(type(n) is not int or n < 0 for n in (position, current_line, column)) or current_line < 1:
                    raise ValueError()
                if data["version"] != version:
                    return _error("file_changed", "File changed since the previous page; restart reading.", path=str(path))
                start = current_line
            except (ValueError, KeyError, TypeError, UnicodeError):
                return _error("invalid_cursor", "Invalid cursor or cursor belongs to another file.")
        with path.open("r", encoding="utf-8", errors="strict", newline=None) as handle:
            if cursor is not None:
                handle.seek(position)
            else:
                # Bounded chunks even when skipped lines are extremely long.
                skipped = 1
                while skipped < start:
                    chunk = handle.readline(8192)
                    if not chunk:
                        break
                    skipped += chunk.endswith("\n")
            while current_line - start < line_limit:
                before = handle.tell()
                char = handle.read(1)
                if not char:
                    break
                if char == "\x00":
                    return _error("not_text_file", "Binary files are not supported.", path=str(path))
                prefix = f"{current_line}: " if include_line_numbers and column == 0 else ""
                size = len((prefix + char).encode("utf-8"))
                if used + size > budget:
                    handle.seek(before)
                    break
                parts.append(prefix + char)
                used += size
                if char == "\n":
                    current_line += 1
                    column = 0
                else:
                    column += 1
            position = handle.tell()
            truncated = bool(handle.read(1))
        after = path.stat()
        if version != [after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns]:
            return _error("file_changed", "File changed while reading; restart reading.", path=str(path))
    except UnicodeError:
        return _error("not_text_file", "Invalid UTF-8; convert the file encoding before reading.", path=str(path))
    except (OSError, ValueError, OverflowError) as exc:
        return _error("read_failed", str(exc)[:1000], path=str(path))
    if truncated and not parts:
        return _error("output_budget_too_small", "Budget cannot fit the next character and line prefix.", path=str(path))
    content = "".join(parts)
    if content.endswith("\n"):
        content = content[:-1]
        separator_after = "\n"
    next_cursor = None
    if truncated:
        next_cursor = base64.urlsafe_b64encode(json.dumps({
            "path": str(path.resolve()), "version": version, "position": position,
            "line": current_line, "column": column,
        }, separators=(",", ":")).encode()).decode()

    language = (mimetypes.guess_type(str(path))[0] or "").split("/")[-1] or path.suffix.lstrip(".")
    if not language:
        language = "text"
    end_line = current_line - 1 if column == 0 else current_line
    if not parts:
        end_line = start - 1

    return {
        "success": True,
        "path": str(path),
        "content": content,
        "truncated": truncated,
        "next_offset": current_line if truncated and column == 0 else None,
        "next_cursor": next_cursor,
        "partial_line": truncated and column != 0,
        "separator_after": separator_after,
        "bytes_returned": len(content.encode("utf-8")),
        "file_version": version,
        "truncation_reason": ("line_limit" if current_line - start >= line_limit else "byte_limit") if truncated else None,
        "offset_unit": "lines",
        "start_line": start,
        "end_line": end_line,
        "language": language,
        "include_line_numbers": include_line_numbers,
    }


def read_files(
    paths: list[Path],
    *,
    offset: int | None,
    limit: int | None,
    max_lines: int,
    max_bytes: int,
    include_line_numbers: bool = False,
    total_max_bytes: int = READ_BATCH_BYTES,
) -> dict[str, object]:
    if len(paths) > READ_BATCH_FILES:
        return _error("too_many_files", f"Read at most {READ_BATCH_FILES} files per call.", limit=READ_BATCH_FILES)
    results = []
    if type(total_max_bytes) is not int or total_max_bytes <= 0:
        return _error("invalid_arguments", "total_max_bytes must be positive")
    remaining = min(total_max_bytes, READ_BATCH_BYTES)
    for path in paths:
        if remaining < 16:
            results.append({"success": False, "path": str(path), "deferred": True,
                            "reason": "batch_byte_budget", "next_start_line": offset or 1})
            continue
        result = read_file(
            path,
            offset=offset,
            limit=limit,
            max_lines=max_lines,
            max_bytes=min(max_bytes, remaining),
            include_line_numbers=include_line_numbers,
        )
        remaining -= len(str(result.get("content", "")).encode("utf-8"))
        results.append(result)
    return {
        "success": all(result.get("success") is True for result in results),
        "results": results,
        "bytes_returned": min(total_max_bytes, READ_BATCH_BYTES) - remaining,
    }


def write_file(path: Path, *, content: str, dry_run: bool = False) -> dict[str, object]:
    if not dry_run:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return _error("write_failed", str(exc)[:1000], path=str(path), written=False,
                          may_be_partially_written=True, requires_reread=True)
    return {
        "success": True,
        "path": str(path),
        "bytes_written": len(content.encode("utf-8")),
        "dry_run": dry_run,
        "written": not dry_run,
    }


def _line_numbers_of(original: str, needle: str) -> list[int]:
    """Return 1-based line numbers at which ``needle`` starts in ``original``."""
    positions: list[int] = []
    start = 0
    while True:
        idx = original.find(needle, start)
        if idx < 0:
            break
        positions.append(original.count("\n", 0, idx) + 1)
        start = idx + max(len(needle), 1)
    return positions


def _fuzzy_candidates(
    original: str, needle: str, *, k: int = 3
) -> list[dict[str, object]]:
    """Find the top ``k`` line windows in ``original`` that most resemble
    ``needle``. Returned entries include a 1-based ``line`` and a short
    ``snippet`` preview so callers can show “did you mean this?” hints.
    """
    needle_lines = needle.splitlines() or [""]
    window_size = max(len(needle_lines), 1)
    all_lines = original.splitlines()
    if not all_lines:
        return []

    scored: list[tuple[float, int, str]] = []
    for i in range(0, max(len(all_lines) - window_size + 1, 1)):
        window = "\n".join(all_lines[i : i + window_size])
        ratio = difflib.SequenceMatcher(None, window, needle, autojunk=False).ratio()
        if ratio <= 0.0:
            continue
        scored.append((ratio, i + 1, window))

    scored.sort(key=lambda item: item[0], reverse=True)
    suggestions: list[dict[str, object]] = []
    for ratio, line_no, snippet in scored[:k]:
        # Keep previews short so we do not blow up the response size.
        preview = snippet if len(snippet) <= 400 else snippet[:400] + "\u2026"
        suggestions.append(
            {
                "line": line_no,
                "similarity": round(ratio, 3),
                "snippet": preview,
            }
        )
    return suggestions


def replace_in_file(
    path: Path,
    *,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    if not path.exists():
        return _error("file_not_found", f"File not found: {path}", resolved_path=str(path))
    if not path.is_file():
        return _error("not_a_file", f"Path is not a file: {path}", resolved_path=str(path))

    try:
        original = _read_text(path)
    except ValueError as exc:
        return _error("not_text_file", str(exc), resolved_path=str(path))

    if not old_text:
        return _error(
            "empty_old_text",
            "old_text must not be empty; to write a file from scratch use write_file.",
            resolved_path=str(path),
        )

    occurrences = original.count(old_text)
    if occurrences == 0:
        return _error(
            "match_not_found",
            "old_text was not found. See `candidates` for the closest line windows in the file.",
            resolved_path=str(path),
            candidates=_fuzzy_candidates(original, old_text, k=3),
        )
    if occurrences > 1 and not replace_all:
        return _error(
            "match_not_unique",
            (
                f"old_text matched {occurrences} times; provide a unique fragment "
                "or pass replace_all=True."
            ),
            resolved_path=str(path),
            occurrences=occurrences,
            match_lines=_line_numbers_of(original, old_text),
        )

    replacements = occurrences if replace_all else 1
    replaced = original.replace(old_text, new_text, replacements)
    if not dry_run:
        path.write_text(replaced, encoding="utf-8")
    return {
        "success": True,
        "path": str(path),
        "replacements": replacements,
        "dry_run": dry_run,
        "written": not dry_run,
    }
