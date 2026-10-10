from __future__ import annotations

import asyncio
import importlib

import pytest


@pytest.fixture(autouse=True)
def _reload_gate(isolated_db):
    import events.reviewer_gate as gate_mod
    importlib.reload(gate_mod)
    yield gate_mod


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


def _check_suite_payload(
    conclusion: str = "success",
    sha: str = "abc123def",
    repo: str = "owner/repo",
    pr_number: int = 42,
    base_ref: str = "dev",
    head_ref: str = "feature/test",
) -> dict:
    return {
        "action": "completed",
        "check_suite": {
            "conclusion": conclusion,
            "head_sha": sha,
            "head_branch": head_ref,
            "pull_requests": [
                {
                    "number": pr_number,
                    "base": {"ref": base_ref},
                    "head": {"ref": head_ref, "sha": sha},
                },
            ],
        },
        "repository": {"full_name": repo},
    }


_DEFAULT_PR_INFO = {
    "number": 42,
    "mergeable": "MERGEABLE",
    "isDraft": False,
    "baseRefName": "dev",
    "baseRefOid": "base123def",
    "headRefOid": "abc123def",
    "labels": [],
    "title": "Contain reviewer scope",
    "body": "## Scope\nOnly review the changed release slice.",
}


def _call_on_backend_loop(callback, *args, **kwargs):
    async def invoke():
        loop = asyncio.get_running_loop()
        return await asyncio.to_thread(callback, *args, loop=loop, **kwargs)

    return asyncio.run(invoke())


def _call_gate(gate_mod, **kwargs):
    defaults = dict(
        repo="owner/repo",
        head_sha="abc123def",
        conclusion="success",
        pr_number=42,
        internal_event="webhook.github.check_suite_completed",
    )
    defaults.update(kwargs)
    _call_on_backend_loop(gate_mod.on_check_suite_completed, **defaults)
