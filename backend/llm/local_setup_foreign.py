"""One server-owned answer about all of the person's running oMLX processes."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Literal

from llm.local_setup_managed import OmlxProcess, foreign_omlx_processes

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
    if not isinstance(value, dict):
        return {}
    # Preserve an answer made by the earlier dev setup, without trusting a new PID.
    if isinstance(value.get("stop"), bool):
        return {"choice": "stop" if value["stop"] else "keep",
                "pids": [value.get("pid")], "applied": True}
    return value


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


def stop_foreign_omlx_processes(chosen: tuple[OmlxProcess, ...]) -> bool:
    """Stop every chosen PID only after the whole current personal set matches it.

    launchd jobs are booted out for this login; plain processes receive SIGTERM.
    No newly appeared process is covered by a previous answer.
    """
    current = foreign_omlx_processes()
    if current is None or set(current) != set(chosen):
        return False
    stopped_labels: set[str] = set()
    for process in current:
        if process.label and process.label in stopped_labels:
            continue
        if not _stop_process(process):
            return False
        if process.label:
            stopped_labels.add(process.label)
    return True


def foreign_omlx_refusal(code: str) -> dict[str, str]:
    """Give an inspection failure a log reference; choice errors need only their code."""
    if code != "foreign_omlx_unknown":
        return {"code": code}
    from api.cli_install import new_reference

    ref = new_reference()
    log.error("could not identify running oMLX processes reference=%s", ref)
    return {"code": code, "ref": ref}


def apply_foreign_omlx_choice(*, other_omlx: OtherOmlxChoice | None = None) -> str | None:
    """Apply one answer to the offered process set, or return an explicit refusal.

    Unknown inspection blocks the action without asking about or stopping any
    process. Keep both survives PID changes; Stop requires the exact offered set.
    """
    processes = foreign_omlx_processes()
    if processes is None:
        return "foreign_omlx_unknown"
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
    if not stop_foreign_omlx_processes(processes):
        return "foreign_omlx_changed"
    _write_choice("stop", pids, applied=True)
    return None
