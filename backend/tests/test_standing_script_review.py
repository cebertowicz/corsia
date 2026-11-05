"""Standing scripts use the application reviewer with their own verdicts."""
import pytest

from host_access.reviewer import STANDING_SCRIPT_REVIEW, review_application
from test_host_access_reviewer import _target


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["allow", "reject", "ask_person"])
@pytest.mark.parametrize("auto_approve", [False, True])
async def test_standing_script_review_uses_script_policy(verdict, auto_approve):
    seen = {}

    async def invoke(target, **kwargs):
        seen.update(kwargs)
        return {"schema_version": 1, "verdict": verdict, "reason": "A reason"}

    result = await review_application(
        _target(), {"purpose": "test", "user_request": "test", "trigger": "event", "script": "print(1)"},
        auto_approve=auto_approve, review_spec=STANDING_SCRIPT_REVIEW, invoke=invoke,
    )
    assert result.verdict == verdict
    assert "registers a standing script" in seen["system_prompt"]
    assert "activates the lease" not in seen["system_prompt"]
    assert "quoted untrusted data" in seen["system_prompt"]
    assert set(seen["output_schema"]["properties"]["verdict"]["enum"]) == {"allow", "reject", "ask_person"}
