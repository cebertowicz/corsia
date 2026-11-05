"""Safely make an interactive-shell CLI visible to the CORSIA service."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from cli_location import BINARIES, login_shell_binary, service_path
from connections.vendor_accounts import VENDORS
from infra.auth import require_user

router = APIRouter(prefix="/api/providers/cli", tags=["providers"])

_invisible_binaries: dict[str, str | None] = {}


def cache_invisible_binary(vendor: str) -> str | None:
    """Refresh one recheck-only invisible binary observation."""
    if vendor not in VENDORS:
        return None
    try:
        found = login_shell_binary(vendor)
    except TimeoutError:
        found = None
    _invisible_binaries[vendor] = str(found) if found else None
    return _invisible_binaries[vendor]


def invisible_binary(vendor: str) -> str | None:
    return _invisible_binaries.get(vendor)


def link_cli(vendor: str) -> str:
    """Link a login-shell CLI into the service path, without replacing user files."""
    if vendor not in VENDORS:
        raise KeyError(vendor)
    binary = BINARIES[vendor]
    home = Path.home()
    try:
        source = login_shell_binary(vendor)
    except TimeoutError:
        return "timed_out"
    if source is None:
        return "not_found"
    destination = home / ".local/bin" / binary
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() and destination.resolve() == source:
        outcome = "already_linked"
    elif destination.exists() or destination.is_symlink():
        return "conflict"
    else:
        destination.symlink_to(source)
        outcome = "linked"
    if shutil.which(binary, path=service_path()) != str(destination):
        if outcome == "linked":
            destination.unlink()
        return "conflict"
    return outcome


@router.post("/{vendor}/link")
async def link_provider_cli(vendor: str, _user: dict = Depends(require_user)) -> dict[str, str | bool]:
    try:
        outcome = await asyncio.to_thread(link_cli, vendor)
    except KeyError:
        raise HTTPException(404, "Unknown CLI provider") from None
    if outcome in {"linked", "already_linked"}:
        _invisible_binaries.pop(vendor, None)
        from connections.source_state import check_harness_source
        from infra.state.ws import schedule_models_changed

        await check_harness_source(vendor)
        schedule_models_changed()
    return {"ok": outcome in {"linked", "already_linked"}, "outcome": outcome}
