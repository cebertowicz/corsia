"""Nightly-loop entry point for the ordinary Update lifecycle."""

from __future__ import annotations

import os
from pathlib import Path

from api.settings_restart import prelaunch_settings_restart
from automation.restart_callbacks import callback_credential_hash, mint_callback_credential


async def launch_scheduled_update(trigger: str, target_version: str | None) -> str:
    """Run the approved Update lifecycle from the nightly loop without a human token."""
    if trigger not in {"tonight", "automatic"}:
        raise ValueError("scheduled update trigger is invalid")
    port = os.environ.get("BACKEND_PORT", "")
    if not port.isdigit():
        raise RuntimeError("scheduled update cannot launch: BACKEND_PORT is not a port number")
    from infra.config import LOGS_DIR

    credential = mint_callback_credential()
    restart_env = {
        **os.environ,
        "BACKEND_PORT": port,
        "CORSIA_RESTART_BACKEND_PORT": port,
    }
    corsia_script = Path(__file__).parent.parent.parent / "scripts" / "corsia"

    def restart_log_path(name: str) -> Path:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        return LOGS_DIR / name

    operation_id, _process = await prelaunch_settings_restart(
        None,
        "update",
        corsia_script,
        restart_env,
        ["restart"],
        restart_log_path,
        caller_token=credential,
        trigger=trigger,
        target_version=target_version,
        callback_hash=callback_credential_hash(credential),
    )
    return operation_id
