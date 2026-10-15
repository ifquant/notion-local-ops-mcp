from __future__ import annotations

import json
import base64
import math
import re
import shutil
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import LOG_READ_BYTES

TERMINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TaskStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        self._lock = threading.RLock()

    def _task_dir(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or re.fullmatch(r"[0-9a-f]{12}", task_id) is None:
            raise ValueError("Invalid task_id; expected 12 lowercase hexadecimal characters.")
        parent = self.root / "tasks"
        path = parent / task_id
        if parent.is_symlink() or path.is_symlink():
            raise ValueError("Task directories must not be symbolic links.")
        for name in ("meta.json", "stdout.log", "stderr.log", "summary.txt"):
            if (path / name).is_symlink():
                raise ValueError("Task files must not be symbolic links.")
        return path

    def _meta_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "meta.json"

    def _stdout_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "stdout.log"

    def _stderr_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "stderr.log"

    def _summary_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "summary.txt"

    def _write_text(self, path: Path, content: str) -> None:
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(content, encoding="utf-8")
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        temp_path.replace(path)

    def create(
        self,
        *,
        task: str,
        executor: str,
        cwd: str,
        timeout: int | None = None,
        context_files: list[str] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        with self._lock:
            task_id = uuid.uuid4().hex[:12]
            task_dir = self._task_dir(task_id)
            task_dir.mkdir(parents=True, exist_ok=True)
            try:
                task_dir.parent.chmod(0o700)
                task_dir.chmod(0o700)
            except OSError:
                pass
            payload = {
                "task_id": task_id,
                "task": task,
                "executor": executor,
                "cwd": cwd,
                "timeout": timeout,
                "context_files": context_files or [],
                "status": "queued",
                "created_at": _now(),
                "updated_at": _now(),
            }
            if metadata:
                payload.update(metadata)
            self._write_text(self._meta_path(task_id), json.dumps(payload, indent=2))
            self._write_text(self._stdout_path(task_id), "")
            self._write_text(self._stderr_path(task_id), "")
            self._write_text(self._summary_path(task_id), "")
            return payload

    def get(self, task_id: str) -> dict[str, object]:
        with self._lock:
            return json.loads(self._meta_path(task_id).read_text(encoding="utf-8"))

    def update(self, task_id: str, **fields: object) -> dict[str, object]:
        with self._lock:
            payload = self.get(task_id)
            if payload["status"] in TERMINAL_TASK_STATUSES:
                return payload
            payload.update(fields)
            payload["updated_at"] = _now()
            if payload["status"] in TERMINAL_TASK_STATUSES:
                payload["finished_at"] = _now()
            self._write_text(self._meta_path(task_id), json.dumps(payload, indent=2))
            return payload

    def write_logs(self, task_id: str, *, stdout: str, stderr: str) -> None:
        with self._lock:
            self._write_text(self._stdout_path(task_id), stdout)
            self._write_text(self._stderr_path(task_id), stderr)

    def write_summary(self, task_id: str, summary: str) -> None:
        with self._lock:
            self._write_text(self._summary_path(task_id), summary)

    def read_stdout(self, task_id: str) -> str:
        with self._lock:
            return self._stdout_path(task_id).read_text(encoding="utf-8")

    def read_stderr(self, task_id: str) -> str:
        with self._lock:
            return self._stderr_path(task_id).read_text(encoding="utf-8")

    def read_summary(self, task_id: str) -> str:
        with self._lock:
            with self._summary_path(task_id).open(encoding="utf-8") as handle:
                return handle.read(1000).strip()

    def append_log(self, task_id: str, stream: str, data: bytes) -> None:
        if stream not in {"stdout", "stderr"}:
            raise ValueError("Invalid output stream")
        with self._lock:
            path = self._task_dir(task_id) / f"{stream}.log"
            with path.open("ab") as handle:
                handle.write(data)

    def snapshot(self, task_id: str, *, cursor: str | None = None, max_bytes: int = LOG_READ_BYTES) -> dict[str, object]:
        if type(max_bytes) is not int or not 4 <= max_bytes <= LOG_READ_BYTES:
            raise ValueError(f"max_bytes must be 4..{LOG_READ_BYTES}")
        with self._lock:
            meta = self.get(task_id)
            paths = [self._stdout_path(task_id), self._stderr_path(task_id)]
            sizes = [p.stat().st_size for p in paths]
            offsets = [0, 0]
            if cursor not in (None, "start"):
                try:
                    if len(cursor) > 1024:
                        raise ValueError()
                    data = json.loads(base64.urlsafe_b64decode(cursor))
                    offsets = data["offsets"]
                    if data["task_id"] != task_id or len(offsets) != 2:
                        raise ValueError()
                    if any(type(n) is not int or n < 0 or n > size for n, size in zip(offsets, sizes)):
                        raise ValueError()
                except (ValueError, TypeError, KeyError):
                    raise ValueError("Invalid log cursor or cursor belongs to another task.") from None
            remaining = max_bytes
            chunks = []
            for index, path in enumerate(paths):
                with path.open("rb") as handle:
                    offset = offsets[index] if cursor is not None else max(0, sizes[index] - min(4000, remaining))
                    handle.seek(offset)
                    raw = handle.read(min(remaining, sizes[index] - offset))
                    if cursor is None:
                        # Tail may start in the middle of a UTF-8 code point.
                        while raw and raw[0] & 0xC0 == 0x80:
                            raw, offset = raw[1:], offset + 1
                    elif raw and raw[0] & 0xC0 == 0x80:
                        raise ValueError("Log cursor is not on a UTF-8 character boundary.")
                    text = raw.decode("utf-8", errors="ignore")
                    consumed = len(text.encode("utf-8"))
                    offsets[index] = offset + consumed
                    remaining -= consumed
                    chunks.append(text)
            meta.update({
                "summary": self.read_summary(task_id),
                "stdout_tail": chunks[0], "stderr_tail": chunks[1],
                "stdout": chunks[0], "stderr": chunks[1],
                "next_cursor": base64.urlsafe_b64encode(json.dumps({"task_id": task_id, "offsets": offsets}).encode()).decode(),
                "has_more": any(offset < size for offset, size in zip(offsets, sizes)),
                "completed": meta["status"] in TERMINAL_TASK_STATUSES,
                "artifacts": [],
            })
            meta.setdefault("execution_timed_out", bool(meta.get("timed_out", False)))
            return meta

    def purge_tasks(self, *, older_than_seconds: float, dry_run: bool = False) -> dict[str, object]:
        """Remove task directories whose ``updated_at`` is older than the threshold."""
        if not math.isfinite(older_than_seconds) or older_than_seconds < 0:
            raise ValueError("older_than_seconds must be finite and non-negative")
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=max(float(older_than_seconds), 0.0))
        tasks_root = self.root / "tasks"
        scanned = 0
        purged = 0
        purged_ids: list[str] = []
        skipped: list[dict[str, str]] = []

        with self._lock:
            if tasks_root.is_symlink():
                raise ValueError("Task root must not be a symbolic link")
            if not tasks_root.exists():
                return {
                    "success": True,
                    "scanned": 0,
                    "purged": 0,
                    "task_ids": [],
                    "dry_run": dry_run,
                    "cutoff": cutoff.isoformat(),
                }
            for task_dir in sorted(tasks_root.iterdir()):
                if not task_dir.is_dir():
                    continue
                scanned += 1
                task_id = task_dir.name
                meta_path = task_dir / "meta.json"
                should_purge = False
                try:
                    self._task_dir(task_id)
                    payload = json.loads(meta_path.read_text(encoding="utf-8"))
                    if payload.get("status") not in TERMINAL_TASK_STATUSES:
                        skipped.append({"task_id": task_id, "reason": "not_terminal"})
                        continue
                    updated_raw = str(payload.get("finished_at") or payload.get("updated_at") or payload.get("created_at") or "")
                    updated_at = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
                    if updated_at.tzinfo is None:
                        updated_at = updated_at.replace(tzinfo=UTC)
                    should_purge = updated_at < cutoff
                except Exception:
                    skipped.append({"task_id": task_id, "reason": "invalid_metadata_or_path"})
                    continue

                if not should_purge:
                    continue
                purged += 1
                purged_ids.append(task_id)
                if not dry_run:
                    shutil.rmtree(task_dir)

        return {
            "success": True,
            "scanned": scanned,
            "purged": purged,
            "task_ids": purged_ids,
            "dry_run": dry_run,
            "cutoff": cutoff.isoformat(),
            "skipped": skipped,
        }
