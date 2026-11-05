"""Registration reviews authenticated scripts before deciding whether they may run."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from automation import script_access
from host_access.review_models import ReviewerIdentity, ScriptApplicationReview


@pytest.mark.asyncio
@pytest.mark.parametrize("auto", [False, True])
@pytest.mark.parametrize("verdict", ["allow", "reject", "ask_person"])
async def test_script_review_decides_registration(monkeypatch, tmp_path, auto, verdict):
    script = tmp_path / "task.py"
    script.write_text("print('ok')\n")
    sha = hashlib.sha256(script.read_bytes()).hexdigest()
    monkeypatch.setattr(script_access, "pin_script", lambda *args, **kwargs: (script, sha))
    monkeypatch.setattr(script_access, "get_host_access_auto_approve", lambda: auto)
    monkeypatch.setattr(script_access.reviewer_target, "resolve_reviewer_target", AsyncMock(return_value=SimpleNamespace(harness="native")))
    identity = ReviewerIdentity("test", "native", "test", "test", "test")
    review = ScriptApplicationReview(identity, verdict, "reason")
    reviewer = AsyncMock(return_value=review)
    delivery = AsyncMock(return_value=True)
    monkeypatch.setattr(script_access, "review_application", reviewer)
    monkeypatch.setattr(script_access, "deliver_to_thread", delivery)
    params = {"script_path": str(script), "purpose": "work", "user_request": "please do work"}
    if verdict == "reject":
        with pytest.raises(ValueError, match="rejected"):
            await script_access.review_script("owner", params)
        delivery.assert_awaited_once()
    else:
        path, digest, record = await script_access.review_script("owner", params)
        assert (path, digest) == (script, sha)
        approved = auto and verdict == "allow"
        assert record["state"] == ("approved" if approved else "pending")
        assert record["approved_sha256"] == (sha if approved else None)
        assert reviewer.await_args.args[1]["script"] == "print('ok')\n"
        assert reviewer.await_args.args[1]["user_request"] == "please do work"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["purpose", "user_request"])
async def test_script_review_requires_persons_request_and_purpose(missing):
    params = {"purpose": "work", "user_request": "please do work", "script_path": "x"}
    del params[missing]
    with pytest.raises(ValueError, match=missing):
        await script_access.review_script("owner", params)
