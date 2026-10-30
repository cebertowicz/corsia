"""Refuse local-model setup when the server is not macOS."""

from fastapi import HTTPException

from llm import local_setup_gates


def require_local_model_host() -> None:
    if local_setup_gates.host_never_runs_local():
        raise HTTPException(409, detail={
            "code": "never_local_host",
            "message": "Local models don't run on this computer. Connect a provider.",
        })
