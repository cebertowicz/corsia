"""One server-owned answer about all of the person's running oMLX processes."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Literal

from llm.managed_omlx_binary import OmlxProcess, foreign_omlx_processes

log = logging.getLogger(__name__)
OtherOmlxChoice = Literal["stop", "keep"]


def foreign_omlx_choice_path() -> Path:
    """Return the private server record of the offered PID set and the person's answer."""
    from infra.config import STATE_ROOT

    return Path(STATE_ROOT) / "foreign-omlx-choice.json"


def _read_choice() -> dict:
    try:
        value = json.loads(foreign_omlx_choice_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_choice(choice: str | None, pids: list[int], *, applied: bool) -> None:
    path = foreign_omlx_choice_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"choice": choice, "pids": pids, "applied": applied}), encoding="utf-8")


def foreign_omlx_answer() -> str:
    """The applied answer for setup status and the first wake; never inspect processes."""
    value = _read_choice()
    if value.get("applied") is True:
        if value.get("choice") == "keep":
            return "kept_both"
        if value.get("choice") == "stop":
            return "stopped_theirs"
    return "none"


def _stop_process(process: OmlxProcess) -> bool:
    try:
        if process.label:
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{process.label}"],
                           capture_output=True, text=True, timeout=30, check=True)
        else:
            os.kill(process.pid, 15)
    except (OSError, subprocess.SubprocessError):
        log.warning("could not stop the chosen personal oMLX process")
        return False
    return True


def stop_foreign_omlx_processes(chosen: tuple[OmlxProcess, ...]) -> Literal["stopped", "changed", "unknown", "failed"]:
    """Stop every chosen PID only after the whole current personal set matches it.

    launchd jobs are booted out for this login; plain processes receive SIGTERM.
    No newly appeared process is covered by a previous answer. An unreadable
    revalidation is kept distinct so setup can continue without stopping.
    """
    current = foreign_omlx_processes()
    if current is None:
        return "unknown"
    if set(current) != set(chosen):
        return "changed"
    stopped_labels: set[str] = set()
    for process in current:
        if process.label and process.label in stopped_labels:
            continue
        if not _stop_process(process):
            return "failed"
        if process.label:
            stopped_labels.add(process.label)
    return "stopped"


def _continue_after_unknown_inspection() -> None:
    """Record an unreadable process list without exposing a setup state to the user."""
    from infra.failure_reference import new_reference

    ref = new_reference("local")
    log.warning("could not identify running oMLX processes; continuing without stopping reference=%s", ref)


def apply_foreign_omlx_choice(*, other_omlx: OtherOmlxChoice | None = None) -> str | None:
    """Apply one answer to the offered process set, or return an explicit refusal.

    Unknown inspection continues setup without asking about or stopping any
    process. Keep both survives PID changes; Stop requires the exact offered set.
    """
    processes = foreign_omlx_processes()
    # Supervisor identity cannot make OS inspection readable; never stop an unverified process.
    if processes is None:
        _continue_after_unknown_inspection()
        return None
    if not processes:
        return None
    pids = sorted(process.pid for process in processes)
    record = _read_choice()
    remembered = record.get("choice")
    if other_omlx == "keep":
        _write_choice("keep", pids, applied=True)
        return None
    if other_omlx == "stop":
        if record.get("pids") != pids:
            _write_choice(None, pids, applied=False)
            return "foreign_omlx_changed"
        _write_choice("stop", pids, applied=False)
    elif remembered == "keep":
        return None
    elif remembered != "stop" or record.get("pids") != pids:
        _write_choice(None, pids, applied=False)
        return "consent_required"
    stop_result = stop_foreign_omlx_processes(processes)
    if stop_result == "unknown":
        _continue_after_unknown_inspection()
        return None
    if stop_result != "stopped":
        return "foreign_omlx_changed"
    _write_choice("stop", pids, applied=True)
    return None
