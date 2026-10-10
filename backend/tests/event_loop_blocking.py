"""Detect blocking work that runs on a thread driving an asyncio event loop.

``blocking_work_on_event_loop()`` records, while active, work whose duration
grows with data or with another process when it runs on a thread with a running
loop: process spawns, SQLite connection checkouts, file opens for content I/O,
directory enumeration, tree copies and sleeps. Work handed to
``asyncio.to_thread`` or ``run_in_db`` runs on a worker thread without a loop and
is not recorded. Single metadata syscalls (stat, unlink, chmod, mkdir, opening a
directory descriptor, exclusively creating an empty file) and module imports are
not recorded.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager

import infra.database as database

_BLOCKING_EVENTS = frozenset({
    "subprocess.Popen", "os.posix_spawn", "os.fork", "os.system", "open",
    "os.listdir", "os.scandir", "glob.glob", "pathlib.Path.glob", "pathlib.Path.rglob",
    "shutil.copyfile", "shutil.copytree", "shutil.rmtree", "time.sleep",
})
_IMPORT_MACHINERY = ("<frozen importlib._bootstrap", "<frozen zipimport>")
_NOT_CALLERS = ("/infra/database.py", "/tests/event_loop_blocking.py")
_recorders: list[list[str]] = []
_hook_installed = False


def _running_loop_thread() -> bool:
    events = sys.modules.get("asyncio.events")
    running_loop = getattr(events, "_get_running_loop", None)
    return running_loop is not None and running_loop() is not None


def _importing(frame) -> bool:
    while frame is not None:
        filename = frame.f_code.co_filename
        if filename.startswith(_IMPORT_MACHINERY) or filename.endswith("/linecache.py"):
            return True
        frame = frame.f_back
    return False


def _metadata_open(args: tuple) -> bool:
    """A directory descriptor, an exclusive empty-file creation, or a source import."""
    path, _mode, flags = args
    exclusive_create = os.O_CREAT | os.O_EXCL
    if isinstance(flags, int) and (
        flags & os.O_DIRECTORY or flags & exclusive_create == exclusive_create
    ):
        return True
    return isinstance(path, str) and path.endswith((".py", ".pyc"))


def _caller(frame) -> str:
    """Name the backend call chain, innermost first, that performed the work."""
    chain = []
    while frame is not None:
        filename = frame.f_code.co_filename
        if "/backend/" in filename and not filename.endswith(_NOT_CALLERS):
            chain.append(
                f"{filename.split('/backend/', 1)[1]}:{frame.f_lineno} {frame.f_code.co_name}"
            )
        frame = frame.f_back
    return " <- ".join(chain) or "<outside backend>"


def _audit(event: str, args: tuple) -> None:
    if not _recorders or event not in _BLOCKING_EVENTS or not _running_loop_thread():
        return
    if event == "open" and _metadata_open(args):
        return
    frame = sys._getframe(1)
    if _importing(frame):
        return
    for recorded in _recorders:
        recorded.append(f"{event} from {_caller(frame)}")


class _RecordingCheckout(database._NoOpClose):
    """The pooled-connection wrapper ``get_db`` returns, noting loop-thread checkouts."""

    def __init__(self, conn) -> None:
        if _recorders and _running_loop_thread():
            caller = _caller(sys._getframe(1))
            for recorded in _recorders:
                recorded.append(f"sqlite checkout from {caller}")
        super().__init__(conn)


@contextmanager
def blocking_work_on_event_loop(monkeypatch) -> Iterator[list[str]]:
    """Yield the blocking work recorded on event-loop threads while active."""
    global _hook_installed
    if not _hook_installed:
        sys.addaudithook(_audit)
        _hook_installed = True
    recorded: list[str] = []
    _recorders.append(recorded)
    monkeypatch.setattr(database, "_NoOpClose", _RecordingCheckout)
    try:
        yield recorded
    finally:
        _recorders.remove(recorded)
