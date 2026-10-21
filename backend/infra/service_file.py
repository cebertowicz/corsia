"""Read the app's ``service.json``: the state root, workspace and port it records."""

from __future__ import annotations

import json
import os
import unicodedata
from pathlib import Path

SERVICE_FILE = "service.json"
_SERVICE_FILE_KEYS = ("state_dir", "workspace", "port")


def state_dir_is_valid(value: object) -> bool:
    """Whether ``service.json`` may name this folder as the state root.

    A string that starts with ``/`` and holds no control character (Unicode
    category Cc). The app shell applies the same rule in
    ``desktop/src-tauri/src/app_paths.rs`` (``valid_state_dir``), and a parity
    test holds the two together. A recorded state root that fails it is
    refused, never replaced by a different folder.
    """
    return (
        isinstance(value, str)
        and value.startswith("/")
        and not any(unicodedata.category(character) == "Cc" for character in value)
    )


def service_file_lines(path: Path) -> list[str]:
    """Parse the app's ``service.json`` into ``key=value`` lines for shell callers.

    The file is parsed, never executed. ``state_dir`` (see
    :func:`state_dir_is_valid`), ``workspace`` (an absolute, printable path)
    and ``port`` (1-65535) are honoured; anything else becomes a ``note=``
    line that never repeats a value. A file that cannot be read, or a
    ``state_dir`` that is not valid, becomes an ``error=`` line instead: the
    launcher refuses to start rather than open a different, empty state root.
    The bundle's launcher and its lifecycle script both read the file through
    this one function.
    """

    def name(value: object) -> str:
        text = str(value)
        return text if text.isprintable() and len(text) <= 64 else "<unprintable name>"

    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read(65536).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except Exception as error:
        return [f"error={SERVICE_FILE} unreadable: {name(type(error).__name__)}"]
    lines = [
        f"note={SERVICE_FILE}: ignoring unsupported key {name(key)}"
        for key in data
        if key not in _SERVICE_FILE_KEYS
    ]
    if state_dir_is_valid(data.get("state_dir")):
        lines.append(f"state_dir={data['state_dir']}")
    elif "state_dir" in data:
        lines.append(f"error={SERVICE_FILE} has an invalid state_dir")
    workspace = data.get("workspace")
    if isinstance(workspace, str) and workspace.startswith("/") and workspace.isprintable():
        lines.append(f"workspace={workspace}")
    elif "workspace" in data:
        lines.append(f"note={SERVICE_FILE}: ignoring an invalid workspace value")
    port = data.get("port")
    if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536:
        lines.append(f"port={port}")
    elif "port" in data:
        lines.append(f"note={SERVICE_FILE}: ignoring an invalid port value")
    return lines


def service_file_state_dir(path: Path) -> Path | None:
    """The state root ``service.json`` claims; None if absent, ValueError if unusable."""
    try:
        data = json.loads(path.read_bytes()[:65536])
    except FileNotFoundError:
        return None
    except Exception as error:
        raise ValueError(f"{SERVICE_FILE} unreadable") from error
    if not isinstance(data, dict):
        raise ValueError(f"{SERVICE_FILE} is not a JSON object")
    if "state_dir" not in data:
        return None
    state_dir = data["state_dir"]
    if not state_dir_is_valid(state_dir):
        raise ValueError(f"{SERVICE_FILE} has an invalid state_dir")
    return Path(os.path.realpath(state_dir))
