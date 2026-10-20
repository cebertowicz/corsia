"""Admin sign-in for the oMLX that local setup installs.

A fresh oMLX has no key: its admin routes answer 401 to everyone and sign-in is
refused (``POST /admin/api/login`` returns 400). Setup gives it a key CORSIA
generated and registered first. It then confirms that the managed server remains
loopback-only before oMLX applies ``skip_api_key_verification``. oMLX
honors that flag only on a loopback-only bind (``omlx/utils/network.py``,
``network_auth_error``), so it opens the admin and ``/v1`` routes to this Mac
alone, and CORSIA keeps calling ``/v1`` with the Bearer it already used.

The person's own oMLX never comes through here: its flights write nothing to it.
Neither the key nor any cookie is logged, returned, or put in an error message.
"""

from __future__ import annotations

import ipaddress
import subprocess

from packaging.version import InvalidVersion, Version

from llm.managed_omlx_binary import managed_omlx_executable
from llm.providers.omlx_endpoints import OmlxAdminError, admin_request, setup_admin_key

SKIP_AUTH_SETTING = "skip_api_key_verification"
NOT_LOOPBACK = "oMLX is listening beyond this Mac, so CORSIA will not change its sign-in settings."
NO_SETTINGS = "oMLX did not report its settings."
# `network_auth_error` first shipped in oMLX v0.7.0.dev4 (upstream commit
# 09a7c4376a094ffe9329fb0cf131fea31e32b038). Older releases let a later bind
# change expose skip_api_key_verification beyond loopback, so setup must not write it.
# The starter's `prism_hadamard_qwen35` format first loads in oMLX v0.7.0rc1 (upstream
# commit d94d4cec, #3782); dev4 downloads it and then cannot run it. The later wins.
MINIMUM_OMLX = Version("0.7.0rc1")
UPDATE_OMLX_MESSAGE = "Update oMLX, then retry."


def _omlx_version(executable: str | None) -> str | None:
    """Return ``executable``'s usable version without changing its installation."""
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=15, check=False,
        )
        version = result.stdout.strip()
        if result.returncode != 0:
            return None
        Version(version)
        return version
    except (OSError, subprocess.TimeoutExpired, InvalidVersion):
        return None


def omlx_version() -> str | None:
    """Return CORSIA's managed oMLX version without changing its installation."""
    return _omlx_version(managed_omlx_executable() or "omlx")


def omlx_version_is_compatible(version: str | None) -> bool:
    """Whether a reported oMLX version keeps auth loopback-only and runs the starter model."""
    return Version(version) >= MINIMUM_OMLX if version is not None else False


def _omlx_supports_loopback_auth(version: str | None = None) -> bool:
    """Whether the installed oMLX preserves auth when its bind later changes."""
    return omlx_version_is_compatible(omlx_version() if version is None else version)


def _loopback_only(hosts: object) -> bool:
    """True when every host oMLX reports binding to is a loopback address."""
    parts = [part.strip() for part in str(hosts or "").split(",") if part.strip()]
    if not parts:
        return False
    for part in parts:
        if part == "localhost":
            continue
        try:
            address = ipaddress.ip_address(part)
        except ValueError:
            return False
        mapped = getattr(address, "ipv4_mapped", None)
        if not (address.is_loopback or (mapped is not None and mapped.is_loopback)):
            return False
    return True


async def secure_local_omlx(server_id: str, key: str) -> None:
    """Give a freshly installed oMLX the registered key and confirm it is loopback-only.

    The key is already stored on ``server_id``. ``setup-api-key`` succeeds once
    (200); a retry gets 400 and falls through to sign-in, which proves the stored
    key is the one oMLX holds. Nothing is overwritten, and any failure stops setup
    with a fixed message before the skip flag is written.
    """
    cfg = {"server_id": server_id}
    await setup_admin_key(cfg, key)
    response = await admin_request(cfg, "GET", "/global-settings")
    if response.status_code != 200:
        raise OmlxAdminError(f"oMLX admin sign-in failed (oMLX HTTP {response.status_code})")
    try:
        payload = response.json()
    except ValueError:
        raise OmlxAdminError(NO_SETTINGS) from None
    server = payload.get("server") if isinstance(payload, dict) else None
    if not isinstance(server, dict):
        raise OmlxAdminError(NO_SETTINGS)
    if not _loopback_only(server.get("host")):
        raise OmlxAdminError(NOT_LOOPBACK)
