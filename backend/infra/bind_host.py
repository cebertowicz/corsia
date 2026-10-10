"""The network exposure the user chose at setup, read in one place.

The machine bootstrap (``~/.corsia/config.json``) records two keys written by
``cli init`` and ``cli setup``:

- ``setup``: ``"local"`` (this computer only) or ``"server"`` (reached from
  other devices);
- ``bind_host``: the interface every CORSIA-owned listener uses, which must
  match the setup (``local`` → ``127.0.0.1``, ``server`` → ``0.0.0.0``).

An existing bootstrap that predates the choice is rewritten once with the
all-interface bind it already had. Only a missing bootstrap uses that legacy
default. Anything else (an unknown value, one key without the other, or a setup
whose bind exposes more or less than it says) fails closed with the command that
fixes it. Callers outside the running service refuse on that error; the running
service never crash-loops on it.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

LOCAL_BIND = "127.0.0.1"
SERVER_BIND = "0.0.0.0"
LEGACY_BIND = SERVER_BIND
SETUP_BINDS = {"local": LOCAL_BIND, "server": SERVER_BIND}
FIX_COMMAND = (
    "Run: corsia setup local   (this computer only)\n"
    "  or: corsia setup server  (reach it from other devices)"
)


class BindHostError(ValueError):
    """The recorded setup cannot be trusted to choose an interface."""

    def __init__(self, problem: str) -> None:
        super().__init__(f"{problem}\n{FIX_COMMAND}")


def _read_payload(path: Path) -> dict | None:
    """Return the bootstrap object, or None when no bootstrap exists."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise BindHostError(f"{path} is not valid JSON ({error.msg}).") from error
    if not isinstance(payload, dict):
        raise BindHostError(f"{path} must hold a JSON object.")
    return payload


def resolve_bind_host(path: Path) -> str:
    """Return the interface CORSIA listeners bind, or raise BindHostError."""
    bootstrap = Path(path)
    payload = _read_payload(bootstrap)
    if payload is None:
        # Remove this legacy default once every install writes a bootstrap.
        return LEGACY_BIND
    setup = payload.get("setup")
    bind = payload.get("bind_host")
    if setup is None and bind is None:
        payload.update(exposure_keys("server"))
        _write_payload(bootstrap, payload)
        return SERVER_BIND
    if setup not in SETUP_BINDS:
        raise BindHostError(f"Unknown setup {setup!r} in {path}.")
    if bind not in SETUP_BINDS.values():
        raise BindHostError(f"Unknown bind_host {bind!r} in {path}.")
    if SETUP_BINDS[setup] != bind:
        raise BindHostError(
            f"Setup {setup!r} does not match bind_host {bind!r} in {path}."
        )
    return bind


def exposure_keys(setup: str) -> dict[str, str]:
    """Return the bootstrap keys recording ``setup``."""
    if setup not in SETUP_BINDS:
        raise ValueError(f"setup must be one of {', '.join(SETUP_BINDS)}")
    return {"setup": setup, "bind_host": SETUP_BINDS[setup]}


def recorded_setup(path: Path) -> str | None:
    """Return the recorded setup when it is valid, else None."""
    try:
        payload = _read_payload(Path(path))
    except BindHostError:
        return None
    setup = (payload or {}).get("setup")
    return setup if setup in SETUP_BINDS else None


def _write_payload(bootstrap: Path, payload: dict) -> None:
    """Atomically save an already-validated bootstrap payload."""
    encoded = json.dumps(payload, indent=2) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        dir=bootstrap.parent, prefix=f".{bootstrap.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, bootstrap)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_setup(path: Path, setup: str) -> None:
    """Record ``setup`` in an existing bootstrap, keeping every other key."""
    bootstrap = Path(path)
    payload = _read_payload(bootstrap)
    if payload is None:
        raise FileNotFoundError(
            f"{bootstrap} does not exist yet. Run: corsia start"
        )
    payload.update(exposure_keys(setup))
    _write_payload(bootstrap, payload)
